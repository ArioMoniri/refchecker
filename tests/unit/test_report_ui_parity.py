"""Regression tests for the report-vs-UI and honesty fixes:

[B] export honors the FE's style-filtered per-reference issues
    (backend.export.apply_filtered_issues), so the downloaded report no longer
    shows cosmetic / style-suppressed issues the app already hid.
[C] the reference-extraction system prompt forbids expanding an initial into a
    fabricated first name ("Morgan KA" must stay "Morgan KA").
[D] transient checker-infra failures (throttled/timeout/5xx) are no longer
    surfaced as citation findings.
"""
from unittest.mock import patch

from backend import export as _export
from refchecker.checkers.enhanced_hybrid_checker import EnhancedHybridReferenceChecker
from refchecker.llm.providers import LLMProviderMixin


# ── [B] apply_filtered_issues ─────────────────────────────────────────

def _check_with_issues():
    return {
        "paper_title": "P",
        "results": [
            {"title": "Ref 1", "authors": ["Porto JR"],
             "errors": [{"error_type": "author", "error_details": "Author 2 mismatch: cited: Kira A. Morgan actual: Kerry A. Morgan"}],
             "warnings": []},
            {"title": "Ref 2", "authors": ["Doe J"], "errors": [], "warnings": []},
        ],
    }


def test_apply_filtered_issues_overrides_by_index():
    check = _check_with_issues()
    # FE decided ref 1's author error is a cosmetic false positive → dropped.
    filtered = [{"errors": [], "warnings": []}, {"errors": [], "warnings": []}]
    out = _export.apply_filtered_issues(check, filtered)
    assert out is not check  # a copy, original untouched
    assert out["results"][0]["errors"] == []
    assert check["results"][0]["errors"]  # original still has it
    # And it must not appear in the rendered model's issue lines.
    errs, _major, _minor = _export._issues_for(out["results"][0])
    assert errs == []


def test_apply_filtered_issues_keeps_surviving_issues():
    check = _check_with_issues()
    surviving = [{"error_type": "year", "error_details": "Year mismatch: 2020 vs 2021"}]
    filtered = [{"errors": surviving, "warnings": []}, {"errors": [], "warnings": []}]
    out = _export.apply_filtered_issues(check, filtered)
    errs, _major, _minor = _export._issues_for(out["results"][0])
    assert errs == ["Year mismatch: 2020 vs 2021"]


def test_apply_filtered_issues_noops_on_length_mismatch():
    check = _check_with_issues()
    # Only one filtered entry for two refs → don't risk misalignment.
    out = _export.apply_filtered_issues(check, [{"errors": [], "warnings": []}])
    assert out is check


def test_apply_filtered_issues_noops_on_empty():
    check = _check_with_issues()
    assert _export.apply_filtered_issues(check, None) is check
    assert _export.apply_filtered_issues(check, []) is check


# ── [C] extraction prompt preserves initials ──────────────────────────

def test_extraction_prompt_forbids_expanding_initials():
    prompt = LLMProviderMixin._get_system_prompt(None)  # returns a literal; self unused
    low = prompt.lower()
    assert "never expand" in low or "never expand or invent" in low
    assert "morgan ka" in low  # the worked example that anchors the rule
    # The worked example must map the Vancouver initials verbatim (no first name).
    assert "Porto JR*Morgan KA*Hecht CJ*Burkhart RJ*Liu RW" in prompt


# ── [D] transient checker failures not surfaced as findings ───────────

def _checker():
    with patch.object(EnhancedHybridReferenceChecker, "_initialize_checker", return_value=None):
        return EnhancedHybridReferenceChecker(
            enable_openalex=False, enable_crossref=False, enable_arxiv_citation=False,
        )


def test_transient_dblp_failure_dropped_when_genuine_negatives_exist():
    c = _checker()
    # DBLP transiently failed; Semantic Scholar + CrossRef cleanly said "no match".
    attempted = ["semantic_scholar", "crossref", "dblp"]
    failed = [{"name": "dblp", "failure_type": "throttled",
               "failure_detail": "DBLP: DBLP API request failed"}]
    msg = c._build_unverified_error_details(attempted, failed)
    assert "Paper not found by any checker" in msg
    assert "DBLP" not in msg  # transient failure must not appear in the finding
    assert "request failed" not in msg.lower()


def test_all_transient_failures_yield_soft_retry_message():
    c = _checker()
    attempted = ["dblp"]
    failed = [{"name": "dblp", "failure_type": "throttled",
               "failure_detail": "DBLP: DBLP API request failed"}]
    msg = c._build_unverified_error_details(attempted, failed)
    assert "not found" not in msg.lower()  # don't fabricate a not-found verdict
    assert "temporary" in msg.lower() and "retry" in msg.lower()


def test_genuine_checker_failure_still_reported():
    c = _checker()
    # A non-transient failure (e.g. a parse error) is still shown honestly.
    attempted = ["semantic_scholar", "crossref"]
    failed = [{"name": "crossref", "failure_type": "other",
               "failure_detail": "CrossRef: malformed response"}]
    msg = c._build_unverified_error_details(attempted, failed)
    assert "checker failures" in msg
    assert "malformed response" in msg
