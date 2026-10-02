"""Lucaryin job hygiene — the runtime half (runtime-patches/0037).

The rules live in the app bundle (lucaryin-ai hermes-bridge/cron_hygiene.py):
which fires are worth a model turn, what run context a job needs, which
sanctioned toolsets its prompt depends on, the heartbeat's budget, and what
reaches the owner's chat. This module is the scheduler's only door to them,
and it is FAIL-OPEN by contract: no bridge on this machine, an older bridge
without cron_hygiene, or any error at all means the job runs and delivers
exactly as it did before patch 0037.

Why it exists (Lucaryin local conversation review, Sep 20 - Oct 1 2026):
F36 the */30 heartbeat delivered 155 of 213 runs (55 overnight) and ran
unbudgeted to 54 minutes — the upstream rebase dropped the budget hand-off to
cron_gate; F44 polling jobs spent most runs on "nothing new"; F21 Team Huddle
had no ask_agent / fleet_send / board tools in its cron toolset and improvised
curl with the bridge bearer; F35 a run that finished its report but had one
step blocked delivered only "failed: needs_grant".

Hooks (cron/scheduler.py, patch 0037):
  _prepare_job_prompt    pre_run(job): skip (silent, no model call) or add
                         per-run context through the Run Context seam.
  _construct_cron_agent  toolsets(job, resolved): union in the job's
                         sanctioned extras (config denylist still wins) and
                         drop what the job must never have;
                         disabled_toolsets(job, disabled): add that to the
                         denylist too, so it holds when the resolved set is
                         the full default (None) or the job's own list.
  _install_cron_approval_gate  install_kwargs(): budget / job_id / job for
                         cron_gate.install, feature-detected.
  _compose_run_delivery  filter_delivery() on success; blocked_run_delivery()
                         on a needs_grant run that still has a report.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
from typing import Any, List, NamedTuple, Optional

logger = logging.getLogger(__name__)

# Toolsets the bridge may add to a scheduled job. Defense in depth: whatever
# the bridge returns, only these internal, per-call-gated sets get through.
ALLOWED_EXTRA_TOOLSETS = frozenset({"fleet", "board", "session_search", "file", "email"})

# Upper bound on the run context we inject (a bounded prompt is part of the
# point: the review found 20-45k-character assignment prompts).
MAX_CONTEXT_CHARS = 6000


class PreRun(NamedTuple):
    skip: bool = False
    reason: str = ""
    context: str = ""


def _bridge_candidates() -> List[str]:
    # Same order as patch 0024's cron_gate locator.
    return [
        os.environ.get("LUCARYIN_BRIDGE_DIR", ""),
        "/Applications/Lucaryin AI.app/Contents/Resources/app.asar.unpacked/hermes-bridge",
        os.path.expanduser("~/.lucaryin/hermes-bridge"),
    ]


def _hygiene() -> Optional[Any]:
    """The bridge's cron_hygiene module, or None (fail-open)."""
    try:
        for cand in _bridge_candidates():
            if cand and os.path.exists(os.path.join(cand, "cron_hygiene.py")):
                if cand not in sys.path:
                    sys.path.insert(0, cand)
                break
        else:
            return None
        import cron_hygiene  # noqa: PLC0415 — lives in the app bundle
        return cron_hygiene
    except Exception:
        logger.debug("cron hygiene unavailable", exc_info=True)
        return None


def _context_is_scannable(context: str, job: dict) -> bool:
    """The Run Context is scanned with the STRICT user-prompt set when a job
    has no skills or script; a false positive there would BLOCK the job. Our
    own text must never be the reason a job is blocked, so a context the
    scanner would refuse is dropped instead (and logged)."""
    try:
        from tools.cronjob_tools import _scan_cron_prompt
        err = _scan_cron_prompt(context)
    except Exception:
        return True
    if err:
        logger.warning("Job '%s': hygiene context dropped — the prompt scanner refuses it (%s)",
                       job.get("name") or job.get("id"), err)
        return False
    return True


