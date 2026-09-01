#!/usr/bin/env python3
"""sandbox_executor.py — FD1 P1b: the in-sandbox tool executor.

Runs as an unprivileged per-agent macOS user (`_lucaryin-<id>`), started by
launchd (see electron/provisioning/agent-sandbox-setup.ts). It listens on a unix
socket and executes `terminal` / `execute_code` requests IN-SANDBOX — as this
user, with this user's HOME, no principal creds on disk or in env, and no
logged-in Messages/Mail session. The bridge (running as the principal, a member
of the `_lucaryin-shared` group) connects and RPCs a command; raw shell egress
from here has no credential and no session, so the consent boundary holds
STRUCTURALLY instead of by regex (docs/sprint1-fd1-egress-boundary-design.md).

Wire protocol — one JSON object per connection, length-prefixed:
    [4-byte big-endian length][utf-8 JSON]
Request:
    {"op":"run","kind":"terminal"|"execute_code","payload":"<cmd|code>",
     "cwd":"<dir>","timeout":600}
    {"op":"ping"}
Response:
    {"stdout":"...","stderr":"...","exit_code":0,"timed_out":false}
    {"ok":true,"user":"_lucaryin-ptah"}          # to a ping

The socket is created 0660, group `_lucaryin-shared`, so only the principal
(bridge) can connect. This program NEVER holds principal creds and NEVER reaches
the principal's home — that absence is the whole point.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading

MAX_MSG = 8 * 1024 * 1024          # 8 MB cap on a request or response frame
DEFAULT_TIMEOUT = 600
SHARED_GROUP = "_lucaryin-shared"


# ── framing ───────────────────────────────────────────────────────────────────
def _send(conn: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    conn.sendall(struct.pack(">I", len(data)) + data)


def _recv(conn: socket.socket) -> dict | None:
    hdr = _recv_exact(conn, 4)
    if hdr is None:
        return None
    (n,) = struct.unpack(">I", hdr)
    if n <= 0 or n > MAX_MSG:
        raise ValueError(f"frame length {n} out of bounds")
    body = _recv_exact(conn, n)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ── execution (runs AS this sandbox user) ─────────────────────────────────────
def _run(kind: str, payload: str, cwd: str, timeout: float) -> dict:
    home = os.path.expanduser("~")
    workdir = cwd if cwd and os.path.isdir(cwd) else home
    try:
        if kind == "execute_code":
            with tempfile.NamedTemporaryFile(
                "w", suffix=".py", dir=home, delete=False, encoding="utf-8"
            ) as f:
                f.write(payload)
                script = f.name
            try:
                proc = subprocess.run(
                    [sys.executable, script],
                    cwd=workdir, capture_output=True, text=True, timeout=timeout,
                    stdin=subprocess.DEVNULL,
                )
            finally:
                try:
                    os.unlink(script)
                except OSError:
                    pass
        elif kind == "terminal":
            proc = subprocess.run(
                ["/bin/bash", "-c", payload],
                cwd=workdir, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        else:
            return {"error": f"unknown kind {kind!r}"}
        return {
            "stdout": proc.stdout, "stderr": proc.stderr,
            "exit_code": proc.returncode, "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "") if isinstance(e.stderr, str) else "",
            "exit_code": 124, "timed_out": True,
        }
    except Exception as e:  # never let one bad request kill the executor
        return {"stdout": "", "stderr": f"sandbox executor error: {e}",
                "exit_code": 1, "timed_out": False}


def handle_request(req: dict) -> dict:
    op = req.get("op")
    if op == "ping":
        return {"ok": True, "user": _whoami()}
    if op == "run":
        kind = str(req.get("kind") or "")
        payload = req.get("payload")
        if not isinstance(payload, str) or not payload:
            return {"stdout": "", "stderr": "missing payload", "exit_code": 1, "timed_out": False}
        cwd = str(req.get("cwd") or "")
        try:
            timeout = float(req.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        return _run(kind, payload, cwd, timeout)
    return {"error": f"unknown op {op!r}"}


def _whoami() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return str(os.getuid())


def _serve_conn(conn: socket.socket) -> None:
    try:
        req = _recv(conn)
        if req is None:
            return
        _send(conn, handle_request(req))
    except Exception as e:
        try:
            _send(conn, {"stdout": "", "stderr": f"executor protocol error: {e}",
                         "exit_code": 1, "timed_out": False})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(sock_path: str) -> None:
    """Listen forever on sock_path. Socket is 0660 group-scoped."""
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    os.makedirs(os.path.dirname(sock_path), exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    _lock_down_socket(sock_path)
    srv.listen(16)
    print(f"[sandbox-executor] {_whoami()} listening on {sock_path}", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_serve_conn, args=(conn,), daemon=True).start()


def _lock_down_socket(sock_path: str) -> None:
    # group-only access: only the principal (in _lucaryin-shared) may connect.
    try:
        os.chmod(sock_path, 0o660)
    except OSError:
        pass
    try:
        import grp
        gid = grp.getgrnam(SHARED_GROUP).gr_gid
        os.chown(sock_path, -1, gid)
    except Exception:
        pass  # group may not exist in a bare test env; perms above still apply


# ── client helper (used by the runtime, which runs as the principal) ──────────
def run_in_sandbox(sock_path: str, kind: str, payload: str,
                   cwd: str = "", timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Send one run request to a sandbox executor and return its result dict.
    Raises OSError if the socket isn't reachable (caller falls back in-process)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.settimeout(timeout + 30)
        c.connect(sock_path)
        _send(c, {"op": "run", "kind": kind, "payload": payload,
                  "cwd": cwd, "timeout": timeout})
        resp = _recv(c)
        if resp is None:
            raise OSError("sandbox executor closed the connection without a reply")
        return resp


def ping_sandbox(sock_path: str, timeout: float = 5) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.settimeout(timeout)
        c.connect(sock_path)
        _send(c, {"op": "ping"})
        return _recv(c) or {}


def main() -> int:
    sock_path = (sys.argv[1] if len(sys.argv) > 1 else "") or os.environ.get("SANDBOX_SOCKET", "")
    if not sock_path:
        print("usage: sandbox_executor.py <socket-path>  (or $SANDBOX_SOCKET)", file=sys.stderr)
        return 2
    serve(sock_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
