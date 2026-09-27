from datetime import date

import pytest

from penny import statements

# Layouts as pdftotext -layout prints them (spacing trimmed); names and amounts invented.
ONEPAY = """\
  JANE DOE
  Account Number ending in 0001
PAGE 1 of 3  Visit us at web.onepay.com or Call 1-866-796-1537
Account Summary
Previous Balance as of 12/29/2025  $100.00  Credit Limit  $8,000
  Payments  - 100.00
  Other Credits  - 12.00
  Purchases/Debits  + 60.50
New Balance as of 01/28/2026  $48.50
31 Day Billing Cycle from 12/29/2025 to 01/28/2026
Transaction Detail
Date  Reference #  Description  Amount
Payments  - $100.00
01/04  8141021EG00XS6H12  ONLINE PAYMENT THANK YOU  -$100.00
Other Credits  - $12.00
12/30  F893900EA000OP058  REWARDS STATEMENT CREDIT  -$10.00
01/10  8141021EG00XS6H99  WALMART.COM BENTONVILLE AR  -$2.00
Purchases and Other Debits  $60.50
12/31  8141021ED00XTMJG0  WALMART.COM BENTONVILLE AR  $50.25
  Merchandise/Consumables
01/15  8141021EE00XTMJG9  WALMART.COM BENTONVILLE AR  $10.25
Total Fees Charged This Period  $0.00
Total Interest Charged This Period  $0.00
01/28  INTEREST CHARGE ON PURCHASES  $0.00
(Continued on next page)
PAGE 2 of 3  Visit us at web.onepay.com or Call 1-866-796-1537
Transaction Detail (Continued)
01/28  INTEREST CHARGE ON CASH ADVANCES  $0.00
  2026 Year-to-Date Fees and Interest
  Total Fees Charged  $0.00
"""

APPLE = """\
  Statement
Apple Card Customer
Jane Doe, jane@example.com  Aug 1 — Aug 31, 2026
Your August Balance  Minimum  Payment
as of Aug 31, 2026  Payment Due  Due By
$55.82  $25.00  Sep 30, 2026
  Previous Monthly Balance  $50.00
  as of Jul 31, 2026
Payments
Date  Description  Amount
08/31/2026  ACH Deposit Internet transfer from account ending in 0000  -$50.00
Total payments for this period  -$50.00
Transactions
Date  Description  Daily Cash  Amount
08/04/2026  PIZZA PLACE STREAMWOOD 60107 IL USA  1%  $0.32  $31.61
08/29/2026  APPLE.COM/BILL ONE APPLE PARK WAY CUPERTINO 95014 CA USA (RETURN)  -$10.00
  Daily Cash Adjustment  -3%  $0.30
Total Daily Cash this month  $0.32
Total charges, credits and returns  $21.91
Apple Card Monthly Installments
Dates  Description  Daily Cash  Amounts
08/09/2026  Apple Online Store Cupertino CA  3%  $24.42  $814.00
  TRANSACTION #df5896f3b69b
  This month’s installment: $33.91
  Final installment: Aug 31, 2028
Total financed  $814.00
Daily Cash
Daily Cash from Apple Card  $0.32
Interest Charged
Total interest for this month  $0.00
"""


def test_onepay_lines_kinds_and_new_year():
    st = statements.parse_text(ONEPAY)
    assert (st.issuer, st.mask, st.start, st.end, st.previous, st.new) == \
        ("onepay", "0001", date(2025, 12, 29), date(2026, 1, 28), 100.0, 48.5)
    kinds = [(l.date, l.kind, l.amount) for l in st.lines if l.amount]
    assert kinds == [(date(2026, 1, 4), "payment", -100.0), (date(2025, 12, 30), "reward credit", -10.0),
                     (date(2026, 1, 10), "refund", -2.0), (date(2025, 12, 31), "purchase", 50.25),
                     (date(2026, 1, 15), "purchase", 10.25)]


def test_apple_monthly_balance_counts_the_installment_not_the_lump_sum():
    st = statements.parse_text(APPLE)
    assert (st.issuer, st.start, st.end, st.previous, st.new) == ("apple", date(2026, 8, 1), date(2026, 8, 31), 50.0, 55.82)
    got = [(l.kind, l.amount) for l in st.lines]
    assert got == [("payment", -50.0), ("purchase", 31.61), ("refund", -10.0), ("reward credit", 0.3),
                   ("installment", 33.91)]
    assert st.lines[1].description == "PIZZA PLACE STREAMWOOD 60107 IL USA"


def test_a_missed_line_fails_the_parse_rather_than_shortening_the_list():
    with pytest.raises(statements.ParseError, match="a line was missed"):
        statements.parse_text(ONEPAY.replace("01/15  8141021EE00XTMJG9  WALMART.COM BENTONVILLE AR  $10.25\n", ""))


def test_other_pdfs_are_refused():
    with pytest.raises(statements.ParseError):
        statements.parse_text("Chase Sapphire Preferred statement")
