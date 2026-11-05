"""The worth-it layer (penny/worth.py) and the capture cache (penny/captures.py):
the board and `penny report` read the same inputs, the shortcuts the layer
takes agree with the long way round, and a capture is parsed once per
version of its file."""

import copy
import json
import os
import shutil
from datetime import date

import pytest
from feedfix import install
from test_board import ASSUMPTIONS, FIX, INSTANCE, _synthetic
from test_board_pages import _wallet

from penny import __main__ as cli
from penny import (
    board,
    captures,
    cardworth,
    costco,
    home,
    model,
    report,
    walmart,
    worth,
)
from penny.model import LEVELS

TODAY = date(2026, 9, 13)


@pytest.fixture
def inst(tmp_path):
    install(tmp_path)
    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "assumptions.toml").write_text(ASSUMPTIONS)
    return tmp_path


def _rules(inst):
    return model.resolve_points(home.load_rules(inst / "rules.toml"))


def _assumptions(inst):
    return cli._toml(inst / "assumptions.toml")


# --------------------------------------------------------------------------
# One loader


def test_board_state_is_what_worth_load_reads(inst):
    rules, assumptions = _rules(inst), _assumptions(inst)
    i = worth.load(inst, rules, assumptions)
    s = board.load_state(board.Config(inst, inst / "rules.toml", inst / "assumptions.toml"), TODAY)
    assert s.spend == i.spend and s.window == i.window and s.n_txns == len(i.tx)
    assert s.carried == i.carried and s.credit_years == i.credit_years
    assert [w.worth for w in s.worth] == [w.worth for w in i.worth]


def test_report_and_board_print_the_same_verdicts(inst, capsys):
    cli.main(["--home", str(inst), "report"])
    out = capsys.readouterr().out
    s = board.load_state(board.Config(inst, inst / "rules.toml", inst / "assumptions.toml"), TODAY)
    for v in s.analysis.verdicts.values():
        assert report.money(v.net["base"]) in out


def test_analyze_evaluates_once_and_every_part_reads_it(inst, monkeypatch):
    rules, assumptions = _rules(inst), _assumptions(inst)
    i = worth.load(inst, rules, assumptions)
    calls = []
    real = model.evaluate
    monkeypatch.setattr(model, "evaluate", lambda *a, **k: calls.append(1) or real(*a, **k))
    a = worth.analyze(i, [], TODAY, inst)
    # Once for the verdicts; the ¢/pt swing (a baseline rate change, not linear) re-runs it twice.
    assert len(calls) == 3
    assert {r.membership for r in a.rows} == set(rules["memberships"])
    assert a.cards.cards and a.cards.swipe


def test_load_with_no_rows_says_how_to_get_some(tmp_path):
    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "data").mkdir()
    with pytest.raises(FileNotFoundError, match="penny import csv"):
        worth.load(tmp_path, _rules(tmp_path), {})


# --------------------------------------------------------------------------
# Shortcuts agree with the long way round


def _perk_swing_by_evaluation(rules, assumptions, spend, mkey, pkey, frm, to):
    """What perk_swing used to do: evaluate twice on mutated copies."""
    def net(level):
        a = copy.deepcopy(assumptions)
        a[mkey][pkey]["base"] = a[mkey][pkey][level]
        return worth.by_key(model.evaluate(rules, a, spend, 1.0))[worth.headline_key(rules, mkey)].net["base"]
    return net(to) - net(frm)


@pytest.mark.parametrize(("pkey", "frm", "to"), [("rx", "low", "high"), ("delivery", "base", "high"), ("rx", "base", "low")])
def test_perk_swing_is_what_re_evaluating_gives(pkey, frm, to):
    rules, assumptions, spend = _synthetic()
    got = worth.perk_swing(rules, assumptions, spend, 1.0, "shop", pkey, frm, to)
    assert got == pytest.approx(_perk_swing_by_evaluation(rules, assumptions, spend, "shop", pkey, frm, to))


def test_flip_point_is_where_the_margin_crosses_zero():
    rules, assumptions, spend = _wallet()
    rules["cards"]["pts"]["caps"] = [{"families": ["restaurants"], "annual": 600, "then": 0.02}]
    c = worth.flip_point(rules, assumptions, spend)
    assert c is not None and 0.01 < c < 0.03
    assert worth.margin_at(rules, assumptions, spend, c) == pytest.approx(0.0, abs=1e-9)
    # A straight line: either side of it has the margin's two signs.
    assert worth.margin_at(rules, assumptions, spend, c - 0.001) * worth.margin_at(rules, assumptions, spend, c + 0.001) < 0


