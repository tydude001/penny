"""What Costco saves you on what you buy there, measured against Walmart.

Both stores' own captures say what you paid: the Costco receipts
(``penny costco``) and the Walmart order history (``penny walmart``). What
no capture can say is which Costco item stands in for which Walmart product,
so the instance keeps that in ``costco-pairs.toml``, one ``[[pair]]`` each,
and names the perk the total answers::

    perk = "costco.prices"            # the assumptions.toml perk it measures
    skip = ["1234567"]                # Costco items with no Walmart counterpart (a
                                      #   monitor, movie tickets): out of the coverage.
                                      #   Top level, so above the first [[pair]]

    [[pair]]
    name = "Lean ground beef"
    unit = "lb"
    costco = { 88739 = 10 }           # Costco item number = units in one, or
                                      #   "by weight" for one priced a lb at the shelf
    walmart = ["824841960"]           # Walmart item ids, priced off its "$8.47/lb" label

    [[pair]]
    name = "Coke Zero"
    unit = "can"
    costco = { 891742 = 35 }
    walmart = { 13812835 = 24 }       # id = units, when Walmart shows no label
    walmart_price = 0.64              # $ a unit read off walmart.com: used when
    walmart_price_on = "2026-09-23"   #   it is newer than the newest order, or none

Walmart's price a unit is the median over the orders in the 180 days up to its
newest one, so one sale doesn't set it; a price over 90 days older than the
last receipt is flagged stale. Coverage is the share of the year's Costco
spend the priced pairs account for, and the biggest unpaired items are listed
beside it: the total is a floor until those are paired or skipped.
`penny pairs --suggest` proposes pairs for those from the two captures' item
names, for you to check and paste in.

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
STALE_DAYS = 90
MEDIAN_DAYS = 180
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
    price_on: date | None = None  # the newest Walmart price's date
    stale: bool = False  # that date is over STALE_DAYS before the last receipt

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
    total: float = 0.0  # $ a year of Costco warehouse items, skipped ones aside
    unpaired: list[tuple[costco.Item, float]] = field(default_factory=list)  # (item, $ a year), biggest first

    @property
    def coverage(self) -> float:
        """Share of ``total`` the priced pairs account for."""
        return sum(r.paid for r in self.priced) / self.total if self.total > 0 else 0.0

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
        stale = [f"{r.name} ({r.price_on:%b %-d})" for r in self.priced if r.stale]
        if stale:
            out += f" Walmart price over {STALE_DAYS} days old: {', '.join(stale)}."
        if self.total > 0:
            big = ", ".join(f"{short_name(it.name)} {_money(a)}" for it, a in self.unpaired[:3])
            out += (f" The priced pairs cover {self.coverage:.0%} of your {_money(self.total)} a year at Costco"
                    + (f"; biggest unpaired: {big}" if big else "") + ".")
        return out + " penny pairs lists every line; costco-pairs.toml says which item replaces which."

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1


def short_name(name: str) -> str:
    """A receipt description without Costco's shelf codes ("P70", "T9H5",
    "35/12OZ"): its first three plain words, title-cased."""
    words = [w for w in name.split() if not re.search(r"\d", w)]
    return " ".join(words[:3]).title() or name.strip()


def _money(x: float) -> str:
    return f"−${-x:,.0f}" if x < 0 else f"${x:,.0f}"


BY_WEIGHT = "by weight"


def _ids(v, what: str, weighed: bool = False) -> dict[str, float | str | None]:
    """``["id", ...]`` (sized by the label) or ``{id = units}``; with
    ``weighed``, a unit may also be "by weight"."""
    if isinstance(v, list):
        return {str(k): None for k in v}
    if isinstance(v, dict):
        out = {}
        for k, n in v.items():
            if weighed and n == BY_WEIGHT:
                out[str(k)] = BY_WEIGHT
                continue
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
        costco_ids = _ids(p["costco"], f"pair {i} costco", weighed=True)
        price = p.get("walmart_price")
        if price is not None and (not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0):
            raise ValueError(f"{FILE}: pair {i} walmart_price must be $ a unit, above 0")
        pairs.append({"name": str(p["name"]), "unit": str(p["unit"]).strip().lower(), "costco": costco_ids,
                      "walmart": _ids(p.get("walmart", []), f"pair {i} walmart"),
                      "walmart_price": float(price) if price is not None else None,
                      "walmart_price_on": str(p.get("walmart_price_on", ""))})
    skip = doc.get("skip", [])
    if not isinstance(skip, list):
        raise ValueError(f"{FILE}: skip must be a list of Costco item numbers")  # noqa: TRY004 -- a file error, as the rest
    return {"perk": perk, "pairs": pairs, "skip": {str(k) for k in skip},
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


def _unit_prices(lines: list[walmart.Line], ids: dict[str, float | None], unit: str) -> tuple[list[tuple[date, float]], str]:
    """Each order of ``ids`` as (day, $ a unit), and why any couldn't be read."""
    out, why = [], ""
    for ln in lines:
        if ln.item_id not in ids or ln.qty <= 0:
            continue
        size = ids[ln.item_id]
        v = ln.price / ln.qty / size if size else label_price(ln.unit_price, unit)
        if v is None:
            why = (f"its label reads {ln.unit_price!r}" if ln.unit_price else "it shows no unit price") + f"; give it as id = {unit}s in one"
        else:
            out.append((ln.date, v))
    return out, why


