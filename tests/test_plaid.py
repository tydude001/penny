import re

import pytest

from penny import plaid


def page(added=(), modified=(), removed=(), cursor="c", more=False, status="HISTORICAL_UPDATE_COMPLETE", accounts=()):
    return {"added": [{"transaction_id": t, "account_id": "a", "amount": v} for t, v in added],
            "modified": [{"transaction_id": t, "account_id": "a", "amount": v} for t, v in modified],
            "removed": [{"transaction_id": t} for t in removed],
            "accounts": [{"account_id": a} for a in accounts],
            "next_cursor": cursor, "has_more": more, "transactions_update_status": status}


def test_apply_pages_in_order():
    ledger = {"accounts": {}, "transactions": {}}
    n = plaid.apply_pages(ledger, [page(added=[("t1", 5), ("t2", 7)], accounts=["a"]),
                                   page(modified=[("t1", 6)], removed=["t2", "gone"])])
    assert ledger["transactions"] == {"t1": {"transaction_id": "t1", "account_id": "a", "amount": 6}}
    assert set(ledger["accounts"]) == {"a"}
    # A removal of a row never seen doesn't count.
    assert n == {"added": 2, "modified": 1, "removed": 1}


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def post(self, path, body):
        self.calls.append(body.get("cursor"))
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_fetch_sync_restarts_from_original_cursor_on_mutation():
    err = plaid.PlaidError({"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"})
    c = FakeClient([page(cursor="p1", more=True), err, page(cursor="p1", more=True), page(cursor="end")])
    pages, cur = plaid.fetch_sync(c, "tok", "start")
    assert c.calls == ["start", "p1", "start", "p1"]
    assert len(pages) == 2 and cur == "end"


def test_fetch_sync_other_errors_raise():
    c = FakeClient([plaid.PlaidError({"error_code": "ITEM_LOGIN_REQUIRED"})])
    try:
        plaid.fetch_sync(c, "tok", None)
    except plaid.PlaidError as e:
        assert e.code == "ITEM_LOGIN_REQUIRED"
    else:
        raise AssertionError("expected PlaidError")


def test_wait_ready_polls_past_the_complete_flag(tmp_path, monkeypatch):
    # The Sandbox sequence seen live: history lands after the flag, not with it.
    monkeypatch.setattr(plaid.time, "sleep", lambda s: None)
    store = plaid.Store(tmp_path, "sandbox")
    store.save_items({"i": {"access_token": "tok", "cursor": None}})
    c = FakeClient([page(status="NOT_READY", cursor="1"), page(added=[("new", 1)], cursor="2"),
                    page(cursor="3"), page(added=[("old", 2), ("older", 3)], cursor="4")])
    res = plaid.wait_ready(c, store, "i")
    assert res["rows"] == 3 and store.items()["i"]["cursor"] == "4"
    assert len(list((store.dir / "raw" / "i").iterdir())) == 4


def test_raw_pages_never_overwrite(tmp_path, monkeypatch):
    store = plaid.Store(tmp_path, "sandbox")

    class Frozen(plaid.datetime):
        @classmethod
        def now(cls, tz=None):
            return plaid.datetime(2026, 9, 26, tzinfo=tz)

    monkeypatch.setattr(plaid, "datetime", Frozen)
    a, b = store.save_raw("i", [1]), store.save_raw("i", [2])
    assert a != b and a.exists() and b.exists()


def test_snapshot_puts_statement_fields_on_credit_accounts_only():
    accts = [{"account_id": "c", "mask": "1111", "type": "credit", "balances": {"current": 50.0, "limit": 1000}},
             {"account_id": "d", "mask": "2222", "type": "depository", "balances": {"current": 9.0, "available": 9.0}}]
    lines = plaid.snapshot("i", accts, {"c": {"last_statement_balance": 40.0, "last_statement_issue_date": "2026-09-16"}}, "T")
    assert lines[0]["current"] == 50.0 and lines[0]["last_statement_balance"] == 40.0
    assert "last_statement_balance" not in lines[1] and lines[1]["available"] == 9.0


class Router:
    """Answers by path; a path mapped to an exception raises it."""

    def __init__(self, routes):
        self.routes, self.paths = routes, []

    def post(self, path, body):
        self.paths.append(path)
        r = self.routes[path]
        if isinstance(r, Exception):
            raise r
        return r


def test_snapshot_item_stops_asking_an_institution_without_liabilities(tmp_path):
    store = plaid.Store(tmp_path, "sandbox")
    store.save_items({"i": {"access_token": "tok", "cursor": "c"}})
    store.save_ledger("i", {"accounts": {"c": {"account_id": "c", "type": "credit", "balances": {"current": 5.0}}},
                            "transactions": {}})
    c = Router({"/liabilities/get": plaid.PlaidError({"error_code": "PRODUCTS_NOT_SUPPORTED"})})
    assert plaid.snapshot_item(c, store, "i") == 1
    assert store.items()["i"]["liabilities"] is False
    plaid.snapshot_item(c, store, "i")
    assert c.paths == ["/liabilities/get"]  # the second snapshot didn't ask again
    assert [b["current"] for b in store.balances()] == [5.0, 5.0]


def test_sync_all_records_a_failed_item_and_carries_on(tmp_path):
    store = plaid.Store(tmp_path, "sandbox")
    store.save_items({"bad": {"access_token": "x", "cursor": "c", "institution": "Bad"},
                      "good": {"access_token": "y", "cursor": "c", "institution": "Good"}})

    class Client:
        def post(self, path, body):
            if body["access_token"] == "x":
                raise plaid.PlaidError({"error_code": "ITEM_LOGIN_REQUIRED"})
            return page(accounts=["a"])

    lines, ok = plaid.sync_all(Client(), store)
    assert not ok and "FAILED" in lines[0] and lines[1].startswith("Good:")
    last = store.last_sync()
    assert last["ok"] is False and "ITEM_LOGIN_REQUIRED" in last["items"]["Bad"] and last["items"]["Good"] == "ok"


def test_update_mode_token_adds_statements_within_two_years():
    sent = {}

    class C:
        def post(self, path, body):
            sent.update(body)
            return {"link_token": "lt"}

    assert plaid.link_token(C(), None, "access") == "lt"
    assert sent["access_token"] == "access" and sent["products"] == ["statements"]
    from datetime import date
    span = date.fromisoformat(sent["statements"]["end_date"]) - date.fromisoformat(sent["statements"]["start_date"])
    assert span.days < 730 and "transactions" not in sent


def test_client_user_id_is_random_and_kept_per_instance(tmp_path):
    a = plaid.Store(tmp_path, "sandbox").client_user_id()
    assert re.fullmatch(r"[0-9a-f]{32}", a)
    assert plaid.Store(tmp_path, "production").client_user_id() == a  # one per instance, every env
    assert (tmp_path / "data" / "plaid" / "client-user-id").stat().st_mode & 0o777 == 0o600
    assert plaid.Store(tmp_path / "other", "sandbox").client_user_id() != a


def test_link_token_sends_the_given_user_id():
    sent = {}

    class C:
        def post(self, path, body):
            sent.update(body)
            return {"link_token": "lt"}

    plaid.link_token(C(), None, client_user_id="abc")
    assert sent["user"] == {"client_user_id": "abc"}


def test_production_link_needs_a_redirect_uri(tmp_path):
    from penny.__main__ import main

    with pytest.raises(SystemExit) as e:
        main(["--home", str(tmp_path), "plaid", "link", "--env", "production"])
    assert "--redirect-uri" in str(e.value.code)
