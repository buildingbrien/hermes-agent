"""Lucaryin fold 0026 (HA4 / R2-1-43; pair contract with B5 in
hermes-bridge/cron_tick_support.py).

With serial ticks (the bridge's HERMES_CRON_MAX_PARALLEL=1 stopgap, B5) a tick
lasts the sum of its due jobs' durations, so a one-shot scanned a few minutes
late used to be retired unfired. Three parts:

1. The late-fire WINDOW: every one-shot may fire up to 30 min late; a
   meeting_join until its meeting is over (``meeting.end_iso``, else start + 30
   min); any record may pin ``late_fire_window_seconds`` (clamped to
   [120, 21600]). The first cut widened only ``type`` meeting_join / reminder —
   and no writer anywhere sets type="reminder": "remind me at 9:05" through the
   cronjob tool is a plain prompt one-shot, which kept 120 s (review,
   2026-09-23). ``TestThroughTheCronjobTool`` creates the job the way a model
   does.
2. One-shots run FIRST in a tick (B5: "HA4 must submit kind == 'once' jobs
   before recurring ones") — create_job appends, so a reminder created last
   started only after every recurring job due that minute.
3. A one-shot past its window is kept only for a LIVE claim (B5: "must retire
   when the claim is NOT live instead of merely present, with a diagnostic").

Hermetic: the upstream cron store fixture with a pinned clock, the real due
scan, the real tick() with the model call stubbed.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cron.jobs import (
    ONESHOT_GRACE_SECONDS, _oneshot_late_fire_window_seconds, get_due_jobs, load_jobs, save_jobs,
)

RUN_AT = datetime(2026, 9, 24, 11, 58, 0, tzinfo=timezone.utc)   # dial 2 min before a 12:00 meeting
THIRTY_MIN = 30 * 60


@pytest.fixture
def clock(tmp_path, monkeypatch):
    """Cron storage in a tmp dir; ``clock(now)`` pins the scan clock."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")

    def set_now(now):
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
    return set_now


def _oneshot(jid, **extra):
    job = {
        "id": jid, "name": jid, "prompt": "x", "type": "prompt",
        "schedule": {"kind": "once", "run_at": RUN_AT.isoformat()},
        "next_run_at": RUN_AT.isoformat(), "last_run_at": None, "enabled": True,
        "state": "scheduled", "repeat": {"times": 1, "completed": 0}, "deliver": "local",
        "run_claim": None, "fire_claim": None,
    }
    job.update(extra)
    return job


def _meeting(jid, end_iso=None, **extra):
    meeting = {"dial_number": "+15555550100", "label": "Board sync", "style": "clerk",
               "start_iso": "2026-09-24T12:00:00+00:00"}
    if end_iso:
        meeting["end_iso"] = end_iso
    return _oneshot(jid, type="meeting_join", meeting=meeting, **extra)


def _due_ids(clock, now, *jobs):
    clock(now)
    save_jobs(list(jobs))
    due = {j["id"] for j in get_due_jobs()}
    remaining = {j["id"] for j in load_jobs()}
    return due, remaining


class TestWindowSize:
    def test_every_oneshot_gets_thirty_minutes(self):
        assert _oneshot_late_fire_window_seconds(_oneshot("p"), RUN_AT) == THIRTY_MIN
        assert _oneshot_late_fire_window_seconds(_oneshot("r", type="reminder"), RUN_AT) == THIRTY_MIN

    def test_the_create_time_grace_is_unchanged(self):
        """create/update/resume still refuse a schedule already 120 s in the past."""
        assert ONESHOT_GRACE_SECONDS == 120

    def test_meeting_without_end_gets_thirty_minutes_from_start(self):
        # start 12:00 + 30 min = 12:30; fired at 11:58 -> 32 min window
        assert _oneshot_late_fire_window_seconds(_meeting("m"), RUN_AT) == 32 * 60

    def test_meeting_with_end_fires_until_it_is_over(self):
        assert _oneshot_late_fire_window_seconds(_meeting("m", end_iso="2026-09-24T12:45:00+00:00"), RUN_AT) == 47 * 60

    def test_short_meeting_still_gets_the_thirty_minute_floor(self):
        assert _oneshot_late_fire_window_seconds(_meeting("m", end_iso="2026-09-24T12:05:00+00:00"), RUN_AT) == THIRTY_MIN

    @pytest.mark.parametrize("explicit,expected", [
        (900, 900), (10, 120), (99999, 6 * 3600), ("600", 600), ("junk", THIRTY_MIN)])
    def test_explicit_field_is_honoured_and_clamped(self, explicit, expected):
        assert _oneshot_late_fire_window_seconds(_oneshot("p", late_fire_window_seconds=explicit), RUN_AT) == expected

    def test_garbage_records_fall_back_to_the_default(self):
        assert _oneshot_late_fire_window_seconds(None, RUN_AT) == THIRTY_MIN
        # an unparseable end_iso falls back to start_iso + 30 min (12:30 - 11:58)
        assert _oneshot_late_fire_window_seconds(_meeting("m", end_iso="not a date"), RUN_AT) == 32 * 60
        bare = _meeting("m"); bare["meeting"] = {"label": "no times at all"}
        assert _oneshot_late_fire_window_seconds(bare, RUN_AT) == THIRTY_MIN


