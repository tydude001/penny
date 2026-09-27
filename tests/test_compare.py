"""compare.py: Rocket Money vs the feed, by family, account and merchant. Synthetic rows."""

from datetime import date

import pytest

from penny import compare
from penny.load import Txn


def _t(name, amount, family=None, acct="", source="plaid", account=""):
    t = Txn(date(2026, 9, 1), name, amount, "", account=account, account_number=acct, source=source)
    t.family = family
    return t


def test_by_family_keeps_negatives_like_by_account():
    rows = [_t("Store", 50.0, "costco", "1111"), _t("Store refund", -80.0, "costco", "1111"), _t("Gas", 30.0, "gas", "2222")]
    assert compare.by_family(rows) == {"costco": pytest.approx(-30.0), "gas": 30.0}
    assert compare.by_account(rows) == {"1111": pytest.approx(-30.0), "2222": 30.0}


def test_by_family_puts_unlabelled_rows_in_other():
    assert compare.by_family([_t("x", 5.0)]) == {"other": 5.0}


def test_by_account_keys_apple_and_nameless_accounts():
    rows = [_t("a", 10.0, source="apple_csv"), _t("b", 5.0, account="Savings"), _t("c", 1.0)]
    assert compare.by_account(rows) == {compare.APPLE: 10.0, "Savings": 5.0, "?": 1.0}


def test_merchant_key_is_the_first_word():
    assert compare.merchant_key(_t("CHIPOTLE MEX GR ONLINE", 1.0)) == "chipotle"
    assert compare.merchant_key(_t("Chipotle Mexican Grill", 1.0)) == "chipotle"
    assert compare.merchant_key(_t("WAL-MART #1234", 1.0)) == "walmart"
    assert compare.merchant_key(_t("#123", 1.0)) == "?"


def test_by_merchant_only_counts_the_family():
    rows = [_t("Walmart", 10.0, "walmart_store"), _t("WAL-MART #9", 5.0, "walmart_store"), _t("Kroger", 7.0, "groceries")]
    assert compare.by_merchant(rows, "walmart_store") == {"walmart": 15.0}


def test_diff_covers_both_sides_biggest_first():
    rows = compare.diff({"a": 10.0, "b": 5.0}, {"a": 12.0, "c": -20.0})
    assert rows == [("c", 0.0, -20.0, -20.0), ("b", 5.0, 0.0, -5.0), ("a", 10.0, 12.0, 2.0)]
