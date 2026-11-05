"""Crashes and slow clients leave penny's files and the board usable."""

import json
import os
import socket
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from penny import apple, board, fsio, plaid, statements


def test_a_torn_last_line_costs_one_snapshot_not_every_read(tmp_path, capsys):
    s = plaid.Store(tmp_path, "sandbox")
    s.append_balances([{"a": 1}])
    with open(s.dir / "balances.jsonl", "a") as f:
        f.write('{"a": 2, "torn')  # a crash mid-append
    s.append_balances([{"a": 3}])
    assert s.balances() == [{"a": 1}, {"a": 3}]
    assert "1 torn line" in capsys.readouterr().err
    assert oct((s.dir / "balances.jsonl").stat().st_mode & 0o777) == "0o600"


def test_append_lines_makes_a_private_file(tmp_path):
    p = tmp_path / "d" / "log.jsonl"
    fsio.append_lines(p, ["{}"])
    fsio.append_lines(p, ["[]"])
    assert p.read_text() == "{}\n[]\n" and p.stat().st_mode & 0o777 == 0o600


def test_an_empty_client_user_id_is_replaced_not_kept(tmp_path):
    s = plaid.Store(tmp_path, "sandbox")
    path = s.dir.parent / plaid.USER_ID_FILE
    path.write_text("")  # a crash between create and write, before this fix
    uid = s.client_user_id()
    assert len(uid) == 32 and s.client_user_id() == uid and path.read_text().strip() == uid
    assert [p.name for p in s.dir.parent.iterdir() if p.name.startswith(".")] == []


def test_write_atomic_writes_through_a_symlink(tmp_path):
    real = tmp_path / "real.toml"
    real.write_text("a = 1\n")
    link = tmp_path / "rules.toml"
    link.symlink_to(real)
    fsio.write_atomic(link, "a = 2\n")
    assert link.is_symlink() and real.read_text() == "a = 2\n"


def test_an_apple_import_is_written_whole_and_private(tmp_path):
    src = tmp_path / "w.csv"
    src.write_text(",".join(apple.FIELDS) + "\n")
    out = apple.import_files([src], tmp_path / "apple")
    assert "imported" in out[0]
    dest = tmp_path / "apple" / "w.csv"
    assert dest.read_bytes() == src.read_bytes() and dest.stat().st_mode & 0o777 == 0o600


def test_pdftotext_is_given_an_absolute_path_and_a_timeout(monkeypatch, tmp_path):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, kw=kw)
        raise statements.subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(statements.subprocess, "run", run)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(statements.ParseError, match="took over"):
        statements.pdf_text("-x.pdf")
    assert seen["argv"][2] == str(tmp_path / "-x.pdf") and seen["kw"]["timeout"] == statements.PDFTOTEXT_TIMEOUT


def test_a_stalled_request_body_frees_its_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "REQUEST_TIMEOUT", 0.5)
    cfg = board.Config(tmp_path, tmp_path / "rules.toml", tmp_path / "assumptions.toml", None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        c = socket.create_connection(("127.0.0.1", port))
        c.sendall(f"POST /record HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                  f"Content-Length: 1000\r\n\r\n{{\"a\"".encode())
        c.settimeout(5)
        t = time.monotonic()
        chunks = []
        while chunk := c.recv(4096):
            chunks.append(chunk)
        reply = b"".join(chunks).decode()
        assert time.monotonic() - t < 4
        assert " 408 " in reply.splitlines()[0] and "stalled" in reply
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_audit_line_goes_through_fsio(tmp_path):
    cfg = board.Config(tmp_path, tmp_path / "rules.toml", tmp_path / "assumptions.toml", None)
    board._audit(cfg, [{"x": 1}])
    p = tmp_path / "data" / "board-decisions.jsonl"
    assert json.loads(p.read_text()) == {"x": 1} and os.stat(p).st_mode & 0o777 == 0o600


def test_an_override_save_waits_for_one_already_running(tmp_path):
    from penny import categorize as cat

    p = tmp_path / "overrides.json"
    done = []
    with fsio.locked(p):
        t = threading.Thread(target=lambda: done.append(cat.save_override(p, "b", family="x")))
        t.start()
        time.sleep(0.2)
        assert not done  # blocked on the lock, not reading a file about to change
        cat.fsio.write_atomic(p, json.dumps({"a": {"family": "y"}}))
    t.join(5)
    assert json.loads(p.read_text()) == {"a": {"family": "y"}, "b": {"family": "x"}}


def test_a_read_against_a_missing_instance_says_so_and_makes_nothing(tmp_path):
    from penny import __main__ as cli

    missing = tmp_path / "typo"
    with pytest.raises(SystemExit, match="no penny instance at"):
        cli.main(["--home", str(missing), "report"])
    with pytest.raises(FileNotFoundError):
        from penny import feed

        feed.load(missing)
    assert not missing.exists()
    plaid.Store(tmp_path / "empty", "production", create=False).items()
    assert not (tmp_path / "empty").exists()
