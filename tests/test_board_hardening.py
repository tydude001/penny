"""The board's server edges and caches: Host / Origin / Content-Type checks,
error handling and logging, the all-or-nothing batch, atomic writes, and the
mtime-keyed caches that stand in for rereading everything on every GET."""

import copy
import json
import os
import re
import threading
from datetime import timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from feedfix import install
from test_board import ASSUMPTIONS, INSTANCE
from test_board_pages import CARD_WORTH, TODAY

from penny import board, check, home, tomledit
from penny import categorize as cat


@pytest.fixture
def root(tmp_path):
    (tmp_path / "rules.toml").write_text((INSTANCE / "rules.toml").read_text())
    (tmp_path / "assumptions.toml").write_text(ASSUMPTIONS + CARD_WORTH)
    install(tmp_path)
    return tmp_path


def _cfg(root):
    return board.Config(root, root / "rules.toml", root / "assumptions.toml", None, None)


@pytest.fixture
def srv(root):
    s = ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(_cfg(root), today=lambda: TODAY, names={"board.example", "board"}))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        yield root, s.server_address[1]
    finally:
        s.shutdown()
        s.server_close()


def _req(port, method, path, body=None, headers=None, skip_host=False):
    c = HTTPConnection("127.0.0.1", port)
    c.putrequest(method, path, skip_host=skip_host, skip_accept_encoding=True)
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    data = body.encode() if isinstance(body, str) else body
    if data is not None and not any(k.lower() == "content-length" for k in (headers or {})):
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data)
    r = c.getresponse()
    out = r.status, r.read()
    c.close()
    return out


GOOD = json.dumps({"file": "assumptions", "section": "prime.rxpass", "key": "base", "value": 240})


# --- 1. Host, Origin, Content-Type -----------------------------------------


def test_host_allowed_names_the_server_only():
    names = frozenset({"board", "board.example"})
    ok = lambda h, bound="100.64.0.1": board.host_allowed(h, bound, 8766, names)
    assert ok("100.64.0.1:8766") and ok("localhost:8766") and ok("127.0.0.1:8766") and ok("[::1]:8766")
    assert ok("board:8766") and ok("BOARD.example:8766") and ok("board.example")  # a proxy in front: no port
    assert not ok("evil.example:8766") and not ok("100.64.0.1:9999") and not ok("") and not ok(None)
    assert not ok("100.64.0.2:8766")  # another address: bound to one, answer to one
    assert not ok("user@localhost:8766") and not ok("localhost:x") and not ok("localhost/x")
    assert ok("10.0.0.5:8766", bound="0.0.0.0") and not ok("evil.example:8766", bound="0.0.0.0")


def test_origin_must_match_host():
    assert board.origin_allowed(None, "h:1")
    assert board.origin_allowed("http://h:1", "h:1") and board.origin_allowed("http://H:1", "h:1")
    assert not board.origin_allowed("http://evil:1", "h:1") and not board.origin_allowed("null", "h:1")
    assert not board.origin_allowed("http://h:2", "h:1") and not board.origin_allowed("http://h:1", None)


def test_foreign_host_is_403_on_get_and_post_and_nothing_changes(srv):
    root, port = srv
    before = (root / "assumptions.toml").read_text()
    status, _ = _req(port, "GET", "/", headers={"Host": f"evil.example:{port}"}, skip_host=True)
    assert status == 403
    status, _ = _req(port, "POST", "/record", GOOD, {"Host": f"evil.example:{port}", "Content-Type": "application/json"}, skip_host=True)
    assert status == 403
    status, _ = _req(port, "GET", "/favicon.ico", headers={"Host": f"board:{port}"}, skip_host=True)
    assert status in (200, 404)  # a MagicDNS name passes the Host check
    assert (root / "assumptions.toml").read_text() == before


def test_post_needs_same_origin_and_json(srv):
    root, port = srv
    before = (root / "assumptions.toml").read_text()
    host = f"127.0.0.1:{port}"
    for headers, code in [
        ({"Content-Type": "application/json", "Origin": "http://evil.example"}, 403),
        ({"Content-Type": "application/json", "Origin": "null"}, 403),
        ({"Content-Type": "text/plain"}, 415),  # a cross-site form's simple request
        ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
        ({}, 415),
    ]:
        status, _ = _req(port, "POST", "/record", GOOD, headers)
        assert status == code, headers
    assert (root / "assumptions.toml").read_text() == before
    status, body = _req(port, "POST", "/record", GOOD, {"Content-Type": "application/json; charset=utf-8", "Origin": f"http://{host}"})
    assert status == 200 and json.loads(body)["recorded"]


