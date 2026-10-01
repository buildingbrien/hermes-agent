"""Lucaryin fold 0036 (review findings F03 / F08, 2026-10-01): credentials the
fleet actually leaked stay out of model context, and secrets no shipped skill
needs stay out of every child the model can drive.

F08: two cron runs used read_file on ~/.lucaryin/bridge.env. The DeepSeek,
OpenAI and Anthropic keys came back masked, but GEMINI_API_KEY and
GOOGLE_API_KEY (Google's newer 'AQ.' format) came back in cleartext, and so
would the Twilio auth token and the Plaid secret: read_file runs with
file_read=True, which implies code_file and skips every assignment pass. A
heartbeat also echoed a Vercel 'vcp_' token into synced chat, and Plaid
'access-production-' tokens sat in ~96 tool outputs.

F03: `env` in a mobile turn printed a live Stripe key; the Twilio auth token,
the memory engine's embedding key and the Supabase service-role key were one
`printenv` away in every terminal child.

Every value below is built by concatenation at run time: nothing here is, or
looks like, a live key in the source.
"""

import os
from unittest.mock import patch

import pytest

from agent.redact import (
    names_credential_store,
    redact_code_output,
    redact_sensitive_text,
    redact_terminal_output,
)
from tools.environments.local import _make_run_env, _sanitize_subprocess_env, hermes_subprocess_env
from tools.environments.local_env_policy import _is_lucaryin_worker_secret
from tools.process_registry import ProcessRegistry

ALNUM = "Zq7Xk2Lm9Pw4Rt6Yv8Nb3Hc5Jd1Fg0SaTeUoIy" * 3
HEX = "3f9a1c7e5b2d8046" * 4
UUID = "3f9a1c7e-5b2d-4804-6a1c-7e5b2d80463f"

NEW_SHAPES = {
    "google AQ.": "AQ" + "." + ALNUM[:53],
    "vercel vcp_": "vcp" + "_" + ALNUM[:24],
    "vercel vci_": "vci" + "_" + ALNUM[:30],
    "vercel vca_": "vca" + "_" + ALNUM[:30],
    "vercel vcr_": "vcr" + "_" + ALNUM[:30],
    "vercel vck_": "vck" + "_" + ALNUM[:30],
    "stripe rk_test_": "rk" + "_test_" + ALNUM[:24],
    "stripe whsec_": "whsec" + "_" + ALNUM[:32],
    "plaid production": "access" + "-production-" + UUID,
    "plaid development": "access" + "-development-" + UUID,
    "plaid sandbox": "access" + "-sandbox-" + UUID,
}


class TestNewTokenShapes:
    @pytest.mark.parametrize("label", sorted(NEW_SHAPES))
    @pytest.mark.parametrize("mode", [{}, {"code_file": True}, {"file_read": True}])
    def test_shape_never_reaches_context(self, label, mode):
        secret = NEW_SHAPES[label]
        for text in (secret, f"key={secret}", f"the note said {secret} today", f'{{"token": "{secret}"}}'):
            out = redact_sensitive_text(text, force=True, **mode)
            assert secret not in out, (label, mode, out)
            assert secret[-8:] not in out, (label, mode, out)

    def test_file_read_uses_the_non_reusable_sentinel(self):
        out = redact_sensitive_text(f"GEMINI_API_KEY={NEW_SHAPES['google AQ.']}", force=True, file_read=True)
        assert "«redacted:" in out

    @pytest.mark.parametrize("text", [
        "The AQ. prefix is Google's newer key format",
        "access-production- tokens are Plaid's",
        "vcp_ is a Vercel prefix",
        "whsec_ secrets sign Stripe webhooks",
    ])
    def test_prose_about_the_prefix_is_untouched(self, text):
        assert redact_sensitive_text(text, force=True) == text


class TestTwilioPair:
    SID = "AC" + HEX[:32]
    TOKEN = ALNUM[:32]

    @pytest.mark.parametrize("mode", [{}, {"code_file": True}, {"file_read": True}])
    def test_basic_auth_pair_masks_the_token_and_keeps_the_sid(self, mode):
        text = f"curl -u {self.SID}:{self.TOKEN} https://api.twilio.com/2010-04-01/Accounts.json"
        out = redact_sensitive_text(text, force=True, **mode)
        assert self.TOKEN not in out
        assert f"{self.SID}:" in out

    def test_a_sid_alone_is_an_identifier(self):
        text = f"account {self.SID} is active"
        assert redact_sensitive_text(text, force=True) == text


