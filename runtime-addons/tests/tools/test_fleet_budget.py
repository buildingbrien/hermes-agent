"""One delegation budget for the whole fleet (R2-2-23, HA4).

delegate_to_neith carried its own copy with a hop cap of 1 while the bridge
worker (hermes-bridge/worker.py::max_fleet_depth) and the rest of the fleet
used 3 — so an agent handling any bus-delivered task (depth 1) was refused
research delegation. And its "automatic research-subagent fallback" never ran:
the registry never hands a tool the running agent. Now tools/fleet_budget.py is
the single implementation, fleet_send and delegate_to_neith both use it, and a
failed delegation says plainly that no fallback was available.
"""

import json
import os
import urllib.error
from unittest import mock

import pytest

from tools import delegate_neith as dn
from tools import fleet_budget as fb
from tools import fleet_send as fs

#: hermes-bridge/worker.py::max_fleet_depth default — the third implementation,
#: outside this repo. If the bridge changes it, change it here in the same tag.
BRIDGE_WORKER_DEFAULT = 3


class TestOneCapEverywhere:
    def test_default_cap_is_three_in_every_implementation(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("MAX_FLEET_DEPTH", None)
            assert fb.max_fleet_depth() == dn._max_fleet_depth() == fb.MAX_FLEET_DEPTH_DEFAULT == BRIDGE_WORKER_DEFAULT

    def test_env_override_and_junk(self):
        with mock.patch.dict(os.environ, {"MAX_FLEET_DEPTH": "2"}):
            assert fb.max_fleet_depth() == dn._max_fleet_depth() == 2
        with mock.patch.dict(os.environ, {"MAX_FLEET_DEPTH": "junk"}):
            assert fb.max_fleet_depth() == 3

    def test_the_two_tools_attach_identical_next_hop_fields(self):
        with mock.patch.dict(os.environ, {"FLEET_DELEGATION_DEPTH": "1", "FLEET_DELEGATION_ORIGIN": "Thoth",
                                          "FLEET_DELEGATION_VISITED": "Thoth"}):
            assert fs._delegation_budget_fields("neith") == dn.next_hop_fields("neith") == {
                "delegation_depth": 2, "delegation_origin": "thoth", "delegation_visited": ["thoth", "neith"]}
        with mock.patch.dict(os.environ, {"FLEET_DELEGATION_DEPTH": "", "FLEET_DELEGATION_ORIGIN": "",
                                          "FLEET_DELEGATION_VISITED": ""}):
            assert fs._delegation_budget_fields("Thoth") == {
                "delegation_depth": 1, "delegation_origin": "thoth", "delegation_visited": ["thoth"]}

    def test_budget_from_env(self):
        with mock.patch.dict(os.environ, {"FLEET_DELEGATION_DEPTH": "1", "FLEET_DELEGATION_ORIGIN": "Thoth",
                                          "FLEET_DELEGATION_VISITED": "Thoth, Neith ,thoth"}):
            assert dn._fleet_budget_from_env() == fb.budget_from_env() == (1, "thoth", ["thoth", "neith"])


class TestRefusal:
    def test_bus_delivered_task_may_still_delegate_research(self):
        """depth 1 (ptah handling a task thoth sent it) -> neith is within a cap of 3."""
        with mock.patch.dict(os.environ):
            os.environ.pop("MAX_FLEET_DEPTH", None)
            assert dn._budget_refusal("ptah", 1, ["thoth"]) is None
            assert dn._budget_refusal("set", 2, ["thoth", "ptah"]) is None

    def test_over_budget_and_loops_are_refused(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("MAX_FLEET_DEPTH", None)
            assert "limit" in dn._budget_refusal("set", 3, ["thoth", "ptah", "set"])
            assert "loop" in dn._budget_refusal("ptah", 1, ["thoth", "neith"])
            assert "yourself" in dn._budget_refusal("neith", 0, [])
            assert "loop" in fb.refusal_reason("ptah", "set", 1, "Thoth, PTAH")


class TestHonestFallback:
    @pytest.fixture
    def neith_down(self, monkeypatch):
        monkeypatch.setattr(dn, "_call_neith_sync", mock.Mock(side_effect=urllib.error.URLError("refused")))
        monkeypatch.setenv("BRIDGE_PROFILE", "thoth")
        for k in ("FLEET_DELEGATION_DEPTH", "FLEET_DELEGATION_ORIGIN", "FLEET_DELEGATION_VISITED", "MAX_FLEET_DEPTH"):
            monkeypatch.delenv(k, raising=False)

    def test_registry_call_reports_no_fallback_instead_of_pretending(self, neith_down, monkeypatch):
        spy = mock.Mock(return_value="should never be called")
        monkeypatch.setattr(dn, "_fallback_subagent", spy)
        out = json.loads(dn.delegate_to_neith_tool({"task": "find X"}, task_id="t", session_id="s", user_task="u"))
        assert out["status"] == "failed" and "no in-process research fallback" in out["error"], out
        assert out.get("source") == "neith"  # never "research_subagent_fallback"
        spy.assert_not_called()

    def test_fallback_runs_when_a_parent_agent_is_supplied(self, neith_down, monkeypatch):
        monkeypatch.setattr(dn, "_fallback_subagent", lambda task, agent: "subagent findings")
        out = json.loads(dn.delegate_to_neith_tool({"task": "find X"}, parent_agent=object()))
        assert out["source"] == "research_subagent_fallback" and out["result"] == "subagent findings"

    def test_schema_no_longer_promises_an_automatic_fallback(self):
        assert "automatically handled" not in dn.DELEGATE_TO_NEITH_SCHEMA["description"]


def test_bridge_caps_match_when_a_lucaryin_ai_checkout_is_present():
    """The third implementation lives in the bridge (worker.py max_fleet_depth and
    server.py's receiver-side copy). CI pins it via BRIDGE_WORKER_DEFAULT; with a
    lucaryin-ai checkout next to this repo (or LUCARYIN_AI_DIR) the live source
    is read too, so a bridge-side change cannot drift silently."""
    import ast
    import os
    from pathlib import Path
    roots = [Path(os.environ["LUCARYIN_AI_DIR"])] if os.environ.get("LUCARYIN_AI_DIR") else []
    roots.append(Path(__file__).resolve().parents[2].parents[2] / "lucaryin-ai")
    root = next((r for r in roots if (r / "hermes-bridge" / "worker.py").is_file()), None)
    if root is None:
        pytest.skip("no lucaryin-ai checkout next to this repo (set LUCARYIN_AI_DIR)")
    defaults = {}
    for rel in ("hermes-bridge/worker.py", "hermes-bridge/server.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name in ("max_fleet_depth", "_max_fleet_depth"):
                for call in ast.walk(fn):
                    if (isinstance(call, ast.Call) and len(call.args) == 2
                            and isinstance(call.args[0], ast.Constant) and call.args[0].value == "MAX_FLEET_DEPTH"
                            and isinstance(call.args[1], ast.Constant)):
                        defaults[f"{rel}:{fn.name}"] = int(call.args[1].value)
    assert defaults, "no MAX_FLEET_DEPTH default found in the bridge"
    assert set(defaults.values()) == {fb.MAX_FLEET_DEPTH_DEFAULT}, defaults
