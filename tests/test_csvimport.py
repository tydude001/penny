"""penny import csv: presets and a column map, into the instance's feed. Synthetic rows only."""

import shutil
from datetime import date
from pathlib import Path

import pytest
from feedfix import install

from penny import __main__ as cli
from penny import board, csvimport, feed, home

FIX = Path(__file__).parent / "fixtures" / "sample.csv"
WALLET = ("Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\n"
          '09/02/2026,09/02/2026,"COFFEE","Coffee","Restaurants","Purchase","4.50","Jane"\n')
BANK = ("Posting Date,Details,Amount,Card\n"
        "09/01/2026,KROGER #0588,-54.20,Visa 4242\n"
        "09/01/2026,KROGER #0588,-54.20,Visa 4242\n"  # two identical trips in a day both count
        "09/03/2026,SHELL OIL 5744,-41.00,Visa 4242\n"
        "09/04/2026,AMAZON REFUND,12.00,Visa 4242\n"
        "09/05/2026,AUTOPAY PAYMENT - THANK YOU,500.00,Visa 4242\n")
SPLIT = ("Date,Description,Debit,Credit\n"
         "2026-09-01,TORCHYS TACOS,25.00,\n"
         "2026-09-02,RETURN SOME MERCHANT,,10.00\n")


HELD = """
[held]
baseline_card = "baseline"
cards = ["prime_visa", "walmart_card"]
memberships = ["prime", "walmart_plus", "costco"]
costco_tier = "gold_star"
"""


@pytest.fixture
def inst(tmp_path):
    d = tmp_path / "home"
    home.init(d)
    (d / "rules.toml").write_text(HELD)  # a starter instance holds nothing, and the report needs a baseline
    return d


def _cli(inst, *argv):
    cli.main(["--home", str(inst), *argv])


def test_rocket_preset_lands_in_the_feed_with_non_spend_kept_apart(inst, capsys):
    _cli(inst, "import", "csv", str(FIX), "--preset", "rocket")
    assert "imported 14 rows" in capsys.readouterr().out
    rows = feed.load(inst, spend_only=False)
    assert len(rows) == 14 and {t.source for t in rows} == {"rocket_money"}
    paycheck = next(t for t in rows if t.description == "ACME PAYROLL")
    assert paycheck.kind == "income" and not feed.is_spend(paycheck)
    assert len(feed.load(inst)) == 13
    refund = next(t for t in rows if t.amount < 0 and t.kind != "income")
    assert refund.kind == "refund"


def test_importing_the_same_file_again_replaces_it(inst, capsys):
    _cli(inst, "import", "csv", str(FIX), "--preset", "rocket")
    ids = [t.txn_id for t in feed.load(inst, spend_only=False)]
    _cli(inst, "import", "csv", str(FIX), "--preset", "rocket")
    assert "replaced 14 rows" in capsys.readouterr().out
    assert [t.txn_id for t in feed.load(inst, spend_only=False)] == ids
    assert [p.name for p in (inst / "data" / "imports").iterdir()] == ["sample.csv"]


def test_report_reads_the_import_with_no_export_given(inst, capsys):
    _cli(inst, "import", "csv", str(FIX), "--preset", "rocket")
    capsys.readouterr()
    _cli(inst, "report")
    out = capsys.readouterr().out
    assert "13 transactions" in out and "Walmart+" in out


def test_report_with_nothing_imported_says_how(inst):
    with pytest.raises(SystemExit, match="penny import csv"):
        _cli(inst, "report")


