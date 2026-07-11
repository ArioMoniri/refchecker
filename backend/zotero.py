"""Thin server-side relay to a locally-running Zotero desktop connector.

RefChecker's "Send to Zotero" button maps verified references to Zotero item
JSON in the browser (web-ui/src/utils/formatters.js — reusing the same
corrected-metadata path as the RIS/BibTeX exports, so only verifier-corrected
values are ever sent, never the raw as-cited/wrong ones). The browser cannot
READ the connector's response cross-origin, so the POST is performed here and
the result is reported back to the frontend.

This only works when Zotero is running on the SAME machine as this backend —
i.e. the desktop app, or a local dev run. On a remote web deployment the
loopback connector is unreachable, ``send_items`` reports
``connector_available=False`` and the frontend falls back to an RIS download.

No credentials, no Zotero Web API — this only talks to the loopback connector.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# The Zotero desktop app's built-in connector server. Loopback only — never a
# user-supplied host, so there is no SSRF surface here.
CONNECTOR_BASE = "http://127.0.0.1:23119"
_PING_URL = f"{CONNECTOR_BASE}/connector/ping"
_SAVE_URL = f"{CONNECTOR_BASE}/connector/saveItems"

# Newer Zotero (6.0.27+) requires ``Zotero-Allowed-Request`` on connector
# requests as CSRF protection; it is harmless on older builds that ignore it.
_HEADERS = {
    "Content-Type": "application/json",
    "X-Zotero-Connector-API-Version": "3",
    "Zotero-Allowed-Request": "true",
    "User-Agent": "RefChecker (Send to Zotero)",
}


async def connector_available(timeout: float = 2.0) -> bool:
    """True if a Zotero connector answers on the loopback port."""
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(_PING_URL, headers=_HEADERS, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


async def send_items(
    items: List[Dict[str, Any]],
    *,
    source_uri: str = "http://localhost/refchecker",
    timeout: float = 20.0,
) -> Dict[str, Any]:
    """POST already-mapped Zotero items to the local connector's saveItems.

    ``items`` are built client-side (formatters.referencesToZoteroItems); this
    function is a dumb relay and does not inspect or rewrite their metadata.

    Returns a JSON-able dict ``{ok, sent, connector_available, detail}``. Never
    raises for connector/network problems — the caller turns
    ``connector_available in (False, None)`` (or ``ok=False``) into the RIS
    download fallback.
    """
    import httpx

    items = [it for it in (items or []) if isinstance(it, dict)]
    if not items:
        return {
            "ok": False,
            "sent": 0,
            "connector_available": None,
            "detail": "No references to send.",
        }

    if not await connector_available(timeout=min(timeout, 3.0)):
        return {
            "ok": False,
            "sent": 0,
            "connector_available": False,
            "detail": "Zotero isn't running on this machine.",
        }

    # saveItems drops the items into the user's currently-selected Zotero
    # collection. sessionID lets Zotero group a single save action.
    payload = {"items": items, "uri": source_uri, "sessionID": uuid.uuid4().hex}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(_SAVE_URL, json=payload, headers=_HEADERS, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — any transport error → fallback
        logger.info("Zotero saveItems request failed: %s", e)
        return {
            "ok": False,
            "sent": 0,
            "connector_available": True,
            "detail": f"Couldn't reach Zotero: {e}",
        }

    if r.status_code in (200, 201):
        n = len(items)
        return {
            "ok": True,
            "sent": n,
            "connector_available": True,
            "detail": f"Sent {n} reference{'' if n == 1 else 's'} to Zotero.",
        }

    logger.info("Zotero saveItems returned HTTP %s", r.status_code)
    return {
        "ok": False,
        "sent": 0,
        "connector_available": True,
        "detail": f"Zotero rejected the save (HTTP {r.status_code}).",
    }
