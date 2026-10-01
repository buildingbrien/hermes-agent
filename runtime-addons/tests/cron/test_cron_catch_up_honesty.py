"""Lucaryin fold 0027 (review F16 / F42 / F46, founder's Mac, Sep 20-28; pair contract with
lucaryin-ai hermes-bridge/cron_tick_support.py classes 6 and 7).

F16 — the app and every bridge were down from ~2026-09-23T13:06Z to 03:56:44Z on Sep 24. The
weekly AD|BC Sync clerk join (job 595a2a80b875, '25 12 * * 3' = 16:25Z, a prompt job carrying an
unattended outbound_call grant to the meeting's dial-in) fired as a catch-up 41,521 s late, at
midnight. The run declined to dial (model judgement was the only guard), then told the founder
"The cron schedule is misconfigured — it should fire ~12:25 PM ET on Wednesdays (e.g.
25 12 * * 3)": exactly the stored schedule.
  1. A time-bound RECURRING job past its window (the one-shot window: 30 min, or until its
     meeting ends) is skipped, stamped ``last_missed``, and kept on its schedule — never caught
     up. A job scanned late because a serial tick ran long (app up the whole time) inside that
     window still fires (review of the first cut, 2026-10-01: a 10-minute window skipped it).
  2. A catch-up run's prompt says it is late, by how much and why (with the bridge's downtime
     window when it covers the slot), and that the schedule is fine.

F42 — the one-month trademark reminder (36276a62b94d) was written and then delivered nowhere:
its origin, phone thread mobile_3b0042fa_1787587612 (Aug 24), had been archived. The execution
row said completed.
  3. A lucaryin target session that is gone delivers to the owner's current conversation, with a
     one-line note; only when there is none is it an error.
  4. An execution whose run succeeded but whose message reached nobody is finished as failed —
     and still counts as a completed occurrence for the at-most-once guard.

F46 — Morning Briefing's origin was a leftover test chat (meeting_chain_test_20260818).
  5. A test chat is never a delivery target, explicit or fallback; the fallback is the bridge's
     owner_main_thread rule (latest HUMAN-typed message on web/mobile/telegram, never a machine
     id such as a voice driver_ session, never a group chat, no message-count floor).

Hermetic: the cron store fixture with a pinned clock and the real due scan; a real SessionDB in
the per-test HERMES_HOME.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from cron import scheduler as _sched  # noqa: F401 — binds the delivery module's late-bound refs
from cron import scheduler_delivery as delivery
from cron.jobs import get_due_jobs, load_jobs, save_jobs
from cron.scheduler_prompt import _build_job_prompt, _catch_up_note
from hermes_state import SessionDB

NOW = datetime(2026, 9, 24, 3, 57, 1, tzinfo=timezone.utc)          # the Sep 24 relaunch tick
ADBC_DUE = datetime(2026, 9, 23, 16, 25, 0, tzinfo=timezone.utc)     # 12:25 EDT, Wednesday
LATENESS = (NOW - ADBC_DUE).total_seconds()                           # 41,521 s
DOWN_FROM = datetime(2026, 9, 23, 13, 6, 0, tzinfo=timezone.utc).timestamp()
BACK_AT = datetime(2026, 9, 24, 3, 56, 44, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def clock(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")

    def set_now(now):
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
    set_now(NOW)
    return tmp_path / "cron"


def _weekly(jid, due=ADBC_DUE, **extra):
    job = {
        "id": jid, "name": jid, "prompt": "Dial in to the AD|BC Sync as a clerk.", "type": "prompt",
        "schedule": {"kind": "cron", "expr": "25 16 * * 3"},
        "next_run_at": due.isoformat(), "last_run_at": None, "enabled": True,
        "state": "scheduled", "repeat": {"times": None, "completed": 0}, "deliver": "local",
    }
    job.update(extra)
    return job


DIAL_GRANT = {"action": "outbound_call", "to": "+15550100", "unattended": True}


# ── 1. time-bound recurring jobs are missed, not caught up ───────────────────

class TestTimeBoundRecurring:
    def test_sep23_adbc_sync_is_skipped_and_stamped_not_caught_up(self, clock):
        save_jobs([_weekly("595a2a80b875", grants=[DIAL_GRANT])])
        assert get_due_jobs() == []
        rec = load_jobs()[0]
        assert rec["last_missed"]["scheduled_at"] == ADBC_DUE.isoformat()
        assert rec["last_missed"]["lateness_seconds"] == pytest.approx(LATENESS, abs=1)
        assert rec["last_missed"]["window_seconds"] == 1800
        assert rec["last_missed"]["detected_at"] == NOW.isoformat()
        nxt = datetime.fromisoformat(rec["next_run_at"])
        assert nxt > NOW, "kept on its schedule: next Wednesday"
        assert rec["enabled"] and rec.get("state") == "scheduled"
        assert "pending_slot" not in rec

    def test_an_ordinary_job_still_catches_up(self, clock):
        save_jobs([_weekly("weekly_digest")])
        due = get_due_jobs()
        assert [d["id"] for d in due] == ["weekly_digest"]
        assert due[0]["last_dispatch"]["kind"] == "catch_up"
        assert "last_missed" not in load_jobs()[0]

    @pytest.mark.parametrize("extra", [
        {"meeting": {"label": "AD|BC Sync", "dial_number": "+15550100"}},
        {"type": "meeting_join"},
        {"grants": [DIAL_GRANT]},
    ])
    def test_what_makes_a_job_time_bound(self, clock, extra):
        save_jobs([_weekly("tb", **extra)])
        assert get_due_jobs() == []
        assert load_jobs()[0]["last_missed"]["window_seconds"] == 1800

    def test_a_few_minutes_late_still_fires(self, clock, monkeypatch):
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: ADBC_DUE + timedelta(minutes=4))
        save_jobs([_weekly("595a2a80b875", grants=[DIAL_GRANT])])
        assert [d["id"] for d in get_due_jobs()] == ["595a2a80b875"]
        assert "last_missed" not in load_jobs()[0]

    @pytest.mark.parametrize("minutes_late", [9, 15, 25, 29])
    def test_a_busy_serial_tick_inside_the_window_still_fires(self, clock, monkeypatch, minutes_late):
        """App up the whole time; the scan ran late because the tick before it lasted as long as
        all its due jobs (HERMES_CRON_MAX_PARALLEL=1). The base fires this as 'late'; so must we."""
        monkeypatch.setattr("cron.jobs._hermes_now",
                            lambda: ADBC_DUE + timedelta(minutes=minutes_late))
        save_jobs([_weekly("595a2a80b875", grants=[DIAL_GRANT])])
        due = get_due_jobs()
        assert [d["id"] for d in due] == ["595a2a80b875"]
        assert due[0]["last_dispatch"]["kind"] in ("late", "on_time", "catch_up")
        assert "last_missed" not in load_jobs()[0]

    def test_past_the_window_it_is_missed(self, clock, monkeypatch):
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: ADBC_DUE + timedelta(minutes=45))
        save_jobs([_weekly("595a2a80b875", grants=[DIAL_GRANT])])
        assert get_due_jobs() == []
        missed = load_jobs()[0]["last_missed"]
        assert missed["window_seconds"] == 1800
        assert missed["lateness_seconds"] == pytest.approx(45 * 60, abs=1)

    def test_a_standing_meeting_may_still_be_joined_until_it_ends(self, clock, monkeypatch):
        """A recurring job carrying a 90-minute meeting (stored as an earlier week's occurrence):
        its window is the meeting's length, not 30 min."""
        meeting = {"label": "AD|BC Sync", "dial_number": "+15550100",
                   "start_iso": (ADBC_DUE - timedelta(days=7, minutes=-5)).isoformat(),
                   "end_iso": (ADBC_DUE - timedelta(days=7, minutes=-95)).isoformat()}
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: ADBC_DUE + timedelta(minutes=80))
        save_jobs([_weekly("standing", meeting=meeting)])
        assert [d["id"] for d in get_due_jobs()] == ["standing"]
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: ADBC_DUE + timedelta(minutes=100))
        save_jobs([_weekly("standing", meeting=meeting)])
        assert get_due_jobs() == []
        assert load_jobs()[0]["last_missed"]["window_seconds"] == 90 * 60

    def test_this_occurrences_own_meeting_end_is_used(self, clock, monkeypatch):
        meeting = {"start_iso": (ADBC_DUE + timedelta(minutes=5)).isoformat(),
                   "end_iso": (ADBC_DUE + timedelta(hours=2)).isoformat()}
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: ADBC_DUE + timedelta(minutes=110))
        save_jobs([_weekly("today", type="meeting_join", meeting=meeting)])
        assert [d["id"] for d in get_due_jobs()] == ["today"], "the meeting is still on"

    def test_explicit_window_is_honoured_and_clamped(self, clock, monkeypatch):
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: ADBC_DUE + timedelta(minutes=50))
        save_jobs([_weekly("long", grants=[DIAL_GRANT], late_fire_window_seconds=3600),
                   _weekly("short", late_fire_window_seconds=5)])
        due = {d["id"] for d in get_due_jobs()}
        assert "long" in due, "an hour-long window still fires 50 min late"
        assert "short" not in due, "5 s clamps to 120 s; 50 min late is missed"
        stamped = {j["id"]: j.get("last_missed") for j in load_jobs()}
        assert stamped["short"]["window_seconds"] == 120

    def test_manual_run_is_never_skipped(self, clock):
        job = _weekly("595a2a80b875", grants=[DIAL_GRANT])
        job["manual_run_at"] = job["next_run_at"]
        save_jobs([job])
        assert [d["id"] for d in get_due_jobs()] == ["595a2a80b875"]


