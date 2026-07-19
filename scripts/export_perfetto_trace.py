#!/usr/bin/env python3
"""Export Hermes session history as a Perfetto-loadable trace.

Reads one or more SessionDB sqlite files (``state.db``) and emits Chrome
Trace Event Format JSON, which https://ui.perfetto.dev opens natively.
Zero dependencies — stdlib only. The DB is opened read-only.

Mapping:
    process (pid)  = agent (one per state.db: main home + each profile)
    thread  (tid)  = session
    slice          = message (duration = gap since previous message, i.e.
                     generation time for assistant slices, execution time
                     for tool slices)
    counter        = cumulative tokens per agent over time
    category       = "memory" for memory-related ops, "tool" for other
                     tool activity, "llm" for assistant text, "user" for
                     user messages — so memory traffic is filterable.

The point (per the memory-layer audit): make visible whether memory
reads/writes actually fire, where tokens go, and which sessions bloat.

Usage:
    python scripts/export_perfetto_trace.py                  # auto-discover
    python scripts/export_perfetto_trace.py --days 7 --out trace.json
    python scripts/export_perfetto_trace.py --db thoth=~/.hermes/state.db
    python scripts/export_perfetto_trace.py --session 20260717_103255_967dee

Message content is EXCLUDED by default (it can contain secrets); pass
``--include-content`` to embed a short preview in slice args.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Tool names counted as memory operations. Exact names first, then a
# substring net for provider-prefixed tools (e.g. hindsight_retain).
MEMORY_TOOL_EXACT = {"memory", "session_search"}
MEMORY_TOOL_SUBSTRINGS = (
    "memory", "recall", "remember", "gbrain", "hindsight", "mem0",
    "supermemory", "byterover", "honcho", "retaindb", "openviking",
    "holographic",
)

CONTENT_PREVIEW_CHARS = 120


def is_memory_op(tool_name: Optional[str]) -> bool:
    if not tool_name:
        return False
    low = tool_name.lower()
    if low in MEMORY_TOOL_EXACT:
        return True
    return any(s in low for s in MEMORY_TOOL_SUBSTRINGS)


def discover_dbs(hermes_home: Path) -> List[Tuple[str, Path]]:
    """Default DB set: main home plus every profile that has a state.db."""
    found: List[Tuple[str, Path]] = []
    main = hermes_home / "state.db"
    if main.exists():
        found.append(("main", main))
    profiles = hermes_home / "profiles"
    if profiles.is_dir():
        for p in sorted(profiles.iterdir()):
            db = p / "state.db"
            if db.exists():
                found.append((p.name, db))
    return found


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _iter_sessions(
    conn: sqlite3.Connection, since_epoch: float, session_filter: Optional[str]
) -> Iterator[sqlite3.Row]:
    q = """SELECT * FROM sessions WHERE started_at >= ?"""
    params: List[Any] = [since_epoch]
    if session_filter:
        q += " AND id = ?"
        params.append(session_filter)
    q += " ORDER BY started_at"
    yield from conn.execute(q, params)


def _tool_call_names(tool_calls_json: Optional[str]) -> List[str]:
    """Extract function names from a serialized tool_calls list."""
    if not tool_calls_json:
        return []
    try:
        calls = json.loads(tool_calls_json)
    except (json.JSONDecodeError, TypeError):
        return []
    names: List[str] = []
    if isinstance(calls, list):
        for c in calls:
            if isinstance(c, dict):
                fn = c.get("function") or {}
                name = fn.get("name") if isinstance(fn, dict) else None
                names.append(name or c.get("name") or "?")
    return names


def export_db(
    agent: str,
    db_path: Path,
    pid: int,
    *,
    since_epoch: float,
    session_filter: Optional[str],
    include_content: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Export one state.db → (trace events, summary stats)."""
    events: List[Dict[str, Any]] = [
        {"ph": "M", "name": "process_name", "pid": pid, "tid": 0,
         "args": {"name": f"agent:{agent}"}},
    ]
    stats = {
        "agent": agent, "sessions": 0, "messages": 0, "tool_results": 0,
        "tool_calls_issued": 0, "memory_ops": 0, "tokens": 0,
        "top_sessions": [],  # (total_input_tokens, session_id, source)
    }

    conn = _open_ro(db_path)
    try:
        tid = 0
        cumulative_tokens = 0
        for sess in _iter_sessions(conn, since_epoch, session_filter):
            tid += 1
            stats["sessions"] += 1
            sid = sess["id"]
            started_us = int(float(sess["started_at"]) * 1_000_000)

            events.append(
                {"ph": "M", "name": "thread_name", "pid": pid, "tid": tid,
                 "args": {"name": f"{sess['source']}:{sid}"}})

            msgs = conn.execute(
                "SELECT role, tool_name, tool_calls, timestamp, token_count,"
                "       finish_reason, LENGTH(COALESCE(content,'')) AS clen,"
                "       SUBSTR(COALESCE(content,''), 1, ?) AS preview "
                "FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (CONTENT_PREVIEW_CHARS, sid),
            ).fetchall()

            # Enclosing session span.
            last_ts = float(msgs[-1]["timestamp"]) if msgs else float(sess["started_at"])
            ended = float(sess["ended_at"]) if sess["ended_at"] else last_ts
            total_in = (sess["input_tokens"] or 0) + (sess["cache_read_tokens"] or 0)
            events.append({
                "ph": "X", "cat": "session",
                "name": f"session:{sess['source']}",
                "pid": pid, "tid": tid, "ts": started_us,
                "dur": max(int((ended - float(sess["started_at"])) * 1_000_000), 1),
                "args": {
                    "session_id": sid,
                    "model": sess["model"],
                    "end_reason": sess["end_reason"],
                    "messages": sess["message_count"],
                    "tool_calls": sess["tool_call_count"],
                    "input_tokens": sess["input_tokens"],
                    "cache_read_tokens": sess["cache_read_tokens"],
                    "output_tokens": sess["output_tokens"],
                },
            })
            stats["top_sessions"].append((total_in, sid, sess["source"]))

            prev_us = started_us
            for m in msgs:
                ts_us = int(float(m["timestamp"]) * 1_000_000)
                dur = max(ts_us - prev_us, 1)
                role = m["role"]
                tool_name = m["tool_name"]
                issued = _tool_call_names(m["tool_calls"])

                if role == "tool":
                    name = f"tool:{tool_name or '?'}"
                    cat = "memory" if is_memory_op(tool_name) else "tool"
                    stats["tool_results"] += 1
                    if cat == "memory":
                        stats["memory_ops"] += 1
                elif role == "assistant" and issued:
                    name = f"assistant→{','.join(issued[:3])}" + (
                        f"(+{len(issued) - 3})" if len(issued) > 3 else "")
                    cat = "memory" if any(is_memory_op(n) for n in issued) else "llm"
                    stats["tool_calls_issued"] += len(issued)
                    if cat == "memory":
                        stats["memory_ops"] += 1
                else:
                    name = role
                    cat = "llm" if role == "assistant" else "user"

                args: Dict[str, Any] = {"content_len": m["clen"]}
                if m["token_count"]:
                    args["tokens"] = m["token_count"]
                if m["finish_reason"]:
                    args["finish_reason"] = m["finish_reason"]
                if include_content and m["preview"]:
                    args["preview"] = m["preview"]

                events.append({"ph": "X", "cat": cat, "name": name,
                               "pid": pid, "tid": tid, "ts": prev_us,
                               "dur": dur, "args": args})

                if m["token_count"]:
                    cumulative_tokens += m["token_count"]
                    stats["tokens"] += m["token_count"]
                    events.append({"ph": "C", "name": "tokens",
                                   "pid": pid, "tid": 0, "ts": ts_us,
                                   "args": {"cumulative": cumulative_tokens}})

                stats["messages"] += 1
                prev_us = ts_us
    finally:
        conn.close()

    stats["top_sessions"] = sorted(stats["top_sessions"], reverse=True)[:5]
    return events, stats


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", action="append", metavar="NAME=PATH",
                    help="agent=state.db pair (repeatable; default: auto-discover)")
    ap.add_argument("--out", default="hermes-trace.json")
    ap.add_argument("--days", type=float, default=14.0,
                    help="how far back to export (default 14)")
    ap.add_argument("--session", help="export only this session id")
    ap.add_argument("--include-content", action="store_true",
                    help="embed content previews (may expose secrets — off by default)")
    args = ap.parse_args(argv)

    import time
    since = time.time() - args.days * 86400

    if args.db:
        dbs: List[Tuple[str, Path]] = []
        for spec in args.db:
            name, _, p = spec.partition("=")
            if not p:
                ap.error(f"--db expects NAME=PATH, got {spec!r}")
            dbs.append((name, Path(os.path.expanduser(p))))
    else:
        from_env = os.environ.get("HERMES_HOME", "~/.hermes")
        dbs = discover_dbs(Path(os.path.expanduser(from_env)))

    if not dbs:
        print("No state.db found — nothing to export.", file=sys.stderr)
        return 1

    all_events: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    for pid, (agent, path) in enumerate(dbs, start=1):
        events, stats = export_db(
            agent, path, pid, since_epoch=since,
            session_filter=args.session, include_content=args.include_content)
        all_events.extend(events)
        summaries.append(stats)

    out = Path(args.out)
    out.write_text(json.dumps({"traceEvents": all_events,
                               "displayTimeUnit": "ms"}))

    total_msgs = sum(s["messages"] for s in summaries)
    total_tools = sum(s["tool_results"] for s in summaries)
    total_mem = sum(s["memory_ops"] for s in summaries)
    print(f"Wrote {out}  ({len(all_events)} events)")
    print(f"Open at https://ui.perfetto.dev  (drag the file in)\n")
    for s in summaries:
        print(f"  agent:{s['agent']:8} sessions={s['sessions']:<3} "
              f"messages={s['messages']:<5} tool_results={s['tool_results']:<4} "
              f"memory_ops={s['memory_ops']}")
    if total_tools or total_mem:
        pct = 100.0 * total_mem / max(total_tools + total_mem, 1)
        print(f"\n  memory ops: {total_mem} of {total_tools + total_mem} "
              f"tool events ({pct:.1f}%) — if this is ~0, memory is not firing.")
    if total_msgs == 0:
        print("  (no messages in window — widen --days?)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
