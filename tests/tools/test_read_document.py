"""Tests for read_document — the "cron can't read PDFs" fix.

Real PDFs are generated with fpdf2 (a provisioned dependency), so extraction
runs against genuine files rather than mocks.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

pytest.importorskip("pypdf")
fpdf = pytest.importorskip("fpdf")

from tools.read_document_tool import read_document_tool, _parse_pages


def make_pdf(path: Path, pages: list[str]) -> str:
    doc = fpdf.FPDF()
    doc.set_font("helvetica", size=12)
    for content in pages:
        doc.add_page()
        if content:
            doc.multi_cell(0, 8, content)
    doc.output(str(path))
    return str(path)


@pytest.fixture
def lease_pdf(tmp_path):
    return make_pdf(
        tmp_path / "lease.pdf",
        ["Lease agreement for unit 1I.", "Rent is due monthly.", "Signatures page."],
    )


def test_extracts_all_pages(lease_pdf):
    result = read_document_tool({"path": lease_pdf})
    assert result["total_pages"] == 3
    assert result["pages_read"] == [1, 2, 3]
    assert "unit 1I" in result["text"]
    assert "Signatures" in result["text"]


def test_page_selection(lease_pdf):
    result = read_document_tool({"path": lease_pdf, "pages": "2"})
    assert result["pages_read"] == [2]
    assert "Rent is due" in result["text"]
    assert "unit 1I" not in result["text"]


def test_page_range_and_list(lease_pdf):
    result = read_document_tool({"path": lease_pdf, "pages": "1,3"})
    assert result["pages_read"] == [1, 3]


def test_range_beyond_end_is_clamped(lease_pdf):
    result = read_document_tool({"path": lease_pdf, "pages": "2-99"})
    assert result["pages_read"] == [2, 3]


def test_bad_page_spec_errors(lease_pdf):
    result = read_document_tool({"path": lease_pdf, "pages": "5-2"})
    assert "error" in result


def test_non_pdf_points_at_read_file(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("plain text")
    result = read_document_tool({"path": str(txt)})
    assert "error" in result
    assert "read_file" in result["error"]


def test_missing_file_errors():
    result = read_document_tool({"path": "/nowhere/at/all.pdf"})
    assert "error" in result


def test_truncation_notes_page_count(tmp_path):
    long_page = "word " * 2000  # ~10k chars; fpdf auto-paginates as needed
    path = make_pdf(tmp_path / "long.pdf", [long_page, long_page])
    result = read_document_tool({"path": path, "max_chars": 1000})
    assert len(result["text"]) == 1000
    assert "Truncated" in result["note"]
    assert f"{result['total_pages']} pages" in result["note"]


def test_blank_pdf_reports_no_text_layer(tmp_path):
    path = make_pdf(tmp_path / "scan.pdf", ["", ""])
    result = read_document_tool({"path": path})
    assert result["text"] == ""
    assert "scanned" in result["note"].lower()
    assert "error" not in result


def test_parse_pages_none_means_all():
    assert _parse_pages(None, 5) is None
    assert _parse_pages("  ", 5) is None
