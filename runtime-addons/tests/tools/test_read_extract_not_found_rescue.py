"""Lucaryin fold (runtime-patches/0019): document-extraction reads (.pdf/.docx/...)
get the same not-found recovery as text reads.

Canary 2026-09-13: read_file on "Documents - <Name>’s MacBook Pro/Theatrical
Resume 2025.pdf" returned "document extraction failed — File not found" although
the file existed as " Theatrical Resume 2025.pdf" (LEADING space — invisible in
``ls -l`` output, so the model retyped it without). The text path repairs
unicode-equivalent spellings and lists similar files; the extraction path
(``read_file_bytes``) returned a bare not-found, and ``_suggest_similar_files``
stripped the leading space off the first listing entry.
"""

import json

import pytest

from tools.file_tools import read_file_tool

DIR = "Documents - Michael’s MacBook Pro"
ON_DISK = " Theatrical Resume 2025.pdf"  # leading space, as Finder/iCloud created it
RETYPED = "Theatrical Resume 2025.pdf"   # what the model typed after reading ``ls -l``


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    # Routing under test, not the extractor: any bytes "extract" to a fixed text.
    monkeypatch.setattr(
        "tools.read_extract.extract_document_bytes",
        lambda data, path: "MICHAEL BONINI\nHeight: 6’0”\n")
    d = tmp_path / DIR
    d.mkdir()
    (d / ON_DISK).write_bytes(b"%PDF-1.4\n%stub\n")
    return d


class TestExtractedDocumentNotFoundRescue:
    def test_leading_space_spelling_repairs(self, ws):
        result = json.loads(read_file_tool(str(ws / RETYPED)))
        assert "MICHAEL BONINI" in result.get("content", ""), result
        assert result.get("extracted_document") is True
        assert "unicode-equivalent" in (result.get("hint") or "")

    def test_exact_spelling_no_note(self, ws):
        result = json.loads(read_file_tool(str(ws / ON_DISK)))
        assert "MICHAEL BONINI" in result.get("content", "")
        assert "unicode-equivalent" not in (result.get("hint") or "")

    def test_visible_difference_suggests_true_on_disk_name(self, ws):
        result = json.loads(read_file_tool(str(ws / "Theatrical Resume 2023.pdf")))
        error = result.get("error") or ""
        assert error.startswith("File not found:"), result
        assert "document extraction failed" not in error
        # The suggestion must be the byte-exact on-disk spelling (leading space kept).
        assert str(ws / ON_DISK) in result.get("similar_files", []), result

    def test_ambiguous_twins_not_repaired(self, ws):
        (ws / "Resume.pdf ").write_bytes(b"%PDF-1.4\n")  # trailing space
        (ws / " Resume.pdf").write_bytes(b"%PDF-1.4\n")  # leading space
        result = json.loads(read_file_tool(str(ws / "Resume.pdf")))
        assert (result.get("error") or "").startswith("File not found:"), result
        assert "unicode-equivalent" not in (result.get("hint") or "")

    def test_plain_missing_document_unchanged(self, ws):
        result = json.loads(read_file_tool(str(ws / "nothing-like-it.docx")))
        assert (result.get("error") or "").startswith("File not found:"), result
        assert "document extraction failed" not in result["error"]
