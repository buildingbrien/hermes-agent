"""ask_agent from a scheduled run, and the reply's identity (addon tools/ask_agent_tool.py).

Lucaryin local conversation review (Sep 20 - Oct 1 2026), F20/F21: Team Huddle had no
ask_agent in its cron toolset, so it curled /api/chat/sync with the bridge bearer — and for
Neith it ran the legacy OpenClaw CLI, whose memory ended in July, then told the board and Team
Chat the real Neith was offline. Patch 0037 gives such jobs ask_agent; this pins the two things
ask_agent now does for them:

* a question asked inside a cron run carries ``"unattended": true``, which the Lucaryin bridge
  turns into an unattended teammate turn (blocked + carded, never waited on) — a scheduled job
  cannot borrow a teammate's attended trust;
* a reply from a bridge that names a different profile than the one asked is not used
  (an older bridge that names none is still taken at its port).
"""

from __future__ import annotations

import io
import json

import pytest

from tools import ask_agent_tool as aa


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def wire(monkeypatch):
    sent = {}
    reply = {"body": {"success": True, "response": "Shipped the Q3 memo.", "profile": "neith"}}

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["payload"] = json.loads(req.data.decode())
        return _Resp(json.dumps(reply["body"]).encode())

    monkeypatch.setattr(aa.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(aa, "_budget_fields", lambda sender: {})
    return sent, reply


def _in_cron():
    from gateway.session_context import _VAR_MAP
    return _VAR_MAP["HERMES_CRON_SESSION"]


def test_a_question_from_a_cron_run_is_marked_unattended(wire):
    sent, _ = wire
    var = _in_cron()
    token = var.set("1")
    try:
        out = json.loads(aa.ask_agent("neith", "What did you ship today?", sender="thoth"))
    finally:
        var.reset(token)
    assert out["success"] is True
    assert sent["url"] == "http://127.0.0.1:9007/api/chat/sync"
    assert sent["payload"]["unattended"] is True


def test_an_attended_question_is_unchanged(wire):
    sent, _ = wire
    out = json.loads(aa.ask_agent("neith", "What did you ship today?", sender="thoth"))
    assert out["success"] is True
    assert "unattended" not in sent["payload"]


def test_a_reply_from_another_profile_is_not_used(wire):
    _, reply = wire
    reply["body"] = {"success": True, "response": "Neith OFFLINE since July", "profile": "thoth"}
    out = json.loads(aa.ask_agent("neith", "Status?", sender="ptah"))
    assert out["success"] is False
    assert "not reachable" in out["error"]
    assert "OFFLINE" not in json.dumps(out)


def test_an_older_bridge_that_names_no_profile_is_taken_at_its_port(wire):
    _, reply = wire
    reply["body"] = {"success": True, "response": "pong"}
    out = json.loads(aa.ask_agent("ptah", "Reply with PONG", sender="thoth"))
    assert out == {"success": True, "agent": "ptah", "answer": "pong"}
