"""lucaryin_filelock — one cross-process lock and one atomic replace for the addon
stores that several agent processes share (action_items, the email_send ledger).

Both used to roll their own: action_items had no lock at all (R2-2-10) and the
email ledger did ``import fcntl`` inside the send path, which raises ImportError
on Windows (the canary ring includes zenitsu). Here:

* ``exclusive_lock(lock_path)`` — fcntl.flock on POSIX, msvcrt.locking on
  Windows (``LK_LOCK`` retries for ~10 s before raising). Yields True when the
  lock is held, False when the platform or filesystem refused it: callers
  proceed unlocked rather than lose the write they were asked to make. The lock
  handle is always closed, whichever way the acquire went.
* ``replace_with_retry(src, dst)`` — ``os.replace`` that retries a Windows
  sharing violation (PermissionError while another process has ``dst`` open)
  with a short backoff, the pattern the bridge uses in
  hermes-bridge/platform_compat.py ``replace_with_retry``.
* ``unique_tmp(path)`` — a temp name no other writer can share (pid + random).
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from typing import Iterator

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


@contextlib.contextmanager
def exclusive_lock(lock_path: str) -> Iterator[bool]:
    """Hold an exclusive advisory lock on ``lock_path`` for the with-block."""
    lf = None
    held = False
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        lf = open(lock_path, "a+")
        if fcntl is not None:
            fcntl.flock(lf, fcntl.LOCK_EX)
            held = True
        elif msvcrt is not None:  # pragma: no cover - Windows
            lf.seek(0)
            msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
            held = True
    except OSError:
        held = False
    try:
        yield held
    finally:
        if lf is not None:
            if held:
                try:
                    if fcntl is not None:
                        fcntl.flock(lf, fcntl.LOCK_UN)
                    elif msvcrt is not None:  # pragma: no cover - Windows
                        lf.seek(0)
                        msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            try:
                lf.close()
            except OSError:
                pass


def unique_tmp(path: str) -> str:
    """``<path>.<pid>.<random>.tmp`` — never shared by two concurrent writers."""
    return f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"


def replace_with_retry(src: str, dst: str, *, attempts: int = 10, delay: float = 0.05) -> None:
    """``os.replace(src, dst)``, retrying PermissionError (a Windows sharing
    violation while a reader holds ``dst`` open) with backoff; re-raises the last
    error when every attempt fails."""
    for i in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))
