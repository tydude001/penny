"""Review fixes to the Plaid feed: locking, durability, errors, retries,
re-login, snapshot failures and the first-pull poll. No network: urlopen is
replaced, and every sleep is injected."""

import http.client
import io
import json
import os
import stat
import urllib.error

import pytest
from test_plaid import FakeClient, Router, page

from penny import __main__ as cli
from penny import plaid


def store_with(tmp_path, items):
    store = plaid.Store(tmp_path, "sandbox")
    store.save_items(items)
    return store


# 1. items.json races --------------------------------------------------------

def test_a_link_during_a_sync_survives_the_sync(tmp_path):
    store = store_with(tmp_path, {"i": {"access_token": "tok", "cursor": "c"}})

    class LinkMidSync:
        def post(self, path, body):
            # Another process links a card while this sync is on the network.
            plaid.Store(tmp_path, "sandbox").put_item("new", {"access_token": "t2", "cursor": None})
            return page(cursor="c2")

    plaid.sync_item(LinkMidSync(), store, "i")
    items = store.items()
    assert set(items) == {"i", "new"} and items["new"]["access_token"] == "t2"
    assert items["i"]["cursor"] == "c2"


def test_snapshot_keeps_a_cursor_moved_by_a_concurrent_sync(tmp_path):
    store = store_with(tmp_path, {"i": {"access_token": "tok", "cursor": "c"}})
    store.save_ledger("i", {"accounts": {"c": {"account_id": "c", "type": "credit", "balances": {"current": 1.0}}},
                            "transactions": {}})

    class MovesCursor:
        def post(self, path, body):
            plaid.Store(tmp_path, "sandbox").update_item("i", cursor="moved")
            raise plaid.PlaidError({"error_code": "PRODUCTS_NOT_SUPPORTED"})

    plaid.snapshot_item(MovesCursor(), store, "i")
    assert store.items()["i"] == {"access_token": "tok", "cursor": "moved", "liabilities": False}


def test_update_item_does_not_resurrect_a_removed_item(tmp_path):
    store = store_with(tmp_path, {})
    assert store.update_item("gone", cursor="x") is False and store.items() == {}


def test_a_second_sync_waits_then_gives_up_without_touching_anything(tmp_path):
    store = store_with(tmp_path, {"i": {"access_token": "tok", "cursor": "c"}})
    waits = []
    with store.sync_lock(), pytest.raises(plaid.SyncBusy):
        plaid.sync_all(Router({}), store, lock_wait=0.01, sleep=waits.append)
    assert store.last_sync() is None
    assert plaid.sync_all(Router({"/transactions/sync": page()}), store)[1] is True  # free again


def test_cli_sync_exits_cleanly_when_another_sync_is_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PENNY_HOME", str(tmp_path))
    monkeypatch.setattr(plaid.Client, "from_root", classmethod(lambda cls, root, env: Router({})))
    monkeypatch.setattr(plaid, "SYNC_LOCK_WAIT", 0)
    store = store_with(tmp_path, {"i": {"access_token": "tok", "cursor": "c"}})

    def busy(client, store):
        raise plaid.SyncBusy("another penny plaid sync (sandbox) is still running")

    monkeypatch.setattr(plaid, "sync_all", busy)
    cli.main(["plaid", "sync"])  # no SystemExit
    assert "still running" in capsys.readouterr().out and store.last_sync() is None


# 2. durability ---------------------------------------------------------------

