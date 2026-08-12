"""
Cron job scheduler - executes due jobs.

Provides tick() which checks for due jobs and runs them. The gateway
calls this every 60 seconds from a background thread.

Uses a file-based lock (~/.hermes/cron/.tick.lock) so only one tick
runs at a time if multiple processes overlap.
"""

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import os
import subprocess
import sys
import time

# fcntl is Unix-only; on Windows use msvcrt for file locking
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
from pathlib import Path
from typing import List, Optional

# Add parent directory to path for imports BEFORE repo-level imports.
# Without this, standalone invocations (e.g. after `hermes update` reloads
# the module) fail with ModuleNotFoundError for hermes_time et al.
sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_constants import get_hermes_home
from hermes_cli.config import load_config
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

# Valid delivery platforms — used to validate user-supplied platform names
# in cron delivery targets, preventing env var enumeration via crafted names.
_KNOWN_DELIVERY_PLATFORMS = frozenset({
    "telegram", "discord", "slack", "whatsapp", "signal",
    "matrix", "mattermost", "homeassistant", "dingtalk", "feishu",
    "wecom", "wecom_callback", "weixin", "sms", "email", "webhook", "bluebubbles",
    "qqbot",
    # "lucaryin" is not a messenger: its adapter writes the run output straight
    # into the origin chat session in state.db (see _deliver_to_lucaryin_session),
    # so a cron created in the Lucaryin desktop/mobile chat delivers back into
    # that same thread. Listed here so an explicit `deliver=lucaryin:<session>`
    # also validates.
    "lucaryin",
})

# Platforms that support a configured cron/notification home target, mapped to
# the environment variable used by gateway setup/runtime config.
_HOME_TARGET_ENV_VARS = {
    "matrix": "MATRIX_HOME_ROOM",
    "telegram": "TELEGRAM_HOME_CHANNEL",
    "discord": "DISCORD_HOME_CHANNEL",
    "slack": "SLACK_HOME_CHANNEL",
    "signal": "SIGNAL_HOME_CHANNEL",
    "mattermost": "MATTERMOST_HOME_CHANNEL",
    "sms": "SMS_HOME_CHANNEL",
    "email": "EMAIL_HOME_ADDRESS",
    "dingtalk": "DINGTALK_HOME_CHANNEL",
    "feishu": "FEISHU_HOME_CHANNEL",
    "wecom": "WECOM_HOME_CHANNEL",
    "weixin": "WEIXIN_HOME_CHANNEL",
    "bluebubbles": "BLUEBUBBLES_HOME_CHANNEL",
    "qqbot": "QQBOT_HOME_CHANNEL",
}

# Legacy env var names kept for back-compat.  Each entry is the current
# primary env var → the previous name.  _get_home_target_chat_id falls
# back to the legacy name if the primary is unset, so users who set the
# old name before the rename keep working until they migrate.
_LEGACY_HOME_TARGET_ENV_VARS = {
    "QQBOT_HOME_CHANNEL": "QQ_HOME_CHANNEL",
}

from cron.jobs import get_due_jobs, mark_job_run, save_job_output, advance_next_run

# Sentinel: when a cron agent has nothing new to report, it can start its
# response with this marker to suppress delivery.  Output is still saved
# locally for audit.
SILENT_MARKER = "[SILENT]"

# Resolve Hermes home directory (respects HERMES_HOME override)
_hermes_home = get_hermes_home()

# File-based lock prevents concurrent ticks from gateway + daemon + systemd timer
_LOCK_DIR = _hermes_home / "cron"
_LOCK_FILE = _LOCK_DIR / ".tick.lock"


def _resolve_origin(job: dict) -> Optional[dict]:
    """Extract origin info from a job, preserving any extra routing metadata."""
    origin = job.get("origin")
    if not origin:
        return None
    platform = origin.get("platform")
    chat_id = origin.get("chat_id")
    if platform and chat_id:
        return origin
    return None


def _get_home_target_chat_id(platform_name: str) -> str:
    """Return the configured home target chat/room ID for a delivery platform."""
    env_var = _HOME_TARGET_ENV_VARS.get(platform_name.lower())
    if not env_var:
        return ""
    value = os.getenv(env_var, "")
    if not value:
        legacy = _LEGACY_HOME_TARGET_ENV_VARS.get(env_var)
        if legacy:
            value = os.getenv(legacy, "")
    return value


def _resolve_single_delivery_target(job: dict, deliver_value: str) -> Optional[dict]:
    """Resolve one concrete auto-delivery target for a cron job."""

    origin = _resolve_origin(job)

    if deliver_value == "local":
        return None

    if deliver_value == "origin":
        if origin:
            return {
                "platform": origin["platform"],
                "chat_id": str(origin["chat_id"]),
                "thread_id": origin.get("thread_id"),
            }
        # Origin missing (e.g. job created via API/script) — try each
        # platform's home channel as a fallback instead of silently dropping.
        for platform_name in _HOME_TARGET_ENV_VARS:
            chat_id = _get_home_target_chat_id(platform_name)
            if chat_id:
                logger.info(
                    "Job '%s' has deliver=origin but no origin; falling back to %s home channel",
                    job.get("name", job.get("id", "?")),
                    platform_name,
                )
                return {
                    "platform": platform_name,
                    "chat_id": chat_id,
                    "thread_id": None,
                }
        return None

    if ":" in deliver_value:
        platform_name, rest = deliver_value.split(":", 1)
        platform_key = platform_name.lower()

        from tools.send_message_tool import _parse_target_ref

        parsed_chat_id, parsed_thread_id, is_explicit = _parse_target_ref(platform_key, rest)
        if is_explicit:
            chat_id, thread_id = parsed_chat_id, parsed_thread_id
        else:
            chat_id, thread_id = rest, None

        # Resolve human-friendly labels like "Alice (dm)" to real IDs.
        try:
            from gateway.channel_directory import resolve_channel_name
            resolved = resolve_channel_name(platform_key, chat_id)
            if resolved:
                parsed_chat_id, parsed_thread_id, resolved_is_explicit = _parse_target_ref(platform_key, resolved)
                if resolved_is_explicit:
                    chat_id, thread_id = parsed_chat_id, parsed_thread_id
                else:
                    chat_id = resolved
        except Exception:
            pass

        return {
            "platform": platform_name,
            "chat_id": chat_id,
            "thread_id": thread_id,
        }

    platform_name = deliver_value
    if origin and origin.get("platform") == platform_name:
        return {
            "platform": platform_name,
            "chat_id": str(origin["chat_id"]),
            "thread_id": origin.get("thread_id"),
        }

    if platform_name.lower() not in _KNOWN_DELIVERY_PLATFORMS:
        return None
    chat_id = _get_home_target_chat_id(platform_name)
    if not chat_id:
        return None

    return {
        "platform": platform_name,
        "chat_id": chat_id,
        "thread_id": None,
    }


