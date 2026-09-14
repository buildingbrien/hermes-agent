"""Lucaryin fold (runtime-patches/0023): cron delivery to the "lucaryin" pseudo-platform.

Canary.5 soak, 2026-09-14, both boxes: once the model problem was out of the way every
scheduled job still failed at delivery — executions.db showed
``Job 'heartbeat_default': unknown platform 'lucaryin'``. The jobs on the boxes look like
``heartbeat_default deliver=origin origin={platform: lucaryin, chat_id: <state.db session id>}``
and ``creative_learnings_distiller_nightly deliver=lucaryin origin=None``.

"lucaryin" is not a messenger: the Lucaryin desktop app writes ``origin.platform = "lucaryin"``
with ``chat_id`` = the ``hermes_state.SessionDB`` session id of the chat the job was created in,
and renders that session from ~/.hermes/state.db. Upstream v2026.9.7 moved delivery into
``cron/scheduler_delivery.py``, whose ``_KNOWN_DELIVERY_PLATFORMS`` lacks the name and whose
every lane (live adapter / relay / standalone) needs a gateway ``Platform`` — none can perform
a state.db write. Patch 0023 ports the old fork's lane: the platform is known, ``deliver=origin``
without an origin and bare ``deliver=lucaryin`` fall back to the conversation a human is
actually reading, ``lucaryin:<session_id>`` is taken verbatim, and ``_deliver_result`` appends
the wrapped output to the session (plus the mobile hub-flush hint) before any adapter work.
Review fix-up: the fallback ranks threads by the latest HUMAN message (cron's own assistant writes
are not activity), and a host with no state.db gets the not-found error without a store being
created.

Bare tier: stdlib + the runtime, a real ``SessionDB`` in the hermetic per-test HERMES_HOME.
"""

import os
import time

import pytest

from cron import scheduler as _sched  # noqa: F401 — binds the delivery module's late-bound refs
from cron import scheduler_delivery as delivery
from cron.scheduler_delivery import (
    LUCARYIN_PLATFORM, _HOME_TARGET_ENV_VARS, _LEGACY_HOME_TARGET_ENV_VARS, _deliver_result,
    _is_known_delivery_platform, _latest_lucaryin_session, _resolve_delivery_targets,
    is_lucaryin_deliver_token)
from cron.scheduler_preflight import _preflight_check_delivery
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

OLD_CHAT = "20260901_090000_oldchat"
NEW_CHAT = "20260913_210000_newchat"
ONE_SHOT = "20260914_080000_oneshot"
CLI_CHAT = "20260914_080100_clichat"


@pytest.fixture(autouse=True)
def _no_messenger_home_channels(monkeypatch):
    """A Lucaryin box has no messenger home channel configured — the app IS the surface."""
    for env_var in list(_HOME_TARGET_ENV_VARS.values()) + list(_LEGACY_HOME_TARGET_ENV_VARS.values()):
        monkeypatch.delenv(env_var, raising=False)
        monkeypatch.delenv(env_var + "_THREAD_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CRON_THREAD_ID", raising=False)
    monkeypatch.delenv("_HERMES_CRON_EXTERNAL_WORKER", raising=False)


def _seed(session_id: str, source: str, n_messages: int) -> None:
    with SessionDB() as db:
        db.create_session(session_id, source)
        for i in range(n_messages):
            db.append_message(session_id=session_id, role="user", content=f"{session_id} #{i}")


def _messages(session_id: str) -> list:
    with SessionDB() as db:
        return db.get_messages(session_id)


@pytest.fixture
def human_sessions():
    """Two real conversations (older 25-message, newer 30-message), a fresh one-shot ``web``
    session (what /api/chat/sync opens for fleet ask_agent / delegation) and a busy CLI session.
    The newer real conversation is the one a human is reading."""
    _seed(OLD_CHAT, "web", 25)
    time.sleep(0.01)
    _seed(NEW_CHAT, "web", 30)
    time.sleep(0.01)
    _seed(ONE_SHOT, "web", 2)
    _seed(CLI_CHAT, "cli", 40)
    return {"old": OLD_CHAT, "new": NEW_CHAT, "one_shot": ONE_SHOT, "cli": CLI_CHAT}


def _job(job_id="heartbeat_default", **fields):
    job = {"id": job_id, "name": job_id, "deliver": "origin", "origin": None}
    job.update(fields)
    return job


def _lucaryin_origin(session_id):
    return {"platform": LUCARYIN_PLATFORM, "chat_id": session_id}


# ── platform validation ──────────────────────────────────────────────────────

