"""CLI: penny init [DIR], penny inspect|unmatched|families|report|compare EXPORT.csv, penny walmart|costco CAPTURE.json, penny board, penny plaid, penny import apple, penny check

Every file penny keeps is in the instance: --home, else $PENNY_HOME, else ./home."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from . import (
    apple,
    board,
    cardworth,
    check,
    compare,
    costco,
    csvimport,
    demo,
    feed,
    home,
    model,
    plaid,
    report,
    walmart,
)
from . import categorize as cat
from . import load as ld


def _toml(p: str | Path) -> dict:
    with open(p, "rb") as f:
        return tomllib.load(f)


def _rules(args) -> dict:
    """The instance's rules.toml (or --rules) merged over penny's defaults."""
    return home.load_rules(args.rules)


def _prep(args):
    rules = model.resolve_points(_rules(args))
    fams = cat.build_families(rules)
    if args.export is None:
        # No file: the instance's feed (Plaid, Apple Card, penny import csv).
        txns = feed.labelled(args.home, rules, getattr(args, "env", "production"))
        if not txns:
            sys.exit(f"no transactions in {args.home / 'data'}; penny import csv FILE, or pass an export")
    else:
        ex = rules.get("export", {})
        txns = ld.load(args.export, expense_sign=ex.get("expense_sign", "positive"), exclude_categories=ex.get("exclude_categories", []))
        cat.categorize(txns, fams)
    labels = {f.key: f.label for f in fams}
    labels[cat.OTHER] = "other"
    return rules, txns, fams, labels


def cmd_inspect(args):
    info = ld.inspect(args.export)
    print(f"columns: {info['columns']}")
    print(f"rows: {info['rows']}  range: {info['first']} → {info['last']}")
    print(f"income-like rows: {info['income_rows']}, sum {info['income_sum']:,.2f} → expense_sign guess: {info['expense_sign_guess']}")
    print("categories:")
    for c, n in info["categories"]:
        print(f"  {n:5d}  {c}")


def cmd_unmatched(args):
    _, txns, _, _ = _prep(args)
    w = model.pick_window(txns, year=args.year, days=args.days)
    rows = cat.unmatched(model.in_window(txns, w), top=args.top)
    print(report.table(["merchant", "spend", "n"], [[n, report.money(s), str(c)] for n, s, c in rows]))


def cmd_families(args):
    _, txns, _, labels = _prep(args)
    w = model.pick_window(txns, year=args.year, days=args.days)
    tx = model.in_window(txns, w)
    by: dict[str, dict[str, float]] = {}
    for t in tx:
        if t.amount > 0:
            by.setdefault(t.family, {}).setdefault(t.name, 0.0)
            by[t.family][t.name] += t.amount
    for fam, merchants in sorted(by.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"{labels.get(fam, fam)}  {report.money(sum(merchants.values()))}")
        for n, s in sorted(merchants.items(), key=lambda kv: -kv[1])[: args.top]:
            print(f"    {report.money(s):>10}  {n}")


def cmd_report(args):
    rules, txns, _fams, labels = _prep(args)
    assumptions = _toml(args.assumptions)
    model.validate_rules(rules, assumptions)
    w = model.pick_window(txns, year=args.year, days=args.days)
    tx = model.in_window(txns, w)
    spend = model.spend_by_family(tx)
    verdicts = model.evaluate(rules, assumptions, spend, w.annualize)
    fees = model.observed_fees(tx, rules)
    unverified = [k for k, c in rules.get("cards", {}).items() if not c.get("verified", False)]
    unverified += [k for k, m in rules.get("memberships", {}).items() if not m.get("verified", False)]
    pr = report.point_range_block(rules, assumptions, spend, w.annualize)
    extra = []
    if rules.get("card_credits"):
        # Credits hide in categories exclude_categories drops: re-read with none dropped.
        ctx = cardworth.load_for_credits(args.export, rules) if args.export else feed.labelled(args.home, rules, spend_only=False)
        extra.append(report.credits_block(rules, ctx, first=ctx[0].date, last=ctx[-1].date))
    alts = assumptions.get("card_worth", {}).get("alternatives", [])
    if alts:
        extra.append(report.card_worth_block(rules, assumptions, cardworth.worth_spend(tx, rules, alts[0]), w.annualize))
    print(report.render(w, len(tx), spend, labels, verdicts, fees, rules, unverified, pr, extra))


def cmd_walmart(args):
    with open(args.capture, encoding="utf-8") as f:
        capture = json.load(f)
    lines = walmart.lines_from_capture(capture)
    if not lines:
        sys.exit("no paid line items found -- was the capture taken with order details open?")
    items = walmart.rank(lines)
    orders = {ln.order_id for ln in lines}
    first, last = lines[0].date, lines[-1].date
    days = (last - first).days + 1
    share, opened, listed = walmart.coverage(capture, lines)
    # Skipped orders are assumed to look like the opened ones: scale by the share
    # of listed spend that was opened, then by the window to a year.
    annualize = 365.0 / days / share
    total = sum(it.spend for it in items)
    print(f"{len(orders)} orders opened, {first} -> {last} ({days} days), "
          f"{len(lines)} paid lines, {len(items)} distinct products, {report.money(total)}")
    if listed > opened:
        print(f"  {opened} of {listed} listed orders opened, {100 * share:.0f}% of listed spend; the rest is assumed alike")
    if days < 365:
        print(f"  under a year of orders: 'per yr' is this window x{annualize:.2f}, a guess, not a measurement")
    rows, cum = [], 0.0
    for i, it in enumerate(items[: args.top], 1):
        cum += it.spend
        rows.append([str(i), it.name[:52], it.shelf, str(it.orders), f"{it.qty:g}", report.money(it.spend),
                     f"{100 * cum / total:.0f}%", report.money(it.spend * annualize), it.unit_price])
    print(report.table(["#", "item", "shelf", "orders", "qty", "spend", "cum", "per yr", "unit price"], rows))
    by_shelf: dict[str, float] = {}
    for it in items:
        by_shelf[it.shelf] = by_shelf.get(it.shelf, 0.0) + it.spend
    print("by shelf guess: " + ", ".join(f"{k} {report.money(v)} ({100 * v / total:.0f}%)" for k, v in sorted(by_shelf.items(), key=lambda kv: -kv[1])))
    if args.sheet:
        walmart.write_sheet(items, args.sheet, annualize, args.top)
        print(f"sheet: {args.sheet} (top {args.top}, Costco columns blank)")


def cmd_costco(args):
    with open(args.capture, encoding="utf-8") as f:
        raw = costco.receipts(json.load(f))
    if not raw:
        sys.exit("no receipts found -- was the capture taken with receipts opened?")
    recs = costco.summary(raw)
    wh = [r for r in recs if r.warehouse]
    first, last = recs[0].date, recs[-1].date
    days = (last - first).days + 1
    annualize = 365.0 / days
    items = costco.rank(raw)
    total = sum(it.spend for it in items)
    print(f"{len(wh)} warehouse and {len(recs) - len(wh)} gas receipts, {first} -> {last} ({days} days), "
          f"{len(items)} distinct items, {report.money(total)} net of coupons")
    if days < 365:
        print(f"  under a year of receipts: 'per yr' is this window x{annualize:.2f}, a guess, not a measurement")
    rows, cum = [], 0.0
    for i, it in enumerate(items[: args.top], 1):
        cum += it.spend
        rows.append([str(i), it.name[:44], str(it.trips), f"{it.units:g}", report.money(it.spend),
                     f"{100 * cum / total:.0f}%", report.money(it.spend * annualize), f"${it.shelf_price:.2f}"])
    print(report.table(["#", "item", "trips", "units", "spend", "cum", "per yr", "shelf price"], rows))
    # The Executive 2% is paid on pre-tax warehouse purchases; gas earns nothing.
    tiers = _rules(args)["memberships"]["costco"]["tiers"]
    ex, gold = tiers["executive"], tiers["gold_star"]
    sub = sum(r.subtotal for r in wh) * annualize
    reward = min(ex["reward_rate"] * sub, ex["reward_cap"])
    delta = ex["fee"] - gold["fee"]
    print(f"warehouse pre-tax {report.money(sub)}/yr -> Executive reward {report.money(reward)} "
          f"against the {report.money(delta)} upgrade; break-even at {report.money(delta / ex['reward_rate'])}/yr")


def cmd_board(args):
    board.serve(
        args.home,
        export=Path(args.export) if args.export else None,
        host=args.host,
        port=args.port,
        todo_file=Path(args.todo_file).expanduser() if args.todo_file else None,
        rules=Path(args.rules),
        assumptions=Path(args.assumptions),
        tailscale=args.tailscale,
    )


def cmd_plaid(args):
    if args.action == "link":
        if args.env == "production" and not args.redirect_uri:
            sys.exit("production Link needs --redirect-uri https://...: an HTTPS address that reaches this link page "
                     "(a reverse proxy, or `tailscale serve`, in front of it), allowlisted in the Plaid Dashboard "
                     "under Allowed redirect URIs. OAuth banks send you back there.")
        plaid.serve_link(args.home, args.env, args.host, args.port, args.redirect_uri, args.update, args.relogin)
        return
    client, store = plaid.Client.from_root(args.home, args.env), plaid.Store(args.home, args.env)
    if args.action == "sandbox-item":
        if args.env != "sandbox":
            sys.exit("sandbox-item only works with --env sandbox")
        with store.sync_lock():
            item_id = plaid.sandbox_item(client, store)
            res = plaid.wait_ready(client, store, item_id)
        print(f"sandbox item {item_id}: {res['rows']} rows in {res['accounts']} accounts, status {res['status']}")
    elif args.action == "sync":
        # Exit status, for penny-sync.service: 0 all synced (or another sync
        # was already running and still is); 1 some failure may clear by itself
        # (network, Plaid or bank outage, rate limit, or an unexpected error),
        # so systemd tries again; 2 only failures you must fix, such as
        # ITEM_LOGIN_REQUIRED, which a retry can't clear (RestartPreventExitStatus=2).
        try:
            lines, ok = plaid.sync_all(client, store)
        except plaid.SyncBusy as e:
            print(f"{e}; not syncing again")
            return
        print("\n".join(lines))
        if not ok:
            sys.exit(1 if (store.last_sync() or {}).get("retry", True) else 2)
    print("\n".join(plaid.status(args.home, args.env)) or "no items linked")


def cmd_check(args):
    marks = {True: "✅", False: "❌", None: "·"}
    c = check.run_all(args.home, _rules(args), args.env)
    errors, results = c.errors, c.results
    for e in errors:
        print(f"❌ statement {e}")
    if not results:
        sys.exit("no balances yet; run penny plaid sync")
    for r in results:
        print(f"{marks[r.ok]} {r.account}: {r.detail}")
        for t in r.near:
            print(f"      {t['date']} {t['amount']:>10.2f} {t.get('name', '')}{' (pending)' if t.get('pending') else ''}")
        for t in r.inferred:
            print(f"      {t['date']} {t['amount']:>10.2f} {t['name']}")
    if errors or any(r.ok is False for r in results):
        sys.exit(1)


def cmd_compare(args):
    """Spend by family and by account on both sources, over the export's window."""
    rules, rm, _fams, labels = _prep(args)
    if args.export is None:  # the imported Rocket Money rows against the rest of the feed
        rm = [t for t in rm if t.source == "rocket_money"]
        if not rm:
            sys.exit("no Rocket Money rows imported; penny import csv EXPORT --preset rocket, or pass the export")
    # The feed side is Plaid and the Apple Card only, never an imported CSV.
    fd = [t for t in feed.labelled(args.home, rules, args.env) if t.source in ("plaid", "apple_csv")]
    end = min(rm[-1].date, fd[-1].date)
    w = model.Window(end - model.timedelta(days=args.days - 1), end)
    a, b = model.in_window(rm, w), model.in_window(fd, w)
    print(f"{w.start} → {w.end}: Rocket Money {len(a)} rows, feed {len(b)} rows (spend rows only)")

    def show(title, rows):
        print(f"\n{title}")
        print(report.table(["", "Rocket Money", "feed", "feed − RM"],
                           [[k, report.money(x), report.money(y), report.money(d)] for k, x, y, d in rows if abs(d) >= 1]))

    show("by family", [(labels.get(k, k), x, y, d) for k, x, y, d in compare.diff(compare.by_family(a), compare.by_family(b))])
    show("by account (last four)", compare.diff(compare.by_account(a), compare.by_account(b)))
    for fam in args.family or []:
        show(f"{labels.get(fam, fam)} by merchant", compare.diff(compare.by_merchant(a, fam), compare.by_merchant(b, fam))[: args.top])


