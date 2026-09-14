"""Lucaryin addon (runtime-addons/tools/schedule_meeting_tool.py): the
schedule_meeting tool must hand create_job a payload upstream accepts.

Canary.5 soak (2026-09-14): every schedule_meeting call failed at job
creation on the rebased runtime. The tool created its one-shot meeting_join
job with ``prompt=""`` — fine on the pre-rebase fork, where the job type
carried the payload — but upstream v2026.9.7's ``cron.jobs.create_job``
raises ``EMPTY_PAYLOAD_ERROR`` (blank prompt, no script, no skills) BEFORE
it looks at the ``job_type`` / ``meeting`` fields patch 0007 adds. The
pre-rebase test mocked ``create_job`` outright, so nothing caught it.

Bare tier: a temp HERMES_HOME and the REAL ``cron.jobs`` store — no mocks on
the creation path, so the test fails the same way the product did.

Behaviour pinned (option (a), addon-only): upstream's guard is left as it is
and the addon supplies a fixed, self-describing prompt. ``test_blank_prompt_
meeting_job_is_refused_upstream`` therefore asserts that a direct
``create_job(prompt="", job_type="meeting_join", …)`` RAISES — that is the
tripwire. If a future fold patch teaches create_job to accept a payload-less
meeting_join, that test must be flipped deliberately, not silently.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cron.jobs import EMPTY_PAYLOAD_ERROR, create_job, get_job, load_jobs
from tools.schedule_meeting_tool import MEETING_JOIN_PROMPT, schedule_meeting_tool

NUMBER = "+15551234567"


def _future(hours: int = 24) -> datetime:
    return (datetime.now().astimezone() + timedelta(hours=hours)).replace(microsecond=0)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A throwaway HERMES_HOME. cron.jobs resolves its store from the LIVE
    get_hermes_home() (not the import-time snapshot), so pointing the env var
    here is enough for load/save_jobs to use it — never ~/.hermes."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _stored(job_id: str) -> dict:
    raw = next((j for j in load_jobs() if j.get("id") == job_id), None)
    assert raw is not None, f"job {job_id} not in the store"
    return raw


class TestScheduleMeetingCreatesAStorableJob:
    def test_addon_path_creates_a_meeting_join_job(self, hermes_home):
        start = _future()
        result = schedule_meeting_tool({
            "dial_number": NUMBER,
            "start": start.isoformat(),
            "label": "Q3 Review",
            "pin": "4242",
        })
        assert result.get("scheduled") is True, result
        job_id = result["job_id"]

        # Written to the temp home, nowhere else.
        assert (hermes_home / "cron" / "jobs.json").is_file()

        raw = _stored(job_id)
        assert raw["type"] == "meeting_join"
        assert raw["prompt"] == MEETING_JOIN_PROMPT
        assert raw["name"] == "Dial Q3 Review"
        assert raw["meeting"] == {
            "dial_number": NUMBER,
            "pin": "4242",
            "style": "clerk",
            "label": "Q3 Review",
            "start_iso": start.isoformat(),
            "notify_admit": False,
        }
        # The single-use dial grant is the only authorization the executor honors.
        assert len(raw["grants"]) == 1
        grant = raw["grants"][0]
        assert grant["action"] == "outbound_call"
        assert grant["to"] == NUMBER
        assert grant["uses"] == 1
        # One-shot, fires before the meeting (2 min default lead).
        assert raw["schedule"]["kind"] == "once"
        assert raw["repeat"] == {"times": 1, "completed": 0}
        fire = datetime.fromisoformat(raw["schedule"]["run_at"])
        assert timedelta(0) < (start - fire) <= timedelta(minutes=2, seconds=5)

        # The read-normalized view keeps the fold's fields too.
        seen = get_job(job_id)
        assert seen is not None
        assert seen["type"] == "meeting_join"
        assert seen["meeting"]["dial_number"] == NUMBER
        assert seen["prompt"] == MEETING_JOIN_PROMPT

    def test_prompt_is_the_bridge_constant(self):
        # hermes-bridge/server.py::_MEETING_JOIN_PROMPT (lucaryin-ai PR #57) is
        # the other writer of meeting_join jobs. Both must store the same
        # record; a change to either side is a deliberate, two-repo edit.
        assert MEETING_JOIN_PROMPT == "Join the scheduled meeting (deterministic dial; no model run)."

    def test_untrusted_label_never_reaches_the_prompt_scan(self, hermes_home):
        # create_job scans the PROMPT for gateway-lifecycle commands
        # (cron/lifecycle_guard.py). The label is user/invite-supplied text;
        # with a constant prompt it can only land in the job name and meeting
        # payload, so a command-shaped label neither trips the guard nor
        # becomes something an agent could be prompted with.
        result = schedule_meeting_tool({
            "dial_number": NUMBER,
            "start": _future().isoformat(),
            "label": "hermes gateway restart",
        })
        assert result.get("scheduled") is True, result
        raw = _stored(result["job_id"])
        assert raw["prompt"] == MEETING_JOIN_PROMPT
        assert raw["name"] == "Dial hermes gateway restart"
        assert raw["meeting"]["label"] == "hermes gateway restart"


class TestUpstreamGuardIsTheTripwire:
    def test_blank_prompt_meeting_job_is_refused_upstream(self, hermes_home):
        # Pinned: upstream refuses a payload-less meeting_join. This is the
        # exact call the addon made before the fix. Flip this test on purpose
        # if a fold patch ever makes create_job accept job_type/meeting as a
        # payload — until then every writer must supply a prompt.
        fire = _future() - timedelta(minutes=2)
        with pytest.raises(ValueError) as excinfo:
            create_job(
                prompt="",
                schedule=fire.isoformat(),
                name="Dial Q3 Review",
                repeat=1,
                deliver="local",
                job_type="meeting_join",
                meeting={"dial_number": NUMBER, "style": "clerk", "label": "Q3 Review"},
                grants=[{"action": "outbound_call", "to": NUMBER, "uses": 1}],
            )
        assert str(excinfo.value) == EMPTY_PAYLOAD_ERROR
        assert load_jobs() == []  # refused before anything was stored