def test_column_map_negate_payments_and_identical_rows(inst, tmp_path, capsys):
    src = tmp_path / "My Bank Sept.csv"
    src.write_text(BANK)
    _cli(inst, "import", "csv", str(src), "--date", "Posting Date", "--amount", "Amount", "--description", "Details",
         "--account", "Card", "--last4", "4242", "--negate")
    out = capsys.readouterr().out
    assert "imported 5 rows (4 spend, 1 payments" in out and "--negate" not in out
    rows = feed.load(inst, spend_only=False)
    assert [t.amount for t in rows] == [54.2, 54.2, 41.0, -12.0, -500.0]
    assert [t.kind for t in rows] == ["purchase", "purchase", "purchase", "refund", "payment"]
    assert len({t.txn_id for t in rows}) == 5
    assert {t.account for t in rows} == {"Visa 4242"} and {t.account_number for t in rows} == {"4242"}
    assert (inst / "data" / "imports" / "My-Bank-Sept.csv").exists()
    rules = home.load_rules(inst / "rules.toml")
    fams = {t.name: t.family for t in feed.labelled(inst, rules)}
    assert fams["KROGER #0588"] == "groceries" and fams["SHELL OIL 5744"] == "gas"


def test_without_negate_a_negative_file_gets_a_hint(inst, tmp_path, capsys):
    src = tmp_path / "bank.csv"
    src.write_text(BANK)
    _cli(inst, "import", "csv", str(src), "--date", "Posting Date", "--amount", "Amount", "--description", "Details")
    assert "--negate" in capsys.readouterr().out


def test_debit_and_credit_columns(inst, tmp_path):
    src = tmp_path / "split.csv"
    src.write_text(SPLIT)
    _cli(inst, "import", "csv", str(src), "--date", "Date", "--debit", "Debit", "--credit", "Credit",
         "--description", "Description", "--account-name", "Visa")
    rows = feed.load(inst, spend_only=False)
    assert [(t.amount, t.kind, t.account) for t in rows] == [(25.0, "purchase", "Visa"), (-10.0, "refund", "Visa")]


def test_bank_flag_reads_money_in_as_income(tmp_path):
    src = tmp_path / "chk.csv"
    src.write_text("Date,Memo,Amt\n2026-09-01,ACME PAYROLL,-2000.00\n2026-09-02,ONLINE TRANSFER TO SAV,300.00\n")
    rows = csvimport.read_mapped(src, csvimport.ColumnMap(date="Date", description="Memo", amount="Amt", bank=True))
    assert [r["kind"] for r in rows] == ["income", "transfer"]


def test_a_missing_column_or_no_map_is_refused(inst, tmp_path):
    src = tmp_path / "bank.csv"
    src.write_text(BANK)
    with pytest.raises(SystemExit, match="no column 'Nope'"):
        _cli(inst, "import", "csv", str(src), "--date", "Nope", "--amount", "Amount", "--description", "Details")
    with pytest.raises(SystemExit, match="--preset rocket"):
        _cli(inst, "import", "csv", str(src))
    assert not (inst / "data" / "imports").exists()


def test_apple_preset_and_import_apple_both_copy_into_data_apple(inst, tmp_path, capsys):
    src = tmp_path / "wallet.csv"
    src.write_text(WALLET)
    _cli(inst, "import", "csv", str(src), "--preset", "apple")
    assert "imported (1 rows)" in capsys.readouterr().out
    _cli(inst, "import", "apple", str(src))
    assert "already imported" in capsys.readouterr().out
    assert [p.name for p in (inst / "data" / "apple").iterdir()] == ["wallet.csv"]
    (row,) = feed.load(inst)
    assert row.source == "apple_csv" and row.amount == 4.5


def test_compare_with_no_export_takes_the_imported_rocket_rows(inst, capsys):
    install(inst)
    _cli(inst, "import", "csv", str(FIX), "--preset", "rocket")
    capsys.readouterr()
    _cli(inst, "compare", "--days", "30")
    out = capsys.readouterr().out
    # 13 spend rows each side: the feed side is Plaid only, never the import itself.
    assert "Rocket Money 13 rows, feed 13 rows" in out


def test_the_board_reads_imported_rows(inst, tmp_path):
    shutil.copy(FIX, tmp_path / "e.csv")
    _cli(inst, "import", "csv", str(tmp_path / "e.csv"), "--preset", "rocket")
    cfg = board.Config(inst, inst / "rules.toml", inst / "assumptions.toml", None, None)
    s = board.base_state(cfg, date(2026, 1, 31))
    assert s.n_txns == 13 and s.spend["costco"] == 300.0
