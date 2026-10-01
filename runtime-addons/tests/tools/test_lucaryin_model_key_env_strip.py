"""Lucaryin, review F09 (model lock): no non-DeepSeek model key reaches a child the
model can drive.

On Sep 25 Ptah's ``execute_code`` posted rendered documents and a client's live
CRM screenshots to Google ``gemini-2.5-flash:generateContent``. The Gemini key is
on every box on purpose (image, Veo video and TTS generation), and the app also
provisions an OpenAI key (speech-to-text) and, until the model-lock heal, an
Anthropic key. None of them may be usable as a model key from a terminal,
background/PTY or ``execute_code`` child: there the agent can call any endpoint
it likes. Upstream strips them today (``_STATIC_PROVIDER_ENV_BLOCKLIST`` + the
provider registry loop in ``tools/environments/local_env_policy.py``); this pins
it, so an upstream bump or a registry change that let one through fails here
first. Every spawn surface is covered, and a skill/config ``env_passthrough``
registration cannot re-allow them. DEEPSEEK_API_KEY is stripped the same way
(the agent's own model calls run in-process, never through a child).

Not covered: upstream's ``_HERMES_FORCE_<NAME>`` opt-in, which carries a value
the CALLER supplies (operator terminal config), never the process's own key;
and how a child may READ a key file, which is the bridge approval gate's
business.

Bare tier: env-dict assertions plus one real child process.
"""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from tools.environments.local import (
    _make_run_env,
    _sanitize_subprocess_env,
    build_subprocess_env,
    hermes_subprocess_env,
)

MODEL_KEYS = {
    "GEMINI_API_KEY": "gemini-not-a-real-key",
    "GOOGLE_API_KEY": "google-not-a-real-key",
    "ANTHROPIC_API_KEY": "anthropic-not-a-real-key",
    "OPENAI_API_KEY": "openai-not-a-real-key",
    "DEEPSEEK_API_KEY": "deepseek-not-a-real-key",
}
KEEP = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "GEMINI_MODEL_NOTE": "not a key"}


def _env():
    return {**KEEP, **MODEL_KEYS}


def _assert_no_model_keys(env, surface):
    leaked = sorted(k for k in MODEL_KEYS if k in env)
    assert leaked == [], f"{surface} child env carries {leaked}"
    assert env.get("GEMINI_MODEL_NOTE") == "not a key", f"{surface}: an unrelated var was stripped too"


def test_terminal_child_env_has_no_model_keys():
    with patch.dict(os.environ, _env(), clear=False):
        _assert_no_model_keys(_make_run_env({}), "terminal (_make_run_env)")


def test_background_pty_and_execute_code_env_has_no_model_keys():
    # _sanitize_subprocess_env: background/PTY spawns, search workers, script runners,
    # and execute_code's child (the surface Ptah's Gemini calls ran from).
    _assert_no_model_keys(_sanitize_subprocess_env(_env()), "_sanitize_subprocess_env")
    _assert_no_model_keys(build_subprocess_env(_env()), "build_subprocess_env")


def test_non_terminal_spawns_have_no_model_keys():
    with patch.dict(os.environ, _env(), clear=False):
        _assert_no_model_keys(hermes_subprocess_env(), "hermes_subprocess_env")


def test_env_passthrough_cannot_reallow_a_model_key():
    from tools import env_passthrough

    env_passthrough.clear_env_passthrough()
    try:
        env_passthrough.register_env_passthrough(list(MODEL_KEYS))
        for key in MODEL_KEYS:
            assert not env_passthrough.is_env_passthrough(key), f"{key} registered as passthrough"
        with patch.dict(os.environ, _env(), clear=False):
            _assert_no_model_keys(_make_run_env({}), "terminal after a passthrough registration")
        _assert_no_model_keys(_sanitize_subprocess_env(_env()), "subprocess after a passthrough registration")
    finally:
        env_passthrough.clear_env_passthrough()


def test_a_real_child_sees_none_of_them():
    env = _sanitize_subprocess_env(_env())
    code = "import os, json; print(json.dumps(sorted(k for k in %r if os.environ.get(k))))" % sorted(MODEL_KEYS)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


@pytest.mark.parametrize("key", sorted(MODEL_KEYS))
def test_each_model_key_is_on_the_hermes_blocklist(key):
    from tools.environments.local_env_policy import _HERMES_PROVIDER_ENV_BLOCKLIST

    assert key in _HERMES_PROVIDER_ENV_BLOCKLIST