def test_writes_are_fsynced_and_items_json_keeps_a_private_backup(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(plaid.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    store = store_with(tmp_path, {"a": {"access_token": "1"}})
    assert not (store.dir / "items.json.bak").exists()  # nothing to back up the first time
    assert len(synced) == 2  # the temp file, then the directory
    store.save_items({"a": {"access_token": "1"}, "b": {"access_token": "2"}})
    bak = store.dir / "items.json.bak"
    assert json.loads(bak.read_text()) == {"a": {"access_token": "1"}}
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600
    assert stat.S_IMODE((store.dir / "items.json").stat().st_mode) == 0o600
    assert not list(store.dir.glob("*.tmp"))


def test_a_damaged_items_json_is_not_backed_up_over_the_good_one(tmp_path):
    store = store_with(tmp_path, {"a": {"access_token": "1"}})
    store.save_items({"a": {"access_token": "2"}})
    (store.dir / "items.json").write_text("{not json")
    store.save_items({"a": {"access_token": "3"}})
    assert json.loads((store.dir / "items.json.bak").read_text()) == {"a": {"access_token": "1"}}


# 3 and 4. Client errors and retries -----------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def http_error(code, body: bytes):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


def client_with(monkeypatch, script):
    """A real Client whose urlopen plays ``script``; returns it and the sleeps it asked for."""
    script, sleeps = list(script), []

    def urlopen(req, timeout):
        r = script.pop(0)
        if isinstance(r, BaseException):
            raise r
        return FakeResponse(json.dumps(r).encode() if isinstance(r, dict) else r)

    monkeypatch.setattr(plaid.urllib.request, "urlopen", urlopen)
    return plaid.Client("sandbox", "id", "secret", sleep=sleeps.append), sleeps, script


def test_a_non_json_error_body_becomes_a_plaid_error_with_status_and_snippet(monkeypatch):
    c, sleeps, _ = client_with(monkeypatch, [http_error(403, b"<html>  Forbidden by proxy </html>")])
    with pytest.raises(plaid.PlaidError) as e:
        c.post("/x", {})
    assert e.value.status == 403 and e.value.code == "HTTP_403" and "Forbidden by proxy" in str(e.value)
    assert sleeps == []  # a 403 isn't transient


def test_5xx_and_network_failures_are_retried_with_backoff(monkeypatch):
    c, sleeps, _ = client_with(monkeypatch, [http_error(502, b"Bad Gateway"), urllib.error.URLError("dns"),
                                             http.client.IncompleteRead(b"x"), {"ok": 1}])
    assert c.post("/x", {}) == {"ok": 1}
    assert sleeps == list(plaid.RETRY_DELAYS)


def test_retries_are_bounded(monkeypatch):
    c, sleeps, script = client_with(monkeypatch, [TimeoutError("slow")] * 10)
    with pytest.raises(TimeoutError):
        c.post("/x", {})
    assert len(sleeps) == len(plaid.RETRY_DELAYS) and len(script) == 10 - len(plaid.RETRY_DELAYS) - 1


@pytest.mark.parametrize("status,body", [
    (429, {"error_type": "RATE_LIMIT_EXCEEDED", "error_code": "TRANSACTIONS_SYNC_LIMIT"}),
    (400, {"error_type": "INSTITUTION_ERROR", "error_code": "INSTITUTION_DOWN"}),
    (400, {"error_type": "INSTITUTION_ERROR", "error_code": "INSTITUTION_NOT_RESPONDING"}),
    (500, {"error_type": "API_ERROR", "error_code": "INTERNAL_SERVER_ERROR"}),
    (503, {"error_type": "API_ERROR", "error_code": "PLANNED_MAINTENANCE"}),
])
def test_transient_plaid_errors_are_retried(monkeypatch, status, body):
    c, sleeps, _ = client_with(monkeypatch, [http_error(status, json.dumps(body).encode()), {"ok": 1}])
    assert c.post("/x", {}) == {"ok": 1} and sleeps == [plaid.RETRY_DELAYS[0]]


@pytest.mark.parametrize("code", ["ITEM_LOGIN_REQUIRED", "INVALID_ACCESS_TOKEN", "INVALID_FIELD"])
def test_permanent_plaid_errors_are_not_retried(monkeypatch, code):
    body = json.dumps({"error_type": "ITEM_ERROR", "error_code": code}).encode()
    c, sleeps, _ = client_with(monkeypatch, [http_error(400, body), {"ok": 1}])
    with pytest.raises(plaid.PlaidError) as e:
        c.post("/x", {})
    assert e.value.code == code and sleeps == []


def test_a_mutation_restart_waits_first():
    err = plaid.PlaidError({"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"})
    sleeps = []
    c = FakeClient([err, err, page(cursor="end")])
    assert plaid.fetch_sync(c, "tok", "s", sleep=sleeps.append)[1] == "end"
    assert sleeps == list(plaid.MUTATION_DELAYS)
    with pytest.raises(plaid.SyncMutationError):
        plaid.fetch_sync(FakeClient([err, err, err]), "tok", "s", sleep=sleeps.append)


def test_sync_all_survives_any_exception_and_saves_last_sync(tmp_path):
    store = store_with(tmp_path, {"bad": {"access_token": "x", "cursor": "c", "institution": "Bad"},
                                  "good": {"access_token": "y", "cursor": "c", "institution": "Good"}})

    class C:
        def post(self, path, body):
            if body["access_token"] == "x":
                raise ValueError("a parser bug")
            return page(accounts=["a"])

    _lines, ok = plaid.sync_all(C(), store)
    last = store.last_sync()
    assert not ok and "ValueError: a parser bug" in last["items"]["Bad"] and last["items"]["Good"] == "ok"
    assert last["retry"] is True


# 5. exit status ---------------------------------------------------------------

def run_sync(tmp_path, monkeypatch, client):
    monkeypatch.setenv("PENNY_HOME", str(tmp_path))
    monkeypatch.setattr(plaid.Client, "from_root", classmethod(lambda cls, root, env: client))
    with pytest.raises(SystemExit) as e:
        cli.main(["plaid", "sync"])
    return e.value.code


def test_sync_exits_2_when_only_a_login_is_needed(tmp_path, monkeypatch):
    store_with(tmp_path, {"i": {"access_token": "x", "cursor": "c", "institution": "Bank"}})
    err = plaid.PlaidError({"error_code": "ITEM_LOGIN_REQUIRED"}, 400)
    assert run_sync(tmp_path, monkeypatch, Router({"/transactions/sync": err})) == 2


def test_sync_exits_1_when_a_failure_may_clear(tmp_path, monkeypatch):
    store_with(tmp_path, {"i": {"access_token": "x", "cursor": "c", "institution": "Bank"},
                          "j": {"access_token": "y", "cursor": "c", "institution": "Other"}})

    class C:
        def post(self, path, body):
            if body["access_token"] == "x":
                raise plaid.PlaidError({"error_code": "ITEM_LOGIN_REQUIRED"}, 400)
            raise urllib.error.URLError("no route")

    assert run_sync(tmp_path, monkeypatch, C()) == 1


# 6. re-login -------------------------------------------------------------------

def test_relogin_token_asks_for_no_product():
    sent = {}

    class C:
        def post(self, path, body):
            sent.update(body)
            return {"link_token": "lt"}

    assert plaid.link_token(C(), None, "access", statements=False) == "lt"
    assert sent["access_token"] == "access"
    assert "products" not in sent and "statements" not in sent and "transactions" not in sent


def test_relogin_page_does_not_ask_for_statements():
    relogin = plaid.link_page("production", "Sign in to Bank again", update=True, statements=False)
    assert "const UPDATE = true;" in relogin and "const STATEMENTS = false;" in relogin
    adding = plaid.link_page("production", "Add Statements", update=True, statements=True)
    assert "const STATEMENTS = true;" in adding
    assert "const STATEMENTS = false;" in plaid.link_page("sandbox", "Link a card", update=False, statements=True)


def test_relogin_flag_parses_and_excludes_update(monkeypatch):
    got = {}
    monkeypatch.setattr(plaid, "serve_link", lambda *a: got.setdefault("args", a))
    cli.main(["plaid", "link", "--relogin", "item1"])
    assert got["args"][-2:] == (None, "item1")
    with pytest.raises(SystemExit):
        cli.main(["plaid", "link", "--relogin", "a", "--update", "b"])


def test_a_login_failure_names_the_relogin_command(tmp_path):
    store = store_with(tmp_path, {"item1": {"access_token": "x", "cursor": "c", "institution": "Bank"}})
    plaid.sync_all(Router({"/transactions/sync": plaid.PlaidError({"error_code": "ITEM_LOGIN_REQUIRED"}, 400)}), store)
    msg = store.last_sync()["items"]["Bank"]
    assert "ITEM_LOGIN_REQUIRED" in msg and "penny plaid link --env sandbox --relogin item1" in msg
    assert store.last_sync()["retry"] is False


# 7. snapshot failure after a good sync ----------------------------------------

def test_a_snapshot_failure_is_recorded_apart_from_the_transactions(tmp_path):
    store = store_with(tmp_path, {"i": {"access_token": "x", "cursor": "c", "institution": "Card"}})
    credit = page(cursor="c2", added=[("t", 1)])
    credit["accounts"] = [{"account_id": "a", "type": "credit"}]
    c = Router({"/transactions/sync": credit,
                "/liabilities/get": plaid.PlaidError({"error_code": "INVALID_FIELD"}, 400)})
    lines, ok = plaid.sync_all(c, store)
    msg = store.last_sync()["items"]["Card"]
    assert not ok and msg.startswith("transactions ok; balance snapshot failed:") and "INVALID_FIELD" in msg
    assert "+1" in lines[0] and "balance snapshot FAILED" in lines[0]
    assert store.items()["i"]["cursor"] == "c2" and "t" in store.ledger("i")["transactions"]


# 10. the first-pull poll -------------------------------------------------------

def test_wait_ready_writes_the_ledger_and_items_once(tmp_path, monkeypatch):
    store = store_with(tmp_path, {"i": {"access_token": "tok", "cursor": None}})
    writes = []
    monkeypatch.setattr(store, "save_ledger", lambda i, led, real=store.save_ledger: (writes.append("ledger"), real(i, led)))
    monkeypatch.setattr(store, "update_item", lambda i, real=store.update_item, **f: (writes.append("items"), real(i, **f)))
    c = FakeClient([page(status="NOT_READY", cursor="1"), page(added=[("new", 1)], cursor="2"),
                    page(cursor="3"), page(added=[("old", 2)], cursor="4")])
    sleeps = []
    res = plaid.wait_ready(c, store, "i", sleep=sleeps.append)
    assert writes == ["ledger", "items"] and sleeps == [5, 5, 5]
    assert c.calls == [None, "1", "2", "3"]  # the cursor carried in memory between polls
    assert res["rows"] == 2 and res["added"] == 2 and store.items()["i"]["cursor"] == "4"
    assert len(list((store.dir / "raw" / "i").iterdir())) == 4  # every poll's pages kept


def test_a_failed_poll_leaves_the_old_cursor(tmp_path):
    store = store_with(tmp_path, {"i": {"access_token": "tok", "cursor": None}})
    c = FakeClient([page(status="NOT_READY", cursor="1"), plaid.PlaidError({"error_code": "INVALID_FIELD"})])
    with pytest.raises(plaid.PlaidError):
        plaid.wait_ready(c, store, "i", sleep=lambda s: None)
    assert store.items()["i"]["cursor"] is None and not (store.dir / "i.json").exists()
