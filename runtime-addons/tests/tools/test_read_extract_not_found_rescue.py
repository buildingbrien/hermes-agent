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

    def test_denied_path_never_reaches_the_recovery(self, ws, monkeypatch):
        # Adversarial-review finding: on a MISS inside a read-denied directory
        # (HERMES_HOME credential stores, mcp-tokens/, browser-profile/) the
        # recovery would `ls` that directory and surface sibling names. The
        # deny-list check must run BEFORE read_file_bytes, as on the text path.
        (ws / "secret-token.pdf").write_bytes(b"%PDF-1.4\n")
        monkeypatch.setattr(
            "tools.file_tools.get_read_block_error",
            lambda p: "Reading credential stores is blocked" if "MacBook Pro" in p else None)
        result = json.loads(read_file_tool(str(ws / "missing.pdf")))
        assert result.get("error") == "Reading credential stores is blocked", result
        assert "similar_files" not in result
        assert "secret-token" not in json.dumps(result)
        # And an EXISTING document in a denied dir is blocked too, not extracted.
        result = json.loads(read_file_tool(str(ws / ON_DISK)))
        assert result.get("error") == "Reading credential stores is blocked", result


# ── R2-3-02: the rescue must never read a deny-listed store ───────────────────
#
# Bug hunt round 2 (2026-09-22): the deny list ran on the REQUESTED path only.
# ``~/.hermes/auth/google_oauth.json `` (one trailing space) passed it — basename
# "google_oauth.json " is not a credential name — then the not-found rescue
# canonicalised the spelling, matched the real file and read it. Every exact-file
# read deny was one space away from a no-op. Now (1) the read tools check the
# canonical spelling up front and (2) the rescue re-checks the variant it picks,
# on both the text and the document path, and did-you-mean never lists a
# denied sibling.

SECRET = "refresh_token=1//0gDEADBEEF-not-a-real-token"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A fake HERMES_HOME holding credential stores; the real deny list checks it."""
    import agent.file_safety as fs
    home = tmp_path / "hermes"
    (home / "auth").mkdir(parents=True)
    (home / "auth" / "google_oauth.json").write_text('{"' + SECRET + '"}')
    (home / ".env").write_text("OPENAI_API_KEY=" + SECRET)
    (home / "mcp-tokens").mkdir()
    (home / "mcp-tokens" / "grant.pdf").write_bytes(b"%PDF-1.4\n" + SECRET.encode())
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: home)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: home)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setattr("tools.read_extract.extract_document_bytes", lambda data, path: data.decode(errors="replace"))
    return home


def _assert_denied(result: dict):
    assert "Access denied" in (result.get("error") or ""), result
    blob = json.dumps(result)
    assert SECRET not in blob and "similar_files" not in result, result


class TestRescueNeverReadsDeniedStores:
    @pytest.mark.parametrize("spelling", [
        "auth/google_oauth.json ", " auth/google_oauth.json"[1:] + " ", "auth/ google_oauth.json",
        "auth/google_oauth.json ", ".env ", " .env",
    ])
    def test_whitespace_variant_of_a_credential_store_is_denied(self, hermes_home, spelling):
        _assert_denied(json.loads(read_file_tool(str(hermes_home / spelling))))

    def test_the_rescue_itself_rechecks_the_variant(self, hermes_home, monkeypatch):
        """Mutation target: even with the up-front (file_tools) check taken away, the
        rescue inside ShellFileOperations refuses the variant it resolved to."""
        monkeypatch.setattr("tools.file_tools.get_read_block_error", lambda p: None)
        _assert_denied(json.loads(read_file_tool(str(hermes_home / "auth" / "google_oauth.json "))))

    def test_document_path_variant_is_denied(self, hermes_home):
        _assert_denied(json.loads(read_file_tool(str(hermes_home / "mcp-tokens" / "grant.pdf "))))

    def test_document_rescue_itself_rechecks_the_variant(self, hermes_home, monkeypatch):
        monkeypatch.setattr("tools.file_tools.get_read_block_error", lambda p: None)
        _assert_denied(json.loads(read_file_tool(str(hermes_home / "mcp-tokens" / "grant.pdf "))))

    def test_document_bytes_rescue_rechecks_an_exact_file_deny(self, tmp_path, monkeypatch):
        """Mutation target for read_file_bytes' OWN re-check (review, 2026-09-23).
        The document cases above sit under a read-denied DIRECTORY (mcp-tokens/),
        whose prefix deny already covers the requested spelling, so removing the
        re-check in read_file_bytes left every test green — and a TRAILING space
        (" .pdf ") is not a document extension, so those spellings took the text
        path. A LEADING space keeps the .pdf routing: the requested spelling
        (" statement.pdf") is not the name the exact deny lists, so only the
        document-path rescue's own re-check stands between it and the file."""
        import os
        d = tmp_path / "exports"
        d.mkdir()
        target = d / "statement.pdf"
        target.write_bytes(b"%PDF-1.4\n" + SECRET.encode())
        real = os.path.realpath(str(target))
        monkeypatch.setattr("tools.file_tools.get_read_block_error", lambda p: None)
        monkeypatch.setattr(
            "tools.file_operations.get_read_block_error",
            lambda p: "Access denied: an exact credential file" if os.path.realpath(p) == real else None)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        monkeypatch.setattr("tools.read_extract.extract_document_bytes",
                            lambda data, path: data.decode(errors="replace"))
        seen = []
        real_extract = lambda data, path: seen.append(path) or data.decode(errors="replace")  # noqa: E731
        monkeypatch.setattr("tools.read_extract.extract_document_bytes", real_extract)
        _assert_denied(json.loads(read_file_tool(str(d / (" " + target.name)))))
        assert seen == [], "the credential's bytes reached the extractor"

    def test_did_you_mean_never_names_a_denied_sibling(self, hermes_home):
        result = json.loads(read_file_tool(str(hermes_home / "auth" / "google_oauth.jsn")))
        assert (result.get("error") or "").startswith("File not found:"), result
        assert "google_oauth" not in json.dumps(result.get("similar_files", [])), result

    def test_exact_denied_path_still_denied(self, hermes_home):
        _assert_denied(json.loads(read_file_tool(str(hermes_home / "auth" / "google_oauth.json"))))

    def test_ordinary_files_keep_the_rescue(self, hermes_home):
        (hermes_home / "notes.txt").write_text("plain notes\n")
        result = json.loads(read_file_tool(str(hermes_home / "notes.txt ")))
        assert "plain notes" in result.get("content", ""), result
        assert "unicode-equivalent" in (result.get("hint") or "")
