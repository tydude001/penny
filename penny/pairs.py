"""What Costco saves you on what you buy there, measured against Walmart.

Both stores' own captures say what you paid: the Costco receipts
(``penny costco``) and the Walmart order history (``penny walmart``). What
no capture can say is which Costco item stands in for which Walmart product,
so the instance keeps that in ``costco-pairs.toml``, one ``[[pair]]`` each,
and names the perk the total answers::

    perk = "costco.prices"            # the assumptions.toml perk it measures

    [[pair]]
    name = "Lean ground beef"
    unit = "lb"
    costco = { 88739 = 10 }           # Costco item number = units in one
    walmart = ["824841960"]           # Walmart item ids, priced off its "$8.47/lb" label

    [[pair]]
    name = "Coke Zero"
    unit = "can"
    costco = { 891742 = 35 }
    walmart = { 13812835 = 24 }       # id = units, when Walmart shows no label
    walmart_price = 0.64              # $ a unit, used only when no order of it
    walmart_price_on = "2026-09-23"   #   is in the capture: read off walmart.com

For each pair, over the last 365 days of receipts (scaled up, and said so,
when there is less):

    bought  = Costco units x units in a pack;  paid = Costco spend net of coupons
    walmart = bought x Walmart's most recent price a unit
    saving  = walmart x (1 - Walmart rate) - paid x (1 - Costco rate)

where each rate is the best held card's at that store: the reward you'd earn
buying it at Walmart instead is part of the price. The Executive reward is
not in it; the membership's verdict counts that already. A saving can be
negative: that is Costco costing more on the line.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import costco, model, walmart

FILE = "costco-pairs.toml"
WALMART_FAMILY = "walmart_delivery"
COSTCO_FAMILY = "costco"
_LABEL = re.compile(r"^\s*(\$)?\s*([\d,]*\.?\d+)\s*(¢)?\s*/\s*(.+?)\s*$")


@dataclass
class Row:
    name: str
    unit: str
    bought: float  # units a year at Costco
    paid: float  # $ a year at Costco, net of coupons
    walmart_unit: float | None  # $ a unit at Walmart; None: unpriced
    walmart_from: str  # where that price came from, or why there is none
    saving: float | None  # $ a year; None: unpriced

    @property
    def costco_unit(self) -> float | None:
        return self.paid / self.bought if self.bought else None


@dataclass
class Measure:
    perk: str  # "membership.perk", as in assumptions.toml
    rows: list[Row]
    first: date  # receipts counted, from
    last: date  # to
    walmart_rate: float
    costco_rate: float
    scale: float = 1.0  # 365 / days when under a year of receipts
    sources: list[Path] = field(default_factory=list)

    @property
    def priced(self) -> list[Row]:
        return [r for r in self.rows if r.saving is not None]

    @property
    def saving(self) -> float:
        return sum(r.saving for r in self.priced)

    @property
    def partial(self) -> bool:
        """Under a year of receipts: the total is a scaled guess."""
        return self.scale > 1.0

    def summary(self) -> str:
        """One sentence for the board: what was measured, and what wasn't."""
        top = sorted(self.priced, key=lambda r: -abs(r.saving))
        lines = ", ".join(f"{r.name} {_money(r.saving)}" for r in top[:4])
        more = f" and {len(top) - 4} more" if len(top) > 4 else ""
        out = (f"Measured from your Costco receipts through {self.last:%b %-d, %Y} against your Walmart prices: "
               f"{len(top)} item{'s' if len(top) != 1 else ''} ({lines}{more}), after the "
               f"{(self.walmart_rate - self.costco_rate) * 100:.3g}% more you'd earn on the card at Walmart.")
        if self.partial:
            out += f" Under a year of receipts, so the year is these {self.days} days x{self.scale:.2f}, a guess."
        unpriced = [r.name for r in self.rows if r.saving is None]
        if unpriced:
            out += f" Not priced: {', '.join(unpriced)}."
        return out + " penny pairs lists every line; costco-pairs.toml says which item replaces which."

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1


def _money(x: float) -> str:
    return f"−${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def _ids(v, what: str) -> dict[str, float | None]:
    """``["id", ...]`` (sized by the label) or ``{id = units}``."""
    if isinstance(v, list):
        return {str(k): None for k in v}
    if isinstance(v, dict):
        out = {}
        for k, n in v.items():
            if not isinstance(n, (int, float)) or isinstance(n, bool) or n <= 0:
                raise ValueError(f"{FILE}: {what} {k} = {n!r}: units in one must be a number above 0")
            out[str(k)] = float(n)
        return out
    raise ValueError(f"{FILE}: {what} must be a list of ids or a table of id = units")


