"""delegate_task's explicit teammate target (patch 0032 + tools/teammate_handoff.py).

Routing decision 6 (founder, Oct 1): the hand-off target is an explicit agent
id in the tool call, never inferred from the goal's wording. Until now the
only way to hand a delegate_task to a teammate was to name it in the goal; the
bridge parsed the prose, published the goal to whoever it found, and the local
subagent ran the same goal too. Three review rounds of lucaryin-ai#132 each
found another sentence shape the prose grammar sent to the wrong agent.

Now each task may carry ``agent`` (an id, enum-checked). When it does:
  * that field alone picks the recipient, and the task goes to the teammate's
    own bridge (POST /api/chat/sync, delegation budget, bearer) exactly as
    ask_agent does; its answer is the task's result;
  * the task does NOT also spawn locally;
  * a display name, a session without the fleet tools, or a hop over the fleet
    budget is refused with a plain reason, and nothing is sent.
A call where no task carries ``agent`` reaches upstream's body untouched.

Bare tier: a real loopback HTTP server stands in for the teammate's bridge, so
the actual urllib request is what is asserted. Nothing touches a real bridge.
"""

from __future__ import annotations

import json
import os
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from tools import ask_agent_tool, delegate_tool, fleet_send, teammate_handoff as th

FLEET_IDS = ["neith", "ptah", "set", "thoth"]


