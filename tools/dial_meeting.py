"""Dial the fleet phone into a meeting and take notes.

The bridge already owns the hard parts: it places the Twilio call from the
machine's provisioned fleet number, punches the conference PIN, records only
the inbound leg (what the bot HEARS, never its own silent one), transcribes
locally with faster-whisper, and files the transcript into Documents/Lucaryin
with a note back into chat.

What was missing is this: a way for the person to simply ask.  Every piece of
that pipeline shipped, and the agent had no tool that reached it, so "have
Thoth sit in on my 12 o'clock" was not a thing anyone could do.
"""

import json
import os
import re
import urllib.error
import urllib.request

from tools.registry import tool_error

# Bridge ports by profile — same map fleet_send uses for direct routing.
AGENT_PORTS = {"thoth": 9001, "neith": 9007, "ptah": 9005, "set": 9006}

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _bridge_url(path: str) -> str:
    """This agent's own bridge.  HERMES_SERVER_PORT is set by the bridge when
    it spawns the worker; fall back to the profile's well-known port."""
    port = os.environ.get("HERMES_SERVER_PORT", "").strip()
    if not port:
        profile = (os.environ.get("BRIDGE_PROFILE", "") or "thoth").strip().lower()
        port = str(AGENT_PORTS.get(profile, 9001))
    return f"http://127.0.0.1:{port}{path}"