def cmd_categories(args):
    """Spend by budget category on the feed, and what is left uncategorised."""
    rules = model.resolve_points(_rules(args))
    txns = feed.labelled(args.home, rules, args.env)
    w = model.pick_window(txns, days=args.days)
    tx = model.in_window(txns, w)
    by: dict[str, float] = {}
    for t in tx:
        by[t.budget_category] = by.get(t.budget_category, 0.0) + t.amount
    total = sum(v for v in by.values() if v > 0) or 1.0
    print(f"{w.start} → {w.end}, {len(tx)} spend rows")
    print(report.table(["category", "spend", "share"],
                       [[k, report.money(v), f"{100 * v / total:.0f}%"] for k, v in sorted(by.items(), key=lambda kv: -kv[1])]))
    left: dict[str, float] = {}
    for t in tx:
        if t.budget_category == cat.UNCATEGORISED:
            n = t.description or t.name
            left[n] = left.get(n, 0.0) + t.amount
    if left:
        print("\nuncategorised, biggest first (add a [[categories.rules]] pattern):")
        print(report.table(["merchant", "spend"], [[n[:40], report.money(v)] for n, v in sorted(left.items(), key=lambda kv: -abs(kv[1]))[: args.top]]))


def cmd_override(args):
    """Find a feed row, or set its family / budget category by hand in data/overrides.json."""
    rules = model.resolve_points(_rules(args))
    txns = feed.labelled(args.home, rules, args.env)
    if args.find:
        needle = args.find.lower()
        for t in txns:
            if needle in t.match_text:
                print(f"{t.txn_id}  {t.date}  {t.amount:>9.2f}  {(t.description or t.name)[:32]:32}  {t.family} / {t.budget_category}")
        return
    if not args.id or not (args.family or args.category):
        sys.exit("penny override --find TEXT, or penny override ID --family F and/or --category C")
    try:
        cat.check_override(txns, rules, args.id, args.family, args.category)
    except ValueError as e:
        sys.exit(f"{e}; find it with --find" if "no spend row" in str(e) else str(e))
    if args.category and args.category not in {t.budget_category for t in txns}:
        print(f"note: {args.category!r} is a new category")
    entry = cat.save_override(args.home / feed.OVERRIDES, args.id, args.family, args.category)
    print(f"{args.id}: {entry}")


