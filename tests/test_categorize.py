from pathlib import Path

from feedfix import install

from penny import categorize as cat
from penny import feed, home

INSTANCE = Path(__file__).parent / "fixtures" / "home"  # a test instance, read over penny/defaults


def _rules():
    return home.load_rules(INSTANCE / "rules.toml")


def _cat(root):
    install(root)
    txns = feed.load(root, spend_only=False)
    cat.categorize(txns, cat.build_families(_rules()))
    return {(t.name, t.amount): t.family for t in txns}


def test_specific_beats_general(tmp_path):
    fam = _cat(tmp_path)
    assert fam[("Amazon Pharmacy", 45.0)] == "amazon_pharmacy"
    assert fam[("Amazon.com", 120.0)] == "amazon"
    assert fam[("Costco Gas", 60.0)] == "costco_gas"
    assert fam[("Costco", 300.0)] == "costco"
    assert fam[("Amazon Prime", 139.0)] == cat.FEES
    assert fam[("Walmart+", 98.0)] == cat.FEES


def test_walmart_split_and_fallbacks(tmp_path):
    fam = _cat(tmp_path)
    assert fam[("Walmart.com", 200.0)] == "walmart_online"
    assert fam[("Walmart", 150.0)] == "walmart_store"
    assert fam[("Shell", 40.0)] == "gas"
    assert fam[("Torchy's Tacos", 25.0)] == "restaurants"  # category fallback
    assert fam[("Some Merchant", 75.0)] == cat.OTHER


def test_unmatched_lists_other_only(tmp_path):
    install(tmp_path)
    txns = feed.load(tmp_path)
    cat.categorize(txns, cat.build_families(_rules()))
    rows = cat.unmatched(txns)
    names = [r[0] for r in rows]
    assert "Some Merchant" in names
    assert "Amazon.com" not in names


def _txn(name, amount, account_number="", description="", category="Shopping", custom_name=""):
    from datetime import date

    from penny.load import Txn

    return Txn(date(2026, 1, 1), name, amount, category, account_number=account_number, description=description, custom_name=custom_name)


def test_fee_amounts_restrict_costco_com():
    fams = cat.build_families(_rules())
    fee = _txn("Costco", 65.0, "2002", "WWW COSTCO COM")
    order = _txn("Costco", 89.99, "2005", "WWW COSTCO COM 800-955-2292 WA")
    cat.categorize([fee, order], fams)
    assert fee.family == cat.FEES
    assert order.family == "costco"


def test_account_restricted_family():
    fams = cat.build_families(_rules())
    onepay = _txn("Walmart", 40.0, "2001", "Walmart", "Groceries")
    chase = _txn("Walmart", 40.0, "2002", "WAL-MART #1234", "Groceries")
    cat.categorize([onepay, chase], fams)
    assert onepay.family == "walmart_delivery"
    assert chase.family == "walmart_store"


def test_rxpass_is_the_five_dollar_amazon_row():
    fams = cat.build_families(_rules())
    rx = _txn("AMAZON", 5.0, "2004", "AMAZON", "Health & Wellness", custom_name="medication")
    tip = _txn("Amazon Tips*X", 5.0, "2004", "Amazon Tips*X", "Health & Wellness")
    shop = _txn("AMAZON", 23.0, "2004", "AMAZON", "Shopping")
    cat.categorize([rx, tip, shop], fams)
    assert rx.family == "rxpass"
    assert tip.family == "amazon"
    assert shop.family == "amazon"


def test_apple_is_named_merchants_only():
    fams = cat.build_families(_rules())
    store = _txn("Apple Store", 109.0, "2002", "APPLE STORE #R018")
    bill = _txn("Apple", 9.99, "2002", "APPLE.COM/BILL 866-712-7753 CA")
    booth = _txn("CTLP*APPLE PHOTO BOOTH", 10.0, "2002", "CTLP*APPLE PHOTO BOOTH", "Entertainment & Rec.")
    xfer = _txn("APPLE CASH INST XFER", 20.0, "", "APPLE CASH INST XFER", "Uncategorized")
    cat.categorize([store, bill, booth, xfer], fams)
    assert store.family == "apple"
    assert bill.family == "apple"
    assert booth.family != "apple"
    assert xfer.family != "apple"
