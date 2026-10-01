"""Lucaryin fork addon: the DeepSeek-only model lock (runtime-patches 0028).

Founder rule (2026-07-26): DeepSeek models only — no other model provider.

What broke it (review F04, Sep 20-26): thoth's config.yaml carried
``fallback_model: anthropic/claude-opus-4-6`` and the app provisioned an
Anthropic key, so a single DeepSeek request timeout made the cron scheduler's
fallback chain (``get_fallback_chain``) switch Heartbeat, Meeting Recap, Inbox
Triage and Team Huddle runs — mail, calendar, iMessage and finance content — to
Anthropic, while ``sessions.model`` and ``cron/usage_audit.jsonl`` still said
deepseek-v4-pro. Nothing in the runtime checked the lock.

What this module does while the lock is ACTIVE (``LUCARYIN_MODEL_LOCK`` set to
anything but an explicit off value; the Lucaryin app and hermes-bridge set it to
``deepseek`` for every bridge, worker and cron tick):

* ``filter_fallback_chain`` — every fallback chain the runtime builds (config
  ``fallback_providers`` / ``fallback_model``, a chain handed to ``AIAgent``)
  keeps DeepSeek routes only. One WARNING per dropped route per process.
* ``refuse_fallback_entry`` — last-line check in the agent's fallback walk.
* ``guard_resolved_client`` — the central client router
  (``agent.auxiliary_client.resolve_provider_client``) returns ``(None, None)``
  for a client whose endpoint is not DeepSeek, the same "provider not
  configured" answer every caller already handles.
* ``enforce_agent_primary`` / ``enforce_runtime`` — an ``AIAgent`` or a cron
  job whose PRIMARY route is not DeepSeek refuses to start (RuntimeError with a
  clear ``MODEL-LOCK`` message): unattended runs fail closed.
* ``enforce_switch`` — a live ``/model`` switch to a non-DeepSeek route is
  refused (RuntimeError; ``switch_model``'s callers already report a failed
  switch and keep the current model).
* ``external_logins_allowed`` — the runtime never borrows another program's
  model login (Claude Code's keychain entry or ``~/.claude/.credentials.json``,
  the Codex CLI's ``~/.codex/auth.json``). Also honours
  ``auth.adopt_external_logins: false`` in config.yaml with the lock off — the
  key upstream reads from v2026.9.21, which the Lucaryin app pins on every
  profile.
* ``note_fail_closed`` — when DeepSeek fails and no allowed fallback is left,
  one clear WARNING per agent: the run fails instead of switching provider (the
  retry loop's own jittered backoff still applies before that).
* ``alert_if_violation`` — after every main-loop and auxiliary API call: one
  ``MODEL-LOCK VIOLATION`` ERROR line per (provider, model, host) per process if
  a call was ever served off DeepSeek. Detection behind the prevention above.

A route is DeepSeek when its base_url host is ``deepseek.com`` or a subdomain
(api.deepseek.com, including its Anthropic-compatible ``/anthropic`` path); a
route with no base_url is DeepSeek when its provider is ``deepseek``. A
``deepseek`` label on a foreign base_url is NOT DeepSeek.

Lock INACTIVE (variable unset — a stock install, upstream's own test suite):
every function is a pass-through, so upstream behaviour is unchanged (the one
addition: an explicit ``auth.adopt_external_logins: false`` is honoured).

Every function here is total: it never raises except the three ``enforce_*``
refusals, which are the point.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("lucaryin.model_lock")

LOCK_ENV = "LUCARYIN_MODEL_LOCK"
LOCKED_PROVIDER = "deepseek"
ALERT_PREFIX = "MODEL-LOCK"
_OFF_VALUES = frozenset({"", "0", "off", "false", "no", "none", "disabled"})
_ALLOWED_HOST = "deepseek.com"

_seen_lock = threading.Lock()
_seen: set = set()


def lock_active() -> bool:
    """True while the DeepSeek-only lock is on (read per call, never cached:
    hermes-bridge sets it in main() after the runtime may have been imported)."""
    return os.environ.get(LOCK_ENV, "").strip().lower() not in _OFF_VALUES


def _host(base_url: Any) -> str:
    text = str(base_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text if "://" in text else f"https://{text}")
        return (parsed.hostname or "").lower()
    except Exception:
        return ""


def is_deepseek_host(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    return host == _ALLOWED_HOST or host.endswith("." + _ALLOWED_HOST)


def route_allowed(provider: Any, base_url: Any = None) -> bool:
    """Whether a (provider, base_url) route is DeepSeek (see module docstring)."""
    if str(base_url or "").strip():
        return is_deepseek_host(_host(base_url))
    return str(provider or "").strip().lower() == LOCKED_PROVIDER


def _once(key: Tuple) -> bool:
    """True the first time ``key`` is seen in this process."""
    with _seen_lock:
        if key in _seen:
            return False
        _seen.add(key)
        return True


def _route_label(provider: Any, model: Any, base_url: Any) -> str:
    host = _host(base_url)
    label = f"provider={provider or '?'} model={model or '?'}"
    return f"{label} host={host}" if host else label


def filter_fallback_chain(chain: Any, *, source: str) -> Any:
    """``chain`` with every non-DeepSeek entry removed while the lock is active.

    Inactive, or not a list: returned unchanged (the same object). Entries that
    are not dicts pass through untouched — callers already skip them."""
    if not lock_active() or not isinstance(chain, list):
        return chain
    kept: List[Any] = []
    for entry in chain:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        base_url = entry.get("base_url")
        if route_allowed(provider, base_url):
            kept.append(entry)
            continue
        if _once(("drop", source, provider.lower(), model, _host(base_url))):
            logger.warning(
                "%s: dropped non-DeepSeek fallback %s from %s — DeepSeek-only lock; "
                "a DeepSeek outage now fails closed instead of switching provider",
                ALERT_PREFIX, _route_label(provider, model, base_url), source)
    return kept


def refuse_fallback_entry(provider: Any, model: Any, base_url: Any = None) -> bool:
    """True when the agent's fallback walk must skip this entry (lock active and
    the route is not DeepSeek)."""
    if not lock_active() or route_allowed(provider, base_url):
        return False
    if _once(("skip", str(provider or "").lower(), str(model or ""), _host(base_url))):
        logger.warning("%s: refused fallback to %s — DeepSeek-only lock",
                       ALERT_PREFIX, _route_label(provider, model, base_url))
    return True


def _client_base_url(client: Any) -> str:
    for attr in ("base_url", "_base_url"):
        value = getattr(client, attr, None)
        if value:
            return str(value)
    return ""


def guard_resolved_client(client: Any, model: Any, *, provider: Any, task: Any = None) -> Tuple[Any, Any]:
    """Result filter for ``resolve_provider_client``: ``(client, model)`` when
    allowed, ``(None, None)`` for a client whose endpoint is not DeepSeek."""
    if client is None or not lock_active():
        return client, model
    base_url = _client_base_url(client)
    if route_allowed(provider, base_url):
        return client, model
    if _once(("client", str(provider or "").lower(), str(model or ""), _host(base_url), str(task or ""))):
        logger.warning("%s: refused a model client for %s (task=%s) — DeepSeek-only lock",
                       ALERT_PREFIX, _route_label(provider, model, base_url), task or "main")
    return None, None


def _agent_route(agent: Any) -> Tuple[str, str, str]:
    provider = str(getattr(agent, "provider", "") or "")
    model = str(getattr(agent, "model", "") or "")
    base_url = str(getattr(agent, "base_url", "") or "") or _client_base_url(getattr(agent, "client", None))
    return provider, model, base_url


def enforce_agent_primary(agent: Any) -> None:
    """Refuse to build an agent whose primary route is not DeepSeek."""
    if not lock_active():
        return
    provider, model, base_url = _agent_route(agent)
    if route_allowed(provider, base_url):
        return
    raise RuntimeError(
        f"{ALERT_PREFIX}: refusing to start an agent on {_route_label(provider, model, base_url)} — "
        "this machine is locked to DeepSeek models. Set model.provider: deepseek in config.yaml.")


def enforce_runtime(runtime: Any, model: Any, *, where: str) -> None:
    """Refuse a resolved runtime (cron job) whose route is not DeepSeek."""
    if not lock_active():
        return
    runtime = runtime if isinstance(runtime, dict) else {}
    provider = runtime.get("provider")
    base_url = runtime.get("base_url")
    if route_allowed(provider, base_url):
        return
    raise RuntimeError(
        f"{ALERT_PREFIX}: {where} resolves to {_route_label(provider, model, base_url)} — this machine is "
        "locked to DeepSeek models, so the run is refused instead of sent there. Fix the job's "
        "provider/model pin or config.yaml model.provider.")


def enforce_switch(new_provider: Any, base_url: Any, new_model: Any) -> None:
    """Refuse a live model switch (``/model``, a gateway or API switch) whose
    destination is not DeepSeek. Raised before any state changes."""
    if not lock_active() or route_allowed(new_provider, base_url):
        return
    raise RuntimeError(
        f"{ALERT_PREFIX}: refusing to switch to {_route_label(new_provider, new_model, base_url)} — "
        "this machine is locked to DeepSeek models.")


def _adopt_external_logins_config() -> bool:
    """config.yaml ``auth.adopt_external_logins`` (default True; any read failure = default)."""
    try:
        from hermes_cli.config import load_config_readonly
        auth_cfg = (load_config_readonly() or {}).get("auth")
    except Exception:
        return True
    return not isinstance(auth_cfg, dict) or bool(auth_cfg.get("adopt_external_logins", True))


def external_logins_allowed(*, source: str = "external CLI") -> bool:
    """Whether the runtime may read and use another program's model login
    (``source`` names it for the one-time log line). False while the lock is
    active, or when config.yaml sets ``auth.adopt_external_logins: false``."""
    if lock_active():
        if _once(("external-login", source)):
            logger.info("%s: not adopting the %s login — DeepSeek-only lock (auth.adopt_external_logins)",
                        ALERT_PREFIX, source)
        return False
    return _adopt_external_logins_config()


def note_fail_closed(agent: Any, reason: Any = None) -> None:
    """Once per agent: DeepSeek failed and no allowed fallback is left."""
    if not lock_active() or getattr(agent, "_lucaryin_lock_fail_closed_noted", False):
        return
    try:
        agent._lucaryin_lock_fail_closed_noted = True
    except Exception:
        pass
    provider, model, base_url = _agent_route(agent)
    logger.warning(
        "%s: %s unavailable (%s); no other provider is allowed (DeepSeek-only lock) — "
        "failing closed instead of switching provider",
        ALERT_PREFIX, _route_label(provider, model, base_url), getattr(reason, "value", reason) or "error")


def alert_if_violation(provider: Any, base_url: Any, model: Any, *, where: str) -> bool:
    """One ERROR line per (provider, model, host) per process when a call was
    served off DeepSeek while the lock is active. Returns True on a violation."""
    try:
        if not lock_active() or route_allowed(provider, base_url):
            return False
        if _once(("violation", str(provider or "").lower(), str(model or ""), _host(base_url))):
            logger.error("%s VIOLATION: %s API call served by %s — this machine is locked to DeepSeek models",
                         ALERT_PREFIX, where, _route_label(provider, model, base_url))
        return True
    except Exception:  # detection must never break a call
        return False


def _reset_for_tests() -> None:
    with _seen_lock:
        _seen.clear()


__all__: Iterable[str] = (
    "LOCK_ENV", "LOCKED_PROVIDER", "ALERT_PREFIX", "lock_active", "is_deepseek_host", "route_allowed",
    "filter_fallback_chain", "refuse_fallback_entry", "guard_resolved_client", "enforce_agent_primary",
    "enforce_runtime", "enforce_switch", "external_logins_allowed", "note_fail_closed", "alert_if_violation",
)
