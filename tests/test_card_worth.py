"""The card-worth block (docs/sapphire-valuation.md § Likely repo changes, 3):
is the points card worth its fee against a flat 2% card, on the spend it
actually carries. Separate from the membership net, fed by assumptions.toml
[card_worth]; benefit values stay verified = false until you check them.
Synthetic data only."""

from datetime import date

import pytest

from penny import board, cardworth, model, report
from penny.load import Txn


def _rules():
    return {
        "held": {"baseline_card": "points"},
        "cards": {
            # 1.5¢ points card, 3x restaurants, $95 fee, spend on two accounts.
            "points": {"label": "Points", "default_rate": 0.015, "rates": {"restaurants": 0.045}, "annual_fee": 95, "accounts": ["1111", "2222"]},
            "flat2": {"label": "Flat 2%", "default_rate": 0.02, "annual_fee": 0, "verified": True},
        },
        "memberships": {},
        "card_credits": {"points": {"year_starts": "09-26", "fee_patterns": ["annual membership fee"], "credits": {"travel": {"patterns": ["travel credit"]}}}},
    }


def _assumptions():
    return {
        "point_value": {"points": {"low": 0.01, "high": 0.02}},
        "card_worth": {
            "alternatives": ["points", "flat2"],
            "benefits": {
                "points": {
                    "hotel": {"note": "hotel credit", "verified": False, "low": 0, "base": 50, "high": 100},
                    "dash": {"note": "delivery", "verified": False, "low": 0, "base": 0, "high": 0},
                }
            },
        },
    }


SPEND = {"other": 1000.0, "restaurants": 1000.0}


def _t(name, amount, acct="1111", category="Shopping"):
    return Txn(date=date(2026, 1, 1), name=name, amount=amount, category=category, account_number=acct)


def test_worth_spend_is_the_cards_own_accounts_without_fee_or_credits():
    tx = [
        _t("Store", 100.0),
        _t("Store", 50.0, acct="2222"),
        _t("Elsewhere", 999.0, acct="9999"),
        _t("ANNUAL MEMBERSHIP FEE", 95.0),
        _t("TRAVEL CREDIT $300/YEAR", -40.0),
    ]
    for t in tx:
        t.family = "other"
    assert cardworth.worth_spend(tx, _rules(), "points") == {"other": pytest.approx(150.0)}


def test_worth_spend_without_accounts_is_all_spend():
    r = _rules()
    del r["cards"]["points"]["accounts"]
    tx = [_t("Store", 100.0), _t("Elsewhere", 50.0, acct="9999")]
    for t in tx:
        t.family = "other"
    assert cardworth.worth_spend(tx, r, "points") == {"other": pytest.approx(150.0)}


def test_points_card_worth_at_each_level():
    w = {x.card: x for x in cardworth.card_worth(_rules(), _assumptions(), SPEND)}
    p = w["points"]
    # earn: 1x other + 3x restaurants, each at the ¢/pt level
    assert p.earn == {"low": pytest.approx(10 + 30), "base": pytest.approx(15 + 45), "high": pytest.approx(20 + 60)}
    assert p.benefits == {"low": 0, "base": 50, "high": 100}
    assert p.fee == 95
    assert p.worth["low"] == pytest.approx(40 + 0 - 95)
    assert p.worth["base"] == pytest.approx(60 + 50 - 95)
    assert p.worth["high"] == pytest.approx(80 + 100 - 95)


def test_cash_card_worth_does_not_move_with_points():
    w = {x.card: x for x in cardworth.card_worth(_rules(), _assumptions(), SPEND)}
    f = w["flat2"]
    assert f.earn == {"low": pytest.approx(40), "base": pytest.approx(40), "high": pytest.approx(40)}
    assert f.worth == f.earn  # no fee, no benefits


def test_worth_is_kept_out_of_the_membership_net():
    before = [v.net for v in model.evaluate(_rules(), {}, SPEND, 1.0)]
    after = [v.net for v in model.evaluate(_rules(), _assumptions(), SPEND, 1.0)]
    assert before == after


def test_unverified_benefits_are_listed():
    assert cardworth.unverified_benefits(_assumptions()) == ["points.hotel", "points.dash"]
    a = _assumptions()
    a["card_worth"]["benefits"]["points"]["hotel"]["verified"] = True
    assert cardworth.unverified_benefits(a) == ["points.dash"]


def test_block_compares_first_alternative_to_the_rest_and_marks_flips():
    text = report.card_worth_block(_rules(), _assumptions(), SPEND, 1.0)
    assert "Points" in text and "Flat 2%" in text
    # Points worth -55 / +15 / +85 against Flat 2%'s flat 40: -95 / -25 / +45 → flips
    line = next(ln for ln in text.splitlines() if "over Flat 2%" in ln)
    assert "-$95" in line and "-$25" in line and "+$45" in line and "FLIPS" in line
    assert "UNVERIFIED" in text and "points.hotel" in text


def test_block_no_flip_when_sign_holds():
    a = _assumptions()
    # 4¢ low: 160 earn - 95 fee = 65 > Flat 2%'s 40 at every level
    a["point_value"]["points"] = {"low": 0.04, "high": 0.05}
    rules = _rules()
    rules["cards"]["points"].update(default_rate=0.045, rates={"restaurants": 0.135})
    text = report.card_worth_block(rules, a, SPEND, 1.0)
    line = next(ln for ln in text.splitlines() if "over Flat 2%" in ln)
    assert "FLIPS" not in line


def test_block_is_annualised():
    text = report.card_worth_block(_rules(), _assumptions(), {k: v / 2 for k, v in SPEND.items()}, 2.0)
    line = next(ln for ln in text.splitlines() if "over Flat 2%" in ln)
    assert "-$25" in line


def test_block_flags_a_missing_fee():
    r = _rules()
    del r["cards"]["points"]["annual_fee"]
    text = report.card_worth_block(r, _assumptions(), SPEND, 1.0)
    assert "annual_fee" in text


def test_block_is_empty_without_a_section():
    assert report.card_worth_block(_rules(), {}, SPEND, 1.0) == ""


def test_board_ignores_the_card_worth_table():
    keys = {d.key for d in board.derive(_rules(), _assumptions(), SPEND, 1.0, {}, [])}
    assert not any("card_worth" in k or "hotel" in k for k in keys)