class TestKnownPlatform:
    def test_lucaryin_is_a_known_delivery_platform(self):
        assert LUCARYIN_PLATFORM == "lucaryin"
        assert _is_known_delivery_platform("lucaryin")
        assert _is_known_delivery_platform("LUCARYIN")

    def test_unknown_platform_is_still_rejected(self):
        assert not _is_known_delivery_platform("carrierpigeon")
        assert _resolve_delivery_targets(_job(deliver="carrierpigeon")) == []
        assert "no delivery target resolved" in (_deliver_result(_job(deliver="carrierpigeon"), "x") or "")
        assert "not a known cron delivery target" in _preflight_check_delivery(_job(deliver="carrierpigeon"))

    def test_deliver_token_recognizer(self):
        assert is_lucaryin_deliver_token("lucaryin")
        assert is_lucaryin_deliver_token(" Lucaryin:20260913_210000_newchat ")
        assert not is_lucaryin_deliver_token("slack:C123")
        assert not is_lucaryin_deliver_token("")
        assert not is_lucaryin_deliver_token(None)


# ── target resolution ────────────────────────────────────────────────────────

class TestResolution:
    def test_origin_from_a_lucaryin_chat_resolves_to_that_session(self, human_sessions):
        job = _job(deliver="origin", origin=_lucaryin_origin(human_sessions["old"]))
        assert _resolve_delivery_targets(job) == [{
            "platform": "lucaryin", "chat_id": human_sessions["old"], "thread_id": None,
            "_resolved_from": "origin"}]

    def test_origin_without_origin_falls_back_to_the_conversation_a_human_reads(self, human_sessions):
        """Not the newest session (a one-shot), not the busiest (CLI): the newest ``web``/``mobile``
        session with real history."""
        assert _latest_lucaryin_session() == human_sessions["new"]
        targets = _resolve_delivery_targets(_job(deliver="origin", origin=None))
        assert targets == [{
            "platform": "lucaryin", "chat_id": human_sessions["new"], "thread_id": None,
            "_resolved_from": "origin_fallback"}]

    def test_fallback_follows_the_latest_human_message_not_crons_own_writes(self, human_sessions):
        """Cron deliveries are assistant messages. Counting them as activity would pin an origin-less
        heartbeat to whichever thread it last wrote to until the human posted elsewhere."""
        now = time.time()
        with SessionDB() as db:
            for i in range(3):  # three deliveries into the OLD thread, all newer than any human message
                db.append_message(session_id=human_sessions["old"], role="assistant",
                                  content=f"Cronjob Response: heartbeat_default #{i}", timestamp=now + 60 + i)
            # A busy thread with no human message at all never wins either.
            db.create_session("20260914_120000_botonly", "web")
            for i in range(25):
                db.append_message(session_id="20260914_120000_botonly", role="assistant",
                                  content=f"bot #{i}", timestamp=now + 90 + i)
        assert _latest_lucaryin_session() == human_sessions["new"]
        targets = _resolve_delivery_targets(_job(deliver="origin", origin=None))
        assert [t["chat_id"] for t in targets] == [human_sessions["new"]]
        # The human replying on the older thread (later than anything on the newer one) moves it there.
        with SessionDB() as db:
            db.append_message(session_id=human_sessions["old"], role="user", content="back here",
                              timestamp=now + 200)
        assert _latest_lucaryin_session() == human_sessions["old"]

    def test_origin_without_origin_resolves_nothing_when_no_human_session_exists(self):
        assert _latest_lucaryin_session() == ""  # no state.db at all
        assert _resolve_delivery_targets(_job(deliver="origin", origin=None)) == []
        _seed(ONE_SHOT, "web", 2)  # a store, but nothing a human is reading
        assert _latest_lucaryin_session() == ""
        assert _resolve_delivery_targets(_job(deliver="origin", origin=None)) == []
        # Origin-less deliver=origin is not a failure (upstream #43014 semantics unchanged).
        assert _deliver_result(_job(deliver="origin", origin=None), "x") is None

    def test_latest_session_never_creates_a_store(self):
        assert _latest_lucaryin_session() == ""
        assert not os.path.exists(get_hermes_home() / "state.db")

    def test_bare_lucaryin_resolves_to_the_conversation_a_human_reads(self, human_sessions):
        targets = _resolve_delivery_targets(_job("creative_learnings_distiller_nightly", deliver="lucaryin"))
        assert targets == [{"platform": "lucaryin", "chat_id": human_sessions["new"], "thread_id": None}]

    def test_bare_lucaryin_prefers_a_lucaryin_origin(self, human_sessions):
        job = _job(deliver="lucaryin", origin=_lucaryin_origin(human_sessions["old"]))
        assert _resolve_delivery_targets(job)[0]["chat_id"] == human_sessions["old"]

    def test_bare_lucaryin_with_no_sessions_is_an_honest_unresolved_delivery(self):
        job = _job(deliver="lucaryin")
        assert _resolve_delivery_targets(job) == []
        assert "no delivery target resolved for deliver=lucaryin" in _deliver_result(job, "x")

    def test_explicit_session_id_is_taken_verbatim(self, monkeypatch):
        """``lucaryin:<sid>`` never goes near send_message target resolution (which has no parser
        or directory for it and rejects "lucaryin" as an unregistered plugin platform)."""
        import tools.send_message_tool as smt

        def _boom(*a, **k):  # pragma: no cover — must not be reached
            raise AssertionError("resolve_send_target must not see a lucaryin target")

        monkeypatch.setattr(smt, "resolve_send_target", _boom)
        monkeypatch.setattr(smt, "prepare_send_message_platforms", _boom)
        sid = "20260914_101010_ab12cd"
        assert _resolve_delivery_targets(_job(deliver=f"lucaryin:{sid}")) == [{
            "platform": "lucaryin", "chat_id": sid, "thread_id": None, "_resolved_from": "explicit"}]
        assert _resolve_delivery_targets(_job(deliver=f"LUCARYIN: {sid} "))[0]["chat_id"] == sid
        assert _resolve_delivery_targets(_job(deliver="lucaryin:")) == []

    def test_comma_list_and_dedup(self, human_sessions):
        job = _job(deliver=f"origin,lucaryin:{human_sessions['old']}",
                   origin=_lucaryin_origin(human_sessions["old"]))
        targets = _resolve_delivery_targets(job)
        assert [t["chat_id"] for t in targets] == [human_sessions["old"]]
        assert targets[0]["_resolved_from"] == "origin"  # strongest provenance wins the merge


