"""Lucaryin fold (patch 0030): customer-facing failure copy without runtime or vendor branding.

Why: upstream v2026.9.21 moved failed-turn and cron-failure chat copy into tables that name the
runtime ("Hermes hit repeated errors…"), the model vendor (``provider_label_for`` → "DeepSeek"),
the model id, and commands a Lucaryin user cannot run (`hermes doctor` / `hermes setup` /
`hermes fallback add` / `hermes cron …`, /model, /reasoning). The Lucaryin bridge emits a failed
turn's ``final_response`` verbatim when nothing streamed (lucaryin-ai hermes-bridge/worker.py),
and cron failure notices are appended to the customer's chat (patch 0023), so all of it reached
customers: it broke the founder lock (customer-facing Hermes/Nous branding neutralized), named
DeepSeek (absent from the public privacy policy) and nudged users toward a non-DeepSeek backup
provider. A hermetic HTTP 503 showed it end to end: base runtime "API call failed after 1
retries: HTTP 503: Service overloaded"; v2026.9.21 "DeepSeek reported it was overloaded on all 1
attempts … add a backup provider with `hermes fallback add`".

How: patch 0030 appends ONE call at the end of each upstream copy module, so every importer
(``from agent.turn_failure_copy import site_copy`` runs after the module body) binds the neutral
tables and functions defined here:

  agent/turn_failure_copy.py          -> neutralize_turn_failure_copy(globals())
  agent/turn_explainers.py            -> neutralize_turn_explainers(globals())
  agent/thinking_timeout_guidance.py  -> neutralize_thinking_timeout_guidance(globals())
  cron/scheduler_failure_copy.py      -> neutralize_cron_failure_copy(globals())

Keeping the copy here instead of rewriting upstream's strings in place keeps the fold to a few
stable lines at the end of each file: upstream re-words these tables every hop (v2026.9.24 already
changes ``provider_policy_blocked``), and an in-place patch would need a hand re-fit each time.

Rules for every string here: no runtime, vendor or model names (the provider label and the model
id are never interpolated), no CLI commands, no config paths or keys; only next steps a Lucaryin
user can take in chat (/retry, /new, /compress, ``continue``, or asking the agent). Raw provider or
exception detail still rides a trailing "Details:" line, as the base runtime's copy did.

Upstream drift is fail-safe: a table entry upstream adds later keeps upstream's text only when it
passes :data:`BANNED`; otherwise it gets a generic neutral sentence and its key is recorded in the
patched module's ``LUCARYIN_UNCOVERED``. runtime-addons/tests/agent/test_lucaryin_neutral_copy.py
fails while that set is non-empty, so the next hop writes real copy for it instead of shipping
the generic one silently.

This module must not import the modules it neutralizes at import time (it runs inside their
module body); everything it needs from them arrives through ``ns``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Set, Tuple

#: What customer copy must never contain: the runtime/vendor names, CLI commands, slash commands
#: Lucaryin chat does not offer, config paths/keys, and placeholders that would interpolate the
#: provider label, the model id or a Hermes path.
BANNED = re.compile(
    r"hermes|\bnous\b|deepseek|fallback add|/model\b|/reasoning\b|/login\b|"
    r"\{(?:label|model|home|relogin|profile_arg|prefix_hint|resume|db_path|backups_dir|recovery_docs)\}|"
    r"config\.yaml|agent\.log|extra_headers|custom_providers|max_tokens|max_iterations",
    re.IGNORECASE,
)

#: The neutral name for the model vendor (upstream's own FAILURE_CAUSE_GLOSS wording).
SERVICE = "the AI model service"
_SERVICE_CAP = "The AI model service"

#: Fallback for a table entry upstream adds whose text fails BANNED (see module docstring).
GENERIC_TURN_FAILURE = (
    "Something went wrong on my side, so I stopped this turn. Send /retry to try again, or start "
    "a new session with /new."
)

_RETRY = "Wait a minute and send /retry."
_LOOP_NEXT = "Your message is saved. Send `continue` to try again, or start a new session with /new."
_SHRINK_NEXT = "Start a new session with /new (your history is kept), or try /compress once more."


def find_banned(text: Any) -> list:
    """Every BANNED match in ``text`` (empty list for clean text or a non-string)."""
    if not isinstance(text, str):
        return []
    return [m.group(0) for m in BANNED.finditer(text)]


def _uncovered(ns: MutableMapping[str, Any]) -> Set[str]:
    found = ns.get("LUCARYIN_UNCOVERED")
    if not isinstance(found, set):
        found = set()
        ns["LUCARYIN_UNCOVERED"] = found
    return found


def _merge(table: str, upstream: Any, ours: Mapping[str, str], uncovered: Set[str],
           generic: str) -> Dict[str, str]:
    """Upstream's keys, each mapped to our copy, upstream's own text when it is already clean, or
    ``generic`` (recorded in ``uncovered``). Our keys upstream does not have are kept too, so a
    key that comes back in a later hop is already worded."""
    out: Dict[str, str] = {}
    for key, text in dict(upstream or {}).items():
        if key in ours:
            out[key] = ours[key]
        elif isinstance(text, str) and not find_banned(text):
            out[key] = text
        else:
            out[key] = generic
            uncovered.add(f"{table}:{key}")
    for key, text in ours.items():
        out.setdefault(key, text)
    return out


class _Blank(dict):
    """``format_map`` mapping: a placeholder this module does not supply renders empty."""

    def __missing__(self, key: str) -> str:
        return ""


def _tries(attempts: Any) -> str:
    try:
        n = int(attempts)
    except (TypeError, ValueError):
        return "after several attempts"
    return "after 1 attempt" if n == 1 else f"after {n} attempts"


# ── agent/turn_failure_copy.py ──────────────────────────────────────────────────────────────

#: ``_FAILURE_CODE_COPY`` (site codes a loop site renders itself).
TURN_FAILURE_CODE_COPY: Dict[str, str] = {
    "context_overflow": (
        "This conversation has grown too long for the model to read, and I couldn't shrink it "
        "enough automatically. " + _SHRINK_NEXT
    ),
    "truncated": (
        "The model's reply was cut off before it finished (it hit its output length limit), so "
        "I didn't run the incomplete action. Nothing was changed. Send `continue`, or ask for the "
        "work in smaller steps."
    ),
    "invalid_response": (
        _SERVICE_CAP + " sent back an empty or broken reply {attempts} times — it is probably "
        "overloaded right now. " + _RETRY + "\n\nDetails: {detail}"
    ),
    "loop_error": (
        "I hit repeated errors and stopped this turn so I wouldn't keep retrying. " + _LOOP_NEXT
        + "\n\nDetails: {detail}"
    ),
    "interpreter_shutdown": (
        "I was shutting down and stopped this turn. Your conversation is saved — send your "
        "message again in a moment."
    ),
}

#: ``_ONE_OFF_COPY`` (deterministic loop outcomes that are not failure codes).
TURN_ONE_OFF_COPY: Dict[str, str] = {
    "payload_too_large": (
        "This conversation (including attachments) has grown too large to send to the model, "
        "and I couldn't shrink it enough automatically. " + _SHRINK_NEXT
    ),
    "compression_disabled": (
        "This conversation is too long for the model, and automatic shrinking is turned off. "
        "Send /compress to shrink it now, or /new to start fresh."
    ),
    "server_context_rejection": (
        _SERVICE_CAP + " rejected this request as too large, even though this conversation fits "
        "the model's context window, so shrinking it would not help. The service was probably "
        "busy with another request. Wait a moment and send /retry."
    ),
    "stream_dropped_tool_call": (
        "The connection to " + SERVICE + " kept dropping while the model was writing a large "
        "action, so nothing was run. Send /retry; asking for the work in smaller pieces also helps."
    ),
    "local_processing_error": (
        "I hit an internal error while handling the model's reply and stopped this turn. "
        + _LOOP_NEXT + "\n\nDetails: {detail}"
    ),
    "reasoning_only": (
        "⚠️ The model spent all of its output budget thinking and never wrote an answer. Send "
        "/retry, or ask again more simply. Its last thoughts, which may contain the answer:"
        "\n\n{preview}"
    ),
    "max_iterations_no_summary": (
        "I ran out of steps for this turn ({limit} tool calls) before finishing, and couldn't "
        "produce a summary. Send `continue` to keep going."
    ),
    "nous_rate_limit": "Wait for the usage limit to reset, then send /retry.",
}

#: ``_EXHAUSTED_LEADS`` keyed by classifier reason; ``{tries}`` = "after N attempts".
TURN_EXHAUSTED_LEADS: Dict[str, str] = {
    "rate_limit": _SERVICE_CAP + " rate-limited every request {tries}",
    "upstream_rate_limit": _SERVICE_CAP + " rate-limited every request {tries}",
    "overloaded": _SERVICE_CAP + " reported it was overloaded {tries}",
    "server_error": _SERVICE_CAP + " returned a server error {tries}",
    "timeout": _SERVICE_CAP + " didn't respond in time {tries}",
}
TURN_EXHAUSTED_DEFAULT_LEAD = _SERVICE_CAP + " didn't answer {tries}"

#: ``_NONRETRYABLE_COPY`` keyed by classifier reason.
TURN_NONRETRYABLE_COPY: Dict[str, str] = {
    "model_not_found": (
        "The model I use isn't available from " + SERVICE + " right now, so I couldn't answer."
    ),
    "format_error": (
        _SERVICE_CAP + " rejected this request as malformed, so the model didn't answer. Start a "
        "clean session with /new."
    ),
    "role_alternation": (
        _SERVICE_CAP + " rejected the shape of this conversation, so the model didn't answer. "
        "Start a clean session with /new."
    ),
    "ssl_cert_verification": (
        "I couldn't verify the security certificate of " + SERVICE + ", so the connection was "
        "refused. This is usually a network proxy or an outdated certificate store on this "
        "computer."
    ),
    "provider_policy_blocked": (
        _SERVICE_CAP + " refused this request because of a policy on the account, so the model "
        "didn't answer and retrying won't help."
    ),
    "upstream_blocked": (
        "A firewall or proxy in front of " + SERVICE + " blocked the request before it reached "
        "the model. Check this computer's network, then send /retry."
    ),
}
TURN_NONRETRYABLE_DEFAULT_COPY = (
    _SERVICE_CAP + " rejected the request and retrying won't help. Try rewording your message, or "
    "start a new session with /new."
)
#: ``_AUTH_COPY`` keyed by ``auth_kind`` (OAuth vs API key — the same next step for a user).
TURN_AUTH_COPY: Dict[str, str] = {
    "oauth": (
        _SERVICE_CAP + " rejected this computer's credentials, so the model can't be reached "
        "right now. The credentials need to be refreshed before I can answer."
    ),
    "api_key": (
        _SERVICE_CAP + " rejected this computer's credentials, so the model can't be reached "
        "right now. The credentials need to be refreshed before I can answer."
    ),
}
TURN_CONTENT_POLICY_NEXT_STEPS = "Try rewording your message or removing sensitive attachments."


def neutralize_turn_failure_copy(ns: MutableMapping[str, Any]) -> None:
    """Rebind ``agent/turn_failure_copy.py``'s copy tables and copy builders (patch 0030)."""
    uncovered = _uncovered(ns)
    ns["_NEXT_STEPS_RETRY"] = _RETRY
    ns["_NEXT_STEPS_LOOP"] = _LOOP_NEXT
    ns["CONTENT_POLICY_NEXT_STEPS"] = TURN_CONTENT_POLICY_NEXT_STEPS

    failure_code = _merge("_FAILURE_CODE_COPY", ns.get("_FAILURE_CODE_COPY"), TURN_FAILURE_CODE_COPY,
                          uncovered, GENERIC_TURN_FAILURE)
    one_off = _merge("_ONE_OFF_COPY", ns.get("_ONE_OFF_COPY"), TURN_ONE_OFF_COPY, uncovered,
                     GENERIC_TURN_FAILURE)
    ns["_FAILURE_CODE_COPY"] = failure_code
    ns["_ONE_OFF_COPY"] = one_off
    ns["_SITE_COPY"] = {**failure_code, **one_off}

    leads = _merge("_EXHAUSTED_LEADS", ns.get("_EXHAUSTED_LEADS"), TURN_EXHAUSTED_LEADS, uncovered,
                   TURN_EXHAUSTED_DEFAULT_LEAD)
    nonretryable = _merge("_NONRETRYABLE_COPY", ns.get("_NONRETRYABLE_COPY"), TURN_NONRETRYABLE_COPY,
                          uncovered, TURN_NONRETRYABLE_DEFAULT_COPY)
    auth = _merge("_AUTH_COPY", ns.get("_AUTH_COPY"), TURN_AUTH_COPY, uncovered, TURN_AUTH_COPY["api_key"])
    ns["_EXHAUSTED_LEADS"] = leads
    ns["_EXHAUSTED_DEFAULT_LEAD"] = TURN_EXHAUSTED_DEFAULT_LEAD
    ns["_NONRETRYABLE_COPY"] = nonretryable
    ns["_NONRETRYABLE_DEFAULT_COPY"] = TURN_NONRETRYABLE_DEFAULT_COPY
    ns["_AUTH_COPY"] = auth

    # The one gloss table cron, subagent and chat notices share is already neutral upstream
    # ("the AI model service …"); a branded entry added later is caught like any other.
    gloss = ns.get("FAILURE_CAUSE_GLOSS")
    if isinstance(gloss, dict):
        for key, text in list(gloss.items()):
            if find_banned(text):
                gloss[key] = "something went wrong at " + SERVICE
                uncovered.add(f"FAILURE_CAUSE_GLOSS:{key}")

    def exhausted_copy(reason: str, *, label: str = "", attempts: int = 0, summary: str = "",
                       reset_seconds: Optional[float] = None, **_future: Any) -> str:
        """Retries exhausted (``max_retries_exhausted_result``). ``label`` (the provider's name) is
        accepted for signature compatibility and never shown."""
        lead = leads.get(str(reason), TURN_EXHAUSTED_DEFAULT_LEAD).format_map(_Blank(tries=_tries(attempts)))
        if reset_seconds is not None and reset_seconds >= 120:
            from agent.retry_utils import format_reset_window

            situation = (f"its usage limit resets in {format_reset_window(reset_seconds)}. "
                         "Send /retry after that.")
        else:
            situation = f"it looks temporarily unavailable. {_RETRY}"
        return f"{lead} — {situation}\n\nDetails: {summary}"

    def nonretryable_copy(classified: Any, *, provider: Any = None, model: Any = None, summary: str = "",
                          prefix_suggestion: Optional[str] = None, **_future: Any) -> str:
        """A terminal non-retryable rejection (auth, model missing, TLS, generic 4xx). The
        provider, the model id and the vendor-prefix suggestion are never shown."""
        if getattr(classified, "is_auth", False):
            body = TURN_AUTH_COPY["api_key"]
        else:
            reason = getattr(getattr(classified, "reason", None), "value", None)
            body = nonretryable.get(str(reason), TURN_NONRETRYABLE_DEFAULT_COPY)
        return f"{body}\n\nDetails: {summary}"

    def content_policy_copy(*, label: str = "", summary: str = "", **_future: Any) -> str:
        return (
            f"The safety filter at {SERVICE} refused this request, so the model didn't answer. "
            f"{TURN_CONTENT_POLICY_NEXT_STEPS}\n\nDetails: {summary}"
        )

    for fn in (exhausted_copy, nonretryable_copy, content_policy_copy):
        fn.__module__ = ns.get("__name__", fn.__module__)
        fn.__lucaryin_neutral__ = True  # type: ignore[attr-defined]
        ns[fn.__name__] = fn


