"""The Plaid + Apple feed (M3): rows into Txn, transfers out of spend, and the
family rules the feed's fields add (online_patterns, mccs)."""

from datetime import date
from pathlib import Path

from penny import categorize as cat
from penny import compare, feed, home, plaid
from penny.load import Txn

INSTANCE = Path(__file__).parent / "fixtures" / "home"  # a test instance, read over penny/defaults


def _rules():
    return home.load_rules(INSTANCE / "rules.toml")


def prow(tid, amt, primary, detailed, *, acct="c", authorized=None, posted="2026-09-10", pending=False,
         name="X", merchant=None, channel="in store", mcc=None):
    return {"transaction_id": tid, "account_id": acct, "amount": amt, "date": posted, "authorized_date": authorized,
            "pending": pending, "name": name, "merchant_name": merchant, "payment_channel": channel,
            "merchant_category_code": mcc, "personal_finance_category": {"primary": primary, "detailed": detailed}}


def store_with(tmp_path, rows):
    store = plaid.Store(tmp_path, "production")
    store.save_items({"i": {"access_token": "t"}})
    store.save_ledger("i", {"accounts": {"c": {"name": "CREDIT CARD", "mask": "2002"},
                                         "s": {"name": "SoFi Checking", "mask": "2009"}},
                            "transactions": {r["transaction_id"]: r for r in rows}})
    return store


def test_plaid_rows_become_txns_on_the_transaction_date_with_the_mask(tmp_path):
    rows = [prow("a", 12.5, "FOOD_AND_DRINK", "FOOD_AND_DRINK_FAST_FOOD", authorized="2026-09-08", mcc="5814"),
            prow("b", 9.0, "FOOD_AND_DRINK", "FOOD_AND_DRINK_FAST_FOOD", pending=True)]
    (t,) = feed.from_plaid(store_with(tmp_path, rows))
    assert (t.date, t.posted, t.account_number, t.category, t.mcc) == (
        date(2026, 9, 8), date(2026, 9, 10), "2002", "FOOD_AND_DRINK_FAST_FOOD", "5814")
    assert (t.kind, t.source, t.transfer) == ("purchase", "plaid", False)