def _walmart_price(lines: list[walmart.Line], p: dict) -> tuple[float | None, date | None, str]:
    """$ a unit, its date, and where from: the median over the orders in
    MEDIAN_DAYS up to the newest, unless walmart_price is newer (or the only
    price there is)."""
    got, why = _unit_prices(lines, p["walmart"], p["unit"])
    on = _date(p["walmart_price_on"])
    if got:
        newest = max(d for d, _ in got)
        if not (p["walmart_price"] and on and on > newest):
            recent = sorted(v for d, v in got if (newest - d).days <= MEDIAN_DAYS)
            n = len(recent)
            mid = recent[n // 2] if n % 2 else (recent[n // 2 - 1] + recent[n // 2]) / 2
            src = f"order {newest.isoformat()}" if n == 1 else f"median of {n} orders to {newest.isoformat()}"
            return mid, newest, src
    if p["walmart_price"]:
        return p["walmart_price"], on, "walmart.com" + (f", {on.isoformat()}" if on else "")
    return None, None, (f"Walmart order: {why}" if why else "no Walmart order of it, and no walmart_price")


def _date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def measure(spec: dict, receipts: list[dict], lines: list[walmart.Line], rules: dict) -> Measure | None:
    """The saving per pair over the last 365 days of ``receipts``."""
    recs = costco.summary(receipts)
    if not recs:
        return None
    last = recs[-1].date
    first = max(recs[0].date, last - timedelta(days=364))
    scale = max(1.0, 365.0 / ((last - first).days + 1))
    ranked = costco.rank(receipts, since=first)
    items = {it.item_number: it for it in ranked}
    w_rate, _ = model.best_held_rate(rules, spec["walmart_family"])
    c_rate, _ = model.best_held_rate(rules, spec["costco_family"])
    rows = []
    for p in spec["pairs"]:
        got = [(items[k], n) for k, n in p["costco"].items() if k in items]
        bought = sum(it.weight if n == BY_WEIGHT else it.units * n for it, n in got) * scale
        paid = sum(it.spend for it, _ in got) * scale
        price, on, source = _walmart_price(lines, p)
        saving = None if price is None else bought * price * (1 - w_rate) - paid * (1 - c_rate)
        stale = bool(price is not None and on and (last - on).days > STALE_DAYS)
        rows.append(Row(p["name"], p["unit"], bought, paid, price, source, saving, on, stale))
    paired = {k for p in spec["pairs"] for k in p["costco"]}
    skip = spec.get("skip", set())
    total = sum(it.spend for it in ranked if it.item_number not in skip) * scale
    unpaired = [(it, it.spend * scale) for it in ranked if it.item_number not in paired | skip and it.spend > 0]
    return Measure(spec["perk"], rows, first, last, w_rate, c_rate, scale, total=total, unpaired=unpaired)


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


# --------------------------------------------------------------------------
# Suggestions: candidate pairs for the biggest unpaired Costco items, from
# the two captures' item names alone. They are guesses to check, never
# counted: a pair only counts once it is in the file.

_NOISE = {"ks", "kirkland", "signature", "organic", "org", "the", "and", "with", "for", "pack", "count", "ct"}
_C_SIZE = [  # Costco's abbreviated descriptions: "35/12OZ", "30/213 SHEET", "4LBS", "24OZ"
    (re.compile(r"(\d+)\s*/\s*\d+(?:\.\d+)?\s*OZ\b", re.IGNORECASE), "can", lambda m: float(m.group(1))),
    (re.compile(r"(\d+)\s*/\s*(\d+)\s*SHEET", re.IGNORECASE), "sheet", lambda m: float(m.group(1)) * float(m.group(2))),
    (re.compile(r"(\d+(?:\.\d+)?)\s*LBS?\b", re.IGNORECASE), "lb", lambda m: float(m.group(1))),
    (re.compile(r"(\d+(?:\.\d+)?)\s*OZ\b", re.IGNORECASE), "oz", lambda m: float(m.group(1))),
]
_W_SIZE = {  # Walmart's product names: "11 oz Bag", "24 Pack", "12 Mega Rolls, 220 Sheets Per Roll"
    "sheet": re.compile(r"(\d+)\s+\w*\s*Rolls?,\s*(\d+)\s+Sheets", re.IGNORECASE),
    "can": re.compile(r"(\d+)\s*(?:/\s*)?(?:Pack|pk|ct|Count)\b", re.IGNORECASE),
    "lb": re.compile(r"(\d+(?:\.\d+)?)\s*lbs?\b", re.IGNORECASE),
    "oz": re.compile(r"(\d+(?:\.\d+)?)\s*oz\b", re.IGNORECASE),
}


@dataclass
class Candidate:
    item_id: str
    name: str
    score: float  # share of the Costco item's words found in this name
    size: float | None  # units in one, read off the name; None: not readable
    labelled: bool  # Walmart's own label is per the unit: no size needed
    letters: int = 0  # letters in the words matched: a tiebreak


@dataclass
class Suggestion:
    items: list[tuple[costco.Item, float | str | None]]  # Costco items with the same best match, and each one's pack size or "by weight"
    annual: float
    unit: str | None
    candidates: list[Candidate]

    @property
    def item(self) -> costco.Item:
        return self.items[0][0]

    def toml(self) -> str:
        """A [[pair]] block to check and paste: a size penny couldn't read is
        0, which the file refuses, so nothing counts until it is filled in."""
        unit = self.unit or "?"
        c = self.candidates[0]
        packs = ", ".join(f'{it.item_number} = "{n}"' if n == BY_WEIGHT else f"{it.item_number} = {n or 0:g}" for it, n in self.items)
        lines = ["[[pair]]", f'name = "{self.item.name.strip().title()}"', f'unit = "{unit}"',
                 f"costco = {{ {packs} }}" + ("" if all(n for _, n in self.items) else "   # fill in each 0: units in one pack")]
        if c.labelled:
            lines.append(f'walmart = ["{c.item_id}"]   # {c.name[:60]}')
        else:
            lines.append(f"walmart = {{ {c.item_id} = {c.size or 0:g} }}" + ("" if c.size else "   # fill in: units in one") + f"   # {c.name[:60]}")
        lines += [f"#   or {o.item_id}: {o.name[:60]}" for o in self.candidates[1:]]
        return "\n".join(lines)


def _words(name: str) -> list[str]:
    """The name's distinct words worth matching, in order."""
    return list(dict.fromkeys(w for w in re.findall(r"[a-z]+", name.lower()) if len(w) >= 3 and w not in _NOISE))


def _same(short: str, word: str) -> bool:
    """A Costco word matching a Walmart one: the same word or its start
    ("SPARKLING", "CHOC" for chocolate), or, for an abbreviation with its
    vowels dropped ("BRST", "BNLS"), the same first letter and the rest in
    order. A word that kept its vowels is never read as an abbreviation:
    "CUT" is not "crust"."""
    if short == word or (word.startswith(short) and len(short) >= 4):
        return True
    if short[0] != word[0] or len(short) > len(word) or re.search(r"[aeiou]", short[1:]):
        return False
    it = iter(word)
    return all(ch in it for ch in short[1:])


def _score(costco_name: str, walmart_name: str) -> tuple[int, float, int]:
    """(Costco words matched, share of them, letters in them)."""
    cw, ww = _words(costco_name), _words(walmart_name)
    hits = [c for c in cw if any(_same(c, w) for w in ww)]
    return len(hits), (len(hits) / len(cw) if cw else 0.0), sum(map(len, hits))


def _costco_size(name: str) -> tuple[str | None, float | None]:
    for rx, unit, f in _C_SIZE:
        m = rx.search(name)
        if m:
            return unit, f(m)
    return None, None


def _walmart_size(name: str, unit: str | None) -> float | None:
    rx = _W_SIZE.get(unit or "")
    m = rx.search(name) if rx else None
    if not m:
        return None
    return float(m.group(1)) * float(m.group(2)) if unit == "sheet" else float(m.group(1))


def suggest(m: Measure, spec: dict, lines: list[walmart.Line], top: int = 8, cutoff: float = 0.5) -> list[Suggestion]:
    """For the ``top`` biggest unpaired Costco items, the Walmart items whose
    names share at least two words and ``cutoff`` of the Costco item's,
    best first. Costco items whose best match is the same Walmart item
    (one product, several item numbers) come back as one suggestion."""
    used = {k for p in spec["pairs"] for k in p["walmart"]}
    witems = [it for it in walmart.rank(lines) if it.item_id not in used]
    out: list[Suggestion] = []
    for item, annual in m.unpaired[:top]:
        unit, size = ("lb", BY_WEIGHT) if item.by_weight else _costco_size(item.name)
        cands = []
        for w in witems:
            hit, sc, letters = _score(item.name, w.name)
            if sc < cutoff or hit < 2:
                continue
            labelled = bool(unit and label_price(w.unit_price, unit) is not None)
            if unit is None:  # take the unit off Walmart's label when the receipt has none
                lm = _LABEL.match(w.unit_price or "")
                unit = lm.group(4).strip().lower() if lm else None
                labelled = unit is not None
            cands.append(Candidate(w.item_id, w.name, sc, _walmart_size(w.name, unit), labelled, letters))
        if not cands:
            continue
        cands.sort(key=lambda c: (-c.score, -c.letters, not c.labelled))
        same = next((sg for sg in out if sg.candidates[0].item_id == cands[0].item_id), None)
        if same:
            same.items.append((item, size))
            same.annual += annual
        else:
            out.append(Suggestion([(item, size)], annual, unit, cands[:3]))
    return out