def cmd_init(args):
    d = Path(args.dir).expanduser() if args.dir else args.home
    try:
        written = home.init(d)
    except FileExistsError as e:
        sys.exit(str(e))
    for p in written:
        print(f"wrote {p}")


def cmd_import(args):
    if args.source == "csv":
        return csvimport.cmd(args, _rules(args) if args.rules.exists() else home.defaults())
    for line in apple.import_files([Path(f) for f in args.files], args.home / "data" / "apple"):
        print(line)


def cmd_demo(args):
    demo.run(args.home_flag, args.seed, args.no_board, args.port)


def main(argv=None):
    p = argparse.ArgumentParser(prog="penny")
    p.add_argument("--home", help=f"the instance directory (default: ${home.ENV}, else ./home)")
    p.add_argument("--rules", help="default: rules.toml in the instance")
    p.add_argument("--assumptions", help="default: assumptions.toml in the instance")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="write a starter instance: rules.toml, all-zero assumptions.toml, data/ (never overwrites)")
    s.add_argument("dir", nargs="?", help="default: the instance (--home, $PENNY_HOME, else ./home)")
    s.set_defaults(fn=cmd_init)
    for name, fn in (("inspect", cmd_inspect), ("unmatched", cmd_unmatched), ("families", cmd_families), ("report", cmd_report)):
        s = sub.add_parser(name)
        s.add_argument("export", nargs=None if name == "inspect" else "?",
                       help=None if name == "inspect" else "a Rocket Money export CSV; default: the instance's feed, imported CSVs included")
        if name != "inspect":
            g = s.add_mutually_exclusive_group()
            g.add_argument("--year", type=int, help="calendar year window")
            g.add_argument("--days", type=int, help="trailing window ending at the last transaction (default 365)")
            s.add_argument("--top", type=int, default=40 if name == "unmatched" else 5)
        s.set_defaults(fn=fn)
    s = sub.add_parser("compare", help="Rocket Money export vs the Plaid + Apple feed: spend by family and account")
    s.add_argument("export", nargs="?", help="default: the Rocket Money rows penny import csv --preset rocket brought in")
    s.add_argument("--days", type=int, default=365, help="trailing window ending where both sources have data")
    s.add_argument("--family", action="append", help="also break this family down by merchant (repeatable)")
    s.add_argument("--top", type=int, default=15)
    s.add_argument("--env", choices=["sandbox", "production"], default="production")
    s.set_defaults(fn=cmd_compare)
    s = sub.add_parser("categories", help="spend by budget category on the Plaid + Apple feed, and the uncategorised merchants")
    s.add_argument("--days", type=int, default=365)
    s.add_argument("--top", type=int, default=15)
    s.add_argument("--env", choices=["sandbox", "production"], default="production")
    s.set_defaults(fn=cmd_categories)
    s = sub.add_parser("override", help="set one feed row's family or budget category by hand (data/overrides.json)")
    s.add_argument("id", nargs="?", help="the row's id, from --find")
    s.add_argument("--find", metavar="TEXT", help="list spend rows whose merchant text contains TEXT, with their ids")
    s.add_argument("--family")
    s.add_argument("--category")
    s.add_argument("--env", choices=["sandbox", "production"], default="production")
    s.set_defaults(fn=cmd_override)
    s = sub.add_parser("walmart", help="rank Walmart line items from a scripts/walmart-orders-capture.js file")
    s.add_argument("capture")
    s.add_argument("--top", type=int, default=30)
    s.add_argument("--sheet", help="also write the top items as a CSV with blank Costco columns (put it under data/)")
    s.set_defaults(fn=cmd_walmart)
    s = sub.add_parser("costco", help="rank Costco warehouse items from a scripts/costco-receipts-capture.js file")
    s.add_argument("capture")
    s.add_argument("--top", type=int, default=30)
    s.set_defaults(fn=cmd_costco)
    s = sub.add_parser("board", help="serve the decision board; Record writes into rules.toml / assumptions.toml")
    s.add_argument("--export", help="run on a Rocket Money export CSV instead of the Plaid + Apple feed")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--tailscale", action="store_true", help="bind this machine's Tailscale IPv4 instead of 127.0.0.1")
    g.add_argument("--host", help="bind address (default 127.0.0.1); anything else is reachable by others and warns, as the board has no login")
    s.add_argument("--port", type=int, default=8766)
    s.add_argument("--todo-file", metavar="PATH", help="a Markdown file whose table rows carrying <!-- id:penny-... --> show as open to-dos (off by default)")
    s.set_defaults(fn=cmd_board)
    s = sub.add_parser("plaid", help="Plaid feed: link cards, sync transactions into data/plaid/<env>/")
    s.add_argument("action", choices=["link", "sync", "status", "sandbox-item"])
    s.add_argument("--env", choices=["sandbox", "production"], default="sandbox")
    s.add_argument("--host", default="127.0.0.1", help="link page bind address")
    s.add_argument("--port", type=int, default=8767)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--update", metavar="ITEM_ID", help="link: update mode on an existing Item, adding Statements (keeps the Item and its slot)")
    g.add_argument("--relogin", metavar="ITEM_ID", help="link: update mode on an existing Item to sign in again after ITEM_LOGIN_REQUIRED; asks for no new product")
    s.add_argument("--redirect-uri", help="OAuth redirect, an HTTPS URL that reaches the link page and is allowlisted in the Dashboard; required with --env production")
    s.set_defaults(fn=cmd_plaid)
    s = sub.add_parser("import", help="apple: copy Apple Card Wallet CSVs and statement PDFs into data/apple/; "
                       "csv: any bank CSV into data/imports/, which report and the board read")
    s.add_argument("source", choices=["apple", "csv"])
    s.add_argument("files", nargs="+")
    csvimport.add_arguments(s)
    s.set_defaults(fn=cmd_import)
    s = sub.add_parser("check", help="tie each account's feed rows to its known balances: statements, PDFs and snapshots")
    s.add_argument("--env", choices=["sandbox", "production"], default="production")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("demo", help="a year of synthetic transactions in a throwaway instance (a new temp dir, or an empty --home), then the board on it")
    s.add_argument("--seed", type=int, default=0, help="the same seed and day give the same instance")
    s.add_argument("--no-board", action="store_true", help="build it and print its path; don't serve the board")
    s.add_argument("--port", type=int, default=8766)
    s.set_defaults(fn=cmd_demo)
    args = p.parse_args(argv)
    args.home_flag = args.home  # as given: penny demo writes only into an explicit --home
    args.home = home.resolve(args.home)
    args.rules = Path(args.rules) if args.rules else args.home / "rules.toml"
    args.assumptions = Path(args.assumptions) if args.assumptions else args.home / "assumptions.toml"
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
