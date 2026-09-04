"""Voice fast-path for session_search (2026-09-04).

On a live phone call the tool must NOT load+Gemini-summarize each matching
session (100-180s of dead air, the founder's 194s turn). Voice worker turns run
under an inbound_/dialer_/driver_ session id; the tool detects that and returns
FTS snippets directly.
"""
import json
import tools.session_search_tool as ss


class _FakeDB:
    def __init__(self): self.loads = 0
    def search_messages(self, query, role_filter=None, exclude_sources=None, limit=50, offset=0):
        return [
            {"session_id": "s1", "role": "user",
             "snippet": "...I live in >>>Long Island City<<< now...",
             "content": "I live in Long Island City now",
             "timestamp": 1788400000, "session_title": "Move"},
        ]
    def get_session(self, sid):
        return {"id": sid, "title": "Move", "started_at": 1788400000, "parent_session_id": None}
    def get_messages_as_conversation(self, sid):
        self.loads += 1
        return [{"role": "user", "content": "x"}]


def _no_summary(monkeypatch):
    calls = {"n": 0}
    async def fake(text, query, meta):
        calls["n"] += 1; return "SUMMARY"
    monkeypatch.setattr(ss, "_summarize_session", fake)
    return calls


def test_is_voice_turn():
    assert ss._is_voice_turn("inbound_MZ1")
    assert ss._is_voice_turn("dialer_MZ2")
    assert ss._is_voice_turn("driver_MZ3")
    assert not ss._is_voice_turn("web_1")
    assert not ss._is_voice_turn("mobile_1")
    assert not ss._is_voice_turn(None)


def test_voice_turn_returns_snippets_without_summarizing(monkeypatch):
    calls = _no_summary(monkeypatch)
    db = _FakeDB()
    out = json.loads(ss.session_search("where do I live", db=db, current_session_id="inbound_MZabc"))
    assert out["mode"] == "voice_snippet"
    assert calls["n"] == 0 and db.loads == 0          # no Gemini, no full-session load
    assert "Long Island City" in out["results"][0]["excerpt"]
    assert ">>>" not in out["results"][0]["excerpt"]  # markers stripped for speech


def test_desktop_turn_still_summarizes(monkeypatch):
    calls = _no_summary(monkeypatch)
    db = _FakeDB()
    json.loads(ss.session_search("where do I live", db=db, current_session_id="web_1234"))
    assert calls["n"] > 0


# ── Budget / robustness (2026-09-04): desktop path was 104s ──────────────────
def test_budget_constants_stay_under_the_timeout_wall():
    # 1200 tok ≈ 24s finishes in ONE attempt under auxiliary.session_search
    # timeout=30; raising past ~1300 without raising that timeout re-triggers
    # the 3x-retry storm that caused the 104s call.
    assert ss.MAX_SUMMARY_TOKENS <= 1300
    assert ss.MAX_SESSION_CHARS <= 40000
    # floor: _truncate_around_matches needs room around a match
    assert ss.MAX_SESSION_CHARS >= 12000


def test_timeout_does_not_retry(monkeypatch):
    import asyncio
    from openai import APITimeoutError
    calls = {"n": 0}

    async def fake_llm(*a, **k):
        calls["n"] += 1
        raise APITimeoutError(request=None)

    monkeypatch.setattr(ss, "async_call_llm", fake_llm)
    out = asyncio.get_event_loop().run_until_complete(
        ss._summarize_session("transcript", "query", {"source": "web"}))
    assert out is None
    assert calls["n"] == 1, "a timed-out summary must NOT retry (that is the 104s storm)"


# ── Summarization routed to DeepSeek (2026-09-04) ────────────────────────────
def test_summary_routes_to_deepseek_when_key_present(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key")
    # no explicit config override in the test env → DeepSeek preferred
    ov = ss._summary_llm_overrides()
    assert ov.get("provider") == "deepseek"
    assert ov.get("model") == "deepseek-v4-flash"
    assert ov.get("base_url") == "https://api.deepseek.com"
    assert ov.get("api_key") == "sk-test-key"


def test_summary_falls_back_without_deepseek_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert ss._summary_llm_overrides() == {}  # → configured aux provider


def test_summarize_passes_overrides_to_llm(monkeypatch):
    import asyncio
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key")
    seen = {}
    async def fake_llm(**kw):
        seen.update(kw); return {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(ss, "async_call_llm", fake_llm)
    monkeypatch.setattr(ss, "extract_content_or_reasoning", lambda r: "ok")
    asyncio.get_event_loop().run_until_complete(
        ss._summarize_session("t", "q", {"source": "web"}))
    assert seen.get("provider") == "deepseek" and seen.get("model") == "deepseek-v4-flash"
