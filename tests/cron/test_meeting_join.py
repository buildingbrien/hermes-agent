"""Tests for the deterministic meeting-join executor and its grant gate.

The security property under test: the executor dials ONLY when a live,
matching, unconsumed outbound_call grant is present. The grant is the encoded
form of the human authorization captured at schedule time; without it, no dial.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.meeting_join import find_live_grant, run_meeting_join


def _iso(dt):
    return dt.isoformat()


def _grant(number="+15551234567", minutes_ahead=30, uses=1):
    return {
        "action": "outbound_call",
        "to": number,
        "reason": "meeting:Q3 Review",
        "expires_at": _iso(datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead)),
        "uses": uses,
    }


def _job(grants=None, number="+15551234567", style="clerk"):
    return {
        "id": "mtg-1",
        "type": "meeting_join",
        "agent": "thoth",
        "meeting": {"dial_number": number, "style": style, "label": "Q3 Review", "pin": "4417"},
        "grants": grants if grants is not None else [_grant(number)],
        "origin": {"platform": "lucaryin", "chat_id": "sess-xyz"},
    }


# ── Grant validation ────────────────────────────────────────────────────────

def test_live_grant_found():
    assert find_live_grant([_grant()], "+15551234567") is not None


def test_grant_for_other_number_rejected():
    assert find_live_grant([_grant(number="+15550000000")], "+15551234567") is None


def test_expired_grant_rejected():
    assert find_live_grant([_grant(minutes_ahead=-5)], "+15551234567") is None


def test_used_up_grant_rejected():
    assert find_live_grant([_grant(uses=0)], "+15551234567") is None


def test_non_outbound_grant_rejected():
    g = _grant()
    g["action"] = "email_send"
    assert find_live_grant([g], "+15551234567") is None


# ── Executor: dials only with authorization ─────────────────────────────────

def test_dials_with_valid_grant():
    with patch("cron.meeting_join._post_dial", return_value=(True, "CA123")) as pd:
        ok, doc, msg, err = run_meeting_join(_job())
    assert ok and err is None
    assert "dialing into" in msg.lower()
    # The dial carried the stored params.
    body = pd.call_args.args[0]
    assert body["to"] == "+15551234567"
    assert body["style"] == "clerk"
    assert body["pin"] == "4417"
    assert body["session_id"] == "sess-xyz"


def test_no_grant_never_dials():
    with patch("cron.meeting_join._post_dial") as pd:
        ok, doc, msg, err = run_meeting_join(_job(grants=[]))
    assert not ok
    assert err == "no live authorization for this call"
    pd.assert_not_called()  # fail-closed: the dial was never attempted
    assert "authorization" in msg.lower()


def test_expired_grant_never_dials():
    with patch("cron.meeting_join._post_dial") as pd:
        ok, _, msg, err = run_meeting_join(_job(grants=[_grant(minutes_ahead=-1)]))
    assert not ok
    pd.assert_not_called()


def test_grant_consumed_on_successful_dial():
    job = _job()
    with patch("cron.meeting_join._post_dial", return_value=(True, "CA1")):
        run_meeting_join(job)
    assert job["grants"][0]["uses"] == 0  # single-use spent


def test_grant_not_consumed_on_failed_dial():
    job = _job()
    with patch("cron.meeting_join._post_dial", return_value=(False, "no answer")):
        ok, _, msg, err = run_meeting_join(job)
    assert not ok
    assert job["grants"][0]["uses"] == 1  # a failed dial can be retried
    assert "couldn't" in msg.lower() and "Q3 Review" in msg


def test_dial_failure_is_reported_not_silent():
    with patch("cron.meeting_join._post_dial", return_value=(False, "voice service down")):
        ok, doc, msg, err = run_meeting_join(_job())
    assert not ok
    assert "voice service down" in err
    assert msg  # user-facing text exists for delivery


def test_missing_number_reported():
    job = _job(number="")
    job["meeting"]["dial_number"] = ""
    ok, _, msg, err = run_meeting_join(job)
    assert not ok
    assert "number" in err


def test_invalid_style_falls_back_to_clerk():
    job = _job(style="karaoke")
    with patch("cron.meeting_join._post_dial", return_value=(True, "CA1")) as pd:
        run_meeting_join(job)
    assert pd.call_args.args[0]["style"] == "clerk"