class FakeBridge:
    """Records every /api/chat/sync request; answers like a teammate's bridge."""

    def __init__(self, reply=None, delay=0.0, status=200, replies=None):
        self.requests: list[dict] = []
        self.reply = reply if reply is not None else {"success": True, "response": "done: the answer"}
        self.replies = list(replies or [])  # answered in order before `reply`
        self.delay = delay
        self.status = status
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self.port = 0

    def serve(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                record = {"path": self.path, "json": json.loads(body or b"{}"),
                          "auth": self.headers.get("Authorization"), "started": time.monotonic()}
                with bridge._lock:
                    bridge.active += 1
                    bridge.max_active = max(bridge.max_active, bridge.active)
                    bridge.requests.append(record)
                    reply = (bridge.replies.pop(0) if bridge.replies else bridge.reply)
                try:
                    if bridge.delay:
                        time.sleep(bridge.delay)
                finally:
                    # The turn ends BEFORE the reply goes out: once the caller
                    # has the answer it may start the next turn at once.
                    with bridge._lock:
                        bridge.active -= 1
                        record["ended"] = time.monotonic()
                out = json.dumps(reply).encode()
                self.send_response(bridge.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):  # noqa: D401 — silence the test server
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


@pytest.fixture
def bridges(monkeypatch):
    """One fake bridge per teammate, wired into ask_agent's port map."""
    made: dict[str, FakeBridge] = {}
    servers = []

    def make(agent, **kw):
        b = FakeBridge(**kw)
        servers.append(b.serve())
        made[agent] = b
        monkeypatch.setitem(ask_agent_tool.AGENT_PORTS, agent, b.port)
        return b

    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BRIDGE_PROFILE", "thoth")
    for name in ("FLEET_DELEGATION_DEPTH", "FLEET_DELEGATION_ORIGIN",
                 "FLEET_DELEGATION_VISITED", "MAX_FLEET_DEPTH", "BRIDGE_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def spawn(monkeypatch):
    """Stand-in for upstream's body: records what it was asked to spawn."""
    calls: list[dict] = []

    def fake(**kw):
        calls.append(kw)
        return json.dumps({"results": [{"task_index": i, "status": "completed", "summary": f"local {i}"}
                                       for i, _ in enumerate(kw.get("tasks") or [])]})

    monkeypatch.setattr(delegate_tool, "_spawn_delegate_task", fake)
    return calls


def parent(tools=("ask_agent", "delegate_task", "fleet_send")):
    return types.SimpleNamespace(valid_tool_names=set(tools), _delegate_depth=0)


def run(**kw):
    kw.setdefault("parent_agent", parent())
    return json.loads(delegate_tool.delegate_task(**kw))


# ── The schema ───────────────────────────────────────────────────────────────

class TestSchema:
    def test_each_task_advertises_an_agent_id_field(self):
        props = delegate_tool.DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        agent = props["tasks"]["items"]["properties"]["agent"]
        assert agent["type"] == "string"
        assert sorted(agent["enum"]) == FLEET_IDS
        assert "agent id" in agent["description"]
        assert "agent" not in props["tasks"]["items"].get("required", [])
        # Only per task: the legacy top-level goal stays unadvertised, and so does a top-level agent.
        assert "agent" not in props

    @pytest.mark.parametrize("independent", [True, False])
    def test_the_per_call_schema_keeps_the_field(self, independent):
        with mock.patch("tools.delegate_tool_config._get_independent_completions", return_value=independent):
            params = delegate_tool._build_dynamic_schema_overrides()["parameters"]
        assert sorted(params["properties"]["tasks"]["items"]["properties"]["agent"]["enum"]) == FLEET_IDS

    def test_every_hand_off_tool_takes_the_same_ids(self):
        assert sorted(th.FLEET_AGENT_IDS) == FLEET_IDS
        assert sorted(ask_agent_tool.AGENT_PORTS) == FLEET_IDS
        assert sorted(fleet_send.AGENT_PORTS) == FLEET_IDS
        ask = ask_agent_tool.ASK_AGENT_SCHEMA["parameters"]["properties"]["agent"]
        send = fleet_send.FLEET_SEND_SCHEMA["parameters"]["properties"]["recipient"]
        assert sorted(ask["enum"]) == sorted(send["enum"]) == FLEET_IDS

    def test_a_display_name_is_refused_by_ask_agent_and_fleet_send(self):
        out = json.loads(ask_agent_tool.ask_agent("Fox", "q", sender="thoth"))
        assert out["success"] is False and "agent id" in out["error"]
        out = json.loads(fleet_send.fleet_send_tool({"recipient": "Fox", "message": "hi"}))
        assert "Unknown agent" in out["error"] and "agent id" in out["error"]


# ── Pass-through: no agent field, no change ─────────────────────────────────

class TestPassThrough:
    def test_no_agent_field_reaches_upstream_untouched(self, spawn):
        tasks = [{"goal": "Summarize the three attached reports", "context": "c"}]
        out = run(tasks=tasks, context="shared", background=True)
        assert out["results"][0]["summary"] == "local 0"
        assert spawn == [{"goal": None, "context": "shared", "tasks": tasks, "max_iterations": None,
                          "role": None, "background": True, "output_schema": None, "images": None,
                          "action": None, "subagent_id": None, "message": None,
                          "credentials_cfg": None, "parent_agent": spawn[0]["parent_agent"]}]
        assert spawn[0]["tasks"] is tasks

    def test_a_legacy_goal_is_upstreams(self, spawn):
        run(goal="ask Fox to research Acme")
        assert spawn[0]["goal"] == "ask Fox to research Acme" and spawn[0]["tasks"] is None

    def test_control_actions_pass_through(self, spawn):
        run(action="list", tasks=[{"goal": "x", "agent": "ptah"}])
        assert spawn[0]["action"] == "list"

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_a_blank_agent_is_no_target(self, spawn, blank):
        run(tasks=[{"goal": "Summarize the three attached reports", "agent": blank}])
        assert spawn[0]["tasks"] == [{"goal": "Summarize the three attached reports"}]

    def test_your_own_id_spawns_locally(self, spawn, bridges):
        b = bridges("thoth")
        out = run(tasks=[{"goal": "Summarize the three attached reports", "agent": "THOTH"}])
        assert spawn[0]["tasks"] == [{"goal": "Summarize the three attached reports"}]
        assert out["results"][0]["summary"] == "local 0"
        assert b.requests == []


# ── The hand-off itself ──────────────────────────────────────────────────────

class TestHandOff:
    def test_the_field_alone_picks_the_teammate(self, spawn, bridges):
        ptah, neith = bridges("ptah"), bridges("neith")
        # The goal names Fox (neith) and ends with a (Set) owner tag; the field says ptah.
        out = run(tasks=[{"goal": "Ask Fox for the comps, then draft the launch post (Set)",
                          "context": "Brand voice: plain.", "agent": "ptah"}])
        assert spawn == []                                   # never also a local subagent
        assert neith.requests == []
        [req] = ptah.requests
        assert req["path"] == "/api/chat/sync"
        assert req["json"]["agent_id"] == "ptah"
        assert req["json"]["messages"] == [{"role": "user", "content":
                                            "Ask Fox for the comps, then draft the launch post (Set)"
                                            "\n\nContext:\nBrand voice: plain."}]
        # The fleet budget rides along, so ptah's bridge can refuse onward loops.
        assert req["json"]["delegation_depth"] == 1
        assert req["json"]["delegation_visited"] == ["thoth"]
        assert out["handed_to"] == ["ptah"]
        [entry] = out["results"]
        assert entry["task_index"] == 0 and entry["agent"] == "ptah"
        assert entry["handled_by"] == "teammate" and entry["status"] == "completed"
        assert entry["summary"] == "done: the answer"

    def test_ids_are_case_insensitive(self, spawn, bridges):
        b = bridges("set")
        out = run(tasks=[{"goal": "Reconcile the September ledger", "agent": " Set "}])
        assert out["results"][0]["agent"] == "set" and len(b.requests) == 1

    def test_json_string_tasks_are_read_like_upstream(self, spawn, bridges):
        b = bridges("neith")
        run(tasks=json.dumps([{"goal": "Find the Q3 filings for Acme", "agent": "neith"}]))
        assert len(b.requests) == 1 and spawn == []

    def test_images_and_schema_travel_in_the_question(self, spawn, bridges):
        b = bridges("ptah", reply={"success": True, "response": '{"verdict": "ship it"}'})
        run(tasks=[{"goal": "Review the mock", "agent": "ptah", "images": ["/tmp/mock.png"],
                    "output_schema": {"type": "object", "required": ["verdict"]}}])
        text = b.requests[0]["json"]["messages"][0]["content"]
        # The same OUTPUT CONTRACT block a local subagent gets.
        assert "- /tmp/mock.png" in text and "OUTPUT CONTRACT" in text and '"verdict"' in text

    def test_mixed_call_hands_off_some_and_spawns_the_rest(self, spawn, bridges):
        b = bridges("neith")
        out = run(tasks=[{"goal": "Summarize the three attached reports"},
                         {"goal": "Find the Q3 filings for Acme", "agent": "neith"},
                         {"goal": "Draft an outline of the memo", "agent": "thoth"}])
        assert spawn[0]["tasks"] == [{"goal": "Summarize the three attached reports"},
                                     {"goal": "Draft an outline of the memo"}]
        assert out["local_task_indices"] == [0, 2]
        assert [r["summary"] for r in out["results"]] == ["local 0", "local 1"]
        [entry] = out["teammate_results"]
        assert entry["task_index"] == 1 and entry["agent"] == "neith"
        assert len(b.requests) == 1

    def test_a_local_spawn_crash_still_returns_the_teammates_answer(self, bridges, monkeypatch):
        def boom(**kw):
            raise RuntimeError("no credentials")
        monkeypatch.setattr(delegate_tool, "_spawn_delegate_task", boom)
        b = bridges("ptah")
        out = run(tasks=[{"goal": "Summarize the three attached reports"},
                         {"goal": "Draft the weekly post", "agent": "ptah"}])
        assert "could not start" in out["error"]
        assert out["teammate_results"][0]["status"] == "completed" and len(b.requests) == 1

    def test_one_teammate_never_gets_two_turns_at_once_within_a_call(self, spawn, bridges):
        b = bridges("set", delay=0.2)
        p = bridges("ptah", delay=0.2)
        out = run(tasks=[{"goal": "Run lesson 5 in the course", "agent": "set"},
                         {"goal": "Run lesson 6 in the course", "agent": "set"},
                         {"goal": "Draft the recap of both lessons", "agent": "ptah"}])
        assert b.max_active == 1 and len(b.requests) == 2      # F32: one at a time per agent
        first, second = b.requests
        assert second["started"] >= first["ended"]             # set's second turn waited for its first
        [recap] = p.requests
        assert recap["started"] < second["ended"]              # ptah ran beside set, not after it
        assert [r["task_index"] for r in out["results"]] == [0, 1, 2]


# ── Refusals: nothing is sent ────────────────────────────────────────────────

class TestRefusals:
    @pytest.mark.parametrize("name", ["Fox", "Merlin", "Clara", "neith-bot", "@ptah", "ptah, set"])
    def test_a_name_is_not_an_id(self, spawn, bridges, name):
        b = bridges("neith")
        out = run(tasks=[{"goal": "Find the Q3 filings for Acme", "agent": name}])
        assert "not an agent id" in out["error"] and "neith, ptah, set, thoth" in out["error"]
        assert spawn == [] and b.requests == []

    def test_no_fleet_tools_no_hand_off(self, spawn, bridges):
        # A cron job's toolset has delegate_task but not the fleet tools.
        b = bridges("ptah")
        out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}],
                  parent_agent=parent(tools=("delegate_task", "terminal")))
        assert "not available in this session" in out["error"]
        assert spawn == [] and b.requests == []

    def test_a_parent_without_tool_names_is_refused(self, spawn, bridges):
        b = bridges("ptah")
        out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}],
                  parent_agent=types.SimpleNamespace(_delegate_depth=0))
        assert "not available in this session" in out["error"] and b.requests == []

    def test_a_loop_is_refused_before_anything_leaves(self, spawn, bridges, monkeypatch):
        b = bridges("ptah")
        monkeypatch.setenv("FLEET_DELEGATION_DEPTH", "1")
        monkeypatch.setenv("FLEET_DELEGATION_VISITED", "ptah")
        out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}])
        [entry] = out["results"]
        assert entry["status"] == "refused" and "already handled" in entry["error"]
        assert "Do not retry" in entry["guidance"] and b.requests == []

    def test_over_the_hop_budget_is_refused(self, spawn, bridges, monkeypatch):
        b = bridges("ptah")
        monkeypatch.setenv("FLEET_DELEGATION_DEPTH", "3")
        out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}])
        assert out["results"][0]["status"] == "refused" and b.requests == []

    def test_a_missing_goal_is_refused(self, spawn, bridges):
        b = bridges("ptah")
        out = run(tasks=[{"goal": "  ", "agent": "ptah"}])
        assert "missing a 'goal'" in out["error"] and b.requests == []

    def test_too_many_tasks(self, spawn, bridges):
        with mock.patch("tools.delegate_tool_config._get_max_concurrent_children", return_value=2):
            out = run(tasks=[{"goal": f"Draft section {i} of the memo", "agent": "ptah"} for i in range(3)])
        assert "Too many tasks" in out["error"] and spawn == []