class TestDueScan:
    def test_plain_oneshot_ten_minutes_late_still_fires(self, clock):
        due, remaining = _due_ids(clock, RUN_AT + timedelta(minutes=10), _oneshot("p"))
        assert due == {"p"} and remaining == {"p"}

    def test_plain_oneshot_forty_minutes_late_is_retired(self, clock):
        due, remaining = _due_ids(clock, RUN_AT + timedelta(minutes=40), _oneshot("p"))
        assert due == set() and remaining == set()

    def test_meeting_join_ten_minutes_late_still_fires(self, clock):
        due, remaining = _due_ids(clock, RUN_AT + timedelta(minutes=10), _meeting("m"))
        assert due == {"m"} and remaining == {"m"}

    def test_meeting_join_forty_minutes_late_is_retired(self, clock):
        due, remaining = _due_ids(clock, RUN_AT + timedelta(minutes=40), _meeting("m"))
        assert due == set() and remaining == set()

    def test_meeting_with_end_time_fires_while_it_is_in_progress(self, clock):
        job = _meeting("m", end_iso="2026-09-24T12:45:00+00:00")
        due, _ = _due_ids(clock, RUN_AT + timedelta(minutes=40), job)
        assert due == {"m"}
        due, remaining = _due_ids(clock, RUN_AT + timedelta(minutes=50), job)
        assert due == set() and remaining == set()

    def test_serial_tick_scenario_long_job_then_meeting(self, clock):
        """A 25-minute job ran first in the tick; the one-shots are scanned 25
        minutes late and must still be due."""
        due, _ = _due_ids(clock, RUN_AT + timedelta(minutes=25), _meeting("m"), _oneshot("reminder"))
        assert due == {"m", "reminder"}

    def test_retired_diagnostic_names_the_jobs_own_window(self, clock, tmp_path):
        _due_ids(clock, RUN_AT + timedelta(minutes=40), _meeting("m"))
        texts = [p.read_text() for p in (tmp_path / "cron" / "output").rglob("*") if p.is_file()]
        assert any("grace window: 1920s" in t for t in texts), texts


class TestDeadClaimsAreRetired:
    """B5 pair contract: a claim that is merely PRESENT used to keep a one-shot
    past its window forever (never run, never retired, never diagnosed)."""

    def test_stale_claim_past_the_window_is_retired_with_a_diagnostic(self, clock, tmp_path):
        stale = {"at": (RUN_AT - timedelta(hours=3)).isoformat(), "by": "other-host:1"}
        job = _oneshot("stranded", run_claim=stale, repeat={"times": 1, "completed": 1})
        due, remaining = _due_ids(clock, RUN_AT + timedelta(hours=3), job)
        assert due == set() and remaining == set()
        texts = [p.read_text() for p in (tmp_path / "cron" / "output").rglob("*") if p.is_file()]
        assert texts, "a stranded one-shot must leave a diagnostic, not vanish"

    def test_a_live_claim_keeps_the_record(self, clock):
        now = RUN_AT + timedelta(hours=1)
        live = {"at": (now - timedelta(minutes=1)).isoformat(), "by": "other-host:1"}
        due, remaining = _due_ids(clock, now, _oneshot("inflight", run_claim=live))
        assert due == set() and remaining == {"inflight"}

    def test_a_live_fire_claim_keeps_the_record(self, clock):
        now = RUN_AT + timedelta(hours=1)
        fire = {"at": (now - timedelta(seconds=30)).isoformat(), "by": "other-host:1"}
        due, remaining = _due_ids(clock, now, _oneshot("firing", fire_claim=fire))
        assert due == set() and remaining == {"firing"}


