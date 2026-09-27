"""Cash flow by month: what counts as in, spent and earned, and what is left out."""

from datetime import date

import pytest

from penny import cashflow, feed
from penny.load import Txn
from penny.statements import Line, Statement


def _t(d, amount, kind="purchase", cat="dining", pfc="", acct="credit", transfer=False):
    return Txn(date=date.fromisoformat(d), name="x", amount=amount, category=pfc, kind=kind,
               budget_category=cat, account_type=acct, transfer=transfer)


def test_in_spent_earned_and_what_is_left_out():
    rows = [
        _t("2026-08-01", -3000, "income", "income", "INCOME_SALARY", "depository"),
        _t("2026-08-02", -4, "income", "income", "INCOME_INTEREST_EARNED", "depository"),
        _t("2026-08-03", 100),
        _t("2026-08-04", -20, "refund"),  # nets against dining
        _t("2026-08-05", 60, cat="groceries"),
        _t("2026-08-06", -50, "refund", "earned"),  # a statement credit
        _t("2026-08-07", -25, "transfer", "transfer", "TRANSFER_IN_OTHER_TRANSFER_IN", transfer=True),  # a card credit filed as a transfer
        _t("2026-08-08", 500, "payment", "transfer", "LOAN_PAYMENTS", "depository", True),  # a card payment: never counts
        _t("2026-08-09", 40, "transfer", "transfer", "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS", "depository", True),
        _t("2026-08-10", -15, "transfer", "transfer", "TRANSFER_IN_TRANSFER_IN_FROM_APPS", "depository", True),
        _t("2026-08-11", 200, "transfer", "transfer", "TRANSFER_OUT_SAVINGS", "depository", True),  # to savings: not spend
        _t("2026-08-12", 90, "payment", "transfer", "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT", "depository", True),  # a loan: money out
        _t("2026-09-01", 10),
    ]
    rows[7].name = "CHASE CREDIT CRD"
    rows[-2].name = "DEPT EDUCATION"
    aug, sep = cashflow.build(rows, feed_card_payees=["chase credit crd"])
    assert aug.key == "2026-08" and sep.key == "2026-09"
    assert aug.income == {"pay": 3000, "interest": 4}
    assert aug.spent == {"dining": 80, "groceries": 60, cashflow.OFF_FEED: 90}
    assert aug.off_feed == {"DEPT EDUCATION": 90}
    assert aug.earned == 75
    assert (aug.apps_in, aug.apps_out) == (15, 40)
    assert aug.net == pytest.approx(3004 + 75 - 230)


def test_a_month_with_no_rows_is_a_zero_month():
    keys = [m.key for m in cashflow.build([_t("2025-11-03", 10), _t("2026-02-01", 5)])]
    assert keys == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_onepay_statement_reward_credits_count_as_earned_only_for_a_card_whose_feed_lacks_them():
    st = Statement("onepay", date(2026, 8, 1), date(2026, 8, 31), 0, 0, mask="2001",
                   lines=[Line(date(2026, 8, 20), "REWARDS STATEMENT CREDIT", -12.5, "reward credit"),
                          Line(date(2026, 8, 21), "SHOP", 30, "purchase")])
    (m,) = cashflow.build([_t("2026-08-03", 10)], [st], {"2001"})
    assert m.earned == 12.5 and m.spent == {"dining": 10}  # the purchase is already in the feed
    (m,) = cashflow.build([_t("2026-08-03", 10)], [st], set())
    assert m.earned == 0


def test_a_card_refund_plaid_files_as_income_is_a_refund():
    t = {"name": "SOME SHOP", "amount": -30.0}
    pfc = {"primary": "INCOME", "detailed": "INCOME_CONTRACTOR"}
    assert feed._kind(t, pfc, "credit") == "refund"
    assert feed._kind(t, pfc, "depository") == "income"