# ── Honest results when the teammate does not finish (F19) ──────────────────

class TestUnfinished:
    def test_unreachable_is_failed_not_finished(self, spawn, monkeypatch):
        monkeypatch.setitem(ask_agent_tool.AGENT_PORTS, "ptah", 9)  # nothing listens on :9
        out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}])
        entry = out["results"][0]
        assert entry["status"] == "failed" and entry["summary"] == ""
        assert "NOT done" in entry["guidance"]

    def test_a_timeout_is_unfinished(self, spawn, bridges, monkeypatch):
        bridges("ptah", delay=1.0)
        monkeypatch.setattr(ask_agent_tool, "_SYNC_TIMEOUT", 0.2)
        out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}])
        entry = out["results"][0]
        assert entry["status"] == "timeout" and entry["summary"] == ""
        assert "unfinished" in entry["guidance"]

    def test_a_refusal_from_the_far_bridge_is_relayed(self, spawn, bridges):
        bridges("ptah", reply={"refused": True, "error": "ptah already handled this request"})
        entry = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}])["results"][0]
        assert entry["status"] == "refused" and "already handled" in entry["error"]

    def test_an_empty_answer_is_failed(self, spawn, bridges):
        bridges("ptah", reply={"success": True, "response": ""})
        entry = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}])["results"][0]
        assert entry["status"] == "failed" and "Do NOT guess" in entry["error"]


