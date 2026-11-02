"""Every window under a year says it is scaled up, wherever it is shown."""

from datetime import date
from pathlib import Path

import pytest

from penny import board, model, report
from penny.load import Txn


def _t(d, amount=10.0):
    return Txn(date=d, name="x", amount=amount, category="", account="", description="")


TXNS = [_t(date(2025, 3, 1)), _t(date(2025, 9, 16))]


def test_a_year_outside_the_data_is_refused_not_negative():
    with pytest.raises(ValueError, match="no transactions in 2023"):
        model.pick_window(TXNS, year=2023)
    with pytest.raises(ValueError, match="no transactions in 2026"):
        model.pick_window(TXNS, year=2026)


def test_a_year_after_data_ending_on_dec_31_is_refused_not_a_zero_division():
    with pytest.raises(ValueError):
        model.pick_window([_t(date(2025, 6, 1)), _t(date(2025, 12, 31))], year=2026)


def test_days_must_be_positive():
    with pytest.raises(ValueError):
        model.pick_window(TXNS, days=0)


def test_a_short_window_by_request_is_short_not_partial():
    w = model.pick_window(TXNS, days=60)
    assert w.days == 60 and not w.partial and w.short
    assert not model.pick_window([_t(date(2024, 1, 1)), _t(date(2025, 6, 30))]).short


def _rules():
    return {"held": {"baseline_card": "b", "cards": []}, "cards": {"b": {"label": "B", "default_rate": 0.02}}, "memberships": {}, "families": {}}


def test_report_flags_a_requested_short_window():
    w = model.Window(date(2025, 7, 19), date(2025, 9, 16), asked=60)
    out = report.render(w, 1, {}, {}, [], {}, _rules(), [])
    assert "Short: a 60-day window is under a year" in out and "×6.08" in out
    assert "Partial" not in out


def _state(w):
    return board.State(w.end, Path("x.csv"), {}, {}, w, w.start, 0, {"x": 1.0}, {}, [], [], None)


def test_the_board_rail_warns_when_the_data_is_under_a_year():
    # pick_window has already clamped start to the first row, so the rail must
    # not recompute coverage from it: 90 days of data is 90 of 365.
    w = model.pick_window([_t(date(2026, 6, 18)), _t(date(2026, 9, 15))])
    html = board.render_rail(_state(w))
    assert "90 of 365 days" in html and "a guess, not a measurement" in html


def test_the_board_rail_is_quiet_over_a_full_year():
    w = model.pick_window([_t(date(2025, 1, 1)), _t(date(2026, 9, 15))])
    html = board.render_rail(_state(w))
    assert "of 365 days" not in html and "a guess" not in html
