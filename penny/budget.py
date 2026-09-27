"""Budget vs actual (M5): a monthly amount per budget category, set in
``assumptions.toml`` ``[budget]``, against what the month spent.

Actuals are Cash flow's ``Month.spent``: spend rows by budget category, refunds
netted, plus the payments off the feed. A key is the category with spaces as
underscores (``personal_care``), since the board writes bare TOML keys. A
budget of 0 means none is set: the category is shown with its average, not
as a zero-dollar budget blown.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from .cashflow import Month


def key_of(category: str) -> str:
    return category.replace(" ", "_")


def category_of(key: str) -> str:
    return key.replace("_", " ")


@dataclass
class Line:
    category: str
    key: str | None  # its [budget] key, None when the file has none
    budget: float  # 0: none set
    actual: float
    average: float  # a month, over the full months given

    @property
    def left(self) -> float:
        return self.budget - self.actual


def lines(month: Month, full: list[Month], budget: dict) -> list[Line]:
    """One line per category that has a budget key or spending in ``month`` or
    ``full``; budgeted ones first, biggest budget first, then by spend."""
    keyed = {category_of(k): k for k in budget}
    cats = set(keyed) | set(month.spent) | {c for m in full for c in m.spent}
    n = len(full)
    out = []
    for c in cats:
        avg = sum(m.spent.get(c, 0.0) for m in full) / n if n else 0.0
        k = keyed.get(c)
        b = float(budget.get(k, 0) or 0) if k else 0.0
        out.append(Line(c, k, b, month.spent.get(c, 0.0), avg))
    out.sort(key=lambda ln: (ln.budget <= 0, -ln.budget, -ln.actual, -ln.average, ln.category))
    return [ln for ln in out if ln.key or round(ln.actual) or round(ln.average)]


def elapsed(month_key: str, today: date) -> float | None:
    """The share of ``month_key`` gone by ``today``, counting today; None
    unless it is today's month."""
    y, m = int(month_key[:4]), int(month_key[5:])
    if (today.year, today.month) != (y, m):
        return None
    return today.day / calendar.monthrange(y, m)[1]
