"""``penny demo``: a year of made-up money in a throwaway instance.

Everything here is synthetic: the people, merchants' amounts, account numbers
and balances come from a seeded random generator, so the same ``--seed`` and
day give the same instance. It is written as a Plaid ledger (the shape
``penny plaid sync`` stores, as ``tests/feedfix.py`` does) so every page of
the board has something on it: four cards and a checking and savings account,
all three memberships and their fees, monthly statements paid in full from
checking, balance snapshots that tie to the rows, recurring bills, a
``[held]`` set, perk values and budgets.

Nothing is written outside the instance directory.
"""

from __future__ import annotations

import calendar
import random
import sys
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from . import fsio, plaid

# item -> institution, and its accounts: key -> (name, mask, type, subtype)
ITEMS = {
    "demo-chase": ("Chase", {"sapphire": ("Sapphire Preferred", "4821", "credit", "credit card"),
                             "prime": ("Prime Visa", "3007", "credit", "credit card")}),
    "demo-onepay": ("OnePay", {"onepay": ("OnePay Cash Rewards", "6612", "credit", "credit card")}),
    "demo-citi": ("Citi", {"citi": ("Citi Custom Cash", "5150", "credit", "credit card")}),
    "demo-bank": ("Example Credit Union", {"checking": ("Checking", "0420", "depository", "checking"),
                                           "savings": ("Savings", "7788", "depository", "savings")}),
}
CARDS = {"sapphire": 25, "prime": 25, "onepay": 12, "citi": 3}  # statement closing day
PAYEE = {"sapphire": "CHASE CREDIT CRD AUTOPAY", "prime": "CHASE CREDIT CRD AUTOPAY",
         "onepay": "ONEPAY CASHREWRD PAYMENT", "citi": "CITI CARD ONLINE PAYMENT"}
PAY_AFTER = 21  # days from a statement's close to its autopay
OPENING = {"checking": 6200.00, "savings": 8500.00}