# read_file content as the tool returns it ("<n>|" gutter), and a search hit.
BRIDGE_ENV_READ = "\n".join([
    f"1|GEMINI_API_KEY={NEW_SHAPES['google AQ.']}",
    f"2|TWILIO_AUTH_TOKEN={HEX[:32]}",
    f"3|PLAID_CLIENT_SECRET={HEX[:30]}",
    "4|ZEROENTROPY_API_KEY=ze_" + ALNUM[:30],
    "5|TWILIO_PHONE_NUMBER=+15550001111",
    "6|MAX_TOKENS=4096",
    "7|PLAID_ENVIRONMENT=production",
    "8|export FLEET_EMAIL_PASSWORD='" + ALNUM[:18] + "'",
])


class TestFileReadOfADotenvStore:
    def test_every_secret_line_is_masked(self):
        out = redact_sensitive_text(BRIDGE_ENV_READ, force=True, file_read=True)
        for secret in (NEW_SHAPES["google AQ."], HEX[:32], HEX[:30], "ze_" + ALNUM[:30], ALNUM[:18]):
            assert secret not in out, out
        assert out.count("«redacted") >= 5

    def test_the_names_and_non_secrets_survive(self):
        out = redact_sensitive_text(BRIDGE_ENV_READ, force=True, file_read=True)
        for kept in ("2|TWILIO_AUTH_TOKEN=", "6|MAX_TOKENS=4096", "7|PLAID_ENVIRONMENT=production"):
            assert kept in out, out

    def test_a_search_hit_line_is_a_dotenv_line_too(self):
        hit = f"/Users/x/.lucaryin/bridge.env:2:TWILIO_AUTH_TOKEN={HEX[:32]}"
        out = redact_sensitive_text(hit, force=True, file_read=True)
        assert HEX[:32] not in out and out.startswith("/Users/x/.lucaryin/bridge.env:2:TWILIO_AUTH_TOKEN=")

    @pytest.mark.parametrize("source", [
        '12|API_KEY = "fixture-value-1234567890"',
        "13|    self.token=compute_token_from_seed(seed)",
        '14|CONFIG = {"API_KEY": "fixture-value-1234567890"}',
        "15|url = f\"https://x.example/?key={key}\"",
    ])
    def test_source_code_is_not_a_dotenv_line(self, source):
        assert redact_sensitive_text(source, force=True, file_read=True) == source


class TestTerminalCatOfANamedEnvFile:
    @pytest.mark.parametrize("command", [
        "cat ~/.lucaryin/bridge.env",
        "python3 -c \"print(open('/Users/x/.lucaryin/bridge.env').read())\"",
        "set -a; . ~/.lucaryin/bridge.env; set +a; env | grep TWILIO",
        "source ~/.lucaryin/bridge.env && printenv",
        "awk 1 ~/.lucaryin/bridge.env",
    ])
    def test_any_command_naming_a_store_runs_the_assignment_pass(self, command):
        """Review of the round-1 fix: only cat/grep/head-style readers turned
        the assignment passes on; `python -c print(open(...bridge.env))` and
        a sourced store came back in cleartext."""
        out = redact_terminal_output(BRIDGE_ENV_DUMP, command)
        for secret in BRIDGE_ENV_SECRETS:
            assert secret not in out, (command, out)
        assert "TWILIO_PHONE_NUMBER=" in out and "MAX_TOKENS=4096" in out

    def test_cat_bridge_env_runs_the_assignment_pass(self):
        out = redact_terminal_output(f"TWILIO_AUTH_TOKEN={HEX[:32]}\n", "cat ~/.lucaryin/bridge.env")
        assert HEX[:32] not in out

    def test_env_example_stays_a_template(self):
        out = redact_terminal_output("MISTRAL_API_KEY=placeholder_value_here", "cat .env.example")
        assert "placeholder_value_here" in out


# ── F03: no child sees secrets no shipped skill needs ─────────────────────────

