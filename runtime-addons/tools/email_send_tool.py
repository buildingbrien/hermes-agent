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
import re
import subprocess
from email import message_from_bytes
from email import policy as email_policy
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
    """Accept either a list or a comma-separated string (attachment paths)."""
    if not v:
        return []
    if isinstance(v, str):
        return [p.strip() for p in v.split(",") if p.strip()]
    return [str(p).strip() for p in v if str(p).strip()]


# ── Recipients: exactly the set the approval gate checked ───────────────────
# A standing grant ("may email these people") is checked by the bridge gate
# (hermes-bridge approval_gate._recipients_of) against EVERY recipient it reads
# out of to/cc/bcc. What this tool mails must be that same set, so:
#   * address fields are split exactly where the gate splits them: ',', ';'
#     and line breaks (approval_gate._RECIPIENT_SEP_RE), inside a string AND
#     inside every list item;
#   * the To/Cc/Bcc headers carry the BARE addresses only. The gate reads the
#     address inside "Name <addr>", but a display name is parsed by whatever
#     reads the header next: EmailMessage decodes an RFC 2047 encoded-word name
#     and writes it back unquoted, so
#     "=?utf-8?q?Owner_=3Cz=40evil.com=3E=2C?= <owner@x.com>" went out as
#     "Owner <z@evil.com>, <owner@x.com>", a second, real recipient. Dropping
#     the name removes that whole class.
_RECIPIENT_SEP_RE = re.compile(r"[,;\r\n]")

# An RFC 5322 dot-atom addr-spec, ASCII only: no quoted local part, no
# comments, no whitespace, and a dotted domain. Nothing in it can split into a
# second address.
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_ADDR_SPEC_RE = re.compile(rf"{_ATEXT}+(?:\.{_ATEXT}+)*@{_LABEL}(?:\.{_LABEL})+")
# "anything <addr>": the name is dropped, so it is not inspected.
_NAMED_ADDR_RE = re.compile(r"([^<>]*)<([^<>]*)>")
# An explicit From: plain ASCII name (letters, digits, spaces, . ' _ -),
# optionally in plain double quotes. No commas or group syntax (more than one
# From mailbox), and no = ? (an encoded word, which EmailMessage decodes).
_FROM_NAME_ADDR_RE = re.compile(r"""(?:[A-Za-z0-9 .'_-]*|"[A-Za-z0-9 .'_-]*") *<([^<>]*)>""")


def _is_addr_spec(s: str) -> bool:
    # "=?" starts an encoded word, which EmailMessage decodes even inside an
    # address: "=?utf-8?q?z=40evil.com?=@x.com" serialises as "z@evil.com@x.com".
    return bool(_ADDR_SPEC_RE.fullmatch(s)) and "=?" not in s


def _bare_address(piece: Any) -> Optional[str]:
    """'bob@x.com' or 'Any Name <bob@x.com>' → 'bob@x.com'; anything else → None."""
    if not isinstance(piece, str):
        return None
    s = piece.strip()
    m = _NAMED_ADDR_RE.fullmatch(s)
    if m:
        s = m.group(2).strip()
    return s if _is_addr_spec(s) else None


def _address_pieces(v: Any) -> Optional[List[str]]:
    """Every raw recipient in an address field ([] when absent), split the way
    the gate splits it. None when the field is not a string or a list of
    strings: a dict would be read by its keys, a number would crash."""
    if v is None or (isinstance(v, (str, list, tuple)) and not v):
        return []
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, (list, tuple)) or not all(isinstance(x, str) for x in items):
        return None
    return [p.strip() for item in items for p in _RECIPIENT_SEP_RE.split(item) if p.strip()]


def _wire_addresses(header: str, value: str) -> List[str]:
    """The addresses a header value carries once serialised and parsed back —
    what the MTA reads, not what we meant."""
    probe = EmailMessage()
    probe[header] = value
    wire = message_from_bytes(probe.as_bytes(), policy=email_policy.default)
    return [a.addr_spec for a in wire[header].addresses]