def _auth_headers() -> dict:
    h = {"Content-Type": "application/json"}
    token = os.environ.get("BRIDGE_AUTH_TOKEN", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _normalize_number(raw: str) -> str:
    """Accept what people actually paste — '(724) 442-9557', '1-724-442-9557',
    '+1 724 442 9557' — and return E.164, or "" if it cannot be made valid.

    Dial-ins get copied out of calendar invites, so being strict here means
    failing in front of a meeting that is already starting.
    """
    s = re.sub(r"[^\d+]", "", (raw or "").strip())
    if not s:
        return ""
    if not s.startswith("+"):
        # Bare 10-digit US number, or 11 digits starting with 1.
        digits = s
        if len(digits) == 10:
            s = "+1" + digits
        elif len(digits) == 11 and digits.startswith("1"):
            s = "+" + digits
        else:
            s = "+" + digits
    return s if _E164_RE.match(s) else ""


def dial_meeting_tool(args, **kwargs):
    to_raw = (args.get("to") or "").strip()
    if not to_raw:
        return tool_error(
            "Need the meeting's dial-in phone number. It is usually in the "
            "calendar invite next to the video link (e.g. '+1 941-800-3261')."
        )

    to = _normalize_number(to_raw)
    if not to:
        return tool_error(
            f"'{to_raw}' does not look like a dialable phone number. Meeting "
            "dial-ins look like '+1 941-800-3261'. A room URL or meeting code "
            "will not work on its own — the invite's phone number is needed."
        )

    pin = re.sub(r"[^\d#*w]", "", (args.get("pin") or "").strip())
    label = (args.get("label") or "").strip()[:120]

    minutes = args.get("minutes")
    try:
        minutes = int(minutes) if minutes else 120
    except (TypeError, ValueError):
        minutes = 120
    time_limit_s = max(60, min(minutes * 60, 14400))  # 1 min – 4 h

    # Two very different ways to be on a call (founder finding 2026-08-18:
    # he added a bot to a live call, addressed it by name, and it could not
    # answer — because this tool only ever reached the notetaker, which has
    # NO live audio path at all; the transcript is batch, after the call):
    #   notetaker — silent recorder, transcript afterward (the default).
    #   clerk     — live participant via the voice server stream: hears the
    #               call in real time and SPEAKS when addressed by wake word
    #               ("copilot" or the agent's name).
    mode = (args.get("mode") or "notetaker").strip().lower()
    if mode not in ("notetaker", "clerk"):
        mode = "notetaker"

    if mode == "clerk":
        agent = (os.environ.get("BRIDGE_PROFILE", "") or "thoth").strip().lower()
        payload = {
            "to": to,
            "pin": pin,
            "label": label,
            "style": "clerk",
            "agent": agent,
            "time_limit_s": time_limit_s,
        }
        endpoint = "/api/voice/dial-meeting"
    else:
        payload = {
            "to": to,
            "pin": pin,
            "label": label,
            "time_limit_s": time_limit_s,
            "session_id": os.environ.get("HERMES_SESSION_ID", "")[:80],
        }
        for key in ("email_to", "email_cc"):
            val = (args.get(key) or "").strip()
            if val:
                payload[key] = val[:200]
        endpoint = "/api/voice/notetaker"

    req = urllib.request.Request(
        _bridge_url(endpoint),
        data=json.dumps(payload).encode(),
        headers=_auth_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # The bridge sends readable reasons (no fleet number provisioned, bad
        # PIN, Twilio rejected the call).  Pass them through rather than
        # flattening everything into "request failed".
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:
            detail = ""
        return tool_error(detail or f"The bridge refused the call (HTTP {e.code}).")
    except urllib.error.URLError as e:
        return tool_error(
            f"Could not reach this machine's bridge to place the call ({e.reason}). "
            "The Lucaryin app needs to be running."
        )
    except Exception as e:  # noqa: BLE001 - surface anything else verbatim
        return tool_error(f"Could not place the call: {e}")

    if not result.get("success"):
        return tool_error(result.get("error", "The call could not be placed."))

    where = label or to
    recap = ""
    if payload.get("email_to"):
        recap = f" A recap will be emailed to {payload['email_to']}"
        if payload.get("email_cc"):
            recap += f" (cc {payload['email_cc']})"
        recap += " once it ends."

    if mode == "clerk":
        message = (
            f"Dialing into {where} now from the fleet line as a LIVE "
            f"participant. I can hear the call in real time — address me as "
            f"\"copilot\" or by my name and I will answer out loud. I stay on "
            f"for up to {minutes} minutes; the transcript is filed after."
        )
    else:
        message = (
            f"Dialing into {where} now from the fleet line. I will stay on the "
            f"call silently for up to {minutes} minutes, then transcribe it. "
            f"The notes land in Files → Meeting notes and I will post here when "
            f"they are ready.{recap} (I cannot hear or speak live in this "
            f"mode — ask for mode=clerk if you want me participating.)"
        )

    return json.dumps({
        "success": True,
        "mode": mode,
        "call_sid": result.get("call_sid", ""),
        "dialed": to,
        "label": label,
        "status": result.get("status", ""),
        "message": message,
    })


DIAL_MEETING_SCHEMA = {
    "name": "dial_meeting",
    "description": (
        "Call into a phone/conference meeting from this machine's fleet "
        "number. Two modes:\n"
        "mode='notetaker' (default) — join SILENTLY, record, transcribe after "
        "the call, file the notes (optionally email a recap). It cannot hear "
        "or speak live.\n"
        "mode='clerk' — join as a LIVE participant: hears the call in real "
        "time and speaks when addressed by wake word ('copilot' or the "
        "agent's name). Use clerk whenever the person wants you to "
        "PARTICIPATE, answer questions, or be available on the call — e.g. "
        "'join and answer questions', 'be on the call with me', 'add you to "
        "a call so I can ask you things'. Writing 'clerk' in the label does "
        "NOT do this — only mode='clerk' does.\n"
        "Needs the dial-in PHONE NUMBER from the invite; a video link alone "
        "will not work, so ask for the number if you only have a URL.\n"
        "Be honest about the mode you chose: notetaker notes come after the "
        "meeting ends; clerk answers live."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Dial-in phone number from the invite, e.g. '+1 941-800-3261'.",
            },
            "mode": {
                "type": "string",
                "enum": ["notetaker", "clerk"],
                "description": "notetaker = silent recorder (default). clerk = live participant that hears the call and answers when addressed ('copilot' or agent name).",
            },
            "pin": {
                "type": "string",
                "description": "Conference PIN/passcode, digits only (the '#' is added automatically).",
            },
            "label": {
                "type": "string",
                "description": "What to call this meeting in the notes, e.g. 'Comcast pod standup'.",
            },
            "minutes": {
                "type": "integer",
                "description": "How long to stay on the call before hanging up. Default 120, max 240.",
            },
            "email_to": {
                "type": "string",
                "description": "Email the recap here when the transcript is ready.",
            },
            "email_cc": {
                "type": "string",
                "description": "CC this address on the recap email.",
            },
        },
        "required": ["to"],
    },
}


# --- Registry ---
from tools.registry import registry

registry.register(
    name="dial_meeting",
    toolset="voice",
    schema=DIAL_MEETING_SCHEMA,
    handler=dial_meeting_tool,
    emoji="📞",
)
