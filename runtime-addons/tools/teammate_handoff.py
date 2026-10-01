#!/usr/bin/env python3
"""teammate_handoff — delegate_task's explicit teammate target (routing decision 6).

A delegate_task task may name the teammate who should do it, by agent id, in
its own ``agent`` field (patch 0032 advertises the field in
DELEGATE_TASK_SCHEMA and routes delegate_task through ``hand_off`` below).
When a task carries the field, the field alone decides who gets it:

* the task goes to that teammate's own bridge the way ``ask_agent`` asks one
  (POST /api/chat/sync with the fleet delegation budget and the bridge bearer),
  and the teammate's answer comes back as that task's result;
* it never ALSO runs as a local subagent of the caller. Before this, the only
  way to hand a delegate_task to a teammate was to name it in the goal; the
  bridge read the wording, published the goal to that teammate, and the local
  subagent ran the same goal as well.

The legacy single-goal shape (top-level ``goal``, no ``tasks``) with a
top-level ``agent`` is folded into a one-task list carrying that agent
(``fold_top_level_agent``, called by run_agent's dispatch and the registry
handler), so it is handed off the same way instead of running locally while
the bridge also publishes it.

Without the field nothing here changes upstream's delegate_task: the call is
passed through untouched. The bridge worker reads the same field first and
falls back to the goal's wording only when no task carries it (lucaryin-ai
hermes-bridge/worker.py ``detect_bridge_handoffs``); it asks
``planned_handoffs`` which tasks will really leave, so its hand-off bubbles
never claim one that is refused.

Rules:

* The value is an agent id: thoth, neith, ptah or set. A display name ("Fox",
  "Merlin", a name the owner chose) is refused with the list of ids, never
  guessed at; on a box where the owner renamed agents, a name can belong to a
  different agent than its default.
* Your own id means "do it yourself": that task spawns as a local subagent.
* Before anything leaves, the whole call is checked the way upstream checks a
  spawn: the operator spawn pause, the task list, output schemas and images
  (upstream's own validators), and, when some tasks spawn locally, the spawn
  depth limit. A call that fails any of them is refused and nothing is sent.
* A hand-off needs the fleet tools in this session (``ask_agent``). Where they
  are not enabled (a cron job's toolset, the coding posture) the call is
  refused, so delegate_task never widens a toolset boundary, and the schema
  rewrite drops the field there (``drop_agent_field_without_fleet``).
* A scheduled (cron) run never hands off, even when its job has the fleet
  tools (patch 0037 gives them to the Team Huddle): the hand-off runs on
  threads that do not carry the run's scheduled marker, so the teammate
  would answer an ATTENDED turn (Lucaryin review of #140, round 5). The call
  is refused and the field is dropped there, as before the job had the
  fleet tools; the job asks a teammate with ``ask_agent``, which marks the
  request unattended (tools/fleet_send.py ``_unattended_fields``).
* Over the fleet hop budget, or back to an agent already in the chain, a task
  is refused locally with the reason (tools/fleet_budget.py), as
  delegate_to_neith does. The receiving bridge enforces the same budget. The
  fleet hop budget, not the local spawn depth, governs hand-offs: the bridge
  worker seeds the spawn depth from the fleet hops, so the local cap would
  refuse every hand-off from a delegated turn while ask_agent allows it.
* A teammate that times out, is busy or fails is reported as unfinished, with
  the facts ask_agent reports (still running, files written) and guidance not
  to tell the user it was done or write over its files (review finding F19).
  ask_agent's structured ``status`` decides; its error wording is only read
  when it carries none.
* A task with an output_schema gets the answer checked with upstream's
  validator and one correction re-ask, as a local subagent does.

Several tasks for the SAME teammate in one call run one after another;
different teammates run in parallel. Within one delegate_task call a teammate
never gets two of these turns at once (review finding F32: two runs of one
task drove the same signed-in tab). Separate calls, and other sessions, are
the receiving bridge's single-flight guard's job.

Hand-offs run synchronously inside the tool call (a top-level local
delegate_task runs in the background). While they run, a status note goes out
every ``_HEARTBEAT_S`` seconds so the chat does not read the wait as a stall.

Importable on its own: module-level imports are stdlib only (the bridge
worker imports it to ask ``planned_handoffs``).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

#: The fleet's agent ids, the only values ``agent`` takes. The same set as
#: ask_agent_tool.AGENT_PORTS, fleet_send.AGENT_PORTS, the enum patch 0032 puts
#: in the schema, and the bridge worker's FLEET_AGENT_IDS (pinned by tests).
FLEET_AGENT_IDS: Tuple[str, ...] = ("neith", "ptah", "set", "thoth")

#: The tool a session must have for a hand-off to be allowed (toolset "fleet").
HANDOFF_REQUIRES_TOOL = "ask_agent"

#: Seconds between "still waiting" notes while hand-offs run. The desktop chat
#: reaps a stream after 600 s with no event; one hand-off can take 320 s, twice
#: that with a schema correction, and tasks for one teammate run back to back.
_HEARTBEAT_S = 120.0

_CONTROL_ACTIONS = ("list", "steer", "stop")

#: ask_agent statuses for a teammate that has not finished the task.
_UNFINISHED_STATUSES = ("timeout", "busy", "duplicate")
#: Facts ask_agent reports about an unfinished task, carried into the entry.
_CARRIED_FACTS = ("still_running", "files_written", "late_window_s", "started_at")

_NO_RETRY_GUIDANCE = (
    "Do not retry the hand-off. Tell the user plainly what happened; do the "
    "task yourself only if they want that."
)
_FAILED_GUIDANCE = (
    "{agent} did not take the task, so it is NOT done. Tell the user plainly, "
    "and never invent {agent}'s answer."
)
_TIMEOUT_GUIDANCE = (
    "{agent} did not answer in time, so the task is unfinished. Never tell the "
    "user {agent} finished it. If you redo it, read whatever {agent} already "
    "wrote first and do not write over it."
)
_BUSY_GUIDANCE = (
    "{agent} is already working on this, so this copy was not started and the "
    "task is unfinished. Do not send it again now; the earlier answer comes "
    "back when it finishes. Tell the user it is in progress, never that it is done."
)
_SCHEMA_GUIDANCE = (
    "{agent} answered, but not in the requested shape (see schema_errors), so "
    "the task did not complete. Check the summary yourself before using any of "
    "it, and tell the user plainly what is missing."
)


def agent_field(task: Any) -> Optional[str]:
    """A task's ``agent`` value as given (stripped), or None when it is absent,
    null or blank. Non-string values come back as their text, so they are
    present and then refused as unknown rather than silently ignored."""
    if not isinstance(task, dict):
        return None
    value = task.get("agent")
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else str(value).strip()
    return text or None


def normalize_agent_id(value: str) -> Optional[str]:
    """The agent id ``value`` names exactly (case and surrounding spaces
    ignored), or None. Display names and near-misses are not ids."""
    key = (value or "").strip().lower()
    return key if key in FLEET_AGENT_IDS else None


def sender_id() -> str:
    """This agent's id, as fleet_send and delegate_to_neith read it."""
    return (os.environ.get("BRIDGE_PROFILE", "") or "thoth").strip().lower()