def _address_header(header: str, addrs: List[str]) -> str:
    """A To/Cc/Bcc value of bare addresses only. Raises ValueError unless it
    serialises to exactly those addresses (a differential check behind the
    addr-spec pattern, in case some parser quirk still reinterprets one)."""
    bare = []
    for a in addrs:
        b = _bare_address(a)
        if b is None:
            raise ValueError(f"not an email address: {a!r}")
        bare.append(b)
    value = ", ".join(bare)
    try:
        exact = _wire_addresses(header, value) == bare
    except Exception:  # noqa: BLE001 - the caller only turns ValueError into an error reply
        exact = False
    if not exact:
        raise ValueError(f"{header} would not reach exactly {value}")
    return value


def _explicit_from(value: Any) -> Optional[str]:
    """The 'from' argument as the From header will carry it: a bare address,
    or a plain ASCII 'Name <address>'. None for anything else, including any
    line break (which can start a new header such as Bcc)."""
    if not isinstance(value, str) or not value.isascii() or not value.isprintable():
        return None
    s = value.strip()
    if _is_addr_spec(s):
        addr = s
    else:
        m = _FROM_NAME_ADDR_RE.fullmatch(s)
        addr = m.group(1).strip() if m else ""
        if not _is_addr_spec(addr):
            return None
    try:
        return s if _wire_addresses("From", s) == [addr] else None
    except Exception:  # noqa: BLE001 - an unparseable From is simply not accepted
        return None


def _org_signature() -> tuple:
    """Return (text_sig, html_sig) for this machine/org — the company signature
    the send tool appends so every fleet email carries it (there was NO signature
    mechanism before: himalaya's piped `message send` injects nothing and no
    signature= was set). Editable per-org. Priority: env override → this agent's
    HERMES_HOME file → the shared ~/.hermes org file → empty. Never raises."""
    txt = os.environ.get("LUCARYIN_EMAIL_SIGNATURE", "")
    html = os.environ.get("LUCARYIN_EMAIL_SIGNATURE_HTML", "")
    homes = []
    h = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
    homes.append(h)
    canonical = os.path.expanduser("~/.hermes")
    try:
        if os.path.realpath(h) != os.path.realpath(canonical):
            homes.append(canonical)  # per-agent profile → fall back to the org file
    except Exception:
        pass
    for d in homes:
        if not txt:
            try:
                with open(os.path.join(d, "email-signature.txt")) as f:
                    txt = f.read().strip()
            except Exception:
                pass
        if not html:
            try:
                with open(os.path.join(d, "email-signature.html")) as f:
                    html = f.read().strip()
            except Exception:
                pass
    return txt, html


# ── Idempotent-send ledger ───────────────────────────────────────────────────
# The send is the LAST line of defence against a double-send. Upstream approval
# dedup reduces which sends get filed, but retries, the approval resume/re-drive,
# the */15 auto-recap cron, and any route-around can still call this tool more
# than once for one outcome. Reserving BEFORE the himalaya call means exactly
# one of them physically sends; the rest are reported as not sent. (The founder
# received the same recap 3x on 2026-08-24 — the cron recap plus re-sends, each
# re-rendering the body; recipient+subject was the keystone that stopped it.)
#
# Two keys since R2-1-38 (bug hunt round 2; decision recorded in the HA PR):
#   * the MESSAGE key — recipients (to/cc/bcc), subject with Re:/Fwd: folded,
#     body, html and attachment paths. The same message inside the window is
#     never sent twice, whoever asks.
#   * the THREAD key — recipients (to/cc/bcc) + folded subject, the Aug-24
#     keystone. It still blocks a send when EITHER side of the match is
#     unattended (a cron run): a re-rendered recap, or a manual re-send right
#     after a cron recap, is the Aug-24 class. Two ATTENDED sends in a
#     conversation — a reply in the same thread, a corrected body, a new bcc —
#     are different messages and both go out (they were silently dropped and
#     reported sent:true).
# A skip returns one of two shapes — the EMAIL_SEND RESULT CONTRACT, which the
# bridge (lucaryin-ai hermes-bridge worker/server) reads and mirrors in a
# fixture test; change both repos together (see _dedup_result).
# force=true sends a deliberate second copy.
import hashlib
import json
import time as _time