def test_both_halves_of_a_card_payment_and_app_transfers_are_not_spend(tmp_path):
    rows = [prow("card", -100.0, "LOAN_DISBURSEMENTS", "LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT"),  # Chase files it here
            prow("bank", 100.0, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", acct="s"),
            prow("paypal", 40.0, "TRANSFER_OUT", "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS", acct="s"),
            prow("pay", -900.0, "INCOME", "INCOME_SALARY", acct="s"),
            prow("refund", -5.0, "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_SUPERSTORES")]
    txns = {t.txn_id: t for t in feed.from_plaid(store_with(tmp_path, rows))}
    assert [txns[k].kind for k in ("card", "bank", "paypal", "pay", "refund")] == [
        "payment", "payment", "transfer", "income", "refund"]
    assert [k for k, t in txns.items() if feed.is_spend(t)] == ["refund"]


def test_apple_rows_join_the_feed_and_payments_are_transfers(tmp_path):
    d = tmp_path / "data" / "apple"
    d.mkdir(parents=True)
    (d / "Apple Card Transactions - August 2026.csv").write_text(
        "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\n"
        "08/31/2026,09/02/2026,APPLE.COM/BILL,Apple Services,Other,Purchase,13.49,Alex\n"
        "08/25/2026,08/25/2026,ACH DEPOSIT,Payment,Payment,Payment,-50.00,Alex\n")
    plaid.Store(tmp_path, "production").save_items({})
    txns = feed.load(tmp_path, spend_only=False)
    assert [(t.kind, t.transfer, t.account, t.category) for t in txns] == [
        ("payment", True, feed.APPLE_ACCOUNT, ""), ("purchase", False, feed.APPLE_ACCOUNT, "")]
    assert [t.amount for t in feed.load(tmp_path)] == [13.49]


def _txn(name, amount, **kw):
    return Txn(date(2026, 9, 1), name, amount, kw.pop("category", ""), **kw)


def test_a_plaid_walmart_row_is_online_only_when_its_channel_says_so():
    fams = cat.build_families(_rules())
    online = _txn("Walmart", 72.0, description="Walmart", account_number="2002", channel="online", mcc="5310")
    store = _txn("Walmart", 63.0, description="Walmart", account_number="2002", channel="in store", mcc="5411")
    cat.categorize([online, store], fams)
    assert (online.family, store.family) == ("walmart_online", "walmart_store")


def test_the_merchant_code_decides_over_plaids_category():
    fams = cat.build_families(_rules())
    bar = _txn("Tavern", 30.0, category="FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR", mcc="5813")
    liquor = _txn("Liquor Barn", 30.0, category="FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR", mcc="5921")
    no_code = _txn("Diner", 20.0, category="FOOD_AND_DRINK_RESTAURANT")
    cat.categorize([bar, liquor, no_code], fams)
    assert (bar.family, liquor.family, no_code.family) == ("restaurants", cat.OTHER, "restaurants")


def test_plaids_plain_costco_fee_row_is_a_fee_only_at_a_fee_amount():
    fams = cat.build_families(_rules())
    fee = _txn("Costco", 65.0, description="Costco", mcc="5300")
    trip = _txn("Costco", 212.4, description="Costco", mcc="5300")
    cat.categorize([fee, trip], fams)
    assert (fee.family, trip.family) == (cat.FEES, "costco")


def test_parkmobile_is_not_mobil_gas():
    fams = cat.build_families(_rules())
    park = _txn("Parkmobile", 12.0, category="TRANSPORTATION_PARKING", mcc="7523")
    cat.categorize([park], fams)
    assert park.family == cat.OTHER


def test_compare_diff_orders_by_the_biggest_gap():
    rows = compare.diff({"a": 10.0, "b": 5.0}, {"a": 11.0, "c": 7.0})
    assert [(k, d) for k, _, _, d in rows] == [("c", 7.0), ("b", -5.0), ("a", 1.0)]


def test_the_board_runs_on_the_feed_when_started_without_an_export(tmp_path):
    import shutil

    from penny import board
    from tests.test_board import ASSUMPTIONS

    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "assumptions.toml").write_text(ASSUMPTIONS)
    store_with(tmp_path, [
        prow("dinner", 40.0, "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", mcc="5812", posted="2026-09-01"),
        prow("walmart", 60.0, "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_SUPERSTORES", name="Walmart",
             merchant="Walmart", channel="online", mcc="5310", posted="2026-09-02"),
        prow("paid", -100.0, "LOAN_DISBURSEMENTS", "LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT", posted="2026-09-03"),
    ])
    cfg = board.Config(tmp_path, tmp_path / "rules.toml", tmp_path / "assumptions.toml", None, None)
    s = board.load_state(cfg, date(2026, 9, 13))
    assert s.export is None and s.n_txns == 2  # the card payment is not spend
    assert s.spend == {"restaurants": 40.0, "walmart_online": 60.0}
    assert "Plaid + Apple Card" in board.render_rail(s)


def _cats(*txns):
    rules = _rules()
    cat.assign_categories(cat.categorize(list(txns), cat.build_families(rules)), rules)
    return [t.budget_category for t in txns]


def test_budget_category_first_answer_wins():
    assert _cats(
        _txn("Payment Thank You", -100.0, kind="payment", transfer=True),
        _txn("Daily Cash Adjustment", -0.4, kind="reward credit"),
        _txn("TRAVEL CREDIT $300/YEAR", -20.0, category="OTHER_OTHER", kind="refund"),  # a rule
        _txn("Costco", 150.0, description="Costco", mcc="5300"),  # the family's default
        _txn("Lyft", 18.0, category="TRANSPORTATION_TAXIS_AND_RIDE_SHARES"),  # Plaid primary
        _txn("Geico", 90.0, category="GENERAL_SERVICES_INSURANCE"),  # Plaid detailed beats primary
        _txn("Mystery", 10.0),
    ) == ["transfer", "earned", "earned", "groceries", "transport", "insurance", cat.UNCATEGORISED]


