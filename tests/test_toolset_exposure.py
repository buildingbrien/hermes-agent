"""Tools that register into a toolset which does not list them are invisible.

The incident this file exists for (2026-08-16): v4.6.38 shipped "agents can
finally read gbrain". ``tools/gbrain_tool.py`` registered ``gbrain_search`` and
``gbrain_read`` with ``toolset="memory"``, but ``TOOLSETS["memory"]["tools"]``
was ``["memory"]``. Registration records membership; ``resolve_toolset()``
decides exposure, and it returns exactly the declared list. Asked directly
which tools they had, Ptah and Neith both answered "ask_agent, fleet_send,
session_search". 854 compiled pages, unreadable, for a full release — with no
warning at import, at registration, or at call time.

``ask_agent`` was added the same day and worked, which is what made the bug so
hard to see: its toolset ``"fleet"`` is not declared in ``TOOLSETS`` at all, so
``get_toolset()`` falls through to the registry and returns whatever registered
under it. Undeclared toolset = reachable; declared-but-not-listed = dead.
"""

import logging

import pytest

import toolsets
from tools.registry import ToolRegistry
from toolsets import (
    TOOLSETS,
    find_unexposed_tools,
    report_unexposed_tools,
    resolve_toolset,
)


# Tools that were ALREADY shadowed when this audit was written (2026-08-16),
# both found by the audit's first run. Deliberately left unfixed here: handing
# a tool to every agent on the fleet is a product decision owned by whoever
# ships that tool, not a mechanical edit to make while adding a check.
#
# Freezing them (rather than skipping them) makes this test fail in BOTH
# directions — on any NEW shadowed tool, and again the moment one of these is
# properly exposed, at which point delete it from this set.
# Empty, and it should stay that way.
#
# It briefly held action_items and read_document, both found dead by this audit
# on the day it was written (2026-08-16) and both fixed the same hour rather
# than parked:
#   action_items  — the durable commitments store. The voice-call prompt orders
#                   every agent to call it before promising to track or close
#                   anything, citing a real call where an agent said "noting it
#                   as closed everywhere" six ways and wrote nothing. The prompt
#                   was demanding a tool the agent did not have; the founder had
#                   been reading that as agents not keeping their word.
#   read_document — agents could not read a PDF at all (read_file returns bytes).
#
# A name belongs here only while someone has DECIDED a tool should stay
# unreachable. "We haven't got to it" is not that decision — it is the gbrain
# bug with a note attached.
KNOWN_UNEXPOSED: frozenset = frozenset()


def _dummy_handler(args, **kwargs):
    return "{}"


def _make_schema(name: str):
    return {
        "name": name,
        "description": "test tool",
        "parameters": {"type": "object", "properties": {}},
    }


class TestTheGbrainIncident:
    """Reproduce the exact shape of the 2026-08-16 failure."""

    def test_tool_registered_into_a_toolset_that_omits_it_is_flagged(self, monkeypatch):
        # Put "memory" back the way it was when v4.6.38 shipped.
        monkeypatch.setitem(TOOLSETS["memory"], "tools", ["memory"])

        findings = find_unexposed_tools({
            "memory": "memory",
            "gbrain_search": "memory",
            "gbrain_read": "memory",
        })

        assert [f["tool"] for f in findings] == ["gbrain_read", "gbrain_search"]
        for f in findings:
            assert f["toolset"] == "memory"
            # No declared toolset listed them — that is why both agents
            # reported having no gbrain tools at all, not a degraded version.
            assert f["exposed_by"] == []
            assert f["severity"] == "invisible"
        assert findings[0]["remedy"] == (
            'add "gbrain_read" to TOOLSETS["memory"]["tools"] in toolsets.py'
        )

    def test_ask_agent_pattern_is_not_flagged(self):
        """An UNDECLARED toolset is reachable via the registry fallback.

        ask_agent/fleet_send register with toolset="fleet", which is not a
        TOOLSETS key, so get_toolset() serves them from the registry. Flagging
        this would train people to ignore the audit.
        """
        findings = find_unexposed_tools({
            "ask_agent": "fleet",
            "fleet_send": "fleet",
        })

        assert findings == []

    def test_memory_toolset_still_exposes_gbrain(self):
        """Guards the v4.6.38 exposure fix against a silent revert."""
        memory_tools = resolve_toolset("memory")

        assert "gbrain_search" in memory_tools, (
            "gbrain_search dropped out of the 'memory' toolset — agents lose "
            "read access to the compiled brain again (the 2026-08-16 bug)"
        )
        assert "gbrain_read" in memory_tools


class TestFindUnexposedTools:
    def test_listed_tool_is_not_flagged(self):
        assert find_unexposed_tools({"web_search": "web"}) == []

    def test_tool_exposed_only_by_another_toolset_is_misfiled_not_invisible(self):
        # "browser_navigate" is listed by the "browser" toolset, so a tool
        # claiming toolset="web" under that name still reaches agents — wrong
        # home, but not dead. Different severity, different remedy urgency.
        findings = find_unexposed_tools({"browser_navigate": "web"})

        assert len(findings) == 1
        assert findings[0]["severity"] == "misfiled"
        assert "browser" in findings[0]["exposed_by"]

    def test_composed_toolsets_count_as_exposure(self):
        # "debugging" has tools=["terminal","process"] and includes=["web","file"],
        # so read_file is exposed by composition, not by its own list. The audit
        # asks resolve_toolset(), never the raw ["tools"] key.
        assert find_unexposed_tools({"read_file": "debugging"}) == []

    def test_empty_registry_is_soft(self):
        # A plain hermes-agent host with nothing registered must get silence,
        # never an exception raised into a turn.
        assert find_unexposed_tools({}) == []