from tools.lucaryin_filelock import exclusive_lock, replace_with_retry, unique_tmp

_SEND_WINDOW_S = 2700  # 45 min — matches the approval-store DOA window


def _ledger_path() -> str:
    home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
    return os.path.join(home, "email-send-ledger.json")


def _addr_only(a: str) -> str:
    _, e = parseaddr(a or "")
    return (e or a or "").strip().lower()


def _thread_parts(to: List[str], cc: List[str], subject: str, bcc=()) -> str:
    addrs = sorted({_addr_only(a) for a in (list(to) + list(cc) + list(bcc or ())) if a})
    subj = " ".join((subject or "").lower().split())
    while subj[:3] in ("re:", "fw:") or subj[:4] == "fwd:":
        subj = subj.split(":", 1)[1].strip()
    return "|".join(addrs) + "||" + subj


def _thread_key(to: List[str], cc: List[str], subject: str, *, bcc=()) -> str:
    """Recipients (bcc included) + subject with reply/forward prefixes folded."""
    return "t:" + hashlib.sha256(_thread_parts(to, cc, subject, bcc).encode()).hexdigest()[:40]


def _idem_key(to: List[str], cc: List[str], subject: str, *, bcc=(), body: str = "",
              html: str = "", attachments=()) -> str:
    """The MESSAGE key: every recipient (bcc included — a bcc is a different
    audience), the subject with reply/forward prefixes folded, and the exact
    content (body, html, attachment paths). Whitespace-only edits to the body do
    not count as a different message; any other edit does."""
    content = hashlib.sha256()
    for part in (" ".join((body or "").split()), " ".join((html or "").split()),
                 "\x00".join(str(a) for a in (attachments or ()))):
        content.update(part.encode("utf-8", "surrogatepass"))
        content.update(b"\x1f")
    return "m:" + hashlib.sha256(
        (_thread_parts(to, cc, subject, bcc) + "||" + content.hexdigest()).encode()).hexdigest()[:40]


def _is_unattended_send() -> bool:
    """True inside a cron run (the runtime's own contextvar-first marker,
    HERMES_CRON_SESSION). Anything unreadable counts as attended: the message
    key still dedups it."""
    try:
        from tools.approval_context import _is_cron_approval_context
        return bool(_is_cron_approval_context())
    except Exception:  # noqa: BLE001
        return False


def _ledger_txn(fn):
    """Run fn(ledger, now) under an exclusive cross-process lock (fcntl or msvcrt
    — tools/lucaryin_filelock.py; the old in-line ``import fcntl`` raised on
    Windows), prune the window and persist. IO/lock failure runs fn against
    whatever could be read (a missing dedup is recoverable; a stuck send is
    not) — but a readable ledger is authoritative, so the reserve below only
    skips on real, fresh records."""
    path = _ledger_path()
    with exclusive_lock(path + ".lock"):
        try:
            with open(path, encoding="utf-8") as f:
                ledger = json.load(f)
            if not isinstance(ledger, dict):
                ledger = {}
        except Exception:
            ledger = {}
        now = _time.time()
        ledger = {k: v for k, v in ledger.items()
                  if isinstance(v, dict) and now - float(v.get("ts") or 0) < _SEND_WINDOW_S}
        out = fn(ledger, now)
        tmp = unique_tmp(path)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(ledger, f)
            replace_with_retry(tmp, path)
        except Exception:
            pass
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return out


def _live(rec, now: float) -> bool:
    """A record that blocks an equivalent send: sent in the window, or a pending
    attempt younger than the send timeout (older = a crashed attempt, free)."""
    if not isinstance(rec, dict):
        return False
    if rec.get("status") == "sent":
        return True
    return rec.get("status") == "pending" and now - float(rec.get("ts") or 0) < SEND_TIMEOUT_S