def fleet_tools_enabled(parent_agent: Any) -> bool:
    """True when this session may hand work to teammates (it has ask_agent)."""
    names = getattr(parent_agent, "valid_tool_names", None)
    try:
        return HANDOFF_REQUIRES_TOOL in names
    except TypeError:
        return False


def in_scheduled_run(parent_agent: Any = None) -> bool:
    """True inside a cron run: the agent the scheduler builds (platform
    "cron"), or the HERMES_CRON_SESSION session var cron/scheduler.py
    _CronRunScope sets for the run (read as tools/fleet_send.py reads it).
    Imports lazily: this module stays importable on its own."""
    if str(getattr(parent_agent, "platform", "") or "").strip().lower() == "cron":
        return True
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_CRON_SESSION") == "1"
    except Exception:  # noqa: BLE001 — no session module: the process env decides
        return os.environ.get("HERMES_CRON_SESSION") == "1"


def _recover_tasks(tasks: Any) -> Tuple[Any, Optional[str]]:
    """Upstream accepts ``tasks`` as a JSON-array string too; read it the same way."""
    from tools.delegate_tool_tasks import _recover_tasks_from_json_string
    recovered, err = _recover_tasks_from_json_string(tasks)
    if err:
        return tasks, err
    return (recovered if recovered is not None else tasks), None


