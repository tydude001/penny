from datetime import date

import pytest

from penny import costco


def _line(num, d1, amount, unit=1, d2="", price=None):
    return {"itemNumber": num, "itemDescription01": d1, "itemDescription02": d2, "unit": unit,
            "amount": amount, "itemUnitPriceAmount": amount if price is None else price}


def _receipt(code, day, lines, kind="In-Warehouse", total=None):
    sub = sum(ln["amount"] for ln in lines)
    return {"url": "https://ecom-api.costco.com/ebusiness/order/v1/orders/graphql",
            "body": {"data": {"receiptsWithCounts": {"receipts": [{
                "transactionBarcode": code, "transactionDate": day, "receiptType": kind,
                "subTotal": sub, "total": sub * 1.1 if total is None else total, "itemArray": lines}]}}}}


CAPTURE = [
    _receipt("A", "2026-01-07", [
        _line("111", "KS SPARKLING", 11.99, d2="35/12OZ"),
        _line("222", "TRU FRU", 13.89),
        _line("0", "/222", -3.50, unit=-1, price=0),  # coupon on Tru Fru
    ]),
    _receipt("B", "2026-02-01", [_line("111", "KS SPARKLING", 12.49, d2="35/12OZ")]),
    _receipt("B", "2026-02-01", [_line("111", "KS SPARKLING", 12.49, d2="35/12OZ")]),  # opened twice
    _receipt("G", "2026-02-03", [_line("9", "REGULAR", 40.0)], kind="Gas Station"),
]


def test_receipts_dedupe_by_barcode():
    raw = costco.receipts(CAPTURE)
    assert [r["transactionBarcode"] for r in raw] == ["A", "B", "G"]
    recs = costco.summary(raw)
    assert [r.warehouse for r in recs] == [True, True, False]
    assert recs[0].date == date(2026, 1, 7)


def test_rank_folds_coupons_and_skips_gas():
    items = costco.rank(costco.receipts(CAPTURE))
    by = {it.item_number: it for it in items}
    assert set(by) == {"111", "222"}
    assert by["111"].trips == 2 and by["111"].units == 2
    assert by["111"].spend == pytest.approx(24.48)
    assert by["111"].shelf_price == pytest.approx(12.49)  # latest trip
    assert by["111"].name == "KS SPARKLING 35/12OZ"
    assert by["222"].spend == pytest.approx(10.39)
    assert items[0].item_number == "111"


def test_measured_sums_warehouse_pretax_over_a_span_that_includes_gas(tmp_path):
    m = costco.measured(CAPTURE, tmp_path / "costco-receipts-x.json")
    assert m.receipts == 2 and (m.first, m.last) == (date(2026, 1, 7), date(2026, 2, 3))
    assert m.pretax == pytest.approx(11.99 + 13.89 - 3.50 + 12.49)
    assert m.days == 28 and m.annual == pytest.approx(m.pretax * 365 / 28)
    assert costco.newest_measured(tmp_path) is None
