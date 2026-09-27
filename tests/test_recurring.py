"""Recurring charges (budget-plan M5): the detector on synthetic rows, and the
page on the fixture feed."""

import re
from datetime import date, timedelta

from test_board_pages import TODAY, _get, server  # noqa: F401  (the fixture)

from penny import recurring
from penny.load import Txn

D0 = date(2026, 1, 3)


def row(name, amount, d, **kw):
    return Txn(d, name, amount, "", description=name, **kw)


def monthly(name, amount, n, start=D0, step=30):
    return [row(name, amount, start + timedelta(days=step * i)) for i in range(n)]


def test_a_monthly_bill_with_a_price_rise_is_one_line():
    rows = monthly("Visible", 25.0, 5) + [row("Visible", 27.0, D0 + timedelta(days=150))]
    (r,) = recurring.find(rows, D0 + timedelta(days=160))
    assert (r.cadence, r.every, len(r.rows), r.amount, r.before, r.stopped) == ("monthly", 30, 6, 27.0, 25.0, False)
    assert r.yearly == 27.0 * 12 and r.next_due == D0 + timedelta(days=180)


def test_it_stops_when_half_an_interval_overdue():
    rows = monthly("Spotify", 11.99, 4)
    last = D0 + timedelta(days=90)
    assert not recurring.find(rows, last + timedelta(days=30 + 15))[0].stopped
    assert recurring.find(rows, last + timedelta(days=30 + 16))[0].stopped


def test_two_rows_are_not_enough_and_a_same_day_split_is_one_charge():
    assert recurring.find(monthly("Gym", 10.0, 2), D0) == []
    rows = monthly("Gym", 10.0, 2) + [row("Gym", 10.0, D0 + timedelta(days=30))]
    assert recurring.find(rows, D0) == []  # three rows, two days


def test_a_restaurant_visited_often_is_not_recurring():
    # every sixth visit happens to be the same price a month apart; the rest vary
    regular = monthly("Chipotle", 12.5, 4)
    visits = [row("Chipotle", 5.0 + i * 0.9, D0 + timedelta(days=3 + 5 * i)) for i in range(20)]
    assert recurring.find(regular + visits, D0 + timedelta(days=95)) == []


def test_two_subscriptions_at_one_merchant_both_count():
    rows = monthly("Apple Services", 2.99, 6) + monthly("Apple Services", 10.99, 6, start=D0 + timedelta(days=9))
    found = recurring.find(rows, D0 + timedelta(days=160))
    assert sorted(r.amount for r in found) == [2.99, 10.99]


def test_refunds_and_uncounted_rows_are_left_out():
    rows = monthly("Visible", 25.0, 4) + [row("Visible", -25.0, D0 + timedelta(days=1))]
    assert len(recurring.find(rows, D0)[0].rows) == 4
    assert recurring.find(rows, D0, counts=lambda t: False) == []


def test_merchant_key_drops_store_numbers():
    a = row("x", 1, D0)
    a.description = "WALGREENS #1234"
    b = row("x", 1, D0)
    b.description = "Walgreens  #99"
    assert recurring.merchant_key(a) == recurring.merchant_key(b) == "walgreens"


def test_page_lists_the_fixture_membership_fee(server):  # noqa: F811
    _, _, c = server
    status, page = _get(c, "/recurring")
    assert status == 200
    assert re.findall(r"<a href='([^']*)' class=on aria-current=page>", page) == ["/recurring"] * 2  # the sidebar and the Spending switcher
    assert "Nothing recurring found." in page  # fifteen rows in one month
    fees = page.split("Fees and the credits against them", 1)[1]
    assert "Amazon Prime Membership" in fees and "membership fee" in fees and "WALMART+ ANNUAL" in fees
