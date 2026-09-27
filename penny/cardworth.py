"""Is a fee card worth carrying? (docs/sapphire-valuation.md § Likely repo changes, 2-3.)

Two pieces, both kept apart from the membership ``net`` in ``model``:

* **Credit detection** — per anniversary year, the annual fee charged and each
  statement credit, matched by descriptor patterns in rules.toml
  ``[card_credits.<card>]``. Never by category: Rocket Money files these credits
  under ``Credit Card Payment`` and ``Internal Transfers``, which
  ``exclude_categories`` drops at load, so :func:`load_for_credits` re-reads the
  export with nothing excluded. A credit is the negated sum of its rows, so a
  clawback (The Edit's $250 charged back after a cancelled booking) nets
  against the credit it reverses.

* **Card worth** — for each card in assumptions.toml ``[card_worth].alternatives``,
  on the spend the *first* one actually carries (its ``accounts`` in rules.toml)::

      worth = earn + benefits - annual_fee

  at low / base / high. ``earn`` values a points card at its own ¢/pt range
  (``[point_value.<card>]``, base = its ``default_rate``); a card without a range
  earns the same at every level. ``benefits`` are the hand values in
  ``[card_worth.benefits.<card>.*]``, each ``verified = false`` until you
  check it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cache
from pathlib import Path

from . import load as ld
from .load import Txn
from .model import LEVELS, card_rate, scaled_card, spend_by_family

# ---------------------------------------------------------------------------
# Credit detection


def load_for_credits(path: str | Path, rules: dict) -> list[Txn]:
    """The export with no category excluded and ignored rows kept: a credit is
    real money whatever Rocket Money filed it under. Cross-account dedup stays
    on (a re-issued card is imported twice for the overlap)."""
    ex = rules.get("export", {})
    return ld.load(path, expense_sign=ex.get("expense_sign", "positive"), drop_ignored=False, exclude_categories=[])


@cache
def _pat(p: str) -> re.Pattern:
    return re.compile(p, re.IGNORECASE)


def _pats(ps: list[str]) -> list[re.Pattern]:
    return [_pat(p) for p in ps]


def _hit(t: Txn, pats: list[re.Pattern]) -> bool:
    text = t.match_text
    return any(p.search(text) for p in pats)


def credit_patterns(spec: dict) -> list[re.Pattern]:
    """Every fee and credit pattern of one ``[card_credits.<card>]`` table."""
    pats = _pats(spec.get("fee_patterns", []))
    for c in spec.get("credits", {}).values():
        pats += _pats(c.get("patterns", []))
    return pats


def is_credit_row(t: Txn, spec: dict, pats: list[re.Pattern] | None = None) -> bool:
    """A fee or credit row under ``spec`` (one ``[card_credits.<card>]`` table).
    Pass ``pats`` (``credit_patterns(spec)``) when testing many rows."""
    return _hit(t, credit_patterns(spec) if pats is None else pats)


def year_start(d: date, mmdd: str) -> date:
    """Start of the anniversary year holding ``d``; years start on ``mmdd``."""
    m, dd = (int(x) for x in mmdd.split("-"))
    s = date(d.year, m, dd)
    return s if d >= s else date(d.year - 1, m, dd)


def _next_start(s: date) -> date:
    return date(s.year + 1, s.month, s.day)


@dataclass
class AnnYear:
    start: date
    end: date
    fee: float = 0.0
    credits: dict[str, float] = field(default_factory=dict)
    partial: str | None = None  # why the data doesn't cover the whole year


def card_accounts(rules: dict, ckey: str) -> list[str] | None:
    """The card's own ``accounts`` (last four digits), or None when it names none."""
    card = rules.get("cards", {}).get(ckey, {})
    return [str(a) for a in card["accounts"]] if "accounts" in card else None


def _on_card(t: Txn, accounts: list[str] | None) -> bool:
    return accounts is None or t.account_number in accounts


