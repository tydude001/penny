from datetime import date
from pathlib import Path

from penny import load as ld

FIX = Path(__file__).parent / "fixtures" / "sample.csv"


def test_load_parses_and_drops_ignored():
    txns = ld.load(FIX)
    assert len(txns) == 14  # 15 rows minus the "everything"-ignored transfer
    assert txns[0].date == date(2026, 1, 5)
    assert txns[0].amount == 120.0
    assert all("TO SAVINGS" not in t.description for t in txns)


def test_exclude_categories():
    txns = ld.load(FIX, exclude_categories=["Income"])
    assert len(txns) == 13
    assert all(t.category != "Income" for t in txns)


def test_negative_sign_flips():
    txns = ld.load(FIX, expense_sign="negative")
    assert txns[0].amount == -120.0


def test_inspect_guesses_sign():
    info = ld.inspect(FIX)
    assert info["expense_sign_guess"] == "positive"
    assert info["first"] == date(2026, 1, 5)
    assert "Name" in info["columns"]


def test_dedup_across_accounts(tmp_path):
    hdr = "Date,Account Name,Account Number,Name,Amount,Description,Category,Ignored From\n"
    rows = [
        "2026-01-05,CREDIT CARD,2008,Taco Bell,7.23,TACO BELL #1,Dining & Drinks,",
        "2026-01-05,CREDIT CARD,2002,Taco Bell,7.23,TACO BELL #1,Dining & Drinks,",  # re-issued card twin → dropped
        "2026-01-05,CREDIT CARD,2002,Coffee,3.00,COFFEE,Dining & Drinks,",
        "2026-01-05,CREDIT CARD,2002,Coffee,3.00,COFFEE,Dining & Drinks,",  # same account → kept
    ]
    p = tmp_path / "x.csv"
    p.write_text(hdr + "\n".join(rows) + "\n")
    assert len(ld.load(p)) == 3
    assert len(ld.load(p, dedup_across_accounts=False)) == 4
