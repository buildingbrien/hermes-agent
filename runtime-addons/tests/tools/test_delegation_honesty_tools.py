"""Delegation tools tell the caller what actually happened (Lucaryin local
conversation review, Sep 20 - Oct 1 2026; bridge half: lucaryin-ai
fix/delegation-honesty).

F19  Sep 26: Neith's 28,015-byte report was on disk when delegate_to_neith
     said only "did not finish within her 300-second limit"; Merlin then wrote
     9,142 bytes over the same file. The bridge's 504 now says whether she is
     still working and which files she wrote, and these tools relay it as
     UNFINISHED work, never as an answer. ask_agent used to report the same
     504 as "bridge is unreachable" (HTTPError is a URLError) and timed out on
     the client at the bridge's own 300 s, losing the facts.
F31  Sep 25 04:12Z: fleet_send said "delivered" for Merlin's reply to the agent
     that had delegated to him; that agent's loop guard refused it. The
     refusal is deterministic, so fleet_send now runs it first and says
     "not_delivered" (no dead letter, no retry).
F32  A 409 from the bridge (the same request already running there) is
     relayed as busy/duplicate, not as a failure to retry.

Round 2: every delegation names the chat it was asked from (and its surface),
so the bridge writes a late result into that chat, and never into the user's
chat for a scheduled run; the still-running wording says truthfully where the
result will show (the phone app does not show relayed rows).

Round 3: ask_agent names who is asking when the RUNTIME calls it. The
dispatcher passes a handler only task_id, session_id and user_task, so the
handler's sender was always "": the far bridge could not frame the question
as a teammate's (it read as the owner's), single-flight never applied, a
timed-out answer was never delivered late, and in a nested hand-off (Set,
working on Merlin's task, asking Ptah) the previous hop was named instead.
These tests go through registry.dispatch with exactly those kwargs.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
from email.message import Message
from unittest import mock

import pytest

from tools import ask_agent_tool as aa
from tools import delegate_neith as dn
from tools import fleet_send as fs

REPORT = "/Users/x/Documents/Lucaryin/JS|BC Sync — Research Report — 2026-09-26.md"


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://127.0.0.1/api/chat/sync", code, "err", Message(),
                                  io.BytesIO(json.dumps(body).encode()))


@pytest.fixture
def thoth_turn(monkeypatch):
    monkeypatch.setenv("BRIDGE_PROFILE", "thoth")
    for k in ("FLEET_DELEGATION_DEPTH", "FLEET_DELEGATION_ORIGIN", "FLEET_DELEGATION_VISITED",
              "MAX_FLEET_DEPTH", "LUCARYIN_TURN_SESSION_ID", "HERMES_TURN_SOURCE",
              "HERMES_CRON_SESSION"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def phone_turn(thoth_turn, monkeypatch):
    monkeypatch.setenv("LUCARYIN_TURN_SESSION_ID", "mobile_3b0042fa_1790456858")
    monkeypatch.setenv("HERMES_TURN_SOURCE", "mobile")


BRIDGE_504_RUNNING = {
    "success": False, "timed_out": True, "agent_id": "neith", "status": "timeout",
    "agent": "neith", "waited_s": 300, "still_running": True,
    "files_written": [{"path": REPORT, "bytes": 28015}],
    "error": "neith did not finish within 5 minutes. It is still working; ...",
    "guidance": "It is unfinished, not done ...",
}


class TestDelegateToNeith:

    def test_sep26_timeout_is_unfinished_still_running_and_names_her_report(self, thoth_turn, monkeypatch):
        monkeypatch.setattr(dn, "_call_neith_sync",
                            mock.Mock(side_effect=_http_error(504, BRIDGE_504_RUNNING)))
        out = json.loads(dn.delegate_to_neith_tool({"task": "research the JS|BC sync"}))
        assert out["status"] == "timeout" and out["unfinished"] is True
        assert out["still_running"] is True
        assert out["files_written"] == [{"path": REPORT, "bytes": 28015}]
        assert "still working" in out["error"]
        assert "Do not redo the research" in out["guidance"]
        assert "never write over the files" in out["guidance"]
        assert "result" not in out  # nothing reads as an answer

    def test_an_older_bridges_bare_504_is_still_unfinished(self, thoth_turn, monkeypatch):
        monkeypatch.setattr(dn, "_call_neith_sync", mock.Mock(side_effect=_http_error(
            504, {"success": False, "error": "Worker timed out after 300s"})))
        out = json.loads(dn.delegate_to_neith_tool({"task": "research X"}))
        assert out["status"] == "timeout" and out["still_running"] is False
        assert "stopped, unfinished" in out["error"]
        assert "UNFINISHED, not done" in out["guidance"]
        assert "files_written" not in out

    def test_the_same_request_already_running_is_busy_not_a_failure(self, thoth_turn, monkeypatch):
        monkeypatch.setattr(dn, "_call_neith_sync", mock.Mock(side_effect=_http_error(
            409, {"success": False, "busy": True, "status": "duplicate", "agent": "neith",
                  "error": "neith is already working on this same request (started 15:46Z)"})))
        fallback = mock.Mock()
        monkeypatch.setattr(dn, "_fallback_subagent", fallback)
        out = json.loads(dn.delegate_to_neith_tool({"task": "research X"}, parent_agent=object()))
        assert out["status"] == "duplicate"
        assert "Do not re-send it" in out["guidance"]
        fallback.assert_not_called()

    def test_schema_says_a_timeout_is_unfinished(self):
        d = dn.DELEGATE_TO_NEITH_SCHEMA["description"]
        assert "UNFINISHED work, never finished" in d


class TestAskAgent:

    def test_client_waits_past_the_bridges_window(self):
        assert aa._SYNC_TIMEOUT > 300

    def test_504_is_a_timeout_not_unreachable(self, thoth_turn, monkeypatch):
        monkeypatch.setattr(aa.urllib.request, "urlopen",
                            mock.Mock(side_effect=_http_error(504, BRIDGE_504_RUNNING)))
        out = json.loads(aa.ask_agent("neith", "What do the comps say?", sender="thoth"))
        assert out["status"] == "timeout" and out["unfinished"] is True
        assert "unreachable" not in out["error"]
        assert out["still_running"] is True
        assert out["files_written"][0]["bytes"] == 28015

    def test_409_is_relayed_as_busy(self, thoth_turn, monkeypatch):
        monkeypatch.setattr(aa.urllib.request, "urlopen", mock.Mock(side_effect=_http_error(
            409, {"success": False, "busy": True, "status": "busy", "agent": "set",
                  "error": "set is still working on your earlier request",
                  "guidance": "Do not re-send it now."})))
        out = json.loads(aa.ask_agent("set", "status?", sender="thoth"))
        assert out["status"] == "busy" and out["guidance"] == "Do not re-send it now."

    def test_a_client_side_timeout_is_unfinished(self, thoth_turn, monkeypatch):
        monkeypatch.setattr(aa.urllib.request, "urlopen", mock.Mock(side_effect=TimeoutError()))
        out = json.loads(aa.ask_agent("ptah", "Design the five gig cards", sender="thoth"))
        assert out["status"] == "timeout" and out["still_running"] is False
        assert "Do not guess" in out["guidance"]

    def test_a_refused_hop_is_still_reported_as_refused(self, thoth_turn, monkeypatch):
        monkeypatch.setattr(aa.urllib.request, "urlopen", mock.Mock(side_effect=_http_error(
            403, {"success": False, "refused": True, "error": "Delegation refused by ptah: loop"})))
        out = json.loads(aa.ask_agent("ptah", "hi", sender="thoth"))
        assert out["refused"] is True


class TestAskAgentThroughTheRuntimeDispatcher:
    """What model_tools passes a handler: task_id, session_id, user_task."""

    @staticmethod
    def _dispatch(monkeypatch, agent="ptah"):
        sent = []

        def urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode()))
            raise _http_error(504, BRIDGE_504_RUNNING)
        monkeypatch.setattr(aa.urllib.request, "urlopen", urlopen)
        from tools.registry import registry
        out = registry.dispatch("ask_agent", {"agent": agent, "question": "Design the five gig cards"},
                                task_id="task-1", session_id="sess-1", user_task=None)
        return sent, out

    def test_a_top_level_turn_names_the_asker(self, thoth_turn, monkeypatch):
        sent, _out = self._dispatch(monkeypatch)
        assert sent[0]["delegation_depth"] == 1
        assert sent[0]["delegation_visited"] == ["thoth"]
        assert sent[0]["delegation_origin"] == "thoth"

    def test_a_delegated_turn_names_itself_last_not_the_previous_hop(self, thoth_turn, monkeypatch):
        # Set is running a task Merlin handed it (the bridge seeded these) and
        # asks Ptah: Ptah's bridge must see Set as the asker.
        monkeypatch.setenv("BRIDGE_PROFILE", "set")
        monkeypatch.setenv("FLEET_DELEGATION_DEPTH", "1")
        monkeypatch.setenv("FLEET_DELEGATION_ORIGIN", "thoth")
        monkeypatch.setenv("FLEET_DELEGATION_VISITED", "thoth")
        sent, _out = self._dispatch(monkeypatch)
        assert sent[0]["delegation_visited"] == ["thoth", "set"]
        assert sent[0]["delegation_depth"] == 2
        assert sent[0]["delegation_origin"] == "thoth"

    def test_asking_yourself_is_caught_through_the_dispatcher_too(self, thoth_turn, monkeypatch):
        sent, out = self._dispatch(monkeypatch, agent="thoth")
        assert not sent
        assert "That is you" in str(out)

    def test_an_explicit_agent_id_kwarg_still_wins(self, thoth_turn, monkeypatch):
        assert aa._asking_agent({"agent_id": "Neith"}) == "neith"
        assert aa._asking_agent({}) == "thoth"
        monkeypatch.delenv("BRIDGE_PROFILE")
        assert aa._asking_agent({}) == ""


class TestFleetSendNotDelivered:

    def test_sep25_reply_to_the_agent_that_delegated_is_not_delivered(self, monkeypatch):
        # Merlin is running a task Ptah handed him (the bridge seeded these).
        monkeypatch.setenv("BRIDGE_PROFILE", "thoth")
        monkeypatch.setenv("FLEET_DELEGATION_DEPTH", "1")
        monkeypatch.setenv("FLEET_DELEGATION_ORIGIN", "ptah")
        monkeypatch.setenv("FLEET_DELEGATION_VISITED", "ptah")
        post = mock.Mock()
        monkeypatch.setattr(fs, "_post_json", post)
        dead = mock.Mock()
        monkeypatch.setattr(fs, "_write_dead_letter", dead)
        out = json.loads(fs.fleet_send_tool({"recipient": "ptah",
                                             "message": "Confirmed, I'm ready for the token stages."}))
        assert out["success"] is False and out["status"] == "not_delivered"
        assert "goes back to ptah automatically" in out["message"]
        post.assert_not_called()
        dead.assert_not_called()

    def test_a_chain_loop_to_someone_else_is_not_delivered_either(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_PROFILE", "set")
        monkeypatch.setenv("FLEET_DELEGATION_DEPTH", "2")
        monkeypatch.setenv("FLEET_DELEGATION_VISITED", "thoth,ptah")
        monkeypatch.setattr(fs, "_post_json", mock.Mock())
        out = json.loads(fs.fleet_send_tool({"recipient": "thoth", "message": "done?"}))
        assert out["status"] == "not_delivered"
        assert "Do not retry it" in out["message"]

    def test_bridge_refusal_is_not_delivered_not_dead(self, thoth_turn, monkeypatch):
        monkeypatch.setattr(fs, "_post_json", mock.Mock(return_value={
            "success": False, "refused": True, "status": "not_delivered",
            "error": "Not delivered: Ptah's bridge would refuse it (loop).",
            "guidance": "Your reply goes back to Ptah automatically."}))
        dead = mock.Mock()
        monkeypatch.setattr(fs, "_write_dead_letter", dead)
        out = json.loads(fs.fleet_send_tool({"recipient": "ptah", "message": "ok"}))
        assert out["status"] == "not_delivered"
        assert "goes back to Ptah automatically" in out["message"]
        dead.assert_not_called()

    def test_a_fresh_owner_turn_still_sends(self, thoth_turn, monkeypatch):
        calls = []

        def post(url, payload, timeout=10):
            calls.append((url, payload))
            return {"success": True, "task_id": payload["task_id"]}
        monkeypatch.setattr(fs, "_post_json", post)
        monkeypatch.setattr(fs, "_check_recipient_inbox", lambda *a, **k: True)
        out = json.loads(fs.fleet_send_tool({"recipient": "neith", "message": "pull the comps"}))
        assert out["status"] == "delivered"
        assert calls and calls[0][1]["delegation_visited"] == ["thoth"]

    def test_schema_warns_against_replying_by_fleet_send(self):
        assert "not_delivered" in fs.FLEET_SEND_SCHEMA["description"]


class TestTheAskingChatTravelsWithTheDelegation:

    def test_turn_origin_reads_the_workers_turn(self, phone_turn):
        assert fs.turn_origin() == {"session_id": "mobile_3b0042fa_1790456858",
                                    "source": "mobile"}

    def test_a_scheduled_run_is_cron_and_names_no_chat(self, phone_turn, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        assert fs.turn_origin() == {"session_id": "", "source": "cron"}

    def test_where_a_late_result_lands_is_said_truthfully(self, thoth_turn):
        assert "posted into this chat" in fs.late_result_where("web")
        phone = fs.late_result_where("mobile")
        assert "phone app does not show it" in phone and "posted into this chat" not in phone
        assert "will not be posted to the user" in fs.late_result_where("cron")
        voice = fs.late_result_where("voice")
        assert "not to this call" in voice and "posted into this chat" not in voice

    def test_fleet_send_records_the_chat_with_its_own_bridge(self, phone_turn, monkeypatch):
        calls = []

        def post(url, payload, timeout=10):
            calls.append((url, payload))
            return {"success": True, "task_id": payload["task_id"]}
        monkeypatch.setattr(fs, "_post_json", post)
        monkeypatch.setattr(fs, "_check_recipient_inbox", lambda *a, **k: True)
        json.loads(fs.fleet_send_tool({"recipient": "neith", "message": "pull the comps"}))
        assert calls[0][1]["origin_session_id"] == "mobile_3b0042fa_1790456858"
        assert calls[0][1]["origin_source"] == "mobile"

    def test_ask_agent_sends_the_chat_and_its_late_wording_fits_the_phone(self, phone_turn, monkeypatch):
        sent = []

        def urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode()))
            raise _http_error(504, BRIDGE_504_RUNNING)
        monkeypatch.setattr(aa.urllib.request, "urlopen", urlopen)
        out = json.loads(aa.ask_agent("neith", "What do the comps say?", sender="thoth"))
        assert sent[0]["requester_session_id"] == "mobile_3b0042fa_1790456858"
        assert sent[0]["requester_source"] == "mobile"
        assert "phone app does not show it" in out["error"]
        assert "posted into your chat" not in out["error"]

    def test_delegate_to_neith_sends_the_chat_and_says_where_her_result_lands(self, phone_turn, monkeypatch):
        sent = []

        def urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode()))
            raise _http_error(504, BRIDGE_504_RUNNING)
        monkeypatch.setattr(dn.urllib.request, "urlopen", urlopen)
        out = json.loads(dn.delegate_to_neith_tool({"task": "research the JS|BC sync"}))
        assert sent[0]["requester_session_id"] == "mobile_3b0042fa_1790456858"
        assert sent[0]["requester_source"] == "mobile"
        assert out["still_running"] is True
        assert "phone app does not show it" in out["guidance"]
        assert "Do not redo the research" in out["guidance"]

    def test_a_desktop_turn_without_a_chat_id_sends_nothing_extra(self, thoth_turn, monkeypatch):
        sent = []

        def urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode()))
            raise _http_error(504, BRIDGE_504_RUNNING)
        monkeypatch.setattr(aa.urllib.request, "urlopen", urlopen)
        out = json.loads(aa.ask_agent("neith", "What do the comps say?", sender="thoth"))
        assert "requester_session_id" not in sent[0] and "requester_source" not in sent[0]
        assert "posted into this chat" in out["error"]
