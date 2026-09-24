"""Lucaryin fold 0025 (HA3 / R2-1-03, bug hunt round 2): the bridge worker's own
credentials never reach a child the model can drive.

BRIDGE_AUTH_TOKEN reached terminal and code children, so an agent could
``curl 127.0.0.1:<bridge>/api/approvals/<id>/decide`` and approve its own cards
or flip always-approve with no human. The same env carried the hub admin key,
the fleet bus key and mailbox password, the voice signer, the Plaid access
tokens, the iMessage relay password and the transcription key. All of them are
stripped on EVERY
spawn surface: the terminal's ``_make_run_env``, ``_sanitize_subprocess_env``
(``ProcessRegistry._spawn_env`` for background/PTY children, search workers,
script runners) and ``hermes_subprocess_env`` even with ``inherit_credentials``;
the ``_HERMES_FORCE_`` prefix cannot re-inject them. In-process addons keep
reading them from ``os.environ`` (they run in the worker, not in a child).

Exact names, not prefixes (review, 2026-09-23): the shipped plaid-banking-api
skill curls Plaid from the terminal with $PLAID_CLIENT_ID / $PLAID_CLIENT_SECRET
and email-data-feeds reads $FLEET_EMAIL, so those — and the non-secret fleet
routing / delegation vars — must still reach a terminal child (``SKILL_ENV``).

Bare tier: pure env-dict assertions plus one real child process (``env=`` is
the dict under test; the child prints which of the names it can see).
"""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from tools.environments.local import _make_run_env, _sanitize_subprocess_env, hermes_subprocess_env
from tools.environments.local_env_policy import _is_hermes_internal_secret, _is_lucaryin_worker_secret
from tools.process_registry import ProcessRegistry

WORKER_SECRETS = {
    "BRIDGE_AUTH_TOKEN": "brg-not-a-real-token",
    "LUCARYIN_API_KEY": "hub-not-a-real-key",
    "VOICE_JWT_SECRET": "voice-not-a-real-secret",
    "IMESSAGE_PASSWORD": "imsg-not-a-real-password",
    "ASSEMBLYAI_API_KEY": "aai-not-a-real-key",
    "FLEET_TOKEN": "fleet-not-a-real-token",
    "FLEET_EMAIL_PASSWORD": "mailbox-not-a-real-password",
    "PLAID_ACCESS_TOKENS": '{"bank": "access-not-a-real-token"}',
    "bridge_auth_token": "lowercase-spelling",
}
# What shipped skills read in a terminal child, and the non-secret fleet routing /
# delegation vars: stripping these broke plaid-banking-api and email-data-feeds.
SKILL_ENV = {
    "PLAID_CLIENT_ID": "plaid-id",
    "PLAID_CLIENT_SECRET": "plaid-client-secret",
    "PLAID_ENVIRONMENT": "production",
    "FLEET_EMAIL": "thoth@fleet.example",
    "FLEET_ID": "fleet-001",
    "FLEET_DELEGATION_DEPTH": "1",
    "FLEET_DELEGATION_VISITED": "thoth",
}
KEEP = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
        "HERMES_HOME": os.environ.get("HERMES_HOME", "/tmp/hh"), "LANG": "C.UTF-8", "FLEETWOOD": "a band"}


def _leaked(env: dict) -> list:
    return sorted(k for k in env if k.upper() in {n.upper() for n in WORKER_SECRETS})


@pytest.fixture
def worker_environ():
    with patch.dict(os.environ, {**KEEP, **SKILL_ENV, **WORKER_SECRETS}, clear=True):
        yield


class TestNamesAreRecognised:
    @pytest.mark.parametrize("name", sorted(WORKER_SECRETS))
    def test_every_worker_secret_is_an_internal_secret(self, name):
        assert _is_lucaryin_worker_secret(name) and _is_hermes_internal_secret(name), name

    @pytest.mark.parametrize("name", ["PATH", "HOME", "HERMES_HOME", "FLEETWOOD", "PLAIDS", "TERM", "OPENAI_API_KEY"])
    def test_unrelated_names_are_not(self, name):
        assert not _is_lucaryin_worker_secret(name), name

    @pytest.mark.parametrize("name", sorted(SKILL_ENV))
    def test_names_shipped_skills_read_are_not(self, name):
        assert not _is_lucaryin_worker_secret(name), name


class TestEverySpawnSurfaceStrips:
    def test_terminal_run_env(self, worker_environ):
        env = _make_run_env({})
        assert _leaked(env) == [], _leaked(env)
        assert "PATH" in env and env.get("FLEETWOOD") == "a band"

    def test_terminal_child_keeps_what_shipped_skills_read(self, worker_environ):
        """plaid-banking-api's curl and email-data-feeds' $FLEET_EMAIL still work."""
        for env in (_make_run_env({}), ProcessRegistry._spawn_env({})):
            missing = sorted(k for k, v in SKILL_ENV.items() if env.get(k) != v)
            assert missing == [], missing

    def test_terminal_run_env_extra_cannot_add_them_back(self, worker_environ):
        env = _make_run_env({"BRIDGE_AUTH_TOKEN": "again", "_HERMES_FORCE_BRIDGE_AUTH_TOKEN": "forced",
                             "PLAID_ACCESS_TOKENS": "again"})
        assert _leaked(env) == [], _leaked(env)

    def test_background_and_pty_spawn_env(self, worker_environ):
        env = ProcessRegistry._spawn_env({"FLEET_TOKEN": "again"})
        assert _leaked(env) == [], _leaked(env)
        env = _sanitize_subprocess_env(os.environ, {"_HERMES_FORCE_LUCARYIN_API_KEY": "forced"})
        assert _leaked(env) == [], _leaked(env)

    @pytest.mark.parametrize("inherit", [False, True])
    def test_non_terminal_surface_even_with_inherited_credentials(self, worker_environ, inherit):
        env = hermes_subprocess_env(inherit_credentials=inherit)
        assert _leaked(env) == [], _leaked(env)

    def test_a_real_child_sees_none_of_the_names(self, worker_environ):
        probe = ("import os, json; names = " + repr(sorted(WORKER_SECRETS)) +
                 "; print(json.dumps(sorted(k for k in os.environ if k.upper() in {n.upper() for n in names})))")
        for env in (_make_run_env({}), ProcessRegistry._spawn_env({})):
            out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=60)
            assert out.returncode == 0, out.stderr
            assert out.stdout.strip() == "[]", out.stdout
