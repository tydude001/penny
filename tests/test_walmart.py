from datetime import date
from pathlib import Path

import pytest

from penny import walmart


def _item(uid, name, price, qty=1, unit=""):
    return {"quantity": qty, "productInfo": {"usItemId": uid, "name": name},
            "priceInfo": {"linePrice": {"value": price}, "unitPrice": {"displayValue": unit} if unit else None}}


def _order(oid, day, cats):
    # Walmart lists each group's items twice: under categories and subGroups.
    group = {"categories": cats, "subGroups": [{"categories": cats}]}
    return {"url": "https://www.walmart.com/orchestra/orders/graphql/getOrder/abc",
            "body": {"data": {"order": {"id": oid, "orderDate": f"{day}T10:00:00.000-0500", "groups_2101": [group]}}}}


def _listing(orders):
    return {"url": "https://www.walmart.com/orchestra/cph/graphql/PurchaseHistoryV3/xyz",
            "body": {"data": {"purchaseHistory": {"orders": [
                {"id": oid, "orderDate": f"{day}T10:00:00", "priceDetails": {"orderTotal": {"value": tot}}}
                for oid, day, tot in orders]}}}}


CAPTURE = [
    _order("o1", "2026-05-01", [
        {"type": "REGULAR", "items": [_item("1", "Great Value Large White Eggs, 12 Count", 3.0, unit="7.0¢/oz"),
                                      _item("2", "Charmin Toilet Paper 12 Mega Rolls", 15.0)]},
        {"type": "UNAVAILABLE", "items": [_item("3", "Fresh Strawberries", 4.0)]},
    ]),
    _order("o2", "2026-05-15", [
        {"type": "WEIGHT_ADJUSTED", "items": [_item("1", "Great Value Large White Eggs, 12 Count", 3.5)]},
        {"type": "SUBSTITUTED", "items": [_item("4", "Santitas Tortilla Chips", 4.0)]},
        {"type": "RETURNED", "items": [_item("2", "Charmin Toilet Paper 12 Mega Rolls", 15.0)]},
    ]),
    _order("o2", "2026-05-15", [  # opened twice: counted once
        {"type": "WEIGHT_ADJUSTED", "items": [_item("1", "Great Value Large White Eggs, 12 Count", 3.5)]},
        {"type": "SUBSTITUTED", "items": [_item("4", "Santitas Tortilla Chips", 4.0)]},
    ]),
    {"url": "https://www.walmart.com/orchestra/orders/graphql/multiReviews/q", "body": {"data": {}}},
]


def test_paid_lines_only_and_no_double_count():
    lines = walmart.lines_from_capture(CAPTURE)
    # o1: eggs + TP (strawberries unavailable); o2 last capture: eggs + chips
    assert [(ln.order_id, ln.item_id) for ln in lines] == [("o1", "1"), ("o1", "2"), ("o2", "1"), ("o2", "4")]
    assert lines[0].date == date(2026, 5, 1)
    assert lines[0].unit_price == "7.0¢/oz"


def test_rank_aggregates_by_product():
    items = walmart.rank(walmart.lines_from_capture(CAPTURE))
    assert items[0].item_id == "2" and items[0].spend == 15.0
    eggs = next(i for i in items if i.item_id == "1")
    assert eggs.orders == 2 and eggs.qty == 2 and eggs.spend == pytest.approx(6.5)
    assert eggs.unit_price == "7.0¢/oz"  # the later order had none, so the last known one is kept


def test_shelf_guess():
    assert walmart.shelf_guess("Great Value Large White Eggs, 12 Count") == "fresh"
    assert walmart.shelf_guess("Santitas White Corn Tortilla Chips, 11 oz") == "shelf"
    assert walmart.shelf_guess("Squirt Zero Sugar Grapefruit Soda Pop") == "shelf"
    assert walmart.shelf_guess("Great Value Beef Taquitos") == "frozen"
    assert walmart.shelf_guess("Charmin Toilet Paper 12 Mega Rolls") == "shelf"
    assert walmart.shelf_guess("Mystery Product") == "?"


def test_coverage_counts_skipped_orders():
    lines = walmart.lines_from_capture(CAPTURE)
    listing = _listing([("o0", "2026-04-20", 50.0), ("o1", "2026-05-01", 30.0),
                        ("skip", "2026-05-08", 20.0), ("o2", "2026-05-15", 50.0)])
    share, opened, listed = walmart.coverage(CAPTURE + [listing], lines)
    # o0 predates the first opened order, so it is outside the window
    assert (opened, listed) == (2, 3)
    assert share == pytest.approx(80 / 100)
    assert walmart.coverage(CAPTURE, lines) == (1.0, 2, 2)


def test_sheet_has_blank_costco_columns(tmp_path):
    items = walmart.rank(walmart.lines_from_capture(CAPTURE))
    out = tmp_path / "sheet.csv"
    walmart.write_sheet(items, out, annualize=2.0, top=2)
    rows = out.read_text(encoding="utf-8").splitlines()
    assert rows[0].split(",") == walmart.SHEET_COLUMNS
    assert len(rows) == 3 and rows[1].startswith("1,Charmin") and rows[1].endswith(",30,,,,,,")


def test_measured_prefers_listed_order_totals():
    capture = CAPTURE + [_listing([("o1", "2026-05-01", 30.0), ("o2", "2026-05-15", 9.0), ("o3", "2026-05-10", 21.0)])]
    m = walmart.measured(capture, Path("walmart-orders-x.json"))
    assert m.listed and m.orders == 3 and m.total == 60.0
    assert (m.first, m.last, m.days) == (date(2026, 5, 1), date(2026, 5, 15), 15)
    assert m.annual == pytest.approx(60.0 * 365 / 15)


def test_measured_falls_back_to_opened_line_items():
    m = walmart.measured(CAPTURE, Path("walmart-orders-x.json"))
    assert not m.listed and m.orders == 2 and m.total == pytest.approx(3.0 + 15.0 + 3.5 + 4.0)
    assert walmart.measured([], Path("x")) is None
