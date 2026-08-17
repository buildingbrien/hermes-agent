"""Words for a job's standing grants, addressed to the agent running the job.

Kept in its own module (not scheduler.py) so tests can pin the phrasing
without importing the scheduler, and kept out of cron_gate.py because that
file ships with the bridge while this one ships with the runtime.
"""
from typing import Any, List

_PHRASES = {
    "email_send": "send email",
    "message_send": "send messages",
    "calendar_create": "create calendar events",
    "meeting_schedule": "schedule meetings",
    "outbound_call": "place phone calls",
    "file_write": "write files",
    "ui_action": "act in the user's signed-in browser",
}


def describe_grants_for_prompt(grants: List[Any]) -> str:
    """One bullet per grant, in the agent's second person. Never raises."""
    lines = []
    for g in grants or []:
        try:
            if isinstance(g, str):
                lines.append(f"- You may {_PHRASES.get(g, g.replace('_', ' '))}.")
                continue
            if not isinstance(g, dict):
                continue
            act = str(g.get("action", ""))
            phrase = _PHRASES.get(act, act.replace("_", " "))
            line = f"- You may {phrase}"
            if g.get("to"):
                tos = g["to"] if isinstance(g["to"], list) else [g["to"]]
                line += " to " + " or ".join(str(t) for t in tos)
                line += " ONLY"
            caps = []
            if g.get("max_per_run") is not None:
                caps.append(f"at most {g['max_per_run']} per run")
            if g.get("max_per_day") is not None:
                caps.append(f"{g['max_per_day']} per day")
            if g.get("expires"):
                caps.append(f"until {str(g['expires'])[:10]}")
            if caps:
                line += f" ({', '.join(caps)})"
            lines.append(line + ".")
        except Exception:
            continue
    return "\n".join(lines)
