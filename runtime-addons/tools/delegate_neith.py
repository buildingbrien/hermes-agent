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
from tools.bridge_auth import bridge_bearer

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
# depth reaches MAX_FLEET_DEPTH or Neith already appears in the visited chain,
# delegation is refused with a structured result instead of cascading. The
# budget itself lives in tools/fleet_budget.py, shared with fleet_send and
# pinned to the bridge worker's default of 3 (R2-2-23: this tool's own default
# of 1 refused research delegation to every agent handling a bus-delivered
# task). The three names below are kept for callers and tests.
from tools.fleet_budget import (  # noqa: E402
    budget_from_env as _fleet_budget_from_env, max_fleet_depth as _max_fleet_depth,
    next_hop_fields, refusal_reason)
from tools.fleet_send import _unattended_fields  # noqa: E402  (F21, shared with ask_agent)


def _budget_refusal(sender: str, depth: int, visited) -> "str | None":
    """Refusal reason for a hop from ``sender`` to Neith, or None when within budget."""
    return refusal_reason("neith", sender, depth, visited)


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
    "The research is UNFINISHED, not done: tell the user that plainly. If any "
    "files are listed, Neith already wrote them: read them before doing any "
    "research yourself, and never write over them. Offer to try again with a "
    "narrower, more specific task. Do not retry automatically."
)

# Local-convo review F19 (Sep 26): Neith's 28 KB report was already on disk
# when this tool said only "did not finish within her 300-second limit"; Merlin
# then wrote a 9 KB report over the same file. The bridge's 504 now carries
# what is known: whether she is still working (her result is then posted into
# the caller's chat with the owner when it lands) and which files she wrote.
_STILL_RUNNING_GUIDANCE = (
    "The research is UNFINISHED, not done: tell the user that plainly. Neith is "
    "still working on it. {where} Do not redo the research, do not re-send the "
    "task, and never write over the files listed here; read them if you need "
    "what is already there."
)


def _origin() -> dict:
    try:
        from tools.fleet_send import turn_origin
        return turn_origin()
    except Exception:  # noqa: BLE001
        return {"session_id": "", "source": ""}


def _where() -> str:
    """Where her late result lands, for this turn's surface (fleet_send)."""
    try:
        from tools.fleet_send import late_result_where
        return late_result_where()
    except Exception:  # noqa: BLE001
        return "Her result comes back to you when it lands."

_BUSY_GUIDANCE = (
    "Do not re-send it. Neith is already working on this request and its result "
    "comes back to you when it finishes; tell the user it is in progress."
)


def _files_written(body) -> list:
    files = body.get("files_written") if isinstance(body, dict) else None
    out = []
    for f in files if isinstance(files, list) else []:
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            rec = {"path": f["path"]}
            if isinstance(f.get("bytes"), int):
                rec["bytes"] = f["bytes"]
            out.append(rec)
    return out[:20]


def _timeout_result(body) -> str:
    """The bridge's 504 for a delegation that missed its window, relayed with
    every fact it carries — never as a bare "timed out"."""
    body = body if isinstance(body, dict) else {}
    still = bool(body.get("still_running"))
    files = _files_written(body)
    where = _where()
    error = ("Neith accepted the task but did not finish within her 300-second "
             "limit. " + (f"She is still working on it. {where}"
                          if still else "Her run was stopped, unfinished."))
    out = {"source": "neith", "status": "timeout", "unfinished": True,
           "still_running": still, "error": error,
           "guidance": (_STILL_RUNNING_GUIDANCE.format(where=where) if still
                        else _TIMEOUT_GUIDANCE)}
    if files:
        out["files_written"] = files
    return json.dumps(out, ensure_ascii=False)


DELEGATE_TO_NEITH_SCHEMA = {
    "name": "delegate_to_neith",
    "description": (
        "Delegate a research, web-search, data-gathering, or deep-analysis task "
        "to Neith — the fleet's dedicated research agent — and get her findings "
        "back as the tool result, which you then relay to the user. Use this "
        "whenever the user needs live web research or information beyond your "
        "knowledge cutoff. Neith does NOT see this conversation, so make the task "
        "specific and self-contained. If Neith's bridge is unavailable the result "
        "says so (status 'failed' or 'timeout') — tell the user honestly and do "
        "the research with your own tools if you can; it is not retried for you. "
        "A 'timeout' is UNFINISHED work, never finished: if it says she is still "
        "working, her result comes back to you when it lands (the result says "
        "where), so do not redo it; and never write over any files it lists."
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
    # The chat this turn runs in: her late result is written there (F19/F31).
    origin = _origin()
    if origin.get("session_id"):
        payload["requester_session_id"] = origin["session_id"]
    if origin.get("source"):
        payload["requester_source"] = origin["source"]
    # F21: from a scheduled run, Neith answers as an unattended turn (the
    # Lucaryin bridge also refuses this tool in scheduled runs in favour of
    # ask_agent; this keeps the marker on whichever path runs).
    payload.update(_unattended_fields())
    headers = {"Content-Type": "application/json"}
    # Bridge auth (P4): attach the per-launch token when present so this keeps
    # working once the bridges require authentication. Harmless when unset.
    token = bridge_bearer()  # file-then-env (tools/bridge_auth.py, HA3)
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
    budget = next_hop_fields(sender)  # the same fields fleet_send attaches

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
            # Neith took the task but her worker hit its 300s window. Do NOT
            # run the fallback here — she is up, just slow; stacking another
            # multi-minute attempt on top invites pile-ups. Relay what the
            # bridge knows (still running? files already written?).
            return _timeout_result(body)
        if e.code == 409 and isinstance(body, dict) and body.get("status") in ("busy", "duplicate"):
            # F32: the same request is already running on Neith's bridge.
            return _structured_failure(
                body["status"],
                str(body.get("error") or "Neith is already working on this request."),
                _BUSY_GUIDANCE,
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

    # ── Fallback: in-process research subagent (single attempt) — ONLY when the
    # caller handed us the running agent. The registry dispatches handlers with
    # task_id/session_id/user_task and no agent (model_tools._execute_tool), so
    # from a normal tool call this never runs (R2-2-23); the result then says
    # plainly that no fallback was available instead of implying one ran.
    if parent_agent is None:
        return _structured_failure(
            "failed",
            f"Could not reach Neith ({neith_err}); no in-process research fallback "
            "is available from this call.",
            "Tell the user the research could not be delegated, and why; do it "
            "with your own tools if you can. Do not retry the delegation "
            "automatically.",
        )
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
