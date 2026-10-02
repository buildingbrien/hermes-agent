"""A cron run's "fleet" toolset cannot produce an ATTENDED teammate turn.

Lucaryin local conversation review (Sep 20 - Oct 1 2026), F21: patch 0037 gives the Team Huddle
(and any job whose prompt names ask_agent or fleet_send) the runtime's whole "fleet" toolset.
Every tool in it asks a teammate's bridge for a turn: ask_agent and delegate_to_neith on
/api/chat/sync, fleet_send on the bus (/api/bus/send). A turn asked for from a scheduled run
must not run with the teammate's attended trust — the inline approval wait, its always-approve
toggle and interactive grants, whose floor cannot see that the turn came from cron. So every one
of them marks a request made inside a cron run ``"unattended": true`` (tools/fleet_send.py
_unattended_fields), which the Lucaryin bridge turns into an unattended worker turn on both
paths (hermes-bridge server.py _mark_turn_unattended); outside a cron run nothing changes.

This walks the registry, so a NEW tool registered in the "fleet" toolset fails here until it
carries the marker too (add it to CALLS below once it does).

delegate_task's per-task ``agent`` (patch 0032) is the one other way a tool call starts a
teammate turn, and its hand-off runs on threads that do not carry the run's marker. A cron run
never had it (no fleet tools: the field was dropped and the hand-off refused), so a run that now
has the fleet tools keeps that (review of lucaryin-ai#140, round 5): the field is dropped from
the schema and the hand-off is refused, with nothing sent (TestDelegateTaskFromACronRun).
"""

from __future__ import annotations

import io
import json

import pytest

import tools.ask_agent_tool  # noqa: F401  (registers ask_agent)
import tools.delegate_neith  # noqa: F401  (registers delegate_to_neith)
import tools.fleet_send  # noqa: F401  (registers fleet_send)
from tools.registry import registry

