#!/usr/bin/env python3
"""
Toolsets Module

This module provides a flexible system for defining and managing tool aliases/toolsets.
Toolsets allow you to group tools together for specific scenarios and can be composed
from individual tools or other toolsets.

Features:
- Define custom toolsets with specific tools
- Compose toolsets from other toolsets
- Built-in common toolsets for typical use cases
- Easy extension for new toolsets
- Support for dynamic toolset resolution

Usage:
    from toolsets import get_toolset, resolve_toolset, get_all_toolsets
    
    # Get tools for a specific toolset
    tools = get_toolset("research")
    
    # Resolve a toolset to get all tool names (including from composed toolsets)
    all_tools = resolve_toolset("full_stack")
"""

import logging
import threading
from typing import List, Dict, Any, Set, Optional

logger = logging.getLogger(__name__)


# Shared tool list for CLI and all messaging platform toolsets.
# Edit this once to update all platforms simultaneously.
_HERMES_CORE_TOOLS = [
    # Web
    "web_search", "web_extract",
    # Terminal + process management
    "terminal", "process",
    # File manipulation
    "read_file", "write_file", "patch", "search_files",
    # Vision + image generation
    "vision_analyze", "image_generate",
    # Skills
    "skills_list", "skill_view", "skill_manage",
    # Browser automation
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_type", "browser_scroll", "browser_back",
    "browser_press", "browser_get_images",
    "browser_vision", "browser_console", "browser_cdp",
    # Text-to-speech
    "text_to_speech",
    # Planning & memory
    "todo", "memory",
    # Session history search
    "session_search",
    # Clarifying questions
    "clarify",
    # Code execution + delegation
    "execute_code", "delegate_task",
    # Cronjob management
    "cronjob",
    # Cross-platform messaging (gated on gateway running via check_fn)
    "send_message",
    # Fleet inter-agent bus messaging
    "fleet_send",
    # Synchronous research delegation to the persistent Neith agent (:9007)
    "delegate_to_neith",
    # Home Assistant smart home control (gated on HASS_TOKEN via check_fn)
    "ha_list_entities", "ha_get_state", "ha_list_services", "ha_call_service",
]


