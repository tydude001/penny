"""Recurring charges (M5): the same merchant at a similar amount on a regular
interval, three times or more.

Rows are spend rows out (refunds and credits aside) plus the payments off the
feed, as Cash flow counts them. They group by a cleaned merchant name, then by
amount: a row joins a run when it is within ``AMOUNT_TOL`` of the run's
smallest, so a price rise stays one run and a $5 and a $50 charge at one
merchant don't. A run is recurring when it has ``MIN_ROWS`` or more rows and
its typical gap fits a cadence, with most gaps near it, and it is at least
half of the merchant's rows over its span (a restaurant visited weekly can
line up with some interval by chance). It has stopped when
the next charge is more than half an interval overdue.
"""

from __future__ import annotations

import itertools
import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from .load import Txn

MIN_ROWS = 3
AMOUNT_TOL = 0.15  # a price rise up to this share stays one run
DOMINANT = 0.5  # the run's share of the merchant's rows over its span
CADENCES = (  # name, typical days, the band a median gap may fall in, times a year
    ("weekly", 7, (6, 8), 52.0),
    ("every two weeks", 14, (13, 16), 26.0),
    ("monthly", 30, (27, 34), 12.0),
    ("every two months", 61, (56, 66), 6.0),
    ("quarterly", 91, (84, 99), 4.0),
    ("twice a year", 182, (170, 195), 2.0),
    ("yearly", 365, (345, 385), 1.0),
)
_NOISE = re.compile(r"#\s*\d+|\d{4,}|[^a-z0-9 &.+']")


def merchant_key(t: Txn) -> str:
    """The merchant, lower-cased, without store numbers and reference digits."""
    return " ".join(_NOISE.sub(" ", (t.description or t.name).lower()).split())


@dataclass
class Recurring:
    merchant: str  # as the newest row names it
    key: str
    cadence: str
    every: int  # median days between charges
    per_year: float
    rows: list[Txn]  # oldest first
    amount: float  # the newest charge
    before: float | None  # the charge before it, when the price changed
    next_due: date
    stopped: bool

    @property
    def last(self) -> date:
        return self.rows[-1].date

    @property
    def yearly(self) -> float:
        return self.amount * self.per_year

    @property
    def category(self) -> str:
        return self.rows[-1].budget_category

    @property
    def family(self) -> str | None:
        return self.rows[-1].family


def _runs(rows: list[Txn]) -> list[list[Txn]]:
    out: list[list[Txn]] = []
    for t in sorted(rows, key=lambda t: t.amount):
        if out and t.amount <= out[-1][0].amount * (1 + AMOUNT_TOL) + 0.5:
            out[-1].append(t)
        else:
            out.append([t])
    return [sorted(r, key=lambda t: t.date) for r in out]


def _cadence(gaps: list[int]):
    med = statistics.median(gaps)
    for name, days, (lo, hi), per in CADENCES:
        if lo <= med <= hi:
            near = sum(1 for g in gaps if abs(g - med) <= max(3, med * 0.25))
            if near * 3 >= len(gaps) * 2:
                return name, round(med), per
    return None


def find(txns: list[Txn], today: date, counts=lambda t: True) -> list[Recurring]:
    """Every recurring run in ``txns``, active first, then by yearly cost.
    ``counts`` picks the rows that are money out (spend, or off-feed payments)."""
    groups: dict[str, list[Txn]] = {}
    for t in txns:
        if t.amount > 0 and counts(t):
            groups.setdefault(merchant_key(t), []).append(t)
    out = []
    for key, rows in groups.items():
        regular = []
        for run in _runs(rows):
            # two charges on one day are one bill split, not a cadence
            days = sorted({t.date for t in run})
            if len(days) < MIN_ROWS:
                continue
            c = _cadence([(b - a).days for a, b in itertools.pairwise(days)])
            if c:
                regular.append((run, days, c))
        for run, days, (name, every, per) in regular:
            # A shop visited often lines up with some interval by chance; a
            # bill is most of what that merchant charges while it runs. Other
            # regular runs there (a second subscription) don't count against it.
            others = {id(t) for r, _, _ in regular if r is not run for t in r}
            during = sum(1 for t in rows if days[0] <= t.date <= days[-1] and id(t) not in others)
            if len(run) < during * DOMINANT:
                continue
            last, prev = run[-1], run[-2]
            changed = abs(last.amount - prev.amount) > max(0.5, prev.amount * 0.02)
            due = last.date + timedelta(days=every)
            out.append(Recurring((last.description or last.name), key, name, every, per, run, last.amount,
                                 prev.amount if changed else None, due, today > due + timedelta(days=max(5, every // 2))))
    out.sort(key=lambda r: (r.stopped, -r.yearly if not r.stopped else -r.last.toordinal()))
    return out
