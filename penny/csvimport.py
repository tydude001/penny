"""``penny import csv``: any bank's CSV into the instance's feed.

A CSV is read once, at import, into one normalised file per source under
``data/imports/`` (same name as the file imported, so importing it again
replaces it rather than doubling it). ``feed.load`` reads every file there
beside the Plaid ledgers and the Apple Card exports, so ``penny report``,
``penny categories`` and the board see imported rows with no further flags.

Presets:

- ``rocket``: a Rocket Money export, read by ``load.py`` as the report
  always has (``[export]`` in rules.toml sets its sign). A row in one of
  ``exclude_categories`` is kept but marked income, a card payment or a
  transfer, so it is never spend but cash flow can see it.
  ``penny compare`` with no file compares these rows to the Plaid + Apple feed.
- ``apple``: an Apple Card Wallet export, copied into ``data/apple/`` exactly
  as ``penny import apple`` does, since the feed already reads it there.

Anything else takes a column map: ``--date``, ``--amount`` (or ``--debit`` and
``--credit`` for a file that splits them), ``--description``, and optionally
``--account``, ``--category``, ``--merchant``. Amounts are read as positive =
money out; ``--negate`` flips a file that writes purchases negative. A row
whose description reads like a card payment or a transfer is never spend.

The normalised file's ``id`` is a hash of the row plus how many times the
same row occurs in its file, so two identical coffees on one day both
survive and an override keyed on the id outlives a re-import.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import apple, fsio
from . import load as ld

IMPORTS = Path("data") / "imports"
COLUMNS = ("id", "date", "amount", "name", "merchant", "account", "account_number", "category", "kind", "source")
# Money moved between your own accounts, never spend: a card payment on either
# side of it, or a transfer.
PAYMENT = re.compile(r"\b(payment|pymt|autopay|auto pay|epay)\b", re.IGNORECASE)
TRANSFER = re.compile(r"\b(transfer|xfer)\b", re.IGNORECASE)
TRANSFER_KINDS = ("payment", "transfer")


@dataclass
class ColumnMap:
    date: str
    description: str
    amount: str | None = None
    debit: str | None = None  # money out, in a file that splits the two
    credit: str | None = None  # money in
    account: str | None = None
    account_name: str | None = None  # one account for the whole file
    last4: str | None = None  # its last four digits, which rules.toml [accounts] names
    category: str | None = None
    merchant: str | None = None
    negate: bool = False
    bank: bool = False  # a bank account: money in is income, not a refund

    def check(self, header: list[str]) -> None:
        if not self.amount and not (self.debit or self.credit):
            raise ValueError("give --amount, or --debit and/or --credit")
        named = [c for c in (self.date, self.description, self.amount, self.debit, self.credit,
                             self.account, self.category, self.merchant) if c]
        missing = [c for c in named if c not in header]
        if missing:
            raise ValueError(f"no column {', '.join(map(repr, missing))} in this file; it has {header}")


def _kind(text: str, amount: float, bank: bool) -> str:
    if PAYMENT.search(text):
        return "payment"
    if TRANSFER.search(text):
        return "transfer"
    if amount < 0:
        return "income" if bank else "refund"
    return "purchase"


def _cell(row: dict, col: str | None) -> str:
    return (row.get(col) or "").strip() if col else ""


def read_mapped(path: Path, m: ColumnMap) -> list[dict]:
    """A bank CSV through a column map, as normalised rows (without ids)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        m.check(list(reader.fieldnames or []))
        out = []
        for row in reader:
            raw_date = _cell(row, m.date)
            if not raw_date:
                continue
            if m.amount:
                amt = ld._parse_amount(_cell(row, m.amount) or "0")
            else:
                out_ = _cell(row, m.debit)
                in_ = _cell(row, m.credit)
                amt = (abs(ld._parse_amount(out_)) if out_ else 0.0) - (abs(ld._parse_amount(in_)) if in_ else 0.0)
            if m.negate:
                amt = -amt
            name = _cell(row, m.description)
            out.append({
                "date": ld._parse_date(raw_date).isoformat(),
                "amount": round(amt, 2),
                "name": name,
                "merchant": _cell(row, m.merchant),
                "account": _cell(row, m.account) or m.account_name or "",
                "account_number": m.last4 or "",
                "category": _cell(row, m.category),
                "kind": _kind(name, amt, m.bank),
                "source": "csv",
            })
    return out


def read_rocket(path: Path, rules: dict) -> list[dict]:
    """A Rocket Money export, as ``load.py`` reads it for the report, with the
    excluded categories kept and marked as not spend."""
    ex = rules.get("export", {})
    excl = {c.lower() for c in ex.get("exclude_categories", [])}
    out = []
    for t in ld.load(path, expense_sign=ex.get("expense_sign", "positive")):
        c = t.category.lower()
        if c in excl:
            kind = "income" if "income" in c else "payment" if "payment" in c else "transfer"
        else:
            kind = "refund" if t.amount < 0 else "purchase"
        out.append({
            "date": t.date.isoformat(), "amount": round(t.amount, 2), "name": t.custom_name or t.name,
            "merchant": t.description, "account": t.account, "account_number": t.account_number,
            "category": t.category, "kind": kind, "source": "rocket_money",
        })
    return out


