"""Balance checks: the feed's rows must carry one known balance to the next.

    balance at A + rows after A up to B = balance at B

Three kinds of A and B:

- **Liabilities statement to today** (Chase): A is the last statement from
  Plaid Liabilities, B today's current balance, both from the newest
  balances.jsonl line.
- **Statement PDF** (OnePay, Apple Card): each statement's previous balance
  plus the feed's rows in its period must reach its new balance. The newest
  one is also carried to today's balance where the account has one (OnePay).
- **Snapshot to snapshot** (SoFi, and any account with neither): the oldest
  snapshot of the last ``SPAN_DAYS`` from an earlier day, to the newest.

Plaid's credit amounts already follow the repo's sign (positive = money out,
adding to what is owed), so on a card the rows sum as they are; on a
depository account money out lowers the balance, so they subtract. A period
includes both end dates, so rows count from the day after A. Pending rows
count only once posted. Rows are matched on the date the statement lists:
Plaid's ``date`` (posted) for OnePay, which tied all ten 2025-11 to 2026-08
statements to the cent where ``authorized_date`` missed three. Apple Card
prints the transaction date but bills a row in the period it *cleared* in, so
its rows match on ``posted``: a charge on the 31st that clears on the 2nd is on
next month's statement (all thirteen 2025-08 to 2026-08 tie that way; by
transaction date five missed).

**OnePay reward credits** are missing from Plaid's feed. A statement's own
reward-credit lines are added to its period. Past the newest statement, a gap
in the credit direction passes as an inferred reward credit, but only up to
what the card can have earned since its last known redemption (its best rate
× net purchases, plus ``REWARD_SLACK`` for rounding). Anything larger, or in
the other direction, fails, so a dropped refund can't pass as a reward.

Every result carries ``through``, the day it checked the account up to.
``attention`` names the accounts whose newest check misses or is older than
``STALE_DAYS``, because feeds break silently; the board shows those on every
page.

Snapshots are taken at sync time, so a row dated on either snapshot's day may
fall on either side of it. A snapshot check that misses tries moving those
edge rows across the boundary before it reports a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import combinations
from pathlib import Path

from . import apple, statements
from .plaid import Store
from .statements import Statement

EDGE_DAYS = 3  # rows this close to the statement date are listed first when a check misses
SPAN_DAYS = 35  # a snapshot check reaches back this far for its A
REWARD_SLACK = 1.00  # per-row rounding of rewards; the 2025-11 to 2026-06 redemptions drift by cents
MAX_EDGE_ROWS = 10  # subsets of this many snapshot-edge rows are tried (2**10)
PAYMENT_CATEGORIES = ("LOAN_PAYMENTS", "TRANSFER_IN", "TRANSFER_OUT")
STALE_DAYS = 45  # an account not tied for this long gets a warning on every page
IMPORT_GRACE = 7  # days after an Apple Card statement closes before its import is overdue
APPLE = "Apple Card"


@dataclass
class Result:
    account: str
    ok: bool | None  # None: no check for this account yet
    detail: str
    gap: float = 0.0
    near: list[dict] = field(default_factory=list)
    inferred: list[dict] = field(default_factory=list)  # reward credits the gap was taken to be
    through: date | None = None  # the day the check reached: a statement's end, or the snapshot's day


def latest_snapshots(store: Store) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in store.balances():
        out[line["account_id"]] = line  # the file is in time order, so the last line wins
    return out


def label(snap: dict) -> str:
    return f"{snap.get('name') or '?'} …{snap.get('mask') or '?'}"


def _d(t: dict) -> date:
    return date.fromisoformat(t["date"])


def _billed(t: dict) -> date:
    """The day that puts a row on a statement: its clearing date where the feed has one (Apple Card)."""
    return date.fromisoformat(t.get("posted") or t["date"])


def _posted(rows: list[dict]) -> list[dict]:
    return [t for t in rows if not t.get("pending")]


def _near(rows: list[dict], gap: float, around: list[date]) -> list[dict]:
    edge = timedelta(days=EDGE_DAYS)
    near = [t for t in rows if abs(abs(t["amount"]) - abs(gap)) < 0.005
            or any(abs(_d(t) - a) <= edge for a in around)]
    near.sort(key=lambda t: (abs(abs(t["amount"]) - abs(gap)) >= 0.005, t["date"]))
    return near[:5]


def sign(snap: dict) -> int:
    """+1 when money out raises the balance (cards), -1 when it lowers it (bank accounts)."""
    return -1 if snap.get("type") == "depository" else 1


def is_payment(t: dict) -> bool:
    if t.get("kind"):
        return t["kind"] == "payment"
    return (t.get("personal_finance_category") or {}).get("primary") in PAYMENT_CATEGORIES


def statement_check(snap: dict, rows: list[dict]) -> Result:
    closed = date.fromisoformat(snap["last_statement_issue_date"])
    since = [t for t in _posted(rows) if _d(t) > closed]
    expected = round(snap["last_statement_balance"] + sum(t["amount"] for t in since), 2)
    actual = round(snap["current"], 2)
    gap = round(actual - expected, 2)
    detail = (f"statement {closed} {snap['last_statement_balance']:.2f} + {len(since)} rows since = "
              f"{expected:.2f}; current {actual:.2f}")
    through = local_day(snap["at"]) if snap.get("at") else closed
    if abs(gap) < 0.005:
        return Result(label(snap), True, detail, through=through)
    return Result(label(snap), False, f"{detail}; gap {gap:+.2f}", gap, _near(rows, gap, [closed]), through=through)


def period_check(name: str, st: Statement, rows: list[dict], from_pdf: tuple[str, ...] = ()) -> Result:
    """One statement: its previous balance + the feed's rows in its period (+ the
    PDF's own lines of kinds the feed lacks) = its new balance."""
    inside = [t for t in _posted(rows) if st.start <= _billed(t) <= st.end]
    extra = [l for l in st.lines if l.kind in from_pdf]
    expected = round(st.previous + sum(t["amount"] for t in inside) + sum(l.amount for l in extra), 2)
    gap = round(st.new - expected, 2)
    detail = (f"statement {st.start} to {st.end}: {st.previous:.2f} + {len(inside)} rows"
              + (f" + {len(extra)} {'/'.join(from_pdf)} from the PDF" if extra else "")
              + f" = {expected:.2f}; statement {st.new:.2f}")
    if abs(gap) < 0.005:
        return Result(name, True, detail, through=st.end)
    missing = [{"date": l.date.isoformat(), "amount": l.amount, "name": f"on the statement: {l.description}"}
               for l in st.lines if l.kind not in from_pdf and abs(abs(l.amount) - abs(gap)) < 0.005]
    return Result(name, False, f"{detail}; gap {gap:+.2f}", gap,
                  (missing + _near(inside, gap, [st.start, st.end]))[:5], through=st.end)