def fold_top_level_agent(args: Any) -> Any:
    """``args["tasks"]``, except for the legacy single-goal shape with a
    top-level ``agent``: no tasks, a top-level goal. That becomes one task
    carrying the agent, so the hand-off routes it. A call with tasks keeps
    them as they are (per-task fields decide; a top-level agent is not read).

    Called by run_agent._dispatch_delegate_task and the registry handler
    (patch 0032), which otherwise drop the unknown top-level key."""
    if not isinstance(args, dict):
        return None
    tasks = args.get("tasks")
    if agent_field(args) is None:
        return tasks
    try:
        recovered, err = _recover_tasks(tasks)
    except Exception:  # noqa: BLE001 — upstream reports a bad tasks value itself
        return tasks
    if err or (recovered is not None and recovered != []):
        return tasks
    goal = args.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return tasks
    task: Dict[str, Any] = {"goal": goal, "agent": args.get("agent")}
    context = args.get("context")
    if isinstance(context, str) and context.strip():
        task["context"] = context
    return [task]


def drop_agent_field_without_fleet(td: Dict[str, Any], available) -> Dict[str, Any]:
    """delegate_task's schema without the per-task ``agent`` field when this
    session has no fleet tools (a cron job's toolset) or is a scheduled run:
    there it can only be refused. Called from model_tools._rewrite_delegate_task
    (patch 0032); the scheduler builds a run's tools inside its run scope, and
    the tool-definition cache already keys a cron run apart from a chat
    (model_tools._is_dispatcher_owned_worker). The hand-off itself is refused
    in a scheduled run whatever the schema showed (_plan).
    Never mutates ``td``: the static schema is shared."""
    if HANDOFF_REQUIRES_TOOL in (available or ()) and not in_scheduled_run():
        return td
    try:
        fn = td["function"]
        params = fn["parameters"]
        tasks = params["properties"]["tasks"]
        props = tasks["items"]["properties"]
    except (KeyError, TypeError):
        return td
    if "agent" not in props:
        return td
    items = {**tasks["items"], "properties": {k: v for k, v in props.items() if k != "agent"}}
    properties = {**params["properties"], "tasks": {**tasks, "items": items}}
    return {**td, "function": {**fn, "parameters": {**params, "properties": properties}}}


def teammate_question(task: Dict[str, Any], shared_context: Any = None,
                      schema: Optional[Dict[str, Any]] = None,
                      images: Optional[List[str]] = None) -> str:
    """The self-contained text a teammate receives for one task: its goal, then
    its context (or the call's shared context), images and the output contract
    (the same block a local subagent gets)."""
    parts = [str(task.get("goal") or "").strip()]
    context = task.get("context") or shared_context
    if isinstance(context, str) and context.strip():
        parts.append("Context:\n" + context.strip())
    if images:
        parts.append("Images (local paths or URLs):\n" + "\n".join(f"- {r}" for r in images))
    if isinstance(schema, dict) and schema:
        from tools.delegation_output_schema import append_output_contract
        parts.append(append_output_contract("", schema))
    return "\n\n".join(p for p in parts if p)


def _correction_question(goal: str, answer: str, errors: List[str], schema: Dict[str, Any]) -> str:
    """The one correction re-ask. The teammate's bridge starts a fresh session
    for every request, so this carries the earlier answer and the schema."""
    from tools.delegation_output_schema import append_output_contract, build_retry_message
    return "\n\n".join([
        "You were asked to do this task:\n" + goal.strip(),
        "Your answer was:\n" + answer.strip(),
        build_retry_message(errors),
        "Do not redo the work: reformat the answer above.",
        append_output_contract("", schema),
    ])


def _tool_error(message: str) -> str:
    try:
        from tools.registry import tool_error
        return tool_error(message)
    except Exception:
        return json.dumps({"error": message}, ensure_ascii=False)


def _top_role(role: Any) -> str:
    try:
        from tools.delegate_tool import _normalize_role
        return _normalize_role(role)
    except Exception:
        return "leaf"