# ── 2. the catch-up note ─────────────────────────────────────────────────────

def _catch_up_job(**extra):
    job = _weekly("595a2a80b875")
    job.update({
        "last_dispatch": {"scheduled_at": ADBC_DUE.isoformat(), "dispatched_at": NOW.isoformat(),
                          "lateness_seconds": round(LATENESS, 1), "kind": "catch_up"},
        "_scheduled_instant": ADBC_DUE.isoformat(),
    })
    job.update(extra)
    return job


class TestCatchUpNote:
    def test_due_scan_stamp_reaches_the_prompt(self, clock):
        save_jobs([_weekly("weekly_digest")])
        due = get_due_jobs()[0]
        note = _catch_up_note(due)
        assert note.startswith("## Scheduling note")
        assert "11 h 32 min late" in note

    def test_with_the_bridge_downtime_record_it_says_when_the_app_was_down(self, clock):
        clock.mkdir(parents=True, exist_ok=True)
        (clock / ".scheduler_downtime.json").write_text(
            json.dumps({"from": DOWN_FROM, "until": BACK_AT}), encoding="utf-8")
        prompt = _build_job_prompt(_catch_up_job())
        assert "## Scheduling note" in prompt
        assert "Lucaryin app was not running" in prompt
        assert "is not misconfigured" in prompt
        assert "do not suggest changing it" in prompt
        assert "Dial in to the AD|BC Sync as a clerk." in prompt, "the task itself is kept"

    def test_without_a_record_it_does_not_invent_one(self, clock):
        note = _catch_up_note(_catch_up_job())
        assert "scheduler did not run at that time" in note
        assert "Lucaryin app was not running" not in note

    def test_an_earlier_window_kept_in_recent_is_used(self, clock):
        """Dark-wake cycles on a sleeping laptop record several windows; the latest one (top
        level) need not be the one that covers the slot."""
        clock.mkdir(parents=True, exist_ok=True)
        later = {"from": BACK_AT + 600, "until": BACK_AT + 4000}
        (clock / ".scheduler_downtime.json").write_text(json.dumps(dict(
            later, recent=[{"from": DOWN_FROM, "until": BACK_AT}, later])), encoding="utf-8")
        note = _catch_up_note(_catch_up_job())
        assert "Lucaryin app was not running" in note

    def test_a_downtime_record_from_another_day_is_not_used(self, clock):
        clock.mkdir(parents=True, exist_ok=True)
        (clock / ".scheduler_downtime.json").write_text(
            json.dumps({"from": BACK_AT + 86400, "until": BACK_AT + 90000}), encoding="utf-8")
        assert "Lucaryin app was not running" not in _catch_up_note(_catch_up_job())

    def test_on_time_late_and_manual_runs_get_no_note(self, clock):
        on_time = _catch_up_job()
        on_time["last_dispatch"] = dict(on_time["last_dispatch"], kind="late")
        assert _catch_up_note(on_time) == ""
        manual = _catch_up_job(_scheduled_instant=None)   # trigger_job: no scheduled instant
        assert _catch_up_note(manual) == ""
        stale = _catch_up_job(_scheduled_instant="2026-09-30T16:25:00+00:00")
        assert _catch_up_note(stale) == "", "a stamp from an earlier occurrence"

    def test_the_note_passes_the_prompt_injection_scanner(self, clock):
        # _build_job_prompt raises CronPromptInjectionBlocked on a hit; the strict tier applies
        # when there are no skills and no injected data.
        assert "Scheduling note" in _build_job_prompt(_catch_up_job())


