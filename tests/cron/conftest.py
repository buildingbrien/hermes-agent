"""Shared fixtures for cron scheduler tests.

tick() takes a real flock on ~/.hermes/cron/.tick.lock so only one tick runs
at a time in production. In-process, an flock is released only when every fd
referring to that inode is closed — so a test whose tick() left its lock fd
alive (uncollected) makes EVERY later tick() in the same process silently
no-op (LOCK_NB fails → returns 0, no deliver, no save). That turned
TestSilentDelivery et al. into order-dependent tests that passed or failed
purely on collection order. Unlinking the lock file before each test gives
tick() a fresh inode, so any stale fd's lock no longer applies.
"""

import pytest


@pytest.fixture(autouse=True)
def _fresh_tick_lock():
    from cron import scheduler
    try:
        scheduler._LOCK_FILE.unlink()
    except (FileNotFoundError, OSError):
        pass
    yield
    try:
        scheduler._LOCK_FILE.unlink()
    except (FileNotFoundError, OSError):
        pass
