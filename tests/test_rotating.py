"""Pooled points (``points_of``) and rotating quarters: the Freedom Flex shape.
Synthetic rules only."""

from datetime import date

import pytest

from penny import board, model


def _rules():
    return model.resolve_points({
        "held": {"baseline_card": "pts", "cards": ["pts", "flex", "grocer"]},
        "cards": {
            "pts": {"label": "Points", "default_rate": 0.02, "rates": {"restaurants": 0.06}},
            "flex": {
                "label": "Flex", "points_of": "pts", "points": {"default": 1, "restaurants": 3, "drugstores": 3},
                "quarters": [{"start": "2026-10-01", "end": "2026-12-31", "families": ["restaurants", "groceries"], "points": 5, "cap": 1500}],
            },
            "grocer": {"label": "Grocer", "default_rate": 0.01, "rates": {"groceries": 0.05}},
        },
    })


SPEND = {"restaurants": 4000.0, "groceries": 2000.0, "drugstores": 100.0, "other": 1000.0}


def test_pooled_points_take_the_pool_cards_cents():
    c = _rules()["cards"]["flex"]
    assert c["default_rate"] == pytest.approx(0.02)
    assert c["rates"] == {"restaurants": pytest.approx(0.06), "drugstores": pytest.approx(0.06)}
    assert c["quarters"][0]["rate"] == pytest.approx(0.10)


def test_revaluing_the_pool_moves_the_pooled_card():
    c = model.rules_at(_rules(), 0.01)["cards"]["flex"]
    assert c["rates"]["drugstores"] == pytest.approx(0.03)
    assert c["quarters"][0]["rate"] == pytest.approx(0.05)


def test_rotating_edge_is_capped_and_only_counts_quarters_to_come():
    r = _rules()
    flex, pts = r["cards"]["flex"], r["cards"]["pts"]
    # A quarter of the year: $1,000 dining + $500 groceries = $1,500, exactly the cap.
    e, per = model.rotating_edge(flex, pts, SPEND, date(2026, 9, 23))
    assert per["restaurants"] == pytest.approx(0.04 * 1000)  # 10% over the shared 6%
    assert per["groceries"] == pytest.approx(0.08 * 500)  # 10% over the 2% baseline
    doubled = {k: v * 2 for k, v in SPEND.items()}
    assert model.rotating_edge(flex, pts, doubled, date(2026, 9, 23))[0] == pytest.approx(e)  # the cap holds
    # Half the quarter gone: half of it left.
    mid = model.rotating_edge(flex, pts, SPEND, date(2026, 11, 15))[0]
    assert mid == pytest.approx(e * 47 / 92)
    assert model.rotating_edge(flex, pts, SPEND, date(2027, 1, 1)) == (0.0, {})


def test_card_edge_on_adds_the_quarter_to_the_standing_edge():
    r = _rules()
    flex, pts = r["cards"]["flex"], r["cards"]["pts"]
    base, _ = model.card_edge(flex, pts, SPEND)
    assert base == pytest.approx(0.04 * 100)  # drugstores 3x over 1x
    assert model.card_edge_on(flex, pts, SPEND, None)[0] == pytest.approx(base)
    assert model.card_edge_on(flex, pts, SPEND, date(2026, 9, 23))[0] == pytest.approx(base + 40 + 40)


def test_swipe_sheet_uses_the_quarter_only_while_it_runs():
    r = _rules()
    before = {w.family: (w.card, w.until) for w in board.swipe_rows(r, SPEND, 1.0, date(2026, 9, 23))}
    assert before["restaurants"] == ("pts", "") and before["groceries"] == ("grocer", "")
    assert before["drugstores"] == ("flex", "")
    during = {w.family: w for w in board.swipe_rows(r, SPEND, 1.0, date(2026, 10, 2))}
    assert during["restaurants"].card == "flex" and during["restaurants"].rate == pytest.approx(0.10)
    assert during["restaurants"].until == "to $1,500 a quarter, through Dec 31"
    assert during["groceries"].card == "flex"


def test_card_rows_count_the_quarter_as_bonus():
    r = _rules()
    row = next(x for x in board.card_rows(r, {}, SPEND, 1.0, today=date(2026, 9, 23)) if x.key == "flex")
    assert row.edge == pytest.approx(4 + 40 + 40)
    assert row.bonus == pytest.approx(row.edge) and row.default_part == pytest.approx(0)