def _plan(kwargs: Dict[str, Any], parent_agent: Any) -> Tuple[str, Any]:
    """What delegate_task does with this call, deciding nothing by sending.

    ``("spawn", kwargs)``: no task leaves; upstream's body gets these kwargs.
    ``("error", message)``: the call is refused and nothing is sent.
    ``("handoff", (handoffs, local))``: ``handoffs`` are dicts with index,
    agent, goal, question and schema; ``local`` is ``[(index, task)]``."""
    action = (kwargs.get("action") or "").strip().lower()
    if action and action != "spawn":
        return "spawn", kwargs
    tasks, err = _recover_tasks(kwargs.get("tasks"))
    if err or not isinstance(tasks, list) or not tasks or parent_agent is None:
        return "spawn", kwargs  # upstream's own handling
    if not any(agent_field(t) for t in tasks):
        if any(isinstance(t, dict) and "agent" in t for t in tasks):
            # A blank or null `agent` is no target: the tasks spawn locally, minus the key.
            kwargs = dict(kwargs, tasks=[_without_agent(t) for t in tasks])
        return "spawn", kwargs

    sender = sender_id()
    targets: Dict[int, str] = {}
    for index, task in enumerate(tasks):
        given = agent_field(task)
        if given is None:
            continue
        agent = normalize_agent_id(given)
        if agent is None:
            return "error", (
                f"Task {index}: '{given}' is not an agent id. Set 'agent' to the "
                f"teammate's id: {', '.join(FLEET_AGENT_IDS)}. The fleet roster pairs "
                "each teammate's name with its id; a name is not an id. Leave "
                "'agent' out to spawn your own subagent.")
        if agent != sender:  # your own id: do it yourself, as a local subagent
            targets[index] = agent
    stripped = [_without_agent(t) for t in tasks]
    if not targets:
        return "spawn", dict(kwargs, tasks=stripped)

    # Something would leave this machine: the whole call is checked first.
    from tools.delegate_tool_config import _get_max_concurrent_children, _get_max_spawn_depth
    from tools.delegate_tool_registry import is_spawn_paused
    from tools.delegate_tool_tasks import _coerce_task_images, _coerce_task_schemas, _normalize_task_list
    if is_spawn_paused():
        return "error", ("Delegation spawning is paused. Clear the pause via the TUI "
                         "(`p` in /agents) or the `delegation.pause` RPC before retrying.")
    try:
        task_list, err = _normalize_task_list(
            kwargs.get("goal"), kwargs.get("context"), stripped, kwargs.get("output_schema"),
            _top_role(kwargs.get("role")), _get_max_concurrent_children())
        schemas: List[Any] = []
        images: List[Any] = []
        if not err:
            schemas, err = _coerce_task_schemas(task_list, kwargs.get("output_schema"))
        if not err:
            images, err = _coerce_task_images(task_list, kwargs.get("images"))
    except (AttributeError, TypeError, ValueError) as exc:
        err = f"The tasks could not be read ({type(exc).__name__}: {exc}). Give every task a 'goal' string."
    if err:
        return "error", err
    local = [(i, stripped[i]) for i in range(len(stripped)) if i not in targets]
    if local:
        depth = getattr(parent_agent, "_delegate_depth", 0) or 0
        max_spawn = _get_max_spawn_depth()
        if depth >= max_spawn:
            return "error", (
                f"Delegation depth limit reached (depth={depth}, max_spawn_depth={max_spawn}) for the "
                "tasks without 'agent', so nothing was sent to a teammate either. Hand those tasks "
                "to a teammate (set 'agent') or do them with your own tools.")
    if not fleet_tools_enabled(parent_agent):
        return "error", (
            "Handing a task to a teammate is not available in this session (its "
            "toolset has no fleet tools). Leave 'agent' out and do the task with a "
            "subagent or your own tools, or tell the user it needs the teammate.")
    if in_scheduled_run(parent_agent):
        return "error", (
            "Handing a task to a teammate through delegate_task is not available in "
            "a scheduled run. Leave 'agent' out and do the task with a subagent or "
            "your own tools, or ask the teammate with ask_agent.")
    handoffs = [{
        "index": i,
        "agent": agent,
        "goal": str(task_list[i].get("goal") or ""),
        "question": teammate_question(task_list[i], kwargs.get("context"), schemas[i], images[i]),
        "schema": schemas[i],
    } for i, agent in sorted(targets.items())]
    return "handoff", (handoffs, local)


def _without_agent(task: Any) -> Any:
    """The task minus its ``agent`` key, for upstream's local spawn."""
    if not isinstance(task, dict) or "agent" not in task:
        return task
    return {k: v for k, v in task.items() if k != "agent"}