# ── agent/turn_explainers.py ────────────────────────────────────────────────────────────────

#: ``EMPTY_RESPONSE_EXPLANATION`` — upstream fills ``{model}`` with the model id; ours names none.
EMPTY_RESPONSE_EXPLANATION = (
    "The model didn't produce a reply this time, even after retries. Send `continue` to try again."
)

#: ``_EXIT_REASON_EXPLANATIONS`` entries whose upstream text names a model id or a step a
#: Lucaryin user cannot take (switch model/provider, raise a config limit).
EXIT_REASON_EXPLANATIONS: Dict[str, str] = {
    "empty_response_exhausted": EMPTY_RESPONSE_EXPLANATION,
    "all_retries_exhausted_no_response": (
        SERVICE + " didn't answer after all retries. Send /retry to try again."
    ),
    "rebuilt_restart_limit_exceeded": (
        SERVICE + " kept failing, so the turn stopped instead of retrying forever. Send "
        "`continue` to try again."
    ),
}
EXIT_REASON_PREFIX_EXPLANATIONS: Dict[str, str] = {
    "max_iterations_reached": (
        "the maximum number of iterations (tool steps) for one turn was reached before a final "
        "answer. Send `continue` to keep going."
    ),
}

#: ``_PERSISTENCE_CAUSE_EXPLANATIONS`` (``session_persistence_failed`` by classified cause).
PERSISTENCE_CAUSE_EXPLANATIONS: Dict[str, str] = {
    "turn_lease": (
        "the turn was stopped because another session took over this conversation. Your reply "
        "was not saved — wait a moment, then send your message again."
    ),
    "locked": (
        "the turn was stopped because conversation storage was busy. Your message should already "
        "be saved — please send it again in a moment."
    ),
    "replaced": (
        "the conversation database file was replaced while I was running, so this message was not "
        "saved (a copy was kept). Restart the app, then send your message again."
    ),
    "deleted_wal": (
        "another process still holds an old copy of the conversation database, so I stopped "
        "writing to keep the file safe and this message was not saved (a copy was kept). Nothing "
        "is lost. Restart the app, then send your message again."
    ),
    "corrupt": (
        "the turn was stopped because the conversation database reported structural damage, so "
        "this message was not saved. Freeing disk space will not help. Restart the app; if this "
        "keeps happening, the database has to be repaired before you send your message again."
    ),
    "fts_index": (
        "the turn was stopped because the conversation search index is damaged, so this message "
        "was not saved. The messages themselves are intact. Restart the app (it repairs the index "
        "when it opens), then send your message again."
    ),
    "disk": (
        "I couldn't save this conversation to disk, so I stopped rather than lose your messages. "
        "The disk is probably full: free some space, then send your message again."
    ),
}
PERSISTENCE_DEFAULT_EXPLANATION = (
    "I couldn't save this conversation, so I stopped rather than lose your messages. The drive may "
    "be out of room, or another process may be holding the conversation database. Free some space "
    "or restart the app, then send your message again."
)


