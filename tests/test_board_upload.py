"""POST /import/apple: an Apple Card file sent as the raw request body, the
way an iOS Shortcut's Get Contents of URL sends it, lands in data/apple/ the
same as `penny import apple`."""

import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from test_apple import HEADER, PAY
from test_board_hardening import _cfg, _req

from penny import apple, board, statements


@pytest.fixture
def srv(tmp_path):
    s = ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(_cfg(tmp_path)))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        yield tmp_path, s.server_address[1]
    finally:
        s.shutdown()
        s.server_close()


def test_a_wallet_csv_is_imported_once(srv):
    root, port = srv
    code, body = _req(port, "POST", "/import/apple", HEADER + PAY)
    assert code == 200 and b"imported (1 rows)" in body
    code, body = _req(port, "POST", "/import/apple", HEADER + PAY)
    assert code == 200 and b"already imported (1 rows)" in body
    (saved,) = (root / "data" / "apple").iterdir()
    assert saved.name.startswith("upload-") and saved.suffix == ".csv"
    assert [p.name for p in (root / "data").iterdir() if p.name.startswith("tmp")] == []


def test_a_file_that_is_not_a_wallet_export_is_refused(srv):
    root, port = srv
    code, body = _req(port, "POST", "/import/apple", "a,b\n1,2\n")
    assert code == 400 and b"skipped, not a Wallet export" in body
    assert not list((root / "data" / "apple").iterdir())


def test_an_empty_body_is_refused(srv):
    _, port = srv
    code, body = _req(port, "POST", "/import/apple", b"")
    assert code == 400 and b"request body" in body


def test_a_cross_site_page_cannot_upload(srv):
    root, port = srv
    code, _ = _req(port, "POST", "/import/apple", HEADER + PAY, {"Origin": "http://evil.example"})
    assert code == 403 and not (root / "data" / "apple").exists()


def test_a_wrong_host_cannot_upload(srv):
    _, port = srv
    code, _ = _req(port, "POST", "/import/apple", HEADER + PAY, {"Host": f"evil.example:{port}"}, skip_host=True)
    assert code == 403


def test_a_pdf_is_read_as_a_statement(tmp_path, monkeypatch):
    seen = []

    def parse(p):
        seen.append(p.suffix)
        return SimpleNamespace(issuer="apple", start="2026-08-01", end="2026-08-31")

    monkeypatch.setattr(statements, "parse", parse)
    (line,) = apple.import_upload(b"%PDF-1.7 fake", tmp_path / "apple")
    assert seen == [".pdf"] and line.endswith("imported (statement 2026-08-01 to 2026-08-31)")