def planned_handoffs(args: Any, parent_agent: Any) -> List[Dict[str, Any]]:
    """``[{"task_index", "agent"}]``: every task this delegate_task call (the
    model's raw arguments) will send to a teammate, in task order. Empty when it
    sends none or the call will be refused. Sends nothing; reads the same
    environment and config hand_off does.

    The bridge worker calls this before the tool runs, so its hand-off bubbles
    show only what really leaves (a refused call, a loop or an over-budget hop
    gets none). Mirrors run_agent._dispatch_delegate_task's arguments, which do
    not include a top-level output_schema."""
    if not isinstance(args, dict):
        return []
    kwargs = {
        "goal": args.get("goal"), "context": args.get("context"),
        "tasks": fold_top_level_agent(args), "role": args.get("role"),
        "images": args.get("images"), "action": args.get("action"),
    }
    try:
        kind, payload = _plan(kwargs, parent_agent)
        if kind != "handoff":
            return []
        from tools.fleet_budget import budget_from_env, refusal_reason
        depth, _origin, visited = budget_from_env()
        sender = sender_id()
        return [{"task_index": h["index"], "agent": h["agent"]} for h in payload[0]
                if not refusal_reason(h["agent"], sender, depth, visited)]
    except Exception:  # noqa: BLE001 — a planning failure claims no hand-off
        return []


def _ask(agent: str, question: str, sender: str) -> Dict[str, Any]:
    try:
        from tools.ask_agent_tool import ask_agent
        raw = json.loads(ask_agent(agent, question, sender=sender))
    except Exception as exc:  # noqa: BLE001 — a hand-off failure is a result, not a crash
        raw = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(raw, dict):
        raw = {"success": False, "error": "unreadable reply from the teammate's bridge"}
    return raw


def _unfinished(entry: Dict[str, Any], raw: Dict[str, Any], agent: str) -> None:
    """Fill a not-completed entry from ask_agent's reply. Its structured
    ``status`` decides; its error wording is read only when it has none."""
    status = str(raw.get("status") or "").strip().lower()
    error = str(raw.get("error") or f"{agent} returned nothing")
    if raw.get("refused") or status in ("refused", "not_delivered"):
        status, default = "refused", _NO_RETRY_GUIDANCE
    elif status in _UNFINISHED_STATUSES:
        default = _TIMEOUT_GUIDANCE if status == "timeout" else _BUSY_GUIDANCE
    else:
        timed_out = "timed out" in error.lower() or "timeout" in error.lower()
        status = "timeout" if timed_out else "failed"
        default = _TIMEOUT_GUIDANCE if timed_out else _FAILED_GUIDANCE
    guidance = raw.get("guidance")
    entry.update(status=status, summary="", error=error,
                 guidance=(guidance if isinstance(guidance, str) and guidance.strip()
                           else default.format(agent=agent)))
    if status in _UNFINISHED_STATUSES:
        entry["unfinished"] = True
    for key in _CARRIED_FACTS:
        if key in raw:
            entry[key] = raw[key]


def hand_one(index: int, agent: str, question: str, sender: str,
             schema: Optional[Dict[str, Any]] = None, goal: str = "") -> Dict[str, Any]:
    """Hand one task to ``agent`` and return its result entry (never raises)."""
    from tools.fleet_budget import budget_from_env, refusal_reason

    entry: Dict[str, Any] = {"task_index": index, "agent": agent, "handled_by": "teammate"}
    depth, _origin, visited = budget_from_env()
    refusal = refusal_reason(agent, sender, depth, visited)
    if refusal:
        entry.update(status="refused", summary="", error=f"Hand-off refused: {refusal}.",
                     guidance=_NO_RETRY_GUIDANCE)
        return entry

    started = time.monotonic()
    raw = _ask(agent, question, sender)
    if not raw.get("success"):
        entry["duration_seconds"] = round(time.monotonic() - started, 2)
        _unfinished(entry, raw, agent)
        return entry

    answer = str(raw.get("answer") or "")
    if isinstance(schema, dict):
        try:
            from tools.delegation_output_schema import validate_output
            valid, errors = validate_output(answer, schema)
            retries = 0
            if not valid and answer.strip():
                retries = 1
                again = _ask(agent, _correction_question(goal, answer, errors, schema), sender)
                fixed = str(again.get("answer") or "") if again.get("success") else ""
                if fixed.strip():
                    answer = fixed
                    valid, errors = validate_output(answer, schema)
        except Exception as exc:  # noqa: BLE001 — an unreadable check is a failed one
            valid, errors, retries = False, [f"The answer could not be checked: {exc}"], 0
        entry["schema_valid"] = bool(valid)
        if retries:
            entry["schema_retries"] = retries
        if not valid:
            entry["duration_seconds"] = round(time.monotonic() - started, 2)
            entry.update(status="failed", summary=answer, schema_errors=errors,
                         error="Final answer does not satisfy the declared output_schema"
                               + (" (after 1 retry)." if retries else "."),
                         guidance=_SCHEMA_GUIDANCE.format(agent=agent))
            return entry
    entry["duration_seconds"] = round(time.monotonic() - started, 2)
    entry.update(status="completed", summary=answer)
    return entry


