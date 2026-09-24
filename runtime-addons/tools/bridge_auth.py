"""bridge_auth — the one place an addon gets the bridge bearer (HA3, bug hunt round 2).

Every addon that calls a bridge (fleet_send, delegate_to_neith, ask_agent, board,
dial/meeting_notes, gbrain_search/gbrain_read on the :9050 gbrain bridge, the
cron meeting_join and p7 hooks) used to read
``BRIDGE_AUTH_TOKEN`` straight out of ``os.environ``. Patch 0025 now strips that
name from every child the runtime spawns, and the bridge (B1) publishes the token
to a file so the worker env stops being the only carrier. Readers go FILE first,
ENV second, so either half of the pair can ship first and the addons keep
working:

* file: ``$LUCARYIN_AUTH_DIR/bridge.token`` (``LUCARYIN_AUTH_DIR`` defaults to
  ``~/.lucaryin/auth``) — EXACTLY the path hermes-bridge/server.py
  ``_bridge_bearer_file_path()`` writes (``_publish_bridge_bearer_file``: the raw
  token, no trailing newline, mode 0600, rewritten at every bridge start and
  removed when no token is provisioned). Surrounding whitespace is ignored.
  ``test_bridge_auth.py`` pins the name and the directory rule to that function.
* env: ``BRIDGE_AUTH_TOKEN`` in this (worker) process, which the bridge keeps
  exporting for in-process addons.

There is deliberately no other override: a reader that looks anywhere the bridge
does not write silently falls back to the env and hides a broken pair.
An unreadable or empty file falls through to the env; nothing here ever raises.
The runtime's file tools refuse this file (patch 0008, ``_lucaryin_auth_dirs``);
this module reads it with plain ``open()``, which that deny does not touch.
"""

from __future__ import annotations

import os

AUTH_DIR_ENV = "LUCARYIN_AUTH_DIR"
BEARER_ENV = "BRIDGE_AUTH_TOKEN"
#: hermes-bridge/server.py ``_bridge_bearer_file_path()`` — the pair contract.
BEARER_FILE_NAME = "bridge.token"


def bearer_file_path() -> str:
    """Where the bridge writes the bearer: ``$LUCARYIN_AUTH_DIR`` (or
    ``~/.lucaryin/auth`` when unset or empty) joined with ``bridge.token`` — the
    same rule as the bridge's ``_bridge_bearer_file_path()``."""
    auth_dir = os.environ.get(AUTH_DIR_ENV) or os.path.join(
        os.path.expanduser("~"), ".lucaryin", "auth")
    return os.path.join(auth_dir, BEARER_FILE_NAME)


def bridge_bearer() -> str:
    """The bridge bearer: file first, then ``BRIDGE_AUTH_TOKEN``; "" when neither."""
    try:
        with open(bearer_file_path(), encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    except Exception:  # noqa: BLE001 — a decode error must fall through, never raise
        pass
    return (os.environ.get(BEARER_ENV) or "").strip()


def bridge_auth_headers(headers: dict | None = None) -> dict:
    """``headers`` (or a new dict) with ``Authorization: Bearer <token>`` added when a
    bearer is available — the shape every addon's request builder wants."""
    out = dict(headers or {})
    token = bridge_bearer()
    if token:
        out["Authorization"] = f"Bearer {token}"
    return out