# ── The live dispatch paths reach the hand-off ───────────────────────────────

class TestLivePaths:
    def test_run_agent_dispatch_goes_through_the_hand_off(self, spawn, bridges):
        import run_agent
        b = bridges("ptah")
        fake_self = types.SimpleNamespace(valid_tool_names={"ask_agent"}, _delegate_depth=0)
        out = json.loads(run_agent.AIAgent._dispatch_delegate_task(
            fake_self, {"tasks": [{"goal": "Draft the weekly post", "agent": "ptah"}]}))
        assert out["handed_to"] == ["ptah"] and len(b.requests) == 1 and spawn == []

    def test_the_registry_handler_goes_through_the_hand_off(self, spawn, bridges):
        from tools.registry import registry
        b = bridges("set")
        handler = registry.get_entry("delegate_task").handler
        out = json.loads(handler({"tasks": [{"goal": "Reconcile the September ledger", "agent": "set"}]},
                                 parent_agent=parent()))
        assert out["handed_to"] == ["set"] and len(b.requests) == 1 and spawn == []


# ── ask_agent's structured status decides (round 2: F19 replay) ─────────────

#: ask_agent's replies for a teammate that did not finish, in the shape the
#: fleet's delegation-honesty change (hermes-agent fix/delegation-honesty)
#: gives them: none of these errors says "timed out".
_TIMEOUT_REPLY = {
    "success": False, "agent": "neith", "status": "timeout", "unfinished": True,
    "still_running": True,
    "files_written": [{"path": "/Users/o/research/acme.md", "bytes": 28672}],
    "error": "neith did not answer within the 300-second window. neith is still working.",
    "guidance": "Tell the user plainly that it is unfinished. Never write over any files listed here.",
}
_BUSY_REPLY = {
    "success": False, "agent": "neith", "status": "busy",
    "error": "neith is still working on your earlier request, started 14:02; this one was not started.",
    "guidance": "Do not re-send it now. The earlier result comes back to you when it finishes.",
}