def with_ids(rows: list[dict], stem: str) -> list[dict]:
    seen: Counter = Counter()
    for r in rows:
        key = "\x1f".join(str(r[k]) for k in ("date", "amount", "name", "merchant", "account", "account_number"))
        h = hashlib.sha256(key.encode()).hexdigest()[:16]
        seen[h] += 1
        r["id"] = f"csv-{stem}-{h}-{seen[h]}"
    return rows


def write(rows: list[dict], dest: Path) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in sorted(rows, key=lambda r: (r["date"], r["id"])):
        w.writerow({k: r.get(k, "") for k in COLUMNS})
    fsio.write_atomic(dest, buf.getvalue())


def _safe_stem(p: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", p.stem).strip("-.") or "import"


def import_file(root: Path, path: Path, preset: str | None, rules: dict, cmap: ColumnMap | None = None) -> list[str]:
    """Import one CSV into the instance ``root``; lines saying what happened."""
    path = Path(path)
    if preset == "apple":
        return apple.import_files([path], root / "data" / "apple")
    if preset == "rocket":
        rows = read_rocket(path, rules)
    elif cmap is not None:
        rows = read_mapped(path, cmap)
    else:
        raise ValueError("give --preset rocket|apple, or a column map (--date, --amount, --description)")
    stem = _safe_stem(path)
    dest = root / IMPORTS / f"{stem}.csv"
    replaced = dest.exists()
    write(with_ids(rows, stem), dest)
    spend = [r for r in rows if r["kind"] not in (*TRANSFER_KINDS, "income")]
    lines = [(f"{path.name}: {'replaced' if replaced else 'imported'} {len(rows)} rows "
              f"({len(spend)} spend, {len(rows) - len(spend)} payments, transfers or income) into {dest}")]
    if rows:
        lines.append(f"  {min(r['date'] for r in rows)} to {max(r['date'] for r in rows)}, "
                     f"spend {sum(r['amount'] for r in spend):,.2f}")
    purchases = [r for r in spend if r["amount"] > 0]
    if spend and len(purchases) < len(spend) / 2:
        lines.append("  most rows are money in: if this file writes purchases as negative, import it again with --negate")
    return lines


def load(imports_dir: Path) -> list[ld.Txn]:
    """Every row of every imported file, as feed rows."""
    out = []
    for p in sorted(imports_dir.glob("*.csv")) if imports_dir.is_dir() else []:
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                kind = r.get("kind") or "purchase"
                out.append(ld.Txn(
                    date=date.fromisoformat(r["date"]),
                    name=r["name"],
                    amount=float(r["amount"]),
                    category=r.get("category") or "",
                    account=r.get("account") or "",
                    account_number=r.get("account_number") or "",
                    description=r.get("merchant") or "",
                    kind=kind,
                    source=r.get("source") or "csv",
                    transfer=kind in TRANSFER_KINDS,
                    txn_id=r["id"],
                ))
    return out


def add_arguments(s) -> None:
    """``penny import csv``'s options, on the ``import`` subparser."""
    g = s.add_argument_group("csv", "penny import csv FILE: a preset, or a column map for any other bank's CSV")
    g.add_argument("--preset", choices=["rocket", "apple"], help="rocket: a Rocket Money export; apple: an Apple Card Wallet export")
    g.add_argument("--date", metavar="COL", help="the transaction date column")
    g.add_argument("--amount", metavar="COL", help="one signed amount column, positive = money out (see --negate)")
    g.add_argument("--debit", metavar="COL", help="money out, where the file splits amounts in two columns")
    g.add_argument("--credit", metavar="COL", help="money in, beside --debit")
    g.add_argument("--description", metavar="COL", help="the merchant or payee text the families match")
    g.add_argument("--merchant", metavar="COL", help="a cleaner merchant name, if the file has one")
    g.add_argument("--category", metavar="COL", help="the bank's own category, a family's fallback")
    g.add_argument("--account", metavar="COL", help="a column naming the account, for a file of several")
    g.add_argument("--account-name", metavar="NAME", help="the account the whole file is from")
    g.add_argument("--last4", metavar="DIGITS", help="that account's last four digits, as rules.toml [accounts] names it")
    g.add_argument("--negate", action="store_true", help="the file writes purchases as negative: flip every amount")
    g.add_argument("--bank", action="store_true", help="a bank account: money in is income, not a refund")


def cmd(args, rules: dict) -> None:
    cmap = None
    if args.preset is None:
        if not (args.date and args.description and (args.amount or args.debit or args.credit)):
            sys.exit("penny import csv FILE --preset rocket|apple, or map the columns: "
                     "--date COL --amount COL --description COL [--account COL] [--negate]")
        cmap = ColumnMap(date=args.date, description=args.description, amount=args.amount, debit=args.debit,
                         credit=args.credit, account=args.account, account_name=args.account_name, last4=args.last4,
                         category=args.category, merchant=args.merchant, negate=args.negate, bank=args.bank)
    for f in args.files:
        try:
            lines = import_file(args.home, Path(f), args.preset, rules, cmap)
        except (ValueError, OSError, KeyError) as e:
            sys.exit(f"{f}: {e}")
        print("\n".join(lines))
