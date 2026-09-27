# Competitor analysis

What else is out there, what it does better than penny, and whether any of
it changes what penny should be. Findings come from reading each project's
source, not its README; commit ids and dates are when it was read, and a
project's own claims are marked as such.

## The short version

None of the projects below answers penny's question: whether a paid
membership, and the store card that comes with it, pays for itself on your
own spending. Each one tracks where money went. A card's annual fee is at
most a display field or a recurring bill, a cost with nothing set against
it. That question is penny's reason to exist, and it stays penny's.

Where they win is the budget-tracker half: accounts, budgets, recurring
bills, net worth, investments, mobile apps. Sure in particular is years of
work ahead of penny's board there. The open question is therefore not
"switch?" but "should penny keep growing its own tracker, or read
transactions from one?" — see [Sure as a transaction source](#sure-as-a-transaction-source).

| | penny | Sure | KevFin | Securo |
|---|---|---|---|---|
| Membership / card worth-it | **yes — the point** | no | no | no |
| Budget tracker | basic board | mature | solid | mature |
| Bank sync | Plaid (own keys), CSV | ~20 providers incl. Plaid, SimpleFIN | Plaid, SimpleFIN | Pluggy, Enable Banking, SimpleFIN; OFX/QIF/CAMT/CSV — no Plaid |
| Stack | Python, flat files | Rails, Postgres, Redis, Sidekiq | TypeScript, Express, SQLite | FastAPI, Postgres + pgvector, Redis, Celery |
| Hosting | local CLI + loopback board | Docker, 5 services | Docker / Electron | Docker (7 services), Helm |
| Auth | none; loopback only | API keys, OAuth | none; LAN by design | JWT (24 h), TOTP, passkeys, OIDC |
| License | MIT | AGPL-3.0 | MIT | AGPL-3.0 |
| Activity | — | ~200 commits/month | quiet since 2026-07-28 | ~110 commits/month, releases every few days |

## What penny needs from a transaction source

Written down once so each candidate can be checked against it. Per row, for
the verdict math to work as it does today:

- **date and signed amount** — the trailing-365-day window and spend per
  family (`penny/model.py`).
- **the raw bank descriptor and the clean merchant name**, both. Family and
  fee patterns run on `Txn.match_text`, which is `custom_name | name |
  description` joined (`penny/load.py`), and some default patterns rely on
  that joined shape — the Costco fee pattern expects both halves to read
  "costco".
- **the account's last four** — `[accounts]`, a card's own spend in
  `cardworth.py`, and family `accounts` filters.
- **a category the family fallback can match** — today Plaid's
  `personal_finance_category.detailed`, which `[categories.plaid]` and the
  family `categories` lists are keyed on.
- **transfer / payment / income classification** — today from the PFC
  primary (`penny/feed.py`).
- **posted rows only**, or a pending flag to drop them.
- **a stable id** — hand overrides are keyed on it.

Nice to have: **MCC** (the gas, restaurant, drugstore and grocery fallback)
and **payment channel** (the online-vs-store split for Walmart). Balances and
liabilities are only needed by the checks, net-worth and cash-flow pages,
not by any verdict.

## Sure

<https://github.com/we-promise/sure>, read at `1052443` (2026-09-27).

The community fork of Maybe Finance, kept alive after the company behind it
stopped. A full personal-finance app: accounts, transactions, budgets,
investments, net worth, an AI assistant, web plus desktop and mobile
clients.

**Stack and hosting.** Ruby on Rails. `compose.example.yml` runs five
services: web, a Sidekiq worker, Postgres 16, Redis, and an optional backup.
Its docs claim it runs in 512 MB with caveats and recommend 4 GB / 2 vCPU on
a VPS; not measured here.

**Bank sync.** About twenty provider adapters (`app/models/provider/`):
Plaid US and EU, SimpleFIN, Enable Banking, Mercury, Wise, several brokers
and crypto exchanges, and more. Plaid uses your own client id and secret,
pulls `/transactions/sync` with `include_original_description`, plus
liabilities and investments. Plaid items sync on Plaid's webhooks and when
someone opens the app — not on a schedule — and `docs/hosting/plaid.md`
says the instance must be reachable from the internet over HTTPS for those
webhooks.

**What it keeps from Plaid** (`plaid_entry/processor.rb`):

| Plaid field | In Sure |
|---|---|
| `transaction_id` | `entries.external_id` |
| `date` | `entries.date` |
| `merchant_name` | a `provider_merchants` row |
| `name` | merchant name and original description joined into one string |
| `pending`, `original_description`, `payment_channel` | `transactions.extra` (jsonb) |
| `personal_finance_category` | used once to pick one of your categories, then dropped |
| `authorized_date` | never read |
| `merchant_category_code` | not found |