def _run_handoffs(handoffs: List[Dict[str, Any]], sender: str,
                  results: Dict[int, Dict[str, Any]]) -> List[threading.Thread]:
    """Start one thread per teammate; each runs that teammate's tasks in order."""
    by_agent: Dict[str, List[Dict[str, Any]]] = {}
    for h in handoffs:
        by_agent.setdefault(h["agent"], []).append(h)
    lock = threading.Lock()

    def run(agent: str, queue: List[Dict[str, Any]]) -> None:
        for h in queue:
            entry = hand_one(h["index"], agent, h["question"], sender,
                             schema=h.get("schema"), goal=h.get("goal") or "")
            with lock:
                results[h["index"]] = entry

    threads = []
    for agent in sorted(by_agent):
        t = threading.Thread(target=run, args=(agent, by_agent[agent]),
                             name=f"teammate-handoff-{agent}", daemon=True)
        t.start()
        threads.append(t)
    return threads


def _note_waiting(parent_agent: Any, started: float) -> None:
    """Tell the chat the turn is waiting, not stalled (best effort)."""
    minutes = max(1, int((time.monotonic() - started) // 60))
    touch = getattr(parent_agent, "_touch_activity", None)
    if callable(touch):
        try:
            touch("delegate_task: waiting for a teammate's answer")
        except Exception:  # noqa: BLE001
            pass
    note = getattr(parent_agent, "status_callback", None)
    if callable(note):
        try:
            note("lifecycle", f"Still waiting for a teammate's answer ({minutes} min so far).")
        except Exception:  # noqa: BLE001
            pass


def _join(threads: List[threading.Thread], parent_agent: Any, started: float) -> None:
    last_note = time.monotonic()
    step = max(0.01, min(1.0, _HEARTBEAT_S))
    for t in threads:
        while t.is_alive():
            t.join(timeout=step)
            if t.is_alive() and time.monotonic() - last_note >= _HEARTBEAT_S:
                _note_waiting(parent_agent, started)
                last_note = time.monotonic()


def hand_off(spawn: Callable[..., str], kwargs: Dict[str, Any], parent_agent: Any) -> str:
    """delegate_task with explicit teammate targets.

    ``spawn`` is upstream's delegate_task body (patch 0032 renames it
    ``_spawn_delegate_task``); ``kwargs`` are delegate_task's arguments except
    ``parent_agent``. A call where no task carries ``agent`` is passed straight
    through, unchanged."""
    kind, payload = _plan(kwargs, parent_agent)
    if kind == "spawn":
        return spawn(parent_agent=parent_agent, **payload)
    if kind == "error":
        return _tool_error(payload)
    handoffs, local = payload

    sender = sender_id()
    started = time.monotonic()
    results: Dict[int, Dict[str, Any]] = {}
    threads = _run_handoffs(handoffs, sender, results)

    local_out: Optional[str] = None
    try:
        if local:
            # A mixed call has two or more tasks, and upstream applies a
            # top-level output_schema / images to a one-task call only: the
            # split must not make them apply to what is left.
            local_kwargs = dict(kwargs, tasks=[t for _, t in local], output_schema=None, images=None)
            try:
                local_out = spawn(parent_agent=parent_agent, **local_kwargs)
            except Exception as exc:  # noqa: BLE001 — the teammates' results must still come back
                local_out = _tool_error(f"The local subagents could not start: {type(exc).__name__}: {exc}")
    finally:
        _join(threads, parent_agent, started)

    teammate_results = [results[h["index"]] for h in handoffs]
    handed_to = sorted({h["agent"] for h in handoffs})
    if local_out is None:
        return json.dumps({
            "results": teammate_results,
            "handed_to": handed_to,
            "total_duration_seconds": round(time.monotonic() - started, 2),
        }, ensure_ascii=False)

    try:
        merged = json.loads(local_out)
    except (TypeError, ValueError):
        merged = None
    if not isinstance(merged, dict):
        merged = {"local": local_out}
    merged["teammate_results"] = teammate_results
    merged["handed_to"] = handed_to
    # The local batch was renumbered from 0; this maps it back to the call's tasks.
    merged["local_task_indices"] = [i for i, _ in local]
    return json.dumps(merged, ensure_ascii=False)