def summarise(name: str, results: list[Result]) -> list[Result]:
    """Passing statements fold into one line; each miss keeps its own."""
    ok = [r for r in results if r.ok]
    out = [r for r in results if not r.ok]  # misses and statements with nothing to check against
    if ok:
        out.insert(0, Result(name, True, f"{len(ok)} of {len(results)} statements tie"
                             + (f", the latest {ok[-1].detail.split(':')[0].removeprefix('statement ')}" if ok else ""),
                             through=max(r.through for r in ok)))
    return out


def reward_pool(rows: list[dict], statements: list[Statement], rate: float) -> tuple[float, date | None]:
    """What the card can have earned since its last known redemption: its best rate × net purchases."""
    known = [l.date for s in statements for l in s.lines if l.kind == "reward credit" and l.amount < 0]
    last = max(known) if known else None
    net = sum(t["amount"] for t in _posted(rows) if not is_payment(t) and (last is None or _d(t) >= last))
    return round(rate * max(net, 0.0), 2), last


def to_now(snap: dict, rows: list[dict], st: Statement, rate: float | None, statements: list[Statement]) -> Result:
    """The newest statement carried to today's balance, a credit-direction gap
    passing as reward credit up to the pool."""
    since = [t for t in _posted(rows) if _d(t) > st.end]
    expected = round(st.new + sign(snap) * sum(t["amount"] for t in since), 2)
    actual = round(snap["current"], 2)
    gap = round(actual - expected, 2)
    detail = f"statement {st.end} {st.new:.2f} + {len(since)} rows since = {expected:.2f}; current {actual:.2f}"
    through = local_day(snap["at"]) if snap.get("at") else st.end
    if abs(gap) < 0.005:
        return Result(label(snap), True, detail, through=through)
    if rate is not None and sign(snap) * gap < 0:
        pool, last = reward_pool(rows, statements, rate)
        if -sign(snap) * gap <= pool + REWARD_SLACK:
            credit = {"date": (local_day(snap["at"]) if snap.get("at") else datetime.now().astimezone().date()).isoformat(), "amount": gap,
                      "name": "reward credit (inferred)", "kind": "reward credit", "source": "inferred"}
            return Result(label(snap), True,
                          f"{detail}; the {gap:+.2f} is taken as reward credit (up to {pool:.2f} earned "
                          f"since the last redemption{f' on {last}' if last else ''})", gap, [], [credit], through)
        detail += f"; more than the {pool:.2f} it can have earned since {last or 'the feed began'}"
    return Result(label(snap), False, f"{detail}; gap {gap:+.2f}", gap, _near(since, gap, [st.end]), through=through)