# One call per fleet tool, with the args a model would send.
CALLS = {
    "ask_agent": {"agent": "neith", "question": "What did you ship today?"},
    "fleet_send": {"recipient": "ptah", "message": "Huddle notes for today."},
    "delegate_to_neith": {"task": "Find the Acme Series B announcement."},
}
TEAMMATE_TURN_ROUTES = ("/api/chat/sync", "/api/bus/send")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def wire(monkeypatch):
    """Every bridge call answered locally; the requests are recorded."""
    sent = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        body = json.loads(req.data.decode()) if req.data else {}
        sent.append((url, body))
        if url.endswith("/api/bus/send"):
            reply = {"success": True, "task_id": body.get("task_id", "t")}
        elif url.endswith("/api/pubsub/messages"):
            last = next((b for u, b in reversed(sent) if u.endswith("/api/bus/send")), {})
            reply = {"messages": [{"task_id": last.get("task_id", "t")}]}
        else:
            agent = body.get("agent_id") or "neith"
            reply = {"success": True, "response": "done", "profile": agent}
        return _Resp(json.dumps(reply).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("BRIDGE_PROFILE", "thoth")
    return sent


def _cron_var():
    from gateway.session_context import _VAR_MAP
    return _VAR_MAP["HERMES_CRON_SESSION"]


def _run(name):
    entry = registry.get_entry(name)
    assert entry is not None, name
    return entry.handler(dict(CALLS[name]), agent_id="thoth")


def test_every_fleet_tool_is_covered():
    assert set(registry.get_tool_names_for_toolset("fleet")) == set(CALLS), (
        "a tool joined or left the 'fleet' toolset: make it carry the unattended marker from a "
        "cron run (tools/fleet_send.py _unattended_fields) and add it to CALLS")


@pytest.mark.parametrize("name", sorted(CALLS))
def test_from_a_cron_run_every_teammate_turn_is_marked_unattended(wire, name):
    var = _cron_var()
    token = var.set("1")
    try:
        _run(name)
    finally:
        var.reset(token)
    turns = [(u, b) for u, b in wire if u.endswith(TEAMMATE_TURN_ROUTES)]
    assert turns, f"{name} asked no bridge for a turn"
    for url, body in turns:
        assert body.get("unattended") is True, (name, url, body)


@pytest.mark.parametrize("name", sorted(CALLS))
def test_an_attended_call_is_unchanged(wire, name):
    _run(name)
    turns = [(u, b) for u, b in wire if u.endswith(TEAMMATE_TURN_ROUTES)]
    assert turns, f"{name} asked no bridge for a turn"
    for url, body in turns:
        assert "unattended" not in body, (name, url, body)


# ── delegate_task's `agent` from a cron run (review of lucaryin-ai#140, round 5) ──

import types  # noqa: E402

from tools import delegate_tool, teammate_handoff  # noqa: E402

HAND_OFF = {"tasks": [{"goal": "Draft the weekly post", "agent": "ptah"}]}


def _huddle_parent(**extra):
    """A cron run's agent that has the fleet tools (patch 0037 gives them to the Team Huddle)."""
    return types.SimpleNamespace(valid_tool_names={"ask_agent", "fleet_send", "delegate_task"},
                                 _delegate_depth=0, **extra)


@pytest.fixture
def spawned(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return json.dumps({"results": [{"task_index": i, "status": "completed", "summary": "local"}
                                       for i, _ in enumerate(kw.get("tasks") or [])]})

    monkeypatch.setattr(delegate_tool, "_spawn_delegate_task", fake)
    return calls


class TestDelegateTaskFromACronRun:
    def test_a_hand_off_inside_the_cron_scope_is_refused_and_sends_nothing(self, wire, spawned):
        var = _cron_var()
        token = var.set("1")
        try:
            out = json.loads(delegate_tool.delegate_task(parent_agent=_huddle_parent(),
                                                         **json.loads(json.dumps(HAND_OFF))))
            planned = teammate_handoff.planned_handoffs(HAND_OFF, _huddle_parent())
        finally:
            var.reset(token)
        assert "not available in a scheduled run" in out["error"]
        assert "ask_agent" in out["error"]
        assert wire == [] and spawned == [] and planned == []

    def test_the_cron_agent_is_refused_even_without_the_session_var(self, wire, spawned):
        # The scheduler builds the run's agent with platform "cron" (_construct_cron_agent).
        out = json.loads(delegate_tool.delegate_task(parent_agent=_huddle_parent(platform="cron"),
                                                     **json.loads(json.dumps(HAND_OFF))))
        assert "not available in a scheduled run" in out["error"]
        assert wire == [] and spawned == []

    def test_the_legacy_top_level_agent_is_refused_too(self, wire, spawned):
        var = _cron_var()
        token = var.set("1")
        try:
            out = json.loads(delegate_tool.delegate_task(
                parent_agent=_huddle_parent(),
                tasks=teammate_handoff.fold_top_level_agent(
                    {"goal": "Draft the weekly post", "agent": "ptah"})))
        finally:
            var.reset(token)
        assert "not available in a scheduled run" in out["error"]
        assert wire == [] and spawned == []

    def test_without_agent_a_cron_run_still_spawns_its_own_subagent(self, wire, spawned):
        var = _cron_var()
        token = var.set("1")
        try:
            out = json.loads(delegate_tool.delegate_task(
                parent_agent=_huddle_parent(),
                tasks=[{"goal": "Draft the weekly post"}, {"goal": "Tidy notes", "agent": "thoth"}]))
        finally:
            var.reset(token)
        assert "error" not in out and wire == []
        assert [t.get("agent") for t in spawned[0]["tasks"]] == [None, None]

    def test_the_schema_drops_the_field_inside_the_cron_scope(self):
        td = {"type": "function", "function": {"name": "delegate_task", "parameters": {
            "type": "object", "properties": {"tasks": {"type": "array", "items": {
                "type": "object", "properties": {"goal": {"type": "string"},
                                                 "agent": {"type": "string"}}}}}}}}
        fleet = {"ask_agent", "fleet_send", "delegate_task"}
        var = _cron_var()
        token = var.set("1")
        try:
            cron = teammate_handoff.drop_agent_field_without_fleet(td, fleet)
        finally:
            var.reset(token)
        items = cron["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]
        assert "agent" not in items and "goal" in items
        chat = teammate_handoff.drop_agent_field_without_fleet(td, fleet)
        assert chat is td  # attended, with the fleet tools: unchanged

    def test_the_cached_definitions_keep_a_cron_run_and_a_chat_apart(self):
        # get_tool_definitions memoizes; a run's scope (the session var and the
        # non-dispatcher mark _CronRunScope sets) must never get a chat's schema
        # or hand a chat its own, in either order.
        import model_tools
        from agent.delegation_context import (enter_non_dispatcher_owned_context,
                                              exit_non_dispatcher_owned_context)

        def has_agent():
            defs = model_tools.get_tool_definitions(enabled_toolsets=["delegation", "fleet"],
                                                    quiet_mode=True)
            td = next(d for d in defs if d["function"]["name"] == "delegate_task")
            return "agent" in td["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]

        def in_cron():
            var = _cron_var()
            token, mark = var.set("1"), enter_non_dispatcher_owned_context()
            try:
                return has_agent()
            finally:
                exit_non_dispatcher_owned_context(mark)
                var.reset(token)

        model_tools._clear_tool_defs_cache()
        try:
            assert [in_cron(), has_agent(), in_cron(), has_agent()] == [False, True, False, True]
        finally:
            model_tools._clear_tool_defs_cache()

    def test_an_attended_hand_off_is_unchanged(self, wire, spawned):
        out = json.loads(delegate_tool.delegate_task(parent_agent=_huddle_parent(),
                                                     **json.loads(json.dumps(HAND_OFF))))
        assert out["handed_to"] == ["ptah"] and spawned == []
        turns = [b for u, b in wire if u.endswith("/api/chat/sync")]
        assert turns and all("unattended" not in b for b in turns)