def _resolve_delivery_targets(job: dict) -> List[dict]:
    """Resolve all concrete auto-delivery targets for a cron job (supports comma-separated deliver)."""
    deliver = job.get("deliver", "local")
    if deliver == "local":
        return []
    parts = [p.strip() for p in str(deliver).split(",") if p.strip()]
    seen = set()
    targets = []
    for part in parts:
        target = _resolve_single_delivery_target(job, part)
        if target:
            key = (target["platform"].lower(), str(target["chat_id"]), target.get("thread_id"))
            if key not in seen:
                seen.add(key)
                targets.append(target)
    return targets


def _resolve_delivery_target(job: dict) -> Optional[dict]:
    """Resolve the concrete auto-delivery target for a cron job, if any."""
    targets = _resolve_delivery_targets(job)
    return targets[0] if targets else None


# Media extension sets — keep in sync with gateway/platforms/base.py:_process_message_background
_AUDIO_EXTS = frozenset({'.ogg', '.opus', '.mp3', '.wav', '.m4a'})
_VIDEO_EXTS = frozenset({'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'})
_IMAGE_EXTS = frozenset({'.jpg', '.jpeg', '.png', '.webp', '.gif'})


def _send_media_via_adapter(adapter, chat_id: str, media_files: list, metadata: dict | None, loop, job: dict) -> None:
    """Send extracted MEDIA files as native platform attachments via a live adapter.

    Routes each file to the appropriate adapter method (send_voice, send_image_file,
    send_video, send_document) based on file extension — mirroring the routing logic
    in ``BasePlatformAdapter._process_message_background``.
    """
    from pathlib import Path

    for media_path, _is_voice in media_files:
        try:
            ext = Path(media_path).suffix.lower()
            if ext in _AUDIO_EXTS:
                coro = adapter.send_voice(chat_id=chat_id, audio_path=media_path, metadata=metadata)
            elif ext in _VIDEO_EXTS:
                coro = adapter.send_video(chat_id=chat_id, video_path=media_path, metadata=metadata)
            elif ext in _IMAGE_EXTS:
                coro = adapter.send_image_file(chat_id=chat_id, image_path=media_path, metadata=metadata)
            else:
                coro = adapter.send_document(chat_id=chat_id, file_path=media_path, metadata=metadata)

            future = asyncio.run_coroutine_threadsafe(coro, loop)
            result = future.result(timeout=30)
            if result and not getattr(result, "success", True):
                logger.warning(
                    "Job '%s': media send failed for %s: %s",
                    job.get("id", "?"), media_path, getattr(result, "error", "unknown"),
                )
        except Exception as e:
            logger.warning("Job '%s': failed to send media %s: %s", job.get("id", "?"), media_path, e)


def _deliver_to_lucaryin_session(job: dict, session_id: str, content: str) -> Optional[str]:
    """Deliver cron output into a Lucaryin chat thread by writing it to state.db.

    The "lucaryin" platform is not a messenger — the origin chat_id IS a
    SessionDB session id, and the Lucaryin app renders that session from
    ~/.hermes/state.db. So we append the run output as an assistant message on
    that session; the desktop app shows it when the thread reloads.

    Mobile is a second surface: it only mirrors state.db via hub_sync, and the
    node's hub_sync poller is not always running (a chat turn triggers a
    fast-flush, but a cron run has no turn). So we drop a best-effort flush
    hint that the Lucaryin bridge's cron ticker drains and flushes. On a plain
    hermes-agent host (no lucaryin delivery ever resolved) the hint is simply
    never written. Returns None on success, or an error string.
    """
    if not session_id:
        return "lucaryin delivery has no session id (origin chat_id missing)"
    try:
        from hermes_state import SessionDB
        SessionDB().append_message(
            session_id=str(session_id),
            role="assistant",
            content=content,
        )
    except Exception as e:  # pragma: no cover — defensive
        logger.error("Job '%s': lucaryin state.db write failed: %s", job.get("id"), e)
        return f"lucaryin delivery failed: {e}"

    # Best-effort flush hint (see docstring). Never fails the delivery.
    try:
        from hermes_constants import get_hermes_home  # same helper SessionDB uses
        hint_dir = get_hermes_home() / "cron"
    except Exception:
        hint_dir = None
    if hint_dir is not None:
        try:
            hint_dir.mkdir(parents=True, exist_ok=True)
            with open(hint_dir / ".pending_hub_flush", "a", encoding="utf-8") as fh:
                fh.write(f"{session_id}\n")
        except Exception:  # pragma: no cover — hint is optional
            pass
    return None