Pending rows are skipped unless a setting turns them on. The raw payload
column is overwritten every sync, so it is not a history.

**API.** A real REST API (`app/controllers/api/v1/`, about 40 controllers)
with API-key or OAuth auth. `GET /api/v1/transactions` pages 100 at a time
and filters by date range, account, category, merchant, amount, tags and
text. The JSON carries id, date, amount in cents, name, merchant, category,
account, `external_id` and timestamps — but **not** `extra`, so no pending
flag, raw descriptor or payment channel, and no Plaid category because none
is kept. There is no `updated_since`, no outbound webhook, and the CSV
export drops ids and merchants.

**Rewards and memberships.** None. The credit-card table has `annual_fee`
and `apr` as display fields; nothing computes value from them.

**Health.** v0.7.4 (2026-08-31), alphas continuing weekly; roughly 200
commits a month on `main`; about 10k stars, 100+ contributors, 600 open
issues. AGPL-3.0.

### Sure as a transaction source

Possible, but not as a drop-in, and not through its API alone.

- Against the list above, the API supplies date, amount, a stable id, the
  clean merchant, and account (but check whether the last four survives as
  anything better than an account name). It does **not** supply the raw
  descriptor, the Plaid category, the MCC or the payment channel, and
  categories are Sure's own, so `[categories.plaid]` and every family
  `categories` list would need a Sure-category equivalent.
- Getting the raw descriptor and pending flag means reading Sure's Postgres
  directly, which ties penny to Sure's schema. The Plaid category and MCC
  aren't stored anywhere, so no route recovers them.
- Sure's Plaid sync wants an internet-facing HTTPS endpoint for webhooks,
  which is a bigger exposure than penny's loopback-only board.
- What penny would shed: `plaid.py` and the Plaid half of `feed.py` (about
  750 lines plus 490 of tests) and roughly 1,000 lines of budget-app board
  pages. The verdict core (about 1,500 lines) stays either way.

A cheaper middle path is to leave penny's own Plaid sync as the feed and use
Sure only as the day-to-day tracker, linked to the same banks separately. That
costs a second Plaid item per bank, and the two tools disagree on categories.

## KevFin

<https://github.com/kxl3785/KevFin>, read 2026-09-27 (last push
2026-07-28).

A one-author, self-hosted net-worth tracker packaged for others to run:
releases v1.1.0–v1.4.0, a GHCR image, an Electron desktop app, Synology and
Portainer compose files.

**Stack.** TypeScript throughout: Express and better-sqlite3 on the server
(about 14.6k lines), React, Vite and TanStack Query on the client (about
11k). node-cron refreshes accounts daily.