def _reserve_send(message_key: str, thread_key: Optional[str] = None, *, unattended: bool = False):
    """Atomically decide whether THIS call physically sends. Returns
    ``('go', reservation)`` — pass the reservation to _commit_send /
    _release_send — or ``('skip', prior)`` where prior carries ``match``:
    ``'message'`` (the same message) or ``'thread'`` (same recipients and
    subject, with a cron run on one side)."""
    def _op(ledger, now):
        rec = ledger.get(message_key)
        if _live(rec, now):
            return ("skip", dict(rec, match="message"))
        trec = ledger.get(thread_key) if thread_key else None
        if _live(trec, now) and (unattended or trec.get("unattended")):
            return ("skip", dict(trec, match="thread"))
        reservation = {"message_key": message_key, "thread_key": thread_key,
                       "unattended": bool(unattended), "prior_thread": trec}
        ledger[message_key] = {"ts": now, "status": "pending"}
        if thread_key:
            ledger[thread_key] = {"ts": now, "status": "pending", "unattended": bool(unattended)}
        return ("go", reservation)
    return _ledger_txn(_op)


def _as_reservation(res) -> dict:
    """Accept the reservation dict, or a bare message key (older callers)."""
    return res if isinstance(res, dict) else {"message_key": res, "thread_key": None,
                                              "unattended": False, "prior_thread": None}


def _commit_send(res, summary: str) -> None:
    r = _as_reservation(res)

    def _op(ledger, now):
        ledger[r["message_key"]] = {"ts": now, "status": "sent", "summary": summary}
        if r.get("thread_key"):
            ledger[r["thread_key"]] = {"ts": now, "status": "sent", "summary": summary,
                                       "unattended": bool(r.get("unattended"))}
    _ledger_txn(_op)


def _release_send(res) -> None:
    """Definitively not sent: free the message key and put the thread key back
    the way it was (an earlier send's record must survive this failed attempt)."""
    r = _as_reservation(res)

    def _op(ledger, now):
        ledger.pop(r["message_key"], None)
        tk = r.get("thread_key")
        if tk:
            prior = r.get("prior_thread")
            if isinstance(prior, dict):
                ledger[tk] = prior
            else:
                ledger.pop(tk, None)
    _ledger_txn(_op)


# EMAIL_SEND RESULT CONTRACT for a de-duplicated call (Wave 2 lead decision;
# lucaryin-ai carries the mirror fixture). Exactly these keys, nothing else:
#
#   match "message" — the identical message already went out in the window. It
#   WAS delivered (earlier), so it counts as sent everywhere: the chat line, the
#   worker's false-success guard, and an approval that resumes into it.
#     {sent: True, already_sent: True, deduplicated: True, match: "message",
#      idempotent_skip: True, summary}
#
#   match "thread" — same recipients + subject from a cron run inside the hold
#   window, different content. It did NOT go out, and an approval must not be
#   consumed by it: no idempotent_skip, no error field.
#     {sent: False, held: True, deduplicated: True, match: "thread", reason, summary}
DEDUP_MESSAGE_KEYS = frozenset(
    {"sent", "already_sent", "deduplicated", "match", "idempotent_skip", "summary"})
DEDUP_THREAD_KEYS = frozenset({"sent", "held", "deduplicated", "match", "reason", "summary"})


def _dedup_result(match: Optional[str], to: List[str], subject: str) -> Dict[str, Any]:
    """The result of a call the ledger skipped (see the contract above)."""
    minutes = _SEND_WINDOW_S // 60
    who = ", ".join(to)
    if match == "message":
        return {
            "sent": True,
            "already_sent": True,
            "deduplicated": True,
            "match": "message",
            "idempotent_skip": True,
            "summary": (f"Already sent: “{subject}” to {who} went out within the last "
                        f"{minutes} minutes; not sent a second time."),
        }
    # "thread" (the only other value _reserve_send returns): nothing went out.
    return {
        "sent": False,
        "held": True,
        "deduplicated": True,
        "match": "thread",
        "reason": (
            f"An email with this subject (“{subject}”) already went to {who} within "
            f"the last {minutes} minutes and a scheduled job is involved, so this "
            "different version was held back rather than sent as a second copy. If "
            "the user asked for a new copy, send again with force=true."
        ),
        "summary": (f"Held, not sent: “{subject}” to {who} — a scheduled job already "
                    f"sent this subject within the last {minutes} minutes."),
    }