def stub_ask(monkeypatch, *replies):
    """ask_agent answers with ``replies`` in order; records the questions."""
    asked: list[tuple[str, str]] = []
    queue = list(replies)

    def fake(agent, question, sender=""):
        asked.append((agent, question))
        return json.dumps(queue.pop(0) if len(queue) > 1 else queue[0])

    monkeypatch.setattr(ask_agent_tool, "ask_agent", fake)
    return asked


class TestStructuredStatus:
    def test_a_timeout_without_the_words_is_still_a_timeout(self, spawn, monkeypatch):
        stub_ask(monkeypatch, _TIMEOUT_REPLY)
        entry = run(tasks=[{"goal": "Research Acme's Q3 filings", "agent": "neith"}])["results"][0]
        assert entry["status"] == "timeout" and entry["unfinished"] is True
        assert entry["summary"] == ""
        assert entry["still_running"] is True
        assert entry["files_written"] == [{"path": "/Users/o/research/acme.md", "bytes": 28672}]
        assert entry["guidance"] == _TIMEOUT_REPLY["guidance"]     # the tool's own words
        assert "still working" in entry["error"]

    @pytest.mark.parametrize("status", ["busy", "duplicate"])
    def test_busy_and_duplicate_are_unfinished_not_failed(self, spawn, monkeypatch, status):
        stub_ask(monkeypatch, dict(_BUSY_REPLY, status=status))
        entry = run(tasks=[{"goal": "Research Acme's Q3 filings", "agent": "neith"}])["results"][0]
        assert entry["status"] == status and entry["unfinished"] is True
        assert entry["guidance"] == _BUSY_REPLY["guidance"]
        assert "NOT done" not in entry["guidance"]                  # it was not "not taken"

    def test_busy_without_guidance_gets_ours(self, spawn, monkeypatch):
        stub_ask(monkeypatch, {"success": False, "status": "busy", "error": "neith is busy"})
        entry = run(tasks=[{"goal": "Research Acme's Q3 filings", "agent": "neith"}])["results"][0]
        assert entry["status"] == "busy" and "Do not send it again now" in entry["guidance"]

    @pytest.mark.parametrize("reply", [
        {"success": False, "refused": True, "error": "neith already handled this request"},
        {"success": False, "status": "not_delivered", "error": "would loop"},
    ])
    def test_refusals_are_refused(self, spawn, monkeypatch, reply):
        stub_ask(monkeypatch, reply)
        entry = run(tasks=[{"goal": "Research Acme's Q3 filings", "agent": "neith"}])["results"][0]
        assert entry["status"] == "refused" and "Do not retry" in entry["guidance"]

    def test_the_wording_is_only_the_fallback(self, spawn, monkeypatch):
        # No status at all (an older ask_agent): the error text still tells a timeout.
        stub_ask(monkeypatch, {"success": False, "error": "timed out"})
        entry = run(tasks=[{"goal": "Research Acme's Q3 filings", "agent": "neith"}])["results"][0]
        assert entry["status"] == "timeout" and "do not write over it" in entry["guidance"]
        stub_ask(monkeypatch, {"success": False, "error": "neith's bridge is unreachable"})
        entry = run(tasks=[{"goal": "Research Acme's Q3 filings", "agent": "neith"}])["results"][0]
        assert entry["status"] == "failed" and "NOT done" in entry["guidance"]

    def test_a_504_with_a_timeout_body_over_http(self, spawn, bridges, monkeypatch):
        """The F19 Sep 26 replay at the HTTP level: the far bridge answers 504
        with what it knows. Whatever structure ask_agent reads from it is
        carried into the entry, and it is never reported as "failed"."""
        bridges("neith", status=504, reply={
            "success": False, "timed_out": True, "agent_id": "neith", "status": "timeout",
            "still_running": True, "files_written": [{"path": "/Users/o/research/acme.md"}],
            "error": "neith did not finish within 5 minutes.",
            "guidance": "It is still working; never write over its files."})
        seen: list[dict] = []
        real = ask_agent_tool.ask_agent

        def recording(agent, question, sender=""):
            out = real(agent, question, sender=sender)
            seen.append(json.loads(out))
            return out

        monkeypatch.setattr(ask_agent_tool, "ask_agent", recording)
        entry = run(tasks=[{"goal": "Research Acme's Q3 filings", "agent": "neith"}])["results"][0]
        assert entry["status"] == "timeout" and entry["summary"] == ""
        assert "unfinished" in entry["guidance"].lower()
        [raw] = seen
        for key in ("still_running", "files_written"):
            if key in raw:
                assert entry[key] == raw[key]
        if raw.get("guidance"):
            assert entry["guidance"] == raw["guidance"]

    def test_a_409_busy_over_http_is_never_completed(self, spawn, bridges, monkeypatch):
        bridges("neith", status=409, reply={"success": False, "busy": True, "agent_id": "neith",
                                            "status": "busy", "error": "neith is still working",
                                            "guidance": "Do not re-send it now."})
        seen: list[dict] = []
        real = ask_agent_tool.ask_agent
        monkeypatch.setattr(ask_agent_tool, "ask_agent",
                            lambda a, q, sender="": seen.append(json.loads(real(a, q, sender=sender)))
                            or json.dumps(seen[-1]))
        entry = run(tasks=[{"goal": "Research Acme's Q3 filings", "agent": "neith"}])["results"][0]
        assert entry["status"] != "completed" and entry["summary"] == ""
        # The status ask_agent reports is the entry's (busy once ask_agent reads 409 bodies).
        assert entry["status"] == (seen[0].get("status") or "failed")