**Ingestion.** SimpleFIN and Plaid (`transactionsGet`, not
`/transactions/sync`); a Monarch-shaped CSV import with a 3-day
feed-vs-CSV duplicate window; and PDF, image and CSV import by shelling out
to a local Claude Code CLI that proposes rows for the user to confirm. One
SQLite file. Transactions keep payee (Plaid's `merchant_name`),
description, posted and transacted dates; Plaid's category is not kept.

**Features.** Budgets with a Sankey cash-flow view, recurring-bill
detection from billing cadence (annual fees spread across twelve months),
net-worth history, fund look-through via SEC N-PORT filings, Zillow home
values, a Monte Carlo retirement forecast, a PDF quarterly report, and a
chat assistant.

**Rewards and memberships.** None. Costco and Walmart appear only in the
groceries regex; an annual fee is a monthly cost with nothing set against
it.

**Security posture.** No authentication, by design ("LAN/Tailscale only");
the server listens on all interfaces with permissive CORS, and bank tokens
sit in plaintext in SQLite. The chat assistant and document import send
financial data to Claude, which qualifies the README's "data never leaves"
claim. An encrypted export (PBKDF2 + AES-GCM) exists. No real data found
committed in the last 50 commits.

**Health.** MIT. 12 stars, one human contributor, 127 commits in a burst
from 2026-06-26 to 2026-07-28, nothing since. About 260 Vitest cases,
including golden snapshot tests.

## Securo

<https://github.com/securo-finance/securo>, read at `8b75707`
(2026-09-25).

A self-hosted budgeting and net-worth app in the same family as Actual or
Firefly III, started around Brazilian banks: Pluggy is its first provider,
bills default to BRL, and it handles *parcelamento* installments. Website,
public demo, docs and roadmap; no paid tier or open-core split found in the
code (a hosted plan on its website was not checked).

**Stack and hosting.** FastAPI with fastapi-users, async SQLAlchemy and
Alembic; Celery worker and beat on Redis; Postgres 16 with pgvector; a
React/TypeScript frontend; a separate MCP server for LLM clients. About 152k
lines of Python, 84k of them tests. Compose runs seven containers (redis,
db, backend, frontend, celery-worker, celery-beat, mcp-server); a Helm chart
and `install.sh` also exist.

**Bank sync.** Pluggy (Brazil), Enable Banking (EU PSD2) and SimpleFIN (US),
all with your own keys. **No Plaid.** File import covers OFX, QIF, CAMT and
CSV; PDFs are only generated (invoices), never parsed.

**What it keeps per transaction** (`models/transaction.py`): `description`
plus `original_description` (the raw text before rules rewrite it), payee,
category, `external_id`, `date` and `effective_date`, a posted/pending
status, currency and FX, installment and bill links, transfer pairing, and
`raw_data`, the whole provider JSON. Account masks are last four only. No
MCC, no payment channel, no separate authorized date.

**API.** Everything is REST: `GET /api/transactions` filters by date range,
account, category, payee, status, amount and text, and pages up to 500.
CSV export and a backup zip (optionally AES-256). Auth is a 24-hour JWT from
fastapi-users; there are no long-lived personal API tokens, so a scheduled
script has to log in each time. No webhooks. Whether the API returns
`raw_data` was not confirmed.

**Features.** Budgets, goals, recurring transactions, net worth, reports,
investments with market prices, multi-currency, workspaces and multiple
users, shared-expense groups, invoicing, reconciliation, attachments, a
bill-based cash forecast. Categorisation is a rule engine (contains,
starts/ends with, equals, regex, on any field); the LLM agents are for chat
and RAG.

**Rewards and memberships.** None. Credit cards are accounts with a limit,
statement close and due days, brand and level, bills and installments. No
reward rates, fees or merchant comparison.

**Security posture.** TOTP, passkeys, OIDC and rate limiting. Provider
credentials and LLM keys are Fernet-encrypted with a key derived from
`SECRET_KEY`, so anyone who has that key can decrypt them; the rest of the
database relies on Postgres alone. No telemetry found. Committed fixtures
were not audited.

**Health.** AGPL-3.0. Created 2026-03-08; about 3.8k stars, 500 forks, 91
contributors, but one maintainer wrote the large majority of commits.
v0.16.2 on 2026-09-25, with releases every few days; about 110 commits a
month in August and September.

**As a transaction source.** Weaker than Sure for penny. The raw descriptor
survives (`original_description`), which Sure's API doesn't give you, but
there's no Plaid, so US banks go through SimpleFIN, and MCC and payment
channel are lost either way. Only worth it for someone already running
Securo on SimpleFIN or EU banks.

## Ideas worth borrowing

- **A live match count in the rule editor** (KevFin's `RuleSuggestModal`):
  show how many transactions a pattern hits while it's being written. penny's
  family patterns are regexes edited blind in `rules.toml`.
- **Fee detection from billing cadence** (KevFin `recurring.ts`): find the
  renewal in the feed from its yearly rhythm instead of only `fee_patterns`
  and `fee_amounts`, useful for memberships the catalogue doesn't list yet.
- **Feed-vs-CSV dedup inside a date window** (KevFin): relevant if a CSV
  import and the Plaid feed ever overlap the same card.
- **Append-only balance observations** where an estimate never overwrites a
  real reading (KevFin); penny's `balances.jsonl` is already append-only but
  has no estimated/real distinction.
- **Golden characterization tests** of whole-report output (KevFin), which
  would pin the verdict table against a fixed synthetic year.
- **SimpleFIN** (Sure, KevFin): a cheaper bank link than Plaid for a single
  user, if its fields cover the list above — not checked here.
- **Keep the raw text beside the rewritten one** (Securo's
  `original_description` next to `description`): any future merchant clean-up
  in penny should never overwrite the descriptor the family patterns match.
- **Rules as field / operator / value** with regexes compiled under a safety
  policy (Securo `rule_engine.py`): a structured form of what
  `[[categories.rules]]` does with bare regexes.
- **A password-encrypted backup** (KevFin, Securo): penny's instance
  directory holds Plaid access tokens and has no export of its own.

## What this doesn't change

penny's security stance — loopback by default, no login yet, nothing
listening on the network unless you ask — is stricter than KevFin's and
simpler than Sure's, and nothing here argues for loosening it. The rate
catalogue in `penny/defaults/rules.toml` has no counterpart in any of these
projects.
