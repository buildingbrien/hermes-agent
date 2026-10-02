#!/usr/bin/env python3
"""ask_agent — ask any teammate a question and get their ANSWER back.

The fleet had two halves of this and neither was the whole thing:

  fleet_send        — works for any agent, but is fire-and-forget. It returns
                      a delivery receipt, not a reply.
  delegate_to_neith — returns the reply synchronously, but is hardcoded to
                      Neith (port 9007).

So an agent that needed to ASK a specific teammate something and use the
answer had no supported path. On 2026-08-16 Ptah tried to run a 25-image
focus group with Clara and Set as reviewers and spent most of its turn
hand-rolling the missing primitive: curling bridge endpoints to find a read
path, polluting another agent's bus with shell text, then scraping replies
out of session files on disk. It ran out of time before finishing the work
it was actually asked to do.

The plumbing was already there — /api/chat/sync, the delegation budget, the
hop-depth refusals — welded to one recipient. This exposes it for the fleet.

Multi-round conversations: each call is a fresh session on the far side, so
the teammate does NOT remember previous exchanges. Carry any context the
answer depends on in the question itself.
"""

import json
import os
import urllib.error
import urllib.request
from tools.bridge_auth import bridge_bearer
from tools.fleet_send import _unattended_fields  # F21: cron asks for unattended turns

# Same map the bridges and voice server use. Keep in sync.
AGENT_PORTS = {"thoth": 9001, "ptah": 9005, "set": 9006, "neith": 9007}

# Long enough for a real tool-bearing turn on the far side (research, file
# reads, vision), short enough that a wedged teammate cannot hang the caller
# for the whole turn budget. The far bridge gives a delegated turn 300 s and
# then answers 504 itself with what it knows (still running? files written?);
# this client waits slightly longer so that answer arrives instead of a bare
# client-side "timed out" (local-convo review F19: both were 300 s, so the
# client always lost the race and the facts with it).
_SYNC_TIMEOUT = 320.0


def _http_error_body(e) -> dict:
    try:
        body = json.loads(e.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _files_written(body: dict) -> list:
    out = []
    for f in body.get("files_written") or []:
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            rec = {"path": f["path"]}
            if isinstance(f.get("bytes"), int):
                rec["bytes"] = f["bytes"]
            out.append(rec)
    return out[:20]


def _origin() -> dict:
    try:
        from tools.fleet_send import turn_origin
        return turn_origin()
    except Exception:  # noqa: BLE001
        return {"session_id": "", "source": ""}


def _where() -> str:
    try:
        from tools.fleet_send import late_result_where
        return late_result_where()
    except Exception:  # noqa: BLE001
        return "Its answer comes back to you when it lands."


def _timeout_answer(target: str, body: dict) -> str:
    """A teammate that missed the window: unfinished, never 'answered'."""
    still = bool(body.get("still_running"))
    out = {
        "success": False, "agent": target, "status": "timeout", "unfinished": True,
        "still_running": still,
        "error": (f"{target} did not answer within the 300-second window. "
                  + (f"{target} is still working. {_where()}"
                     if still else f"{target}'s run was stopped, unfinished.")),
        "guidance": (
            "Tell the user plainly that it is unfinished. Do not guess what "
            f"{target} would have said, do not re-ask the same question now, and "
            "never write over any files listed here; read them if you need them."),
    }
    files = _files_written(body)
    if files:
        out["files_written"] = files
    return json.dumps(out, ensure_ascii=False)


def _budget_fields(sender: str) -> dict:
    """Propagate the delegation budget so the far side can refuse onward hops
    and the A→B→A ping-pong guard keeps working."""
    try:
        from tools.fleet_send import _delegation_budget_fields
        return _delegation_budget_fields(sender) or {}
    except Exception:
        return {}


def ask_agent(agent: str, question: str, sender: str = "") -> str:
    target = (agent or "").strip().lower()
    q = (question or "").strip()
    if target not in AGENT_PORTS:
        return json.dumps({
            "success": False,
            "error": (f"Unknown agent '{agent}'. 'agent' is an agent id: "
                      f"{', '.join(sorted(AGENT_PORTS))}. The fleet roster pairs each "
                      f"teammate's name with its id; a name is not an id."),
        })
    if not q:
        return json.dumps({"success": False, "error": "question is required"})
    if target == (sender or "").strip().lower():
        return json.dumps({
            "success": False,
            "error": "That is you — answer it yourself rather than asking.",
        })

    payload = {"messages": [{"role": "user", "content": q}], "agent_id": target}
    payload.update(_budget_fields(sender))
    # The chat this turn runs in: a late answer is written there (F19/F31).
    origin = _origin()
    if origin.get("session_id"):
        payload["requester_session_id"] = origin["session_id"]
    if origin.get("source"):
        payload["requester_source"] = origin["source"]
    # A question from a scheduled run is answered as an UNATTENDED turn on the
    # far side (lucaryin-ai /api/chat/sync -> LUCARYIN_TURN_UNATTENDED): a
    # gated action there is blocked and carded, never waited on, so a cron job
    # cannot borrow the teammate's attended trust (F21). Shared with fleet_send
    # and delegate_to_neith (tools/fleet_send.py _unattended_fields).
    payload.update(_unattended_fields())
    headers = {"Content-Type": "application/json"}
    token = bridge_bearer()  # file-then-env (tools/bridge_auth.py, HA3)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"http://127.0.0.1:{AGENT_PORTS[target]}/api/chat/sync"
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=_SYNC_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        # HTTPError IS a URLError: before this branch a 504 (the teammate
        # missed its window) was reported as "bridge is unreachable".
        body = _http_error_body(e)
        if e.code == 504:
            return _timeout_answer(target, body)
        if e.code == 409 and body.get("status") in ("busy", "duplicate"):
            return json.dumps({
                "success": False, "agent": target, "status": body["status"],
                "error": body.get("error") or f"{target} is already working on this.",
                "guidance": body.get("guidance") or (
                    "Do not re-ask now; the earlier answer comes back to you when "
                    "it finishes. Tell the user it is in progress."),
            }, ensure_ascii=False)
        if body.get("refused"):
            return json.dumps({"success": False, "agent": target, "refused": True,
                               "error": body.get("error", "refused")})
        return json.dumps({
            "success": False, "agent": target,
            "error": str(body.get("error") or f"{target}'s bridge returned HTTP {e.code}")
                     + ". Say so plainly rather than inventing their answer.",
        })
    except TimeoutError:
        return _timeout_answer(target, {})
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), TimeoutError):
            return _timeout_answer(target, {})
        return json.dumps({
            "success": False, "agent": target,
            "error": f"{target}'s bridge is unreachable ({e}). Say so plainly "
                     f"rather than inventing their answer.",
        })
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "agent": target, "error": str(e)})

    # The reply must come from the agent we asked (F20: Team Huddle reached a
    # legacy OpenClaw 'neith' whose memory ended in July and reported the real
    # one offline). A bridge that names its profile and names another one is
    # not the teammate; an older bridge that names none is taken at its port.
    responder = str(data.get("profile") or "").strip().lower()
    if responder and responder != target:
        return json.dumps({
            "success": False, "agent": target,
            "error": f"The bridge for {target} answered as '{responder}'. That is not "
                     f"{target}: report {target} as not reachable and do not use this "
                     f"reply.",
        })

    # A refusal is a real answer — surface it rather than burying it as failure.
    if data.get("refused"):
        return json.dumps({"success": False, "agent": target, "refused": True,
                           "error": data.get("error", "refused")})

    reply = (data.get("response") or data.get("content")
             or data.get("message") or "").strip()
    if not reply:
        return json.dumps({
            "success": False, "agent": target,
            "error": f"{target} returned nothing. Do NOT guess what they would "
                     f"have said — report that they did not answer.",
        })
    return json.dumps({"success": True, "agent": target, "answer": reply})


