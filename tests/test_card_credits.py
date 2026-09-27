"""Card fee and credit detection per anniversary year (docs/sapphire-valuation.md
§ Likely repo changes, 2). Synthetic data only: credits are matched by
descriptor patterns in rules.toml [card_credits.<card>], never by category,
and rows that exclude_categories drops must still be read."""

from datetime import date

import pytest

from penny import board, cardworth, report
from penny.load import Txn

HEADER = "Date,Original Date,Account Type,Account Name,Account Number,Institution Name,Name,Custom Name,Amount,Description,Category,Note,Ignored From,Tax Deductible\n"


def _rules():
    return {
        "export": {"expense_sign": "positive", "exclude_categories": ["Credit Card Payment", "Internal Transfers"]},
        "held": {"baseline_card": "points"},
        "cards": {
            "points": {"label": "Points", "default_rate": 0.015, "accounts": ["1111", "2222"]},
        },
        "card_credits": {
            "points": {
                "year_starts": "09-26",
                "fee_patterns": ["annual membership fee"],
                "credits": {
                    "travel": {"label": "Travel credit", "patterns": ["travel credit"], "face": 300},
                    "edit": {"label": "The Edit", "patterns": ["the edit"]},
                },
            }
        },
    }


def _t(d, name, amount, acct="1111", category="Travel & Vacation"):
    return Txn(date=d, name=name, amount=amount, category=category, account_number=acct)


def test_year_of_uses_the_anniversary_day():
    assert cardworth.year_start(date(2025, 9, 25), "09-26") == date(2024, 9, 26)
    assert cardworth.year_start(date(2025, 9, 26), "09-26") == date(2025, 9, 26)
    assert cardworth.year_start(date(2026, 1, 3), "09-26") == date(2025, 9, 26)


def test_fee_and_credits_per_year():
    tx = [
        _t(date(2025, 10, 1), "ANNUAL MEMBERSHIP FEE", 550.0, category="Bills & Utilities"),
        _t(date(2025, 10, 5), "TRAVEL CREDIT $300/YEAR", -200.0),
        _t(date(2025, 11, 5), "TRAVEL CREDIT $300/YEAR", -100.0, acct="2222"),
        _t(date(2025, 10, 9), "Some Restaurant", 80.0),  # not a credit
    ]
    years = cardworth.anniversary_years(tx, _rules(), "points", first=date(2025, 9, 26), last=date(2026, 9, 25))
    assert len(years) == 1
    y = years[0]
    assert (y.start, y.end) == (date(2025, 9, 26), date(2026, 9, 25))
    assert y.fee == pytest.approx(550.0)
    assert y.credits["travel"] == pytest.approx(300.0)
    assert y.credits["edit"] == pytest.approx(0.0)
    assert y.partial is None


def test_clawback_nets_against_its_credit():
    tx = [
        _t(date(2025, 11, 11), "THE EDIT $500/YEAR CREDIT", -250.0, category="Credit Card Payment"),
        _t(date(2025, 12, 12), "THE EDIT $500/YEAR CREDIT", 250.0, category="Shopping"),
    ]
    y = cardworth.anniversary_years(tx, _rules(), "points", first=date(2025, 9, 26), last=date(2026, 9, 25))[0]
    assert y.credits["edit"] == pytest.approx(0.0)


def test_only_the_cards_own_accounts_count():
    tx = [_t(date(2025, 10, 5), "TRAVEL CREDIT $300/YEAR", -50.0, acct="9999")]
    y = cardworth.anniversary_years(tx, _rules(), "points", first=date(2025, 9, 26), last=date(2026, 9, 25))[0]
    assert y.credits["travel"] == 0.0


