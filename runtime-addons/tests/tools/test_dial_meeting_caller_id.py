"""Lucaryin addon (runtime-addons/tools/dial_meeting.py + cron/meeting_join.py):
review F17 (conversation review, Sep 20 – Oct 1).

Sophie call, 2026-09-21 (mobile_3b0042fa_1789952463):
  * Three times the founder was told to admit a number that appears nowhere
    in the call data — a stale default from old docs, recalled from memory —
    because dial_meeting's result only said "from the fleet line". The
    bridge's own notice a minute later named the real line.
  * minutes=35 for a 21:00–21:30Z slot, then minutes=30 for the rejoin,
    became hard Twilio limits: both calls were cut at exactly 2100 s and
    1800 s while the room was still talking.

Hermetic: the bridge is a fake urlopen; nothing is dialed. The conftest
sandboxes HOME / HERMES_HOME.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from tools import dial_meeting as dm

FLEET = "+17245550100"


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def bridge(monkeypatch):
    sent = []
    answer = {"success": True, "call_sid": "CA" + "1" * 32, "status": "queued",
              "from": FLEET, "time_limit_s": 7200}

    def urlopen(req, timeout=30):
        sent.append((req.full_url, json.loads(req.data.decode())))
        return _Resp(answer)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("HERMES_SESSION_ID", "mobile_3b0042fa_1789952463")
    monkeypatch.setenv("BRIDGE_PROFILE", "thoth")
    return sent, answer


def _dial(**args):
    return json.loads(dm.dial_meeting_tool(dict({"to": "+1 385-645-7946", "pin": "123456789",
                                                 "label": "Sophie"}, **args)))


def test_the_result_names_the_real_caller_id(bridge):
    out = _dial(mode="clerk")
    assert out["caller_id"] == FLEET
    assert f"the number to admit is {FLEET}" in out["message"]
    assert "give the user exactly that number" in out["message"]


def test_no_caller_id_means_no_number_is_named(bridge):
    sent, answer = bridge
    answer.pop("from")
    out = _dial(mode="clerk")
    assert out["caller_id"] == ""
    assert "do not name a number before then" in out["message"]
    assert "+" not in out["message"], "no number may be offered as the line to admit"


@pytest.mark.parametrize("mode,endpoint", [("clerk", "/api/voice/dial-meeting"),
                                           ("notetaker", "/api/voice/notetaker")])
def test_model_minutes_are_not_a_cut_off(bridge, mode, endpoint):
    sent, _ = bridge
    out = _dial(mode=mode, minutes=35)
    url, payload = sent[-1]
    assert url.endswith(endpoint)
    # (old: time_limit_s 2100 — the Sophie call cut at exactly 35 min)
    assert payload["time_limit_s"] == 7200
    assert "hard_stop" not in payload
    assert out["max_minutes"] == 120
    assert "35" not in out["message"]


def test_a_user_set_hard_stop_is_honoured(bridge):
    sent, answer = bridge
    answer["time_limit_s"] = 1200
    out = _dial(mode="clerk", minutes=20, hard_stop=True)
    payload = sent[-1][1]
    assert payload["time_limit_s"] == 1200 and payload["hard_stop"] is True
    assert "at the 20-minute mark you asked for" in out["message"]


def test_the_notetaker_message_names_the_bridges_real_cap(bridge):
    """Review of PR #33: the bridge ends a silent notetaker line after 75 min
    (later only when the calendar says the meeting runs longer). The message
    said "the line closes at the latest after 120 minutes"."""
    sent, answer = bridge
    answer["time_limit_s"] = 4500
    out = _dial(mode="notetaker")
    assert out["max_minutes"] == 75
    assert "for at most 75 minutes" in out["message"]
    assert "cannot hear when the room empties" in out["message"]
    assert "120" not in out["message"]
    assert "while the meeting runs" not in out["message"]


def test_a_long_meeting_can_ask_for_more(bridge):
    sent, _ = bridge
    _dial(mode="clerk", minutes=180)
    assert sent[-1][1]["time_limit_s"] == 180 * 60


def test_clerk_dials_carry_this_chat(bridge):
    sent, _ = bridge
    _dial(mode="clerk")
    assert sent[-1][1]["session_id"] == "mobile_3b0042fa_1789952463"


def test_the_schema_tells_the_model_how_to_use_minutes():
    props = dm.DIAL_MEETING_SCHEMA["parameters"]["properties"]
    assert "NOT a cut-off" in props["minutes"]["description"]
    assert "Never set it on your own" in props["hard_stop"]["description"]
    assert "ONLY number to tell anyone to admit" in dm.DIAL_MEETING_SCHEMA["description"]


# ── the scheduled join passes the meeting's end to the bridge ───────────────

def test_scheduled_join_sends_the_meeting_end(monkeypatch):
    from cron import meeting_join as mj
    bodies = []
    monkeypatch.setattr(mj, "_post_dial", lambda body: (bodies.append(body), (True, "CA1"))[1])
    job = {"id": "35548a963a3c", "origin": {"platform": "lucaryin", "chat_id": "s1"},
           "meeting": {"label": "Courtenay Sync", "dial_number": "+13856457946",
                       "style": "clerk", "start_iso": "2999-09-27T18:30:00+00:00",
                       "end_iso": "2999-09-27T19:30:00+00:00"},
           "grants": [{"action": "outbound_call", "to": "+13856457946", "uses": 1,
                       "expires_at": "2999-09-27T19:00:00+00:00"}]}
    ok, _doc, _text, err = mj.run_meeting_join(job)
    assert ok and err is None
    assert bodies[0]["meeting_end_iso"] == "2999-09-27T19:30:00+00:00"
    assert bodies[0]["session_id"] == "s1"