# Not named by upstream's blocklist before this fold: they reached every child.
NOT_NEEDED = {
    "ZEROENTROPY_API_KEY": "ze-not-a-real-key",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-not-a-real-key",
    "SUPABASE_SERVICE_KEY": "service-role-not-a-real-key",
    "STRIPE_SECRET_KEY": "stripe-not-a-real-key",
    "STRIPE_API_KEY": "stripe-not-a-real-key",
    "STRIPE_RESTRICTED_KEY": "stripe-not-a-real-key",
    "STRIPE_WEBHOOK_SECRET": "stripe-not-a-real-secret",
    "VERCEL_API_TOKEN": "vercel-not-a-real-token",
    "VERCEL_ACCESS_TOKEN": "vercel-not-a-real-token",
    "TWILIO_API_SECRET": "twilio-not-a-real-secret",
    "TWILIO_API_KEY_SECRET": "twilio-not-a-real-secret",
}
# Already stripped from terminal / background / execute_code children by
# upstream (provider and tool keys). Held here as a regression guard for the
# surfaces the review is about; upstream lets them through only on the
# inherit_credentials path, by design, and this fold leaves that alone.
UPSTREAM_STRIPPED = {
    "GEMINI_API_KEY": "gemini-not-a-real-key",
    "GOOGLE_API_KEY": "google-not-a-real-key",
    "DEEPSEEK_API_KEY": "deepseek-not-a-real-key",
    "TWILIO_AUTH_TOKEN": "twilio-not-a-real-token",
    "VERCEL_TOKEN": "vercel-not-a-real-token",
    "ELEVENLABS_API_KEY": "el-not-a-real-key",
    "TAVILY_API_KEY": "tvly-not-a-real-key",
}
# Shipped skills read these in a terminal child; stripping them breaks the skill.
SKILL_ENV = {
    "PLAID_CLIENT_ID": "plaid-id",
    "PLAID_CLIENT_SECRET": "plaid-client-secret",
    "AIRTABLE_API_KEY": "airtable-key",
    "BUFFER_TOKEN": "buffer-token",
    "REDDIT_CLIENT_ID": "reddit-id",
    "REDDIT_CLIENT_SECRET": "reddit-secret",
    "FLEET_EMAIL": "thoth@fleet.example",
}
KEEP = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
        "HERMES_HOME": os.environ.get("HERMES_HOME", "/tmp/hh"), "LANG": "C.UTF-8"}


@pytest.fixture
def worker_environ():
    with patch.dict(os.environ, {**KEEP, **SKILL_ENV, **NOT_NEEDED, **UPSTREAM_STRIPPED}, clear=True):
        yield


def _leaked(env, names=None):
    names = NOT_NEEDED if names is None else names
    return sorted(k for k in env if k.upper() in names)


class TestNonRequiredSecretsAreStripped:
    @pytest.mark.parametrize("name", sorted(NOT_NEEDED))
    def test_recognised(self, name):
        assert _is_lucaryin_worker_secret(name) and _is_lucaryin_worker_secret(name.lower())

    @pytest.mark.parametrize("name", sorted(SKILL_ENV))
    def test_what_shipped_skills_read_is_not(self, name):
        assert not _is_lucaryin_worker_secret(name)

    def test_every_spawn_surface(self, worker_environ):
        for env in (_make_run_env({}), ProcessRegistry._spawn_env({}),
                    _sanitize_subprocess_env(os.environ, {}),
                    hermes_subprocess_env(inherit_credentials=False),
                    hermes_subprocess_env(inherit_credentials=True)):
            assert _leaked(env) == [], _leaked(env)

    def test_terminal_and_code_children_see_no_provider_key_either(self, worker_environ):
        for env in (_make_run_env({}), ProcessRegistry._spawn_env({}),
                    _sanitize_subprocess_env(os.environ, {}),
                    hermes_subprocess_env(inherit_credentials=False)):
            assert _leaked(env, UPSTREAM_STRIPPED) == [], _leaked(env, UPSTREAM_STRIPPED)

    def test_terminal_children_keep_what_shipped_skills_read(self, worker_environ):
        for env in (_make_run_env({}), ProcessRegistry._spawn_env({})):
            missing = sorted(k for k, v in SKILL_ENV.items() if env.get(k) != v)
            assert missing == [], missing

    def test_cannot_be_forced_back(self, worker_environ):
        env = _make_run_env({"STRIPE_SECRET_KEY": "again", "_HERMES_FORCE_TWILIO_AUTH_TOKEN": "forced"})
        assert _leaked(env) == [], _leaked(env)


