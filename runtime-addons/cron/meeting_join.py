"""meeting_join.py — deterministically dial a scheduled meeting.

A `meeting_join` cron job is NOT run through an LLM. The user already said
what to do — "join Q3 Review at 3, here's the dial-in" — and authorized the
dial when they scheduled it (an `outbound_call` grant sits on the job). So at
fire time this executor just places the call: no model, which means it works
even when the model is degraded, and there's no prompt for the agent to
"forget" the dial playbook (the gap that made the naive cron approach fail).

Security: the executor dials ONLY if the job carries a live, unconsumed
`outbound_call` grant for the exact number. The grant can only have been
written by an approved `schedule_meeting` (the human authorization), so its
presence IS the authorization — and this stays fail-closed: no grant, no dial.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

VALID_STYLES = ("clerk", "scribe", "hold", "driver")
DIAL_TIMEOUT_S = 20.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.astimezone()
    except (ValueError, TypeError):
        return None


def find_live_grant(
    grants: list, number: str, now: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """The outbound_call grant that authorizes dialing `number`, or None.

    A grant authorizes only if it is for outbound_call, targets this exact
    number, hasn't expired, and has uses remaining. Anything else → None,
    which the caller treats as "not authorized, do not dial"."""
    now = now or _now()
    for g in grants or []:
        if not isinstance(g, dict):
            continue
        if g.get("action") != "outbound_call":
            continue
        if str(g.get("to", "")).strip() != str(number).strip():
            continue
        exp = _parse_iso(g.get("expires_at", "")) if g.get("expires_at") else None
        if exp is not None and now >= exp:
            continue
        uses = g.get("uses")
        if isinstance(uses, int) and uses <= 0:
            continue
        return g
    return None


def _consume_grant(grant: Dict[str, Any]) -> None:
    uses = grant.get("uses")
    if isinstance(uses, int):
        grant["uses"] = uses - 1


def _bridge_port() -> str:
    return (
        os.environ.get("HERMES_SERVER_PORT")
        or os.environ.get("BRIDGE_PORT")
        or "9001"
    )


def _post_dial(body: Dict[str, Any]) -> Tuple[bool, str]:
    """POST to the bridge's dial-meeting endpoint. Returns (ok, detail)."""
    url = f"http://127.0.0.1:{_bridge_port()}/api/voice/dial-meeting"
    data = json.dumps(body).encode()
    # Carry the bearer so scheduled meeting-joins survive the
    # BRIDGE_AUTH_ENFORCE flip (Chunk 1 Phase B); token is in the cron
    # subprocess env (scheduler spawns with os.environ.copy()).
    _hdrs = {"Content-Type": "application/json"}
    _tok = os.environ.get("BRIDGE_AUTH_TOKEN", "")
    if _tok:
        _hdrs["Authorization"] = f"Bearer {_tok}"
    req = urllib.request.Request(url, data=data, headers=_hdrs)
    try:
        with urllib.request.urlopen(req, timeout=DIAL_TIMEOUT_S) as resp:
            raw = resp.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001 — surface the real reason to chat
        return False, f"could not reach the voice service ({e})"
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = {}
    call_sid = parsed.get("call_sid") or parsed.get("sid")
    if call_sid:
        return True, str(call_sid)
    return False, (parsed.get("error") or raw[:200] or "the dial did not start")


def run_meeting_join(job: dict, agent_key: str = "thoth") -> Tuple[bool, str, str, Optional[str]]:
    """Execute a meeting_join job. Same (ok, output_doc, delivery_text, error)
    contract as run_job so the scheduler's delivery/marking path is unchanged.

    delivery_text is what the user reads in chat; it is written for both the
    success and failure cases so a missed meeting is never silent.
    """
    meeting = job.get("meeting") or {}
    label = str(meeting.get("label") or "the meeting").strip()
    number = str(meeting.get("dial_number") or "").strip()
    style = str(meeting.get("style") or "clerk").strip().lower()

    def _doc(status: str, detail: str) -> str:
        return (
            f"# Meeting Join: {label}\n\n"
            f"**Job ID:** {job.get('id')}\n"
            f"**Number:** {number or '(none)'}\n"
            f"**Style:** {style}\n"
            f"**Status:** {status}\n"
            f"**Detail:** {detail}\n"
        )

    if style not in VALID_STYLES:
        # REFUSE, never silently demote. An old venv receiving a new 'driver'
        # job used to dial it as a clerk PSTN call with no error; failing loud
        # is safer than a wrong-mode join.
        err = f"unknown meeting style '{style}'"
        return False, _doc("error", err), (
            f"I couldn't join {label} — I don't recognize the meeting mode "
            f"'{style}'. This build may be out of date."
        ), err

    if not number:
        err = "no dial-in number on the job"
        return False, _doc("error", err), (
            f"I couldn't dial into {label} — {err}."
        ), err

    grant = find_live_grant(job.get("grants") or [], number)
    if grant is None:
        err = "no live authorization for this call"
        return False, _doc("blocked", err), (
            f"I couldn't dial into {label} — my authorization to place that "
            f"call was missing or expired. Want me to set it up again?"
        ), err

    # Fire-time guard: a job that fires late (the Mini was asleep, a cron
    # backlog) must not dial a meeting that is already over. Use end_iso if
    # present, else start + 60 min; a 5-min grace keeps an in-progress meeting
    # joinable. Defense-in-depth on top of the grant-expiry backstop, which is
    # duration-agnostic.
    _start = _parse_iso(str(meeting.get("start_iso") or ""))
    if _start is not None:
        _end = _parse_iso(str(meeting.get("end_iso") or "")) or (_start + timedelta(minutes=60))
        if _now() >= _end + timedelta(minutes=5):
            err = "meeting already ended"
            return False, _doc("skipped", err), (
                f"I didn't dial into {label} — it was already over by the time "
                f"the job fired."
            ), err

    body = {
        "to": number,
        "style": style,
        "agent": agent_key,
        "label": label,
    }
    if meeting.get("pin"):
        body["pin"] = str(meeting["pin"])
    if meeting.get("join_url"):
        # driver: the bridge additionally launches the agent-browser guest-join
        # from this URL (Phase 2). Hybrid v1 still places the PSTN dial for audio.
        body["join_url"] = str(meeting["join_url"])
    if meeting.get("join_surface"):
        # Surface hint (teams_meet/teams_meetup/zoom/…) so the browser join drives
        # the right flow. Bridge re-derives from join_url if this is absent.
        body["join_surface"] = str(meeting["join_surface"])
    origin = job.get("origin") or {}
    if origin.get("chat_id"):
        body["session_id"] = str(origin["chat_id"])

    ok, detail = _post_dial(body)
    if ok:
        _consume_grant(grant)  # single-use: caller persists the mutated job
        return True, _doc("dialing", f"call_sid {detail}"), (
            f"I'm dialing into {label} now — I'll join as a {style} and the "
            f"live transcript will land in your Files when the call ends."
        ), None

    return False, _doc("failed", detail), (
        f"I tried to dial into {label} but couldn't — {detail}. "
        f"Want me to try again?"
    ), detail