def check_ask_agent_requirements() -> dict:
    return {"available": True}


ASK_AGENT_SCHEMA = {
    "name": "ask_agent",
    "description": (
        "Ask a specific teammate a question and get their ANSWER back as the "
        "tool result. Use this whenever you need another agent's actual "
        "response — a review, an opinion, an analysis, a check — rather than "
        "just notifying them. This is the tool for running anything "
        "conversational across the fleet: panels, reviews, second opinions, "
        "multi-agent exercises. (fleet_send only DELIVERS a message and "
        "returns a receipt; it cannot bring a reply back.) "
        "The teammate does not see your conversation and does not remember "
        "earlier calls, so make each question self-contained — restate any "
        "role, persona or context the answer depends on. "
        "The `agent` field alone decides who is asked: an agent id (thoth, "
        "neith, ptah or set), never a display name; the fleet roster pairs each "
        "teammate's name with its id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent": {"type": "string",
                      "enum": ["neith", "ptah", "set", "thoth"],
                      "description": "The teammate to ask, by agent id: thoth, neith, ptah or set."},
            "question": {"type": "string",
                         "description": "The self-contained question or task."},
        },
        "required": ["agent", "question"],
    },
}


def _asking_agent(kw: dict) -> str:
    """Who is asking. The runtime's dispatcher passes a handler only task_id,
    session_id and user_task, so the asker is this process's bridge profile
    (BRIDGE_PROFILE, which the bridge sets in every worker's env — the same
    source fleet_send and delegate_to_neith read). Without it the far bridge
    could not name the asker: the question read as if the owner had typed it,
    single-flight never applied, a timed-out answer was never delivered late,
    and in a nested hand-off the previous hop was named instead (Lucaryin
    local-convo review, round 2)."""
    return str(kw.get("agent_id") or kw.get("profile")
               or os.environ.get("BRIDGE_PROFILE") or "").strip().lower()


# --- Registry ---
from tools.registry import registry, tool_error  # noqa: E402

registry.register(
    name="ask_agent",
    toolset="fleet",
    schema=ASK_AGENT_SCHEMA,
    handler=lambda args, **kw: ask_agent(
        agent=args.get("agent") or "",
        question=args.get("question") or "",
        sender=_asking_agent(kw)),
    check_fn=check_ask_agent_requirements,
    emoji="💬",
)