def _deliver_result(job: dict, content: str, adapters=None, loop=None) -> Optional[str]:
    """
    Deliver job output to the configured target(s) (origin chat, specific platform, etc.).

    When ``adapters`` and ``loop`` are provided (gateway is running), tries to
    use the live adapter first — this supports E2EE rooms (e.g. Matrix) where
    the standalone HTTP path cannot encrypt.  Falls back to standalone send if
    the adapter path fails or is unavailable.

    Returns None on success, or an error string on failure.
    """
    targets = _resolve_delivery_targets(job)
    if not targets:
        if job.get("deliver", "local") != "local":
            msg = f"no delivery target resolved for deliver={job.get('deliver', 'local')}"
            logger.warning("Job '%s': %s", job["id"], msg)
            return msg
        return None  # local-only jobs don't deliver — not a failure

    from tools.send_message_tool import _send_to_platform
    from gateway.config import load_gateway_config, Platform

    platform_map = {
        "telegram": Platform.TELEGRAM,
        "discord": Platform.DISCORD,
        "slack": Platform.SLACK,
        "whatsapp": Platform.WHATSAPP,
        "signal": Platform.SIGNAL,
        "matrix": Platform.MATRIX,
        "mattermost": Platform.MATTERMOST,
        "homeassistant": Platform.HOMEASSISTANT,
        "dingtalk": Platform.DINGTALK,
        "feishu": Platform.FEISHU,
        "wecom": Platform.WECOM,
        "wecom_callback": Platform.WECOM_CALLBACK,
        "weixin": Platform.WEIXIN,
        "email": Platform.EMAIL,
        "sms": Platform.SMS,
        "bluebubbles": Platform.BLUEBUBBLES,
        "qqbot": Platform.QQBOT,
    }

    # Optionally wrap the content with a header/footer so the user knows this
    # is a cron delivery.  Wrapping is on by default; set cron.wrap_response: false
    # in config.yaml for clean output.
    wrap_response = True
    try:
        user_cfg = load_config()
        wrap_response = user_cfg.get("cron", {}).get("wrap_response", True)
    except Exception:
        pass

    if wrap_response:
        task_name = job.get("name", job["id"])
        # No raw job ids or "cronjob" plumbing at the customer — the header
        # names the task in plain language; management goes through chat or
        # the Jobs panel (Lucaryin) by NAME, never by id.
        delivery_content = (
            f"⏰ {task_name} — scheduled update\n"
            f"-------------\n\n"
            f"{content}\n\n"
            f"To pause or change this, just tell me (e.g. \"pause {task_name}\")."
        )
    else:
        delivery_content = content

    # Extract MEDIA: tags so attachments are forwarded as files, not raw text
    from gateway.platforms.base import BasePlatformAdapter
    media_files, cleaned_delivery_content = BasePlatformAdapter.extract_media(delivery_content)

    try:
        config = load_gateway_config()
    except Exception as e:
        msg = f"failed to load gateway config: {e}"
        logger.error("Job '%s': %s", job["id"], msg)
        return msg

    delivery_errors = []

    for target in targets:
        platform_name = target["platform"]
        chat_id = target["chat_id"]
        thread_id = target.get("thread_id")

        # Lucaryin: not a messenger — write the output into the origin chat
        # session in state.db (chat_id IS the session id). Deliver the CLEANED
        # content (MEDIA: tags stripped) with the same wrapper as messengers.
        if platform_name.lower() == "lucaryin":
            err = _deliver_to_lucaryin_session(job, str(chat_id), cleaned_delivery_content)
            if err:
                delivery_errors.append(err)
            else:
                logger.info("Job '%s': delivered into Lucaryin session %s", job["id"], chat_id)
            continue

        # Diagnostic: log thread_id for topic-aware delivery debugging
        origin = job.get("origin") or {}
        origin_thread = origin.get("thread_id")
        if origin_thread and not thread_id:
            logger.warning(
                "Job '%s': origin has thread_id=%s but delivery target lost it "
                "(deliver=%s, target=%s)",
                job["id"], origin_thread, job.get("deliver", "local"), target,
            )
        elif thread_id:
            logger.debug(
                "Job '%s': delivering to %s:%s thread_id=%s",
                job["id"], platform_name, chat_id, thread_id,
            )

        platform = platform_map.get(platform_name.lower())
        if not platform:
            msg = f"unknown platform '{platform_name}'"
            logger.warning("Job '%s': %s", job["id"], msg)
            delivery_errors.append(msg)
            continue

        # Prefer the live adapter when the gateway is running — this supports E2EE
        # rooms (e.g. Matrix) where the standalone HTTP path cannot encrypt.
        runtime_adapter = (adapters or {}).get(platform)
        delivered = False
        if runtime_adapter is not None and loop is not None and getattr(loop, "is_running", lambda: False)():
            send_metadata = {"thread_id": thread_id} if thread_id else None
            try:
                # Send cleaned text (MEDIA tags stripped) — not the raw content
                text_to_send = cleaned_delivery_content.strip()
                adapter_ok = True
                if text_to_send:
                    future = asyncio.run_coroutine_threadsafe(
                        runtime_adapter.send(chat_id, text_to_send, metadata=send_metadata),
                        loop,
                    )
                    send_result = future.result(timeout=60)
                    if send_result and not getattr(send_result, "success", True):
                        err = getattr(send_result, "error", "unknown")
                        logger.warning(
                            "Job '%s': live adapter send to %s:%s failed (%s), falling back to standalone",
                            job["id"], platform_name, chat_id, err,
                        )
                        adapter_ok = False  # fall through to standalone path

                # Send extracted media files as native attachments via the live adapter
                if adapter_ok and media_files:
                    _send_media_via_adapter(runtime_adapter, chat_id, media_files, send_metadata, loop, job)

                if adapter_ok:
                    logger.info("Job '%s': delivered to %s:%s via live adapter", job["id"], platform_name, chat_id)
                    delivered = True
            except Exception as e:
                logger.warning(
                    "Job '%s': live adapter delivery to %s:%s failed (%s), falling back to standalone",
                    job["id"], platform_name, chat_id, e,
                )

        if not delivered:
            pconfig = config.platforms.get(platform)
            if not pconfig or not pconfig.enabled:
                msg = f"platform '{platform_name}' not configured/enabled"
                logger.warning("Job '%s': %s", job["id"], msg)
                delivery_errors.append(msg)
                continue

            # Standalone path: run the async send in a fresh event loop (safe from any thread)
            coro = _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files)
            try:
                result = asyncio.run(coro)
            except RuntimeError:
                # asyncio.run() checks for a running loop before awaiting the coroutine;
                # when it raises, the original coro was never started — close it to
                # prevent "coroutine was never awaited" RuntimeWarning, then retry in a
                # fresh thread that has no running loop.
                coro.close()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files))
                    result = future.result(timeout=30)
            except Exception as e:
                msg = f"delivery to {platform_name}:{chat_id} failed: {e}"
                logger.error("Job '%s': %s", job["id"], msg)
                delivery_errors.append(msg)
                continue

            if result and result.get("error"):
                msg = f"delivery error: {result['error']}"
                logger.error("Job '%s': %s", job["id"], msg)
                delivery_errors.append(msg)
                continue

            logger.info("Job '%s': delivered to %s:%s", job["id"], platform_name, chat_id)

    if delivery_errors:
        return "; ".join(delivery_errors)
    return None