def _build_message(
    sender: str,
    to: List[str],
    cc: List[str],
    bcc: List[str],
    subject: str,
    body: str,
    attachments: List[str],
    html: str = "",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    # Bare addresses only, whatever the caller passed: see _address_header.
    msg["To"] = _address_header("To", to)
    if cc:
        msg["Cc"] = _address_header("Cc", cc)
    if bcc:
        msg["Bcc"] = _address_header("Bcc", bcc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    # An HTML alternative makes this multipart/alternative: the plain-text body
    # above stays as the fallback (and the deliverability/spam win of a real
    # text part), while HTML-capable clients render this. Must precede the
    # attachment loop so the tree nests correctly —
    # multipart/mixed[ multipart/alternative[text, html], attachments… ].
    if html:
        msg.add_alternative(html, subtype="html")

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


# himalaya keeps each account's own identity (email + display-name) here. We
# read it so a send from ANY configured account (gmail/zoho/…) carries that
# account's real From — historically only the hardcoded 'fleet' identity was
# filled, so a send from any other account left an EMPTY From header and
# himalaya bounced it with the opaque "cannot send message without a sender".
HIMALAYA_CONFIG = os.path.expanduser("~/.config/himalaya/config.toml")


def _account_from(account: str) -> str:
    """Resolve an account's From identity ("Display Name <email>") from the
    himalaya config. Returns "" when the account or its email isn't found —
    the caller turns that into an actionable error instead of letting himalaya
    reject an empty From."""
    try:
        with open(HIMALAYA_CONFIG, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    # Isolate the [accounts.<account>] table (bare or quoted header), stopping
    # at the next top-level [section].
    m = re.search(
        r'(?ms)^\[accounts\.(?:"' + re.escape(account) + r'"|'
        + re.escape(account) + r')\][ \t]*\n(.*?)(?=^\[|\Z)',
        text,
    )
    if not m:
        return ""
    block = m.group(1)
    em = re.search(r'(?m)^[ \t]*email[ \t]*=[ \t]*"([^"]+)"', block)
    if not em or not em.group(1).strip():
        return ""
    email = em.group(1).strip()
    dn = re.search(r'(?m)^[ \t]*display-name[ \t]*=[ \t]*"([^"]+)"', block)
    name = dn.group(1).strip() if dn else ""
    return f"{name} <{email}>" if name else email


def email_send_tool(args: Dict[str, Any], **_kw) -> Dict[str, Any]:
    args = args if isinstance(args, dict) else {}

    fields = {}
    for key in ("to", "cc", "bcc"):
        pieces = _address_pieces(args.get(key))
        if pieces is None:
            return {"error": f"'{key}' must be an address or a list of addresses."}
        fields[key] = pieces
    subject = (args.get("subject") or "").strip()
    body = args.get("body") or ""
    html = args.get("html") or ""
    # The intent as the caller stated it (before the org signature is appended):
    # what the dedup key hashes.
    raw_body, raw_html = body, html
    attachments = _as_list(args.get("attachments"))
    account = (args.get("account") or "fleet").strip()
    draft = bool(args.get("draft"))

    if not fields["to"]:
        return {"error": "No recipient. Pass 'to' as an address or list of addresses."}
    bad = [a for key in ("to", "cc", "bcc") for a in fields[key] if _bare_address(a) is None]
    if bad:
        return {"error": (
            f"These do not look like email addresses: {', '.join(bad)}. Pass "
            "plain addresses such as person@example.com."
        )}
    # From here on every recipient is its bare address: that is what the
    # headers carry, and what the result and summary report.
    to, cc, bcc = ([_bare_address(a) for a in fields[key]] for key in ("to", "cc", "bcc"))
    if not subject:
        return {"error": "No subject. An email without one reads as spam."}

    # An explicit `from` comes from the model, so it is held to a bare address
    # or a plain ASCII "Name <address>" on one line (see _explicit_from).
    sender = ""
    raw_from = args.get("from")
    if not (raw_from is None or (isinstance(raw_from, str) and not raw_from.strip())):
        sender = _explicit_from(raw_from) or ""
        if not sender:
            return {"error": (
                "'from' must be a bare address (fleet-001@lucaryin.com) or a "
                "plain ASCII name and address (Lucaryin Fleet "
                "<fleet-001@lucaryin.com>) with no line breaks. Omit it to "
                "send as the account's own identity."
            )}

    binary = _himalaya()
    if not binary:
        return {
            "error": "himalaya is not installed or not on PATH — cannot send mail from "
                     "this machine."
        }

    # Resolve the From address from the account himalaya is configured with, so
    # the envelope matches the credentials actually used. An explicit `from`
    # wins; otherwise read the account's own identity out of the himalaya config
    # (works for gmail/zoho/any account, not just fleet). Fleet keeps a hardcoded
    # fallback so it still sends if the config couldn't be read.
    if not sender:
        sender = _account_from(account)
    if not sender and account == "fleet":
        sender = "Lucaryin Fleet <fleet-001@lucaryin.com>"
    if not sender:
        # No sender identity for this account → himalaya would otherwise reject
        # the send with the opaque "cannot send message without a sender". Fail
        # with an actionable message the agent can relay, and point at the
        # account that is known-good.
        return {"error": (
            f"The '{account}' mail account has no sender identity configured on "
            f"this machine, so mail can't be sent from it yet. Set it up under "
            f"Settings → Connectors, pass an explicit From address, or send "
            f"from the 'fleet' account (which is configured)."
        )}

    # Org-wide signature: append to BOTH parts unless the caller opted out or the
    # body already contains it. RFC 3676 "-- \n" delimiter on the text part. The
    # html footer is injected before </body> (or appended for a fragment); it is
    # only touched when an html part exists, so a text-only send stays text/plain.
    if args.get("signature", True):
        sig_txt, sig_html = _org_signature()
        if sig_txt and sig_txt not in body:
            body = body.rstrip() + "\n\n-- \n" + sig_txt
        if html and sig_html and sig_html not in html:
            low = html.lower()
            if "</body>" in low:
                idx = low.rindex("</body>")
                html = html[:idx] + sig_html + html[idx:]
            else:
                html = html + sig_html

    try:
        msg = _build_message(sender or "", to, cc, bcc, subject, body, attachments, html=html)
    except (FileNotFoundError, ValueError) as e:
        return {"error": str(e)}

    # draft:true saves to the account's Drafts folder instead of sending —
    # the user reviews and presses send in their own mail client. Addressed
    # and validated identically, so "turn this draft into a send" is only a
    # flag flip away.
    force = bool(args.get("force"))
    idem = _idem_key(to, cc, subject, bcc=bcc, body=raw_body, html=raw_html, attachments=attachments)
    reservation = None
    if draft:
        cmd = [binary, "message", "save", "-a", account, "--folder", "Drafts"]
        verb = "Saving the draft"
    else:
        # Reserve BEFORE sending (see the ledger note above). force=true
        # bypasses for a deliberate second copy.
        if not force:
            decision, prior = _reserve_send(
                idem, _thread_key(to, cc, subject, bcc=bcc), unattended=_is_unattended_send())
            if decision == "skip":
                return _dedup_result((prior or {}).get("match"), to, subject)
            reservation = prior  # decision == "go": the reservation to commit / release
        # Dry-run: exercise the full gate/dedup/ledger path end to end but never
        # hand bytes to himalaya (tests + the dry-run harness). Records the send
        # so idempotency is exercised.
        if os.environ.get("HERMES_EMAIL_DRYRUN"):
            if reservation:
                _commit_send(reservation, f"[dry-run] Sent “{subject}” to {', '.join(to)}.")
            return {
                "sent": True, "dry_run": True,
                "to": to, "cc": cc, "subject": subject, "account": account,
                "summary": f"[dry-run] Would send “{subject}” to {', '.join(to)} — no mail left the machine.",
            }
        cmd = [binary, "message", "send", "-a", account]
        verb = "Sending"

    try:
        proc = subprocess.run(
            cmd,
            input=msg.as_bytes(),
            capture_output=True,
            timeout=SEND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Uncertain outcome — DON'T release the reservation (a re-send could
        # duplicate a message that actually went through); the window expires it.
        return {"error": f"{verb} timed out after {SEND_TIMEOUT_S}s — the message may "
                         f"or may not have gone through. Check the "
                         f"{'Drafts' if draft else 'Sent'} folder before retrying."}
    except Exception as e:  # noqa: BLE001 — surface the real reason
        if reservation:
            _release_send(reservation)  # definitively did not send → free the reservation
        return {"error": f"Could not run himalaya: {e}"}

    if proc.returncode != 0:
        if reservation:
            _release_send(reservation)  # definitively did not send → allow a retry
        detail = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip()
        return {"error": f"{verb} failed: {detail[:400] or 'himalaya exited non-zero'}"}

    if draft:
        return {
            "drafted": True,
            "sent": False,
            "to": to,
            "cc": cc,
            "subject": subject,
            "attachments": [os.path.basename(a) for a in attachments],
            "account": account,
            "summary": (
                f"Saved “{subject}” to the {account} Drafts folder, addressed to "
                f"{', '.join(to)} — nothing was sent; the user reviews and sends it."
            ),
        }

    recipients = len(to) + len(cc) + len(bcc)
    summary = (
        f"Sent “{subject}” to {', '.join(to)}"
        + (f" (cc {', '.join(cc)})" if cc else "")
        + (f" with {len(attachments)} attachment(s)" if attachments else "")
        + f" — {recipients} recipient(s) total."
    )
    if reservation:
        _commit_send(reservation, summary)  # confirmed sent → block equivalents in-window
    return {
        "sent": True,
        "to": to,
        "cc": cc,
        "subject": subject,
        "attachments": [os.path.basename(a) for a in attachments],
        "account": account,
        "summary": summary,
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
                "description": (
                    "Recipient address(es). A string separated by commas, "
                    "semicolons or line breaks is also accepted. Headers carry "
                    "the bare addresses only; display names are dropped."
                ),
            },
            "subject": {"type": "string", "description": "Subject line."},
            "body": {"type": "string", "description": "Plain-text body of the message."},
            "html": {
                "type": "string",
                "description": (
                    "Optional HTML body. When given, the message is sent as "
                    "multipart/alternative: 'body' is the plain-text fallback and "
                    "this is what HTML-capable clients render. Use email-safe HTML "
                    "(inline styles, table layout) — mail clients strip <style> "
                    "blocks, external CSS, and web-only features."
                ),
            },
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
                "description": (
                    "Optional explicit From header: a bare address or a plain "
                    "ASCII name and address, e.g. 'Lucaryin Fleet "
                    "<fleet-001@lucaryin.com>'. Omit to use the account's identity."
                ),
            },
            "draft": {
                "type": "boolean",
                "description": (
                    "When true, save the fully addressed message to the "
                    "account's Drafts folder instead of sending — the user "
                    "reviews and sends it from their own mail client. Use "
                    "this when the user says 'draft it', wants to review "
                    "wording first, or the send feels consequential."
                ),
            },
            "force": {
                "type": "boolean",
                "description": (
                    "Send a DELIBERATE second copy. Normally a send is "
                    "de-duplicated: when the identical message (same recipients, "
                    "subject, body and attachments) went out in the last ~45 "
                    "minutes the result is sent:true, already_sent:true (it was "
                    "delivered earlier; nothing new went out), and when a "
                    "scheduled job is involved and an email with the same "
                    "subject already went to the same recipients the result is "
                    "sent:false, held:true (this version did NOT go out) — so "
                    "retries and background jobs never double-mail. A DIFFERENT "
                    "message to the same people in a conversation (a reply, a "
                    "corrected body, a new bcc) is sent normally. Set force only "
                    "when the user explicitly asks to resend."
                ),
            },
            "signature": {
                "type": "boolean",
                "description": (
                    "Append the org email signature to the message (default "
                    "true). Set false only for machine-to-machine mail or when "
                    "the body already contains the signature."
                ),
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
