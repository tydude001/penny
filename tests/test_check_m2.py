"""Statement-PDF, reward-inference and snapshot checks (M2)."""

from datetime import date

from penny import check, plaid
from penny.statements import Line, Statement


def row(d, amt, pending=False, acct="o", cat="GENERAL_MERCHANDISE"):
    return {"account_id": acct, "date": d, "amount": amt, "pending": pending, "name": "x",
            "personal_finance_category": {"primary": cat}}


def store_with(tmp_path, snaps, rows):
    store = plaid.Store(tmp_path, "sandbox")
    store.save_items({"i": {"access_token": "t"}})
    store.save_ledger("i", {"accounts": {}, "transactions": {str(n): r for n, r in enumerate(rows)}})
    store.append_balances(snaps)
    return store


def onepay_snap(current, at="2026-09-20T15:00:00+00:00"):
    return {"account_id": "o", "name": "OnePay", "mask": "0001", "type": "credit", "current": current, "at": at}


def stmt(start, end, prev, new, lines=()):
    return Statement("onepay", date.fromisoformat(start), date.fromisoformat(end), prev, new, list(lines), mask="0001")


REDEEMED = Line(date(2026, 8, 5), "REWARDS STATEMENT CREDIT", -5.0, "reward credit")


def test_statement_period_adds_the_pdfs_reward_credits_the_feed_lacks(tmp_path):
    st = stmt("2026-08-01", "2026-08-28", 10.0, 105.0, [REDEEMED])
    rows = [row("2026-07-31", 999.0), row("2026-08-01", 100.0), row("2026-08-28", 0.0), row("2026-08-29", 7.0)]
    results = check.run(store_with(tmp_path, [onepay_snap(112.0)], rows), [st], None, {"0001": 0.05})
    assert results[0].ok is True and "1 of 1 statements tie" in results[0].detail
    assert results[1].ok is True  # 105 + 7 since the statement = 112


def test_a_statement_miss_names_the_pdf_line_the_feed_lacks(tmp_path):
    st = stmt("2026-08-01", "2026-08-28", 10.0, 90.0, [Line(date(2026, 8, 3), "WALMART.COM", -20.0, "refund")])
    rows = [row("2026-08-01", 100.0)]
    (r, _) = check.run(store_with(tmp_path, [onepay_snap(90.0)], rows), [st], None, {"0001": 0.05})
    assert r.ok is False and r.gap == -20.0 and "on the statement: WALMART.COM" in r.near[0]["name"]


EARNED = stmt("2026-08-01", "2026-08-28", 0.0, 100.0, [REDEEMED])
# Purchases since the 08-05 redemption: 105 + 200 = 305, at 5% = 15.25 earned.
SINCE = [row("2026-08-06", 105.0), row("2026-09-02", 200.0), row("2026-09-10", -100.0, cat="LOAN_PAYMENTS")]


def test_a_credit_gap_within_what_was_earned_is_an_inferred_reward(tmp_path):
    r = check.run(store_with(tmp_path, [onepay_snap(185.0)], SINCE), [EARNED], None, {"0001": 0.05})[1]
    assert r.ok is True and r.inferred[0]["amount"] == -15.0 and r.inferred[0]["source"] == "inferred"
    assert r.inferred[0]["date"] == "2026-09-20"


def test_a_credit_gap_past_what_was_earned_fails(tmp_path):
    r = check.run(store_with(tmp_path, [onepay_snap(170.0)], SINCE), [EARNED], None, {"0001": 0.05})[1]
    assert r.ok is False and r.gap == -30.0 and not r.inferred


def test_a_gap_in_the_debit_direction_is_never_a_reward(tmp_path):
    r = check.run(store_with(tmp_path, [onepay_snap(201.0)], SINCE), [EARNED], None, {"0001": 0.05})[1]
    assert r.ok is False and r.gap == 1.0


def bank(current, at):
    return {"account_id": "s", "name": "Checking", "mask": "2222", "type": "depository", "current": current, "at": at}


def test_bank_snapshots_subtract_money_out(tmp_path):
    snaps = [bank(500.0, "2026-09-20T15:00:00+00:00"), bank(430.0, "2026-09-22T15:00:00+00:00")]
    rows = [row("2026-09-21", 100.0, acct="s"), row("2026-09-22", -30.0, acct="s"), row("2026-09-23", 1.0, True, acct="s")]
    (r,) = check.run(store_with(tmp_path, snaps, rows))
    assert r.ok is True and "snapshot 2026-09-20 500.00" in r.detail


def test_a_row_dated_on_the_first_snapshots_day_may_already_be_in_it(tmp_path):
    snaps = [bank(500.0, "2026-09-20T15:00:00+00:00"), bank(300.0, "2026-09-22T15:00:00+00:00")]
    rows = [row("2026-09-20", 100.0, acct="s"), row("2026-09-21", 100.0, acct="s")]
    (r,) = check.run(store_with(tmp_path, snaps, rows))
    assert r.ok is True and "moved across it" in r.detail


def test_snapshots_from_one_day_are_not_a_check(tmp_path):
    snaps = [bank(400.0, "2026-09-20T15:00:00+00:00"), bank(390.0, "2026-09-20T16:00:00+00:00")]
    (r,) = check.run(store_with(tmp_path, snaps, [row("2026-09-20", 10.0, acct="s")]))
    assert r.ok is None


def test_bank_snapshot_gap_is_reported(tmp_path):
    snaps = [bank(500.0, "2026-09-20T15:00:00+00:00"), bank(380.0, "2026-09-23T15:00:00+00:00")]
    (r,) = check.run(store_with(tmp_path, snaps, [row("2026-09-21", 100.0, acct="s")]))
    assert r.ok is False and r.gap == -20.0


def test_apple_statement_ties_to_csv_rows_and_a_missing_csv_is_not_a_miss(tmp_path):
    store = store_with(tmp_path, [], [])
    aug = Statement("apple", date(2026, 8, 1), date(2026, 8, 31), 50.0, 83.91)
    jul = Statement("apple", date(2026, 7, 1), date(2026, 7, 31), 0.0, 50.0)
    rows = [{"date": "2026-08-04", "amount": 50.0, "pending": False}, {"date": "2026-08-31", "amount": -50.0},
            {"date": "2026-08-31", "amount": 33.91, "kind": "installment"}]
    results = check.run(store, [aug, jul], rows)
    assert [r.ok for r in results] == [True, None]
    assert "no CSV rows imported" in results[1].detail


def test_apple_row_is_billed_in_the_period_it_cleared_not_the_one_it_was_made_in(tmp_path):
    store = store_with(tmp_path, [], [])
    aug = Statement("apple", date(2026, 8, 1), date(2026, 8, 31), 0.0, 5.0)
    sep = Statement("apple", date(2026, 9, 1), date(2026, 9, 30), 5.0, 18.49)
    rows = [{"date": "2026-08-10", "posted": "2026-08-11", "amount": 5.0, "pending": False},
            {"date": "2026-08-31", "posted": "2026-09-02", "amount": 13.49, "pending": False}]
    (r,) = check.run(store, [aug, sep], rows)
    assert r.ok is True and "2 of 2 statements tie" in r.detail