# ── 3 + 5. delivery fallback ─────────────────────────────────────────────────

MAIN = "mobile_3b0042fa_1790539238"
ARCHIVED = "mobile_3b0042fa_1787587612"
TEST_CHAT = "meeting_chain_test_20260818"


def _seed(session_id, source, n):
    with SessionDB() as db:
        db.create_session(session_id, source)
        for i in range(n):
            db.append_message(session_id=session_id, role="user", content=f"{session_id} #{i}")


def _messages(session_id):
    with SessionDB() as db:
        return db.get_messages(session_id)


class TestDeliveryFallback:
    def test_archived_origin_goes_to_the_current_conversation_with_a_note(self):
        _seed(MAIN, "mobile", 25)
        job = {"id": "36276a62b94d", "name": "Trademark reminder"}
        err = delivery._deliver_to_lucaryin_session(
            job, ARCHIVED, "Would you like me to proceed with the trademark filing now — yes or no?")
        assert err is None
        last = _messages(MAIN)[-1]
        assert last["role"] == "assistant"
        assert "no longer available" in last["content"]
        assert "trademark filing now" in last["content"]

    def test_archived_origin_with_no_conversation_at_all_is_still_an_error(self):
        _seed("cli_only", "cli", 25)
        err = delivery._deliver_to_lucaryin_session({"id": "x"}, ARCHIVED, "text")
        assert err and "not found in state.db" in err

    def test_a_test_chat_origin_delivers_to_the_real_conversation(self):
        _seed(MAIN, "mobile", 25)
        time.sleep(0.01)
        _seed(TEST_CHAT, "web", 30)       # newer AND busier — still never the target
        err = delivery._deliver_to_lucaryin_session({"id": "658aa2d46601"}, TEST_CHAT, "Briefing")
        assert err is None
        assert _messages(MAIN)[-1]["content"] == "Briefing", "no 'earlier conversation' note"
        assert all(m["role"] == "user" for m in _messages(TEST_CHAT))

    def test_fallback_never_picks_a_test_chat(self):
        _seed(MAIN, "mobile", 25)
        time.sleep(0.01)
        _seed(TEST_CHAT, "web", 30)
        assert delivery._latest_lucaryin_session() == MAIN

    def test_f42_replay_a_newer_voice_driver_session_is_never_the_owners_thread(self):
        """Review replay, 2026-10-01: a 24-row driver_ voice session (source 'web') newer than
        the founder's phone thread got the trademark reminder under the 0023 rule, and the run
        counted as delivered — F42's silent miss again."""
        _seed(MAIN, "mobile", 3)
        time.sleep(0.01)
        _seed("driver_MZ5f170456fe8660e2ae50cae9f4fb06b8", "web", 24)
        err = delivery._deliver_to_lucaryin_session(
            {"id": "36276a62b94d"}, ARCHIVED, "proceed with the trademark filing — yes or no?")
        assert err is None
        assert "trademark filing" in _messages(MAIN)[-1]["content"]
        assert all(m["role"] == "user"
                   for m in _messages("driver_MZ5f170456fe8660e2ae50cae9f4fb06b8"))

    def test_a_fresh_thread_the_owner_is_typing_in_wins_without_a_message_floor(self):
        _seed("20260901_090000_oldchat", "web", 40)
        time.sleep(0.01)
        _seed("mobile_3b0042fa_1790600000", "mobile", 2)
        assert delivery._latest_lucaryin_session() == "mobile_3b0042fa_1790600000"

    @pytest.mark.parametrize("sid", [
        "cron_05ad7960a10b_20260927_200150", "inbound_CA123", "dialer_x", "voice_abc",
        "meeting_join_1", "outbound_9", "board_trigger_mg1", "fleet_set_1", "subagent_7"])
    def test_machine_ids_minted_as_web_never_win(self, sid):
        _seed(MAIN, "mobile", 3)
        time.sleep(0.01)
        _seed(sid, "web", 30)
        assert delivery._latest_lucaryin_session() == MAIN

    def test_a_group_chat_session_never_wins(self):
        _seed(MAIN, "mobile", 3)
        time.sleep(0.01)
        with SessionDB() as db:
            db.create_session("20260926_185549_65c4b4", "web")
            db.append_message(session_id="20260926_185549_65c4b4", role="user",
                              content="[Group Chat — Team] assignment response #2")
        assert delivery._latest_lucaryin_session() == MAIN

    def test_a_telegram_thread_is_a_human_surface(self):
        _seed(MAIN, "mobile", 3)
        time.sleep(0.01)
        _seed("tg_123456", "telegram", 1)
        assert delivery._latest_lucaryin_session() == "tg_123456"

    def test_an_existing_origin_is_untouched(self):
        _seed(MAIN, "mobile", 25)
        _seed("20260924_233813_1e26ea", "web", 25)
        assert delivery._deliver_to_lucaryin_session({"id": "j"}, "20260924_233813_1e26ea", "hi") is None
        assert _messages("20260924_233813_1e26ea")[-1]["content"] == "hi"


