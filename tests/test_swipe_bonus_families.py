"""Where to swipe beyond the shopping families: Lyft, transit, phone,
streaming and travel get rows of their own, a stored-card bill doesn't earn
Apple Pay's rate, inherited point rates follow an instance's ¢/pt, and the
sheet reads card spend only. Synthetic rows throughout."""

from datetime import date

import pytest

from penny import categorize as cat
from penny import home, model, worth
from penny.load import Txn

INSTANCE = """
[held]
baseline_card = "sapphire_preferred"
cards = ["sapphire_preferred", "apple_card", "wf_autograph", "prime_visa"]

[cards.sapphire_preferred]
default_rate = 0.015
rates = { restaurants = 0.045 }
"""


def _rules():
    return model.resolve_points(home.merged_text(INSTANCE))


def _txn(name, amount, **kw):
    return Txn(date(2026, 9, 1), name, amount, kw.pop("category", ""), **kw)


def test_bonus_places_get_families_of_their_own():
    fams = cat.build_families(_rules())
    rows = {
        "lyft": _txn("Lyft", 18.0, category="TRANSPORTATION_TAXIS_AND_RIDE_SHARES"),
        "transit": _txn("STATE TOLLWAY-AUTOREPLENISH", 40.0, category="TRANSPORTATION_TOLLS"),
        "phone": _txn("ACME WIRELESS BILL", 70.0, category="RENT_AND_UTILITIES_TELEPHONE"),
        "streaming": _txn("Spotify", 12.0, category="ENTERTAINMENT_TV_AND_MOVIES"),
        "travel": _txn("SOME AIRLINE", 240.0, category="TRAVEL_FLIGHTS"),
    }
    cat.categorize(list(rows.values()), fams)
    assert {k: t.family for k, t in rows.items()} == {k: k for k in rows}


def test_rent_filed_as_lodging_is_not_travel():
    fams = cat.build_families(_rules())
    rent = _txn("SOME APARTMENTS", 1750.0, category="TRAVEL_LODGING")
    cat.categorize([rent], fams)
    assert rent.family == cat.OTHER


def test_an_instance_cpp_rescales_the_rates_it_does_not_name():
    sp = _rules()["cards"]["sapphire_preferred"]
    assert sp["rates"]["restaurants"] == pytest.approx(0.045)  # named: kept as written
    assert sp["rates"]["lyft"] == pytest.approx(0.075)  # 5x at 1.5¢
    assert sp["rates"]["gas"] == pytest.approx(0.045)  # 3x at 1.5¢


def test_a_cash_card_override_is_not_rescaled():
    r = model.resolve_points(home.merged_text(INSTANCE + '\n[cards.prime_visa]\ndefault_rate = 0.02\n'))
    assert r["cards"]["prime_visa"]["rates"]["amazon"] == pytest.approx(0.05)


def test_a_stored_card_bill_earns_apple_cards_typed_in_rate():
    apple = _rules()["cards"]["apple_card"]
    assert model.card_rate(apple, "phone") == pytest.approx(0.01)
    assert model.card_rate(apple, "apple") == pytest.approx(0.03)  # its own rate wins
    assert model.card_rate(apple, cat.OTHER) == pytest.approx(0.02)


def test_swipe_picks_the_bonus_card_over_apple_pay():
    rows = {w.family: w for w in worth.swipe_rows(_rules(), {"lyft": 600.0, "phone": 1000.0, "transit": 400.0}, 1.0)}
    assert (rows["lyft"].card, rows["lyft"].rate) == ("sapphire_preferred", pytest.approx(0.075))
    assert (rows["phone"].card, rows["phone"].rate) == ("wf_autograph", pytest.approx(0.03))
    assert rows["transit"].card == "sapphire_preferred"  # 2x at 1.5¢ beats the Prime Visa's 2%


def test_card_spend_leaves_out_bank_account_payments():
    tx = [_txn("RENT", 1750.0, account_type="depository"), _txn("SHOP", 40.0, account_type="credit"),
          _txn("EXPORT ROW", 10.0)]
    for t in tx:
        t.family = cat.OTHER
    assert worth.card_spend(tx) == {cat.OTHER: pytest.approx(50.0)}
