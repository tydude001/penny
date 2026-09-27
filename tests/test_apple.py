from penny import apple

HEADER = "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\n"
COFFEE = '09/02/2026,{clear},"COFFEE","Coffee","Restaurants","Purchase","4.50","Jane"\n'
PAY = '09/05/2026,09/05/2026,"ACH DEPOSIT","Ach","Payment","Payment","-50.00","Jane"\n'
CASH = '08/30/2026,08/30/2026,"DAILY CASH ADJUSTMENT","Daily Cash","Debit","Debit","0.39","Jane"\n'


def test_two_identical_coffees_survive_and_an_overlapping_export_does_not_double(tmp_path):
    (tmp_path / "a.csv").write_text(HEADER + COFFEE.format(clear="09/02/2026") * 2 + PAY)
    (tmp_path / "b.csv").write_text(HEADER + COFFEE.format(clear="09/02/2026") * 2)
    rows = apple.load(tmp_path)
    assert [r["amount"] for r in rows] == [4.5, 4.5, -50.0]
    assert [r["kind"] for r in rows] == ["purchase", "purchase", "payment"]


def test_a_pending_row_picks_up_its_clearing_date_from_a_later_export(tmp_path):
    (tmp_path / "a.csv").write_text(HEADER + COFFEE.format(clear=""))
    (tmp_path / "b.csv").write_text(HEADER + COFFEE.format(clear="09/03/2026"))
    (row,) = apple.load(tmp_path)
    assert row["pending"] is False and row["posted"] == "2026-09-03" and row["date"] == "2026-09-02"


def test_daily_cash_clawback_is_a_reward_credit(tmp_path):
    (tmp_path / "a.csv").write_text(HEADER + CASH)
    assert apple.load(tmp_path)[0]["kind"] == "reward credit"


def test_import_copies_exports_once_and_skips_the_rest(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    (src / "wallet.csv").write_text(HEADER + PAY)
    (src / "other.csv").write_text("a,b\n1,2\n")
    dest = tmp_path / "apple"
    out = apple.import_files([src / "wallet.csv", src / "other.csv"], dest)
    assert out[0].endswith("imported (1 rows)") and "skipped" in out[1]
    assert apple.import_files([src / "wallet.csv"], dest)[0].endswith("already imported (1 rows)")
    assert [p.name for p in dest.iterdir()] == ["wallet.csv"]
