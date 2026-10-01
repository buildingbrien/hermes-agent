"""Patch 0030 (+0003/0012 wording): customer-facing failure copy names no runtime, vendor or model.

The Lucaryin bridge emits a failed turn's ``final_response`` verbatim when nothing streamed, and
cron failure notices land in the customer's chat (patch 0023). Upstream v2026.9.21's new copy
tables named "Hermes", the vendor ("DeepSeek" via provider_label_for), the model id and
`hermes doctor|setup|fallback add|cron …` / /model hints. These tests drive the real failure
paths of the materialized runtime — a hermetic local HTTP server answering 503 and a truncated
tool call through ``AIAgent.run_conversation``, the outer loop-error handler, the non-retryable
4xx paths, the session-storage explainer and the cron notices — and assert the text a customer
sees carries none of it. They also pin the drift guard: a branded table entry upstream adds in a
later hop fails here (``LUCARYIN_UNCOVERED``) instead of shipping.
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent.lucaryin_neutral_copy import (
    BANNED, NEUTRALIZED, TURN_FAILURE_CODE_COPY, find_banned, iter_copy_templates,
    neutralize_cron_failure_copy, neutralize_turn_failure_copy,
)

COPY_MODULES = (
    "agent.turn_failure_copy",
    "agent.turn_explainers",
    "agent.thinking_timeout_guidance",
    "cron.scheduler_failure_copy",
)
# What the finding named, plus the model id and the model-switch hint (case-insensitive).
CUSTOMER_FORBIDDEN = ("hermes", "nous", "deepseek", "fallback add", "/model")


def _assert_customer_clean(text: str) -> None:
    assert isinstance(text, str) and text.strip(), text
    lowered = text.lower()
    hits = [tok for tok in CUSTOMER_FORBIDDEN if tok in lowered]
    assert not hits, f"customer copy carries {hits}: {text!r}"
    assert not find_banned(text), f"customer copy carries {find_banned(text)}: {text!r}"


# ── tables and bindings ────────────────────────────────────────────────────────────────────


def test_every_neutralized_copy_table_is_clean_and_fully_worded():
    mods = [importlib.import_module(m) for m in COPY_MODULES]
    dirty = [(where, find_banned(text)) for where, text in iter_copy_templates(mods) if find_banned(text)]
    assert not dirty, dirty
    # A key upstream added whose text was branded got the generic sentence: write real copy.
    uncovered = {m.__name__: sorted(getattr(m, "LUCARYIN_UNCOVERED", set()) or ()) for m in mods}
    assert not any(uncovered.values()), uncovered


def test_copy_builders_are_the_neutral_ones_everywhere_they_are_imported():
    for module, name in NEUTRALIZED:
        fn = getattr(importlib.import_module(module), name)
        assert getattr(fn, "__lucaryin_neutral__", False), f"{module}.{name} is upstream's"
    # Importers bound the names after the module body (including the 0030 hook) ran.
    import agent.thinking_timeout_guidance as ttg
    import agent.turn_failure_copy as tfc
    import agent.turn_recovery as recovery
    import agent.turn_truncation as truncation

    assert recovery.exhausted_copy is tfc.exhausted_copy
    assert recovery.nonretryable_copy is tfc.nonretryable_copy
    assert recovery.content_policy_copy is tfc.content_policy_copy
    assert recovery.build_thinking_timeout_guidance is ttg.build_thinking_timeout_guidance
    assert recovery.CONTENT_POLICY_NEXT_STEPS == tfc.CONTENT_POLICY_NEXT_STEPS
    assert truncation._TRUNCATED_FINAL == TURN_FAILURE_CODE_COPY["truncated"]


@pytest.mark.parametrize("code", sorted(TURN_FAILURE_CODE_COPY) + [
    "payload_too_large", "compression_disabled", "server_context_rejection", "stream_dropped_tool_call",
    "local_processing_error", "reasoning_only", "max_iterations_no_summary",
])
def test_every_site_copy_renders_clean_with_real_fields(code):
    from agent.turn_failure_copy import site_copy

    text = site_copy(code, model="deepseek-v4-pro", label="DeepSeek", attempts=3, detail="boom",
                     tokens=1200, window=128000, limit=30, preview="thinking…",
                     resume=" (CLI: `hermes --resume 20261001_x`)")
    _assert_customer_clean(text)


def test_drift_guard_replaces_a_branded_new_key_and_records_it():
    ns = {
        "__name__": "agent.turn_failure_copy",
        "_FAILURE_CODE_COPY": {"loop_error": "Hermes hit errors", "new_clean": "Plain words."},
        "_ONE_OFF_COPY": {"new_branded": "Run `hermes doctor` now."},
        "_EXHAUSTED_LEADS": {}, "_NONRETRYABLE_COPY": {}, "_AUTH_COPY": {},
        "FAILURE_CAUSE_GLOSS": {"x": "DeepSeek fell over"},
    }
    neutralize_turn_failure_copy(ns)
    assert ns["_SITE_COPY"]["loop_error"] == TURN_FAILURE_CODE_COPY["loop_error"]
    assert ns["_SITE_COPY"]["new_clean"] == "Plain words."
    assert not find_banned(ns["_SITE_COPY"]["new_branded"])
    assert not find_banned(ns["FAILURE_CAUSE_GLOSS"]["x"])
    assert ns["LUCARYIN_UNCOVERED"] == {"_ONE_OFF_COPY:new_branded", "FAILURE_CAUSE_GLOSS:x"}

    cron_ns = {"__name__": "cron.scheduler_failure_copy",
               "_PROVIDER_FAILURE_ACTION": {"brand_new": "Run `hermes cron edit {job_id}`."}}
    neutralize_cron_failure_copy(cron_ns)
    assert not find_banned(cron_ns["_PROVIDER_FAILURE_ACTION"]["brand_new"])
    assert cron_ns["LUCARYIN_UNCOVERED"] == {"_PROVIDER_FAILURE_ACTION:brand_new"}


def test_banned_pattern_catches_what_upstream_shipped():
    for text in ("Hermes hit repeated errors", "add a backup provider with `hermes fallback add`",
                 "DeepSeek reported it was overloaded", "switch models with /model",
                 "{label} rejected your API key", "see `{home}/logs/agent.log`", "Nous Portal"):
        assert BANNED.search(text), text


# ── end to end through AIAgent against a hermetic local server ─────────────────────────────


class _Server:
    """127.0.0.1 OpenAI-compatible stub; ``respond(request_json) -> (status, body, stream_chunks)``."""

    def __init__(self, respond):
        outer = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — http.server API
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                req = json.loads(raw or b"{}")
                outer.requests.append(req)
                status, body, chunks = respond(req)
                if req.get("stream") and chunks is not None:
                    payload = ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                    ctype = "text/event-stream"
                else:
                    payload, ctype = json.dumps(body).encode(), "application/json"
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _deepseek_agent(base_url: str):
    from run_agent import AIAgent

    agent = AIAgent(
        model="deepseek-v4-pro", provider="deepseek", api_key="sk-test-not-real", base_url=base_url,
        quiet_mode=True, platform="web", skip_context_files=True, disabled_toolsets=["clarify"],
        max_iterations=6,
    )
    agent._api_max_retries = 1
    agent._auto_recovery_cycles = 0  # v2026.9.21's 15/30/60/60/60 s ladder; not under test here
    return agent


@pytest.fixture
def quiet_logs():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def test_http_503_from_the_model_service_reaches_chat_neutral(quiet_logs):
    body = {"error": {"message": "Service overloaded", "type": "overloaded"}}
    with _Server(lambda req: (503, body, None)) as base_url:
        result = _deepseek_agent(base_url).run_conversation("hello")
    text = result["final_response"]
    _assert_customer_clean(text)
    assert result.get("failed") is True
    assert "/retry" in text and "Details: HTTP 503" in text
    assert text.startswith("The AI model service")


def test_truncated_tool_call_reaches_chat_neutral(quiet_logs):
    call = {"id": "call_1", "type": "function",
            "function": {"name": "terminal", "arguments": "{\"command\": \"ls -la /tm"}}

    def respond(req):
        body = {"id": "x", "object": "chat.completion", "created": 0, "model": "deepseek-v4-pro",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                             "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        chunks = [
            {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "deepseek-v4-pro",
             "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [dict(index=0, **call)]},
                          "finish_reason": None}]},
            {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "deepseek-v4-pro",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        ]
        return 200, body, chunks

    with _Server(respond) as base_url:
        result = _deepseek_agent(base_url).run_conversation("list my tmp dir")
    _assert_customer_clean(result["final_response"])
    assert result["final_response"] == TURN_FAILURE_CODE_COPY["truncated"]
    assert result.get("failure_reason") == "truncated"


# ── the other failure sites, driven through their real handlers ────────────────────────────


class _StubAgent:
    """Shape of upstream's tests/agent/test_failed_turn_chat_copy.py::_Agent, on DeepSeek."""

    log_prefix = ""
    verbose = False
    verbose_logging = False
    provider = "deepseek"
    model = "deepseek-v4-pro"
    base_url = "https://api.deepseek.com/v1"
    max_iterations = 30
    suppress_status_output = True
    session_id = "20261001_abc"
    _fallback_chain = ()
    _fallback_index = 0

    def _summarize_api_error(self, error):
        return str(error)

    def _clean_error_message(self, msg):
        return msg

    def _has_pending_fallback(self):
        return False

    def _try_activate_fallback(self):
        return False

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Http(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def test_repeated_outer_errors_loop_error_copy_is_neutral():
    from agent.turn_loop_errors import handle_outer_loop_error

    try:
        raise TypeError("expected str, got list")
    except TypeError as exc:
        verdict = handle_outer_loop_error(
            _StubAgent(), e=exc, _outer_error_count=7, api_call_count=2, messages=[],
            conversation_history=None, _turn_exit_reason="unknown", failed=False, final_response=None,
        )
    assert verdict.failed is True
    _assert_customer_clean(verdict.final_response)
    assert "/new" in verdict.final_response
    assert verdict.final_response.rstrip().endswith("expected str, got list")


def test_interpreter_shutdown_copy_drops_the_cli_resume_command():
    from agent.turn_loop_errors import handle_outer_loop_error

    verdict = handle_outer_loop_error(
        _StubAgent(), e=RuntimeError("cannot schedule new futures after interpreter shutdown"),
        _outer_error_count=0, api_call_count=1, messages=[], conversation_history=None,
        _turn_exit_reason="unknown", failed=False, final_response=None,
    )
    _assert_customer_clean(verdict.final_response)
    assert "20261001_abc" not in verdict.final_response


@pytest.mark.parametrize("status,message", [
    (401, "HTTP 401: Authentication Fails, Your api key is invalid"),
    (404, "HTTP 404: The model `deepseek-v4-pro` does not exist"),
    (400, "HTTP 400: Invalid request: messages[1] has an invalid shape"),
    (400, "HTTP 400: Content Exists Risk"),
])
def test_non_retryable_rejections_are_neutral(status, message):
    from agent.error_classifier import classify_api_error
    from agent.turn_recovery import nonretryable_client_error_result

    error = _Http(status, message)
    classified = classify_api_error(error, provider="deepseek", model="deepseek-v4-pro")
    result = nonretryable_client_error_result(
        _StubAgent(), error, classified, status_code=status, api_kwargs=None, api_messages=[], messages=[],
        conversation_history=None, api_call_count=1, approx_tokens=10, provider="deepseek",
        base_url="https://api.deepseek.com/v1", model="deepseek-v4-pro",
    )
    text = result["final_response"]
    # The raw provider line is kept (as the base runtime did); everything around it is ours.
    head = text.split("\n\nDetails:", 1)[0]
    _assert_customer_clean(head)
    assert "hermes" not in text.lower()


@pytest.mark.parametrize("status,message,reset", [
    (503, "HTTP 503: Service overloaded", None),
    (500, "HTTP 500: internal error", None),
    (429, "HTTP 429: Rate limit reached", None),
    (429, "HTTP 429: Rate limit reached", 31000.0),
])
def test_retries_exhausted_copy_is_neutral(status, message, reset):
    from agent.turn_failure_copy import exhausted_copy

    text = exhausted_copy("overloaded" if status == 503 else "server_error" if status == 500 else "rate_limit",
                          label="DeepSeek", attempts=3, summary=message, reset_seconds=reset)
    _assert_customer_clean(text)
    assert ("resets in" in text) == (reset is not None)


def test_thinking_timeout_guidance_is_neutral_for_deepseek():
    from agent.thinking_timeout_guidance import build_thinking_timeout_guidance

    _assert_customer_clean(build_thinking_timeout_guidance(provider="deepseek", model="deepseek-v4-pro"))


@pytest.mark.parametrize("cause", [
    None, "compression", "compression_closed", "turn_lease", "locked", "replaced", "deleted_wal",
    "corrupt", "fts_index", "disk", "something-new",
])
def test_session_storage_explanations_are_neutral(cause):
    from agent.turn_explainers import TurnExplainersMixin

    text = TurnExplainersMixin._format_turn_completion_explanation(
        "session_persistence_failed", persistence_cause=cause, db_path="/x/state.db", model="deepseek-v4-pro")
    _assert_customer_clean(text)


@pytest.mark.parametrize("reason", [
    "empty_response_exhausted", "all_retries_exhausted_no_response", "rebuilt_restart_limit_exceeded",
    "max_iterations_reached(30/30)", "repeated_outer_errors", "budget_exhausted",
])
def test_turn_completion_explanations_are_neutral(reason):
    from agent.turn_explainers import TurnExplainersMixin

    text = TurnExplainersMixin._format_turn_completion_explanation(reason, model="deepseek-v4-pro")
    _assert_customer_clean(text)


# ── cron failure notices (delivered into the customer's chat by patch 0023) ────────────────


@pytest.mark.parametrize("error", [
    "HTTP 503: Service overloaded",
    "Error code: 429 - rate limit reached",
    "HTTP 401: Authentication Fails",
    "HTTP 402: Insufficient Balance",
    "HTTP 404: model not found",
    "ReadTimeout: timed out",
    "Script timed out after 30s: /x/run.sh",
    "Cron job 'x' idle for 700s (limit 600s) — last activity: terminal",
    "RuntimeError: boom",
])
def test_cron_failure_notices_are_neutral(error):
    from cron.scheduler import _summarize_cron_failure_for_delivery

    text = _summarize_cron_failure_for_delivery({"id": "job1", "name": "Daily brief"}, error)
    assert text.startswith("⚠️ Cron 'Daily brief' failed:")
    _assert_customer_clean(text)


def test_cron_notice_builders_are_neutral_for_every_gloss_reason():
    import cron.scheduler_failure_copy as sfc
    from agent.turn_failure_copy import FAILURE_CAUSE_GLOSS

    for reason in sorted(FAILURE_CAUSE_GLOSS) + ["unknown"]:
        text = sfc.provider_failure_notice(
            "Daily brief", "job1", reason, backup_provider_phrase="add one with `hermes fallback add`",
            provider="deepseek")
        if text is not None:
            _assert_customer_clean(text)
    for text in (
        sfc.generic_failure_notice("Daily brief", "job1", "boom"),
        sfc.script_timeout_notice("Daily brief", "job1"),
        sfc.inactivity_notice("Daily brief", "job1"),
        sfc.blocked_config_notice("Daily brief", "no provider configured"),
    ):
        _assert_customer_clean(text)


# ── model-facing wording folded in 0003 / 0012 ─────────────────────────────────────────────


def test_cronjob_schema_never_tells_the_model_about_cli_model_commands():
    from tools.cronjob_tools import CRONJOB_SCHEMA

    blob = json.dumps(CRONJOB_SCHEMA)
    assert "hermes model" not in blob and "/model" not in blob


def test_windows_scratch_guidance_does_not_name_the_runtime():
    import agent.prompt_builder as pb

    texts = [v for v in vars(pb).values() if isinstance(v, str) and "$TMPDIR" in v]
    assert texts, "the Windows local-backend guidance moved; re-check patch 0003"
    assert not [t for t in texts if "Hermes points" in t]


def test_deepseek_unpinned_default_stays_v4_pro():
    """Patch 0031: upstream v2026.9.21 put deepseek-flash first; [0] is the unpinned default."""
    from hermes_cli.models import _PROVIDER_MODELS, get_default_model_for_provider

    assert _PROVIDER_MODELS["deepseek"] == ["deepseek-v4-pro", "deepseek-flash"]
    assert get_default_model_for_provider("deepseek") == "deepseek-v4-pro"

