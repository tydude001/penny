"""Budget vs actual (M5): a monthly amount per budget category, set in
``assumptions.toml`` ``[budget]``, against what the month spent.

Actuals are Cash flow's ``Month.spent``: spend rows by budget category, refunds
netted, plus the payments off the feed. A key is the category with spaces as
underscores (``personal_care``), since the board writes bare TOML keys. A
budget of 0 means none is set: the category is shown with its average, not
as a zero-dollar budget blown.

A category is *running hot* when the month spent more than its median month
over the full months given, and the gap clears both floors in rules.toml
``[running_hot]``: a dollar amount and a share of that median (the shape is
fungible's scorecard, docs/competitors.md § fungible). The month to date is
compared as it stands, never projected: a hot flag mid-month means it has
already passed a typical whole month.
"""

from __future__ import annotations

import calendar
import statistics
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
    median: float = 0.0  # the median month over the same full months, a month with none counting as 0

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
        med = statistics.median(m.spent.get(c, 0.0) for m in full) if n else 0.0
        out.append(Line(c, k, b, month.spent.get(c, 0.0), avg, med))
    out.sort(key=lambda ln: (ln.budget <= 0, -ln.budget, -ln.actual, -ln.average, ln.category))
    return [ln for ln in out if ln.key or round(ln.actual) or round(ln.average)]


def elapsed(month_key: str, today: date) -> float | None:
    """The share of ``month_key`` gone by ``today``, counting today; None
    unless it is today's month."""
    y, m = int(month_key[:4]), int(month_key[5:])
    if (today.year, today.month) != (y, m):
        return None
    return today.day / calendar.monthrange(y, m)[1]


HOT_MIN_MONTHS = 3  # fewer full months than this and a median is one or two months, not a baseline
HOT_DEFAULTS = {"dollars": 50.0, "share": 0.25}  # as penny/defaults/rules.toml [running_hot]


def hot_floors(rules: dict) -> tuple[float, float]:
    """The dollar and share floors from ``[running_hot]``, else the defaults."""
    t = {**HOT_DEFAULTS, **rules.get("running_hot", {})}
    return float(t["dollars"]), float(t["share"])


def running_hot(ln: Line, months: int, dollars: float, share: float) -> bool:
    """Over the median month by more than ``dollars`` and by more than ``share``
    of that median; never on fewer than HOT_MIN_MONTHS full months."""
    gap = ln.actual - ln.median
    return months >= HOT_MIN_MONTHS and gap > 0 and gap >= dollars and gap >= share * ln.median
