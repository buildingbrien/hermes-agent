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


# ---------------------------------------------------------------------------
# Sender identity — the "cannot send message without a sender" bug.
# A send from a non-'fleet' account (e.g. account:"gmail", from "go into my
# Gmail") used to leave an EMPTY From header, so himalaya bounced it. The From
# must now be resolved from the account's own himalaya config, for any account.
# ---------------------------------------------------------------------------

GMAIL_CFG = '''[accounts.gmail]
email = "brien@example.com"
display-name = "Brien Collier"

smtp.server = "smtps://smtp.gmail.com:465"
'''


def _point_config(monkeypatch, tmp_path, contents):
    p = tmp_path / "config.toml"
    p.write_text(contents)
    monkeypatch.setattr(est, "HIMALAYA_CONFIG", str(p))
    return p


def test_gmail_from_resolved_from_config(fake_himalaya, monkeypatch, tmp_path):
    _point_config(monkeypatch, tmp_path, GMAIL_CFG)
    result = est.email_send_tool(dict(BASE_ARGS, account="gmail"))
    assert result.get("sent") is True
    raw = fake_himalaya[0]["input"].decode(errors="replace")
    assert "From: Brien Collier <brien@example.com>" in raw
    assert "-a" in fake_himalaya[0]["cmd"]
    assert "gmail" in fake_himalaya[0]["cmd"]


def test_unconfigured_account_actionable_error_not_empty_send(fake_himalaya, monkeypatch, tmp_path):
    # gmail requested, but the config has no gmail account → no empty From send.
    _point_config(monkeypatch, tmp_path, "[accounts.fleet]\nemail = \"fleet-001@lucaryin.com\"\n")
    result = est.email_send_tool(dict(BASE_ARGS, account="gmail"))
    assert "error" in result
    assert "sender identity" in result["error"].lower()
    assert "fleet" in result["error"].lower()  # points at the known-good account
    assert not fake_himalaya  # himalaya never invoked with an empty sender


def test_explicit_from_wins_over_config(fake_himalaya, monkeypatch, tmp_path):
    _point_config(monkeypatch, tmp_path, GMAIL_CFG)
    est.email_send_tool(dict(BASE_ARGS, account="gmail", **{"from": "Ops <ops@x.com>"}))
    raw = fake_himalaya[0]["input"].decode(errors="replace")
    assert "From: Ops <ops@x.com>" in raw


def test_fleet_still_sends_when_config_missing(fake_himalaya, monkeypatch, tmp_path):
    # Regression: fleet's hardcoded fallback survives an unreadable config.
    monkeypatch.setattr(est, "HIMALAYA_CONFIG", str(tmp_path / "does-not-exist.toml"))
    result = est.email_send_tool(dict(BASE_ARGS))  # no account → 'fleet'
    assert result.get("sent") is True
    raw = fake_himalaya[0]["input"].decode(errors="replace")
    assert "fleet-001@lucaryin.com" in raw


def test_account_from_parses_email_and_name(monkeypatch, tmp_path):
    _point_config(monkeypatch, tmp_path, GMAIL_CFG)
    assert est._account_from("gmail") == "Brien Collier <brien@example.com>"
    assert est._account_from("nonexistent") == ""


def test_account_from_email_only_no_display_name(monkeypatch, tmp_path):
    _point_config(monkeypatch, tmp_path, '[accounts.zoho]\nemail = "z@lucaryin.com"\n')
    assert est._account_from("zoho") == "z@lucaryin.com"
