"""Tests for email_send_tool — one atomic send, and draft:true that never sends.

The draft flag exists because the send path is (rightly) gated: "draft it and
I'll send it myself" is the low-friction alternative, and it must be
impossible for that path to reach `message send`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tools import email_send_tool as est


class FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_himalaya(monkeypatch):
    """Capture the himalaya invocation instead of running it."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "input": kwargs.get("input", b"")})
        return FakeProc()

    monkeypatch.setattr(est, "_himalaya", lambda: "/fake/bin/himalaya")
    monkeypatch.setattr(est.subprocess, "run", fake_run)
    return calls


BASE_ARGS = {"to": "courtenay@example.com", "subject": "Deck", "body": "Attached."}


def test_send_uses_message_send(fake_himalaya):
    result = est.email_send_tool(dict(BASE_ARGS))
    assert result.get("sent") is True
    cmd = fake_himalaya[0]["cmd"]
    assert "send" in cmd
    assert "save" not in cmd


def test_draft_never_reaches_send(fake_himalaya):
    result = est.email_send_tool(dict(BASE_ARGS, draft=True))
    assert result.get("drafted") is True
    assert result.get("sent") is False
    cmd = fake_himalaya[0]["cmd"]
    assert "save" in cmd
    assert "send" not in cmd
    assert "Drafts" in cmd  # explicit folder — himalaya defaults to INBOX


def test_draft_summary_says_nothing_was_sent(fake_himalaya):
    result = est.email_send_tool(dict(BASE_ARGS, draft=True))
    assert "nothing was sent" in result["summary"].lower()


def test_draft_is_fully_addressed(fake_himalaya):
    est.email_send_tool(dict(BASE_ARGS, draft=True, cc="bill@example.com"))
    raw = fake_himalaya[0]["input"].decode(errors="replace")
    assert "courtenay@example.com" in raw
    assert "bill@example.com" in raw
    assert "Deck" in raw


def test_draft_still_validates_recipients(fake_himalaya):
    result = est.email_send_tool({"to": "not-an-address", "subject": "s", "body": "b",
                                  "draft": True})
    assert "error" in result
    assert not fake_himalaya  # himalaya never invoked


def test_draft_failure_names_the_draft_verb(monkeypatch):
    monkeypatch.setattr(est, "_himalaya", lambda: "/fake/bin/himalaya")
    monkeypatch.setattr(
        est.subprocess, "run",
        lambda *a, **k: FakeProc(returncode=1, stderr=b"IMAP down"),
    )
    result = est.email_send_tool(dict(BASE_ARGS, draft=True))
    assert "error" in result
    assert "IMAP down" in result["error"]
    assert "draft" in result["error"].lower()