# ── 4. an undelivered run is not "completed" ─────────────────────────────────

class TestUndeliveredExecution:
    def _finish(self, monkeypatch, delivery_error):
        finished = []
        monkeypatch.setattr(_sched, "mark_job_run", lambda *a, **k: True)
        monkeypatch.setattr(_sched, "finish_execution", lambda *a, **k: finished.append((a, k)))
        d = _sched._RunDelivery(job={"id": "36276a62b94d", "deliver": "origin",
                                     "origin": {"platform": "lucaryin", "chat_id": ARCHIVED}},
                                success=True, error=None, delivery_attempted=True,
                                delivery_error=delivery_error, should_deliver=True)
        assert _sched._finish_completed_run(d, None, "exec-1") is True
        return finished[-1][1]

    def test_undelivered_run_is_finished_as_failed(self, monkeypatch):
        kw = self._finish(monkeypatch, f"lucaryin delivery target session {ARCHIVED} not found in state.db")
        assert kw["success"] is False
        assert kw["error"].startswith("not delivered: ")
        assert kw["delivery_outcome"] == "failed"

    def test_delivered_run_is_completed(self, monkeypatch):
        kw = self._finish(monkeypatch, None)
        assert kw["success"] is True
        assert kw["delivery_outcome"] == "delivered"

    def test_a_meeting_join_web_origin_gap_is_not_an_undelivered_run(self, monkeypatch):
        """Until F17 re-points meeting_join origins, every join ends "unknown platform 'web'";
        the bridge posts the call's own outcome, so the row stays completed."""
        finished = []
        monkeypatch.setattr(_sched, "mark_job_run", lambda *a, **k: True)
        monkeypatch.setattr(_sched, "finish_execution", lambda *a, **k: finished.append(k))
        d = _sched._RunDelivery(job={"id": "35548a963a3c", "type": "meeting_join",
                                     "deliver": "origin",
                                     "origin": {"platform": "web", "chat_id": MAIN}},
                                success=True, error=None, delivery_attempted=True,
                                delivery_error="unknown platform 'web'", should_deliver=True)
        _sched._finish_completed_run(d, None, "exec-2")
        assert finished[-1]["success"] is True
        # Any other delivery error on a meeting join still counts.
        assert not _sched._meeting_join_web_origin_gap(
            {"type": "meeting_join"}, "unknown platform 'web'; lucaryin delivery failed: x")
        assert not _sched._meeting_join_web_origin_gap({"type": "prompt"}, "unknown platform 'web'")


