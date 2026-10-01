"""Lucaryin fold 0028: the DeepSeek-only model lock (review F04; founder rule 2026-07-26).

On Sep 21, 23, 24 and 26 thoth's cron runs (Heartbeat, Meeting Recap, Inbox
Triage, Team Huddle — mail, calendar, iMessage and finance content) moved to
Anthropic claude-opus-4-6 on a single DeepSeek request timeout: config.yaml held
``fallback_model: anthropic/claude-opus-4-6``, the app had provisioned an
Anthropic key, and the cron scheduler builds a live chain from config
(``get_fallback_chain``). ``sessions.model`` and ``cron/usage_audit.jsonl``
still said deepseek-v4-pro. Pinned here, with LUCARYIN_MODEL_LOCK=deepseek (what
the app, hermes-bridge and every cron tick set):

1. every chain the runtime builds keeps DeepSeek routes only (config and a chain
   handed straight to AIAgent), and the fallback walk refuses a foreign entry
   that got in some other way — a DeepSeek outage fails closed, said once;
2. the central client router returns no client for a non-DeepSeek endpoint;
3. an AIAgent or a cron job whose primary route is not DeepSeek refuses to
   start, and a live /model switch never leaves DeepSeek;
4. a call served off DeepSeek logs exactly one MODEL-LOCK VIOLATION line;
5. the records are truthful: usage_audit.jsonl names the provider and model
   that served, and session_model_usage keeps one row per (model, provider);
6. no borrowed model login: Claude Code's and the Codex CLI's are never read
   while locked, nor with auth.adopt_external_logins: false;
7. lock unset (a stock install, upstream's suite): upstream behaviour, unchanged.

Bare tier: no network, no credentials (clients are mocks).
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import lucaryin_model_lock as lock
from hermes_cli.fallback_config import get_fallback_chain

ANTHROPIC = {"provider": "anthropic", "model": "claude-opus-4-6"}
DEEPSEEK_FLASH = {"provider": "deepseek", "model": "deepseek-flash"}
THOTH_CFG = {
    "model": {"provider": "deepseek", "default": "deepseek-v4-pro"},
    "fallback_model": dict(ANTHROPIC),
}


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setenv(lock.LOCK_ENV, "deepseek")
    lock._reset_for_tests()
    yield
    lock._reset_for_tests()


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.delenv(lock.LOCK_ENV, raising=False)
    lock._reset_for_tests()
    yield
    lock._reset_for_tests()


def _lock_records(caplog, level=logging.WARNING):
    return [r for r in caplog.records if r.name == "lucaryin.model_lock" and r.levelno >= level]


def _make_agent(*, provider="deepseek", base_url="https://api.deepseek.com/v1", fallback_model=None):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url=base_url, provider=provider, model="deepseek-v4-pro",
            quiet_mode=True, skip_context_files=True, skip_memory=True, fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


# ── the predicate ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["deepseek", "1", "on", "true", "anything-else"])
def test_any_non_off_value_turns_the_lock_on(monkeypatch, value):
    monkeypatch.setenv(lock.LOCK_ENV, value)
    assert lock.lock_active()


@pytest.mark.parametrize("value", ["", "0", "off", "OFF", "false", "no"])
def test_only_an_explicit_off_value_or_absence_turns_it_off(monkeypatch, value):
    monkeypatch.setenv(lock.LOCK_ENV, value)
    assert not lock.lock_active()
    monkeypatch.delenv(lock.LOCK_ENV)
    assert not lock.lock_active()


@pytest.mark.parametrize(("provider", "base_url", "allowed"), [
    ("deepseek", None, True),
    ("deepseek", "https://api.deepseek.com/v1", True),
    ("anthropic", "https://api.deepseek.com/anthropic", True),   # DeepSeek's Anthropic-compatible endpoint
    ("custom", "api.deepseek.com", True),
    ("DeepSeek", "", True),
    ("deepseek", "https://proxy.example.com/v1", False),          # the label does not make a foreign host DeepSeek
    ("deepseek", "https://deepseek.com.evil.example/v1", False),
    ("deepseek", "https://notdeepseek.com/v1", False),
    ("anthropic", None, False),
    ("anthropic", "https://api.anthropic.com", False),
    ("openrouter", "https://openrouter.ai/api/v1", False),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta", False),
    ("", None, False),
])
def test_route_allowed(provider, base_url, allowed):
    assert lock.route_allowed(provider, base_url) is allowed


# ── 1. fallback chains ───────────────────────────────────────────────────────


def test_config_chain_keeps_only_deepseek_and_warns_once(locked, caplog):
    cfg = {"fallback_providers": [dict(DEEPSEEK_FLASH)], "fallback_model": dict(ANTHROPIC)}
    with caplog.at_level(logging.WARNING, logger="lucaryin.model_lock"):
        assert get_fallback_chain(cfg) == [DEEPSEEK_FLASH]
        assert get_fallback_chain(cfg) == [DEEPSEEK_FLASH]
    records = _lock_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in records]
    assert "dropped non-DeepSeek fallback provider=anthropic model=claude-opus-4-6" in records[0].getMessage()
    assert get_fallback_chain(THOTH_CFG) == [], "the founder-box config: no fallback left at all"


def test_config_chain_is_upstream_when_unlocked(unlocked):
    assert get_fallback_chain(THOTH_CFG) == [ANTHROPIC]


def test_a_chain_handed_to_aiagent_is_filtered_too(locked):
    agent = _make_agent(fallback_model=[dict(ANTHROPIC), dict(DEEPSEEK_FLASH)])
    assert agent._fallback_chain == [DEEPSEEK_FLASH]
    assert agent._fallback_model == DEEPSEEK_FLASH


def test_fallback_walk_refuses_a_foreign_entry_and_fails_closed_once(locked, caplog):
    agent = _make_agent()
    agent._fallback_chain = [dict(ANTHROPIC)]  # got in some other way (gateway reload, a plugin)
    agent._fallback_index = 0
    with patch("agent.auxiliary_client.resolve_provider_client") as rpc, \
            caplog.at_level(logging.WARNING, logger="lucaryin.model_lock"):
        assert agent._try_activate_fallback() is False
        assert agent._try_activate_fallback() is False
    rpc.assert_not_called()  # never even resolved: no client, no key lookup, no request
    assert (agent.provider, agent.model) == ("deepseek", "deepseek-v4-pro")
    messages = [r.getMessage() for r in _lock_records(caplog)]
    assert any("refused fallback to provider=anthropic" in m for m in messages), messages
    assert sum("failing closed" in m for m in messages) == 1, messages


def test_fallback_walk_still_switches_between_deepseek_routes(locked):
    agent = _make_agent(fallback_model=[dict(DEEPSEEK_FLASH)])
    client = MagicMock()
    client.base_url = "https://api.deepseek.com/v1"
    client.api_key = "k"
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, "deepseek-flash")):
        assert agent._try_activate_fallback() is True
    assert (agent.provider, agent.model) == ("deepseek", "deepseek-flash")


# ── 2. the central client router ─────────────────────────────────────────────


def _fake_client(base_url):
    c = MagicMock()
    c.base_url = base_url
    return c


def test_router_returns_no_client_for_a_foreign_endpoint(locked, caplog):
    from agent import auxiliary_client as aux

    foreign = _fake_client("https://api.anthropic.com/v1")
    with patch.object(aux, "_resolve_provider_client_unlocked", return_value=(foreign, "claude-opus-4-6")), \
            caplog.at_level(logging.WARNING, logger="lucaryin.model_lock"):
        assert aux.resolve_provider_client("anthropic", model="claude-opus-4-6") == (None, None)
        # "auto" resolving to a foreign endpoint is refused the same way.
        assert aux.resolve_provider_client("auto", task="compression") == (None, None)
    assert any("refused a model client for provider=anthropic" in r.getMessage() for r in _lock_records(caplog))

    ours = _fake_client("https://api.deepseek.com/v1")
    with patch.object(aux, "_resolve_provider_client_unlocked", return_value=(ours, "deepseek-flash")):
        assert aux.resolve_provider_client("deepseek", model="deepseek-flash", is_vision=True) == (ours, "deepseek-flash")
    with patch.object(aux, "_resolve_provider_client_unlocked", return_value=(None, None)):
        assert aux.resolve_provider_client("deepseek") == (None, None)


def test_router_is_a_pass_through_when_unlocked(unlocked):
    from agent import auxiliary_client as aux

    foreign = _fake_client("https://api.anthropic.com/v1")
    with patch.object(aux, "_resolve_provider_client_unlocked", return_value=(foreign, "claude-opus-4-6")) as inner:
        assert aux.resolve_provider_client("anthropic", model="claude-opus-4-6", task="title") == (foreign, "claude-opus-4-6")
    inner.assert_called_once_with("anthropic", "claude-opus-4-6", False, False, None, None, None, None, False, "title")


# ── 3. a non-DeepSeek primary never starts ───────────────────────────────────


def test_agent_with_a_foreign_primary_refuses_to_start(locked):
    with pytest.raises(RuntimeError, match=r"MODEL-LOCK: refusing to start an agent on provider=openrouter"):
        _make_agent(provider="openrouter", base_url="https://openrouter.ai/api/v1")
    with pytest.raises(RuntimeError, match=r"host=proxy\.example\.com"):
        _make_agent(provider="deepseek", base_url="https://proxy.example.com/v1")
    assert _make_agent().provider == "deepseek"


def test_agent_with_a_foreign_primary_starts_when_unlocked(unlocked):
    assert _make_agent(provider="openrouter", base_url="https://openrouter.ai/api/v1").provider == "openrouter"


def test_cron_job_resolving_off_deepseek_fails_closed(locked):
    import cron.scheduler as sched

    job = {"id": "53d22f9a5f4a", "name": "Heartbeat", "prompt": "check in"}
    jc = SimpleNamespace(cfg={}, model="claude-opus-4-6")
    foreign = {"provider": "anthropic", "base_url": "https://api.anthropic.com", "api_key": "x"}
    with patch.object(sched, "_preflight_or_block", return_value=None), \
            patch.object(sched, "_guard_job_credential_exfil", return_value=None), \
            patch.object(sched, "_resolve_job_runtime", return_value=(foreign, "claude-opus-4-6")), \
            patch.object(sched, "_load_credential_pool") as pool:
        with pytest.raises(RuntimeError, match=r"MODEL-LOCK: cron job 'Heartbeat' \(53d22f9a5f4a\) resolves to provider=anthropic"):
            sched._resolve_cron_agent_setup(job, job["id"], job["name"], jc)
    pool.assert_not_called()

    ours = {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "api_key": "x"}
    cfg = {"fallback_model": dict(ANTHROPIC)}
    with patch.object(sched, "_preflight_or_block", return_value=None), \
            patch.object(sched, "_guard_job_credential_exfil", return_value=None), \
            patch.object(sched, "_resolve_job_runtime", return_value=(ours, "deepseek-v4-pro")), \
            patch.object(sched, "_load_credential_pool", return_value=None), \
            patch.object(sched, "_init_cron_mcp_tools", return_value=None):
        setup = sched._resolve_cron_agent_setup(job, job["id"], job["name"], SimpleNamespace(cfg=cfg, model="deepseek-v4-pro"))
    assert setup.blocked is None
    assert setup.fallback_model is None, "the Anthropic fallback never reaches the cron agent"


def test_a_live_model_switch_never_leaves_deepseek(locked):
    agent = _make_agent()
    with pytest.raises(RuntimeError, match=r"MODEL-LOCK: refusing to switch to provider=anthropic model=claude-opus-4-6"):
        agent.switch_model("claude-opus-4-6", "anthropic", api_key="x", base_url="https://api.anthropic.com")
    with pytest.raises(RuntimeError, match=r"host=openrouter\.ai"):
        agent.switch_model("anthropic/claude-sonnet-4", "openrouter", base_url="https://openrouter.ai/api/v1")
    assert (agent.provider, agent.model, agent.base_url) == ("deepseek", "deepseek-v4-pro", "https://api.deepseek.com/v1")
    # DeepSeek to DeepSeek is inside the lock (the predicate the switch consults).
    lock.enforce_switch("deepseek", "https://api.deepseek.com/v1", "deepseek-flash")
    lock.enforce_switch("deepseek", "", "deepseek-flash")


def test_switch_guard_is_a_pass_through_when_unlocked(unlocked):
    lock.enforce_switch("anthropic", "https://api.anthropic.com", "claude-opus-4-6")


# ── 4. detection ─────────────────────────────────────────────────────────────


def test_a_call_served_off_deepseek_logs_one_violation_line(locked, caplog):
    with caplog.at_level(logging.ERROR, logger="lucaryin.model_lock"):
        for _ in range(3):
            assert lock.alert_if_violation("anthropic", "https://api.anthropic.com", "claude-opus-4-6", where="cron main-loop")
        assert not lock.alert_if_violation("deepseek", "https://api.deepseek.com/v1", "deepseek-v4-pro", where="cron main-loop")
    errors = _lock_records(caplog, logging.ERROR)
    assert [r.getMessage() for r in errors] == [
        "MODEL-LOCK VIOLATION: cron main-loop API call served by provider=anthropic model=claude-opus-4-6 "
        "host=api.anthropic.com — this machine is locked to DeepSeek models"
    ]


def test_no_violation_lines_when_unlocked(unlocked, caplog):
    with caplog.at_level(logging.ERROR, logger="lucaryin.model_lock"):
        assert not lock.alert_if_violation("anthropic", "https://api.anthropic.com", "claude-opus-4-6", where="x")
    assert _lock_records(caplog, logging.ERROR) == []


def test_aux_accounting_alerts_on_a_known_foreign_endpoint_only(locked, caplog):
    from agent.aux_accounting import record_aux_usage

    response = SimpleNamespace(model="gemini-2.5-flash", usage=None)
    with caplog.at_level(logging.ERROR, logger="lucaryin.model_lock"):
        record_aux_usage(response, "vision", provider="gemini", base_url="https://generativelanguage.googleapis.com/v1beta")
        record_aux_usage(response, "vision")  # fallback-path call: no route known, no guess
        record_aux_usage(SimpleNamespace(model="deepseek-flash", usage=None), "vision", provider="deepseek",
                         base_url="https://api.deepseek.com/v1")
    assert [r.getMessage() for r in _lock_records(caplog, logging.ERROR)] == [
        "MODEL-LOCK VIOLATION: auxiliary vision API call served by provider=gemini model=gemini-2.5-flash "
        "host=generativelanguage.googleapis.com — this machine is locked to DeepSeek models"
    ]


# ── 5. truthful records ──────────────────────────────────────────────────────


def test_usage_audit_names_the_route_that_served(locked, tmp_path, monkeypatch):
    import cron.scheduler as sched

    monkeypatch.setattr(sched, "_usage_audit_path", lambda: tmp_path / "usage_audit.jsonl")
    served = SimpleNamespace(provider="deepseek", model="deepseek-flash", _fallback_activated=True)
    audit = sched._FireAudit({"deliver": "lucaryin"}, "53d22f9a5f4a", "deepseek-v4-pro", served)
    audit.write({"total_tokens": 10}, None)
    sched._FireAudit({}, "j2", "deepseek-v4-pro").write({}, "RuntimeError: x")  # failed before the agent existed
    rows = [json.loads(line) for line in (tmp_path / "usage_audit.jsonl").read_text().splitlines()]
    assert {k: rows[0][k] for k in ("model", "provider", "served_model", "fallback_activated")} == {
        "model": "deepseek-v4-pro", "provider": "deepseek", "served_model": "deepseek-flash", "fallback_activated": True}
    assert {k: rows[1][k] for k in ("provider", "served_model", "fallback_activated")} == {
        "provider": None, "served_model": None, "fallback_activated": False}


def test_session_model_usage_keeps_one_row_per_route_that_served(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("cron_x", source="cron", model="deepseek-v4-pro")
        db.update_token_counts("cron_x", input_tokens=100, output_tokens=10, model="deepseek-v4-pro",
                               billing_provider="deepseek", api_call_count=1)
        db.update_token_counts("cron_x", input_tokens=50, output_tokens=5, model="deepseek-flash",
                               billing_provider="deepseek", api_call_count=1)
        rows = db._read_all(
            "SELECT model, billing_provider, input_tokens FROM session_model_usage WHERE session_id = ? ORDER BY model",
            ("cron_x",))
    finally:
        db.close()
    assert [tuple(r) for r in rows] == [("deepseek-flash", "deepseek", 50), ("deepseek-v4-pro", "deepseek", 100)]


# ── 6. no borrowed model login ───────────────────────────────────────────────


_CLAUDE_RECORD = {"accessToken": "not-a-real-token", "refreshToken": "r", "expiresAt": 0, "source": "test"}


@pytest.fixture
def borrowed_logins(tmp_path, monkeypatch):
    """A Claude Code login (both readers stubbed) and a Codex CLI auth.json in a scratch home."""
    from agent import anthropic_credentials as ac
    from hermes_cli import auth as hauth

    calls = []

    def _reader(name):
        def read():
            calls.append(name)
            return dict(_CLAUDE_RECORD)
        return read

    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_keychain", _reader("keychain"))
    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_file", _reader("file"))
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "a", "refresh_token": "r"}}))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(hauth, "_codex_access_token_is_expiring", lambda *_a, **_k: False)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return SimpleNamespace(calls=calls, hermes_home=hermes_home)


def _read_both():
    from agent.anthropic_credentials import read_claude_code_credentials
    from hermes_cli.auth_codex import _import_codex_cli_tokens

    return read_claude_code_credentials(), _import_codex_cli_tokens()


def test_locked_runtime_never_reads_a_borrowed_login(locked, borrowed_logins, caplog):
    with caplog.at_level(logging.INFO, logger="lucaryin.model_lock"):
        assert _read_both() == (None, None)
        assert _read_both() == (None, None)
    assert borrowed_logins.calls == [], "the keychain entry and the credentials file are never even read"
    notes = [r.getMessage() for r in caplog.records if r.name == "lucaryin.model_lock"]
    assert sorted(notes) == [
        "MODEL-LOCK: not adopting the Claude Code login — DeepSeek-only lock (auth.adopt_external_logins)",
        "MODEL-LOCK: not adopting the Codex CLI login — DeepSeek-only lock (auth.adopt_external_logins)",
    ], "one line per source per process"


def test_unlocked_runtime_honours_adopt_external_logins_false(unlocked, borrowed_logins):
    (borrowed_logins.hermes_home / "config.yaml").write_text("auth:\n  adopt_external_logins: false\n")
    assert _read_both() == (None, None)
    assert borrowed_logins.calls == []


def test_unlocked_runtime_without_the_key_is_upstream(unlocked, borrowed_logins):
    claude, codex = _read_both()
    assert claude["accessToken"] == "not-a-real-token"
    assert codex == {"access_token": "a", "refresh_token": "r"}
    assert sorted(borrowed_logins.calls) == ["file", "keychain"]
