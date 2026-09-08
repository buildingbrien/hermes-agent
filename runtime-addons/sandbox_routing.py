"""sandbox_routing.py — FD1 P1b: route a shell/code request to the per-agent
sandbox executor when it's ready, else tell the caller to run in-process.

This is the thin primitive the two hot-path tools (execute_code, terminal) use:

    import sandbox_routing
    r = sandbox_routing.maybe_run("execute_code", code, cwd=cwd, timeout=timeout)
    if r is not None:
        # format r["stdout"]/r["stderr"]/r["exit_code"]/r["timed_out"] into the
        # tool's own return shape and return it
        ...
    # else: fall through to the current in-process subprocess path

FAIL-SAFE BY CONSTRUCTION: maybe_run() returns None on ANY problem — flag off,
no socket, executor unreachable, protocol error — so a shell call NEVER breaks
because of the sandbox; it just runs in-process as it does today. The socket
only exists once the base-image step created the users + launchd daemon AND the
app staged the executor, so on a partially-set-up machine this is transparently
a no-op. (docs/sprint1-fd1-egress-boundary-design.md)
"""
from __future__ import annotations

import os
from pathlib import Path

AGENT_SANDBOX_FLAG = "AGENT_SANDBOX_USER"
SANDBOX_HOME_ROOT = "/Users/Shared/lucaryin-sandbox"   # matches agent-sandbox-setup.ts


def enabled() -> bool:
    v = (os.environ.get(AGENT_SANDBOX_FLAG) or "").strip().lower()
    return v in ("1", "true", "on", "yes")


def agent_id() -> str:
    """Which agent is this runtime? HERMES_HOME is <root>/profiles/<id> for a
    profile agent, or ~/.hermes for thoth."""
    try:
        from hermes_constants import get_hermes_home
        p = Path(str(get_hermes_home()))
    except Exception:
        p = Path(os.environ.get("HERMES_HOME", "")) if os.environ.get("HERMES_HOME") else Path.home() / ".hermes"
    return p.name if p.parent.name == "profiles" else "thoth"


def socket_path(aid: str = "") -> str:
    return os.path.join(SANDBOX_HOME_ROOT, aid or agent_id(), "executor.sock")


def maybe_run(kind: str, payload: str, cwd: str = "", timeout: float = 600):
    """Run `payload` (a terminal command or python code) in this agent's sandbox
    executor and return {stdout, stderr, exit_code, timed_out}; or None if the
    sandbox isn't available (caller runs in-process). Never raises."""
    try:
        if not enabled():
            return None
        sock = socket_path()
        if not os.path.exists(sock):
            return None
        from sandbox_executor import run_in_sandbox
        return run_in_sandbox(sock, kind, payload, cwd=cwd, timeout=timeout)
    except Exception:
        # fail-safe: ANY sandbox problem -> in-process fallback, never break a call
        return None
