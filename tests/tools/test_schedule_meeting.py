"""Tests for the schedule_meeting tool — the attended booking side.

It must: resolve time to an absolute instant, refuse bad/past input, create a
one-shot meeting_join job that fires BEFORE the meeting, and attach a
single-use outbound_call grant scoped to the exact number and expiring after
the meeting. That grant is the only authorization the deterministic executor
will accept.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tools.schedule_meeting_tool import schedule_meeting_tool


def _future(hours=24):
    return (datetime.now().astimezone() + timedelta(hours=hours)).replace(microsecond=0)


@pytest.fixture
def captured_job():
    created = {}

    def fake_create_job(**kwargs):
        created.update(kwargs)
        return {"id": "job-abc", **kwargs}

    with patch("cron.jobs.create_job", side_effect=fake_create_job), \
         patch("tools.cronjob_tools._origin_from_env",
               return_value={"platform": "lucaryin", "chat_id": "sess-1"}):
        yield created


def test_schedules_a_meeting_join_job(captured_job):
    start = _future()
    result = schedule_meeting_tool({
        "dial_number": "+15551234567",
        "start": start.isoformat(),
        "label": "Q3 Review",
    })
    assert result.get("scheduled") is True
    assert captured_job["job_type"] == "meeting_join"
    assert captured_job["meeting"]["dial_number"] == "+15551234567"
    assert captured_job["meeting"]["style"] == "clerk"  # default
    assert captured_job["meeting"]["notify_admit"] is False  # auto-admit


def test_grant_is_scoped_single_use_and_expiring(captured_job):
    start = _future()
    schedule_meeting_tool({
        "dial_number": "+15551234567", "start": start.isoformat(), "label": "Q3",
    })
    grants = captured_job["grants"]
    assert len(grants) == 1
    g = grants[0]
    assert g["action"] == "outbound_call"
    assert g["to"] == "+15551234567"
    assert g["uses"] == 1
    exp = datetime.fromisoformat(g["expires_at"])
    # Expires ~30 min after start, not before.
    assert exp > start


def test_fires_before_the_meeting(captured_job):
    start = _future()
    schedule_meeting_tool({
        "dial_number": "+15551234567", "start": start.isoformat(),
        "label": "Q3", "lead_minutes": 2,
    })
    fire = datetime.fromisoformat(captured_job["schedule"])
    assert fire < start
    assert abs((start - fire).total_seconds() - 120) < 5  # ~2 min lead


def test_past_start_refused(captured_job):
    past = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
    result = schedule_meeting_tool({
        "dial_number": "+15551234567", "start": past, "label": "Late",
    })
    assert "error" in result
    assert "past" in result["error"].lower()
    assert not captured_job  # nothing scheduled


def test_missing_number_refused(captured_job):
    result = schedule_meeting_tool({"start": _future().isoformat(), "label": "X"})
    assert "error" in result
    assert not captured_job


def test_junk_time_refused(captured_job):
    result = schedule_meeting_tool({
        "dial_number": "+15551234567", "start": "sometime next week", "label": "X",
    })
    assert "error" in result
    assert not captured_job


def test_style_passthrough(captured_job):
    schedule_meeting_tool({
        "dial_number": "+15551234567", "start": _future().isoformat(),
        "label": "Notes", "style": "scribe",
    })
    assert captured_job["meeting"]["style"] == "scribe"


def test_bad_style_defaults_to_clerk(captured_job):
    schedule_meeting_tool({
        "dial_number": "+15551234567", "start": _future().isoformat(),
        "label": "X", "style": "interpretive-dance",
    })
    assert captured_job["meeting"]["style"] == "clerk"


def test_imminent_meeting_dials_almost_now(captured_job):
    # Meeting starts in 1 minute with a 2-minute lead → fire time would be in
    # the past; clamp to ~now instead of refusing.
    start = (datetime.now().astimezone() + timedelta(minutes=1)).replace(microsecond=0)
    result = schedule_meeting_tool({
        "dial_number": "+15551234567", "start": start.isoformat(),
        "label": "Now", "lead_minutes": 2,
    })
    assert result.get("scheduled") is True
    fire = datetime.fromisoformat(captured_job["schedule"])
    assert fire <= start