def pre_run(job: dict) -> PreRun:
    mod = _hygiene()
    if mod is None or not isinstance(job, dict):
        return PreRun()
    try:
        raw = mod.pre_run(job) or {}
        skip = bool(raw.get("skip"))
        reason = str(raw.get("reason") or "")[:300]
        context = str(raw.get("context") or "")
        if len(context) > MAX_CONTEXT_CHARS:
            context = context[:MAX_CONTEXT_CHARS].rsplit("\n", 1)[0]
        if context and not _context_is_scannable(context, job):
            context = ""
        return PreRun(skip=skip, reason=reason, context=context)
    except Exception:
        logger.warning("cron hygiene pre_run failed; running the job as before", exc_info=True)
        return PreRun()


def _withheld(mod: Any, job: dict) -> List[str]:
    """Toolsets this job must never have (the Team Huddle's session_search),
    from a bridge that knows (an older bridge has no withheld_toolsets: [])."""
    fn = getattr(mod, "withheld_toolsets", None)
    if fn is None:
        return []
    try:
        return [t for t in (fn(job) or []) if isinstance(t, str) and t]
    except Exception:
        logger.warning("cron hygiene withheld toolsets failed", exc_info=True)
        return []


def toolsets(job: dict, resolved: Optional[List[str]]) -> Optional[List[str]]:
    """``resolved`` plus the job's sanctioned extras, minus what the job must
    never have (even when its own enabled_toolsets or the cron platform
    config list it). None (the full default set) stays None: there is
    nothing to add to everything, and disabled_toolsets() withholds from it."""
    if resolved is None:
        return None
    mod = _hygiene()
    if mod is None:
        return resolved
    try:
        extras = [t for t in (mod.extra_toolsets(job) or [])
                  if isinstance(t, str) and t in ALLOWED_EXTRA_TOOLSETS]
    except Exception:
        logger.warning("cron hygiene toolsets failed; using the resolved set", exc_info=True)
        return resolved
    withheld = _withheld(mod, job)
    out = [t for t in resolved if t not in withheld]
    for t in extras:
        if t not in out and t not in withheld:
            out.append(t)
    if out != list(resolved):
        logger.info("Job '%s': toolsets added %s, withheld %s",
                    job.get("name") or job.get("id"),
                    [t for t in out if t not in resolved],
                    [t for t in resolved if t not in out])
    return out


def disabled_toolsets(job: dict, disabled: List[str]) -> List[str]:
    """The cron denylist plus what this job must never have. The denylist
    wins over every enabled list (upstream _resolve_cron_disabled_toolsets),
    so this holds for the full default set and for the job's own
    enabled_toolsets alike. Fail-open: the denylist unchanged."""
    mod = _hygiene()
    if mod is None or not isinstance(job, dict):
        return disabled
    out = list(disabled or [])
    for t in _withheld(mod, job):
        if t not in out:
            out.append(t)
    return out


def install_kwargs(install_fn: Any, job: dict) -> dict:
    """Extra kwargs for cron_gate.install that this bridge's signature accepts:
    the heartbeat ``budget`` (dropped by the upstream rebase), ``job_id``
    (per-job grant ledgers, R2-1-75) and ``job`` (the hygiene kind)."""
    try:
        params = inspect.signature(install_fn).parameters
    except (TypeError, ValueError):
        return {}
    out: dict = {}
    if "job_id" in params and job.get("id"):
        out["job_id"] = str(job.get("id"))
    if "job" in params:
        out["job"] = job
    if "budget" in params:
        mod = _hygiene()
        if mod is not None:
            try:
                budget = mod.budget_for(job)
            except Exception:
                logger.warning("cron hygiene budget failed", exc_info=True)
                budget = None
            if budget is not None:
                out["budget"] = budget
    return out


def filter_delivery(job: dict, text: str) -> str:
    mod = _hygiene()
    if mod is None or not isinstance(text, str):
        return text
    try:
        out = mod.filter_delivery(job, text)
        return out if isinstance(out, str) else text
    except Exception:
        logger.warning("cron hygiene filter failed; delivering unchanged", exc_info=True)
        return text


def blocked_run_delivery(job: dict, error: Any, final_response: str) -> Optional[str]:
    mod = _hygiene()
    if mod is None:
        return None
    try:
        out = mod.blocked_run_delivery(job, error, final_response)
        return out if isinstance(out, str) and out.strip() else None
    except Exception:
        logger.warning("cron hygiene blocked-run delivery failed", exc_info=True)
        return None