# Core toolset definitions
# These can include individual tools or reference other toolsets
TOOLSETS = {
    # Basic toolsets - individual tool categories
    "web": {
        "description": "Web research and content extraction tools",
        "tools": ["web_search", "web_extract"],
        "includes": []  # No other toolsets included
    },
    
    "search": {
        "description": "Web search only (no content extraction/scraping)",
        "tools": ["web_search"],
        "includes": []
    },
    
    "vision": {
        "description": "Image analysis and vision tools",
        "tools": ["vision_analyze"],
        "includes": []
    },
    
    "image_gen": {
        "description": "Creative generation tools (images)",
        "tools": ["image_generate"],
        "includes": []
    },
    
    "terminal": {
        "description": "Terminal/command execution and process management tools",
        "tools": ["terminal", "process"],
        "includes": []
    },
    
    "moa": {
        "description": "Advanced reasoning and problem-solving tools",
        "tools": ["mixture_of_agents"],
        "includes": []
    },
    
    "skills": {
        "description": "Access, create, edit, and manage skill documents with specialized instructions and knowledge",
        "tools": ["skills_list", "skill_view", "skill_manage"],
        "includes": []
    },
    
    "browser": {
        "description": "Browser automation for web interaction (navigate, click, type, scroll, iframes, hold-click) with web search for finding URLs",
        "tools": [
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_scroll", "browser_back",
            "browser_press", "browser_get_images",
            "browser_vision", "browser_console", "browser_cdp", "web_search"
        ],
        "includes": []
    },
    
    "cronjob": {
        "description": "Cronjob management tool - create, list, update, pause, resume, remove, and trigger scheduled tasks",
        "tools": ["cronjob"],
        "includes": []
    },
    
    "messaging": {
        "description": "Cross-platform messaging: send messages to Telegram, Discord, Slack, SMS, etc.",
        "tools": ["send_message"],
        "includes": []
    },
    
    "rl": {
        "description": "RL training tools for running reinforcement learning on Tinker-Atropos",
        "tools": [
            "rl_list_environments", "rl_select_environment",
            "rl_get_current_config", "rl_edit_config",
            "rl_start_training", "rl_check_status",
            "rl_stop_training", "rl_get_results",
            "rl_list_runs", "rl_test_inference"
        ],
        "includes": []
    },
    
    "file": {
        "description": "File manipulation tools: read, write, patch (with fuzzy matching), and search (content + files)",
        # read_document registers into this toolset (tools/read_document_tool.py)
        # but was never listed, so no agent could read a PDF — read_file hands
        # back raw bytes. Same silent-shadowing bug as gbrain.
        "tools": ["read_file", "write_file", "patch", "search_files", "read_document"],
        "includes": []
    },
    
    "tts": {
        "description": "Text-to-speech: convert text to audio with Edge TTS (free), ElevenLabs, OpenAI, or xAI",
        "tools": ["text_to_speech"],
        "includes": []
    },
    
    "todo": {
        "description": "Task planning and tracking for multi-step work",
        "tools": ["todo"],
        "includes": []
    },
    
    "memory": {
        "description": "Persistent memory across sessions (personal notes + user "
                       "profile) plus read access to the compiled long-term brain",
        # gbrain_search/gbrain_read register with toolset="memory" (see
        # tools/gbrain_tool.py), but registration does NOT grant exposure —
        # resolve_toolset() returns exactly this list, so a tool that registers
        # into an EXISTING toolset without being named here is silently
        # invisible to every agent. That is what happened: v4.6.38 shipped
        # "agents can finally read gbrain" and on 2026-08-16 both Ptah and
        # Neith, asked directly which tools they had, listed ask_agent and
        # fleet_send and no gbrain at all. 854 compiled pages, unreadable.
        # (ask_agent escaped this only because "fleet" is not a declared
        # toolset, so it never hit this filter.)
        #
        # action_items was dead the same way, and it explains a complaint the
        # founder has carried for months. The voice-call system prompt tells
        # every agent: "never say you will note, track, mark, close, send, or
        # follow up on something unless you make the matching tool call IN THIS
        # TURN — use the action_items tool for tracking and closing items",
        # citing a 2026-08-11 call where an agent said "noting it as closed
        # everywhere" six different ways and wrote nothing. The prompt was
        # ordering agents to call a tool they did not have. That was never a
        # discipline problem to be fixed with sterner wording.
        "tools": ["memory", "gbrain_search", "gbrain_read", "action_items"],
        "includes": []
    },
    
    "session_search": {
        "description": "Search and recall past conversations — your own, and "
                       "(on a fleet machine) your teammates' delegated work",
        # fleet_session_search/fleet_session_read register with
        # toolset="session_search" and MUST be named here or they are
        # invisible — the gbrain mistake directly above. They are gated at
        # runtime by check_fn: a host with no other agent stores never sees
        # them. Deliberately NOT added to _HERMES_CORE_TOOLS: that list is
        # shared by every messaging-platform toolset, and handing cross-agent
        # store reads to every Discord/Slack/Telegram deployment is a wider
        # default than this fix needs. The Lucaryin product path (bridge
        # worker → AIAgent with enabled_toolsets=None → union of all
        # toolsets) picks them up from here.
        "tools": ["session_search", "fleet_session_search", "fleet_session_read"],
        "includes": []
    },
    
    "clarify": {
        "description": "Ask the user clarifying questions (multiple-choice or open-ended)",
        "tools": ["clarify"],
        "includes": []
    },
    
    "code_execution": {
        "description": "Run Python scripts that call tools programmatically (reduces LLM round trips)",
        "tools": ["execute_code"],
        "includes": []
    },
    
    "delegation": {
        "description": "Spawn subagents with isolated context for complex subtasks",
        "tools": ["delegate_task"],
        "includes": []
    },

    # "honcho" toolset removed — Honcho is now a memory provider plugin.
    # Tools are injected via MemoryManager, not the toolset system.

    "homeassistant": {
        "description": "Home Assistant smart home control and monitoring",
        "tools": ["ha_list_entities", "ha_get_state", "ha_list_services", "ha_call_service"],
        "includes": []
    },

    "feishu_doc": {
        "description": "Read Feishu/Lark document content",
        "tools": ["feishu_doc_read"],
        "includes": []
    },

    "feishu_drive": {
        "description": "Feishu/Lark document comment operations (list, reply, add)",
        "tools": [
            "feishu_drive_list_comments", "feishu_drive_list_comment_replies",
            "feishu_drive_reply_comment", "feishu_drive_add_comment",
        ],
        "includes": []
    },


    # Scenario-specific toolsets
    
    "debugging": {
        "description": "Debugging and troubleshooting toolkit",
        "tools": ["terminal", "process"],
        "includes": ["web", "file"]  # For searching error messages and solutions, and file operations
    },
    
    "safe": {
        "description": "Safe toolkit without terminal access",
        "tools": [],
        "includes": ["web", "vision", "image_gen"]
    },
    
    # ==========================================================================
    # Full Hermes toolsets (CLI + messaging platforms)
    #
    # All platforms share the same core tools (including send_message,
    # which is gated on gateway running via its check_fn).
    # ==========================================================================

    "hermes-acp": {
        "description": "Editor integration (VS Code, Zed, JetBrains) — coding-focused tools without messaging, audio, or clarify UI",
        "tools": [
            "web_search", "web_extract",
            "terminal", "process",
            "read_file", "write_file", "patch", "search_files",
            "vision_analyze",
            "skills_list", "skill_view", "skill_manage",
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_scroll", "browser_back",
            "browser_press", "browser_get_images",
            "browser_vision", "browser_console", "browser_cdp",
            "todo", "memory",
            "session_search",
            "execute_code", "delegate_task",
        ],
        "includes": []
    },

    "hermes-api-server": {
        "description": "OpenAI-compatible API server — full agent tools accessible via HTTP (no interactive UI tools like clarify or send_message)",
        "tools": [
            # Web
            "web_search", "web_extract",
            # Terminal + process management
            "terminal", "process",
            # File manipulation
            "read_file", "write_file", "patch", "search_files",
            # Vision + image generation
            "vision_analyze", "image_generate",
            # Skills
            "skills_list", "skill_view", "skill_manage",
            # Browser automation
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_scroll", "browser_back",
            "browser_press", "browser_get_images",
            "browser_vision", "browser_console", "browser_cdp",
            # Planning & memory
            "todo", "memory",
            # Session history search
            "session_search",
            # Code execution + delegation
            "execute_code", "delegate_task",
            # Cronjob management
            "cronjob",
            # Home Assistant smart home control (gated on HASS_TOKEN via check_fn)
            "ha_list_entities", "ha_get_state", "ha_list_services", "ha_call_service",

        ],
        "includes": []
    },
    
    "hermes-cli": {
        "description": "Full interactive CLI toolset - all default tools plus cronjob management",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },
    
    "hermes-telegram": {
        "description": "Telegram bot toolset - full access for personal use (terminal has safety checks)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },
    
    "hermes-discord": {
        "description": "Discord bot toolset - full access (terminal has safety checks via dangerous command approval)",
        "tools": _HERMES_CORE_TOOLS + [
            # Discord server introspection & management (gated on DISCORD_BOT_TOKEN via check_fn)
            "discord_server",
        ],
        "includes": []
    },
    
    "hermes-whatsapp": {
        "description": "WhatsApp bot toolset - similar to Telegram (personal messaging, more trusted)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },
    
    "hermes-slack": {
        "description": "Slack bot toolset - full access for workspace use (terminal has safety checks)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },
    
    "hermes-signal": {
        "description": "Signal bot toolset - encrypted messaging platform (full access)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-bluebubbles": {
        "description": "BlueBubbles iMessage bot toolset - Apple iMessage via local BlueBubbles server",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-homeassistant": {
        "description": "Home Assistant bot toolset - smart home event monitoring and control",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-email": {
        "description": "Email bot toolset - interact with Hermes via email (IMAP/SMTP)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-mattermost": {
        "description": "Mattermost bot toolset - self-hosted team messaging (full access)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-matrix": {
        "description": "Matrix bot toolset - decentralized encrypted messaging (full access)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-dingtalk": {
        "description": "DingTalk bot toolset - enterprise messaging platform (full access)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-feishu": {
        "description": "Feishu/Lark bot toolset - enterprise messaging via Feishu/Lark (full access)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-weixin": {
        "description": "Weixin bot toolset - personal WeChat messaging via iLink (full access)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-qqbot": {
        "description": "QQBot toolset - QQ messaging via Official Bot API v2 (full access)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-wecom": {
        "description": "WeCom bot toolset - enterprise WeChat messaging (full access)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-wecom-callback": {
        "description": "WeCom callback toolset - enterprise self-built app messaging (full access)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-sms": {
        "description": "SMS bot toolset - interact with Hermes via SMS (Twilio)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-webhook": {
        "description": "Webhook toolset - receive and process external webhook events",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-gateway": {
        "description": "Gateway toolset - union of all messaging platform tools",
        "tools": [],
        "includes": ["hermes-telegram", "hermes-discord", "hermes-whatsapp", "hermes-slack", "hermes-signal", "hermes-bluebubbles", "hermes-homeassistant", "hermes-email", "hermes-sms", "hermes-mattermost", "hermes-matrix", "hermes-dingtalk", "hermes-feishu", "hermes-wecom", "hermes-wecom-callback", "hermes-weixin", "hermes-qqbot", "hermes-webhook"]
    }
}



def get_toolset(name: str) -> Optional[Dict[str, Any]]:
    """
    Get a toolset definition by name.
    
    Args:
        name (str): Name of the toolset
        
    Returns:
        Dict: Toolset definition with description, tools, and includes
        None: If toolset not found
    """
    toolset = TOOLSETS.get(name)
    if toolset:
        return toolset

    try:
        from tools.registry import registry
    except Exception:
        return None

    registry_toolset = name
    description = f"Plugin toolset: {name}"
    alias_target = registry.get_toolset_alias_target(name)

    if name not in _get_plugin_toolset_names():
        registry_toolset = alias_target
        if not registry_toolset:
            return None
        description = f"MCP server '{name}' tools"
    else:
        reverse_aliases = {
            canonical: alias
            for alias, canonical in _get_registry_toolset_aliases().items()
            if alias not in TOOLSETS
        }
        alias = reverse_aliases.get(name)
        if alias:
            description = f"MCP server '{alias}' tools"

    return {
        "description": description,
        "tools": registry.get_tool_names_for_toolset(registry_toolset),
        "includes": [],
    }


def resolve_toolset(name: str, visited: Set[str] = None) -> List[str]:
    """
    Recursively resolve a toolset to get all tool names.
    
    This function handles toolset composition by recursively resolving
    included toolsets and combining all tools.
    
    Args:
        name (str): Name of the toolset to resolve
        visited (Set[str]): Set of already visited toolsets (for cycle detection)
        
    Returns:
        List[str]: List of all tool names in the toolset
    """
    if visited is None:
        # Top-level entry (the recursive include-walk always passes a visited
        # set). This is the one funnel every caller that builds an agent's tool
        # list goes through — model_tools.get_tool_definitions(), the web
        # server, tools_config — so it is where the exposure audit can actually
        # reach the operator. Runs once per process; see the audit block below.
        _audit_tool_exposure_once()
        visited = set()

    # Special aliases that represent all tools across every toolset
    # This ensures future toolsets are automatically included without changes.
    if name in {"all", "*"}:
        all_tools: Set[str] = set()
        for toolset_name in get_toolset_names():
            # Use a fresh visited set per branch to avoid cross-branch contamination
            resolved = resolve_toolset(toolset_name, visited.copy())
            all_tools.update(resolved)
        return sorted(all_tools)

    # Check for cycles / already-resolved (diamond deps).
    # Silently return [] — either this is a diamond (not a bug, tools already
    # collected via another path) or a genuine cycle (safe to skip).
    if name in visited:
        return []

    visited.add(name)

    # Get toolset definition
    toolset = get_toolset(name)
    if not toolset:
        return []

    # Collect direct tools
    tools = set(toolset.get("tools", []))

    # Recursively resolve included toolsets, sharing the visited set across
    # sibling includes so diamond dependencies are only resolved once and
    # cycle warnings don't fire multiple times for the same cycle.
    for included_name in toolset.get("includes", []):
        included_tools = resolve_toolset(included_name, visited)
        tools.update(included_tools)
    
    return sorted(tools)


def resolve_multiple_toolsets(toolset_names: List[str]) -> List[str]:
    """
    Resolve multiple toolsets and combine their tools.
    
    Args:
        toolset_names (List[str]): List of toolset names to resolve
        
    Returns:
        List[str]: Combined list of all tool names (deduplicated)
    """
    all_tools = set()
    
    for name in toolset_names:
        tools = resolve_toolset(name)
        all_tools.update(tools)
    
    return sorted(all_tools)


# ==========================================================================
# Tool-exposure audit
#
# Registering a tool is NOT the same as exposing it. registry.register(
# toolset="memory") only records membership; what an agent actually receives
# is resolve_toolset("memory") — exactly the names in
# TOOLSETS["memory"]["tools"]. So a tool that registers into an EXISTING
# declared toolset without being named there is invisible to every agent, and
# nothing warned at import, at registration, or at call time.
#
# 2026-08-16: v4.6.38 shipped "agents can finally read gbrain". Asked directly
# which tools they had, Ptah and Neith both answered "ask_agent, fleet_send,
# session_search" — gbrain_search/gbrain_read had registered under "memory"
# while that toolset listed only ["memory"]. 854 compiled pages, unreadable,
# for a full release. ask_agent survived the same day only by accident: its
# toolset "fleet" is not declared in TOOLSETS, so get_toolset() falls through
# to the registry and returns whatever registered under it. Two tools added
# the same day, one reachable and one not, for a reason neither file mentions.
#
# Deliberately NOT consulted here:
#   * check_fn — a tool whose backend is down on this machine is still a
#     registered tool, and its exposure is a static config question. Running
#     check_fn would also shell out (gbrain's runs psql) and would hide the
#     bug on exactly the machines least able to notice it.
#   * undeclared toolsets — those are reachable via the registry fallback
#     (the ask_agent case) and flagging them would train people to ignore this.
# ==========================================================================

_EXPOSURE_AUDIT_LOCK = threading.RLock()
_exposure_audit_done = False


def find_unexposed_tools(
    tool_to_toolset: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Return registered tools that their own toolset does not hand to agents.

    A finding is a dict::

        {"tool": str, "toolset": str,
         "exposed_by": [other declared toolsets that DO list it],
         "severity": "invisible" | "misfiled",
         "remedy": str}

    ``severity="invisible"`` means no declared toolset lists the tool at all,
    so no agent on any surface can see or call it — the gbrain failure.
    ``"misfiled"`` means some other toolset does list it, so it is reachable,
    just not through the toolset it claims to belong to.

    Args:
        tool_to_toolset: ``{tool_name: toolset}``. Defaults to the live
            registry. Tests inject a map to reproduce a specific incident.

    Returns:
        Findings sorted by tool name; empty on a host with no tool registry
        (a plain hermes-agent checkout must never raise out of this).
    """
    if tool_to_toolset is None:
        try:
            from tools.registry import registry
            tool_to_toolset = registry.get_tool_to_toolset_map()
        except Exception:
            return []

    if not tool_to_toolset:
        return []

    # Resolve every DECLARED toolset once. resolve_toolset() is the authority
    # on what an agent receives — recomputing membership from ["tools"] here
    # would miss composition via "includes" and could disagree with the code
    # that actually builds the tool list, which is the whole failure mode.
    #
    # The explicit empty `visited` marks these as non-top-level calls, so
    # asking the question never fires the once-per-process report below. An
    # audit that can trigger itself is a recursion waiting to happen, and
    # callers (tests, a doctor command) must be able to check without
    # latching or emitting anything.
    resolved: Dict[str, Set[str]] = {
        ts_name: set(resolve_toolset(ts_name, set())) for ts_name in list(TOOLSETS)
    }

    findings: List[Dict[str, Any]] = []
    for tool_name, ts_name in sorted(tool_to_toolset.items()):
        if ts_name not in TOOLSETS:
            continue  # registry fallback covers it (the ask_agent case)
        if tool_name in resolved.get(ts_name, ()):
            continue
        exposed_by = sorted(
            other for other, tools in resolved.items() if tool_name in tools
        )
        findings.append({
            "tool": tool_name,
            "toolset": ts_name,
            "exposed_by": exposed_by,
            "severity": "misfiled" if exposed_by else "invisible",
            "remedy": (
                f'add "{tool_name}" to TOOLSETS["{ts_name}"]["tools"] '
                f"in toolsets.py"
            ),
        })
    return findings


def report_unexposed_tools(
    tool_to_toolset: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Log every unexposed tool loudly and return the findings.

    Separate from find_unexposed_tools() so tests can assert on the findings
    without parsing logs, and on the logs without duplicating the rule.
    """
    findings = find_unexposed_tools(tool_to_toolset)
    if not findings:
        return findings

    logger.error(
        "TOOL-EXPOSURE AUDIT: %d registered tool(s) are not handed to agents "
        "by the toolset they registered into. This is the gbrain class "
        "(v4.6.38 shipped gbrain_search/gbrain_read into toolset 'memory' "
        "while 'memory' listed only ['memory']; agents reported having no "
        "gbrain tools at all, 2026-08-16).",
        len(findings),
    )
    for f in findings:
        if f["severity"] == "invisible":
            logger.error(
                "TOOL INVISIBLE TO EVERY AGENT: '%s' registered into toolset "
                "'%s', but no toolset lists it — resolve_toolset() never "
                "returns it, so no agent on any surface can see or call it. "
                "Remedy: %s",
                f["tool"], f["toolset"], f["remedy"],
            )
        else:
            logger.warning(
                "TOOL MISFILED: '%s' registered into toolset '%s', which does "
                "not list it; agents reach it only via %s. Remedy: %s",
                f["tool"], f["toolset"], ", ".join(f["exposed_by"]), f["remedy"],
            )
    return findings


def _audit_tool_exposure_once() -> None:
    """Run the exposure audit the first time tools are resolved in a process.

    Import time is too early — toolsets.py is imported before the tool modules
    have registered anything — so the audit hangs off the first top-level
    resolve_toolset(), which by then is downstream of
    discover_builtin_tools() (model_tools.py imports us, then discovers).
    """
    global _exposure_audit_done
    if _exposure_audit_done:
        return
    with _EXPOSURE_AUDIT_LOCK:
        if _exposure_audit_done:
            return
        try:
            from tools.registry import registry
            registered = registry.get_tool_to_toolset_map()
        except Exception:
            return  # no registry on this host — try again later, never raise
        if not registered:
            # Nothing has registered yet. Do NOT latch, or an early resolve
            # would disable the audit for the life of the process.
            return
        # Latch BEFORE auditing so a failing audit degrades to one bad run
        # instead of re-running on every resolve for the life of the process.
        _exposure_audit_done = True
        try:
            report_unexposed_tools(registered)
        except Exception:
            logger.debug("Tool-exposure audit failed", exc_info=True)


def _get_plugin_toolset_names() -> Set[str]:
    """Return toolset names registered by plugins (from the tool registry).

    These are toolsets that exist in the registry but not in the static
    ``TOOLSETS`` dict — i.e. they were added by plugins at load time.
    """
    try:
        from tools.registry import registry
        return {
            toolset_name
            for toolset_name in registry.get_registered_toolset_names()
            if toolset_name not in TOOLSETS
        }
    except Exception:
        return set()


def _get_registry_toolset_aliases() -> Dict[str, str]:
    """Return explicit toolset aliases registered in the live registry."""
    try:
        from tools.registry import registry
        return registry.get_registered_toolset_aliases()
    except Exception:
        return {}


def get_all_toolsets() -> Dict[str, Dict[str, Any]]:
    """
    Get all available toolsets with their definitions.

    Includes both statically-defined toolsets and plugin-registered ones.
    
    Returns:
        Dict: All toolset definitions
    """
    result = dict(TOOLSETS)
    aliases = _get_registry_toolset_aliases()
    for ts_name in _get_plugin_toolset_names():
        display_name = ts_name
        for alias, canonical in aliases.items():
            if canonical == ts_name and alias not in TOOLSETS:
                display_name = alias
                break
        if display_name in result:
            continue
        toolset = get_toolset(display_name)
        if toolset:
            result[display_name] = toolset
    return result


def get_toolset_names() -> List[str]:
    """
    Get names of all available toolsets (excluding aliases).

    Includes plugin-registered toolset names.
    
    Returns:
        List[str]: List of toolset names
    """
    names = set(TOOLSETS.keys())
    aliases = _get_registry_toolset_aliases()
    for ts_name in _get_plugin_toolset_names():
        for alias, canonical in aliases.items():
            if canonical == ts_name and alias not in TOOLSETS:
                names.add(alias)
                break
        else:
            names.add(ts_name)
    return sorted(names)




def validate_toolset(name: str) -> bool:
    """
    Check if a toolset name is valid.
    
    Args:
        name (str): Toolset name to validate
        
    Returns:
        bool: True if valid, False otherwise
    """
    # Accept special alias names for convenience
    if name in {"all", "*"}:
        return True
    if name in TOOLSETS:
        return True
    if name in _get_plugin_toolset_names():
        return True
    return name in _get_registry_toolset_aliases()


def create_custom_toolset(
    name: str,
    description: str,
    tools: List[str] = None,
    includes: List[str] = None
) -> None:
    """
    Create a custom toolset at runtime.
    
    Args:
        name (str): Name for the new toolset
        description (str): Description of the toolset
        tools (List[str]): Direct tools to include
        includes (List[str]): Other toolsets to include
    """
    TOOLSETS[name] = {
        "description": description,
        "tools": tools or [],
        "includes": includes or []
    }




def get_toolset_info(name: str) -> Dict[str, Any]:
    """
    Get detailed information about a toolset including resolved tools.
    
    Args:
        name (str): Toolset name
        
    Returns:
        Dict: Detailed toolset information
    """
    toolset = get_toolset(name)
    if not toolset:
        return None
    
    resolved_tools = resolve_toolset(name)
    
    return {
        "name": name,
        "description": toolset["description"],
        "direct_tools": toolset["tools"],
        "includes": toolset["includes"],
        "resolved_tools": resolved_tools,
        "tool_count": len(resolved_tools),
        "is_composite": bool(toolset["includes"])
    }




if __name__ == "__main__":
    print("Toolsets System Demo")
    print("=" * 60)
    
    print("\nAvailable Toolsets:")
    print("-" * 40)
    for name, toolset in get_all_toolsets().items():
        info = get_toolset_info(name)
        composite = "[composite]" if info["is_composite"] else "[leaf]"
        print(f"  {composite} {name:20} - {toolset['description']}")
        print(f"     Tools: {len(info['resolved_tools'])} total")
    
    print("\nToolset Resolution Examples:")
    print("-" * 40)
    for name in ["web", "terminal", "safe", "debugging"]:
        tools = resolve_toolset(name)
        print(f"\n  {name}:")
        print(f"    Resolved to {len(tools)} tools: {', '.join(sorted(tools))}")
    
    print("\nMultiple Toolset Resolution:")
    print("-" * 40)
    combined = resolve_multiple_toolsets(["web", "vision", "terminal"])
    print("  Combining ['web', 'vision', 'terminal']:")
    print(f"    Result: {', '.join(sorted(combined))}")
    
    print("\nCustom Toolset Creation:")
    print("-" * 40)
    create_custom_toolset(
        name="my_custom",
        description="My custom toolset for specific tasks",
        tools=["web_search"],
        includes=["terminal", "vision"]
    )
    custom_info = get_toolset_info("my_custom")
    print("  Created 'my_custom' toolset:")
    print(f"    Description: {custom_info['description']}")
    print(f"    Resolved tools: {', '.join(custom_info['resolved_tools'])}")
