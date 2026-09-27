"""Statement PDFs: OnePay (Synchrony) and Apple Card (Goldman Sachs).

Text comes from ``pdftotext -layout`` into memory and is never written out, so
nothing extracted outlives the check that asked for it. A parse proves itself:
the opening balance plus every line read must equal the statement's own new
balance, or ``parse`` raises rather than hand a check a short list.

Signs follow the repo: positive = money out (adds to what is owed), so
payments and credits are negative.

Apple Card's balance is the *monthly* balance: the period's charges plus this
month's installment, never the financed lump sum. Its statement lists the
installment separately from the transactions; it becomes one ``installment``
line dated the period's last day, as the CSV has it.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

MONEY = r"-?\$[\d,]+\.\d\d"


@dataclass
class Line:
    date: date
    description: str
    amount: float
    kind: str  # payment, reward credit, refund, purchase, fee, interest, installment


@dataclass
class Statement:
    issuer: str  # "onepay" or "apple"
    start: date
    end: date  # both ends are in the period
    previous: float
    new: float
    lines: list[Line] = field(default_factory=list)
    mask: str | None = None
    path: Path | None = None

    def total(self) -> float:
        return round(sum(l.amount for l in self.lines), 2)


class ParseError(ValueError):
    pass


def money(s: str) -> float:
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    return round(float(s), 2)


def pdf_text(path: Path) -> str:
    return subprocess.run(["pdftotext", "-layout", str(path), "-"], check=True,
                          capture_output=True, text=True).stdout


def _mdy(s: str) -> date:
    return date(*time.strptime(s, "%m/%d/%Y")[:3])


# --- OnePay -----------------------------------------------------------------

ONEPAY_SECTIONS = [  # header prefix -> kind; "Other Credits" holds the reward credits
    ("Payments", "payment"),
    ("Other Credits", "refund"),
    ("Purchases and Other Debits", "purchase"),
    ("Total Fees Charged", "fee"),
    ("Total Interest Charged", "interest"),
]
ONEPAY_ROW = re.compile(r"^\s*(\d\d)/(\d\d)\s+(?:([A-Z0-9]{17})\s+)?(.+?)\s+(" + MONEY + r")\s*$")


def parse_onepay(text: str) -> Statement:
    m = re.search(r"Billing Cycle from (\d\d/\d\d/\d{4}) to (\d\d/\d\d/\d{4})", text)
    prev = re.search(r"Previous Balance as of \d\d/\d\d/\d{4}\s+(" + MONEY + ")", text)
    new = re.search(r"New Balance as of \d\d/\d\d/\d{4}\s+(" + MONEY + ")", text)
    if not (m and prev and new):
        raise ParseError("OnePay: no billing cycle or balances")
    start, end = _mdy(m.group(1)), _mdy(m.group(2))
    mask = re.search(r"Account Number ending in (\d{4})", text)
    st = Statement("onepay", start, end, money(prev.group(1)), money(new.group(1)),
                   mask=mask.group(1) if mask else None)
    kind = None
    in_detail = False
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("Transaction Detail"):
            in_detail = True
            continue
        if not in_detail:
            continue
        if s.startswith(("Interest Charge Calculation", "2026 Year-to-Date", "Year-to-Date")) or "Year-to-Date" in s:
            in_detail = False
            continue
        for prefix, k in ONEPAY_SECTIONS:
            if s.startswith(prefix):
                kind = k
                break
        r = ONEPAY_ROW.match(raw)
        if not r or kind is None:
            continue
        mo, dy = int(r.group(1)), int(r.group(2))
        year = end.year if mo <= end.month else end.year - 1  # a cycle spans at most one new year
        desc = r.group(4).strip()
        amt = money(r.group(5))
        k = kind
        if s.upper().startswith(f"{r.group(1)}/{r.group(2)}") and "INTEREST CHARGE" in desc.upper():
            k = "interest"
        if k == "refund" and "REWARD" in desc.upper():
            k = "reward credit"
        st.lines.append(Line(date(year, mo, dy), desc, amt, k))
    return st


# --- Apple Card -------------------------------------------------------------

APPLE_ROW = re.compile(r"^\s*(\d\d/\d\d/\d{4})\s+(.+?)\s+(" + MONEY + r")\s*$")
APPLE_ADJ = re.compile(r"^\s*(Daily Cash Adjustment)\s+.*?(" + MONEY + r")\s*$")
MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"


def _mon(s: str) -> date:
    return date(*time.strptime(s, "%b %d, %Y")[:3])


def parse_apple(text: str) -> Statement:
    m = re.search(rf"({MONTHS}) (\d+) — ({MONTHS}) (\d+), (\d{{4}})", text)
    if not m:
        raise ParseError("Apple Card: no statement period")
    end = _mon(f"{m.group(3)} {m.group(4)}, {m.group(5)}")
    start = _mon(f"{m.group(1)} {m.group(2)}, {end.year - (1 if m.group(1) == 'Dec' and m.group(3) == 'Jan' else 0)}")
    prev = re.search(r"Previous Monthly Balance\s+(" + MONEY + ")", text)
    new = re.search(r"Your \w+ Balance.*?(" + MONEY + ")", text, re.DOTALL)
    if not (prev and new):
        raise ParseError("Apple Card: no monthly balances")
    st = Statement("apple", start, end, money(prev.group(1)), money(new.group(1)))
    section = None
    last_date = None
    for raw in text.splitlines():
        s = raw.strip()
        if s == "Payments":
            section = "payment"
        elif s == "Transactions":
            section = "purchase"
        elif s.startswith(("Apple Card Monthly Installments", "Daily Cash", "Interest Charged", "Legal")) \
                and not s.startswith("Daily Cash Adjustment"):
            section = None
        if s.startswith("Total "):
            continue
        if "This month’s installment:" in s or "This month's installment:" in s:
            amt = money(re.search(MONEY, s).group(0))
            st.lines.append(Line(end, "Monthly installment", amt, "installment"))
            continue
        tot = re.match(r"Total interest for this month\s+(" + MONEY + ")", s)
        if tot and money(tot.group(1)):
            st.lines.append(Line(end, "Interest", money(tot.group(1)), "interest"))
            continue
        if section is None:
            continue
        a = APPLE_ADJ.match(raw)
        if a and last_date:
            st.lines.append(Line(last_date, a.group(1), money(a.group(2)), "reward credit"))
            continue
        r = APPLE_ROW.match(raw)
        if not r:
            continue
        d = _mdy(r.group(1))
        last_date = d
        desc = re.sub(r"\s+\d+%\s+" + MONEY + r"$", "", r.group(2)).strip()  # Daily Cash columns
        amt = money(r.group(3))
        kind = section
        if section == "purchase" and amt < 0:
            kind = "refund"
        st.lines.append(Line(d, desc, amt, kind))
    return st


# --- either -----------------------------------------------------------------

def parse_text(text: str) -> Statement:
    if "onepay" in text.lower() and "Billing Cycle" in text:
        st = parse_onepay(text)
    elif "Apple Card" in text and "Previous Monthly Balance" in text:
        st = parse_apple(text)
    else:
        raise ParseError("not a OnePay or Apple Card statement")
    if round(st.previous + st.total(), 2) != st.new:
        raise ParseError(f"{st.issuer} {st.end}: {st.previous:.2f} + lines {st.total():.2f} "
                         f"!= new balance {st.new:.2f}; a line was missed")
    return st


def parse(path: Path) -> Statement:
    st = parse_text(pdf_text(path))
    st.path = path
    return st


def load_dir(*dirs: Path) -> tuple[list[Statement], list[str]]:
    """Every parseable statement under ``dirs``, oldest first, and one error line per PDF that isn't.

    Without pdftotext nothing can be read: that is one error line naming what
    to install, not a crash and not a line per file.
    """
    out, errors = [], []
    for d in dirs:
        for p in sorted(d.glob("*.pdf")) if d.is_dir() else []:
            try:
                out.append(parse(p))
            except (ParseError, subprocess.CalledProcessError) as e:
                errors.append(f"{p.name}: {e}")
            except FileNotFoundError as e:
                if not p.exists():  # the PDF went away mid-run
                    errors.append(f"{p.name}: {e}")
                    continue
                errors.append("PDFs not read: pdftotext not found (install poppler)")
                out.sort(key=lambda s: (s.issuer, s.end))
                return out, errors
    out.sort(key=lambda s: (s.issuer, s.end))
    return out, errors
