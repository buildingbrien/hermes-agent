"""schedule_meeting — book the fleet phone to dial into a meeting later.

The attended half of scheduled meeting-join (spec:
docs/scheduled-meeting-join-spec-2026-08-12.md). When the user asks an agent
to join a meeting at a future time, the agent calls THIS instead of merely
saying "I'll be there" — a promise with no scheduled job behind it is the
false-promise class we keep killing.

What it does, all at schedule time while a human is present:
  1. Resolves the start time to an ABSOLUTE instant in the machine's local
     zone (no "16:00 UTC vs local" ambiguity at fire time).
  2. Creates a one-shot `meeting_join` job that fires at start − lead minutes.
  3. Writes an `outbound_call` grant onto that job — the encoding of "the user
     authorized this dial" — scoped to the exact number, single-use, expiring
     30 min after start. The deterministic executor honors only this grant.

This tool is gated (action_type `meeting_schedule`, sensitive): the approval
the user gives HERE is the one and only human authorization for the future
call. It is attended-only — never exposed to cron (added to the cron
disabled_toolsets), because a scheduler can't re-authorize a dial.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

DEFAULT_STYLE = "clerk"
DEFAULT_LEAD_MINUTES = 2
GRANT_EXPIRY_MINUTES_AFTER_START = 30
VALID_STYLES = ("clerk", "scribe", "hold", "driver")


def _resolve_start(start: str) -> datetime:
    """Parse an ISO start time; make naive values local-aware. Raises on junk."""
    dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.astimezone()


def schedule_meeting_tool(args: Dict[str, Any], **_kw) -> Dict[str, Any]:
    args = args if isinstance(args, dict) else {}
    number = str(args.get("dial_number") or "").strip()
    start_raw = str(args.get("start") or "").strip()
    label = str(args.get("label") or "meeting").strip() or "meeting"
    style = str(args.get("style") or DEFAULT_STYLE).strip().lower()
    pin = str(args.get("pin") or "").strip()
    join_url = str(args.get("join_url") or "").strip()  # driver: browser guest-join

    if style not in VALID_STYLES:
        style = DEFAULT_STYLE
    if not number:
        return {"error": "No dial-in number. Pass 'dial_number' (e.g. '+15551234567')."}
    if not start_raw:
        return {"error": "No start time. Pass 'start' as an ISO time, e.g. '2026-08-13T15:00'."}

    try:
        start_dt = _resolve_start(start_raw)
    except (ValueError, TypeError):
        return {"error": f"Couldn't read the start time {start_raw!r}. Use ISO, e.g. '2026-08-13T15:00'."}

    now = datetime.now(start_dt.tzinfo)
    if start_dt <= now:
        return {"error": "That start time is in the past. Give a future time."}

    try:
        lead = int(args.get("lead_minutes", DEFAULT_LEAD_MINUTES))
    except (TypeError, ValueError):
        lead = DEFAULT_LEAD_MINUTES
    lead = max(0, min(lead, 30))

    fire_dt = start_dt - timedelta(minutes=lead)
    if fire_dt <= now:
        fire_dt = now + timedelta(seconds=30)  # meeting is imminent — dial ~now
    expires_dt = start_dt + timedelta(minutes=GRANT_EXPIRY_MINUTES_AFTER_START)

    meeting = {
        "dial_number": number,
        "pin": pin or None,
        "style": style,
        "label": label,
        "start_iso": start_dt.isoformat(),
        # Auto-admit: no waiting-room nudge (the user already signalled intent
        # by scheduling). A quiet chat trace is delivered at dial time instead,
        # and a lobby timeout catches the rare non-admit.
        "notify_admit": False,
    }
    if style == "driver" and join_url:
        meeting["join_url"] = join_url
    grants = [{
        "action": "outbound_call",
        "to": number,
        "reason": f"meeting:{label}",
        "expires_at": expires_dt.isoformat(),
        "uses": 1,
    }]
    if style == "driver":
        # A user-scheduled driver join is user-initiated (trusted) — mint the
        # meeting-duration screen-control grant so in-meeting share/drive runs
        # card-free. Schema matches approval_gate._grant_permits.
        ui_expires = start_dt + timedelta(
            minutes=GRANT_EXPIRY_MINUTES_AFTER_START + 150)
        grants.append({
            "action": "ui_action",
            "unattended": True,
            "reason": f"meeting-driver:{label}",
            "expires": ui_expires.isoformat(),
            "expires_at": ui_expires.isoformat(),
            "max_per_run": 500,
        })

    try:
        from cron.jobs import create_job
        from tools.cronjob_tools import _origin_from_env
    except Exception as e:  # noqa: BLE001
        return {"error": f"Scheduling isn't available here: {e}"}

    origin = None
    try:
        origin = _origin_from_env()
    except Exception:
        origin = None

    job = create_job(
        prompt="",
        schedule=fire_dt.isoformat(),
        name=f"Dial {label}",
        repeat=1,
        origin=origin,
        deliver="origin" if origin else "local",
        job_type="meeting_join",
        meeting=meeting,
        grants=grants,
    )

    pretty_start = start_dt.strftime("%a %b %-d at %-I:%M %p")
    return {
        "scheduled": True,
        "job_id": job["id"],
        "label": label,
        "dial_number": number,
        "style": style,
        "start": start_dt.isoformat(),
        "summary": (
            f"Scheduled: I'll dial {number} into “{label}” as a {style} on "
            f"{pretty_start} (connecting ~{lead} min early). You don't need to "
            f"remind me — it's authorized and on the Jobs board if you want to "
            f"change or cancel it."
        ),
    }


SCHEDULE_MEETING_SCHEMA = {
    "name": "schedule_meeting",
    "description": (
        "Schedule the fleet phone to dial into a meeting at a future time, "
        "and join automatically — use this whenever the user asks you to "
        "join, attend, or call into a meeting later, INSTEAD of just saying "
        "you will. Do not promise to join without calling this. It captures "
        "the dial-in and the user's authorization now so the call happens "
        "unattended at the right time. Phone dial-in meetings only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dial_number": {
                "type": "string",
                "description": "The meeting's phone dial-in number, E.164 (e.g. '+15551234567').",
            },
            "start": {
                "type": "string",
                "description": "Meeting start time, ISO 8601. Naive times are read in the user's local zone. E.g. '2026-08-13T15:00'.",
            },
            "label": {
                "type": "string",
                "description": "Short meeting name for the transcript and the user's confirmation (e.g. 'Q3 Review').",
            },
            "style": {
                "type": "string",
                "enum": list(VALID_STYLES),
                "description": "clerk (hears all, speaks when named — default), scribe (silent notes), hold (present, silent), or driver (browser guest-join with vision + can share/drive the screen — pass join_url).",
            },
            "join_url": {
                "type": "string",
                "description": "For style 'driver' only: the meeting's join URL (e.g. a Teams/Zoom/Meet link) the agent browser guest-joins.",
            },
            "pin": {
                "type": "string",
                "description": "Optional conference PIN / access code, entered after connecting.",
            },
            "lead_minutes": {
                "type": "integer",
                "description": "Dial this many minutes before start to clear the lobby (default 2).",
            },
        },
        "required": ["dial_number", "start", "label"],
    },
}


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="schedule_meeting",
    toolset="voice",
    schema=SCHEDULE_MEETING_SCHEMA,
    handler=schedule_meeting_tool,
    emoji="📞",
)
