"""Cash flow: money in and out by calendar month, from the feed (M4).

- **In** is income into the bank accounts: pay, contract work, interest, the
  rows Plaid files under INCOME. Money moved between your own accounts and
  card payments are transfers and never count.
- **Spent** is every spend row by budget category, refunds netted against it.
- **Earned** is what the cards paid back: rows in the ``earned`` category
  (reward and statement credits), credits Plaid files as a transfer into a
  card (the Edit and Chase Travel credits, ``TRANSFER_IN_OTHER_TRANSFER_IN``
  with no payment wording; a card payment or an account transfer landing on
  the card is not earned), and the reward credits OnePay's
  statement PDFs list, which its feed lacks. Points still on a card aren't
  money yet and don't count.
- A payment out of a bank account is a transfer only when it pays a card the
  feed carries (``[cashflow] feed_card_payees``): those purchases are already
  counted. Any other payment (a loan, a card that isn't linked, pay-in-4) is
  money out, under ``payments off the feed``.
- Money sent or received through PayPal, Venmo or Zelle is shown beside the
  month but not counted in it: Plaid can't see what it bought or why it came.

``net`` = in + earned − spent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .load import Txn
from .statements import Statement

EARNED = "earned"
APPS = "_FROM_APPS"  # TRANSFER_IN_TRANSFER_IN_FROM_APPS, TRANSFER_OUT_TRANSFER_OUT_FROM_APPS
INCOME_SOURCES = {"INCOME_SALARY": "pay", "INCOME_CONTRACTOR": "contract work", "INCOME_INTEREST_EARNED": "interest"}
OTHER_INCOME = "other"
OFF_FEED = "payments off the feed"
# The detailed category Plaid gave the Edit and Chase Travel credits on the
# Sapphire (2026-09). Payments and account transfers into a card land under
# LOAN_*, TRANSFER_IN_ACCOUNT_TRANSFER and the like instead.
CREDIT_CATEGORY = "TRANSFER_IN_OTHER_TRANSFER_IN"
NOT_A_CREDIT = re.compile(r"\b(payment|pymt|autopay|auto pay|thank you|transfer|xfer|ach|epay|online banking)\b", re.IGNORECASE)


def month_of(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


@dataclass
class Month:
    key: str  # "2026-08"
    income: dict[str, float] = field(default_factory=dict)  # by source: pay, contract work, interest, other
    spent: dict[str, float] = field(default_factory=dict)  # by budget category, refunds netted
    earned: float = 0.0
    apps_in: float = 0.0
    apps_out: float = 0.0
    off_feed: dict[str, float] = field(default_factory=dict)  # payee -> the payments counted under OFF_FEED

    @property
    def total_in(self) -> float:
        return sum(self.income.values())

    @property
    def total_spent(self) -> float:
        return sum(self.spent.values())

    @property
    def net(self) -> float:
        return self.total_in + self.earned - self.total_spent


def _add(d: dict[str, float], key: str, amount: float) -> None:
    d[key] = d.get(key, 0.0) + amount


def statement_credit(t: Txn) -> bool:
    """A credit Plaid files as a transfer into a card: money in on a credit
    account under ``CREDIT_CATEGORY``, and not named like a payment or a
    transfer from a bank account."""
    return (t.account_type == "credit" and t.kind == "transfer" and t.amount < 0
            and t.category == CREDIT_CATEGORY and not NOT_A_CREDIT.search(t.name))


def off_feed(t: Txn, payees: list[re.Pattern]) -> bool:
    """A payment out of a bank account to something the feed can't see into:
    a loan, a card that isn't linked, pay-in-4. It counts as spending."""
    return t.kind == "payment" and t.account_type == "depository" and t.amount > 0 and not any(p.search(t.name) for p in payees)


def build(txns: list[Txn], statements: list[Statement] = (), lacks_rewards: set[str] = frozenset(),
          feed_card_payees: list[str] = ()) -> list[Month]:
    """Every month from the first row's to the last's, oldest first. ``txns`` are the labelled feed with
    transfers kept (``feed.labelled(..., spend_only=False)``); ``statements``
    whose mask is in ``lacks_rewards`` add their reward-credit lines to earned."""
    payees = [re.compile(p, re.IGNORECASE) for p in feed_card_payees]
    months: dict[str, Month] = {}

    def at(d: date) -> Month:
        k = month_of(d)
        return months.setdefault(k, Month(k))

    for t in txns:
        m = at(t.date)
        if t.kind == "income":
            _add(m.income, INCOME_SOURCES.get(t.category, OTHER_INCOME), -t.amount)
        elif t.budget_category == EARNED:
            m.earned -= t.amount
        elif t.transfer:
            if statement_credit(t):
                m.earned -= t.amount
            elif off_feed(t, payees):
                _add(m.spent, OFF_FEED, t.amount)
                _add(m.off_feed, t.name, t.amount)
            elif t.category.endswith(APPS):
                if t.amount < 0:
                    m.apps_in -= t.amount
                else:
                    m.apps_out += t.amount
        else:
            _add(m.spent, t.budget_category, t.amount)
    for st in statements:
        if st.mask in lacks_rewards:
            for line in st.lines:
                if line.kind == "reward credit":
                    at(line.date).earned -= line.amount
    if not months:
        return []
    # A month with no rows between the first and the last is a zero month, and
    # the averages must count it.
    first, last = min(months), max(months)
    y, mo = int(first[:4]), int(first[5:])
    while (k := f"{y}-{mo:02d}") < last:
        months.setdefault(k, Month(k))
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return sorted(months.values(), key=lambda m: m.key)

