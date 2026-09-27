"""report.render and its blocks, on synthetic rules and spend."""

from datetime import date

from penny import model, report
from penny.load import Txn


def _rules():
    return {
        "held": {"baseline_card": "flat"},
        "cards": {
            "flat": {"default_rate": 0.02},
            "club_card": {"label": "Club Card", "default_rate": 0.01, "requires_membership": True, "rates": {"club": 0.05}},
        },
        "memberships": {
            "club": {"label": "Club", "families": ["club"], "card": "club_card", "fee": 60},
            "tiny": {"label": "Tiny", "families": ["tiny"], "fee": 500},
        },
    }


def test_render_has_every_section_and_the_numbers():
    rules = _rules()
    w = model.Window(date(2026, 1, 1), date(2026, 6, 30))  # 181 days
    spend = {"club": 1000.0, "other": 3000.0}
    verdicts = model.evaluate(rules, {}, spend, w.annualize)
    fee = Txn(date(2026, 3, 1), "CLUB MEMBERSHIP", 75.0, "")
    out = report.render(w, 42, spend, {"club": "Club stuff"}, verdicts, {"club": [fee]}, rules, ["club_card"])
    assert "Window 2026-01-01 → 2026-06-30 (181 days, ×2.02 to annualise), 42 transactions" in out
    assert "Club stuff" in out and "$1,000" in out and "$2,017" in out  # 1000 × 365/181
    assert "club: 2026-03-01 $75 (CLUB MEMBERSHIP)" in out
    assert "FEE CHANGED? club: charged $75" in out  # $15 off the $60 list fee
    assert "Club + card" in out and "Tiny" in out
    assert "n/a" in out  # Tiny: nothing to earn, no break-even
    assert "UNVERIFIED rates/fees in rules.toml (confirm before trusting): club_card" in out
    assert "75% of spend is outside every family" in out


def test_render_with_no_fees_and_extra_blocks():
    rules = _rules()
    w = model.Window(date(2026, 1, 1), date(2026, 12, 31))
    out = report.render(w, 0, {"club": 10.0}, {}, [], {}, rules, [], point_range="RANGE", extra=["EXTRA", ""])
    assert "none matched" in out
    assert "\nRANGE" in out and "\nEXTRA" in out
    assert "UNVERIFIED" not in out
    assert "0% of spend is outside every family" in out


def test_spend_table_sorts_biggest_first_and_annualises():
    t = report.spend_table({"a": 10.0, "b": 30.0}, 2.0, {"b": "Bee"})
    lines = t.splitlines()
    assert lines[2].startswith("Bee") and "$60" in lines[2]
    assert lines[3].startswith("a") and "$20" in lines[3]


def test_render_says_when_the_window_is_partial():
    rules = _rules()
    w = model.Window(date(2026, 1, 1), date(2026, 9, 27), asked=365)
    out = report.render(w, 1, {}, {}, model.evaluate(rules, {}, {}, w.annualize), {}, rules, [])
    assert "Partial: the data covers 270 of the 365 days asked for" in out
    full = model.Window(date(2026, 1, 1), date(2026, 12, 31), asked=365)
    assert "Partial" not in report.render(full, 1, {}, {}, [], {}, rules, [])