class TestLoudReporting:
    def test_invisible_tool_logs_error_with_name_toolset_and_remedy(self, caplog):
        with caplog.at_level(logging.WARNING, logger="toolsets"):
            report_unexposed_tools({"orphan_tool": "memory"})

        text = caplog.text
        assert "orphan_tool" in text
        assert "memory" in text
        assert 'TOOLSETS["memory"]["tools"]' in text
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_clean_registry_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="toolsets"):
            assert report_unexposed_tools({"web_search": "web"}) == []

        assert caplog.records == []


class TestAuditActuallyRuns:
    """The check is worthless if nothing calls it — test the wiring, not just
    the function. resolve_toolset() is the funnel every caller that builds an
    agent's tool list goes through."""

    def test_resolve_toolset_triggers_the_audit(self, monkeypatch, caplog):
        reg = ToolRegistry()
        reg.register(
            name="shadowed_at_startup",
            toolset="memory",
            schema=_make_schema("shadowed_at_startup"),
            handler=_dummy_handler,
        )
        monkeypatch.setattr("tools.registry.registry", reg)
        monkeypatch.setattr(toolsets, "_exposure_audit_done", False)

        with caplog.at_level(logging.WARNING, logger="toolsets"):
            resolve_toolset("web")

        assert "shadowed_at_startup" in caplog.text
        assert toolsets._exposure_audit_done is True

    def test_audit_runs_once_per_process(self, monkeypatch, caplog):
        reg = ToolRegistry()
        reg.register(
            name="shadowed_at_startup",
            toolset="memory",
            schema=_make_schema("shadowed_at_startup"),
            handler=_dummy_handler,
        )
        monkeypatch.setattr("tools.registry.registry", reg)
        monkeypatch.setattr(toolsets, "_exposure_audit_done", False)

        resolve_toolset("web")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="toolsets"):
            resolve_toolset("web")
            resolve_toolset("memory")

        assert caplog.records == []

    def test_asking_the_question_does_not_fire_the_report(self, monkeypatch, caplog):
        """find_unexposed_tools() must stay side-effect free.

        It resolves every declared toolset internally. If those resolves went
        through the top-level path they would latch the once-per-process
        report and dump the LIVE registry's findings into whatever log the
        caller was watching — including a test asserting on an injected map.
        """
        reg = ToolRegistry()
        reg.register(
            name="shadowed_at_startup",
            toolset="memory",
            schema=_make_schema("shadowed_at_startup"),
            handler=_dummy_handler,
        )
        monkeypatch.setattr("tools.registry.registry", reg)
        monkeypatch.setattr(toolsets, "_exposure_audit_done", False)

        with caplog.at_level(logging.WARNING, logger="toolsets"):
            assert find_unexposed_tools({"web_search": "web"}) == []

        assert caplog.records == []
        assert toolsets._exposure_audit_done is False

    def test_empty_registry_does_not_latch_the_audit(self, monkeypatch):
        """An early resolve_toolset() must not disable the audit for good.

        toolsets.py is imported before the tool modules register anything, so
        a resolve that lands in that window sees an empty registry. Latching
        there would mean the audit never runs on that process — the exact
        silence this whole check exists to remove.
        """
        monkeypatch.setattr("tools.registry.registry", ToolRegistry())
        monkeypatch.setattr(toolsets, "_exposure_audit_done", False)

        resolve_toolset("web")

        assert toolsets._exposure_audit_done is False


@pytest.fixture(scope="module")
def live_findings():
    # Importing model_tools runs discover_builtin_tools() at module level,
    # which is exactly how the running agent populates the registry.
    import model_tools  # noqa: F401

    return find_unexposed_tools()


class TestLiveRegistryRatchet:
    """CI-facing: the next person who adds a tool to an existing toolset finds
    out here, not from a founder asking why his agent forgot something."""

    def test_no_tool_is_shadowed_by_its_own_toolset(self, live_findings):
        shadowed = {f["tool"] for f in live_findings}

        assert shadowed == set(KNOWN_UNEXPOSED), (
            "Tool exposure drifted.\n"
            f"  newly shadowed: {sorted(shadowed - KNOWN_UNEXPOSED)}\n"
            f"  no longer shadowed: {sorted(KNOWN_UNEXPOSED - shadowed)}\n"
            "A newly shadowed tool registered into a toolset that does not "
            'list it — add it to TOOLSETS["<toolset>"]["tools"] in '
            "toolsets.py, or it is invisible to every agent (see the gbrain "
            "incident at the top of this file). If the name looks like test "
            "scaffolding it leaked from another test's registry fixture. "
            "If a tool is no longer shadowed, it was fixed: delete it from "
            "KNOWN_UNEXPOSED."
        )

    def test_known_unexposed_tools_are_dead_not_merely_misfiled(self, live_findings):
        # Recorded precisely so the debt cannot be misread as cosmetic: no
        # declared toolset lists these, so no agent can call them at all.
        for f in live_findings:
            assert f["severity"] == "invisible", f
            assert f["exposed_by"] == [], f