def load(path: Path) -> dict | None:
    """The pairing file, checked; None when there is none."""
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        doc = tomllib.load(f)
    perk = doc.get("perk")
    if not isinstance(perk, str) or perk.count(".") != 1:
        raise ValueError(f'{FILE}: perk = "membership.perk" is required, e.g. "costco.prices"')
    pairs = []
    for i, p in enumerate(doc.get("pair") or [], 1):
        for k in ("name", "unit", "costco"):
            if k not in p:
                raise ValueError(f"{FILE}: pair {i} has no {k}")
        if not isinstance(p["costco"], dict):
            raise ValueError(f"{FILE}: pair {i} costco must be a table of item number = units in one pack")  # noqa: TRY004 -- a file error, as the rest
        costco_ids = _ids(p["costco"], f"pair {i} costco")
        price = p.get("walmart_price")
        if price is not None and (not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0):
            raise ValueError(f"{FILE}: pair {i} walmart_price must be $ a unit, above 0")
        pairs.append({"name": str(p["name"]), "unit": str(p["unit"]).strip().lower(), "costco": costco_ids,
                      "walmart": _ids(p.get("walmart", []), f"pair {i} walmart"),
                      "walmart_price": float(price) if price is not None else None,
                      "walmart_price_on": str(p.get("walmart_price_on", ""))})
    return {"perk": perk, "pairs": pairs,
            "walmart_family": str(doc.get("walmart_family", WALMART_FAMILY)),
            "costco_family": str(doc.get("costco_family", COSTCO_FAMILY))}


def label_price(label: str, unit: str) -> float | None:
    """Walmart's own unit price, e.g. "$8.47/lb" or "42.7¢/oz", in $ a
    ``unit``; None when it is per something else or unreadable."""
    m = _LABEL.match(label or "")
    if not m or m.group(4).strip().lower() != unit:
        return None
    v = float(m.group(2).replace(",", ""))
    return v / 100 if m.group(3) else v


def _walmart_price(lines: list[walmart.Line], ids: dict[str, float | None], unit: str) -> tuple[float | None, str]:
    """The most recent order of any of ``ids``: $ a unit, and where from."""
    mine = [ln for ln in lines if ln.item_id in ids and ln.qty > 0]
    if not mine:
        return None, ""
    ln = max(mine, key=lambda x: x.date)  # lines are date-sorted, so ties keep the last
    size = ids[ln.item_id]
    if size:
        return ln.price / ln.qty / size, f"order {ln.date.isoformat()}"
    v = label_price(ln.unit_price, unit)
    if v is None:
        why = f"its label reads {ln.unit_price!r}" if ln.unit_price else "it shows no unit price"
        return None, f"order {ln.date.isoformat()}: {why}; give it as id = {unit}s in one"
    return v, f"order {ln.date.isoformat()}"


def measure(spec: dict, receipts: list[dict], lines: list[walmart.Line], rules: dict) -> Measure | None:
    """The saving per pair over the last 365 days of ``receipts``."""
    recs = costco.summary(receipts)
    if not recs:
        return None
    last = recs[-1].date
    first = max(recs[0].date, last - timedelta(days=364))
    scale = max(1.0, 365.0 / ((last - first).days + 1))
    items = {it.item_number: it for it in costco.rank(receipts, since=first)}
    w_rate, _ = model.best_held_rate(rules, spec["walmart_family"])
    c_rate, _ = model.best_held_rate(rules, spec["costco_family"])
    rows = []
    for p in spec["pairs"]:
        got = [(items[k], n) for k, n in p["costco"].items() if k in items]
        bought = sum(it.units * n for it, n in got) * scale
        paid = sum(it.spend for it, _ in got) * scale
        price, source = _walmart_price(lines, p["walmart"], p["unit"])
        if price is None and p["walmart_price"]:
            price = p["walmart_price"]
            source = "walmart.com" + (f", {p['walmart_price_on']}" if p["walmart_price_on"] else "")
        elif price is None and not source:
            source = "no Walmart order of it, and no walmart_price"
        saving = None if price is None else bought * price * (1 - w_rate) - paid * (1 - c_rate)
        rows.append(Row(p["name"], p["unit"], bought, paid, price, source, saving))
    return Measure(spec["perk"], rows, first, last, w_rate, c_rate, scale)


def _newest(data: Path, pattern: str) -> Path | None:
    found = sorted(data.glob(pattern))
    return found[-1] if found else None


def for_instance(root: Path, rules: dict) -> Measure | None:
    """The instance's pairing file against its newest captures; None when
    it has no pairing file or no receipts."""
    spec = load(root / FILE)
    if spec is None:
        return None
    data = root / "data"
    rpath, wpath = _newest(data, "costco-receipts-*.json"), _newest(data, "walmart-orders-*.json")
    if rpath is None:
        return None
    with open(rpath, encoding="utf-8") as f:
        receipts = costco.receipts(json.load(f))
    lines = walmart.load_lines(wpath) if wpath else []
    m = measure(spec, receipts, lines, rules)
    if m:
        m.sources = [p for p in (rpath, wpath) if p]
    return m
