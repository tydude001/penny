import json
import re
import shutil
import threading
from datetime import date
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from feedfix import install

from penny import board, home
from penny import load as ld

INSTANCE = Path(__file__).parent / "fixtures" / "home"  # a test instance, read over penny/defaults
FIX = Path(__file__).parent / "fixtures" / "sample.csv"
TODAY = date(2026, 9, 13)


def _synthetic():
    """One rules/assumptions pair that fires every trigger in the plan's table."""
    rules = {
        "held": {"baseline_card": "flat", "cards": ["flat", "shop_card"], "warehouse_tier": "gold", "club_tier": "basic"},
        "families": {
            "shop": {"label": "Shop", "membership": "shop"},
            "rx": {"label": "Rx", "membership": "shop"},
            "warehouse": {"label": "Warehouse", "membership": "warehouse"},
            "club": {"label": "Club", "membership": "club"},
            "gas": {"label": "Gas"},
        },
        "cards": {
            "flat": {"label": "Flat", "verified": False, "default_rate": 0.015, "rates": {"restaurants": 0.045}},
            "shop_card": {"label": "Shop Card", "verified": False, "requires_membership": False, "default_rate": 0.01, "rates": {"shop": 0.05}, "without_membership_rates": {"shop": 0.03}},
            "wh_card": {"label": "Warehouse Card", "verified": True, "requires_membership": True, "default_rate": 0.01, "rates": {"gas": 0.04}},
        },
        "memberships": {
            "shop": {"label": "Shop+", "fee": 100, "families": ["shop", "rx"], "card": "shop_card"},
            "warehouse": {"label": "Warehouse", "families": ["warehouse"], "card": "wh_card", "tiers": {"gold": {"fee": 60}, "exec": {"fee": 120, "reward_rate": 0.02, "reward_cap": 1000}}},
            "club": {"label": "Club", "families": ["club"], "tiers": {"basic": {"fee": 50}, "plus": {"fee": 100, "reward_rate": 0.05, "reward_cap": 5000}}},
        },
    }
    assumptions = {
        "shop": {
            "rx": {"note": "PLACEHOLDER: price it", "low": 0, "base": 180, "high": 600},
            "delivery": {"note": "zero if you'd pick up", "low": 0, "base": 0, "high": 450},
            "video": {"note": "", "low": 0, "base": 0, "high": 0},
        },
        "warehouse": {"prices": {"note": "", "low": 0, "base": 0, "high": 0}},
    }
    spend = {"shop": 2000.0, "gas": 1000.0, "warehouse": 3000.0, "club": 4000.0}
    return rules, assumptions, spend


WIKI = """\
### Waiting on Alex

| Item | Detail |
|------|--------|
| **penny — call the bank** <!-- id:penny-call --> | **Alex:** call before 2026-10-01 (else to ~2026-11-24). See `docs/x.md`. |
| **penny — spend caps** <!-- id:penny-caps --> | Dormant; last looked 2026-09-01. |
| **other — not ours** <!-- id:lucid-voice --> | 2026-12-01 |
"""


def test_every_trigger_fires_with_kind_swing_and_order():
    rules, assumptions, spend = _synthetic()
    found = {"shop": [ld.Txn(date(2026, 8, 1), "Shop+ fee", 110.0, "Shopping")]}
    ds = board.rank(board.derive(rules, assumptions, spend, 1.0, found, board.parse_wiki(WIKI), TODAY))
    got = {d.key: d for d in ds}
    assert [d.key for d in ds] == [
        "wiki:penny-call",  # dated wiki item pinned on top
        "perk:shop.rx",  # 600
        "perk:shop.delivery",  # 450
        "tier:club",  # 150
        "verify:shop_card",  # 70
        "apply:wh_card:warehouse",  # 25
        "fee:shop",  # 10
        "cpp:flat",  # 5 per half cent
        "wiki:penny-caps",  # not modelled
        "tier:warehouse",  # settled: a wash
        "perks:zero",  # settled: defaults hold
    ]
    kinds = {k: d.kind for k, d in got.items()}
    assert kinds["perk:shop.rx"] == "placeholder" and kinds["perk:shop.delivery"] == "choice"
    assert kinds["verify:shop_card"] == "verify" and kinds["cpp:flat"] == "baseline"
    assert kinds["apply:wh_card:warehouse"] == "apply" and kinds["fee:shop"] == "fee"
    assert got["perk:shop.rx"].swing == pytest.approx(600)
    assert got["perk:shop.delivery"].swing == pytest.approx(450)
    assert got["tier:club"].swing == pytest.approx(150) and not got["tier:club"].settled
    assert got["verify:shop_card"].swing == pytest.approx(70)  # 5% vs 1.5% on $2,000
    assert got["apply:wh_card:warehouse"].swing == pytest.approx(25)  # 4% vs 1.5% on $1,000 gas
    assert got["apply:wh_card:warehouse"].resolver is None  # verified, not held: numbers only
    assert got["fee:shop"].swing == pytest.approx(10) and got["fee:shop"].data["value"] == 110.0
    assert got["cpp:flat"].swing == pytest.approx(5)
    assert got["tier:warehouse"].settled and got["tier:warehouse"].swing == pytest.approx(0)
    assert "3,000" in got["tier:warehouse"].sub  # break-even spend is the trigger text
    assert got["perks:zero"].settled and len(got["perks:zero"].data["perks"]) == 2
    assert got["wiki:penny-call"].due == date(2026, 10, 1)
    assert "wiki:lucid-voice" not in got