_DEFAULT_SCRIPT_TIMEOUT = 120  # seconds
# Backward-compatible module override used by tests and emergency monkeypatches.
_SCRIPT_TIMEOUT = _DEFAULT_SCRIPT_TIMEOUT


def _get_script_timeout() -> int:
    """Resolve cron pre-run script timeout from module/env/config with a safe default."""
    if _SCRIPT_TIMEOUT != _DEFAULT_SCRIPT_TIMEOUT:
        try:
            timeout = int(float(_SCRIPT_TIMEOUT))
            if timeout > 0:
                return timeout
        except Exception:
            logger.warning("Invalid patched _SCRIPT_TIMEOUT=%r; using env/config/default", _SCRIPT_TIMEOUT)

    env_value = os.getenv("HERMES_CRON_SCRIPT_TIMEOUT", "").strip()
    if env_value:
        try:
            timeout = int(float(env_value))
            if timeout > 0:
                return timeout
        except Exception:
            logger.warning("Invalid HERMES_CRON_SCRIPT_TIMEOUT=%r; using config/default", env_value)

    try:
        cfg = load_config() or {}
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        configured = cron_cfg.get("script_timeout_seconds")
        if configured is not None:
            timeout = int(float(configured))
            if timeout > 0:
                return timeout
    except Exception as exc:
        logger.debug("Failed to load cron script timeout from config: %s", exc)

    return _DEFAULT_SCRIPT_TIMEOUT


def _run_job_script(script_path: str) -> tuple[bool, str]:
    """Execute a cron job's data-collection script and capture its output.

    Scripts must reside within HERMES_HOME/scripts/.  Both relative and
    absolute paths are resolved and validated against this directory to
    prevent arbitrary script execution via path traversal or absolute
    path injection.

    Args:
        script_path: Path to a Python script.  Relative paths are resolved
            against HERMES_HOME/scripts/.  Absolute and ~-prefixed paths
            are also validated to ensure they stay within the scripts dir.

    Returns:
        (success, output) — on failure *output* contains the error message so the
        LLM can report the problem to the user.
    """
    from hermes_constants import get_hermes_home

    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir_resolved = scripts_dir.resolve()

    raw = Path(script_path).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (scripts_dir / raw).resolve()

    # Guard against path traversal, absolute path injection, and symlink
    # escape — scripts MUST reside within HERMES_HOME/scripts/.
    try:
        path.relative_to(scripts_dir_resolved)
    except ValueError:
        return False, (
            f"Blocked: script path resolves outside the scripts directory "
            f"({scripts_dir_resolved}): {script_path!r}"
        )

    if not path.exists():
        return False, f"Script not found: {path}"
    if not path.is_file():
        return False, f"Script path is not a file: {path}"

    script_timeout = _get_script_timeout()

    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=script_timeout,
            cwd=str(path.parent),
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        # Redact secrets from both stdout and stderr before any return path.
        try:
            from agent.redact import redact_sensitive_text
            stdout = redact_sensitive_text(stdout)
            stderr = redact_sensitive_text(stderr)
        except Exception:
            pass

        if result.returncode != 0:
            parts = [f"Script exited with code {result.returncode}"]
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            if stdout:
                parts.append(f"stdout:\n{stdout}")
            return False, "\n".join(parts)

        return True, stdout

    except subprocess.TimeoutExpired:
        return False, f"Script timed out after {script_timeout}s: {path}"
    except Exception as exc:
        return False, f"Script execution failed: {exc}"


def _parse_wake_gate(script_output: str) -> bool:
    """Parse the last non-empty stdout line of a cron job's pre-check script
    as a wake gate.

    The convention (ported from nanoclaw #1232): if the last stdout line is
    JSON like ``{"wakeAgent": false}``, the agent is skipped entirely — no
    LLM run, no delivery. Any other output (non-JSON, missing flag, gate
    absent, or ``wakeAgent: true``) means wake the agent normally.

    Returns True if the agent should wake, False to skip.
    """
    if not script_output:
        return True
    stripped_lines = [line for line in script_output.splitlines() if line.strip()]
    if not stripped_lines:
        return True
    last_line = stripped_lines[-1].strip()
    try:
        gate = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        return True
    if not isinstance(gate, dict):
        return True
    return gate.get("wakeAgent", True) is not False


def _build_job_prompt(job: dict, prerun_script: Optional[tuple] = None) -> str:
    """Build the effective prompt for a cron job, optionally loading one or more skills first.

    Args:
        job: The cron job dict.
        prerun_script: Optional ``(success, stdout)`` from a script that has
            already been executed by the caller (e.g. for a wake-gate check).
            When provided, the script is not re-executed and the cached
            result is used for prompt injection. When omitted, the script
            (if any) runs inline as before.
    """
    prompt = job.get("prompt", "")
    skills = job.get("skills")

    # Run data-collection script if configured, inject output as context.
    script_path = job.get("script")
    if script_path:
        if prerun_script is not None:
            success, script_output = prerun_script
        else:
            success, script_output = _run_job_script(script_path)
        if success:
            if script_output:
                prompt = (
                    "## Script Output\n"
                    "The following data was collected by a pre-run script. "
                    "Use it as context for your analysis.\n\n"
                    f"```\n{script_output}\n```\n\n"
                    f"{prompt}"
                )
            else:
                prompt = (
                    "[Script ran successfully but produced no output.]\n\n"
                    f"{prompt}"
                )
        else:
            prompt = (
                "## Script Error\n"
                "The data-collection script failed. Report this to the user.\n\n"
                f"```\n{script_output}\n```\n\n"
                f"{prompt}"
            )

    # Always prepend cron execution guidance so the agent knows how
    # delivery works and can suppress delivery when appropriate.
    cron_hint = (
        "[SYSTEM: You are running as a scheduled cron job. "
        "DELIVERY: Your final response will be automatically delivered "
        "to the user — do NOT use send_message or try to deliver "
        "the output yourself. Just produce your report/output as your "
        "final response and the system handles the rest. "
        "SILENT: If there is genuinely nothing new to report, respond "
        "with exactly \"[SILENT]\" (nothing else) to suppress delivery. "
        "Never combine [SILENT] with content — either report your "
        "findings normally, or say [SILENT] and nothing more.]\n\n"
    )
    prompt = cron_hint + prompt
    if skills is None:
        legacy = job.get("skill")
        skills = [legacy] if legacy else []

    skill_names = [str(name).strip() for name in skills if str(name).strip()]
    if not skill_names:
        return prompt

    from tools.skills_tool import skill_view

    parts = []
    skipped: list[str] = []
    for skill_name in skill_names:
        loaded = json.loads(skill_view(skill_name))
        if not loaded.get("success"):
            error = loaded.get("error") or f"Failed to load skill '{skill_name}'"
            logger.warning("Cron job '%s': skill not found, skipping — %s", job.get("name", job.get("id")), error)
            skipped.append(skill_name)
            continue

        content = str(loaded.get("content") or "").strip()
        if parts:
            parts.append("")
        parts.extend(
            [
                f'[SYSTEM: The user has invoked the "{skill_name}" skill, indicating they want you to follow its instructions. The full skill content is loaded below.]',
                "",
                content,
            ]
        )

    if skipped:
        notice = (
            f"[SYSTEM: The following skill(s) were listed for this job but could not be found "
            f"and were skipped: {', '.join(skipped)}. "
            f"Start your response with a brief notice so the user is aware, e.g.: "
            f"'⚠️ Skill(s) not found and skipped: {', '.join(skipped)}']"
        )
        parts.insert(0, notice)

    if prompt:
        parts.extend(["", f"The user has provided the following instruction alongside the skill invocation: {prompt}"])
    return "\n".join(parts)