def test_the_boards_own_fetches_send_json():
    fetches = re.findall(r"fetch\('(/[a-z]+)',\{method:'POST',headers:\{([^}]*)\}", board.JS)
    assert sorted(p for p, _ in fetches) == ["/override", "/record"]
    assert all("'Content-Type':'application/json'" in h for _, h in fetches)


# --- 4. Content-Length -------------------------------------------------------


@pytest.mark.parametrize("length", ["abc", "-5", "1.5"])
def test_bad_content_length_is_400(srv, length):
    _, port = srv
    status, body = _req(port, "POST", "/record", "", {"Content-Type": "application/json", "Content-Length": length})
    assert status == 400 and "Content-Length" in json.loads(body)["error"]


def test_oversized_body_is_413(srv):
    _, port = srv
    status, _ = _req(port, "POST", "/record", "", {"Content-Type": "application/json", "Content-Length": str(board.MAX_BODY + 1)})
    assert status == 413


# --- 3 and 8. Errors answer and are logged ----------------------------------


def test_an_unexpected_error_is_a_json_500_with_a_logged_traceback(srv, monkeypatch, capfd):
    _, port = srv

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(board, "record", boom)
    status, body = _req(port, "POST", "/record", GOOD, {"Content-Type": "application/json"})
    assert status == 500 and "disk on fire" in json.loads(body)["error"]
    err = capfd.readouterr().err
    assert "Traceback" in err and "RuntimeError: disk on fire" in err
    assert re.search(r'"POST /record HTTP/1\.1" 500', err)


def test_a_get_500_logs_its_traceback_and_a_200_is_quiet(srv, monkeypatch, capfd):
    _, port = srv
    status, _ = _req(port, "GET", "/todo")
    assert status == 200
    assert "GET /todo" not in capfd.readouterr().err  # a page view that worked is noise

    def boom(*a, **k):
        raise RuntimeError("bad state")

    monkeypatch.setattr(board, "load_state", boom)
    status, body = _req(port, "GET", "/todo")
    assert status == 500 and b"bad state" in body
    err = capfd.readouterr().err
    assert "RuntimeError: bad state" in err and "Traceback" in err and '"GET /todo HTTP/1.1" 500' in err


def test_every_post_is_logged(srv, capfd):
    _, port = srv
    status, _ = _req(port, "POST", "/record", GOOD, {"Content-Type": "application/json"})
    assert status == 200
    assert '"POST /record HTTP/1.1" 200' in capfd.readouterr().err


def test_a_failed_audit_append_is_logged_not_a_failed_save(root, capfd):
    (root / "data" / "board-decisions.jsonl").mkdir()  # appending to it fails
    res = board.record(_cfg(root), json.loads(GOOD), TODAY)
    assert [r["key"] for r in res["recorded"]] == ["base"]
    assert "base = 240" in (root / "assumptions.toml").read_text()
    err = capfd.readouterr().err
    assert "audit line was not written" in err and "Traceback" in err


# --- 5. A batch across both files is all or nothing ---------------------------


BATCH = [{"file": "rules", "section": "memberships.prime", "key": "fee", "value": 150},
         {"file": "assumptions", "section": "prime.rxpass", "key": "base", "value": 240}]


def test_batch_rolls_back_when_the_second_file_refuses(root, monkeypatch):
    cfg = _cfg(root)
    rules0, assumptions0 = (root / "rules.toml").read_text(), (root / "assumptions.toml").read_text()
    real = tomledit.write_edits

    def racing(path, edit):
        if Path(path).name == "assumptions.toml":  # someone edits it between the check and the write
            Path(path).write_text(assumptions0 + "\n# edited by hand\n")
        return real(path, edit)

    monkeypatch.setattr(tomledit, "write_edits", racing)
    with pytest.raises(board.Refused, match="changed on disk"):
        board.record(cfg, BATCH, TODAY)
    assert (root / "rules.toml").read_text() == rules0  # put back
    assert not (root / "data" / "board-decisions.jsonl").exists()