def local_day(at: str) -> date:
    return datetime.fromisoformat(at).astimezone().date()


def snapshot_check(snaps: list[dict], rows: list[dict]) -> Result:
    """Oldest snapshot of the last SPAN_DAYS from an earlier day, to the newest.

    Plaid may send a null current balance; those snapshots are skipped, so the
    newest one with a balance is the one checked.
    """
    have = [s for s in snaps if s.get("current") is not None]
    if not have:
        return Result(label(snaps[-1]), None, "not checked: Plaid sent no current balance for this account")
    b = have[-1]
    day_b = local_day(b["at"]) if b.get("at") else None
    earlier = [s for s in have[:-1] if s.get("at") and day_b
               and local_day(s["at"]) < day_b and (day_b - local_day(s["at"])).days <= SPAN_DAYS]
    if not earlier:
        return Result(label(b), None, "not checked yet: needs snapshots from two different days")
    a = earlier[0]
    day_a = local_day(a["at"])
    posted = _posted(rows)
    base = [t for t in posted if day_a < _d(t) <= day_b]
    s = sign(b)
    expected = round(a["current"] + s * sum(t["amount"] for t in base), 2)
    actual = round(b["current"], 2)
    gap = round(actual - expected, 2)
    detail = f"snapshot {day_a} {a['current']:.2f} + {len(base)} rows since = {expected:.2f}; current {actual:.2f}"
    if abs(gap) < 0.005:
        return Result(label(b), True, detail, through=day_b)
    # Rows dated on a snapshot's day may sit on either side of it: +x moves one in, -x moves one out.
    edge = [(t, 1) for t in posted if _d(t) == day_a] + [(t, -1) for t in base if _d(t) == day_b]
    edge = edge[:MAX_EDGE_ROWS]
    for k in range(1, len(edge) + 1):
        for combo in combinations(edge, k):
            if abs(gap - s * sum(d * t["amount"] for t, d in combo)) < 0.005:
                return Result(label(b), True, f"{detail}; ties with {k} row(s) dated on a snapshot's day "
                              "moved across it", through=day_b)
    return Result(label(b), False, f"{detail}; gap {gap:+.2f}", gap, _near(posted, gap, [day_a, day_b]), through=day_b)