def neutralize_turn_explainers(ns: MutableMapping[str, Any]) -> None:
    """Rebind ``agent/turn_explainers.py``'s user-facing explanation tables (patch 0030)."""
    uncovered = _uncovered(ns)
    ns["EMPTY_RESPONSE_EXPLANATION"] = EMPTY_RESPONSE_EXPLANATION
    ns["_EXIT_REASON_EXPLANATIONS"] = _merge(
        "_EXIT_REASON_EXPLANATIONS", ns.get("_EXIT_REASON_EXPLANATIONS"), EXIT_REASON_EXPLANATIONS,
        uncovered, GENERIC_TURN_FAILURE)
    prefixed: list = []
    seen: Set[str] = set()
    for prefix, text in tuple(ns.get("_EXIT_REASON_PREFIX_EXPLANATIONS") or ()):
        seen.add(prefix)
        if prefix in EXIT_REASON_PREFIX_EXPLANATIONS:
            prefixed.append((prefix, EXIT_REASON_PREFIX_EXPLANATIONS[prefix]))
        elif find_banned(text):
            prefixed.append((prefix, GENERIC_TURN_FAILURE))
            uncovered.add(f"_EXIT_REASON_PREFIX_EXPLANATIONS:{prefix}")
        else:
            prefixed.append((prefix, text))
    prefixed.extend((p, t) for p, t in EXIT_REASON_PREFIX_EXPLANATIONS.items() if p not in seen)
    ns["_EXIT_REASON_PREFIX_EXPLANATIONS"] = tuple(prefixed)
    ns["_PERSISTENCE_CAUSE_EXPLANATIONS"] = _merge(
        "_PERSISTENCE_CAUSE_EXPLANATIONS", ns.get("_PERSISTENCE_CAUSE_EXPLANATIONS"),
        PERSISTENCE_CAUSE_EXPLANATIONS, uncovered, PERSISTENCE_DEFAULT_EXPLANATION)
    ns["_PERSISTENCE_DEFAULT_EXPLANATION"] = PERSISTENCE_DEFAULT_EXPLANATION


