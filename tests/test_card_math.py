"""Card math the catalogue relies on: annual caps, pooled cards re-valued,
Feb 29 anniversaries, and Costco trips that only look like fees."""

from datetime import date
from pathlib import Path

import pytest

from penny import cardworth, home, model
from penny import categorize as cat
from penny.load import Txn

DEFAULTS = Path(__file__).parent.parent / "penny" / "defaults" / "rules.toml"


def _defaults():
    return home.load_rules(DEFAULTS)


def test_the_costco_visa_gas_cap_blends_past_7000():
    card = {"default_rate": 0.01, "rates": {"costco_gas": 0.05, "gas": 0.04},
            "caps": [{"families": ["costco_gas", "gas"], "annual": 7000, "then": 0.01}]}
    base = {"default_rate": 0.02}
    # $14k of gas: half under the cap. gas earns 4%·½ + 1%·½ = 2.5%, costco_gas 3%.
    _, per = model.card_edge(card, base, {"gas": 12000.0, "costco_gas": 2000.0})
    assert per["gas"] == pytest.approx((0.025 - 0.02) * 12000)
    assert per["costco_gas"] == pytest.approx((0.03 - 0.02) * 2000)
    # Under the cap nothing changes.
    assert model.card_edge(card, base, {"gas": 5000.0})[0] == pytest.approx(0.02 * 5000)


def test_the_cap_holds_in_the_shipped_catalogue():
    rules = _defaults()
    cv = rules["cards"]["costco_visa"]
    assert cv["caps"][0]["annual"] == 7000
    assert "drugstores" not in rules["cards"]["prime_visa"]["rates"]
    model.validate_rules(rules)


def test_a_cap_naming_an_unknown_family_or_no_amount_is_refused():
    rules = {"families": {"gas": {}}, "cards": {"c": {"caps": [{"families": ["gass"], "annual": 0}]}}}
    with pytest.raises(ValueError) as e:
        model.validate_rules(rules)
    assert "unknown family 'gass'" in str(e.value) and "annual must be a positive" in str(e.value)


def test_a_pooled_card_keeps_its_multiple_when_re_valued():
    rules = {
        "held": {"baseline_card": "pool"},
        "families": {"x": {}},
        "cards": {
            "pool": {"default_rate": 0.01},
            "flex": {"points_of": "pool", "points": {"default": 1.5}},
        },
    }
    rules = model.resolve_points(rules)
    w = cardworth.card_worth(rules, {"card_worth": {"alternatives": ["flex"]}}, {"x": 10000.0})[0]
    assert w.earn["base"] == pytest.approx(150.0)  # 1.5x at 1¢, not 1x


def test_a_feb_29_anniversary_lands_on_feb_28_in_a_common_year():
    assert cardworth.year_start(date(2027, 3, 1), "02-29") == date(2027, 2, 28)
    assert cardworth.year_start(date(2028, 3, 1), "02-29") == date(2028, 2, 29)
    rules = {"cards": {"c": {}}, "card_credits": {"c": {"year_starts": "02-29"}}}
    t = Txn(date(2027, 6, 1), "x", 1.0, "")
    years = cardworth.anniversary_years([t], rules, "c", first=date(2027, 1, 1), last=date(2029, 3, 5))
    assert [y.start for y in years] == [date(2026, 2, 28), date(2027, 2, 28), date(2028, 2, 29), date(2029, 2, 28)]


def _costco(d, amount):
    return Txn(d, "Costco", amount, "GENERAL_MERCHANDISE_SUPERSTORES", description="Costco", mcc="5300")


def test_a_second_fee_sized_costco_trip_in_a_year_is_a_trip():
    fams = cat.build_families(_defaults())
    tx = [_costco(date(2026, 5, 4), 65.0), _costco(date(2026, 7, 1), 65.0),  # the renewal, then a $65.00 trip
          _costco(date(2026, 9, 23), 37.92), _costco(date(2026, 10, 2), 21.67),  # the upgrade, then a trip
          _costco(date(2027, 5, 4), 65.0)]  # next year's renewal
    got = [t.family for t in cat.categorize(tx, fams)]
    assert got == [cat.FEES, "costco", cat.FEES, "costco", cat.FEES]
    found = model.observed_fees(tx, _defaults())
    assert [t.amount for t in found["costco"]] == [65.0, 37.92, 65.0]
