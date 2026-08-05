"""Check on, or wrap up, a meeting the fleet phone is taking notes on.

Companion to dial_meeting. Two real failures on a customer's first organic
use (2026-08-04) motivate this tool:

  1. The customer asked "did you get notes from the call?" while the call was
     still live. With no honest status to read, the agent reported "no
     transcript was generated" — telling him his meeting notes were LOST when
     they simply weren't written yet. This tool's `status` returns the true
     state (recording / processing / ready) so that can't happen again.

  2. The call sat "in-progress" for ~33 minutes past the actual meeting,
     because nothing ended it. `wrap_up` hangs up and transcribes right away.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from tools.registry import tool_error

AGENT_PORTS = {"thoth": 9001, "neith": 9007, "ptah": 9005, "set": 9006}


def _bridge_url(path: str) -> str:
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


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _bridge_url(path), data=data, headers=_auth_headers(), method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"success": False, "error": f"bridge HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"success": False, "error":
                f"Could not reach this machine's bridge ({e.reason}). "
                "The Lucaryin app needs to be running."}


def meeting_notes_tool(args, **kwargs):
    action = (args.get("action") or "status").strip().lower()
    call_sid = (args.get("call_sid") or "").strip()
    if call_sid and not re.fullmatch(r"CA[0-9a-fA-F]{32}", call_sid):
        return tool_error("call_sid must be a Twilio CA… sid, or omit it for "
                          "the most recent meeting.")

    if action == "status":
        q = f"?call_sid={urllib.parse.quote(call_sid)}" if call_sid else ""
        result = _request("GET", f"/api/voice/notetaker/status{q}")
    elif action in ("wrap_up", "wrapup", "end"):
        result = _request("POST", "/api/voice/notetaker/end",
                          {"call_sid": call_sid})
    else:
        return tool_error(f"Unknown action '{action}'. Use 'status' or 'wrap_up'.")

    if not result.get("success"):
        return tool_error(result.get("error", "Could not reach the meeting recording."))
    # Pass the bridge's state + speakable message straight through.
    return json.dumps({k: v for k, v in result.items() if k != "success"})


MEETING_NOTES_SCHEMA = {
    "name": "meeting_notes",
    "description": (
        "Check on, or wrap up, a meeting the fleet phone is taking notes on "
        "(started with dial_meeting).\n"
        "action='status' — is the transcript ready? Returns one of: "
        "recording (still on the call — notes come AFTER it ends), processing "
        "(call ended, transcribing now), ready (notes filed), no_audio, "
        "failed.\n"
        "action='wrap_up' — hang up the call now and transcribe immediately; "
        "use when the user says the meeting is over.\n"
        "CRITICAL: if the state is 'recording' or 'processing', the notes are "
        "PENDING, not lost. Never tell the user their notes weren't captured "
        "or the transcript failed while a call is still recording or "
        "processing — say they're on the way, and offer to wrap up the call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "wrap_up"],
                "description": "'status' to check, 'wrap_up' to end the call and transcribe now.",
            },
            "call_sid": {
                "type": "string",
                "description": "The call's Twilio SID. Omit to use the most recent meeting.",
            },
        },
        "required": ["action"],
    },
}


from tools.registry import registry

registry.register(
    name="meeting_notes",
    toolset="voice",
    schema=MEETING_NOTES_SCHEMA,
    handler=meeting_notes_tool,
    emoji="📝",
)