# ── output_schema on a hand-off (round 2: Codex P1) ──────────────────────────

_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}


class TestOutputSchema:
    def test_a_valid_answer_completes_with_schema_valid(self, spawn, bridges):
        b = bridges("ptah", reply={"success": True, "response": '{"verdict": "ship it"}'})
        entry = run(tasks=[{"goal": "Review the launch post", "agent": "ptah",
                            "output_schema": _SCHEMA}])["results"][0]
        assert entry["status"] == "completed" and entry["schema_valid"] is True
        assert "schema_retries" not in entry and len(b.requests) == 1

    def test_one_correction_re_ask_fixes_it(self, spawn, bridges):
        b = bridges("ptah", replies=[{"success": True, "response": "Looks good to me, ship it."}],
                    reply={"success": True, "response": '{"verdict": "ship it"}'})
        entry = run(tasks=[{"goal": "Review the launch post", "agent": "ptah",
                            "output_schema": _SCHEMA}])["results"][0]
        assert entry["status"] == "completed" and entry["schema_valid"] is True
        assert entry["schema_retries"] == 1 and entry["summary"] == '{"verdict": "ship it"}'
        first, again = (r["json"]["messages"][0]["content"] for r in b.requests)
        # A fresh session on the far side: the re-ask carries the answer and the schema.
        assert "Looks good to me, ship it." in again and "OUTPUT CONTRACT" in again
        assert "rejected by the output contract validator" in again

    def test_still_wrong_after_one_re_ask_is_failed_not_completed(self, spawn, bridges):
        b = bridges("ptah", reply={"success": True, "response": "Looks good to me."})
        entry = run(tasks=[{"goal": "Review the launch post", "agent": "ptah",
                            "output_schema": _SCHEMA}])["results"][0]
        assert entry["status"] == "failed" and entry["schema_valid"] is False
        assert entry["schema_retries"] == 1 and entry["schema_errors"]
        assert entry["summary"] == "Looks good to me."             # kept, as upstream keeps it
        assert "output_schema" in entry["error"] and len(b.requests) == 2

    def test_the_top_level_schema_applies_to_a_one_task_call(self, spawn, bridges):
        b = bridges("ptah", reply={"success": True, "response": '{"verdict": "ok"}'})
        entry = run(tasks=[{"goal": "Review the launch post", "agent": "ptah"}],
                    output_schema=_SCHEMA)["results"][0]
        assert entry["schema_valid"] is True
        assert "OUTPUT CONTRACT" in b.requests[0]["json"]["messages"][0]["content"]

    def test_the_top_level_schema_does_not_apply_to_a_batch(self, spawn, bridges):
        b = bridges("ptah")
        out = run(tasks=[{"goal": "Review the launch post", "agent": "ptah"},
                         {"goal": "Summarize the three attached reports"}],
                  output_schema=_SCHEMA)
        assert "schema_valid" not in out["teammate_results"][0]
        assert "OUTPUT CONTRACT" not in b.requests[0]["json"]["messages"][0]["content"]
        assert spawn[0]["output_schema"] is None                   # nor to the local remainder

    def test_a_malformed_schema_is_refused_before_anything_leaves(self, spawn, bridges):
        b = bridges("ptah")
        out = run(tasks=[{"goal": "Review the launch post", "agent": "ptah",
                          "output_schema": {"type": "not-a-type"}}])
        assert "output_schema invalid" in out["error"] and b.requests == []


# ── The whole call is checked before anything leaves (round 2: Codex P1/P2) ──