def test_negative_swing_keeps_its_sign():
    rules, assumptions, spend = _synthetic()
    found = {"shop": [ld.Txn(date(2026, 8, 1), "Shop+ fee", 8.33, "Shopping")]}  # monthly billing
    fee = next(d for d in board.derive(rules, assumptions, spend, 1.0, found, [], TODAY) if d.kind == "fee")
    assert fee.swing == pytest.approx(8.33 - 100)
    assert any("monthly" in x for x in fee.explain)


def test_recorded_choice_closes_and_held_card_verify_closes():
    rules, assumptions, spend = _synthetic()
    assumptions["shop"]["delivery"]["note"] = "Recorded 2026-09-13: I'd drive"
    assumptions["shop"]["rx"]["note"] = "Recorded 2026-09-13: priced at GoodRx"
    rules["cards"]["shop_card"]["verified"] = True
    keys = {d.key for d in board.derive(rules, assumptions, spend, 1.0, {}, [], TODAY)}
    assert not keys & {"perk:shop.rx", "perk:shop.delivery", "verify:shop_card"}


def test_swing_engine_perk_low_to_high():
    rules, _, spend = _synthetic()
    assumptions = {"shop": {"shipping": {"low": 0, "base": 50, "high": 100}}}
    assert board.perk_swing(rules, assumptions, spend, 1.0, "shop", "shipping") == pytest.approx(100)


def test_verdict_line_names_the_perk_that_flips_it():
    rules, assumptions, spend = _synthetic()
    rows = {r.title: r for r in board.verdict_rows(rules, assumptions, spend, 1.0)}
    shop = rows["Shop+ + Shop Card"]
    assert (shop.low, shop.base) == (pytest.approx(-60), pytest.approx(120))  # 40 from membership + perks − 100
    assert shop.verdict == "keep, if Rx is worth $60+ a year"
    assert rows["Warehouse Gold"].verdict == "keep or drop: can't tell"  # no perk valued


def test_wiki_parse_and_countdown():
    rows = board.parse_wiki(WIKI)
    assert [(r.slug, r.item, r.heading) for r in rows] == [
        ("penny-call", "penny — call the bank", "Waiting on Alex"),
        ("penny-caps", "penny — spend caps", "Waiting on Alex"),
    ]
    assert board.future_dates(rows[0].detail, TODAY) == [date(2026, 10, 1), date(2026, 11, 24)]
    assert board.future_dates(rows[1].detail, TODAY) == []  # a past date doesn't pin
    d = board.external_decision(rows[0], TODAY, None)
    assert (d.due - TODAY).days == 18
    assert d.data["docs"] == ["docs/x.md"]


@pytest.mark.parametrize(
    "write",
    [
        {"file": "rules", "section": "held", "key": "baseline_card", "value": "baseline"},
        {"file": "rules", "section": "cards.prime_visa", "key": "verified", "value": False},
        {"file": "rules", "section": "cards.prime_visa", "key": "default_rate", "value": 0.02},  # not the baseline
        {"file": "rules", "section": "held", "key": "costco_tier", "value": "platinum"},
        {"file": "assumptions", "section": "shop.rx", "key": "base", "value": "lots"},
        {"file": "assumptions", "section": "shop.rx", "key": "bonus", "value": 1},
        {"file": "elsewhere", "section": "shop.rx", "key": "base", "value": 1},
    ],
)
def test_allowlist_refuses(write):
    rules = home.load_rules(INSTANCE / "rules.toml")
    with pytest.raises(board.Refused, match="Allowed"):
        board.check_write(write, rules)