# ── round 2: execute_code, a store named anywhere, the Plaid secret ─────────

# What `print(open(bridge.env).read())` prints (no read_file gutter).
BRIDGE_ENV_SECRETS = [
    HEX[:32],                     # TWILIO_AUTH_TOKEN
    ALNUM[:60],                   # BRIDGE_AUTH_TOKEN
    HEX[:30],                     # PLAID_SECRET
    ALNUM[3:21],                  # IMESSAGE_PASSWORD
    "ze_" + ALNUM[:30],           # ZEROENTROPY_API_KEY
]
BRIDGE_ENV_DUMP = "\n".join([
    f"TWILIO_AUTH_TOKEN={BRIDGE_ENV_SECRETS[0]}",
    f"BRIDGE_AUTH_TOKEN={BRIDGE_ENV_SECRETS[1]}",
    f"PLAID_SECRET={BRIDGE_ENV_SECRETS[2]}",
    f"IMESSAGE_PASSWORD={BRIDGE_ENV_SECRETS[3]}",
    f"ZEROENTROPY_API_KEY={BRIDGE_ENV_SECRETS[4]}",
    "TWILIO_PHONE_NUMBER=+15550001111",
    "MAX_TOKENS=4096",
]) + "\n"


class TestExecuteCodeOutput:
    def test_dotenv_lines_are_judged_even_when_no_store_is_named(self):
        out = redact_code_output(BRIDGE_ENV_DUMP, "print(blob)")
        for secret in BRIDGE_ENV_SECRETS:
            assert secret not in out, out
        assert "MAX_TOKENS=4096" in out

    def test_a_cell_that_names_a_store_gets_every_pass(self):
        """F03: Ptah read the keys into context with execute_code 21 times."""
        code = ("from dotenv import dotenv_values\n"
                "print(dict(dotenv_values('/Users/x/.lucaryin/bridge.env')))")
        dumped = "{" + ", ".join(
            f"'{k}': '{v}'" for k, v in (
                ("TWILIO_AUTH_TOKEN", BRIDGE_ENV_SECRETS[0]),
                ("BRIDGE_AUTH_TOKEN", BRIDGE_ENV_SECRETS[1]),
                ("IMESSAGE_PASSWORD", BRIDGE_ENV_SECRETS[3]))) + "}"
        out = redact_code_output(dumped, code)
        for secret in (BRIDGE_ENV_SECRETS[0], BRIDGE_ENV_SECRETS[1], BRIDGE_ENV_SECRETS[3]):
            assert secret not in out, out

    def test_source_code_output_is_left_alone(self):
        text = 'API_KEY = "fixture-value-1234567890"\nCONFIG = {"apiKey": "x"}\n'
        assert redact_code_output(text, "print(open('app.py').read())") == text

    @pytest.mark.parametrize("code,named", [
        ("open('/Users/x/.lucaryin/bridge.env')", True),
        ("load_dotenv(os.path.expanduser('~/.hermes/.env'))", True),
        ("Path.home() / '.lucaryin/auth/google.token'", True),
        ("open('.env.example')", False),
        ("self.env = {}; process.env.HOME", False),
        ("print(os.environ['HOME'])", False),
    ])
    def test_what_counts_as_naming_a_store(self, code, named):
        assert names_credential_store(code) is named


@pytest.fixture
def _kernels():
    from tools.code_kernel import shutdown_all_kernels
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