def test_point_range_is_the_one_reader():
    rules, assumptions, _ = _wallet()
    assert model.point_values(rules, assumptions) == model.point_range(rules, assumptions) == {"low": 0.01, "base": 0.02, "high": 0.03}
    assert cardworth.card_point_values(rules, assumptions, "flat") == {lvl: 0.02 for lvl in LEVELS}
    rules["cards"]["pooled"] = {"points_of": "pts", "default_rate": 0.03}
    assert model.point_range(rules, assumptions, "pooled") == model.point_range(rules, assumptions, "pts")


@pytest.mark.parametrize(("note", "recorded", "on", "placeholder"), [
    ("Recorded 2026-09-14: $0 a year", True, "2026-09-14", False),
    ("PLACEHOLDER: price both scripts", False, "", True),
    ("Recorded by hand", True, "", False),
    ("zero if you'd pick up", False, "", False),
    (None, False, "", False),
])
def test_perk_state_reads_the_note(note, recorded, on, placeholder):
    p = {"low": 0, "base": 0, "high": 0} | ({"note": note} if note is not None else {})
    assert worth.perk_state(p) == worth.PerkState(recorded, on, placeholder)


# --------------------------------------------------------------------------
# Captures: parsed once per version of the file


def _listing(total):
    return [{"url": "https://www.walmart.com/orchestra/cph/graphql/PurchaseHistoryV3/x",
             "body": {"data": {"purchaseHistory": {"orders": [
                 {"id": "a", "orderDate": "2026-01-01T10:00:00", "priceDetails": {"orderTotal": {"value": total}}},
                 {"id": "b", "orderDate": "2026-12-31T10:00:00", "priceDetails": {"orderTotal": {"value": 100.0}}}]}}}}]


def test_a_capture_is_parsed_once_until_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "walmart-orders-2026-12-31.json"
    path.write_text(json.dumps(_listing(500.0)))
    loads = []
    real = json.load
    monkeypatch.setattr(captures.json, "load", lambda f: loads.append(1) or real(f))
    first = walmart.newest_measured(tmp_path)
    again = walmart.newest_measured(tmp_path)
    assert first.total == again.total == 600.0 and len(loads) == 1
    walmart.load_lines(path)  # a second reader of the same file parses it for itself
    assert len(loads) == 2
    path.write_text(json.dumps(_listing(900.0)))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))  # a rewrite within the clock's tick still reads anew
    assert walmart.newest_measured(tmp_path).total == 1000.0 and len(loads) == 3


def test_newest_capture_wins_and_none_is_none(tmp_path):
    assert walmart.newest_measured(tmp_path) is None and costco.newest_measured(tmp_path) is None
    (tmp_path / "walmart-orders-2026-01-01.json").write_text(json.dumps(_listing(1.0)))
    (tmp_path / "walmart-orders-2026-12-31.json").write_text(json.dumps(_listing(500.0)))
    assert walmart.newest_measured(tmp_path).path.name == "walmart-orders-2026-12-31.json"


def test_the_cache_keeps_only_a_few_products(tmp_path):
    for n in range(captures.KEEP + 3):
        p = tmp_path / f"c{n}.json"
        p.write_text("[]")
        captures.read(p, len)
    assert len(captures._CACHE) <= captures.KEEP


# --------------------------------------------------------------------------
# A capture measures and cross-checks; it never replaces spend


def test_a_walmart_capture_leaves_the_feeds_spend_alone(inst):
    rules, assumptions = _rules(inst), _assumptions(inst)
    before = worth.load(inst, rules, assumptions)
    (inst / "data" / "walmart-orders-2026-12-31.json").write_text(json.dumps(_listing(5000.0)))
    after = worth.load(inst, rules, assumptions)
    assert after.spend == before.spend and after.walmart_measured.total == 5100.0
    # The cross-check: the feed's own Walmart rows over the capture's days, beside it.
    assert after.walmart_feed_span == pytest.approx(sum(t.amount for t in after.tx if t.family in (
        worth.WALMART_FAMILY, worth.WALMART_ONLINE, worth.WALMART_STORE)))


def test_report_on_an_export_says_walmart_is_a_floor(inst, capsys):
    cli.main(["--home", str(inst), "report", str(FIX)])
    assert worth.WALMART_FLOOR in capsys.readouterr().out
    cli.main(["--home", str(inst), "report"])
    assert worth.WALMART_FLOOR not in capsys.readouterr().out


def test_the_board_has_no_export_mode():
    with pytest.raises(SystemExit):
        cli.main(["board", "--export", str(FIX)])
