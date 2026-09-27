"""Walmart order line items, from scripts/walmart-orders-capture.js output.

The capture is a list of ``{url, at, body}`` responses recorded off the
Purchase History page. Only ``getOrder`` responses carry line items; the rest
(order list pages, reviews, layout) are ignored. Walmart's payload is internal
and unversioned, so everything here reads defensively and names the few paths
it relies on:

    body.data.order.{id, orderDate}
    body.data.order.groups_*[].categories[].{type, items[]}
    item.{quantity, productInfo.{usItemId, name}, priceInfo.{linePrice, unitPrice}}

Each group lists its items twice — under ``categories`` and again under
``subGroups[].categories`` — so only ``categories`` is read, falling back to
the subgroups only when a group has none. A category's ``type`` says what
happened to its items: REGULAR, WEIGHT_ADJUSTED and SUBSTITUTED were paid for
(a substitute is listed as the item received, at its price); UNAVAILABLE and
RETURNED were not, and are dropped.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PAID = {"REGULAR", "WEIGHT_ADJUSTED", "SUBSTITUTED"}


@dataclass
class Line:
    order_id: str
    date: date
    item_id: str
    name: str
    qty: float
    price: float  # line total as charged, before tax
    unit_price: str = ""  # Walmart's own label, e.g. "36.7¢/oz"; empty when absent
    status: str = "REGULAR"


@dataclass
class Item:
    item_id: str
    name: str
    orders: int = 0  # distinct orders it appeared in
    qty: float = 0.0
    spend: float = 0.0
    unit_price: str = ""  # from the most recent order
    last: date | None = None
    shelf: str = ""
    _seen: set = field(default_factory=set, repr=False)


def _orders(capture: list[dict]) -> list[dict]:
    """getOrder payloads, one per order id — the last capture wins, since
    opening an order twice records it twice."""
    by_id: dict[str, dict] = {}
    for hit in capture:
        if "/getOrder/" not in str(hit.get("url", "")):
            continue
        order = ((hit.get("body") or {}).get("data") or {}).get("order")
        if order and order.get("id"):
            by_id[order["id"]] = order
    return list(by_id.values())


def _categories(group: dict) -> list[dict]:
    cats = group.get("categories") or []
    if cats:
        return cats
    return [c for sg in group.get("subGroups") or [] for c in sg.get("categories") or []]


def _value(price: dict | None) -> float | None:
    if isinstance(price, dict) and isinstance(price.get("value"), (int, float)):
        return float(price["value"])
    return None


def lines_from_capture(capture: list[dict]) -> list[Line]:
    out: list[Line] = []
    for order in _orders(capture):
        when = date.fromisoformat(str(order.get("orderDate", ""))[:10])
        groups = [v for k, v in order.items() if k.startswith("groups") and isinstance(v, list)]
        for group in (g for gs in groups for g in gs):
            for cat in _categories(group):
                status = str(cat.get("type") or "")
                if status not in PAID:
                    continue
                for it in cat.get("items") or []:
                    pi = it.get("productInfo") or {}
                    price = _value((it.get("priceInfo") or {}).get("linePrice"))
                    if price is None or not pi.get("name"):
                        continue
                    out.append(
                        Line(
                            order_id=str(order["id"]),
                            date=when,
                            item_id=str(pi.get("usItemId") or pi["name"]),
                            name=str(pi["name"]).strip(),
                            qty=float(it.get("quantity") or 1),
                            price=price,
                            unit_price=str(((it.get("priceInfo") or {}).get("unitPrice") or {}).get("displayValue") or ""),
                            status=status,
                        )
                    )
    out.sort(key=lambda ln: ln.date)
    return out


def load_lines(path: str | Path) -> list[Line]:
    with open(path, encoding="utf-8") as f:
        return lines_from_capture(json.load(f))


def listed_orders(capture: list[dict]) -> dict[str, tuple[date, float]]:
    """Every order on the Purchase History list pages that were scrolled past,
    opened or not: id -> (date, order total incl. tax and tip). The list pages
    are what say how much of the window the opened orders cover.

        body.data.purchaseHistory.orders[].{id, orderDate, priceDetails.orderTotal}
    """
    out: dict[str, tuple[date, float]] = {}
    for hit in capture:
        if "/PurchaseHistory" not in str(hit.get("url", "")):
            continue
        ph = ((hit.get("body") or {}).get("data") or {}).get("purchaseHistory") or {}
        for o in ph.get("orders") or []:
            total = _value((o.get("priceDetails") or {}).get("orderTotal"))
            if o.get("id") and total is not None:
                out[o["id"]] = (date.fromisoformat(str(o.get("orderDate", ""))[:10]), total)
    return out


def coverage(capture: list[dict], lines: list[Line]) -> tuple[float, int, int]:
    """Share of listed spend, from the first opened order on, that was opened
    in detail -- (share, opened, listed). 1.0 when no list pages were captured,
    in which case the opened orders are all there is to go on."""
    listed = listed_orders(capture)
    opened = {ln.order_id for ln in lines}
    if not listed or not lines:
        return 1.0, len(opened), len(opened)
    first = lines[0].date
    window = {k: v for k, v in listed.items() if v[0] >= first}
    total = sum(v[1] for v in window.values())
    seen = sum(v[1] for k, v in window.items() if k in opened)
    return (seen / total if total else 1.0), len(opened & set(window)), len(window)


# A first guess at how long a pack keeps, from the product name alone. It
# decides nothing: it sorts the sheet so the fresh lines, which rarely belong
# at Costco for two people, sit apart from the ones worth pricing. Anything it
# cannot place is "?" — read the name.
_SHELF = [
    ("shelf", r"\bchips\b|\bsoda\b|crackers"),  # before "fresh": tortilla chips, grapefruit soda
    ("frozen", r"\bfrozen\b|ice cream|\bpopsicle|taquito"),
    ("fresh", (r"\bmilk\b|\beggs?\b|yogu?rt|\bcream\b|sour cream|cottage|\bdeli\b|lunch ?meat|sliced (turkey|ham)|"
     r"\bbread\b|\bbuns?\b|tortilla|bagel|\bbanana|\bapple|\bberr(y|ies)|strawberr|blueberr|\bgrapes?\b|lettuce|salad|spinach|"
     r"\bkale\b|tomato(?!.*(canned|sauce|paste|diced))|avocado|\bonion|pepper(?!oni)|cucumber|celery|carrot|broccoli|"
     r"potato|lemon|lime|orange|cilantro|parsley|\bherb|mushroom|zucchini|\bfresh\b|ground (beef|turkey)|chicken breast|"
     r"\bsalmon\b|\bshrimp\b|\bsteak\b|\bpork\b|bacon|sausage|hot dog|cheese|\bbutter\b|hummus|guacamole|juice|"
     r"refrigerat|pizza crust|salami|snap peas|iced coffee")),
    ("shelf", (r"paper towel|toilet paper|bath tissue|tissue|napkin|trash bag|garbage bag|\bfoil\b|plastic wrap|zip ?loc|"
     r"detergent|dish soap|dishwasher|cleaner|wipes|sponge|shampoo|conditioner|body wash|toothpaste|deodorant|razor|"
     r"\bsoap\b|lotion|batter(y|ies)|\bcoffee\b|\btea\b|\brice\b|pasta|spaghetti|noodle|cereal|oat|flour|sugar|\bsalt\b|"
     r"\boil\b|vinegar|canned|\bcan\b|\bbeans\b|broth|stock|soup|sauce|ketchup|mustard|mayo|dressing|peanut butter|jelly|"
     r"\bjam\b|honey|syrup|\bnuts?\b|almond|cashew|peanut|pecan|walnut|chips|crackers|pretzel|popcorn|granola|bar\b|"
     r"cookies|candy|chocolate|water|soda|sparkling|seltzer|pet food|dog food|cat food|litter|vitamin|medicine|"
     r"ibuprofen|acetaminophen|allergy|diaper|spice|seasoning|pepperoni|jerky|protein powder|sports drink")),
]
_SHELF_RE = [(k, re.compile(p, re.IGNORECASE)) for k, p in _SHELF]


def shelf_guess(name: str) -> str:
    for label, rx in _SHELF_RE:
        if rx.search(name):
            return label
    return "?"


def rank(lines: list[Line]) -> list[Item]:
    """Aggregate by product, biggest spend first."""
    items: dict[str, Item] = {}
    for ln in lines:
        it = items.setdefault(ln.item_id, Item(ln.item_id, ln.name, shelf=shelf_guess(ln.name)))
        it.qty += ln.qty
        it.spend += ln.price
        it._seen.add(ln.order_id)
        it.orders = len(it._seen)
        if it.last is None or ln.date >= it.last:
            it.last, it.name = ln.date, ln.name
            it.unit_price = ln.unit_price or it.unit_price
    return sorted(items.values(), key=lambda i: -i.spend)


SHEET_COLUMNS = [
    "rank", "item", "shelf_guess", "orders", "qty", "walmart_spend", "annual_spend",
    "walmart_unit_price", "costco_item", "costco_pack_price", "costco_unit_price", "would_you_finish_it", "notes",
]


def write_sheet(items: list[Item], path: str | Path, annualize: float, top: int) -> None:
    """The top-N items with blank Costco columns, for pricing by hand."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SHEET_COLUMNS)
        for i, it in enumerate(items[:top], 1):
            w.writerow([i, it.name, it.shelf, it.orders, f"{it.qty:g}", f"{it.spend:.2f}",
                        f"{it.spend * annualize:.0f}", it.unit_price, "", "", "", "", ""])


@dataclass
class Measured:
    """What the order history says a year of Walmart orders costs."""
    path: Path
    total: float  # order totals over the span, incl. tax and tip when listed
    first: date
    last: date
    orders: int
    listed: bool  # from the Purchase History list pages; False: opened line items only

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1

    @property
    def annual(self) -> float:
        return self.total * 365.0 / self.days


def measured(capture: list[dict], path: Path) -> Measured | None:
    """Order totals off the list pages when they were captured -- what the card
    was charged, and every order scrolled past, opened or not. Without them,
    the opened orders' pre-tax line items, scaled up by nothing."""
    listed = listed_orders(capture)
    if listed:
        days = [d for d, _ in listed.values()]
        return Measured(path, sum(t for _, t in listed.values()), min(days), max(days), len(listed), True)
    lines = lines_from_capture(capture)
    if not lines:
        return None
    return Measured(path, sum(ln.price for ln in lines), lines[0].date, lines[-1].date, len({ln.order_id for ln in lines}), False)


def newest_measured(data: Path) -> Measured | None:
    found = sorted(data.glob("walmart-orders-*.json"))
    if not found:
        return None
    with open(found[-1], encoding="utf-8") as f:
        return measured(json.load(f), found[-1])
