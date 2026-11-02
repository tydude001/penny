"""penny import csv: a row counts once however many files carry it, a replace
never drops rows unasked, and misread amounts and dates are refused. Synthetic rows only."""

import pytest

from penny import __main__ as cli
from penny import csvimport, feed, home
from penny import load as ld

BANK = ("Posting Date,Details,Amount\n"
        "09/01/2026,KROGER #0588,54.20\n"
        "09/01/2026,KROGER #0588,54.20\n"
        "09/03/2026,SHELL OIL 5744,41.00\n")
LATER = ("Posting Date,Details,Amount\n"
         "09/03/2026,SHELL OIL 5744,41.00\n"  # the overlap with BANK
         "09/20/2026,TORCHYS TACOS,22.00\n")
MAP = ("--date", "Posting Date", "--amount", "Amount", "--description", "Details", "--account-name", "Visa")


@pytest.fixture
def inst(tmp_path):
    d = tmp_path / "home"
    home.init(d)
    return d


def _import(inst, path, *extra):
    cli.main(["--home", str(inst), "import", "csv", str(path), *MAP, *extra])


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_a_re_download_under_another_name_adds_nothing(inst, tmp_path, capsys):
    _import(inst, _write(tmp_path, "export.csv", BANK))
    _import(inst, _write(tmp_path, "export (1).csv", BANK))
    assert "all 3 rows are already imported" in capsys.readouterr().out
    assert [t.amount for t in feed.load(inst, spend_only=False)] == [54.2, 54.2, 41.0]


def test_overlapping_exports_count_the_overlap_once(inst, tmp_path, capsys):
    _import(inst, _write(tmp_path, "aug-sep.csv", BANK))
    _import(inst, _write(tmp_path, "sep-oct.csv", LATER))
    assert "skipped 1 rows another imported file already holds" in capsys.readouterr().out
    assert sorted(t.amount for t in feed.load(inst, spend_only=False)) == [22.0, 41.0, 54.2, 54.2]


def test_a_third_identical_row_is_still_new(inst, tmp_path):
    _import(inst, _write(tmp_path, "a.csv", BANK))  # two Kroger trips
    _import(inst, _write(tmp_path, "b.csv", BANK.replace("09/03/2026,SHELL", "09/01/2026,KROGER #0588,54.20\n09/03/2026,SHELL")))
    assert [t.amount for t in feed.load(inst, spend_only=False)].count(54.2) == 3


def test_a_replace_that_would_drop_rows_is_refused(inst, tmp_path):
    _import(inst, _write(tmp_path, "export.csv", BANK))
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(SystemExit, match="already holds 3 rows this file doesn't"):
        _import(inst, _write(other, "export.csv", LATER.replace("09/03/2026,SHELL OIL 5744,41.00\n", "")))
    assert len(feed.load(inst, spend_only=False)) == 3


def test_names_that_collide_after_cleaning_are_refused_too(inst, tmp_path):
    _import(inst, _write(tmp_path, "a b.csv", BANK))
    with pytest.raises(SystemExit, match="--as NAME"):
        _import(inst, _write(tmp_path, "a-b.csv", "Posting Date,Details,Amount\n10/01/2026,TORCHYS TACOS,22.00\n"))


def test_as_and_replace_get_past_the_refusal(inst, tmp_path):
    _import(inst, _write(tmp_path, "export.csv", BANK))
    nov = tmp_path / "n"
    nov.mkdir()
    _import(inst, _write(nov, "export.csv", "Posting Date,Details,Amount\n11/01/2026,TORCHYS TACOS,22.00\n"), "--as", "export-nov")
    assert len(feed.load(inst, spend_only=False)) == 4
    _import(inst, nov / "export.csv", "--replace")
    # export.csv's replacement is all in export-nov.csv already: the September rows go, November counts once.
    assert [t.amount for t in feed.load(inst, spend_only=False)] == [22.0]


def test_a_superset_re_import_still_replaces(inst, tmp_path, capsys):
    p = _write(tmp_path, "export.csv", BANK)
    _import(inst, p)
    p.write_text(BANK + "09/04/2026,TORCHYS TACOS,22.00\n")
    _import(inst, p)
    assert "replaced 4 rows" in capsys.readouterr().out


def test_comma_decimals_are_refused_not_read_as_thousands():
    for s in ("1.234,56", "12,50"):
        with pytest.raises(ValueError, match="comma decimal"):
            ld._parse_amount(s)
    assert ld._parse_amount("1,234.56") == 1234.56 and ld._parse_amount("$-1,234.56") == -1234.56


def test_a_day_first_file_is_refused_and_date_format_reads_it(tmp_path):
    m = csvimport.ColumnMap(date="D", description="N", amount="A")
    p = _write(tmp_path, "x.csv", "D,N,A\n04/03/2026,a,1\n13/03/2026,b,1\n")
    with pytest.raises(ValueError, match="--date-format"):
        csvimport.read_mapped(p, m)
    # No day past 12, but in date order only read day first: 2 Mar, 10 Mar, 1 Apr.
    q = _write(tmp_path, "y.csv", "D,N,A\n02/03/2026,a,1\n10/03/2026,b,1\n01/04/2026,c,1\n")
    with pytest.raises(ValueError, match="only when read day first"):
        csvimport.read_mapped(q, m)
    m.date_format = "%d/%m/%Y"
    assert [r["date"] for r in csvimport.read_mapped(q, m)] == ["2026-03-02", "2026-03-10", "2026-04-01"]


def test_a_fee_on_a_transfer_is_spend():
    assert csvimport._kind("WESTERN UNION TRANSFER FEE", 5.0, False) == "purchase"
    assert csvimport._kind("ONLINE TRANSFER TO SAV", 300.0, True) == "transfer"
    assert csvimport._kind("AUTOPAY PAYMENT - THANK YOU", -500.0, False) == "payment"
