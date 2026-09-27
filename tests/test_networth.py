"""Net worth: newest balances, month-ends worked back from rows, and the
statement-anchored cards (Apple, and a feed that lacks reward credits)."""

from datetime import date

from feedfix import install

from penny import board, networth
from penny.statements import Line, Statement

TODAY = date(2026, 3, 10)


def _store(tmp_path):
    """The fixture ledger (checking, three cards, rows in January) with one
    day of snapshots on 2026-03-10 for checking and the Visa."""
    store = install(tmp_path)
    store.append_balances([
        {"at": "2026-03-10T12:00:00+00:00", "item_id": "fix", "account_id": "checking", "name": "Checking", "mask": "1111",
         "type": "depository", "subtype": "checking", "current": 4000.0, "available": 3900.0, "limit": None},
        {"at": "2026-03-10T12:00:00+00:00", "item_id": "fix", "account_id": "visa", "name": "Visa", "mask": "9999",
         "type": "credit", "subtype": "credit card", "current": 600.0, "available": 4400.0, "limit": 5000.0,
         "last_statement_balance": 480.0, "minimum_payment_amount": 35.0, "next_payment_due_date": "2026-03-20"},
    ])
    return store


def test_newest_balances_make_net_worth(tmp_path):
    nw = networth.build(_store(tmp_path), [], [], TODAY)
    assert [a.name for a in nw.accounts] == ["Checking", "Visa"]  # cash first, then cards
    assert (nw.assets, nw.debts, nw.net) == (4000.0, 600.0, 3400.0)
    visa = nw.accounts[1]
    assert visa.used == 0.12 and visa.due == date(2026, 3, 20) and visa.statement == 480.0


def test_month_ends_are_worked_back_from_the_rows(tmp_path):
    nw = networth.build(_store(tmp_path), [], [], TODAY)
    # The ledger starts 2026-01-05, so the first month-end is January's.
    assert [p.day for p in nw.history] == [date(2026, 1, 31), date(2026, 2, 28), TODAY]
    assert all(p.worked for p in nw.history[:-1]) and not nw.history[-1].worked
    # No rows after January: both month-ends are today's balances.
    assert nw.history[0].net == 3400.0 and nw.first_snapshot == date(2026, 3, 10)


def test_a_row_after_the_month_end_moves_it_the_right_way(tmp_path):
    store = _store(tmp_path)
    ledger = store.ledger("fix")
    ledger["transactions"]["late1"] = {"transaction_id": "late1", "account_id": "checking", "date": "2026-02-05",
                                       "amount": 250.0, "pending": False, "name": "Rent"}
    ledger["transactions"]["late2"] = {"transaction_id": "late2", "account_id": "visa", "date": "2026-02-06",
                                       "amount": 100.0, "pending": False, "name": "Shop"}
    ledger["transactions"]["pend"] = {"transaction_id": "pend", "account_id": "visa", "date": "2026-02-07",
                                      "amount": 999.0, "pending": True, "name": "Pending"}
    store.save_ledger("fix", ledger)
    jan = networth.build(store, [], [], TODAY).history[0]
    # Money out of checking after Jan 31 lowered it, so it held more then;
    # a purchase on the card raised it, so it owed less. Pending rows don't count.
    assert (jan.assets, jan.debts) == (4250.0, 500.0)


def _apple(end: date, new: float) -> Statement:
    return Statement("apple", end.replace(day=1), end, 0.0, new)


def test_apple_card_is_its_statement_plus_the_wallet_rows_since(tmp_path):
    sts = [_apple(date(2026, 1, 31), 300.0), _apple(date(2026, 2, 28), 200.0)]
    rows = [{"date": "2026-03-02", "posted": "2026-03-03", "amount": 50.0, "pending": False},
            {"date": "2026-03-04", "posted": None, "amount": 70.0, "pending": True}]
    nw = networth.build(_store(tmp_path), sts, rows, TODAY)
    apple = next(a for a in nw.accounts if a.key == networth.APPLE_KEY)
    assert (apple.balance, apple.as_of, apple.statement) == (250.0, date(2026, 3, 3), 200.0)
    assert [p.debts for p in nw.history] == [900.0, 800.0, 850.0]  # the Visa's 600 plus each month's statement


def test_a_feed_without_reward_credits_takes_them_from_its_statements(tmp_path):
    # The Visa's feed lacks reward credits; its PDF shows a 25.00 credit in February.
    st = Statement("onepay", date(2026, 1, 1), date(2026, 1, 31), 0.0, 700.0, mask="9999")
    feb = Statement("onepay", date(2026, 2, 1), date(2026, 2, 28), 700.0, 600.0, mask="9999",
                    lines=[Line(date(2026, 2, 10), "Rewards redeemed", -25.0, "reward credit")])
    nw = networth.build(_store(tmp_path), [st, feb], [], TODAY, lacks_rewards=frozenset({"9999"}))
    assert [p.debts for p in nw.history[:2]] == [700.0, 600.0]  # each month-end is its own statement


def test_chart_goes_down_from_zero_and_the_balance_line(tmp_path):
    nw = networth.build(_store(tmp_path), [], [], TODAY)
    chart = board._nw_chart(nw.history, TODAY)
    assert chart.count("<span class='c") == 3 and "<span>Now</span>" in chart and "class=zero" not in chart
    neg = [networth.Point(date(2026, 1, 31), 100.0, 300.0, True), networth.Point(TODAY, 500.0, 100.0, False)]
    chart = board._nw_chart(neg, TODAY)
    assert "<i class='over'" in chart and "<i class='in'" in chart and "class=zero style='bottom:33.3%'" in chart
    line = board._balance_line(nw.accounts[1], TODAY)
    assert "12% of $5,000 limit" in line and "$35 minimum due Mar 20" in line and "as of today" in line