@pytest.mark.skipif(os.name == "nt", reason="session kernels use UDS")
def test_a_bridge_env_dump_through_execute_code_is_masked(tmp_path, monkeypatch, _kernels):
    """The review's replay, end to end: a cell prints a bridge.env."""
    import json as _json
    from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS, execute_code
    monkeypatch.setenv("TERMINAL_ENV", "local")
    store = tmp_path / ".lucaryin" / "bridge.env"
    store.parent.mkdir()
    store.write_text(BRIDGE_ENV_DUMP, encoding="utf-8")
    for code in (f"print(open({str(store)!r}).read())",
                 # the same dump from a cell that does not name the store
                 f"import base64\nprint(base64.b64decode({__import__('base64').b64encode(BRIDGE_ENV_DUMP.encode()).decode()!r}).decode())"):
        result = _json.loads(execute_code(code=code, task_id="lucaryin-0036",
                                          enabled_tools=list(SANDBOX_ALLOWED_TOOLS)))
        assert result["status"] == "success", result
        for secret in BRIDGE_ENV_SECRETS:
            assert secret not in result["output"], result["output"]
        assert "TWILIO_PHONE_NUMBER=" in result["output"]


class TestPlaidClientSecret:
    @pytest.mark.parametrize("mode", [{}, {"code_file": True}, {"file_read": True}])
    @pytest.mark.parametrize("text", [
        'SECRET = "{s}"',
        "creds = {{'client_id': 'abc', 'secret': '{s}'}}",
        '{{"client_secret": "{s}"}}',
        "PLAID_SECRET={s}",
        "12|PLAID_CLIENT_SECRET = '{s}'",
    ])
    def test_a_labelled_30_hex_secret_is_masked_in_every_mode(self, text, mode):
        """F08: the heartbeat's _hb_*.py held it as SECRET = "…"; read_file
        masked the access token and returned the client secret."""
        secret = HEX[:30]
        out = redact_sensitive_text(text.format(s=secret), force=True, **mode)
        assert secret not in out, out

    def test_30_hex_with_no_secret_label_is_left_alone(self):
        text = f"build {HEX[:30]} passed"
        assert redact_sensitive_text(text, force=True) == text


class TestGoogleOAuthTokens:
    @pytest.mark.parametrize("mode", [{}, {"code_file": True}, {"file_read": True}])
    def test_access_and_refresh_tokens_are_masked(self, mode):
        """The credential class the F01 skill pointed at (google.token)."""
        access = "ya29" + "." + ALNUM[:80]
        refresh = "1//0" + ALNUM[:60]
        out = redact_sensitive_text(
            f'{{"access_token": "{access}", "refresh_token": "{refresh}"}}', force=True, **mode)
        assert access not in out and refresh not in out and ALNUM[40:60] not in out, out


class TestNoSentinelWriteBack:
    """A masked read written back whole would overwrite the real values."""

    def _tools(self, monkeypatch, tmp_path):
        import tools.file_tools as ft
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.chdir(tmp_path)
        return ft

    def test_write_file_refuses_new_sentinels(self, monkeypatch, tmp_path):
        import json as _json
        ft = self._tools(monkeypatch, tmp_path)
        target = tmp_path / "service.env"
        target.write_text(f"API_TOKEN={ALNUM[:40]}\nDEBUG=1\n", encoding="utf-8")
        masked = redact_sensitive_text(target.read_text(encoding="utf-8"), force=True, file_read=True)
        assert "«redacted" in masked
        out = _json.loads(ft.write_file_tool(str(target), masked + "NEW=1\n", task_id="t0036"))
        assert "redaction marker" in out.get("error", ""), out
        assert ALNUM[:40] in target.read_text(encoding="utf-8")

    def test_patch_refuses_a_new_sentinel_but_edits_around_secrets(self, monkeypatch, tmp_path):
        import json as _json
        ft = self._tools(monkeypatch, tmp_path)
        target = tmp_path / "service.env"
        target.write_text(f"API_TOKEN={ALNUM[:40]}\nDEBUG=1\n", encoding="utf-8")
        out = _json.loads(ft.patch_tool(mode="replace", path=str(target), old_string="DEBUG=1",
                                        new_string="DEBUG=1\nAPI_TOKEN=«redacted-secret»",
                                        task_id="t0036"))
        assert "redaction marker" in out.get("error", ""), out
        out = _json.loads(ft.patch_tool(mode="replace", path=str(target), old_string="DEBUG=1",
                                        new_string="DEBUG=0", task_id="t0036"))
        assert not out.get("error"), out
        assert ALNUM[:40] in target.read_text(encoding="utf-8")

    def test_a_file_that_already_holds_the_marker_text_can_be_edited(self, monkeypatch, tmp_path):
        import json as _json
        ft = self._tools(monkeypatch, tmp_path)
        target = tmp_path / "notes.md"
        target.write_text("The mask looks like «redacted-secret».\n", encoding="utf-8")
        out = _json.loads(ft.write_file_tool(
            str(target), "The mask looks like «redacted-secret». Updated.\n", task_id="t0036"))
        assert not out.get("error"), out


