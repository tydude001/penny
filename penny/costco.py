"""Costco warehouse receipt line items, from scripts/costco-receipts-capture.js output.

The capture is a list of ``{url, at, body}`` responses from costco.com's Orders
& Purchases page. Each "View Receipt" returns one receipt; the paths relied on:

    body.data.receiptsWithCounts.receipts[].{transactionBarcode, transactionDate,
        receiptType, total, subTotal, itemArray[]}
    item.{itemNumber, itemDescription01, itemDescription02, unit, amount,
        itemUnitPriceAmount}

Gas receipts (``receiptType`` other than In-Warehouse) are kept for totals but
carry no items worth ranking. An instant-savings coupon is its own negative
line whose description is ``/`` plus the item number it applies to; it is
folded into that item so the spend is what was actually paid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

WAREHOUSE = "In-Warehouse"


@dataclass
class Receipt:
    barcode: str
    date: date
    warehouse: bool
    total: float  # incl. tax
    subtotal: float  # before tax: what the Executive 2% is paid on


@dataclass
class Item:
    item_number: str
    name: str  # Costco's two abbreviated description lines, joined
    trips: int = 0
    units: float = 0.0
    spend: float = 0.0  # net of coupons
    shelf_price: float = 0.0  # itemUnitPriceAmount from the latest trip, before coupons
    last: date | None = None
    _seen: set = field(default_factory=set, repr=False)


def receipts(capture: list[dict]) -> list[dict]:
    """Raw receipts, one per barcode -- opening a receipt twice records it twice."""
    by_code: dict[str, dict] = {}
    for hit in capture:
        rwc = ((hit.get("body") or {}).get("data") or {}).get("receiptsWithCounts") or {}
        for r in rwc.get("receipts") or []:
            if r.get("transactionBarcode"):
                by_code[r["transactionBarcode"]] = r
    return sorted(by_code.values(), key=lambda r: str(r.get("transactionDate")))


def summary(raw: list[dict]) -> list[Receipt]:
    return [Receipt(str(r["transactionBarcode"]), date.fromisoformat(str(r["transactionDate"])[:10]),
                    r.get("receiptType") == WAREHOUSE, float(r.get("total") or 0), float(r.get("subTotal") or 0))
            for r in raw]


def rank(raw: list[dict]) -> list[Item]:
    """Warehouse items by net spend, biggest first."""
    items: dict[str, Item] = {}
    for r in raw:
        if r.get("receiptType") != WAREHOUSE:
            continue
        when = date.fromisoformat(str(r["transactionDate"])[:10])
        lines = r.get("itemArray") or []
        coupons = [ln for ln in lines if str(ln.get("itemDescription01") or "").startswith("/")]
        for ln in lines:
            if ln in coupons:
                continue
            num = str(ln.get("itemNumber") or "")
            name = " ".join(s.strip() for s in (ln.get("itemDescription01"), ln.get("itemDescription02")) if s)
            it = items.setdefault(num, Item(num, name))
            it.units += float(ln.get("unit") or 0)
            it.spend += float(ln.get("amount") or 0)
            it._seen.add(r["transactionBarcode"])
            it.trips = len(it._seen)
            if it.last is None or when >= it.last:
                it.last, it.name = when, name
                it.shelf_price = float(ln.get("itemUnitPriceAmount") or 0)
        for c in coupons:
            num = str(c["itemDescription01"]).lstrip("/ ").strip()
            target = items.get(num) or items.setdefault(num, Item(num, f"coupon for item {num}"))
            target.spend += float(c.get("amount") or 0)
    return sorted(items.values(), key=lambda i: -i.spend)


@dataclass
class Measured:
    """What a receipts capture says about warehouse spend, for the board."""

    path: Path
    receipts: int  # warehouse only
    first: date
    last: date
    pretax: float  # warehouse subtotals: what the Executive 2% is paid on

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1

    @property
    def annual(self) -> float:
        return self.pretax * 365.0 / self.days


def measured(capture: list[dict], path: Path) -> Measured | None:
    recs = summary(receipts(capture))
    wh = [r for r in recs if r.warehouse]
    if not wh:
        return None
    # The span runs over gas receipts too: they are trips, and a window that
    # ended at the last warehouse run would overstate the yearly rate.
    return Measured(path, len(wh), recs[0].date, recs[-1].date, sum(r.subtotal for r in wh))


def newest_measured(data: Path) -> Measured | None:
    found = sorted(data.glob("costco-receipts-*.json"))
    if not found:
        return None
    with open(found[-1], encoding="utf-8") as f:
        return measured(json.load(f), found[-1])