# ── agent/thinking_timeout_guidance.py ──────────────────────────────────────────────────────

THINKING_TIMEOUT_GUIDANCE = (
    "The model was thinking for so long that the connection to " + SERVICE + " timed out before "
    "it wrote anything. Send /retry, or ask for the work in smaller steps."
)


def neutralize_thinking_timeout_guidance(ns: MutableMapping[str, Any]) -> None:
    """Rebind ``build_thinking_timeout_guidance`` (appended to ``final_response`` when a reasoning
    model — every DeepSeek id is one — is idle-killed mid-think). Upstream's names the vendor list,
    /reasoning, /model and a config path."""

    def build_thinking_timeout_guidance(provider: str = "", model: str = "",
                                        model_label: Optional[str] = None, **_future: Any) -> str:
        return THINKING_TIMEOUT_GUIDANCE

    build_thinking_timeout_guidance.__module__ = ns.get("__name__", __name__)
    build_thinking_timeout_guidance.__lucaryin_neutral__ = True  # type: ignore[attr-defined]
    ns["build_thinking_timeout_guidance"] = build_thinking_timeout_guidance


# ── cron/scheduler_failure_copy.py ──────────────────────────────────────────────────────────

_NEXT_RUN = "It will run again at its next scheduled time"

#: ``_PROVIDER_FAILURE_ACTION`` keyed by classifier reason (what to do, in a Lucaryin chat:
#: the user asks the agent, which edits the job through cronjob_manage).
CRON_PROVIDER_FAILURE_ACTION: Dict[str, str] = {
    "billing": "The account at " + SERVICE + " needs attention. " + _NEXT_RUN + ".",
    "billing_unverified": "The account at " + SERVICE + " may need attention. " + _NEXT_RUN + ".",
    "auth": "The credentials on this computer need to be refreshed. " + _NEXT_RUN + ".",
    "auth_permanent": "The credentials on this computer need to be refreshed. " + _NEXT_RUN + ".",
    "model_not_found": _NEXT_RUN + ".",
    "provider_policy_blocked": "Retrying won't help until the account policy changes. " + _NEXT_RUN + ".",
    "upstream_blocked": "Check this computer's network or proxy settings. " + _NEXT_RUN + ".",
    "context_overflow": "Ask me to shorten the job's instructions. " + _NEXT_RUN + ".",
    "payload_too_large": "Ask me to shorten the job's instructions. " + _NEXT_RUN + ".",
    "content_policy_blocked": "Ask me to reword the job's instructions. " + _NEXT_RUN + ".",
}
CRON_DEFAULT_FAILURE_ACTION = _NEXT_RUN + "; ask me to run it now, change it, or pause it."
CRON_TRANSIENT_ACTION = _NEXT_RUN + "; ask me if you want it run sooner."
_CRON_TRANSIENT_DEFAULT = frozenset({"timeout", "rate_limit", "upstream_rate_limit", "overloaded", "server_error"})


