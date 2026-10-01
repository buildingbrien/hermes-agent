"""Lucaryin fold 0033 (F37): long sessions never compacted.

Review of 2026-09-20..10-01: ``runtime_errors.log`` logged "Session DB compression
split failed — new session will NOT be indexed: UNIQUE constraint failed:
messages.session_id, messages.tool_call_id" once on Sep 26 and 10 times between
22:45Z Sep 28 and 00:57Z Sep 29, every 2-4 minutes, on the founder's phone thread
(mobile_3b0042fa_1790456858, 343 rows) and on two desktop threads
(20260924_233813_1e26ea, 20260726_115112_9d4181: 1,668 rows).

Cause: the pre-rebase v4.6.87 runtime (commit 07cb2c5, schema v7) created
``idx_messages_tool_result_unique`` = UNIQUE(session_id, tool_call_id) WHERE
tool_call_id IS NOT NULL, with no ``active`` filter. The rebased runtime compacts
in place: it soft-archives the active rows and re-inserts the summary plus the
verbatim carried tail under the SAME session id, so every tool row in the tail
collides with its archived original and the whole transaction rolls back. The
rebased schema never creates that index, and nothing dropped it.

These tests build that v7-era state.db shape and replay the compaction.
"""

import sqlite3

import pytest

from hermes_state import SessionDB

STALE_INDEX = "idx_messages_tool_result_unique"
# Exactly what the v7 migration ran (07cb2c5, hermes_state.py).
STALE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {STALE_INDEX} "
    "ON messages(session_id, tool_call_id) WHERE tool_call_id IS NOT NULL"
)
SID = "mobile_3b0042fa_1790456858"


def _index_present(path) -> bool:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (STALE_INDEX,)
        ).fetchone() is not None
    finally:
        conn.close()


def _add_stale_index(path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(STALE_INDEX_SQL)
        conn.commit()
    finally:
        conn.close()


def _tool_turn(n: int) -> list:
    """One research step the way DeepSeek returns it: an assistant tool call and its result."""
    call_id = f"call_00_{n:04d}EBLsearch"
    return [
        {"role": "user", "content": f"read article {n}"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "web_search", "arguments": f'{{"query": "ebl {n}"}}'},
        }]},
        {"role": "tool", "tool_call_id": call_id, "tool_name": "web_search",
         "content": f"result {n}"},
        {"role": "assistant", "content": f"summary of article {n}"},
    ]


def _seed(db: SessionDB) -> list:
    db.create_session(SID, source="mobile")
    transcript = [m for n in range(6) for m in _tool_turn(n)]
    db.append_messages_batch(SID, transcript)
    return transcript


def _compacted(transcript: list, tail: int) -> list:
    summary = {"role": "user", "content": "[CONTEXT COMPACTION] earlier research summarized"}
    return [summary] + [dict(m) for m in transcript[-tail:]]


class TestStaleIndexIsDropped:
    def test_reopening_a_v7_era_db_drops_the_index(self, tmp_path):
        path = tmp_path / "state.db"
        SessionDB(path).close()
        _add_stale_index(path)
        assert _index_present(path)

        SessionDB(path).close()

        assert not _index_present(path)

    def test_a_fresh_db_never_has_it(self, tmp_path):
        path = tmp_path / "state.db"
        SessionDB(path).close()
        assert not _index_present(path)

    def test_reopening_twice_is_a_no_op(self, tmp_path):
        path = tmp_path / "state.db"
        SessionDB(path).close()
        _add_stale_index(path)
        SessionDB(path).close()
        SessionDB(path).close()
        assert not _index_present(path)


