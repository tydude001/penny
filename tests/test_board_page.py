"""The page layer: questions in words, groups, and the ¢/pt rescale."""

from datetime import date
from pathlib import Path

import pytest

from penny import board, home, model

INSTANCE = Path(__file__).parent / "fixtures" / "home"  # a test instance, read over penny/defaults
TODAY = date(2026, 9, 14)


def _state(rules, assumptions, spend):
    ds = board.rank(board.derive(rules, assumptions, spend, 1.0, {}, [], TODAY))
    rows = board.verdict_rows(rules, assumptions, spend, 1.0)
    w = model.Window(date(2025, 9, 15), TODAY)
    return board.State(TODAY, Path("x.csv"), rules, assumptions, w, w.start, 0, spend, {}, ds, rows, None)


def _pair():
    rules = {
        "held": {"baseline_card": "pts", "cards": ["pts"], "club_tier": "basic"},
        "families": {"club": {"label": "Club", "membership": "club"}, "shop": {"label": "Shop", "membership": "shop"}},
        "cards": {"pts": {"label": "Points", "verified": False, "default_rate": 0.02, "rates": {"restaurants": 0.06, "gas": 0.06}}},
        "memberships": {
            "club": {"label": "Club", "families": ["club"], "fee": 65},
            "shop": {"label": "Shop+", "families": ["shop"], "fee": 100},
        },
    }
    assumptions = {
        "club": {"prices": {"note": "", "low": 0, "base": 0, "high": 0, "ask": "Is Club cheaper?", "low_if": "No real saving"}},
        "shop": {"delivery": {"note": "zero if you'd pick up", "low": 0, "base": 0, "high": 450, "ask": "Would you pay for delivery?", "low_if": "No, pickup", "high_if": "Yes, I'd pay"}},
    }
    return rules, assumptions, {"club": 3000.0, "shop": 2000.0, "gas": 1000.0, "restaurants": 1000.0}


def test_questions_are_worded_grouped_and_linked():
    rules, assumptions, spend = _pair()
    s = _state(rules, assumptions, spend)
    items = {i.key: i for i in board.plan(s)}
    assert items["perk:shop.delivery"].group == "ask"
    assert items["perk:shop.delivery"].title == "Would you pay for delivery?"
    assert "If delivery is worth $100 or more a year to you, it becomes a keep" in items["perk:shop.delivery"].why
    # a can't-tell verdict promotes its zeroed perks into one question
    assert items["ask:club"].group == "ask" and items["ask:club"].title == "Does Club save you $65 or more a year?"
    assert "perks:zero" not in items
    page = board.render_todo(s)
    assert "No, pickup" in page and "Yes, I&#x27;d pay" in page
    page = board.render_memberships(s)
    assert "Hangs on question" in page and "href='/todo#ask:club'" in page


def test_small_baseline_swing_is_nothing_to_do():
    rules, assumptions, spend = _pair()  # no verdict carries the baseline's edge, so ½¢ moves $0
    item = next(i for i in board.plan(_state(rules, assumptions, spend)) if i.key == "cpp:pts")
    assert item.group == "quiet" and item.title == "Points points at 2¢"


def test_perk_recorded_at_zero_settles_the_verdict():
    rules, assumptions, spend = _pair()
    assumptions["club"]["prices"]["note"] = "Recorded 2026-09-14: No real saving, $0 a year"
    rows = {r.membership: r for r in board.verdict_rows(rules, assumptions, spend, 1.0)}
    assert rows["club"].verdict == "drop"
    keys = {d.key for d in board.derive(rules, assumptions, spend, 1.0, {}, [], TODAY)}
    assert "perks:zero" not in keys


def test_cpp_swing_and_allowlist_cover_every_baseline_rate():
    rules, assumptions, _spend = _pair()
    assert board.rate_multiples(rules["cards"]["pts"]) == {"restaurants": 3.0, "gas": 3.0}
    s, _ = board.rate_swing(rules, assumptions, {"gas": 1000.0}, 1.0)
    assert s == 0  # no membership verdict carries the baseline's edge here
    real = home.load_rules(INSTANCE / "rules.toml")
    base = real["held"]["baseline_card"]
    for fam in real["cards"][base].get("rates", {}):
        board.check_write({"file": "rules", "section": f"cards.{base}", "key": "rates", "subkey": fam, "value": 0.05}, real)
    with pytest.raises(board.Refused):
        board.check_write({"file": "rules", "section": f"cards.{base}", "key": "rates", "subkey": "nonsense", "value": 0.05}, real)
