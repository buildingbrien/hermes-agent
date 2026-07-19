"""Tests for scripts/export_perfetto_trace.py — session history → Perfetto trace."""

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_perfetto_trace.py"
_spec = importlib.util.spec_from_file_location("export_perfetto_trace", _SCRIPT)
xpt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xpt)


@pytest.fixture()
def session_db(tmp_path):
    """A real SessionDB with one session: user → assistant(tool calls) → tool results → assistant."""
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    sid = "20260718_test_abc123"
    db.create_session(sid, source="cli", model="deepseek-v4-pro")
    db.append_message(sid, "user", content="what did we decide about pricing?")
    db.append_message(
        sid, "assistant", content=None,
        tool_calls=[
            {"id": "t1", "function": {"name": "memory", "arguments": "{}"}},
            {"id": "t2", "function": {"name": "web_search", "arguments": "{}"}},
        ],
    )
    db.append_message(sid, "tool", content="{...}", tool_name="memory", tool_call_id="t1")
    db.append_message(sid, "tool", content="{...}", tool_name="web_search", tool_call_id="t2")
    db.append_message(sid, "assistant", content="We decided $500/mo.",
                   token_count=42, finish_reason="stop")
    db.end_session(sid, "test_complete")
    db.close()
    return db_path


class TestMemoryOpDetection:
    def test_exact_names(self):
        assert xpt.is_memory_op("memory") is True
        assert xpt.is_memory_op("session_search") is True

    def test_provider_prefixed(self):
        assert xpt.is_memory_op("hindsight_retain") is True
        assert xpt.is_memory_op("gbrain_query") is True

    def test_non_memory(self):
        assert xpt.is_memory_op("web_search") is False
        assert xpt.is_memory_op("terminal") is False
        assert xpt.is_memory_op(None) is False


class TestToolCallNames:
    def test_parses_function_names(self):
        raw = json.dumps([{"id": "1", "function": {"name": "memory", "arguments": "{}"}}])
        assert xpt._tool_call_names(raw) == ["memory"]

    def test_garbage_is_empty(self):
        assert xpt._tool_call_names("not json") == []
        assert xpt._tool_call_names(None) == []


class TestExportDb:
    def test_events_and_stats(self, session_db):
        events, stats = xpt.export_db(
            "thoth", session_db, pid=1,
            since_epoch=0, session_filter=None, include_content=False)

        assert stats["sessions"] == 1
        assert stats["messages"] == 5
        assert stats["tool_results"] == 2
        assert stats["tool_calls_issued"] == 2
        # memory ops: the assistant slice issuing `memory` + the memory tool result
        assert stats["memory_ops"] == 2
        assert stats["tokens"] == 42

        cats = {e.get("cat") for e in events if e["ph"] == "X"}
        assert {"session", "memory", "tool", "llm", "user"} <= cats

        # process metadata + a token counter event exist
        assert any(e["ph"] == "M" and e["name"] == "process_name" for e in events)
        assert any(e["ph"] == "C" and e["name"] == "tokens" for e in events)

        # content is excluded by default
        assert not any("preview" in e.get("args", {}) for e in events)

    def test_include_content(self, session_db):
        events, _ = xpt.export_db(
            "thoth", session_db, pid=1,
            since_epoch=0, session_filter=None, include_content=True)
        previews = [e["args"]["preview"] for e in events if "preview" in e.get("args", {})]
        assert any("pricing" in p for p in previews)

    def test_since_filter_excludes_all(self, session_db):
        events, stats = xpt.export_db(
            "thoth", session_db, pid=1,
            since_epoch=9e12, session_filter=None, include_content=False)
        assert stats["sessions"] == 0


class TestMainCli:
    def test_writes_loadable_trace(self, session_db, tmp_path, capsys):
        out = tmp_path / "trace.json"
        rc = xpt.main(["--db", f"thoth={session_db}", "--out", str(out), "--days", "365"])
        assert rc == 0
        doc = json.loads(out.read_text())
        assert "traceEvents" in doc and len(doc["traceEvents"]) > 5
        assert "memory ops" in capsys.readouterr().out