class TestPreflight:
    def test_the_spawn_pause_stops_hand_offs_too(self, spawn, bridges):
        from tools.delegate_tool_registry import set_spawn_paused
        b = bridges("ptah")
        set_spawn_paused(True)
        try:
            out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}])
        finally:
            set_spawn_paused(False)
        assert "paused" in out["error"] and b.requests == [] and spawn == []

    @pytest.mark.parametrize("bad_local", [
        {"goal": "TODO"},                                           # placeholder goal
        {"goal": "Summarize the <report_name> report"},             # template marker
        {"goal": "Summarize the three attached reports", "images": [""]},
        {"goal": "Summarize the three attached reports", "output_schema": "not json"},
    ])
    def test_a_bad_local_task_stops_the_teammates_too(self, spawn, bridges, bad_local):
        b = bridges("ptah")
        out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}, bad_local])
        assert out.get("error") and b.requests == [] and spawn == []

    def test_too_many_images_on_a_hand_off(self, spawn, bridges):
        b = bridges("ptah")
        out = run(tasks=[{"goal": "Review the mocks", "agent": "ptah",
                          "images": [f"/tmp/m{i}.png" for i in range(9)]}])
        assert "per-task limit" in out["error"] and b.requests == []

    def test_a_mixed_call_past_the_spawn_depth_sends_nothing(self, spawn, bridges):
        b = bridges("ptah")
        deep = types.SimpleNamespace(valid_tool_names={"ask_agent"}, _delegate_depth=5)
        out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"},
                         {"goal": "Summarize the three attached reports"}], parent_agent=deep)
        assert "depth limit" in out["error"] and b.requests == [] and spawn == []

    def test_a_hand_off_only_call_follows_the_fleet_budget_not_the_spawn_depth(self, spawn, bridges):
        # The bridge worker seeds the spawn depth from fleet hops; the fleet
        # budget (3 hops, no loops) is what governs a hand-off, as for ask_agent.
        b = bridges("ptah")
        hop = types.SimpleNamespace(valid_tool_names={"ask_agent"}, _delegate_depth=1)
        out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}], parent_agent=hop)
        assert out["results"][0]["status"] == "completed" and len(b.requests) == 1


# ── The legacy single-goal shape with a top-level agent (round 2) ────────────

class TestLegacyFold:
    def test_fold_only_the_legacy_shape(self):
        fold = th.fold_top_level_agent
        assert fold({"goal": "Draft the post", "agent": "ptah", "context": "c"}) == [
            {"goal": "Draft the post", "agent": "ptah", "context": "c"}]
        assert fold({"goal": "Draft the post", "tasks": [], "agent": "ptah"}) == [
            {"goal": "Draft the post", "agent": "ptah"}]
        tasks = [{"goal": "Draft the post"}]
        assert fold({"goal": "x", "tasks": tasks, "agent": "ptah"}) is tasks   # per-task shape: not read
        assert fold({"goal": "Draft the post", "agent": "  "}) is None
        assert fold({"goal": "Draft the post"}) is None
        assert fold({"agent": "ptah"}) is None

    def test_run_agent_dispatch_hands_the_legacy_shape_off_once(self, spawn, bridges):
        import run_agent
        b = bridges("ptah")
        fake_self = types.SimpleNamespace(valid_tool_names={"ask_agent"}, _delegate_depth=0)
        out = json.loads(run_agent.AIAgent._dispatch_delegate_task(
            fake_self, {"goal": "Draft the weekly post", "context": "Plain voice.", "agent": "ptah"}))
        assert out["handed_to"] == ["ptah"] and spawn == []        # never also a local subagent
        assert b.requests[0]["json"]["messages"][0]["content"] == (
            "Draft the weekly post\n\nContext:\nPlain voice.")

    def test_the_registry_handler_folds_too(self, spawn, bridges):
        from tools.registry import registry
        b = bridges("set")
        handler = registry.get_entry("delegate_task").handler
        out = json.loads(handler({"goal": "Reconcile the September ledger", "agent": "set"},
                                 parent_agent=parent()))
        assert out["handed_to"] == ["set"] and len(b.requests) == 1 and spawn == []

    def test_a_legacy_name_is_refused_not_run_locally(self, spawn, bridges):
        import run_agent
        b = bridges("neith")
        fake_self = types.SimpleNamespace(valid_tool_names={"ask_agent"}, _delegate_depth=0)
        out = json.loads(run_agent.AIAgent._dispatch_delegate_task(
            fake_self, {"goal": "Research Acme", "agent": "Fox"}))
        assert "not an agent id" in out["error"] and b.requests == [] and spawn == []


# ── What the bridge worker is told will leave (round 2) ──────────────────────