def test_years_span_the_data_and_mark_partial_ones():
    tx = [
        _t(date(2024, 10, 1), "ANNUAL MEMBERSHIP FEE", 550.0),
        _t(date(2025, 10, 1), "ANNUAL MEMBERSHIP FEE", 550.0),
    ]
    years = cardworth.anniversary_years(tx, _rules(), "points", first=date(2024, 9, 15), last=date(2026, 9, 12))
    assert [y.start for y in years] == [date(2023, 9, 26), date(2024, 9, 26), date(2025, 9, 26)]
    assert years[0].partial and "2024-09-15" in years[0].partial
    assert years[1].partial is None
    assert years[2].partial and "2026-09-12" in years[2].partial
    assert [y.fee for y in years] == [0.0, 550.0, 550.0]


def test_credit_rows_are_read_even_when_exclude_categories_drops_them(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text(
        HEADER
        + "2025-10-17,2025-10-17,Credit Card,CREDIT CARD,1111,Chase,TRAVEL CREDIT $300/YEAR,,-160.00,,Credit Card Payment,,,\n"
        + "2025-10-20,2025-10-20,Credit Card,CREDIT CARD,1111,Chase,TRAVEL CREDIT $300/YEAR,,-40.00,,Internal Transfers,,,\n"
        + "2025-10-01,2025-10-01,Credit Card,CREDIT CARD,1111,Chase,ANNUAL MEMBERSHIP FEE,,550.00,,Bills & Utilities,,,\n"
    )
    tx = cardworth.load_for_credits(csv, _rules())
    y = cardworth.anniversary_years(tx, _rules(), "points", first=date(2025, 9, 26), last=date(2026, 9, 25))[0]
    assert y.credits["travel"] == pytest.approx(200.0)
    assert y.fee == pytest.approx(550.0)


def test_is_credit_row_covers_fee_and_credits():
    spec = _rules()["card_credits"]["points"]
    assert cardworth.is_credit_row(_t(date(2025, 1, 1), "ANNUAL MEMBERSHIP FEE", 550.0), spec)
    assert cardworth.is_credit_row(_t(date(2025, 1, 1), "TRAVEL CREDIT $300/YEAR", -5.0), spec)
    assert not cardworth.is_credit_row(_t(date(2025, 1, 1), "Lyft", 20.0), spec)


def test_credits_block_shows_fee_credits_and_face():
    tx = [
        _t(date(2025, 10, 1), "ANNUAL MEMBERSHIP FEE", 550.0),
        _t(date(2025, 10, 5), "TRAVEL CREDIT $300/YEAR", -300.0),
    ]
    text = report.credits_block(_rules(), tx, first=date(2025, 9, 26), last=date(2026, 9, 25))
    assert "2025-09-26" in text and "2026-09-25" in text
    assert "$550" in text and "$300" in text
    assert "Travel credit" in text and "The Edit" in text
    assert "/ $300" in text  # used against face


def test_credits_block_is_empty_without_a_section():
    r = _rules()
    del r["card_credits"]
    assert report.credits_block(r, [], first=date(2025, 1, 1), last=date(2025, 12, 31)) == ""


def test_board_ignores_card_credits():
    rules = _rules()
    rules["memberships"] = {}
    keys = {d.key for d in board.derive(rules, {}, {"other": 100.0}, 1.0, {}, [])}
    assert not any("card_credits" in k or "travel" in k for k in keys)


def test_coverage_starts_at_the_cards_own_first_row():
    # The export may start earlier on other accounts; a year before the card's
    # first row is not "unused", it is unseen.
    tx = [
        _t(date(2023, 5, 19), "Groceries", 10.0, acct="9999"),
        _t(date(2024, 9, 15), "TRAVEL CREDIT $300/YEAR", -100.0),
    ]
    assert cardworth.card_first_row(tx, _rules(), "points") == date(2024, 9, 15)
    text = report.credits_block(_rules(), tx, first=date(2023, 5, 19), last=date(2025, 1, 1))
    assert "2022-09-26" not in text  # the year before the card's first row is not shown
    row = next(ln for ln in text.splitlines() if ln.startswith("2023-09-26"))
    assert "data starts 2024-09-15" in row and "$100 / $300" in row
