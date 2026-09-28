"""Apple Card: Wallet CSV exports and statement PDFs kept as downloaded in data/apple/.

``penny import apple`` copies files in; every load re-reads them all.
Overlapping exports are fine: a row is the hash of its fields (all but the
clearing date) plus how many times those same fields occur in one file, and the union keeps each
(hash, occurrence) once. Two identical coffees on one day both survive, and
the same month exported twice doesn't double.

Rows come out in the Plaid ledger's shape (``date``, ``amount``, ``name``,
``pending``, ``transaction_id``) plus ``kind`` and ``source``, so the checks
read both feeds the same way. ``date`` is the transaction date, the one the
statement prints; ``posted`` is the clearing date, the one that decides which
statement a row is billed on.
"""

from __future__ import annotations

import csv
import hashlib
import shutil
import tempfile
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

KINDS = {"Purchase": "purchase", "Other": "purchase", "Payment": "payment",
         "Installment": "installment", "Credit": "refund", "Debit": "fee"}
FIELDS = ("Transaction Date", "Clearing Date", "Description", "Amount (USD)", "Type")
# The identity leaves out the clearing date, so a row exported while pending
# and again once cleared is one row.
IDENTITY = ("Transaction Date", "Description", "Amount (USD)", "Type")


def _iso(s: str) -> str | None:
    return date(*time.strptime(s, "%m/%d/%Y")[:3]).isoformat() if s else None


def is_export(path: Path) -> bool:
    with path.open(newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), [])
    return all(h in header for h in FIELDS)


def kind(row: dict) -> str:
    if row["Type"] == "Debit" and "DAILY CASH" in row["Description"].upper():
        return "reward credit"  # Daily Cash clawed back on a return
    return KINDS.get(row["Type"], "purchase")


def read_file(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    seen: Counter = Counter()
    out = []
    for r in rows:
        key = hashlib.sha256("\x1f".join(r[k] for k in IDENTITY).encode()).hexdigest()[:16]
        seen[key] += 1
        out.append({
            "transaction_id": f"apple-{key}-{seen[key]}",
            "date": _iso(r["Transaction Date"]),
            "posted": _iso(r["Clearing Date"]),
            "name": r["Description"],
            "merchant_name": r.get("Merchant") or None,
            "amount": round(float(r["Amount (USD)"]), 2),
            "kind": kind(r),
            "pending": not r["Clearing Date"],
            "source": "apple_csv",
        })
    return out


def load(apple_dir: Path) -> list[dict]:
    rows: dict[str, dict] = {}
    for p in sorted(apple_dir.glob("*.csv")) if apple_dir.is_dir() else []:
        if not is_export(p):
            continue
        for r in read_file(p):
            # A later export of the same row wins, so a pending row picks up its clearing date.
            if r["transaction_id"] not in rows or not r["pending"]:
                rows[r["transaction_id"]] = r
    return sorted(rows.values(), key=lambda r: (r["date"], r["transaction_id"]))


def import_files(paths: list[Path], apple_dir: Path) -> list[str]:
    """Copy Wallet CSVs and statement PDFs into ``apple_dir``; one line per file saying what happened."""
    from .statements import ParseError, parse

    apple_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for p in paths:
        if p.suffix.lower() == ".csv":
            if not is_export(p):
                out.append(f"{p.name}: skipped, not a Wallet export (no {', '.join(FIELDS)})")
                continue
            what = f"{len(read_file(p))} rows"
        elif p.suffix.lower() == ".pdf":
            try:
                st = parse(p)
            except ParseError as e:
                out.append(f"{p.name}: skipped, {e}")
                continue
            if st.issuer != "apple":
                out.append(f"{p.name}: skipped, a {st.issuer} statement")
                continue
            what = f"statement {st.start} to {st.end}"
        else:
            out.append(f"{p.name}: skipped, not .csv or .pdf")
            continue
        dest = apple_dir / p.name
        if dest.exists() and dest.read_bytes() == p.read_bytes():
            out.append(f"{p.name}: already imported ({what})")
            continue
        if dest.exists():
            dest = apple_dir / f"{p.stem}-{datetime.now().astimezone():%Y%m%dT%H%M%S}{p.suffix}"
        shutil.copy2(p, dest)
        out.append(f"{p.name}: imported ({what})")
    return out


def import_upload(data: bytes, apple_dir: Path) -> list[str]:
    """One uploaded file, as ``import_files`` takes it. An upload has no name of
    its own (an iOS Shortcut sends only the bytes), so it is named for its
    content: a PDF by its magic number, anything else as a CSV, which
    ``import_files`` then refuses unless it is a Wallet export. Sending the same
    file twice lands on the same name and says "already imported"."""
    ext = ".pdf" if data.startswith(b"%PDF-") else ".csv"
    name = f"upload-{hashlib.sha256(data).hexdigest()[:12]}{ext}"
    apple_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=apple_dir.parent) as tmp:
        p = Path(tmp) / name
        p.write_bytes(data)
        return import_files([p], apple_dir)
