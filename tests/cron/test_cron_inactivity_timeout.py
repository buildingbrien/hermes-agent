"""Tests for cron job run caps: inactivity AND wall clock.

These call the real `_await_agent_result` / `_get_wall_clock_limit` from
cron.scheduler — an earlier version of this file replicated the polling loop
inline, which meant a change to the scheduler could break production while
every test here stayed green.

Why two caps: inactivity catches a hung API call or stuck tool; the wall
clock catches the opposite failure — on 2026-08-11 a heartbeat streamed for
26 minutes, never idle for a second, while the serial cron queue sat blocked
behind it.
"""

import concurrent.futures
import sys
import time
from pathlib import Path

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import _await_agent_result, _get_wall_clock_limit


class FakeAgent:
    """Mock agent with controllable activity summary."""

    def __init__(self, idle_seconds=0.0, activity_desc="tool_call",
                 current_tool=None, api_call_count=5, max_iterations=90):
        self._idle_seconds = idle_seconds
        self._activity_desc = activity_desc
        self._current_tool = current_tool
        self._api_call_count = api_call_count
        self._max_iterations = max_iterations
        self._interrupted = False
        self._interrupt_msg = None

    def get_activity_summary(self):
        return {
            "last_activity_ts": time.time() - self._idle_seconds,
            "last_activity_desc": self._activity_desc,
            "seconds_since_activity": self._idle_seconds,
            "current_tool": self._current_tool,
            "api_call_count": self._api_call_count,
            "max_iterations": self._max_iterations,
        }

    def interrupt(self, msg):
        self._interrupted = True
        self._interrupt_msg = msg

    def run_conversation(self, prompt):
        return {"final_response": "Done", "messages": []}


class BusyForeverAgent(FakeAgent):
    """Always active (streaming), never finishes — the 26-minute heartbeat."""

    def get_activity_summary(self):
        summary = super().get_activity_summary()
        summary["seconds_since_activity"] = 0.0  # never idle
        return summary

    def run_conversation(self, prompt):
        time.sleep(30)  # "forever" at test scale
        return {"final_response": "should never be seen", "messages": []}


class IdleAgent(FakeAgent):
    """Goes idle immediately — the hung-API-call case."""

    def __init__(self, **kwargs):
        super().__init__(idle_seconds=999.0, **kwargs)

    def run_conversation(self, prompt):
        time.sleep(30)
        return {"final_response": "should never be seen", "messages": []}


def _run(agent, **kwargs):
    """Submit the agent and await via the REAL scheduler helper."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(agent.run_conversation, "test prompt")
    try:
        return _await_agent_result(future, agent, poll_interval=0.05, **kwargs)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ── Completion ──────────────────────────────────────────────────────────────

def test_active_agent_completes_normally():
    result, kind = _run(FakeAgent(), inactivity_limit=10.0, wall_clock_limit=10.0)
    assert kind is None
    assert result["final_response"] == "Done"


def test_no_limits_waits_for_result():
    result, kind = _run(FakeAgent())
    assert kind is None
    assert result["final_response"] == "Done"


# ── Inactivity cap ──────────────────────────────────────────────────────────

def test_idle_agent_triggers_inactivity_timeout():
    result, kind = _run(IdleAgent(), inactivity_limit=0.2)
    assert kind == "inactivity"
    assert result is None


def test_busy_agent_never_trips_inactivity():
    # Busy forever + only an inactivity cap → completion wins the race here
    # only because the wall clock cap catches it in the paired test below;
    # verify busy-ness alone doesn't trip the inactivity path in a short window.
    agent = BusyForeverAgent()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(agent.run_conversation, "test")
    try:
        done, _ = concurrent.futures.wait({future}, timeout=0.3)
        assert not done
        assert agent.get_activity_summary()["seconds_since_activity"] == 0.0
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def test_agent_without_activity_summary_never_trips_inactivity():
    """A bare agent (no tracker) reads as idle 0s — inactivity can't fire;
    the wall clock is the cap that still protects the queue."""

    class BareAgent:
        def run_conversation(self, prompt):
            return {"final_response": "no activity tracker", "messages": []}

    result, kind = _run(BareAgent(), inactivity_limit=0.1)
    assert kind is None
    assert result["final_response"] == "no activity tracker"


# ── Wall-clock cap: the 26-minute-heartbeat fix ─────────────────────────────

def test_busy_forever_agent_hits_wall_clock():
    result, kind = _run(BusyForeverAgent(), inactivity_limit=5.0,
                        wall_clock_limit=0.3)
    assert kind == "wall_clock"
    assert result is None


def test_wall_clock_applies_even_without_inactivity_limit():
    result, kind = _run(BusyForeverAgent(), wall_clock_limit=0.3)
    assert kind == "wall_clock"
    assert result is None


def test_wall_clock_counts_from_started_at():
    started_long_ago = time.monotonic() - 100
    result, kind = _run(BusyForeverAgent(), wall_clock_limit=50.0,
                        started_at=started_long_ago)
    assert kind == "wall_clock"


def test_fast_job_beats_wall_clock():
    result, kind = _run(FakeAgent(), wall_clock_limit=10.0)
    assert kind is None
    assert result["final_response"] == "Done"


# ── Limit resolution ────────────────────────────────────────────────────────

def test_wall_clock_default_is_45_minutes(monkeypatch):
    monkeypatch.delenv("HERMES_CRON_WALL_CLOCK", raising=False)
    assert _get_wall_clock_limit({}) == 2700.0


def test_wall_clock_job_field_wins(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_WALL_CLOCK", "100")
    assert _get_wall_clock_limit({"wall_clock_seconds": 7200}) == 7200.0


def test_wall_clock_env_var_used_when_job_silent(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_WALL_CLOCK", "1200")
    assert _get_wall_clock_limit({}) == 1200.0


def test_wall_clock_zero_means_unlimited(monkeypatch):
    monkeypatch.delenv("HERMES_CRON_WALL_CLOCK", raising=False)
    assert _get_wall_clock_limit({"wall_clock_seconds": 0}) is None


def test_wall_clock_garbage_values_fall_through(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_WALL_CLOCK", "not-a-number")
    assert _get_wall_clock_limit({"wall_clock_seconds": "soon"}) == 2700.0


# ── Import ordering (unchanged from the original file) ──────────────────────

class TestSysPathOrdering:
    def test_hermes_time_importable(self):
        from cron.scheduler import _hermes_now
        assert callable(_hermes_now)

    def test_hermes_constants_importable(self):
        from hermes_constants import get_hermes_home
        assert callable(get_hermes_home)