class TestThroughTheCronjobTool:
    """The reminder a model actually creates: cronjob_manage(action=create) with a
    one-shot timestamp — no hand-built type='reminder' record."""

    @pytest.fixture
    def hermes_home(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
        return home

    def _create_reminder(self):
        from tools.cronjob_tools import _cronjob_handler
        run_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(microsecond=0)
        result = json.loads(_cronjob_handler({
            "action": "create", "name": "call the dentist",
            "prompt": "Remind the user to call the dentist.",
            "schedule": run_at.isoformat(), "deliver": "local",
        }, task_id="t-1", session_id="s-1"))
        assert result.get("success") is True, result
        stored = next(j for j in load_jobs() if j["id"] == result["job_id"])
        assert stored["schedule"]["kind"] == "once" and stored.get("type", "prompt") == "prompt"
        return stored, datetime.fromisoformat(stored["next_run_at"])

    @pytest.mark.parametrize("late_minutes,fires", [(1, True), (10, True), (25, True), (40, False)])
    def test_a_tool_created_reminder_fires_late_within_the_window(self, hermes_home, monkeypatch, late_minutes, fires):
        stored, due_at = self._create_reminder()
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: due_at + timedelta(minutes=late_minutes))
        due = {j["id"] for j in get_due_jobs()}
        remaining = {j["id"] for j in load_jobs()}
        if fires:
            assert due == {stored["id"]}, late_minutes
        else:
            assert due == set() and stored["id"] not in remaining, late_minutes


class TestOneShotsRunFirst:
    """One worker (the bridge's HERMES_CRON_MAX_PARALLEL=1): the submission order
    IS the run order.

    This runs the REAL tick(): it takes the tick lock and writes 'claimed' rows
    to the execution ledger. Run by its source path (outside build/runtime/tests,
    so without upstream's conftest) it once did both against the founder's live
    ~/.hermes (2026-09-23). The fixture pins every path tick() resolves — the
    env, the scheduler's home override, the ledger file and cron.jobs' import-time
    constants — to a tmp home, and the test asserts the lock it took is that
    one. runtime-addons/tests/conftest.py guards the whole addon suite on top."""

    @pytest.fixture
    def cron_home(self, tmp_path, monkeypatch):
        import cron.executions as executions
        import cron.scheduler as sched

        home = tmp_path / "hermes-home"
        (home / "cron").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(sched, "_hermes_home", home)
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
        monkeypatch.setattr("cron.jobs.HERMES_DIR", home)
        monkeypatch.setattr("cron.jobs.CRON_DIR", home / "cron")
        monkeypatch.setattr("cron.jobs.JOBS_FILE", home / "cron" / "jobs.json")
        monkeypatch.setattr("cron.jobs.OUTPUT_DIR", home / "cron" / "output")
        # Housekeeping tick() may start (worktree GC runs git in a thread) is
        # out of scope for an ordering test.
        monkeypatch.setattr(sched, "_maybe_run_worktree_maintenance", lambda *a, **k: None)
        locks = []
        real_acquire = sched._acquire_tick_lock

        def _acquire(lock_file):
            locks.append(Path(lock_file))
            return real_acquire(lock_file)
        monkeypatch.setattr(sched, "_acquire_tick_lock", _acquire)
        return home, locks

    def test_tick_submits_oneshots_before_recurring_jobs(self, monkeypatch, cron_home):
        import cron.scheduler as sched

        home, locks = cron_home
        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._running_job_ids.clear()
        monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "1")

        def recurring(i):
            return {"id": f"digest-{i}", "name": f"Digest {i}", "prompt": "x",
                    "schedule": {"kind": "cron", "expr": "0 9 * * 1"}, "enabled": True,
                    "next_run_at": "2020-01-01T09:00:00", "deliver": "local"}

        def oneshot(jid):
            return {"id": jid, "name": jid, "prompt": "x",
                    "schedule": {"kind": "once", "run_at": "2020-01-01T09:00:00"}, "enabled": True,
                    "next_run_at": "2020-01-01T09:00:00", "deliver": "local"}

        # jobs.json order: create_job appends, so the one-shots come last.
        jobs = [recurring(0), recurring(1), oneshot("meeting-join"), recurring(2), oneshot("reminder"),
                {"id": "legacy", "name": "legacy", "prompt": "x", "schedule": "every 5m", "enabled": True,
                 "next_run_at": "2020-01-01T09:00:00", "deliver": "local"}]
        ran = []
        monkeypatch.setattr(sched, "get_due_jobs", lambda: jobs)
        monkeypatch.setattr(sched, "advance_next_runs", lambda ids: len(list(ids)))
        # The per-job body (claim, model run, delivery) is out of scope: record the
        # order the one worker picks jobs up in.
        monkeypatch.setattr(sched, "_process_due_job", lambda job, *_a, **_kw: ran.append(job["id"]) or True)
        try:
            assert sched.tick(verbose=False, sync=True) == 6
        finally:
            sched._shutdown_parallel_pool()
        assert ran == ["meeting-join", "reminder", "digest-0", "digest-1", "digest-2", "legacy"], ran
        # The tick lock it took is the tmp home's, never a real one.
        assert locks == [home / "cron" / ".tick.lock"], locks
