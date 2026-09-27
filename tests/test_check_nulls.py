"""Review fixes: a null balance in a snapshot, and statements without pdftotext."""

from penny import check, statements


def snap(at, current, acct="s"):
    return {"at": at, "account_id": acct, "name": "Checking", "mask": "2222", "type": "depository", "current": current}


def test_a_null_newest_balance_checks_the_newest_one_that_has_one():
    snaps = [snap("2026-09-20T12:00:00+00:00", 100.0), snap("2026-09-22T12:00:00+00:00", 90.0),
             snap("2026-09-24T12:00:00+00:00", None)]
    rows = [{"account_id": "s", "date": "2026-09-21", "amount": 10.0}]
    r = check.snapshot_check(snaps, rows)
    assert r.ok is True and "current 90.00" in r.detail


def test_only_null_balances_is_unchecked_not_a_crash():
    r = check.snapshot_check([snap("2026-09-20T12:00:00+00:00", None), snap("2026-09-22T12:00:00+00:00", None)], [])
    assert r.ok is None and "no current balance" in r.detail


def test_run_survives_a_null_current_balance(tmp_path):
    from penny import plaid
    store = plaid.Store(tmp_path, "sandbox")
    store.save_items({"i": {"access_token": "t"}})
    store.append_balances([snap("2026-09-20T12:00:00+00:00", None)])
    (r,) = check.run(store)
    assert r.ok is None


def test_missing_pdftotext_is_one_error_not_a_crash(tmp_path, monkeypatch):
    for n in ("a.pdf", "b.pdf"):
        (tmp_path / n).write_bytes(b"%PDF-")

    def run(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "pdftotext")

    monkeypatch.setattr(statements.subprocess, "run", run)
    out, errors = statements.load_dir(tmp_path)
    assert out == [] and errors == ["PDFs not read: pdftotext not found (install poppler)"]