def _await_agent_result(future, agent, inactivity_limit=None, wall_clock_limit=None,
                        poll_interval=5.0, started_at=None):
    """Wait for an agent run under two independent caps.

    Returns (result, timeout_kind) where timeout_kind is None on completion,
    "inactivity" when the agent went quiet, or "wall_clock" when the run
    exceeded its total allowance regardless of activity.

    Both caps exist for different failure modes. Inactivity catches a hung
    API call or stuck tool. The wall clock catches the opposite: on
    2026-08-11 a heartbeat streamed happily for 26 minutes — never idle for
    even a second — while every other job on this machine's serial cron
    queue sat blocked behind it. An agent that is busy forever is just as
    dead to the queue as one that is hung.
    """
    if started_at is None:
        started_at = time.time()
    if inactivity_limit is None and wall_clock_limit is None:
        return future.result(), None
    while True:
        done, _ = concurrent.futures.wait({future}, timeout=poll_interval)
        if done:
            return future.result(), None
        if wall_clock_limit is not None and (time.time() - started_at) >= wall_clock_limit:
            return None, "wall_clock"
        if inactivity_limit is not None:
            idle_secs = 0.0
            if hasattr(agent, "get_activity_summary"):
                try:
                    idle_secs = agent.get_activity_summary().get(
                        "seconds_since_activity", 0.0)
                except Exception:
                    pass
            if idle_secs >= inactivity_limit:
                return None, "inactivity"


def _get_wall_clock_limit(job: dict):
    """Per-job wall_clock_seconds, else HERMES_CRON_WALL_CLOCK, else 45 min.
    0 (either source) = unlimited."""
    for raw in (job.get("wall_clock_seconds"), os.getenv("HERMES_CRON_WALL_CLOCK")):
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        return value if value > 0 else None
    return 2700.0


# Runtime-emitted sentinels that mean the turn failed even though the agent
# returned them as ordinary text instead of raising. Anchored at the START of
# the response so a job legitimately *discussing* an error isn't misjudged.
_RESPONSE_FAILURE_PREFIXES = (
    "API call failed after",
    "Error during OpenAI-compatible API call",
    "Error executing tool",
    "All API providers failed",
    "Provider error:",
)


def _response_failure_reason(final_response: str) -> Optional[str]:
    """Return a short reason when a final response is itself a failure
    sentinel, else None. Only the leading line is inspected."""
    head = (final_response or "").strip()
    if not head:
        return None
    first_line = head.splitlines()[0].strip()
    for prefix in _RESPONSE_FAILURE_PREFIXES:
        if first_line.startswith(prefix):
            return first_line[:200]
    return None


def _wrap_job_output(job_name: str, job_id: str, prompt: str, response: str,
                     schedule_display: str = "N/A") -> str:
    """The standard cron output document. Shared so the success and
    response-failure paths render identically."""
    return (
        f"# Cron Job: {job_name}\n\n"
        f"**Job ID:** {job_id}\n"
        f"**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"**Schedule:** {schedule_display}\n\n"
        f"## Prompt\n\n{prompt}\n\n"
        f"## Response\n\n{response if response else '(No response generated)'}\n"
    )


def _persist_job_grants(job: dict) -> None:
    """Write the job's (possibly grant-mutated) state back to the store. Used
    after a meeting_join consumes its single-use dial grant, so the grant can't
    be reused across a restart."""
    from cron.jobs import load_jobs, save_jobs
    jobs = load_jobs()
    for i, j in enumerate(jobs):
        if j.get("id") == job.get("id"):
            jobs[i]["grants"] = job.get("grants", [])
            save_jobs(jobs)
            return