def anniversary_years(txns: list[Txn], rules: dict, ckey: str, *, first: date, last: date) -> list[AnnYear]:
    """Every anniversary year overlapping ``first``–``last`` (the export's span),
    with the fee and each credit summed over the card's own accounts."""
    spec = rules["card_credits"][ckey]
    mmdd = spec.get("year_starts", "01-01")
    accounts = card_accounts(rules, ckey)
    fee_pats = _pats(spec.get("fee_patterns", []))
    credit_pats = {k: _pats(c.get("patterns", [])) for k, c in spec.get("credits", {}).items()}

    years: list[AnnYear] = []
    s = year_start(first, mmdd)
    while s <= last:
        n = _next_start(s)
        y = AnnYear(s, n - timedelta(days=1), credits={k: 0.0 for k in credit_pats})
        if first > y.start:
            y.partial = f"data starts {first}"
        elif last < y.end:
            y.partial = f"to date, data ends {last}"
        years.append(y)
        s = n
    by_start = {y.start: y for y in years}
    for t in txns:
        if not (first <= t.date <= last) or not _on_card(t, accounts):
            continue
        y = by_start.get(year_start(t.date, mmdd))
        if y is None:
            continue
        if _hit(t, fee_pats):
            y.fee += t.amount
            continue
        for k, pats in credit_pats.items():
            if _hit(t, pats):
                y.credits[k] -= t.amount  # credits arrive as money in
                break
    return years


# ---------------------------------------------------------------------------
# Card worth


def card_first_row(txns: list[Txn], rules: dict, ckey: str) -> date | None:
    """Date of the first row on the card's own accounts. Coverage starts there:
    an anniversary year before it is unseen, not unused."""
    accounts = card_accounts(rules, ckey)
    own = [t.date for t in txns if _on_card(t, accounts)]
    return min(own) if own else None


def worth_spend(txns: list[Txn], rules: dict, ckey: str) -> dict[str, float]:
    """Spend by family on the card's own ``accounts`` (all spend if it names
    none), leaving out its fee and credit rows, which are not purchases."""
    accounts = card_accounts(rules, ckey)
    spec = rules.get("card_credits", {}).get(ckey)
    pats = credit_patterns(spec) if spec else []
    keep = [t for t in txns if _on_card(t, accounts) and not _hit(t, pats)]
    return spend_by_family(keep)


def card_point_values(rules: dict, assumptions: dict, ckey: str) -> dict[str, float]:
    """A card's $/pt at low / base / high: base is its ``default_rate``, low/high
    from ``[point_value.<card>]``; a card with no range is the same at all three."""
    ckey = rules["cards"][ckey].get("points_of", ckey)  # pooled points are the pool card's
    base = float(rules["cards"][ckey].get("default_rate", 0.0))
    rng = assumptions.get("point_value", {}).get(ckey, {})
    return {"low": float(rng.get("low", base)), "base": base, "high": float(rng.get("high", base))}


def benefits(assumptions: dict, ckey: str) -> dict[str, dict]:
    b = assumptions.get("card_worth", {}).get("benefits", {}).get(ckey, {})
    return {k: v for k, v in b.items() if isinstance(v, dict)}


def unverified_benefits(assumptions: dict) -> list[str]:
    out = []
    for ckey, bs in assumptions.get("card_worth", {}).get("benefits", {}).items():
        out += [f"{ckey}.{k}" for k, v in bs.items() if isinstance(v, dict) and not v.get("verified", False)]
    return out


@dataclass
class Worth:
    card: str
    label: str
    earn: dict[str, float]
    benefits: dict[str, float]
    fee: float
    fee_set: bool
    worth: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        self.worth = {lvl: self.earn[lvl] + self.benefits[lvl] - self.fee for lvl in LEVELS}


def card_worth(rules: dict, assumptions: dict, spend: dict[str, float]) -> list[Worth]:
    """Worth of each ``[card_worth].alternatives`` card on ``spend`` (annual $)."""
    out = []
    for ckey in assumptions.get("card_worth", {}).get("alternatives", []):
        card = rules["cards"][ckey]
        pv = card_point_values(rules, assumptions, ckey)
        earn = {}
        for lvl in LEVELS:
            c = scaled_card(card, pv[lvl])
            earn[lvl] = sum(card_rate(c, fam) * amt for fam, amt in spend.items())
        ben = {lvl: sum(float(b.get(lvl, 0.0)) for b in benefits(assumptions, ckey).values()) for lvl in LEVELS}
        out.append(Worth(ckey, card.get("label", ckey), earn, ben, float(card.get("annual_fee", 0.0)), "annual_fee" in card))
    return out