def test_a_card_payment_plaid_left_under_other_is_a_payment(tmp_path):
    rows = [prow("auto", -9.0, "OTHER", "OTHER_OTHER", name="AUTOMATIC PAYMENT - THANK")]
    (t,) = feed.from_plaid(store_with(tmp_path, rows))
    assert t.kind == "payment" and not feed.is_spend(t)


def test_a_hand_override_beats_the_rules(tmp_path):
    import json

    store_with(tmp_path, [prow("pizza", 32.0, "OTHER", "OTHER_OTHER", name="Rosati's Pizza")])
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "overrides.json").write_text(json.dumps({"pizza": {"family": "restaurants", "category": "dining"}}))
    (t,) = feed.labelled(tmp_path, _rules())
    assert (t.family, t.budget_category) == ("restaurants", "dining")


def test_a_merchant_override_beats_the_rules_and_a_row_override_beats_it(tmp_path):
    import json

    store_with(tmp_path, [prow("p1", 32.0, "OTHER", "OTHER_OTHER", name="Rosati's Pizza"),
                          prow("p2", 18.0, "OTHER", "OTHER_OTHER", name="Rosati's Pizza")])
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "merchants.json").write_text(json.dumps({"rosati's pizza": {"category": "dining"}}))
    (tmp_path / "data" / "overrides.json").write_text(json.dumps({"p2": {"category": "fun"}}))
    by_id = {t.txn_id: t for t in feed.labelled(tmp_path, _rules())}
    assert by_id["p1"].budget_category == "dining" and by_id["p1"].family == cat.OTHER  # only what was set moves
    assert by_id["p2"].budget_category == "fun"


def test_without_overrides_the_rules_stand(tmp_path):
    store_with(tmp_path, [prow("pizza", 32.0, "OTHER", "OTHER_OTHER", name="Rosati's Pizza")])
    (t,) = feed.labelled(tmp_path, _rules())
    assert (t.family, t.budget_category) == (cat.OTHER, cat.UNCATEGORISED)


# --- a purchase a card paid through PayPal is in the feed twice ---

def wallet_store(tmp_path, rows):
    store = plaid.Store(tmp_path, "production")
    store.save_items({"i": {"access_token": "t"}})
    store.save_ledger("i", {"accounts": {"c": {"name": "CREDIT CARD", "mask": "2002", "type": "credit", "subtype": "credit card"},
                                         "s": {"name": "Checking", "mask": "2009", "type": "depository", "subtype": "checking"},
                                         "w": {"name": "PayPal", "mask": "", "type": "depository", "subtype": "paypal"},
                                         "wc": {"name": "PayPal Credit Card", "mask": "2010", "type": "credit", "subtype": "paypal"}},
                            "transactions": {r["transaction_id"]: r for r in rows}})
    return store


SHOP = ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE")


def _loaded(tmp_path, rows, **kw):
    wallet_store(tmp_path, rows)
    return {t.txn_id: t for t in feed.load(tmp_path, **kw)}


def test_a_card_purchase_paid_through_paypal_counts_once_on_the_card(tmp_path):
    rows = [prow("card", 23.0, *SHOP, authorized="2026-09-08", merchant="Steam"),
            prow("pp", 23.0, *SHOP, acct="w", authorized="2026-09-08", merchant="Valve")]  # names differ, as they do live
    txns = _loaded(tmp_path, rows, spend_only=False)
    assert (txns["pp"].kind, txns["pp"].transfer, txns["pp"].paid_by) == ("transfer", True, "card")
    assert (txns["card"].kind, txns["card"].transfer, txns["card"].paid_by) == ("purchase", False, "")
    assert list(_loaded(tmp_path, rows)) == ["card"]


