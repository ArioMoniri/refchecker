"""Tests for the "Send to Zotero" relay (backend/zotero.py) and its endpoint.

The relay is a thin server-side forwarder to a Zotero desktop app on the
loopback connector — the browser can't read the connector's cross-origin
response, so the POST happens here. These tests stub httpx so nothing touches
a real network / a real Zotero.
"""
import asyncio

import pytest

from backend import zotero


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    """Async-context httpx.AsyncClient stand-in that answers by URL suffix."""

    def __init__(self, ping_status=200, save_status=201, save_raises=False):
        self._ping = ping_status
        self._save = save_status
        self._save_raises = save_raises
        self.saved_payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        if url.endswith("/connector/ping"):
            if self._ping is None:
                raise RuntimeError("connection refused")
            return _FakeResp(self._ping)
        # saveItems
        if self._save_raises:
            raise RuntimeError("boom")
        self.saved_payload = kwargs.get("json")
        return _FakeResp(self._save)


def _patch_httpx(monkeypatch, **client_kwargs):
    """Make every httpx.AsyncClient() return a fresh fake configured alike."""
    import httpx

    created = []

    def _factory(*_a, **_k):
        c = _FakeClient(**client_kwargs)
        created.append(c)
        return c

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return created


ITEMS = [
    {"itemType": "journalArticle", "title": "A", "DOI": "10.1/a"},
    {"itemType": "preprint", "title": "B", "archiveID": "arXiv:2101.00001"},
]


def test_send_items_success(monkeypatch):
    _patch_httpx(monkeypatch, ping_status=200, save_status=201)
    result = asyncio.run(zotero.send_items(ITEMS))
    assert result["ok"] is True
    assert result["sent"] == 2
    assert result["connector_available"] is True
    assert "2 references" in result["detail"]


def test_send_items_singular_detail(monkeypatch):
    _patch_httpx(monkeypatch, ping_status=200, save_status=200)
    result = asyncio.run(zotero.send_items(ITEMS[:1]))
    assert result["ok"] is True
    assert result["sent"] == 1
    assert "1 reference " in result["detail"]  # singular, no trailing 's'


def test_send_items_connector_unavailable(monkeypatch):
    # Ping refuses -> Zotero isn't running here -> caller does the RIS fallback.
    _patch_httpx(monkeypatch, ping_status=None)
    result = asyncio.run(zotero.send_items(ITEMS))
    assert result["ok"] is False
    assert result["connector_available"] is False
    assert result["sent"] == 0


def test_send_items_empty(monkeypatch):
    _patch_httpx(monkeypatch, ping_status=200)
    result = asyncio.run(zotero.send_items([]))
    assert result["ok"] is False
    assert result["connector_available"] is None
    assert "No references" in result["detail"]


def test_send_items_save_rejected(monkeypatch):
    # Ping ok but saveItems returns a 4xx: connector is up, save failed.
    _patch_httpx(monkeypatch, ping_status=200, save_status=400)
    result = asyncio.run(zotero.send_items(ITEMS))
    assert result["ok"] is False
    assert result["connector_available"] is True
    assert "HTTP 400" in result["detail"]


def test_send_items_save_transport_error(monkeypatch):
    _patch_httpx(monkeypatch, ping_status=200, save_raises=True)
    result = asyncio.run(zotero.send_items(ITEMS))
    assert result["ok"] is False
    assert result["connector_available"] is True
    assert "Couldn't reach Zotero" in result["detail"]


def test_send_items_drops_non_dict_items(monkeypatch):
    _patch_httpx(monkeypatch, ping_status=200, save_status=200)
    result = asyncio.run(zotero.send_items([ITEMS[0], "junk", None, 3]))
    assert result["ok"] is True
    assert result["sent"] == 1


def test_send_items_posts_only_dicts_in_payload(monkeypatch):
    created = _patch_httpx(monkeypatch, ping_status=200, save_status=200)
    asyncio.run(zotero.send_items(ITEMS))
    # The second created client is the saveItems one (first is the ping).
    save_client = created[-1]
    assert save_client.saved_payload is not None
    assert save_client.saved_payload["items"] == ITEMS
    assert "sessionID" in save_client.saved_payload


# ── Endpoint wiring ───────────────────────────────────────────────────

def _client(monkeypatch):
    from fastapi.testclient import TestClient
    from backend import main as backend_main
    from backend.auth import UserInfo, require_user

    app = backend_main.app
    app.dependency_overrides[require_user] = lambda: UserInfo(id=1, provider="test")
    return TestClient(app)


def test_endpoint_relays_result(monkeypatch):
    client = _client(monkeypatch)
    try:
        async def _fake_send(items, **_kw):
            return {"ok": True, "sent": len(items), "connector_available": True,
                    "detail": f"Sent {len(items)}."}
        monkeypatch.setattr(zotero, "send_items", _fake_send)

        resp = client.post("/api/zotero/send", json={"items": ITEMS})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert body["ok"] is True
        assert body["sent"] == 2
    finally:
        client.app.dependency_overrides.clear()


def test_endpoint_survives_relay_exception(monkeypatch):
    """A relay crash returns a 200 fallback dict (never a 500) so the FE can
    fall back to the RIS download."""
    client = _client(monkeypatch)
    try:
        async def _boom(items, **_kw):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(zotero, "send_items", _boom)

        resp = client.post("/api/zotero/send", json={"items": ITEMS})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["connector_available"] is None
        assert ".ris" in body["detail"]
    finally:
        client.app.dependency_overrides.clear()


def test_endpoint_requires_items_field(monkeypatch):
    client = _client(monkeypatch)
    try:
        resp = client.post("/api/zotero/send", json={})
        assert resp.status_code == 422  # pydantic: items is required
    finally:
        client.app.dependency_overrides.clear()