def test_batch_refuses_before_writing_when_a_file_moved_since_it_was_read(root, monkeypatch):
    cfg = _cfg(root)
    rules0 = (root / "rules.toml").read_text()
    real = tomledit.set_key

    def edit_then_move(text, section, key, value):  # assumptions changes after it was read, before anything is written
        if section == "prime.rxpass":
            (root / "assumptions.toml").write_text((root / "assumptions.toml").read_text() + "\n# moved\n")
        return real(text, section, key, value)

    monkeypatch.setattr(tomledit, "set_key", edit_then_move)
    writes = []
    monkeypatch.setattr(tomledit, "write_edits", lambda p, e: writes.append(p))
    with pytest.raises(board.Refused, match="assumptions.toml changed on disk"):
        board.record(cfg, BATCH, TODAY)
    assert writes == [] and (root / "rules.toml").read_text() == rules0


def test_a_write_that_cannot_be_undone_is_audited(root, monkeypatch, capfd):
    cfg = _cfg(root)
    real = tomledit.write_edits
    calls = []

    def flaky(path, edit):
        calls.append(Path(path).name)
        if len(calls) == 2:  # the assumptions write
            raise OSError("disk full")
        if len(calls) == 3:  # the undo of rules.toml
            raise OSError("still full")
        return real(path, edit)

    monkeypatch.setattr(tomledit, "write_edits", flaky)
    with pytest.raises(OSError, match="disk full"):
        board.record(cfg, BATCH, TODAY)
    assert "fee = 150" in (root / "rules.toml").read_text()
    audit = [json.loads(x) for x in (root / "data" / "board-decisions.jsonl").read_text().splitlines()]
    assert [(a["file"], a["key"], a["after"]) for a in audit] == [("rules.toml", "fee", 150)]
    assert "could not undo rules.toml" in capfd.readouterr().err


# --- 6. The edit sheet escapes the account name --------------------------------


def test_edit_sheet_escapes_the_account(root):
    s = board.load_state(_cfg(root), TODAY)
    t = copy.copy(next(x for x in s.txns if x.txn_id == "fix14"))
    t.account = "<img src=x onerror=alert(1)>"
    html = board._edit_panel(s, t, {}, {}, {t.account: {t.account_number}}, [])
    assert "<img src=x" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html


# --- 2 and 7. Caches -------------------------------------------------------------


def test_load_checks_skips_tmp_and_raw_and_survives_a_vanishing_file(root, monkeypatch):
    cfg = _cfg(root)
    runs = []
    real = check.run_all
    monkeypatch.setattr(check, "run_all", lambda r, rules: runs.append(1) or real(r, rules))
    rules = board.model.resolve_points(home.load_rules(cfg.rules))
    board.load_checks(cfg, rules)
    d = root / "data" / "plaid" / "production"
    (d / "items.tmp").write_text("{")  # a sync mid-write
    (d / "raw" / "fix").mkdir(parents=True, exist_ok=True)
    (d / "raw" / "fix" / "sync-1.json").write_text("[]")  # run_all never reads raw/
    os.symlink(d / "gone.json", d / "dangling.json")  # stat raises FileNotFoundError
    board.load_checks(cfg, rules)
    assert len(runs) == 1
    (d / "balances.jsonl").write_text("")  # a file it does read
    board.load_checks(cfg, rules)
    assert len(runs) == 2
    changed = copy.deepcopy(rules)
    changed.setdefault("accounts", {})["4242"] = {"card": next(iter(rules["cards"])), "feed_lacks_rewards": True}
    board.load_checks(cfg, changed)  # the reward rates it takes from rules
    assert len(runs) == 3