# Daily chances: account, Plaid name, merchant, detailed category, channel, mcc, (low, high), chance a day
RANDOM = [
    ("sapphire", "Torchy's Tacos", "TORCHYS TACOS", "FOOD_AND_DRINK_RESTAURANT", "in store", "5812", (11, 34), 0.12),
    ("sapphire", "Chipotle", "CHIPOTLE 1187", "FOOD_AND_DRINK_FAST_FOOD", "in store", "5814", (9, 26), 0.12),
    ("sapphire", "Olive Garden", "OLIVE GARDEN 0441", "FOOD_AND_DRINK_RESTAURANT", "in store", "5812", (38, 92), 0.04),
    ("sapphire", "Starbucks", "STARBUCKS STORE 22817", "FOOD_AND_DRINK_COFFEE", "in store", "5814", (4, 9), 0.18),
    ("sapphire", "DoorDash", "DD DOORDASH THAI", "FOOD_AND_DRINK_RESTAURANT", "online", "5812", (22, 48), 0.06),
    ("sapphire", "Shell", "SHELL OIL 5744", "TRANSPORTATION_GAS", "in store", "5541", (31, 58), 0.10),
    ("sapphire", "Exxon", "EXXONMOBIL 4410", "TRANSPORTATION_GAS", "in store", "5541", (29, 55), 0.05),
    ("sapphire", "Costco", "COSTCO WHSE #0412", "GENERAL_MERCHANDISE_SUPERSTORES", "in store", "5300", (74, 265), 0.07),
    ("sapphire", "Costco Gas", "COSTCO WHSE GAS #0412", "TRANSPORTATION_GAS", "in store", "5542", (36, 63), 0.06),
    ("sapphire", "Target", "TARGET 00018374", "GENERAL_MERCHANDISE_SUPERSTORES", "in store", "5310", (18, 115), 0.06),
    ("sapphire", "Home Depot", "THE HOME DEPOT #6521", "HOME_IMPROVEMENT_HARDWARE", "in store", "5200", (14, 180), 0.03),
    ("sapphire", "Uber", "UBER TRIP", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "online", "4121", (11, 36), 0.04),
    ("sapphire", "AMC Theatres", "AMC 2291", "ENTERTAINMENT_TV_AND_MOVIES", "in store", "7832", (14, 42), 0.025),
    ("prime", "Amazon.com", "AMZN Mktp US", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", "online", "5942", (8, 120), 0.20),
    ("prime", "Whole Foods", "WHOLEFDS MKT 10233", "FOOD_AND_DRINK_GROCERIES", "in store", "5411", (24, 96), 0.12),
    ("prime", "Amazon Pharmacy", "AMAZON PHARMACY", "MEDICAL_PHARMACIES_AND_SUPPLEMENTS", "online", "5912", (9, 34), 0.02),
    ("onepay", "Walmart.com", "WALMART.COM", "GENERAL_MERCHANDISE_SUPERSTORES", "online", "5310", (14, 88), 0.06),
    ("sapphire", "Walmart", "WAL-MART #3319", "GENERAL_MERCHANDISE_SUPERSTORES", "in store", "5311", (12, 70), 0.04),
    ("citi", "Kroger", "KROGER #0588", "FOOD_AND_DRINK_GROCERIES", "in store", "5411", (28, 125), 0.10),
    ("citi", "Aldi", "ALDI 71042", "FOOD_AND_DRINK_GROCERIES", "in store", "5411", (22, 80), 0.07),
    ("citi", "CVS", "CVS/PHARMACY #04821", "MEDICAL_PHARMACIES_AND_SUPPLEMENTS", "in store", "5912", (6, 45), 0.04),
]
# Monthly: account, name, merchant, category, channel, mcc, day of month, amount (or a range)
MONTHLY = [
    ("sapphire", "Netflix", "NETFLIX.COM", "ENTERTAINMENT_TV_AND_MOVIES", "online", "4899", 5, 15.49),
    ("sapphire", "Spotify", "SPOTIFY USA", "ENTERTAINMENT_MUSIC_AND_AUDIO", "online", "5815", 12, 11.99),
    ("sapphire", "Planet Fitness", "PLANET FITNESS", "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", "other", "7997", 17, 24.99),
    ("prime", "Amazon", "Amazon", "MEDICAL_PHARMACIES_AND_SUPPLEMENTS", "online", "5912", 2, 5.00),  # RxPass
    ("checking", "Parkview Apartments", "PARKVIEW APTS RENT", "RENT_AND_UTILITIES_RENT", "other", None, 1, 1650.00),
    ("checking", "City Power & Light", "CITY POWER & LIGHT", "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY", "other", None, 8, (78, 172)),
    ("checking", "Spectrum", "SPECTRUM", "RENT_AND_UTILITIES_INTERNET_AND_CABLE", "online", None, 20, 65.00),
    ("checking", "Mint Mobile", "MINT MOBILE", "RENT_AND_UTILITIES_TELEPHONE", "online", None, 3, 45.00),
    ("checking", "State Farm", "STATE FARM INSURANCE", "GENERAL_SERVICES_INSURANCE", "other", None, 14, 118.40),
]
# Membership fees whose amount a Costco trip must never equal: the fee
# matcher would take it for one (rules.toml memberships.costco.fee_amounts,
# and each n/12 of the tier gap).
_FEE_LIKE = {60.0, 65.0, 120.0, 130.0} | {round(65 * n / 12, 2) for n in range(1, 12)}

RULES = """\
# A demo instance, made up by `penny demo`: every account, merchant amount and
# balance here is synthetic. Read over penny/defaults/rules.toml.

[held]
baseline_card = "sapphire_preferred"
cards = ["sapphire_preferred", "prime_visa", "walmart_card", "citi_custom_cash"]
memberships = ["prime", "walmart_plus", "costco"]
costco_tier = "gold_star"

[accounts]
"4821" = { card = "sapphire_preferred" }
"3007" = { card = "prime_visa" }
"6612" = { card = "walmart_card" }
"5150" = { card = "citi_custom_cash" }

[families.walmart_delivery]
# The weekly grocery delivery posts on the OnePay card as bare "Walmart".
accounts = ["6612"]

[cards.sapphire_preferred]
# Points valued at 1.5 cents.
default_rate = 0.015
rates = { restaurants = 0.045, gas = 0.045, costco_gas = 0.045 }
accounts = ["4821"]

[card_credits.sapphire_preferred]
year_starts = "{year_starts}"

[cashflow]
feed_card_payees = ["CHASE CREDIT CRD", "ONEPAY CASHREWRD", "CITI CARD"]
"""

ASSUMPTIONS = """\
# A demo instance's perk values, made up by `penny demo`. Annual dollars,
# low / base / high: what you would otherwise have paid, not the list price.

[prime.shipping]
ask = "Without Prime, how much would you pay in shipping a year?"
how = "Count the orders under the free-shipping minimum you'd have paid for, at about $6 each."
note = "Orders that would have paid shipping"
low = 20
base = 45
high = 80

[prime.video]
ask = "Would you pay for Prime Video on its own?"
how = "Worth $0 if you'd cancel it, $108 if you'd pay $8.99 a month."
low_if = "No, I'd cancel it"
high_if = "Yes, I'd pay for it"
low = 0
base = 0
high = 108

[prime.rxpass]
note = "RxPass generics against the same scripts priced elsewhere, less its $60 a year"
low = 0
base = 40
high = 120

[walmart_plus.delivery]
ask = "Without Walmart+, would you pay for grocery delivery?"
how = "The weekly order is 52 deliveries a year; paid delivery runs about $8-10 an order, pickup is free."
low_if = "No, I'd use free pickup"
high_if = "Yes, I'd pay about $9 an order"
low = 0
base = 60
high = 460

[walmart_plus.fuel]
note = "Per-gallon discount x gallons bought at participating stations"
low = 0
base = 0
high = 0

[costco.prices]
ask = "Is Costco cheaper on what you buy there?"
how = "Price your usual Costco basket at your other store: the gap over a year is the answer."
low_if = "No real saving"
low = 0
base = 150
high = 300

[costco.gas]
note = "Costco pump price vs the nearest station x gallons a year"
low = 0
base = 40
high = 80

[point_value.sapphire_preferred]
low = 0.01
high = 0.02

[card_worth]
alternatives = ["sapphire_preferred", "baseline"]

[card_worth.benefits.sapphire_preferred.hotel_credit]
note = "$100 a year Chase Travel hotel credit, used once so far"
verified = true
low = 0
base = 60
high = 100

[card_worth.benefits.sapphire_preferred.doordash]
note = "DashPass: delivery fees saved on about two orders a month"
verified = false
low = 0
base = 70
high = 140

[budget]
dining = 550
entertainment = 90
groceries = 900
health = 60
housing = 1650
insurance = 120
personal_care = 30
shopping = 520
subscriptions = 40
transport = 320
travel = 100
utilities = 290
"""


def _month_days(start: date, end: date, day: int) -> list[date]:
    """That day of each month from start to end, clamped to short months."""
    out, y, m = [], start.year, start.month
    while date(y, m, 1) <= end:
        d = date(y, m, min(day, calendar.monthrange(y, m)[1]))
        if start <= d <= end:
            out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _local_at(d: date) -> str:
    """A snapshot's time: now if it is today, else late on the day, local."""
    now = datetime.now().astimezone()
    return now.isoformat(timespec="seconds") if d == now.date() else \
        datetime.combine(d, time(22, 0)).astimezone().isoformat(timespec="seconds")


class _Rows:
    def __init__(self, rng: random.Random):
        self.rng, self.rows = rng, []

    def add(self, acct, d, name, merchant, amount, detailed, channel="other", mcc=None):
        self.rows.append({"account_id": acct, "date": d.isoformat(), "authorized_date": d.isoformat(), "pending": False,
                          "name": name, "merchant_name": merchant, "amount": round(amount, 2), "payment_channel": channel,
                          "merchant_category_code": mcc,
                          "personal_finance_category": {"primary": _primary(detailed), "detailed": detailed}})

    def amount(self, lo, hi, avoid=frozenset()):
        while True:
            a = round(self.rng.uniform(lo, hi), 2)
            if a not in avoid:
                return a


_PRIMARIES = ("FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "GENERAL_SERVICES", "TRANSPORTATION", "MEDICAL", "INCOME",
              "TRANSFER_OUT", "TRANSFER_IN", "LOAN_PAYMENTS", "RENT_AND_UTILITIES", "HOME_IMPROVEMENT", "ENTERTAINMENT",
              "PERSONAL_CARE", "TRAVEL", "BANK_FEES")


def _primary(detailed: str) -> str:
    return next(p for p in _PRIMARIES if detailed.startswith(p + "_"))


def generate(seed: int, today: date) -> tuple[list[dict], dict[str, float]]:
    """The year's rows, ending ``today``, and each card's last statement balance by account key."""
    rng = random.Random(seed)
    start = today - timedelta(days=364)
    r = _Rows(rng)
    d = start
    while d <= today:
        for acct, name, merchant, detailed, channel, mcc, (lo, hi), p in RANDOM:
            if rng.random() < p:
                avoid = _FEE_LIKE if name.startswith("Costco") else frozenset()
                r.add(acct, d, name, merchant, r.amount(lo, hi, avoid), detailed, channel, mcc)
        if d.weekday() == 5:  # the weekly grocery delivery, as OnePay posts it
            r.add("onepay", d, "Walmart", "Walmart", r.amount(52, 118), "GENERAL_MERCHANDISE_SUPERSTORES", "in store", "5411")
        if rng.random() < 0.025:
            r.add("prime", d, "Amazon.com", "AMZN Mktp US refund", -r.amount(9, 60), "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", "online", "5942")
        d += timedelta(days=1)
    for acct, name, merchant, detailed, channel, mcc, day, amt in MONTHLY:
        for md in _month_days(start, today, day):
            r.add(acct, md, name, merchant, r.amount(*amt) if isinstance(amt, tuple) else amt, detailed, channel, mcc)
    # Pay every two weeks, a transfer to savings and the interest it earns.
    d = start + timedelta(days=4)
    while d <= today:
        r.add("checking", d, "Acme Corp Payroll", "ACME CORP PAYROLL", -2480.00, "INCOME_SALARY")
        d += timedelta(days=14)
    for md in _month_days(start, today, 16):
        r.add("checking", md, "Transfer to Savings", "ONLINE TRANSFER TO SAV 7788", 400.00, "TRANSFER_OUT_SAVINGS")
        r.add("savings", md, "Transfer from Checking", "ONLINE TRANSFER FROM CHK 0420", -400.00, "TRANSFER_IN_SAVINGS")
    for md in _month_days(start, today, 31):
        r.add("savings", md, "Interest Paid", "INTEREST PAYMENT", -round(rng.uniform(18, 34), 2), "INCOME_INTEREST_EARNED")
    # The memberships, the Sapphire's fee and its hotel credit, once each.
    at = lambda n: start + timedelta(days=n)
    r.add("prime", at(40), "Amazon Prime", "Amazon Prime Membership", 139.00, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "online")
    r.add("onepay", at(95), "Walmart+", "WALMART+ ANNUAL", 98.00, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "online")
    r.add("sapphire", at(150), "WWW COSTCO COM", "Costco", 65.00, "GENERAL_MERCHANDISE_SUPERSTORES", "online", "5300")
    r.add("sapphire", at(20), "Annual Membership Fee", "ANNUAL MEMBERSHIP FEE", 95.00, "BANK_FEES_OTHER_BANK_FEES")
    r.add("sapphire", at(205), "Hyatt Place", "HYATT PLACE", 212.40, "TRAVEL_LODGING", "in store", "3640")
    r.add("sapphire", at(209), "Hotel Credit", "HOTEL CREDIT", -100.00, "TRANSFER_IN_OTHER_TRANSFER_IN")
    r.add("sapphire", at(200), "Delta Air Lines", "DELTA AIR LINES", 388.20, "TRAVEL_FLIGHTS", "online", "3058")
    # Statements, each paid in full from checking PAY_AFTER days after it closes.
    last_statement: dict[str, tuple[date, float]] = {}
    for card, close_day in CARDS.items():
        for close in _month_days(start, today, close_day):
            bal = round(sum(t["amount"] for t in r.rows if t["account_id"] == card and t["date"] <= close.isoformat()), 2)
            last_statement[card] = (close, bal)
            pay = close + timedelta(days=PAY_AFTER)
            if bal > 0 and pay <= today:
                r.add(card, pay, "Payment Thank You - Web", "", -bal, "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
                r.add("checking", pay, PAYEE[card], PAYEE[card], bal, "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    r.rows.sort(key=lambda t: (t["date"], t["account_id"], t["name"], t["amount"]))
    for i, t in enumerate(r.rows):
        t["transaction_id"] = f"demo{seed}-{i:05d}"
    return r.rows, last_statement


def _balance(rows: list[dict], acct: str, through: str) -> float:
    moved = sum(t["amount"] for t in rows if t["account_id"] == acct and t["date"] <= through)
    kind = next(a[2] for _, accts in ITEMS.values() for k, a in accts.items() if k == acct)
    return round(OPENING.get(acct, 0.0) - moved if kind == "depository" else moved, 2)


def exists(d: Path) -> bool:
    return any((d / n).exists() for n in ("rules.toml", "assumptions.toml", "data"))


def build(d: Path, seed: int = 0, today: date | None = None) -> Path:
    """Write the demo instance into ``d``. Refuses one that holds an instance already."""
    today = today or datetime.now().astimezone().date()
    d = Path(d)
    if exists(d):
        raise FileExistsError(f"{d} already holds an instance; the demo only writes into an empty directory")
    rows, last_statement = generate(seed, today)
    start = today - timedelta(days=364)
    fee = start + timedelta(days=20)
    d.mkdir(parents=True, exist_ok=True)
    fsio.write_atomic(d / "rules.toml", RULES.replace("{year_starts}", (fee - timedelta(days=5)).strftime("%m-%d")))
    fsio.write_atomic(d / "assumptions.toml", ASSUMPTIONS)
    store = plaid.Store(d, "production")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    store.save_items({item: {"access_token": "demo-not-a-token", "institution": inst, "cursor": "demo", "linked_at": now}
                      for item, (inst, _) in ITEMS.items()})
    snap_days = _month_days(start, today, 31) + [today]
    lines = []
    for item, (_, accts) in ITEMS.items():
        accounts = {k: {"account_id": k, "name": n, "mask": m, "type": t, "subtype": st} for k, (n, m, t, st) in accts.items()}
        txns = {t["transaction_id"]: t for t in rows if t["account_id"] in accts}
        store.save_ledger(item, {"accounts": accounts, "transactions": txns})
        for day in snap_days:
            for k, (n, m, t, st) in accts.items():
                cur = _balance(rows, k, day.isoformat())
                line = {"at": _local_at(day), "item_id": item, "account_id": k, "name": n, "mask": m, "type": t,
                        "subtype": st, "current": cur, "available": cur if t == "depository" else round(8000 - cur, 2),
                        "limit": None if t == "depository" else 8000.0, "currency": "USD"}
                if t == "credit" and day == today and k in last_statement:
                    closed, bal = last_statement[k]
                    line.update({"last_statement_balance": bal, "last_statement_issue_date": closed.isoformat(),
                                 "minimum_payment_amount": min(bal, 35.0) if bal > 0 else 0.0,
                                 "next_payment_due_date": (closed + timedelta(days=PAY_AFTER)).isoformat()})
                lines.append(line)
    store.append_balances(lines)
    store.save_last_sync({"at": now, "ok": True, "items": {inst: "ok" for inst, _ in ITEMS.values()}, "retry": True})
    return d


def run(home_flag: str | None, seed: int, no_board: bool, port: int) -> None:
    """``penny demo``: build into --home (refused if it holds an instance) or a
    new temporary directory, then serve the board on it on loopback."""
    if home_flag:
        d = Path(home_flag).expanduser().absolute()
    else:
        d = Path(tempfile.mkdtemp(prefix="penny-demo-"))
    try:
        build(d, seed)
    except FileExistsError as e:
        sys.exit(str(e))
    print(f"demo instance (synthetic data, seed {seed}): {d}")
    print(f"  use it with: penny --home {d} report")
    if no_board:
        return
    from . import board

    board.serve(d, host="127.0.0.1", port=port)