def test_a_paypal_purchase_with_no_paying_row_stays_spend(tmp_path):
    """Paid from PayPal's balance, or from the bank: the bank's half is a
    transfer naming PayPal, which is not a purchase and so is no pair."""
    rows = [prow("pp", 60.0, *SHOP, acct="w", authorized="2026-09-08"),
            prow("bank", 60.0, "TRANSFER_OUT", "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS", acct="s", authorized="2026-09-09"),
            prow("near", 60.01, *SHOP, authorized="2026-09-08"),         # a cent off
            prow("late", 60.0, *SHOP, authorized="2026-10-08"),          # next month's charge
            prow("back", -60.0, *SHOP, authorized="2026-09-08")]         # a refund is not a purchase
    txns = _loaded(tmp_path, rows, spend_only=False)
    assert (txns["pp"].kind, txns["pp"].paid_by) == ("purchase", "")
    assert sorted(_loaded(tmp_path, rows)) == ["back", "late", "near", "pp"]


def test_the_pairing_window_holds_a_bank_debit_and_stops_there(tmp_path):
    def paired(gap_day):
        rows = [prow("pp", 15.0, *SHOP, acct="w", authorized="2026-09-10"),
                prow("debit", 15.0, *SHOP, acct="s", authorized=f"2026-09-{gap_day:02d}")]
        return _loaded(tmp_path, rows, spend_only=False)["pp"].paid_by == "debit"

    assert [d for d in range(6, 18) if paired(d)] == list(range(10 - feed.PAIR_DAYS_BEFORE, 10 + feed.PAIR_DAYS_AFTER + 1))


def test_each_paying_row_pairs_once_and_a_matching_merchant_wins_the_tie(tmp_path):
    rows = [prow("pp1", 4.99, *SHOP, acct="w", authorized="2026-09-08", merchant="Valve"),
            prow("pp2", 4.99, *SHOP, acct="w", authorized="2026-09-08", merchant="Tebex"),
            prow("pp3", 4.99, *SHOP, acct="w", authorized="2026-09-08", merchant="Valve"),   # a third with no card row
            prow("cardA", 4.99, *SHOP, authorized="2026-09-08", merchant="Steam"),
            prow("cardB", 4.99, *SHOP, authorized="2026-09-08", merchant="Tebex")]
    txns = _loaded(tmp_path, rows, spend_only=False)
    assert txns["pp2"].paid_by == "cardB"
    assert sorted(t.paid_by for t in (txns["pp1"], txns["pp3"])) == ["", "cardA"]
    # three bought, two on a card: the feed counts three
    assert round(sum(t.amount for t in _loaded(tmp_path, rows).values()), 2) == 14.97


def test_a_refund_through_paypal_pairs_with_the_cards_refund(tmp_path):
    rows = [prow("card", -30.0, *SHOP, authorized="2026-09-08"),
            prow("pp", -30.0, *SHOP, acct="w", authorized="2026-09-07")]
    txns = _loaded(tmp_path, rows, spend_only=False)
    assert txns["pp"].paid_by == "card" and txns["pp"].transfer
    assert [t.amount for t in _loaded(tmp_path, rows).values()] == [-30.0]


def test_paypals_credit_card_is_a_card_not_a_wallet(tmp_path):
    """Its purchases are real card spend, and it can be the card that paid."""
    rows = [prow("ppc", 80.0, *SHOP, acct="wc", authorized="2026-09-08"),
            prow("pp", 80.0, *SHOP, acct="w", authorized="2026-09-08"),
            prow("alone", 12.0, *SHOP, acct="wc", authorized="2026-09-08")]
    txns = _loaded(tmp_path, rows, spend_only=False)
    assert (txns["ppc"].wallet, txns["pp"].wallet) == (False, True)
    assert txns["pp"].paid_by == "ppc"
    assert sorted(_loaded(tmp_path, rows)) == ["alone", "ppc"]


def test_a_paired_wallet_row_is_a_transfer_to_the_categoriser(tmp_path):
    rows = [prow("card", 23.0, *SHOP, authorized="2026-09-08", name="Walmart", channel="online"),
            prow("pp", 23.0, *SHOP, acct="w", authorized="2026-09-08", name="Walmart", channel="online")]
    wallet_store(tmp_path, rows)
    (tmp_path / "rules.toml").write_text((INSTANCE / "rules.toml").read_text())
    txns = {t.txn_id: t for t in feed.labelled(tmp_path, _rules(), spend_only=False)}
    assert txns["pp"].budget_category == "transfer"
    assert txns["card"].family == "walmart_online"