class TestPlannedHandoffs:
    def test_planned_is_what_is_sent(self, spawn, bridges):
        bridges("ptah"), bridges("set")
        args = {"tasks": [{"goal": "Summarize the three attached reports"},
                          {"goal": "Draft the weekly post", "agent": "ptah"},
                          {"goal": "Reconcile the September ledger", "agent": "SET"},
                          {"goal": "Outline the memo for the board", "agent": "thoth"}]}
        assert th.planned_handoffs(args, parent()) == [{"task_index": 1, "agent": "ptah"},
                                                       {"task_index": 2, "agent": "set"}]
        out = run(**args)
        assert [(e["task_index"], e["agent"]) for e in out["teammate_results"]] == [(1, "ptah"), (2, "set")]

    @pytest.mark.parametrize("args, who", [
        ({"tasks": [{"goal": "Draft the weekly post", "agent": "Fox"}]}, parent()),
        ({"tasks": [{"goal": "Draft the weekly post", "agent": "ptah"}]}, parent(tools=("delegate_task",))),
        ({"tasks": [{"goal": "Draft the weekly post", "agent": "ptah"}, {"goal": "TODO"}]}, parent()),
        ({"tasks": [{"goal": "Draft the weekly post"}]}, parent()),
        ({"tasks": [{"goal": "Draft the weekly post", "agent": "thoth"}]}, parent()),
        ({"action": "list", "tasks": [{"goal": "Draft the weekly post", "agent": "ptah"}]}, parent()),
        ({"tasks": [{"goal": "Draft the weekly post", "agent": "ptah"}]}, None),
        ("not a dict", parent()),
    ])
    def test_nothing_planned_when_nothing_leaves(self, args, who):
        assert th.planned_handoffs(args, who) == []

    def test_a_loop_is_not_planned(self, monkeypatch):
        monkeypatch.setenv("FLEET_DELEGATION_DEPTH", "1")
        monkeypatch.setenv("FLEET_DELEGATION_VISITED", "ptah")
        args = {"tasks": [{"goal": "Draft the weekly post", "agent": "ptah"},
                          {"goal": "Reconcile the September ledger", "agent": "set"}]}
        assert th.planned_handoffs(args, parent()) == [{"task_index": 1, "agent": "set"}]

    def test_the_legacy_fold_is_planned(self):
        assert th.planned_handoffs({"goal": "Draft the weekly post", "agent": "ptah"}, parent()) == [
            {"task_index": 0, "agent": "ptah"}]

    def test_planning_sends_nothing(self, bridges):
        b = bridges("ptah")
        th.planned_handoffs({"tasks": [{"goal": "Draft the weekly post", "agent": "ptah"}]}, parent())
        assert b.requests == []


# ── Sessions without the fleet tools never see the field (round 2) ───────────

class TestSchemaRewrite:
    def _defs(self, names):
        import model_tools
        from tools.registry import registry
        return {d["function"]["name"]: d for d in
                model_tools._apply_dynamic_schemas(registry.get_definitions(set(names), quiet=True))}

    def test_no_fleet_tools_no_agent_field(self):
        cron = self._defs({"delegate_task", "terminal"})["delegate_task"]
        items = cron["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]
        assert "agent" not in items and "goal" in items

    def test_with_the_fleet_tools_the_field_stays(self):
        chat = self._defs({"delegate_task", "ask_agent"})["delegate_task"]
        items = chat["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]
        assert sorted(items["agent"]["enum"]) == FLEET_IDS

    def test_the_static_schema_is_never_mutated(self):
        self._defs({"delegate_task"})
        assert "agent" in delegate_tool.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]


# ── The wait is not a stall (round 2) ────────────────────────────────────────

class TestWaitingNotes:
    def test_a_long_hand_off_sends_waiting_notes(self, spawn, bridges, monkeypatch):
        monkeypatch.setattr(th, "_HEARTBEAT_S", 0.05)
        bridges("ptah", delay=0.4)
        notes: list[tuple[str, str]] = []
        touched: list[str] = []
        who = types.SimpleNamespace(valid_tool_names={"ask_agent"}, _delegate_depth=0,
                                    status_callback=lambda kind, msg: notes.append((kind, msg)),
                                    _touch_activity=lambda desc, **kw: touched.append(desc))
        out = run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}], parent_agent=who)
        assert out["results"][0]["status"] == "completed"
        assert notes and all(kind == "lifecycle" and "Still waiting" in msg for kind, msg in notes)
        assert touched

    def test_a_quick_hand_off_sends_none(self, spawn, bridges):
        bridges("ptah")
        notes: list = []
        who = types.SimpleNamespace(valid_tool_names={"ask_agent"}, _delegate_depth=0,
                                    status_callback=lambda *a: notes.append(a))
        run(tasks=[{"goal": "Draft the weekly post", "agent": "ptah"}], parent_agent=who)
        assert notes == []