def run_job(job: dict) -> tuple[bool, str, str, Optional[str]]:
    """
    Execute a single cron job.
    
    Returns:
        Tuple of (success, full_output_doc, final_response, error_message)
    """
    from run_agent import AIAgent
    
    # Initialize SQLite session store so cron job messages are persisted
    # and discoverable via session_search (same pattern as gateway/run.py).
    _session_db = None
    try:
        from hermes_state import SessionDB
        _session_db = SessionDB()
    except Exception as e:
        logger.debug("Job '%s': SQLite session store not available: %s", job.get("id", "?"), e)
    
    job_id = job["id"]
    job_name = job["name"]

    # Wake-gate: if this job has a pre-check script, run it BEFORE building
    # the prompt so a ``{"wakeAgent": false}`` response can short-circuit
    # the whole agent run. We pass the result into _build_job_prompt so
    # the script is only executed once.
    prerun_script = None
    script_path = job.get("script")
    if script_path:
        prerun_script = _run_job_script(script_path)
        _ran_ok, _script_output = prerun_script
        if _ran_ok and not _parse_wake_gate(_script_output):
            logger.info(
                "Job '%s' (ID: %s): wakeAgent=false, skipping agent run",
                job_name, job_id,
            )
            silent_doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "Script gate returned `wakeAgent=false` — agent skipped.\n"
            )
            return True, silent_doc, SILENT_MARKER, None

    prompt = _build_job_prompt(job, prerun_script=prerun_script)
    origin = _resolve_origin(job)
    _cron_session_id = f"cron_{job_id}_{_hermes_now().strftime('%Y%m%d_%H%M%S')}"

    # ── P7: Record task start via bridge ──────────────────────────
    try:
        from cron.p7_task_hook import task_start as _p7_task_start, task_end as _p7_task_end
        _p7_task_start(_cron_session_id, job_id, job_name)
    except Exception:
        _p7_task_start = None
        _p7_task_end = None

    logger.info("Running job '%s' (ID: %s)", job_name, job_id)
    logger.info("Prompt: %s", prompt[:100])

    # Mark this as a cron session so the approval system can apply cron_mode.
    # This env var is process-wide and persists for the lifetime of the
    # scheduler process — every job this process runs is a cron job.
    os.environ["HERMES_CRON_SESSION"] = "1"

    # ── Approval gate ────────────────────────────────────────────────
    # The line above was, until 2026-08-09, the whole of "the approval
    # system applies cron_mode": nothing anywhere read HERMES_CRON_SESSION,
    # and because cron jobs build AIAgent directly instead of going through
    # the bridge, no pre_tool_call hook was ever registered for them. Every
    # scheduled job on every machine ran ungated at any trust level.
    #
    # cron_gate installs the same gate the interactive path uses, with
    # unattended=True. `grants` are the actions this job was authorized to
    # perform when the user created it — a job does its own work without
    # asking, and only steps outside that remit stop for a human. A job with
    # no grants can read and report but not act, which is the right default
    # for a goal-less heartbeat.
    try:
        # cron_gate ships with hermes-bridge (the app bundle), not with this
        # tree, so its directory has to reach sys.path BEFORE the import —
        # the module's own locator cannot help us find the module.
        import sys as _sys
        for _cand in (
            os.environ.get("LUCARYIN_BRIDGE_DIR", ""),
            "/Applications/Lucaryin AI.app/Contents/Resources/"
            "app.asar.unpacked/hermes-bridge",
            os.path.expanduser("~/.lucaryin/hermes-bridge"),
        ):
            if _cand and os.path.exists(os.path.join(_cand, "cron_gate.py")):
                if _cand not in _sys.path:
                    _sys.path.insert(0, _cand)
                break
        import cron_gate
        _agent_key = job.get("agent")
        if not _agent_key:
            try:
                from hermes_cli.profiles import get_active_profile_name
                _agent_key = get_active_profile_name() or "thoth"
            except Exception:
                _agent_key = "thoth"
        # The unnamed/"default" profile is Thoth — same convention the bridge
        # and hub_sync use (`_BRIDGE_PROFILE or "thoth"`). Without this the
        # gate would look up ask_before config for an agent named "default".
        if str(_agent_key).lower() in ("", "default", "none"):
            _agent_key = "thoth"
        cron_gate.install(
            agent_key=str(_agent_key).lower(),
            trust_level=(job.get("trust_level") or "cautious"),
            grants=(job.get("grants") or []),
        )
    except Exception:
        # Never let gate wiring break job execution — but cron_gate itself
        # logs CRON UNGATED loudly when it cannot arm, so this is visible.
        logger.warning("cron approval gate could not be installed", exc_info=True)

    try:
        # Inject origin context so the agent's send_message tool knows the chat.
        # Must be INSIDE the try block so the finally cleanup always runs.
        if origin:
            os.environ["HERMES_SESSION_PLATFORM"] = origin["platform"]
            os.environ["HERMES_SESSION_CHAT_ID"] = str(origin["chat_id"])
            if origin.get("chat_name"):
                os.environ["HERMES_SESSION_CHAT_NAME"] = origin["chat_name"]
        # Re-read .env and config.yaml fresh every run so provider/key
        # changes take effect without a gateway restart.
        from dotenv import load_dotenv
        try:
            load_dotenv(str(_hermes_home / ".env"), override=True, encoding="utf-8")
        except UnicodeDecodeError:
            load_dotenv(str(_hermes_home / ".env"), override=True, encoding="latin-1")

        delivery_target = _resolve_delivery_target(job)
        if delivery_target:
            os.environ["HERMES_CRON_AUTO_DELIVER_PLATFORM"] = delivery_target["platform"]
            os.environ["HERMES_CRON_AUTO_DELIVER_CHAT_ID"] = str(delivery_target["chat_id"])
            if delivery_target.get("thread_id") is not None:
                os.environ["HERMES_CRON_AUTO_DELIVER_THREAD_ID"] = str(delivery_target["thread_id"])

        model = job.get("model") or os.getenv("HERMES_MODEL") or ""

        # Load config.yaml for model, reasoning, prefill, toolsets, provider routing
        _cfg = {}
        try:
            import yaml
            _cfg_path = str(_hermes_home / "config.yaml")
            if os.path.exists(_cfg_path):
                with open(_cfg_path) as _f:
                    _cfg = yaml.safe_load(_f) or {}
                _model_cfg = _cfg.get("model", {})
                if not job.get("model"):
                    if isinstance(_model_cfg, str):
                        model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        model = _model_cfg.get("default", model)
        except Exception as e:
            logger.warning("Job '%s': failed to load config.yaml, using defaults: %s", job_id, e)

        # Apply IPv4 preference if configured.
        try:
            from hermes_constants import apply_ipv4_preference
            _net_cfg = _cfg.get("network", {})
            if isinstance(_net_cfg, dict) and _net_cfg.get("force_ipv4"):
                apply_ipv4_preference(force=True)
        except Exception:
            pass

        # Reasoning config from config.yaml
        from hermes_constants import parse_reasoning_effort
        effort = str(_cfg.get("agent", {}).get("reasoning_effort", "")).strip()
        reasoning_config = parse_reasoning_effort(effort)

        # Prefill messages from env or config.yaml
        prefill_messages = None
        prefill_file = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "") or _cfg.get("prefill_messages_file", "")
        if prefill_file:
            import json as _json
            pfpath = Path(prefill_file).expanduser()
            if not pfpath.is_absolute():
                pfpath = _hermes_home / pfpath
            if pfpath.exists():
                try:
                    with open(pfpath, "r", encoding="utf-8") as _pf:
                        prefill_messages = _json.load(_pf)
                    if not isinstance(prefill_messages, list):
                        prefill_messages = None
                except Exception as e:
                    logger.warning("Job '%s': failed to parse prefill messages file '%s': %s", job_id, pfpath, e)
                    prefill_messages = None

        # Max iterations
        max_iterations = _cfg.get("agent", {}).get("max_turns") or _cfg.get("max_turns") or 90

        # Provider routing
        pr = _cfg.get("provider_routing", {})

        from hermes_cli.runtime_provider import (
            resolve_runtime_provider,
            format_runtime_provider_error,
        )
        try:
            runtime_kwargs = {
                "requested": job.get("provider") or os.getenv("HERMES_INFERENCE_PROVIDER"),
            }
            if job.get("base_url"):
                runtime_kwargs["explicit_base_url"] = job.get("base_url")
            runtime = resolve_runtime_provider(**runtime_kwargs)
        except Exception as exc:
            message = format_runtime_provider_error(exc)
            raise RuntimeError(message) from exc

        fallback_model = _cfg.get("fallback_providers") or _cfg.get("fallback_model") or None
        credential_pool = None
        runtime_provider = str(runtime.get("provider") or "").strip().lower()
        if runtime_provider:
            try:
                from agent.credential_pool import load_pool
                pool = load_pool(runtime_provider)
                if pool.has_credentials():
                    credential_pool = pool
                    logger.info(
                        "Job '%s': loaded credential pool for provider %s with %d entries",
                        job_id,
                        runtime_provider,
                        len(pool.entries()),
                    )
            except Exception as e:
                logger.debug("Job '%s': failed to load credential pool for %s: %s", job_id, runtime_provider, e)

        agent = AIAgent(
            model=model,
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            acp_command=runtime.get("command"),
            acp_args=runtime.get("args"),
            max_iterations=max_iterations,
            reasoning_config=reasoning_config,
            prefill_messages=prefill_messages,
            fallback_model=fallback_model,
            credential_pool=credential_pool,
            providers_allowed=pr.get("only"),
            providers_ignored=pr.get("ignore"),
            providers_order=pr.get("order"),
            provider_sort=pr.get("sort"),
            disabled_toolsets=["cronjob", "messaging", "clarify"],
            quiet_mode=True,
            skip_context_files=True,  # Don't inject SOUL.md/AGENTS.md from scheduler cwd
            skip_memory=True,  # Cron system prompts would corrupt user representations
            platform="cron",
            session_id=_cron_session_id,
            session_db=_session_db,
        )
        
        # Run the agent with an *inactivity*-based timeout: the job can run
        # for hours if it's actively calling tools / receiving stream tokens,
        # but a hung API call or stuck tool with no activity for the configured
        # duration is caught and killed.  Default 600s (10 min inactivity);
        # override via HERMES_CRON_TIMEOUT env var.  0 = unlimited.
        #
        # Uses the agent's built-in activity tracker (updated by
        # _touch_activity() on every tool call, API call, and stream delta).
        _cron_timeout = float(os.getenv("HERMES_CRON_TIMEOUT", 600))
        _cron_inactivity_limit = _cron_timeout if _cron_timeout > 0 else None
        _cron_wall_limit = _get_wall_clock_limit(job)
        _cron_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Preserve scheduler-scoped ContextVar state (for example skill-declared
        # env passthrough registrations) when the cron run hops into the worker
        # thread used for inactivity timeout monitoring.
        _cron_context = contextvars.copy_context()
        _run_started = time.time()
        _cron_future = _cron_pool.submit(_cron_context.run, agent.run_conversation, prompt)
        try:
            result, _timeout_kind = _await_agent_result(
                _cron_future, agent,
                inactivity_limit=_cron_inactivity_limit,
                wall_clock_limit=_cron_wall_limit,
                started_at=_run_started,
            )
        except Exception:
            _cron_pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            _cron_pool.shutdown(wait=False, cancel_futures=True)

        if _timeout_kind == "wall_clock":
            _elapsed = time.time() - _run_started
            logger.error(
                "Job '%s' ran %.0fs of wall clock (limit %.0fs) without "
                "finishing — killed so it stops blocking the cron queue",
                job_name, _elapsed, _cron_wall_limit,
            )
            if hasattr(agent, "interrupt"):
                agent.interrupt("Cron job timed out (wall clock)")
            raise TimeoutError(
                f"Cron job '{job_name}' ran {int(_elapsed)}s of wall clock "
                f"(limit {int(_cron_wall_limit)}s) without finishing. The job "
                f"was still active — consider a smaller scope, or raise "
                f"wall_clock_seconds on the job if it legitimately needs longer."
            )

        if _timeout_kind == "inactivity":
            # Build diagnostic summary from the agent's activity tracker.
            _activity = {}
            if hasattr(agent, "get_activity_summary"):
                try:
                    _activity = agent.get_activity_summary()
                except Exception:
                    pass
            _last_desc = _activity.get("last_activity_desc", "unknown")
            _secs_ago = _activity.get("seconds_since_activity", 0)
            _cur_tool = _activity.get("current_tool")
            _iter_n = _activity.get("api_call_count", 0)
            _iter_max = _activity.get("max_iterations", 0)

            logger.error(
                "Job '%s' idle for %.0fs (inactivity limit %.0fs) "
                "| last_activity=%s | iteration=%s/%s | tool=%s",
                job_name, _secs_ago, _cron_inactivity_limit,
                _last_desc, _iter_n, _iter_max,
                _cur_tool or "none",
            )
            if hasattr(agent, "interrupt"):
                agent.interrupt("Cron job timed out (inactivity)")
            raise TimeoutError(
                f"Cron job '{job_name}' idle for "
                f"{int(_secs_ago)}s (limit {int(_cron_inactivity_limit)}s) "
                f"— last activity: {_last_desc}"
            )

        final_response = result.get("final_response", "") or ""
        # Strip leaked placeholder text that upstream may inject on empty completions.
        if final_response.strip() == "(No response generated)":
            final_response = ""

        # The agent swallows some failures (a spent API budget, a provider
        # outage) and returns the error AS its final text instead of raising.
        # Without this the run counts as "ok": the 9:09 Heartbeat whose whole
        # response was "API call failed after 3 retries" showed green, the
        # Errored filter said "none", and the consecutive-failure alert never
        # armed. A run whose entire output is a runtime failure sentinel is a
        # failed run.
        _resp_error = _response_failure_reason(final_response)
        if _resp_error:
            logger.warning("Job '%s' response is a failure signal: %s", job_name, _resp_error)
            if _p7_task_end:
                try:
                    _p7_task_end(_cron_session_id, "failed")
                except Exception:
                    pass
            return False, _wrap_job_output(
                job_name, job_id, prompt, final_response,
                job.get("schedule_display", "N/A"),
            ), "", _resp_error
        # Use a separate variable for log display; keep final_response clean
        # for delivery logic (empty response = no delivery).
        logged_response = final_response if final_response else "(No response generated)"
        
        output = f"""# Cron Job: {job_name}

**Job ID:** {job_id}
**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}
**Schedule:** {job.get('schedule_display', 'N/A')}

## Prompt

{prompt}

## Response

{logged_response}
"""
        
        logger.info("Job '%s' completed successfully", job_name)
        if _p7_task_end:
            try:
                _p7_task_end(_cron_session_id, "completed")
            except Exception:
                pass
        return True, output, final_response, None
        
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.exception("Job '%s' failed: %s", job_name, error_msg)
        
        output = f"""# Cron Job: {job_name} (FAILED)

**Job ID:** {job_id}
**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}
**Schedule:** {job.get('schedule_display', 'N/A')}

## Prompt

{prompt}

## Error

```
{error_msg}
```
"""
        if _p7_task_end:
            try:
                _p7_task_end(_cron_session_id, "failed", error_msg)
            except Exception:
                pass
        return False, output, "", error_msg


    finally:
        # Clean up injected env vars so they don't leak to other jobs
        for key in (
            "HERMES_SESSION_PLATFORM",
            "HERMES_SESSION_CHAT_ID",
            "HERMES_SESSION_CHAT_NAME",
            "HERMES_CRON_AUTO_DELIVER_PLATFORM",
            "HERMES_CRON_AUTO_DELIVER_CHAT_ID",
            "HERMES_CRON_AUTO_DELIVER_THREAD_ID",
        ):
            os.environ.pop(key, None)
        if _session_db:
            try:
                _session_db.end_session(_cron_session_id, "cron_complete")
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to end session: %s", job_id, e)
            try:
                _session_db.close()
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to close SQLite session store: %s", job_id, e)


def tick(verbose: bool = True, adapters=None, loop=None) -> int:
    """
    Check and run all due jobs.
    
    Uses a file lock so only one tick runs at a time, even if the gateway's
    in-process ticker and a standalone daemon or manual tick overlap.
    
    Args:
        verbose: Whether to print status messages
        adapters: Optional dict mapping Platform → live adapter (from gateway)
        loop: Optional asyncio event loop (from gateway) for live adapter sends
    
    Returns:
        Number of jobs executed (0 if another tick is already running)
    """
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)

    # Cross-platform file locking: fcntl on Unix, msvcrt on Windows
    lock_fd = None
    try:
        lock_fd = open(_LOCK_FILE, "w")
        if fcntl:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        logger.debug("Tick skipped — another instance holds the lock")
        if lock_fd is not None:
            lock_fd.close()
        return 0

    try:
        due_jobs = get_due_jobs()

        if verbose and not due_jobs:
            logger.info("%s - No jobs due", _hermes_now().strftime('%H:%M:%S'))
            return 0

        if verbose:
            logger.info("%s - %s job(s) due", _hermes_now().strftime('%H:%M:%S'), len(due_jobs))

        executed = 0
        for job in due_jobs:
            try:
                # For recurring jobs (cron/interval), advance next_run_at to the
                # next future occurrence BEFORE execution.  This way, if the
                # process crashes mid-run, the job won't re-fire on restart.
                # One-shot jobs are left alone so they can retry on restart.
                advance_next_run(job["id"])

                if job.get("type") == "meeting_join":
                    # Deterministic dial — no model. See cron/meeting_join.py.
                    from cron.meeting_join import run_meeting_join
                    _mj_agent = str(job.get("agent") or "thoth").lower() or "thoth"
                    success, output, final_response, error = run_meeting_join(job, _mj_agent)
                    # The executor consumes a single-use grant in-place; persist
                    # the mutated job so a restart can't re-dial on the same grant.
                    try:
                        _persist_job_grants(job)
                    except Exception:
                        logger.warning("could not persist consumed meeting grant for %s",
                                       job.get("id"), exc_info=True)
                else:
                    success, output, final_response, error = run_job(job)

                output_file = save_job_output(job["id"], output)
                if verbose:
                    logger.info("Output saved to: %s", output_file)

                # Deliver the final response to the origin/target chat.
                # If the agent responded with [SILENT], skip delivery (but
                # output is already saved above).  Failed jobs always deliver.
                deliver_content = final_response if success else f"⚠️ Cron job '{job.get('name', job['id'])}' failed:\n{error}"
                should_deliver = bool(deliver_content)
                if should_deliver and success and SILENT_MARKER in deliver_content.strip().upper():
                    logger.info("Job '%s': agent returned %s — skipping delivery", job["id"], SILENT_MARKER)
                    should_deliver = False

                delivery_error = None
                if should_deliver:
                    try:
                        delivery_error = _deliver_result(job, deliver_content, adapters=adapters, loop=loop)
                    except Exception as de:
                        delivery_error = str(de)
                        logger.error("Delivery failed for job %s: %s", job["id"], de)

                # Treat empty final_response as a soft failure so last_status
                # is not "ok" — the agent ran but produced nothing useful.
                # (issue #8585)
                if success and not final_response:
                    success = False
                    error = "Agent completed but produced empty response (model error, timeout, or misconfiguration)"

                mark_job_run(job["id"], success, error, delivery_error=delivery_error)
                executed += 1

            except Exception as e:
                logger.error("Error processing job %s: %s", job['id'], e)
                mark_job_run(job["id"], False, str(e))

        return executed
    finally:
        if fcntl:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        elif msvcrt:
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        lock_fd.close()


if __name__ == "__main__":
    tick(verbose=True)
