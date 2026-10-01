"""session_material_policy — the runtime floor under the R2-2-05 contract (HA3).

The agent may USE the user's signed-in browser sessions; it may never EXTRACT
their material (cookies, Web Storage, IndexedDB, Cache Storage, stored
credentials, a request's POST body) — lucaryin-ai docs/ui-access-scope-2026-08-14.md,
hard condition 1. Two layers enforce it and ship in one tag (rule E, B2 + HA3):

* the bridge gate (hermes-bridge/approval_gate.py) files a credential_read card
  for any browser_cdp / browser_console / browser_exec call that matches the
  shared definition — a card the user can refuse, visible, at every trust level;
* this module — used by the browser tools themselves (patch 0009) — REFUSES the
  same calls outright, with no approval path. A tapped card or a cron job
  granted credential_read at creation passes the gate; it never passes this.

The definition is hermes-bridge/ui_access_policy.py (``SESSION_MATERIAL_JS_RE``,
``COOKIE_EXTRACTION_CDP``, ``SESSION_MATERIAL_CDP_PREFIXES``) plus the gate's
``_CDP_SESSION_MATERIAL_TEXT_RE`` and its per-value params scan
(``_cdp_params_text``), copied here VERBATIM from lucaryin-ai 1ef2e65 —
``test_session_material_contract.py`` pins every copy byte for byte and, when a
lucaryin-ai checkout is next to this repo, against the live source. The first
cut of 0009 scanned only Runtime.evaluate / callFunctionOn and an exact-name
method list, so Page.addScriptToEvaluateOnNewDocument(source=document.cookie),
Runtime.compileScript, Debugger.evaluateOnCallFrame, Page.reload's
scriptToEvaluateOnLoad, a javascript: navigation, DOMStorage.setDOMStorageItem,
Storage.getSharedStorageEntries and Storage.clearDataForOrigin all ran (review,
2026-09-23). Now: every CDP method in a session-material domain is refused by
PREFIX, and every string in every method's params — keys and values, at any
depth, plus the raw value as sent — is scanned.

String values that are themselves JSON (a nested CDP message such as
Target.sendMessageToTarget's ``message``) are decoded and scanned as well, so a
``\\u`` escape inside the nested JSON does not hide the method it names.

What this does NOT chase (the gate's job, as a ui_action card): JS that builds
its own member names at run time (``window['local' + 'Storage']`` inside a JS
string, ``\\u`` escapes, eval/atob). Those are unprovable either way; refusing
them outright would also refuse ordinary page scripting.

Stdlib only; imported by tools/browser_cdp_tool.py, tools/browser_tool_eval_policy.py
and tools/browser_use_cli.py (patch 0009).
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, List, Optional, Tuple

# ── The shared contract (VERBATIM — keep in lock-step with lucaryin-ai) ───────

# hermes-bridge/ui_access_policy.py ``_SECRET_JS_RE`` (= SESSION_MATERIAL_JS_RE).
SESSION_MATERIAL_JS_RE = re.compile(
    r"document\s*\.\s*cookie|\[\s*['\"`]cookie['\"`]\s*\]|\bcookieStore\b|"
    r"localStorage|sessionStorage|indexedDB|\bcaches\s*[.\[]|"
    r"navigator\s*\.\s*credentials|"
    r"\.cookies\b|getAllCookies|__Secure-|__Host-", re.I)

# hermes-bridge/approval_gate.py ``_CDP_SESSION_MATERIAL_TEXT_RE``: the
# session-material CDP methods as they appear INSIDE a string — a script's
# cdp('Network.getAllCookies'), or a nested CDP message such as
# Target.sendMessageToTarget(message='{"method": "Storage.getCookies"}').
CDP_SESSION_MATERIAL_TEXT_RE = re.compile(
    r"\b(?:Network\.(?:get(?:All)?Cookies|setCookies?|clearBrowserCookies|getRequestPostData)"
    r"|Page\.(?:getCookies|deleteCookie))\b"
    r"|(?:cdp\s*\(\s*)?['\"](?:Storage|DOMStorage|IndexedDB|CacheStorage)\.\w+['\"]",
    re.IGNORECASE)

# hermes-bridge/ui_access_policy.py ``COOKIE_EXTRACTION_CDP``.
COOKIE_EXTRACTION_CDP = {
    "network.getcookies", "network.getallcookies", "page.getcookies",
    "storage.getcookies", "network.setcookie", "network.setcookies",
    "storage.getstorageitems", "domstorage.getdomstorageitems",
}

# hermes-bridge/ui_access_policy.py ``SESSION_MATERIAL_CDP_PREFIXES``.
SESSION_MATERIAL_CDP_PREFIXES = (
    "network.getcookies", "network.getallcookies", "network.setcookie",
    "network.clearbrowsercookies", "network.getrequestpostdata",
    "page.getcookies", "page.deletecookie",
    "storage.", "domstorage.", "indexeddb.", "cachestorage.",
)

# hermes-bridge/approval_gate.py ``_CDP_URL_METHODS``: reads that carry a URL. An
# http(s) ``url`` there is a navigation TARGET, not code, and is left out of the
# scan (a docs page about localStorage is not the session leaving the browser);
# a javascript:/data: URL executes, so it stays in.
CDP_URL_METHODS = ("page.navigate", "target.createtarget")

# ── Runtime-only additions (the floor may be stricter than the card) ─────────
# The Credential Management constructors and the cookie property descriptor
# read the same material without naming it the way SESSION_MATERIAL_JS_RE looks
# for. The gate does not card these as credential_read; the tools refuse them.
RUNTIME_EXTRA_JS_RE = re.compile(
    r"\b(?:Password|Federated|PublicKey)Credential\b"
    r"|getOwnPropertyDescriptor\s*\([^)]*['\"`]cookie['\"`]",
    re.IGNORECASE)

_SCAN_LIMIT = 50_000  # strings; a params object bigger than this is refused, not skimmed

_DOCTRINE = ("Browser session material (cookies, Web Storage, IndexedDB, the Cache API, "
             "stored credentials, request bodies) is structurally unreachable to agents: "
             "you can USE the signed-in session by driving pages, never read or export it.")


def is_session_material_cdp(cdp_method: Any) -> bool:
    """True when a CDP method name reads or writes session material outright
    (the bridge's ``is_session_material_cdp``, same inputs, same answer)."""
    m = (cdp_method or "").strip().lower() if isinstance(cdp_method, str) else ""
    if not m:
        return False
    return m in COOKIE_EXTRACTION_CDP or any(m.startswith(p) for p in SESSION_MATERIAL_CDP_PREFIXES)


def session_material_hit(text: str) -> Optional[str]:
    """The first session-material reference in ``text`` (any of the three
    regexes), or None."""
    if not text:
        return None
    for rx in (SESSION_MATERIAL_JS_RE, CDP_SESSION_MATERIAL_TEXT_RE, RUNTIME_EXTRA_JS_RE):
        m = rx.search(text)
        if m:
            return m.group(0)
    return None


_NOT_JSON = object()


def _nested_json(s: str) -> Any:
    """A string value that is itself JSON (starts with '{' or '['), decoded — or
    _NOT_JSON. A nested CDP message travels this way:
    Target.sendMessageToTarget(message='{"method": "Stor\\u0061ge.getCookies"}')
    names Storage.getCookies only once the string is decoded."""
    t = s.strip()
    if len(t) < 2 or t[0] not in "{[":
        return _NOT_JSON
    try:
        return json.loads(t)
    except (ValueError, TypeError, RecursionError):
        return _NOT_JSON


def _strings(value: Any) -> Tuple[List[str], bool]:
    """Every string in ``value`` — dict keys and values, list items, scalars via
    str() — at any depth: ``(strings, complete)``. A string value that is itself
    JSON is decoded and walked too, and its canonical re-serialisation is added
    (the in-string CDP regex looks for a QUOTED method name, which a \\u escape
    in the original hides). Iterative; a container seen twice (a cycle) is
    walked once; past ``_SCAN_LIMIT`` strings — decoded ones included — the
    walk stops and reports incomplete."""
    out: List[str] = []
    stack = [value]
    seen: set = set()
    while stack:
        if len(out) > _SCAN_LIMIT:
            return out, False
        v = stack.pop()
        if isinstance(v, str):
            out.append(v)
            nested = _nested_json(v)
            if nested is not _NOT_JSON:
                try:
                    out.append(json.dumps(nested, ensure_ascii=False))
                except (TypeError, ValueError, RecursionError):
                    pass
                stack.append(nested)
        elif isinstance(v, dict):
            if id(v) in seen:
                continue
            seen.add(id(v))
            for k, x in v.items():
                out.append(str(k))
                stack.append(x)
        elif isinstance(v, (list, tuple)):
            if id(v) in seen:
                continue
            seen.add(id(v))
            stack.extend(v)
        elif v is not None:
            out.append(str(v))
    return out, True


def cdp_params_corpus(method: str, params: Any) -> Tuple[str, bool]:
    """``(text, complete)``: every string the page will see for this call — the
    decoded params' strings (a JSON-string ``params`` is decoded the way the tool
    decodes it) AND the raw value as sent, the gate's two corpora. For the
    URL-carrying reads an http(s) ``url`` is dropped from both."""
    m = (method or "").strip().lower() if isinstance(method, str) else ""
    decoded = params
    if isinstance(params, str):
        try:
            decoded = json.loads(params)
        except (ValueError, TypeError):
            decoded = None  # not JSON: the raw string is the whole corpus
    raw_source = params
    if m in CDP_URL_METHODS and isinstance(decoded, dict):
        u = decoded.get("url")
        if isinstance(u, str) and u.strip().lower().startswith(("http://", "https://")):
            decoded = {k: v for k, v in decoded.items() if k != "url"}
            raw_source = decoded
    strings, complete = _strings(decoded) if decoded is not None else ([], True)
    try:
        raw = raw_source if isinstance(raw_source, str) else json.dumps(
            raw_source, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        raw = str(raw_source)
    strings.append(raw or "")
    return "\n".join(strings), complete


def cdp_refusal(method: Any, params: Any) -> Optional[str]:
    """Refusal message for a raw CDP call that reads or writes session material,
    or None. Judged on the method name (by prefix) and on every string in its
    params, before any connection is made."""
    if is_session_material_cdp(method):
        return (f"CDP method '{method}' is blocked: it reads or writes browser session "
                f"material. {_DOCTRINE}")
    corpus, complete = cdp_params_corpus(method if isinstance(method, str) else "", params)
    if not complete:
        return (f"CDP {method} is blocked: its params are too large to verify that they do "
                f"not reach browser session material. {_DOCTRINE}")
    hit = session_material_hit(corpus)
    if hit:
        return (f"CDP {method} is blocked: its params reference browser session material "
                f"({hit!r}). {_DOCTRINE}")
    return None


def js_refusal(expression: Any, *, tool: str = "browser_console") -> Optional[str]:
    """Refusal message for page JavaScript that references session material."""
    hit = session_material_hit(expression if isinstance(expression, str) else str(expression or ""))
    if hit:
        return (f"Blocked: {tool} JavaScript references browser session material "
                f"({hit!r}). {_DOCTRINE}")
    return None


class _FoldStringConcat(ast.NodeTransformer):
    """'a' + 'b' -> 'ab' (the parser already joins 'a' 'b'); the bridge gate's
    ``_BxFold``, so a Python-level split literal reads as the literal it builds."""

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if (isinstance(node.op, ast.Add)
                and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            return ast.copy_location(ast.Constant(node.left.value + node.right.value), node)
        return node


def _folded(code: str) -> str:
    try:
        return ast.unparse(_FoldStringConcat().visit(ast.parse(code)))
    except Exception:  # noqa: BLE001 - unparseable: the raw code is the whole corpus
        return ""


def script_refusal(code: Any, *, tool: str = "browser_exec") -> Optional[str]:
    """Refusal message for a browser-driving Python script whose js()/cdp()
    strings reach session material — judged on the raw code AND a copy with
    split string literals folded ('document.' + 'cookie'), as the gate judges it."""
    text = code if isinstance(code, str) else str(code or "")
    hit = session_material_hit(text) or session_material_hit(_folded(text))
    if hit:
        return (f"Blocked: this {tool} script reaches browser session material "
                f"({hit!r}). {_DOCTRINE}")
    return None
