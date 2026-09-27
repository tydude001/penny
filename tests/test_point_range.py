"""The baseline card's ¢/pt as a low/base/high range (docs/sapphire-valuation.md
§ Likely repo changes, 1). Synthetic rules only: base is the baseline card's
own default_rate, low/high come from assumptions.toml [point_value.<card>]."""

import pytest

from penny import board, model, report


def _rules():
    return {
        "held": {"baseline_card": "points"},
        "cards": {
            # A points card at 1.5¢: 1x everywhere, 3x restaurants.
            "points": {"label": "Points", "default_rate": 0.015, "rates": {"restaurants": 0.045}},
            "flat2": {"label": "Flat 2%", "default_rate": 0.02},
            "store": {"label": "Store card", "default_rate": 0.01, "rates": {"store": 0.05}, "requires_membership": True},
        },
        "memberships": {
            "shop": {"label": "Shop", "fee": 30, "families": ["store"], "card": "store"},
        },
    }


def _assumptions():
    return {"point_value": {"points": {"low": 0.01, "high": 0.03}}}


SPEND = {"other": 1000.0, "restaurants": 500.0, "store": 1000.0}


def test_point_values_base_is_the_cards_own_rate():
    pv = model.point_values(_rules(), _assumptions())
    assert pv == {"low": pytest.approx(0.01), "base": pytest.approx(0.015), "high": pytest.approx(0.03)}


def test_point_values_without_a_range_collapse_to_base():
    pv = model.point_values(_rules(), {})
    assert pv == {"low": 0.015, "base": 0.015, "high": 0.015}


def test_scaled_card_keeps_each_multiple():
    c = model.scaled_card(_rules()["cards"]["points"], 0.01)
    assert c["default_rate"] == pytest.approx(0.01)
    assert c["rates"]["restaurants"] == pytest.approx(0.03)
    # the original is untouched
    assert _rules()["cards"]["points"]["rates"]["restaurants"] == 0.045


def test_card_edges_at_each_point_value():
    edges = model.card_edges_at_points(_rules(), _assumptions(), SPEND, 1.0)
    assert "points" not in edges  # the baseline has no edge over itself
    # Flat 2% beats 1x only: (2% - ¢/pt) × $1000 of other, and the store $1000.
    assert edges["flat2"]["low"] == pytest.approx(0.01 * 1000 + 0.01 * 1000)
    assert edges["flat2"]["base"] == pytest.approx(0.005 * 1000 + 0.005 * 1000)
    assert edges["flat2"]["high"] == pytest.approx(0.0)
    assert edges["store"]["low"] == pytest.approx(0.04 * 1000)
    assert edges["store"]["high"] == pytest.approx(0.02 * 1000)


def test_card_edges_are_annualised():
    edges = model.card_edges_at_points(_rules(), _assumptions(), {"other": 500.0}, 2.0)
    assert edges["flat2"]["low"] == pytest.approx(0.01 * 1000)


def test_membership_nets_at_each_point_value():
    at = model.evaluate_at_points(_rules(), _assumptions(), SPEND, 1.0)
    net = {lvl: next(v for v in vs if v.membership == "shop" and v.with_card).net["base"] for lvl, vs in at.items()}
    # store card 5% vs the baseline's 1x at each ¢/pt, $1000, minus the $30 fee
    assert net["low"] == pytest.approx(40 - 30)
    assert net["base"] == pytest.approx(35 - 30)
    assert net["high"] == pytest.approx(20 - 30)


def test_evaluate_at_base_matches_evaluate():
    at = model.evaluate_at_points(_rules(), _assumptions(), SPEND, 1.0)
    plain = model.evaluate(_rules(), _assumptions(), SPEND, 1.0)
    assert [v.net for v in at["base"]] == [v.net for v in plain]


def test_report_block_shows_each_level_and_marks_flips():
    text = report.point_range_block(_rules(), _assumptions(), SPEND, 1.0)
    assert "1¢" in text and "1.5¢" in text and "3¢" in text
    flat = next(line for line in text.splitlines() if line.startswith("Flat 2%"))
    assert "FLIPS" in flat  # $20 edge at low, gone at high
    shop = next(line for line in text.splitlines() if line.startswith("Shop + card"))
    assert "FLIPS" in shop  # +$10 at low, -$10 at high
    assert "assumptions.toml [point_value.points]" in text


def test_report_block_no_flip_when_sign_holds():
    a = {"point_value": {"points": {"low": 0.014, "high": 0.016}}}
    text = report.point_range_block(_rules(), a, SPEND, 1.0)
    shop = next(line for line in text.splitlines() if line.startswith("Shop + card"))
    assert "FLIPS" not in shop


def test_report_block_says_when_no_range_is_set():
    text = report.point_range_block(_rules(), {}, SPEND, 1.0)
    assert "no range" in text and "[point_value.points]" in text


def test_report_block_warns_when_base_outside_range():
    a = {"point_value": {"points": {"low": 0.02, "high": 0.03}}}
    text = report.point_range_block(_rules(), a, SPEND, 1.0)
    assert "outside" in text


def test_board_ignores_the_point_value_table():
    rules, assumptions = _rules(), _assumptions()
    keys = {d.key for d in board.derive(rules, assumptions, SPEND, 1.0, {}, [])}
    assert not any("point_value" in k for k in keys)
