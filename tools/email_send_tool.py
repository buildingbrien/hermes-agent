"""email_send — send email as one reviewable, atomic action.

Before this tool, sending mail meant driving `himalaya` by hand through the
terminal tool. That failed three ways at once, and on 2026-08-10 it cost a real
send: a signed agreement the user approved five times never left the machine.

  1. It took TWO approvals. Writing the message to /tmp was one gated action
     and piping it to himalaya was another, so a single intent needed two taps
     with agent state in between.

  2. /tmp does not survive a human. Between "here is an approval card" and the
     user actually tapping it, the temp file was swept — so the send failed,
     the agent rebuilt the file, asked again, and the loop never terminated.

  3. The approval card could not say who the mail was for. The gate reads
     structured args; a heredoc is opaque to it, so the user was asked to
     approve `cat > /tmp/… << 'EMLEND'` under the summary "recipient not
     visible". That is the worst of both worlds: friction without information.

So: real arguments, one action, no intermediate file. The gate sees `to` and
`subject` and can render "Thoth wants to email courtenay@… — 'Signed
agreement'", which is a decision a person can actually make. The message is
piped straight to himalaya's stdin, so there is nothing on disk to expire.
"""

import mimetypes
import os
import subprocess
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any, Dict, List, Optional

# himalaya lives in a userland bin that a Finder-launched app does not inherit.
_HIMALAYA_CANDIDATES = [
    "/opt/homebrew/bin/himalaya",
    "/usr/local/bin/himalaya",
    os.path.expanduser("~/.cargo/bin/himalaya"),
    os.path.expanduser("~/.local/bin/himalaya"),
]

SEND_TIMEOUT_S = 120
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # SMTP realistically dies above this


def _himalaya() -> Optional[str]:
    for p in _HIMALAYA_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    from shutil import which
    return which("himalaya")


def _as_list(v: Any) -> List[str]:
    """Accept either a list or a comma-separated string for address fields."""
    if not v:
        return []
    if isinstance(v, str):
        return [p.strip() for p in v.split(",") if p.strip()]
    return [str(p).strip() for p in v if str(p).strip()]


def _valid(addr: str) -> bool:
    _, email = parseaddr(addr)
    return "@" in email and "." in email.split("@")[-1]


def _build_message(
    sender: str,
    to: List[str],
    cc: List[str],
    bcc: List[str],
    subject: str,
    body: str,
    attachments: List[str],
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)

    for path in attachments:
        p = os.path.expanduser(path)
        if not os.path.isfile(p):
            raise FileNotFoundError(f"attachment not found: {path}")
        size = os.path.getsize(p)
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachment too large ({size // 1024 // 1024}MB): {os.path.basename(p)}"
            )
        ctype, encoding = mimetypes.guess_type(p)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with open(p, "rb") as fh:
            msg.add_attachment(
                fh.read(),
                maintype=maintype,
                subtype=subtype,
                filename=os.path.basename(p),
            )
    return msg


def email_send_tool(args: Dict[str, Any], **_kw) -> Dict[str, Any]:
    args = args if isinstance(args, dict) else {}

    to = _as_list(args.get("to"))
    cc = _as_list(args.get("cc"))
    bcc = _as_list(args.get("bcc"))
    subject = (args.get("subject") or "").strip()
    body = args.get("body") or ""
    attachments = _as_list(args.get("attachments"))
    account = (args.get("account") or "fleet").strip()

    if not to:
        return {"error": "No recipient. Pass 'to' as an address or list of addresses."}
    bad = [a for a in (to + cc + bcc) if not _valid(a)]
    if bad:
        return {"error": f"These do not look like email addresses: {', '.join(bad)}"}
    if not subject:
        return {"error": "No subject. An email without one reads as spam."}

    binary = _himalaya()
    if not binary:
        return {
            "error": "himalaya is not installed or not on PATH — cannot send mail from "
                     "this machine."
        }

    # Resolve the From address from the account himalaya is configured with, so
    # the envelope matches the credentials actually used.
    sender = (args.get("from") or "").strip()
    if not sender:
        sender = f"Lucaryin Fleet <fleet-001@lucaryin.com>" if account == "fleet" else ""

    try:
        msg = _build_message(sender or "", to, cc, bcc, subject, body, attachments)
    except (FileNotFoundError, ValueError) as e:
        return {"error": str(e)}

    try:
        proc = subprocess.run(
            [binary, "message", "send", "-a", account],
            input=msg.as_bytes(),
            capture_output=True,
            timeout=SEND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"Sending timed out after {SEND_TIMEOUT_S}s — the message may "
                         f"or may not have gone out. Check the Sent folder before retrying."}
    except Exception as e:  # noqa: BLE001 — surface the real reason
        return {"error": f"Could not run himalaya: {e}"}

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip()
        return {"error": f"Send failed: {detail[:400] or 'himalaya exited non-zero'}"}

    recipients = len(to) + len(cc) + len(bcc)
    return {
        "sent": True,
        "to": to,
        "cc": cc,
        "subject": subject,
        "attachments": [os.path.basename(a) for a in attachments],
        "account": account,
        "summary": (
            f"Sent “{subject}” to {', '.join(to)}"
            + (f" (cc {', '.join(cc)})" if cc else "")
            + (f" with {len(attachments)} attachment(s)" if attachments else "")
            + f" — {recipients} recipient(s) total."
        ),
    }


EMAIL_SEND_SCHEMA = {
    "name": "email_send",
    "description": (
        "Send an email from one of this machine's configured mail accounts. "
        "Builds the message and sends it in a single action — do NOT compose "
        "mail by writing files in the terminal, which needs two approvals and "
        "loses the draft between them. Attachments are given as file paths."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Recipient address(es). A comma-separated string is also accepted.",
            },
            "subject": {"type": "string", "description": "Subject line."},
            "body": {"type": "string", "description": "Plain-text body of the message."},
            "cc": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional CC address(es).",
            },
            "bcc": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional BCC address(es).",
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional local file paths to attach (e.g. a signed PDF).",
            },
            "account": {
                "type": "string",
                "description": "Mail account to send from: 'fleet' (default), 'gmail', or 'zoho'.",
            },
            "from": {
                "type": "string",
                "description": "Optional explicit From header, e.g. 'Lucaryin Fleet <fleet-001@lucaryin.com>'.",
            },
        },
        "required": ["to", "subject", "body"],
    },
}


def check_email_requirements() -> tuple:
    """Available only where himalaya is actually installed."""
    if _himalaya():
        return True, ""
    return False, "himalaya is not installed on this machine"


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="email_send",
    toolset="email",
    schema=EMAIL_SEND_SCHEMA,
    handler=email_send_tool,
    check_fn=check_email_requirements,
    emoji="✉️",
)