def neutralize_cron_failure_copy(ns: MutableMapping[str, Any]) -> None:
    """Rebind ``cron/scheduler_failure_copy.py``'s notices (patch 0030). The notice prefix
    ``⚠️ Cron '<name>' failed:`` is kept byte-for-byte; the run-output path (under the Hermes
    home) and every `hermes cron …` command are dropped."""
    uncovered = _uncovered(ns)
    actions = _merge("_PROVIDER_FAILURE_ACTION", ns.get("_PROVIDER_FAILURE_ACTION"),
                     CRON_PROVIDER_FAILURE_ACTION, uncovered, CRON_DEFAULT_FAILURE_ACTION)
    ns["_PROVIDER_FAILURE_ACTION"] = actions
    ns["_DEFAULT_FAILURE_ACTION"] = CRON_DEFAULT_FAILURE_ACTION

    def _cause(reason: str) -> Optional[str]:
        from agent.turn_failure_copy import failure_cause_gloss

        return failure_cause_gloss(reason, subject="this job", possessive="the job's")

    def provider_failure_notice(job_name: str, job_id: str, reason: str, *,
                                backup_provider_phrase: str = "", provider: Any = None,
                                **_future: Any) -> Optional[str]:
        """Provider-shaped ``reason`` → notice, else None. ``backup_provider_phrase`` (upstream's
        "add one with `hermes fallback add`") and ``provider`` are accepted and never shown."""
        cause = _cause(reason)
        if cause is None:
            return None
        transient = ns.get("_TRANSIENT_REASONS") or _CRON_TRANSIENT_DEFAULT
        action = CRON_TRANSIENT_ACTION if reason in transient else actions.get(
            reason, CRON_DEFAULT_FAILURE_ACTION)
        return f"⚠️ Cron '{job_name}' failed: {cause}. {action}"

    def generic_failure_notice(job_name: str, job_id: str, cleaned_error: str, **_future: Any) -> str:
        return (
            f"⚠️ Cron '{job_name}' failed: {cleaned_error}. The full run output is saved. "
            f"{CRON_DEFAULT_FAILURE_ACTION}"
        )

    def script_timeout_notice(job_name: str, job_id: str, **_future: Any) -> str:
        return (
            f"⚠️ Cron '{job_name}' failed: its script timed out. No model was invoked. The "
            f"script's output is saved. {_NEXT_RUN}."
        )

    def inactivity_notice(job_name: str, job_id: str, **_future: Any) -> str:
        return (
            f"⚠️ Cron '{job_name}' failed: the job stalled — it stopped doing anything for too "
            f"long and was cut off. What it did so far is saved. {_NEXT_RUN}."
        )

    def blocked_config_notice(job_name: str, reason: str, **_future: Any) -> str:
        reason = str(reason or "").rstrip()
        if reason and reason[-1] not in ".!?":
            reason += "."
        return (
            f"⛔ Cron '{job_name}' did not run: {reason} Nothing was charged. It will try again at "
            "the next scheduled time, and this alert will not repeat."
        )

    for fn in (provider_failure_notice, generic_failure_notice, script_timeout_notice,
               inactivity_notice, blocked_config_notice):
        fn.__module__ = ns.get("__name__", fn.__module__)
        fn.__lucaryin_neutral__ = True  # type: ignore[attr-defined]
        ns[fn.__name__] = fn


