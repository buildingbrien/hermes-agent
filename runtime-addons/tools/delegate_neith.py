#!/usr/bin/env python3
"""delegate_to_neith — delegate a research/analysis task to the persistent Neith
agent (Hermes bridge, port 9007) and return her findings synchronously.

Primary path: POST the task to Neith's /api/chat/sync and return Neith's answer.
This is bus-independent — it does not rely on Supabase Realtime / pub/sub, so it
works even when the fleet bus is down.

Fallback: if Neith's bridge is unreachable or returns no usable result, spawn an
in-process research subagent via delegate_task(toolsets=['web']) so research is
never silently dropped. The result is tagged so the caller (and user) can see
which path produced it.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.request
import urllib.error

# Neith runs on the Hermes bridge (profile neith), port 9007 — migrated off the
# deprecated OpenClaw bridge (9003).
NEITH_PORT = 9007
NEITH_SYNC_URL = f"http://127.0.0.1:{NEITH_PORT}/api/chat/sync"
# Neith's /api/chat/sync worker caps at 300s; give the HTTP call slight headroom
# so we receive the bridge's own 504 rather than a client-side timeout.
_SYNC_TIMEOUT = 320

# ── WS2: Fleet delegation budget (bounds cross-bridge cascades) ──────────────
# The bridge worker exports FLEET_DELEGATION_* env vars when this process is
# itself running a delegated task. Each cross-bridge hop increments depth; once
# depth reaches MAX_FLEET_DEPTH (default 1: thoth→neith fine, neith→anyone
# refused) or the target already appears in the visited chain, delegation is
# refused with a structured result instead of cascading.
_FLEET_VISITED_MAX = 16


def _max_fleet_depth() -> int:
    """Cross-bridge delegation hop cap (env MAX_FLEET_DEPTH, default 1)."""
    try:
        return max(0, int(os.environ.get("MAX_FLEET_DEPTH", "1")))
    except ValueError:
        return 1


def _fleet_budget_from_env() -> tuple:
    """Read (depth, origin, visited) seeded by the bridge worker, if any."""
    try:
        depth = max(0, int(os.environ.get("FLEET_DELEGATION_DEPTH", "0")))
    except ValueError:
        depth = 0
    origin = os.environ.get("FLEET_DELEGATION_ORIGIN", "").strip().lower()
    visited = []
    for item in os.environ.get("FLEET_DELEGATION_VISITED", "").split(","):
        name = item.strip().lower()
        if name and name not in visited:
            visited.append(name)
        if len(visited) >= _FLEET_VISITED_MAX:
            break
    return depth, origin, visited


def _budget_refusal(sender: str, depth: int, visited) -> "str | None":
    """Return a refusal reason when this delegation must not leave the box,
    or None when it is within budget."""
    sender = (sender or "").strip().lower()
    if sender == "neith":
        return "you ARE Neith — delegating to Neith would send the task to yourself"
    if depth >= _max_fleet_depth():
        return (
            f"this conversation was itself delegated across {depth} fleet "
            f"hop(s), which exhausts the limit of {_max_fleet_depth()}"
        )
    if "neith" in visited:
        return (
            f"Neith already handled this request "
            f"(chain: {' -> '.join(visited)}), so delegating back would loop"
        )
    return None


def _structured_failure(status: str, error: str, guidance: str) -> str:
    """A structured non-success tool result the parent model can relay
    honestly — never a raw traceback, never an invitation to retry-loop."""
    return json.dumps(
        {"source": "neith", "status": status, "error": error, "guidance": guidance},
        ensure_ascii=False,
    )


_NO_RETRY_GUIDANCE = (
    "Handle the task with your own tools if you can; otherwise tell the user "
    "honestly that the research could not be delegated. Do not retry the "
    "delegation."
)

_TIMEOUT_GUIDANCE = (
    "Tell the user your research agent timed out, and offer to try again with "
    "a narrower, more specific task. Do not retry automatically."
)


DELEGATE_TO_NEITH_SCHEMA = {
    "name": "delegate_to_neith",
    "description": (
        "Delegate a research, web-search, data-gathering, or deep-analysis task "
        "to Neith — the fleet's dedicated research agent — and get her findings "
        "back as the tool result, which you then relay to the user. Use this "
        "whenever the user needs live web research or information beyond your "
        "knowledge cutoff. Neith does NOT see this conversation, so make the task "
        "specific and self-contained. If Neith's bridge is unavailable, the task "
        "is automatically handled by a research subagent instead, so it never "
        "silently fails."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "The research/analysis task for Neith. Be specific and "
                    "self-contained — include any context she needs, since she "
                    "cannot see your conversation history."
                ),
            },
        },
        "required": ["task"],
    },
}


def _call_neith_sync(task: str, budget: "dict | None" = None) -> dict:
    """POST the task to Neith's /api/chat/sync and return the parsed response.

    Raises urllib.error.URLError (transport) or other exceptions on failure.
    """
    payload = {
        "messages": [{"role": "user", "content": task}],
        "agent_id": "neith",
    }
    if budget:
        # WS2: propagate the delegation budget so Neith's bridge seeds her
        # worker's depth and refuses onward hops.
        payload.update(budget)
    headers = {"Content-Type": "application/json"}
    # Bridge auth (P4): attach the per-launch token when present so this keeps
    # working once the bridges require authentication. Harmless when unset.
    token = os.environ.get("BRIDGE_AUTH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        NEITH_SYNC_URL,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_SYNC_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fallback_subagent(task: str, parent_agent) -> str | None:
    """Run an in-process research subagent when Neith's bridge is unreachable.

    Returns the subagent's summary, or None if the fallback itself fails.
    """
    if parent_agent is None:
        return None
    try:
        from tools.delegate_tool import delegate_task
        result_json = delegate_task(
            goal=task,
            toolsets=["web"],
            parent_agent=parent_agent,
        )
        data = json.loads(result_json)
        results = data.get("results", [])
        if results and isinstance(results, list):
            summary = results[0].get("summary")
            if summary:
                return summary
    except Exception:
        return None
    return None


def delegate_to_neith_tool(args, **kw):
    from tools.registry import tool_error

    task = (args.get("task", "") or "").strip()
    if not task:
        return tool_error("A 'task' for Neith is required.")

    parent_agent = kw.get("parent_agent")

    # ── WS2: fleet delegation budget — refuse instead of cascading ──
    sender = (os.environ.get("BRIDGE_PROFILE", "") or "thoth").strip().lower()
    depth, origin, visited = _fleet_budget_from_env()
    refusal = _budget_refusal(sender, depth, visited)
    if refusal:
        return _structured_failure(
            "refused",
            f"Fleet delegation refused: {refusal}.",
            _NO_RETRY_GUIDANCE,
        )
    budget = {
        "delegation_depth": depth + 1,
        "delegation_origin": origin or sender,
        "delegation_visited": (visited + [sender]) if sender not in visited else list(visited),
    }

    # ── Primary: synchronous call to the persistent Neith on :9007 ──
    try:
        resp = _call_neith_sync(task, budget)
        if resp.get("success") and resp.get("response"):
            return json.dumps(
                {"source": "neith", "result": resp["response"]},
                ensure_ascii=False,
            )
        if resp.get("refused"):
            # Neith's bridge rejected the hop (depth/loop guard) — relay the
            # structured refusal; the fallback would defeat the budget.
            return _structured_failure(
                "refused",
                str(resp.get("error") or "Neith's bridge refused the delegation."),
                _NO_RETRY_GUIDANCE,
            )
        # Bridge reachable but produced no usable answer — try the fallback.
        neith_err = resp.get("error", "Neith returned no result")
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            body = {}
        if isinstance(body, dict) and body.get("refused"):
            return _structured_failure(
                "refused",
                str(body.get("error") or "Neith's bridge refused the delegation."),
                _NO_RETRY_GUIDANCE,
            )
        if e.code == 504:
            # Neith took the task but her worker hit its 300s cap. Do NOT run
            # the fallback here — she is up, just slow; stacking another
            # multi-minute attempt on top invites pile-ups.
            return _structured_failure(
                "timeout",
                "Neith accepted the task but did not finish within her "
                "300-second limit.",
                _TIMEOUT_GUIDANCE,
            )
        neith_err = str(
            (isinstance(body, dict) and body.get("error"))
            or f"Neith's bridge returned HTTP {e.code}"
        )
    except socket.timeout:
        return _structured_failure(
            "timeout",
            "Neith did not respond within the delegation window.",
            _TIMEOUT_GUIDANCE,
        )
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            return _structured_failure(
                "timeout",
                "Neith did not respond within the delegation window.",
                _TIMEOUT_GUIDANCE,
            )
        neith_err = f"Neith bridge unreachable: {reason}"
    except Exception as e:
        neith_err = f"Neith call failed: {type(e).__name__}: {e}"

    # ── Fallback: in-process research subagent (single attempt — this is the
    # only automatic retry in the delegation flow, capped at 1) ──
    summary = _fallback_subagent(task, parent_agent)
    if summary:
        return json.dumps(
            {
                "source": "research_subagent_fallback",
                "note": (
                    f"Neith was unavailable ({neith_err}); a research subagent "
                    "handled this instead."
                ),
                "result": summary,
            },
            ensure_ascii=False,
        )

    return _structured_failure(
        "failed",
        f"Could not reach Neith ({neith_err}) and the research-subagent "
        "fallback also failed.",
        "Tell the user the research could not be completed, and why. Do not "
        "retry the delegation automatically.",
    )


# --- Registry ---
from tools.registry import registry

registry.register(
    name="delegate_to_neith",
    toolset="fleet",
    schema=DELEGATE_TO_NEITH_SCHEMA,
    handler=lambda args, **kw: delegate_to_neith_tool(args, **kw),
    emoji="🔬",
)