class TestInPlaceCompactionReplay:
    """The Sep 28 failure, end to end against the real SessionDB."""

    def test_the_stale_index_is_what_made_the_split_fail(self, tmp_path):
        # Documents the failure mode: with the old index present (index created
        # AFTER open, so the drop on open has not run yet), the carried tail collides.
        path = tmp_path / "state.db"
        db = SessionDB(path)
        transcript = _seed(db)
        db._conn.execute(STALE_INDEX_SQL)
        db._conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="tool_call_id"):
            db.archive_and_compact(SID, _compacted(transcript, tail=8), tail_count=8)

        # Atomic: nothing was archived, every original row is still active.
        assert len(db.get_messages(SID)) == len(transcript)
        db.close()

    def test_after_reopen_the_same_compaction_commits(self, tmp_path):
        path = tmp_path / "state.db"
        db = SessionDB(path)
        transcript = _seed(db)
        db.close()
        _add_stale_index(path)

        db = SessionDB(path)           # the fold drops the index here
        compacted = _compacted(transcript, tail=8)
        active = db.archive_and_compact(SID, compacted, tail_count=8)

        assert active == len(compacted)
        live = db.get_messages(SID)
        assert len(live) == len(compacted)
        # The carried tail keeps its tool_call_ids (the tool result still pairs
        # with its call), and the archived originals are still on disk.
        carried_ids = [m.get("tool_call_id") for m in live if m.get("tool_call_id")]
        assert carried_ids == ["call_00_0004EBLsearch", "call_00_0005EBLsearch"]
        everything = db.get_messages(SID, include_inactive=True)
        assert len(everything) == len(transcript) + len(compacted)
        db.close()

    def test_repeated_compactions_keep_working(self, tmp_path):
        # The live session failed on every turn; after the fix each later
        # compaction (which re-carries the same tool rows again) must commit too.
        path = tmp_path / "state.db"
        db = SessionDB(path)
        transcript = _seed(db)
        db.close()
        _add_stale_index(path)
        db = SessionDB(path)
        for _ in range(3):
            db.archive_and_compact(SID, _compacted(transcript, tail=8), tail_count=8)
        assert len(db.get_messages(SID)) == 9
        db.close()


class TestSplitFailureBackoff:
    def test_a_constraint_failure_backs_off_for_the_sitting(self):
        from agent.conversation_compression import (
            _SPLIT_FAILURE_COOLDOWN_SECONDS, _SPLIT_INTEGRITY_FAILURE_COOLDOWN_SECONDS,
            _split_failure_cooldown_seconds,
        )
        err = sqlite3.IntegrityError(
            "UNIQUE constraint failed: messages.session_id, messages.tool_call_id")
        assert _split_failure_cooldown_seconds(err) == _SPLIT_INTEGRITY_FAILURE_COOLDOWN_SECONDS
        assert _SPLIT_INTEGRITY_FAILURE_COOLDOWN_SECONDS >= 30 * 60
        # A wrapped error keeps the long rung too (the handler sees whatever propagated).
        assert _split_failure_cooldown_seconds(
            RuntimeError("UNIQUE constraint failed: x")) == _SPLIT_INTEGRITY_FAILURE_COOLDOWN_SECONDS

    def test_a_transient_failure_keeps_the_short_rung(self):
        from agent.conversation_compression import (
            _SPLIT_FAILURE_COOLDOWN_SECONDS, _split_failure_cooldown_seconds,
        )
        assert _split_failure_cooldown_seconds(RuntimeError("archive boom")) == _SPLIT_FAILURE_COOLDOWN_SECONDS
        assert _split_failure_cooldown_seconds(
            sqlite3.OperationalError("database is locked")) == _SPLIT_FAILURE_COOLDOWN_SECONDS

    def test_the_split_handler_arms_the_long_cooldown(self, tmp_path, monkeypatch):
        # Through the real in-place split handler (same shape as upstream's
        # test_failed_split_arms_failure_cooldown, with the Sep 28 error).
        import os
        from unittest.mock import MagicMock, patch

        from agent.conversation_compression import _SPLIT_INTEGRITY_FAILURE_COOLDOWN_SECONDS

        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(SID, source="mobile")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key", base_url="https://api.deepseek.com/v1",
                model="deepseek-v4-pro", quiet_mode=True, session_db=db, session_id=SID,
                skip_context_files=True, skip_memory=True,
            )
        compressor = MagicMock()
        compressor.compress.return_value = [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail"},
        ]
        compressor.compression_count = 1
        compressor.last_prompt_tokens = 0
        compressor.last_completion_tokens = 0
        compressor._last_summary_error = None
        compressor._last_compress_aborted = False
        compressor._last_aux_model_failure_model = None
        compressor._last_aux_model_failure_error = None
        agent.context_compressor = compressor
        agent._compression_feasibility_checked = True
        agent.compression_in_place = True
        db.archive_and_compact = MagicMock(side_effect=sqlite3.IntegrityError(
            "UNIQUE constraint failed: messages.session_id, messages.tool_call_id"))

        agent._compress_context([{"role": "user", "content": f"m{i}"} for i in range(20)],
                                "sys", approx_tokens=120_000, force=True)

        calls = compressor._record_compression_failure_cooldown.call_args_list
        assert len(calls) == 1
        seconds, error = calls[0].args
        assert seconds == _SPLIT_INTEGRITY_FAILURE_COOLDOWN_SECONDS
        assert "session_split_failed" in str(error)
        db.close()
