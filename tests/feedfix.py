"""The fixture export (fixtures/sample.csv) as a Plaid ledger: the same fifteen
rows, with the fields Plaid adds (category, mask, channel, merchant code), so a
test can run on the feed and keep the assertions it had on the export."""

from penny import plaid

ACCOUNTS = {"prime": ("Prime Visa", "1234", "credit"), "walmart": ("Walmart Card", "5678", "credit"),
            "visa": ("Visa", "9999", "credit"), "checking": ("Checking", "1111", "depository")}

# date, account, name, merchant (the export's Description), amount, Plaid detailed category, channel, mcc
ROWS = [
    ("2026-01-05", "prime", "Amazon.com", "AMZN Mktp US", 120.00, "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", "online", "5942"),
    ("2026-01-06", "prime", "Amazon Pharmacy", "AMAZON PHARMACY", 45.00, "MEDICAL_PHARMACIES_AND_SUPPLEMENTS", "online", "5912"),
    ("2026-01-07", "prime", "Whole Foods", "WHOLEFDS ATX", 80.00, "FOOD_AND_DRINK_GROCERIES", "in store", "5411"),
    ("2026-01-08", "prime", "Amazon Prime", "Amazon Prime Membership", 139.00, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "online", None),
    ("2026-01-10", "walmart", "Walmart.com", "WALMART.COM", 200.00, "GENERAL_MERCHANDISE_SUPERSTORES", "online", "5310"),
    ("2026-01-11", "walmart", "Walmart", "WAL-MART #1234", 150.00, "GENERAL_MERCHANDISE_SUPERSTORES", "in store", "5411"),
    ("2026-01-12", "walmart", "Walmart+", "WALMART+ ANNUAL", 98.00, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "online", None),
    ("2026-01-15", "visa", "Costco", "COSTCO WHSE #0123", 300.00, "GENERAL_MERCHANDISE_SUPERSTORES", "in store", "5300"),
    ("2026-01-16", "visa", "Costco Gas", "COSTCO WHSE GAS #0123", 60.00, "TRANSPORTATION_GAS", "in store", "5542"),
    ("2026-01-17", "visa", "Shell", "SHELL OIL", 40.00, "TRANSPORTATION_GAS", "in store", "5541"),
    ("2026-01-18", "visa", "Torchy's Tacos", "TORCHYS TACOS", 25.00, "FOOD_AND_DRINK_RESTAURANT", "in store", "5812"),
    ("2026-01-19", "visa", "Amazon.com", "AMZN Mktp US refund", -20.00, "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", "online", "5942"),
    ("2026-01-20", "checking", "Paycheck", "ACME PAYROLL", -3000.00, "INCOME_SALARY", "other", None),
    ("2026-01-21", "checking", "Transfer", "TO SAVINGS", 500.00, "TRANSFER_OUT_ACCOUNT_TRANSFER", "other", None),
    ("2026-01-22", "visa", "Some Merchant", "SOME MERCHANT", 75.00, "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE", "in store", "5999"),
]


def _primary(detailed: str) -> str:
    for p in ("GENERAL_MERCHANDISE", "GENERAL_SERVICES", "FOOD_AND_DRINK", "TRANSPORTATION", "MEDICAL", "INCOME", "TRANSFER_OUT"):
        if detailed.startswith(p + "_"):
            return p
    raise ValueError(detailed)


def install(root) -> plaid.Store:
    """Write the ledger under ``root/data/plaid/production/``, where ``feed.load(root)`` reads it."""
    store = plaid.Store(root, "production")
    store.save_items({"fix": {"access_token": "t"}})
    accounts = {k: {"name": n, "mask": m, "type": t} for k, (n, m, t) in ACCOUNTS.items()}
    txns = {}
    for i, (d, acct, name, merchant, amt, detailed, channel, mcc) in enumerate(ROWS):
        txns[f"fix{i}"] = {"transaction_id": f"fix{i}", "account_id": acct, "date": d, "authorized_date": d, "pending": False,
                           "name": name, "merchant_name": merchant, "amount": amt, "payment_channel": channel,
                           "merchant_category_code": mcc,
                           "personal_finance_category": {"primary": _primary(detailed), "detailed": detailed}}
    store.save_ledger("fix", {"accounts": accounts, "transactions": txns})
    return store