def test_state_is_cached_until_an_input_changes(root, monkeypatch):
    cfg = _cfg(root)
    loads = []
    real = board._load_state
    monkeypatch.setattr(board, "_load_state", lambda c, d: loads.append(d) or real(c, d))
    a = board.load_state(cfg, TODAY)
    b = board.load_state(cfg, TODAY)
    assert len(loads) == 1 and a is not b and a.txns is b.txns
    b.checks = None  # a caller's copy: the cached one is untouched
    assert board.load_state(cfg, TODAY).checks is not None
    board.load_state(cfg, TODAY + timedelta(days=1))  # a new day
    assert len(loads) == 2
    for touch in (
        lambda: board.record(cfg, json.loads(GOOD), TODAY),  # assumptions.toml
        lambda: board.record(cfg, BATCH[0], TODAY),  # rules.toml
        lambda: board.record_override(cfg, {"id": "fix14", "family": "restaurants"}, today=TODAY + timedelta(days=1)),  # overrides.json
        lambda: (root / "data" / "plaid" / "production" / "fix.json").write_text((root / "data" / "plaid" / "production" / "fix.json").read_text() + " "),  # a feed file
        lambda: (root / "data" / "walmart-orders-2099-01-01.json").write_text("[]"),  # a new capture
    ):
        n = len(loads)
        touch()
        board.load_state(cfg, TODAY + timedelta(days=1))
        assert len(loads) == n + 1
    s = board.load_state(cfg, TODAY + timedelta(days=1))
    assert next(t for t in s.txns if t.txn_id == "fix14").family == "restaurants"  # the override shows at once
    board.load_state(cfg, TODAY + timedelta(days=1))
    (root / "data" / "plaid" / "production" / "items.tmp").write_text("{")
    board.load_state(cfg, TODAY + timedelta(days=1))
    assert len(loads) == n + 1  # a sync's temp file is not a change


def test_override_validates_against_the_cached_state(root, monkeypatch):
    cfg = _cfg(root)
    board.load_state(cfg, TODAY)
    monkeypatch.setattr(board.feed, "labelled", lambda *a, **k: pytest.fail("reread the feed"))
    with pytest.raises(board.Refused, match="no spend row"):
        board.record_override(cfg, {"id": "fix13", "family": "gas"}, today=TODAY)  # a transfer
    res = board.record_override(cfg, {"id": "fix14", "category": "dining"}, today=TODAY)
    assert res["changed"] is True  # overrides.json moved, so the next read rebuilds, once


def test_git_state_is_cached_on_the_index_and_head(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("no git")
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "rules.toml").write_text("a = 1\n")
    (tmp_path / "assumptions.toml").write_text("b = 1\n")
    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    cfg = _cfg(tmp_path)
    assert board.cached_git_state(cfg) == {"rules.toml": "committed", "assumptions.toml": "committed"}
    (tmp_path / "rules.toml").write_text("a = 2\n")
    assert board.cached_git_state(cfg)["rules.toml"] == "uncommitted change"
    run("add", "rules.toml")
    assert board.cached_git_state(cfg)["rules.toml"] == "uncommitted change"  # staged, not committed
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "y")
    assert board.cached_git_state(cfg)["rules.toml"] == "committed"


def test_cash_months_parses_statements_once_per_change(root, monkeypatch):
    cfg = _cfg(root)
    rules = home.load_rules(cfg.rules)
    if not any(a.get("feed_lacks_rewards") for a in rules.get("accounts", {}).values()):
        pytest.skip("no feed_lacks_rewards account in rules.toml")
    (root / "data" / "statements").mkdir(parents=True)
    (root / "data" / "statements" / "a.pdf").write_bytes(b"x")
    parsed = []
    # check.run_all parses statements too (with the Apple folder): count cash_months's own calls
    sdir = root / "data" / "statements"
    monkeypatch.setattr(board.statements, "load_dir", lambda *d: (parsed.append(d) if d == (sdir,) else None) or ([], []))
    s = board.load_state(cfg, TODAY)
    first = board.cash_months(s)
    assert board.cash_months(board.load_state(cfg, TODAY)) == first and len(parsed) == 1
    (root / "data" / "statements" / "b.pdf").write_bytes(b"y")
    board.cash_months(board.load_state(cfg, TODAY))
    assert len(parsed) == 2
    (root / "data" / "statements" / "c.pdf.tmp").write_bytes(b"z")
    board.cash_months(board.load_state(cfg, TODAY))
    assert len(parsed) == 2


# --- fsync before replace -------------------------------------------------------


def test_override_write_is_fsynced_and_leaves_no_temp(root, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(board.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    board.record_override(_cfg(root), {"id": "fix14", "family": "restaurants"}, today=TODAY)
    assert synced
    assert cat.load_overrides(root / "data" / "overrides.json") == {"fix14": {"family": "restaurants"}}
    assert not [p for p in (root / "data").iterdir() if p.name.endswith(".tmp")]


def test_report_and_board_share_cents():
    from penny import report

    assert board.cents is report.cents
