"""penny demo: a synthetic instance every board page has something on."""

import json
import threading
from datetime import date
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from penny import __main__ as cli
from penny import board, check, demo, feed, home, model

TODAY = date(2026, 9, 13)
PAGES = ["/", "/accounts", "/cashflow", "/cards", "/memberships", "/todo", "/transactions", "/budget", "/recurring", "/checks", "/data"]


@pytest.fixture(scope="module")
def inst(tmp_path_factory):
    return demo.build(tmp_path_factory.mktemp("demo") / "home", seed=7, today=TODAY)


def _ledgers(d):
    plaid_dir = d / "data" / "plaid" / "production"
    return {p.name: json.loads(p.read_text()) for p in sorted(plaid_dir.glob("demo-*.json"))}


def test_same_seed_same_rows_and_another_seed_differs(inst, tmp_path):
    assert _ledgers(demo.build(tmp_path / "a", seed=7, today=TODAY)) == _ledgers(inst)
    assert _ledgers(demo.build(tmp_path / "b", seed=8, today=TODAY)) != _ledgers(inst)


def test_a_year_ending_today_over_every_held_membership(inst):
    rules = home.load_rules(inst / "rules.toml")
    txns = feed.labelled(inst, rules)
    assert txns[-1].date == TODAY and (TODAY - txns[0].date).days == 364
    spend = model.spend_by_family(txns)
    for m in rules["held"]["memberships"]:
        assert any(spend.get(f, 0) > 0 for f in rules["memberships"][m]["families"]), m
    assert set(model.observed_fees(txns, rules)) == set(rules["held"]["memberships"])
    for k in rules["held"]["cards"]:
        assert k in rules["cards"]


def test_every_balance_check_ties(inst):
    c = check.run_all(inst, home.load_rules(inst / "rules.toml"))
    assert c.results and all(r.ok for r in c.results), [r.detail for r in c.results if not r.ok]


def test_refuses_a_directory_that_holds_an_instance(tmp_path):
    home.init(tmp_path)
    with pytest.raises(FileExistsError):
        demo.build(tmp_path, today=TODAY)
    assert "demo" not in (tmp_path / "rules.toml").read_text()


def test_cli_no_board_writes_only_into_the_given_home(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "inst"
    cli.main(["--home", str(target), "demo", "--no-board", "--seed", "1"])
    assert str(target) in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["inst"]
    with pytest.raises(SystemExit):
        cli.main(["--home", str(target), "demo", "--no-board"])


def test_cli_without_home_uses_a_new_temp_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    cli.main(["demo", "--no-board"])
    made = list((tmp_path / "tmp").iterdir())
    assert len(made) == 1 and made[0].name.startswith("penny-demo-") and (made[0] / "rules.toml").exists()
    assert not (tmp_path / "home").exists()


def test_every_board_page_has_content(inst):
    cfg = board.Config(inst, inst / "rules.toml", inst / "assumptions.toml", None, None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(cfg, today=lambda: TODAY))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = HTTPConnection("127.0.0.1", srv.server_address[1])
        bodies = {}
        for path in PAGES:
            c.request("GET", path)
            r = c.getresponse()
            body = r.read().decode()
            assert r.status == 200, path
            assert "Traceback" not in body, path
            bodies[path] = body
    finally:
        srv.shutdown()
        srv.server_close()
    s = board.base_state(cfg, TODAY)
    assert s.decisions and s.verdicts and s.txns
    for path, text in {"/accounts": "Savings", "/recurring": "NETFLIX.COM", "/budget": "Groceries",
                       "/transactions": "COSTCO WHSE", "/memberships": "Walmart+", "/checks": "6 tie",
                       "/cashflow": "Spent"}.items():
        assert text in bodies[path], path