class TestAtMostOnceGuard:
    """An undelivered run still ran: its side effects (an email, a dial) happened, so the
    occurrence must never fire again through re-anchor, DST migration or an external claim."""

    def _row(self, job_id, instant, *, success, error=None):
        from cron.executions import create_execution, finish_execution
        ex = create_execution(job_id, source="test", scheduled_instant=instant)
        finish_execution(ex["id"], success=success, error=error)

    def test_undelivered_row_counts_as_completed_occurrence(self):
        from cron.occurrences import UNDELIVERED_ERROR_PREFIX, completed_occurrence
        instant = ADBC_DUE.isoformat()
        self._row("36276a62b94d", instant, success=False,
                  error=f"{UNDELIVERED_ERROR_PREFIX}lucaryin delivery target session x not found")
        assert completed_occurrence({"id": "36276a62b94d"}, instant)

    def test_a_real_failure_stays_eligible(self):
        from cron.occurrences import completed_occurrence
        instant = ADBC_DUE.isoformat()
        self._row("weekly_digest", instant, success=False, error="HTTP 429 rate limit")
        assert not completed_occurrence({"id": "weekly_digest"}, instant)
        self._row("weekly_digest", instant, success=True)
        assert completed_occurrence({"id": "weekly_digest"}, instant)
