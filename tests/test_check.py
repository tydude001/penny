from penny import check, plaid


def store_with(tmp_path, snap, rows):
    store = plaid.Store(tmp_path, "sandbox")
    store.save_items({"i": {"access_token": "t"}})
    store.save_ledger("i", {"accounts": {}, "transactions": {str(n): r for n, r in enumerate(rows)}})
    store.append_balances([{"account_id": "c", "name": "CARD", "mask": "1111", **snap}])
    return store


def row(d, amt, pending=False):
    return {"account_id": "c", "date": d, "amount": amt, "pending": pending, "name": "x"}


SNAP = {"last_statement_issue_date": "2026-09-16", "last_statement_balance": 100.0}


def test_statement_to_current_ties(tmp_path):
    # The closing date's row belongs to the statement; pending rows wait.
    rows = [row("2026-09-16", 999.0), row("2026-09-17", 25.5), row("2026-09-20", -100.0), row("2026-09-21", 7.0, True)]
    (r,) = check.run(store_with(tmp_path, {**SNAP, "current": 25.5}, rows))
    assert r.ok is True and r.account == "CARD …1111"


def test_a_miss_reports_the_gap_and_the_row_that_matches_it(tmp_path):
    rows = [row("2026-09-01", 12.34), row("2026-09-17", 5.0)]
    (r,) = check.run(store_with(tmp_path, {**SNAP, "current": 117.34}, rows))
    assert r.ok is False and r.gap == 12.34
    assert r.near[0]["amount"] == 12.34


def test_newest_snapshot_wins_and_accounts_without_statements_are_unchecked(tmp_path):
    store = store_with(tmp_path, {**SNAP, "current": 1.0}, [])
    store.append_balances([{"account_id": "c", "name": "CARD", "mask": "1111", **SNAP, "current": 100.0},
                           {"account_id": "s", "name": "Checking", "mask": "2222", "current": 5.0}])
    a, b = check.run(store)
    assert a.ok is True and b.ok is None