# ── preflight ────────────────────────────────────────────────────────────────

class TestPreflight:
    def test_lucaryin_targets_need_no_connected_gateway(self):
        assert _preflight_check_delivery(_job(deliver="lucaryin")) is None
        assert _preflight_check_delivery(_job(deliver="lucaryin:20260914_101010_ab12cd")) is None
        assert _preflight_check_delivery(
            _job(deliver="origin", origin=_lucaryin_origin("x"), failure_deliver="lucaryin")) is None


# ── delivery ─────────────────────────────────────────────────────────────────

class TestDeliverResult:
    @pytest.fixture(autouse=True)
    def _no_adapter_path(self, monkeypatch):
        def _boom(*a, **k):  # pragma: no cover — must not be reached
            raise AssertionError("lucaryin delivery must never reach _prepare_target_delivery")
        monkeypatch.setattr(delivery, "_prepare_target_delivery", _boom)

    def test_appends_exactly_one_wrapped_assistant_message_and_the_flush_hint(self, human_sessions):
        job = _job(deliver="origin", origin=_lucaryin_origin(human_sessions["old"]))
        before = len(_messages(human_sessions["old"]))

        shot = get_hermes_home() / "shot.png"
        shot.write_bytes(b"\x89PNG\r\n")
        assert _deliver_result(job, f"the findings\nMEDIA:{shot}") is None

        msgs = _messages(human_sessions["old"])
        assert len(msgs) == before + 1
        last = msgs[-1]
        assert last["role"] == "assistant"
        assert last["content"].startswith("Cronjob Response: heartbeat_default")
        assert "the findings" in last["content"]
        assert "MEDIA:" not in last["content"]  # cleaned like every messenger lane
        assert len(_messages(human_sessions["new"])) == 30  # nobody else got it
        hint = get_hermes_home() / "cron" / ".pending_hub_flush"
        assert hint.read_text(encoding="utf-8").split() == [human_sessions["old"]]

    def test_wrap_response_false_delivers_the_raw_output(self, human_sessions, monkeypatch):
        monkeypatch.setattr(_sched, "load_config", lambda: {"cron": {"wrap_response": False}})
        job = _job(deliver="origin", origin=_lucaryin_origin(human_sessions["old"]))
        assert _deliver_result(job, "just this") is None
        assert _messages(human_sessions["old"])[-1]["content"] == "just this"

    def test_failure_notice_uses_the_same_lane(self, human_sessions):
        job = _job(deliver="origin", origin=_lucaryin_origin(human_sessions["old"]))
        assert _deliver_result(job, "it broke", for_failure=True) is None
        assert "it broke" in _messages(human_sessions["old"])[-1]["content"]

    def test_origin_fallback_delivers_to_the_conversation_a_human_reads(self, human_sessions):
        assert _deliver_result(_job(deliver="origin", origin=None), "heartbeat findings") is None
        assert "heartbeat findings" in _messages(human_sessions["new"])[-1]["content"]
        assert len(_messages(human_sessions["old"])) == 25

    def test_explicit_session_on_a_host_with_no_store_is_an_error_and_creates_no_store(self):
        """``lucaryin:<sid>`` never touches state.db to resolve, so on a host with no store the write
        must decide "not found" BEFORE opening a writable SessionDB (which would create an empty one)."""
        home = get_hermes_home()
        assert not (home / "state.db").exists()
        err = _deliver_result(_job(deliver="lucaryin:20260914_101010_ab12cd"), "x")
        assert err and "20260914_101010_ab12cd" in err and "not found" in err and "no state.db" in err
        assert not (home / "state.db").exists()
        assert not (home / "cron" / ".pending_hub_flush").exists()

    def test_unknown_session_id_is_a_delivery_error_not_a_ghost_write(self, human_sessions):
        err = _deliver_result(_job(deliver="lucaryin:20260101_000000_deleted"), "x")
        assert err and "20260101_000000_deleted" in err and "not found" in err
        for sid in human_sessions.values():
            assert all("Cronjob Response" not in m["content"] for m in _messages(sid))
        assert not (get_hermes_home() / "cron" / ".pending_hub_flush").exists()