# ── Round 4: prefixed lower-case credential labels, and browser_exec output ──

ZE_KEY = "ze" + "_" + ALNUM[:28]


class TestPrefixedCredentialLabels:
    """The memory engine's config is JSON with lower-case keys: a cat or a
    Python read of it came back with the embedding key in clear, because
    terminal and execute_code output are code_file (no JSON pass) and
    upstream's JSON pass names only a bare api_key / token / secret."""

    CONFIG = '{"engine": "pglite", "zeroentropy_api_key": "%s", "embedding_model": "x"}' % ZE_KEY

    def test_terminal_and_execute_code_mask_it(self):
        for out in (redact_terminal_output(self.CONFIG, "cat ~/.gbrain/config.json"),
                    redact_terminal_output(self.CONFIG, "python3 -c 'print(1)'"),
                    redact_code_output(self.CONFIG, "print(open(p).read())"),
                    redact_sensitive_text(self.CONFIG, force=True, code_file=True)):
            assert ZE_KEY not in out and ZE_KEY[-8:] not in out, out
            assert '"engine": "pglite"' in out

    @pytest.mark.parametrize("text", [
        "{'plaid_access_token': '%s'}" % ALNUM[:30],
        "supabase_service_role_key: %s" % ALNUM[:40],
        '"stripe_secret": "%s"' % ALNUM[:32],
        '"fleet_email_password" = "%s"' % ALNUM[:20],
    ])
    def test_other_prefixed_labels(self, text):
        out = redact_code_output(text, "print(x)")
        assert ALNUM[:20] not in out, out

    def test_read_file_gets_the_sentinel(self):
        out = redact_sensitive_text(self.CONFIG, force=True, file_read=True)
        assert "«redacted-secret»" in out and ZE_KEY not in out

    @pytest.mark.parametrize("text", [
        '"next_page_token": "CAEQAhoECgIIARIQ7a3b5c9d"',
        '"sync_token": "%s"' % ALNUM[:24],
        '"max_token": "4096"',
        '"api_key": "test"',
        "SERVICE_TOKEN=3JcQ1UzX9vQ2mL7pR4tY8wA1sD5fG6hJ2kSbn7Q0",   # upstream #43025: a source constant
        "CONFIG = {'BRAVE_API_KEY': 'fixture-value-1234567890'}",
    ])
    def test_positions_short_values_and_source_constants_are_not(self, text):
        assert redact_sensitive_text(text, force=True, code_file=True) == text


def test_browser_exec_output_is_masked_like_execute_code(tmp_path, monkeypatch):
    """browser_exec runs host Python; its stdout and stderr reached the model
    and state.db with keys and phone numbers in clear (round-3 review)."""
    import json
    import stat
    from tools import browser_use_cli as bu_cli

    google = NEW_SHAPES["google AQ."]
    script = tmp_path / "browser-use"
    script.write_text(
        "#!/bin/sh\ncat > /dev/null\n"
        f"echo 'key {google}'\n"
        "echo 'call +14155550123 now'\n"
        f"echo '{{\"zeroentropy_api_key\": \"{ZE_KEY}\"}}'\n"
        f"echo 'oops {google}' >&2\nexit 1\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(script)])
    # No browser behind it: the backend routing and vault supervisor are not under test.
    monkeypatch.setattr(bu_cli, "_route_backend", lambda *a, **k: None)
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda *a, **k: None)
    monkeypatch.delenv("BU_NAME", raising=False)
    result = json.loads(bu_cli.browser_exec("print(page_info())"))
    blob = json.dumps(result)
    assert google not in blob and google[-8:] not in blob, blob
    assert "+14155550123" not in blob, blob
    assert ZE_KEY not in blob, blob
    assert "call" in result["output"] and "oops" in result["stderr"]