ASSUMPTIONS = """\
# Synthetic perks for the handler test.

[prime.rxpass]
note = "PLACEHOLDER: price both scripts"   # replace me
low = 0
base = 180
high = 600

[walmart_plus.delivery]
note = "zero if you'd pick up"
low = 0
base = 0
high = 450
"""


def _serve(tmp_path, export):
    shutil.copy(INSTANCE / "rules.toml", tmp_path / "rules.toml")
    (tmp_path / "assumptions.toml").write_text(ASSUMPTIONS)
    cfg = board.Config(tmp_path, tmp_path / "rules.toml", tmp_path / "assumptions.toml", None, export)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(cfg, today=lambda: TODAY))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield tmp_path, HTTPConnection("127.0.0.1", srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def server(tmp_path):
    """The board on the feed, as penny-board runs."""
    install(tmp_path)
    yield from _serve(tmp_path, None)


@pytest.fixture
def export_server(tmp_path):
    """The board on the Rocket Money export (``--export``), kept while the exports are."""
    yield from _serve(tmp_path, FIX)


def _post(c, body):
    c.request("POST", "/record", json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def _get(c, path="/"):
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.read().decode()


def test_record_writes_the_file_and_the_page_shows_it(server):
    root, c = server
    before = (root / "assumptions.toml").read_text()
    status, page = _get(c, "/todo")
    assert status == 200 and "Replace the RxPass placeholder" in page

    status, res = _post(c, [{"file": "assumptions", "section": "prime.rxpass", "key": "base", "value": 240}, {"file": "assumptions", "section": "prime.rxpass", "key": "low", "value": 0}])
    assert status == 200 and [r["key"] for r in res["recorded"]] == ["base"]  # low unchanged: no write, no audit
    after = (root / "assumptions.toml").read_text()
    changed = [(a, b) for a, b in zip(before.split("\n"), after.split("\n")) if a != b]
    assert changed == [("base = 180", "base = 240")]
    status, page = _get(c, "/todo")
    assert "data-section='prime.rxpass' data-key=base data-type=number value='240'" in page

    status, res = _post(c, {"file": "assumptions.toml", "section": "prime.rxpass", "key": "note", "value": "GoodRx, both scripts", "note": "priced"})
    assert status == 200
    text = (root / "assumptions.toml").read_text()
    assert 'note = "Recorded 2026-09-13: GoodRx, both scripts"   # replace me' in text
    status, page = _get(c, "/todo")
    assert "Replace the RxPass placeholder" not in page  # trigger gone, decision closed

    audit = [json.loads(x) for x in (root / "data" / "board-decisions.jsonl").read_text().splitlines()]
    assert [(a["section"], a["key"], a["before"], a["after"]) for a in audit] == [
        ("prime.rxpass", "base", 180, 240),
        ("prime.rxpass", "note", "PLACEHOLDER: price both scripts", "Recorded 2026-09-13: GoodRx, both scripts"),
    ]
    assert audit[1]["note"] == "priced"


def test_disallowed_or_missing_key_is_400_and_nothing_changes(server):
    root, c = server
    rules, assumptions = (root / "rules.toml").read_text(), (root / "assumptions.toml").read_text()
    status, res = _post(c, {"file": "rules", "section": "held", "key": "baseline_card", "value": "baseline"})
    assert status == 400 and "Allowed" in res["error"]
    # a batch with one bad write changes nothing, not even the good one
    status, res = _post(c, [{"file": "assumptions", "section": "prime.rxpass", "key": "base", "value": 1}, {"file": "rules", "section": "cards.prime_visa", "key": "verified", "value": False}])
    assert status == 400
    status, res = _post(c, {"file": "assumptions", "section": "prime.shipping", "key": "base", "value": 5})
    assert status == 400 and "never adds a key" in res["error"]
    assert (root / "rules.toml").read_text() == rules and (root / "assumptions.toml").read_text() == assumptions
    assert not (root / "data" / "board-decisions.jsonl").exists()


def test_rules_write_keeps_comments(server):
    root, c = server
    before = (root / "rules.toml").read_text()
    rules = home.load_rules(root / "rules.toml")
    section = f"cards.{rules['held']['baseline_card']}"  # values read, not assumed: the instance sets the ¢/pt
    status, _ = _post(c, [{"file": "rules", "section": section, "key": "default_rate", "value": 0.0123}, {"file": "rules", "section": section, "key": "rates", "subkey": "restaurants", "value": 0.0369}])
    assert status == 200
    after = (root / "rules.toml").read_text()
    changed = [(a, b) for a, b in zip(before.split("\n"), after.split("\n")) if a != b]
    assert len(changed) == 2 and changed[0][1] == "default_rate = 0.0123"
    old_rates, new_rates = changed[1]  # other rates on the line (the Preferred's gas) stay as they were
    assert new_rates == re.sub(r"restaurants = [\d.]+", "restaurants = 0.0369", old_rates) and new_rates != old_rates
    assert len(before.split("\n")) == len(after.split("\n"))


def test_verdict_why_names_only_what_the_net_is_made_of():
    rules, assumptions, spend = _synthetic()
    assumptions["shop"]["rx"] = {"note": "", "low": 0, "base": 0, "high": 600}  # no perk at base
    rows = {r.title: r for r in board.verdict_rows(rules, assumptions, spend, 1.0)}
    shop = rows["Shop+ + Shop Card"]  # 40 from membership − 100 fee
    assert shop.verdict == "drop, unless Rx is worth $60+ a year"
    assert shop.why.startswith("$40 of Shop Card cash back that needs Shop+ against the $100 fee: −$60.")
    assert "perk" not in shop.why
    assumptions["shop"]["rx"]["base"] = 180
    shop = {r.title: r for r in board.verdict_rows(rules, assumptions, spend, 1.0)}["Shop+ + Shop Card"]
    assert "of which" not in shop.why and shop.why.startswith("Net +$120 counts Rx at $180.")
    assert rows["Warehouse Gold"].why == "No perk has a value yet; nothing offsets the $60 fee: −$60."


def test_without_membership_rates_keep_the_other_rates():
    rules, assumptions, spend = _synthetic()
    rules["cards"]["shop_card"]["rates"]["gas"] = 0.02
    d = next(d for d in board.derive(rules, assumptions, spend, 1.0, {}, [], TODAY) if d.key == "verify:shop_card")
    assert "Without the membership: 3% Shop · 2% Gas · 1% everything else." in d.explain


def test_walmart_capture_replaces_the_exports_floor(export_server):
    root, c = export_server
    status, page = _get(c, "/data")
    assert status == 200 and "a floor" in page and "Capture Walmart order history" in page
    (root / "data").mkdir()
    listing = {"url": "https://www.walmart.com/orchestra/cph/graphql/PurchaseHistoryV3/x",
               "body": {"data": {"purchaseHistory": {"orders": [
                   {"id": "a", "orderDate": "2026-01-01T10:00:00", "priceDetails": {"orderTotal": {"value": 500.0}}},
                   {"id": "b", "orderDate": "2026-12-31T10:00:00", "priceDetails": {"orderTotal": {"value": 1000.0}}}]}}}}
    (root / "data" / "walmart-orders-2026-12-31.json").write_text(json.dumps([listing]))
    s = board.load_state(board.Config(root, root / "rules.toml", root / "assumptions.toml", None, FIX), TODAY)
    assert s.walmart_measured.orders == 2
    assert s.spend[board.WALMART_FAMILY] * s.window.annualize == pytest.approx(1500.0)
    status, page = _get(c, "/data")
    assert status == 200 and "a floor" not in page and "$1,500 a year" in page and "walmart-orders-2026-12-31.json" in page


def test_refundable_upgrade_turns_a_wash_into_take_it_at_renewal():
    rules, assumptions, spend = _synthetic()
    rules["memberships"]["warehouse"]["tiers"]["exec"]["refundable"] = True
    t = next(d for d in board.derive(rules, assumptions, spend, 1.0, {}, [], TODAY) if d.key == "tier:warehouse")
    assert t.settled and t.data["refund"] and t.data["best"] == "exec"
    assert "at renewal" in t.title and "worst case is $0" in t.sub
    rules["memberships"]["warehouse"]["tiers"]["exec"]["fee"] = 300  # far behind: refund or not, no reason to bother
    t = next(d for d in board.derive(rules, assumptions, spend, 1.0, {}, [], TODAY) if d.key == "tier:warehouse")
    assert not t.data["refund"] and "at renewal" not in t.title


def test_measured_answer_is_one_click_and_costco_receipts_show_in_the_rail(server):
    root, c = server
    (root / "assumptions.toml").write_text(ASSUMPTIONS + '\n[costco.prices]\nlow_if = "No real saving"\nmeasured = 80\nmeasured_if = "Move the beef"\nlow = 0\nbase = 0\nhigh = 0\n')
    status, page = _get(c, "/todo")
    assert status == 200 and "Move the beef" in page and "data-fill='80'" in page
    status, page = _get(c, "/data")
    assert "receipts</h2>" not in page
    (root / "data").mkdir(exist_ok=True)  # the fixture feed already made it
    receipt = {"body": {"data": {"receiptsWithCounts": {"receipts": [
        {"transactionBarcode": "A", "transactionDate": "2026-01-01", "receiptType": "In-Warehouse", "subTotal": 100.0, "total": 108.0, "itemArray": []},
        {"transactionBarcode": "B", "transactionDate": "2026-12-31", "receiptType": "In-Warehouse", "subTotal": 200.0, "total": 216.0, "itemArray": []}]}}}}
    (root / "data" / "costco-receipts-2026-12-31.json").write_text(json.dumps([receipt]))
    status, page = _get(c, "/data")
    assert status == 200 and "receipts</h2>" in page and "$300 a year before tax" in page and "costco-receipts-2026-12-31.json" in page


def test_walmart_capture_stands_in_for_the_exports_other_walmart_rows(tmp_path):
    rules = home.load_rules(INSTANCE / "rules.toml")
    (tmp_path / "data").mkdir()
    (tmp_path / "rules.toml").write_text((INSTANCE / "rules.toml").read_text())
    (tmp_path / "assumptions.toml").write_text(ASSUMPTIONS)
    cfg = board.Config(tmp_path, tmp_path / "rules.toml", tmp_path / "assumptions.toml", None, FIX)
    before = board.load_state(cfg, TODAY)
    others = {f: before.spend.get(f, 0.0) for f in (board.WALMART_ONLINE, board.WALMART_STORE)}
    assert all(f in rules["families"] for f in others)
    listing = {"url": "https://www.walmart.com/orchestra/cph/graphql/PurchaseHistoryV3/x",
               "body": {"data": {"purchaseHistory": {"orders": [
                   {"id": "a", "orderDate": "2099-01-01T10:00:00", "priceDetails": {"orderTotal": {"value": 500.0}}}]}}}}
    (tmp_path / "data" / "walmart-orders-2099-01-01.json").write_text(json.dumps([listing]))
    s = board.load_state(cfg, TODAY)
    # A capture after every export row: Walmart.com and store rows are all months its scaling covers.
    assert board.WALMART_ONLINE not in s.spend and board.WALMART_STORE not in s.spend
    assert s.walmart_dropped == pytest.approx(sum(others.values()))


def test_a_settled_verdict_names_its_terms_instead_of_a_zero_width_range():
    rules, assumptions, spend = _synthetic()
    v = next(v for v in board.model.evaluate(rules, assumptions, spend, 1.0) if v.membership == "club")
    v.net = {"low": 40.0, "base": 40.0, "high": 40.0}
    word, _, why = board.verdict_line(rules, assumptions, v)
    assert word == "keep" and "No input left to guess at" in why and "Positive at low" not in why


def test_sync_alert_says_when_the_daily_sync_failed_or_stopped(tmp_path):
    import json as _json
    from datetime import UTC, datetime

    from penny.board import sync_alert
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    assert sync_alert(tmp_path, now) is None  # Plaid never set up
    d = tmp_path / "data" / "plaid" / "production"
    d.mkdir(parents=True)
    (d / "last-sync.json").write_text(_json.dumps({"at": "2026-09-27T06:00:00+00:00", "ok": True, "items": {"Chase": "ok"}}))
    assert sync_alert(tmp_path, now) is None
    assert "since" in sync_alert(tmp_path, datetime(2026, 9, 29, tzinfo=UTC))
    (d / "last-sync.json").write_text(_json.dumps({"at": "2026-09-27T06:00:00+00:00", "ok": False,
                                                   "items": {"Chase": "ok", "OnePay": "ITEM_LOGIN_REQUIRED"}}))
    msg = sync_alert(tmp_path, now)
    assert "failed" in msg and "OnePay: ITEM_LOGIN_REQUIRED" in msg and "Chase" not in msg