def run(store: Store, statements: list[Statement] = (), apple_rows: list[dict] | None = None,
        reward_rates: dict[str, float] | None = None) -> list[Result]:
    """Every check. ``statements`` are parsed PDFs, matched to Plaid accounts by
    mask (Apple Card's go to ``apple_rows``). ``reward_rates`` maps a mask whose
    feed lacks reward credits to the card's best rate."""
    reward_rates = reward_rates or {}
    history: dict[str, list[dict]] = {}
    for line in store.balances():
        history.setdefault(line["account_id"], []).append(line)
    snaps = {k: v[-1] for k, v in history.items()}
    rows_by_acct: dict[str, list[dict]] = {}
    for item_id in store.items():
        for t in store.ledger(item_id)["transactions"].values():
            rows_by_acct.setdefault(t["account_id"], []).append(t)
    by_mask: dict[str, list[Statement]] = {}
    for st in statements:
        by_mask.setdefault(st.mask or st.issuer, []).append(st)
    out = []
    for acct_id, snap in snaps.items():
        rows = rows_by_acct.get(acct_id, [])
        mine = sorted(by_mask.pop(snap.get("mask"), []), key=lambda s: s.end)
        rate = reward_rates.get(snap.get("mask"))
        from_pdf = ("reward credit",) if rate is not None else ()
        if mine:
            out += summarise(label(snap), [period_check(label(snap), s, rows, from_pdf) for s in mine])
        if snap.get("last_statement_issue_date") and snap.get("last_statement_balance") is not None \
                and snap.get("current") is not None:
            out.append(statement_check(snap, rows))
        elif mine and snap.get("current") is not None:
            out.append(to_now(snap, rows, mine[-1], rate, mine))
        else:
            out.append(snapshot_check(history[acct_id], rows))
    apple = sorted(by_mask.pop("apple", []), key=lambda s: s.end)
    if apple or apple_rows:
        name = APPLE
        if not apple:
            out.append(Result(name, None, "not checked yet: no statement PDF imported"))
        else:
            checked = []
            for s in apple:
                if any(s.start <= _billed(t) <= s.end for t in apple_rows or []):
                    checked.append(period_check(name, s, apple_rows or []))
                else:
                    checked.append(Result(name, None, f"statement {s.start} to {s.end}: no CSV rows imported for it"))
            out += summarise(name, checked)
    for mask, sts in by_mask.items():
        out.append(Result(f"statements …{mask}", None, f"{len(sts)} statements match no linked account"))
    return out


def reward_rates(rules: dict) -> dict[str, float]:
    """Mask -> the card's best rate, for each account whose feed lacks reward credits."""
    out = {}
    for mask, acct in rules.get("accounts", {}).items():
        if acct.get("feed_lacks_rewards"):
            card = rules["cards"][acct["card"]]
            out[str(mask)] = max([float(card.get("default_rate", 0.0)), *map(float, card.get("rates", {}).values())])
    return out


@dataclass
class Checks:
    results: list[Result]
    errors: list[str]  # statement PDFs that don't parse
    statements: list[Statement]


def run_all(root: Path, rules: dict, env: str = "production") -> Checks:
    """What ``penny check`` runs: every statement PDF, the Apple Card CSVs and the Plaid store."""
    sts, errors = statements.load_dir(root / "data" / "statements", root / "data" / "apple")
    return Checks(run(Store(root, env), sts, apple.load(root / "data" / "apple"), reward_rates(rules)), errors, sts)


def apple_due(sts: list[Statement], today: date) -> tuple[date, date, bool] | None:
    """(newest imported statement's end, the next one's close, overdue). Apple
    Card statements run a calendar month; the next is overdue ``IMPORT_GRACE``
    days after it closes."""
    ends = [s.end for s in sts if s.issuer == "apple"]
    if not ends:
        return None
    last = max(ends)
    start = last + timedelta(days=1)
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return last, nxt, today > nxt + timedelta(days=IMPORT_GRACE)


def attention(c: Checks, today: date) -> list[str]:
    """What needs looking at, one line each: an account whose newest check
    misses, one not tied for ``STALE_DAYS``, a PDF that won't parse, an
    overdue Apple Card import."""
    out = [f"statement {e}" for e in c.errors]
    due = apple_due(c.statements, today)
    if due and due[2]:
        out.append(f"{APPLE} import overdue: the statement closing {due[1]} isn't imported")
    by: dict[str, list[Result]] = {}
    for r in c.results:
        if r.ok is not None and r.through:
            by.setdefault(r.account, []).append(r)
    for acct, rs in by.items():
        newest = max(rs, key=lambda r: (r.through, r.ok is False))
        if newest.ok is False:
            out.append(f"{acct} misses by {newest.gap:+.2f} ({newest.through})")
        elif (today - newest.through).days > STALE_DAYS and not (acct == APPLE and due and due[2]):
            out.append(f"{acct} not tied since {newest.through}")
    return out