#: Every (module, name) patch 0030 rebinds — the addon test checks each one is ours after import.
NEUTRALIZED: Tuple[Tuple[str, str], ...] = (
    ("agent.turn_failure_copy", "exhausted_copy"),
    ("agent.turn_failure_copy", "nonretryable_copy"),
    ("agent.turn_failure_copy", "content_policy_copy"),
    ("agent.thinking_timeout_guidance", "build_thinking_timeout_guidance"),
    ("cron.scheduler_failure_copy", "provider_failure_notice"),
    ("cron.scheduler_failure_copy", "generic_failure_notice"),
    ("cron.scheduler_failure_copy", "script_timeout_notice"),
    ("cron.scheduler_failure_copy", "inactivity_notice"),
    ("cron.scheduler_failure_copy", "blocked_config_notice"),
)


def iter_copy_templates(modules: Iterable[Any]) -> Iterable[Tuple[str, str]]:
    """(where, text) for every string in the neutralized tables of the given loaded modules —
    what the addon test scans with :data:`BANNED`."""
    names = (
        "_FAILURE_CODE_COPY", "_ONE_OFF_COPY", "_SITE_COPY", "_EXHAUSTED_LEADS", "_NONRETRYABLE_COPY",
        "_AUTH_COPY", "FAILURE_CAUSE_GLOSS", "_EXIT_REASON_EXPLANATIONS", "_PERSISTENCE_CAUSE_EXPLANATIONS",
        "_PROVIDER_FAILURE_ACTION",
    )
    scalars = (
        "_NEXT_STEPS_RETRY", "_NEXT_STEPS_LOOP", "CONTENT_POLICY_NEXT_STEPS", "_EXHAUSTED_DEFAULT_LEAD",
        "_NONRETRYABLE_DEFAULT_COPY", "EMPTY_RESPONSE_EXPLANATION", "_PERSISTENCE_DEFAULT_EXPLANATION",
        "_DEFAULT_FAILURE_ACTION", "FAILED_TURN_NOTICE", "PARTIAL_FAILED_TURN_NOTICE",
    )
    for mod in modules:
        mname = getattr(mod, "__name__", "?")
        for name in names:
            table = getattr(mod, name, None)
            if isinstance(table, dict):
                for key, text in table.items():
                    yield f"{mname}.{name}[{key}]", text
        for name in scalars:
            text = getattr(mod, name, None)
            if isinstance(text, str):
                yield f"{mname}.{name}", text
        for prefix, text in tuple(getattr(mod, "_EXIT_REASON_PREFIX_EXPLANATIONS", ()) or ()):
            yield f"{mname}._EXIT_REASON_PREFIX_EXPLANATIONS[{prefix}]", text
