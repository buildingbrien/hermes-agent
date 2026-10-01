"""Lucaryin fold 0035 (review of lucaryin-ai#141, F56; pair contract with lucaryin-ai
hermes-bridge/disk_guard.py).

The bridge holds scheduled runs while the computer is almost out of disk space (below 1 GB free by
default) and records each hold in <cron dir>/.disk_hold.json. The app is running the whole time,
so no downtime window covers the slot, and a job that came due during the hold and then ran as a
catch-up was told it was late because "the app was closed, the computer was asleep, or earlier
scheduled jobs ran long", and to say "it was missed because the app was not running then".

Hermetic: the cron store fixture with a pinned clock; the hold file is written by the test.
"""

import json
from datetime import datetime, timezone

import pytest

from cron.scheduler_prompt import _build_job_prompt, _catch_up_note

NOW = datetime(2026, 9, 22, 18, 30, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 9, 22, 15, 0, 0, tzinfo=timezone.utc)         # Inbox Triage, during the hold
HOLD_FROM = datetime(2026, 9, 22, 14, 58, 0, tzinfo=timezone.utc).timestamp()
HOLD_UNTIL = datetime(2026, 9, 22, 18, 29, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: NOW)
    (tmp_path / "cron").mkdir(parents=True)
    return tmp_path / "cron"


def _catch_up_job():
    return {
        "id": "inbox_triage", "name": "Inbox Triage", "prompt": "Triage the inbox.",
        "type": "prompt", "schedule": {"kind": "cron", "expr": "0 15 * * *"},
        "next_run_at": DUE.isoformat(), "enabled": True, "state": "scheduled",
        "repeat": {"times": None, "completed": 0}, "deliver": "local",
        "last_dispatch": {"scheduled_at": DUE.isoformat(), "dispatched_at": NOW.isoformat(),
                          "lateness_seconds": (NOW - DUE).total_seconds(), "kind": "catch_up"},
        "_scheduled_instant": DUE.isoformat(),
    }


def _hold(cron_dir, data):
    (cron_dir / ".disk_hold.json").write_text(json.dumps(data), encoding="utf-8")


class TestDiskHoldCatchUpNote:
    def test_a_finished_hold_is_named_as_the_cause(self, cron_dir):
        _hold(cron_dir, {"holding_since": None, "recent": [{"from": HOLD_FROM, "until": HOLD_UNTIL}]})
        note = _catch_up_note(_catch_up_job())
        assert "paused while this computer was almost out of disk space" in note
        assert "missed because the computer was almost out of disk space then" in note
        assert "app was not running" not in note
        assert "the app was closed" not in note

    def test_a_hold_still_in_force_counts_until_now(self, cron_dir):
        _hold(cron_dir, {"holding_since": HOLD_FROM, "recent": []})
        assert "almost out of disk space" in _catch_up_note(_catch_up_job())

    def test_a_downtime_window_covering_the_slot_wins(self, cron_dir):
        _hold(cron_dir, {"holding_since": None, "recent": [{"from": HOLD_FROM, "until": HOLD_UNTIL}]})
        (cron_dir / ".scheduler_downtime.json").write_text(
            json.dumps({"from": HOLD_FROM - 60, "until": HOLD_UNTIL}), encoding="utf-8")
        note = _catch_up_note(_catch_up_job())
        assert "Lucaryin app was not running" in note
        assert "disk space" not in note

    def test_a_hold_on_another_day_is_not_used(self, cron_dir):
        _hold(cron_dir, {"holding_since": None,
                         "recent": [{"from": HOLD_FROM + 86400, "until": HOLD_UNTIL + 86400}]})
        note = _catch_up_note(_catch_up_job())
        assert "disk space" not in note
        assert "scheduler did not run at that time" in note

    @pytest.mark.parametrize("raw", ["not json", "[]", json.dumps({"recent": [{"from": "x"}]})])
    def test_an_unreadable_hold_file_is_ignored(self, cron_dir, raw):
        (cron_dir / ".disk_hold.json").write_text(raw, encoding="utf-8")
        assert "scheduler did not run at that time" in _catch_up_note(_catch_up_job())

    def test_the_note_passes_the_prompt_injection_scanner(self, cron_dir):
        _hold(cron_dir, {"holding_since": None, "recent": [{"from": HOLD_FROM, "until": HOLD_UNTIL}]})
        prompt = _build_job_prompt(_catch_up_job())
        assert "almost out of disk space" in prompt
        assert "Triage the inbox." in prompt
