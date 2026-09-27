"""Plaid feed: Link page, token exchange and cursor-based /transactions/sync.

Keys come from data/plaid.env (scripts/plaid-secrets.sh). Everything this writes
lives under data/plaid/<env>/, mode 600: items.json holds the access tokens and
cursors, <item_id>.json the accounts and current transactions, raw/ every sync
page as Plaid sent it so a parser bug can be replayed.
"""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import fsio

HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}
# First Platypus Bank: a non-OAuth Sandbox institution, so no redirect URI is needed.
SANDBOX_INSTITUTION = "ins_109508"

# Plaid errors a later try can clear: its own outages, the bank's, rate limits,
# and a garbled (non-JSON) answer. RATE_LIMIT_EXCEEDED is an error_type.
TRANSIENT_CODES = {"INTERNAL_SERVER_ERROR", "PLANNED_MAINTENANCE", "INSTITUTION_DOWN",
                   "INSTITUTION_NOT_RESPONDING", "INSTITUTION_NOT_AVAILABLE", "BAD_RESPONSE"}
# Errors only you can clear, by signing in to the bank again through Link's update mode.
RELOGIN_CODES = {"ITEM_LOGIN_REQUIRED", "ITEM_LOCKED", "PENDING_EXPIRATION", "PENDING_DISCONNECT"}
RETRY_DELAYS = (2, 10, 30)  # seconds between tries of one request: four tries in all
MUTATION_DELAYS = (1, 5)  # seconds before each restart of a sync run Plaid said changed under it


class PlaidError(RuntimeError):
    def __init__(self, body: dict, status: int | None = None):
        self.code = body.get("error_code", "") or ""
        self.type = body.get("error_type", "") or ""
        self.status = status
        super().__init__(f"{self.type} {self.code}: {body.get('error_message', '')}")

    @property
    def transient(self) -> bool:
        return (self.status == 429 or (self.status or 0) >= 500 or self.type == "RATE_LIMIT_EXCEEDED"
                or self.code in TRANSIENT_CODES)

    @property
    def needs_relogin(self) -> bool:
        return self.code in RELOGIN_CODES


class SyncMutationError(RuntimeError):
    """Transactions changed under every restart of a /transactions/sync run."""


class SyncBusy(RuntimeError):
    """Another sync holds the lock."""


def transient(e: BaseException) -> bool:
    """Worth another try of the same request: a network failure, or a Plaid error that says so."""
    if isinstance(e, PlaidError):
        return e.transient
    return isinstance(e, (SyncMutationError, OSError, http.client.HTTPException))


def retry_worthy(e: BaseException) -> bool:
    """Worth rerunning the whole sync later: anything but a Plaid error that won't clear by itself."""
    return not isinstance(e, PlaidError) or e.transient


def failure_text(e: BaseException, item_id: str, env: str) -> str:
    """What last-sync.json (and so the board) says about a failure, with the next step when there is one."""
    msg = str(e) if isinstance(e, (PlaidError, OSError)) else f"{type(e).__name__}: {e}"
    if isinstance(e, PlaidError) and e.needs_relogin:
        msg += f" — sign in again: penny plaid link --env {env} --relogin {item_id}"
        if env == "production":
            msg += " --redirect-uri https://YOUR-HOST/"  # production Link refuses to start without one
    return msg


def load_env(root: Path) -> dict[str, str]:
    env = {}
    for line in (root / "data" / "plaid.env").read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.startswith("#"):
            env[k.strip()] = v.strip()
    return env


def _error_body(status: int, raw: bytes, reason) -> dict:
    """Plaid's JSON error, or one made from whatever a proxy or outage page sent instead."""
    try:
        body = json.loads(raw)
        if isinstance(body, dict) and (body.get("error_code") or body.get("error_type")):
            return body
    except ValueError:
        pass
    snippet = " ".join(raw[:200].decode("utf-8", "replace").split()) or str(reason)
    return {"error_type": "HTTP_ERROR", "error_code": f"HTTP_{status}", "error_message": snippet}


class Client:
    def __init__(self, env: str, client_id: str, secret: str, sleep=None, delays: tuple = RETRY_DELAYS):
        self.env, self.host = env, HOSTS[env]
        self.client_id, self.secret = client_id, secret
        self.sleep, self.delays = sleep, tuple(delays)

    @classmethod
    def from_root(cls, root: Path, env: str) -> Client:
        keys = load_env(root)
        return cls(env, keys["PLAID_CLIENT_ID"], keys[f"PLAID_SECRET_{env.upper()}"])

    def post(self, path: str, body: dict) -> dict:
        """One Plaid call, tried again after a transient failure up to len(delays) more times.

        ITEM_LOGIN_REQUIRED, INVALID_* and the like raise at once: no wait clears them.
        """
        data = json.dumps({"client_id": self.client_id, "secret": self.secret, **body}).encode()
        for attempt in range(len(self.delays) + 1):
            try:
                return self._post_once(path, data)
            except Exception as e:
                if attempt == len(self.delays) or not transient(e):
                    raise
                (self.sleep or time.sleep)(self.delays[attempt])
        raise AssertionError("unreachable")

    def _post_once(self, path: str, data: bytes) -> dict:
        req = urllib.request.Request(self.host + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            raise PlaidError(_error_body(e.code, raw, e.reason), e.code) from None
        try:
            return json.loads(raw)
        except ValueError:
            snippet = " ".join(raw[:200].decode("utf-8", "replace").split())
            raise PlaidError({"error_type": "HTTP_ERROR", "error_code": "BAD_RESPONSE",
                              "error_message": f"not JSON: {snippet}"}, 200) from None


class Store:
    """data/plaid/<env>/: items.json (tokens, cursors), <item_id>.json (rows), raw/.

    items.json is only rewritten under an flock on .items.lock and re-read
    inside it, so a link and a sync (or two syncs) can't drop each other's
    changes; the lock is never held across a network call.
    """

    def __init__(self, root: Path, env: str):
        self.dir = root / "data" / "plaid" / env
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir.parent, 0o700)
        os.chmod(self.dir, 0o700)

    def client_user_id(self) -> str:
        """This instance's Link ``client_user_id``: a random id made on first
        use and kept in data/plaid/client-user-id, shared by every env. Plaid
        only reads it when a Link token is made."""
        path = self.dir.parent / USER_ID_FILE
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_text().strip()
        uid = secrets.token_hex(16)
        with os.fdopen(fd, "w") as f:
            f.write(uid + "\n")
            f.flush()
            os.fsync(f.fileno())
        return uid

    @property
    def env(self) -> str:
        return self.dir.name

    def _write_bytes(self, path: Path, data: bytes) -> None:
        fsio.write_atomic(path, data, mode=0o600)

    def _write(self, path: Path, obj) -> None:
        self._write_bytes(path, json.dumps(obj, indent=1).encode())

    def _read(self, path: Path, default):
        return json.loads(path.read_text()) if path.exists() else default

    @contextmanager
    def _flock(self, name: str, timeout: float | None = None, sleep=None, busy: str = ""):
        fd = os.open(self.dir / name, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if timeout is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise SyncBusy(busy) from None
                        (sleep or time.sleep)(1)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the lock

    def items_lock(self):
        """Held only for a read-modify-write of items.json. Not reentrant: don't nest it."""
        return self._flock(".items.lock")

    def sync_lock(self, timeout: float | None = None, sleep=None):
        """One sync at a time; waits up to ``timeout`` seconds (None: forever), then raises SyncBusy."""
        return self._flock(".sync.lock", timeout, sleep, f"another penny plaid sync ({self.env}) is still running")

    def items(self) -> dict:
        return self._read(self.dir / "items.json", {})

    def _save_items(self, items: dict) -> None:
        """Caller holds items_lock. The previous good items.json is kept as items.json.bak."""
        path = self.dir / "items.json"
        if path.exists():
            old = path.read_bytes()
            try:
                json.loads(old)
            except ValueError:
                pass  # a damaged file is no backup: keep the last good one
            else:
                self._write_bytes(self.dir / "items.json.bak", old)
        self._write(path, items)

    def save_items(self, items: dict) -> None:
        with self.items_lock():
            self._save_items(items)

    def update_item(self, item_id: str, **fields) -> bool:
        """Set fields on one Item, re-reading items.json under the lock. False if the Item is gone."""
        with self.items_lock():
            items = self.items()
            if item_id not in items:
                return False
            items[item_id].update(fields)
            self._save_items(items)
            return True

    def put_item(self, item_id: str, entry: dict) -> None:
        with self.items_lock():
            items = self.items()
            items[item_id] = entry
            self._save_items(items)

    def ledger(self, item_id: str) -> dict:
        return self._read(self.dir / f"{item_id}.json", {"accounts": {}, "transactions": {}})

    def save_ledger(self, item_id: str, ledger: dict) -> None:
        self._write(self.dir / f"{item_id}.json", ledger)

    def append_balances(self, lines: list[dict]) -> None:
        fd = os.open(self.dir / "balances.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.writelines(json.dumps(line) + "\n" for line in lines)
            f.flush()
            os.fsync(f.fileno())

    def balances(self) -> list[dict]:
        path = self.dir / "balances.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []

    def last_sync(self) -> dict | None:
        return self._read(self.dir / "last-sync.json", None)

    def save_last_sync(self, obj: dict) -> None:
        self._write(self.dir / "last-sync.json", obj)

    def save_raw(self, item_id: str, pages: list[dict]) -> Path:
        # Microseconds, and never reuse a name: two syncs in one second once
        # overwrote a page, and the raw pages are the replay record.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.dir / "raw" / item_id / f"sync-{stamp}.json"
        while path.exists():
            path = path.with_name(path.stem + "-1.json")
        self._write(path, pages)
        return path


def fetch_sync(client, access_token: str, cursor: str | None, retries: int = 3,
               sleep=None, delays: tuple = MUTATION_DELAYS) -> tuple[list[dict], str]:
    """Every page from `cursor` to the end, and the new cursor.

    Plaid's rule: on TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION, restart the
    whole run from the cursor it began with, not the page that failed, after a
    short wait so the bank's update can settle.
    """
    for attempt in range(retries):
        if attempt:
            (sleep or time.sleep)(delays[min(attempt - 1, len(delays) - 1)])
        pages, cur = [], cursor
        try:
            while True:
                body = {"access_token": access_token, "count": 500}
                if cur:
                    body["cursor"] = cur
                page = client.post("/transactions/sync", body)
                pages.append(page)
                cur = page["next_cursor"]
                if not page["has_more"]:
                    return pages, cur
        except PlaidError as e:
            if e.code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                raise
    raise SyncMutationError("transactions changed during every sync attempt; try again shortly")


def apply_pages(ledger: dict, pages: list[dict]) -> dict[str, int]:
    """Fold sync pages into the ledger in order; counts of added/modified/removed."""
    tx, n = ledger["transactions"], {"added": 0, "modified": 0, "removed": 0}
    for page in pages:
        for a in page.get("accounts", []):
            ledger["accounts"][a["account_id"]] = a
        for kind in ("added", "modified"):
            for t in page.get(kind, []):
                tx[t["transaction_id"]] = t
                n[kind] += 1
        for t in page.get("removed", []):
            if tx.pop(t["transaction_id"], None) is not None:
                n["removed"] += 1
    return n


def _pull(client, store: Store, item_id: str, access_token: str, cursor: str | None) -> tuple[list[dict], str, Path]:
    """One /transactions/sync run, its pages kept in raw/ as Plaid sent them."""
    pages, cursor = fetch_sync(client, access_token, cursor)
    return pages, cursor, store.save_raw(item_id, pages)


def _commit(store: Store, item_id: str, pages: list[dict], cursor: str, raw: Path) -> dict:
    """Fold pages into the ledger, then move this Item's cursor on.

    Only this Item's keys are written, re-read under the items.json lock, so a
    link or another Item's update made during the network calls survives.
    """
    ledger = store.ledger(item_id)
    n = apply_pages(ledger, pages)
    store.save_ledger(item_id, ledger)
    store.update_item(item_id, cursor=cursor, synced_at=datetime.now(UTC).isoformat(timespec="seconds"))
    return {**n, "status": pages[-1].get("transactions_update_status", ""), "rows": len(ledger["transactions"]),
            "accounts": len(ledger["accounts"]), "raw": raw}


def sync_item(client, store: Store, item_id: str) -> dict:
    item = store.items()[item_id]
    pages, cursor, raw = _pull(client, store, item_id, item["access_token"], item.get("cursor"))
    return _commit(store, item_id, pages, cursor, raw)


LIABILITY_FIELDS = ("last_statement_balance", "last_statement_issue_date", "last_payment_amount",
                    "last_payment_date", "minimum_payment_amount", "next_payment_due_date")


def liabilities(client, access_token: str) -> tuple[dict[str, dict], list[dict]] | None:
    """Credit liabilities by account id, and the accounts with their balances.

    None when the institution doesn't offer Liabilities (OnePay:
    PRODUCTS_NOT_SUPPORTED, seen 2026-09-26).
    """
    try:
        r = client.post("/liabilities/get", {"access_token": access_token})
    except PlaidError as e:
        if e.code in ("PRODUCTS_NOT_SUPPORTED", "NO_LIABILITY_ACCOUNTS"):
            return None
        raise
    credit = (r.get("liabilities") or {}).get("credit") or []
    return {c["account_id"]: c for c in credit if c.get("account_id")}, r.get("accounts", [])


def snapshot(item_id: str, accounts: list[dict], credit: dict[str, dict], at: str) -> list[dict]:
    """One balances.jsonl line per account; credit accounts carry the statement fields."""
    out = []
    for a in accounts:
        b = a.get("balances") or {}
        line = {"at": at, "item_id": item_id, "account_id": a["account_id"], "name": a.get("name"),
                "mask": a.get("mask"), "type": a.get("type"), "subtype": a.get("subtype"),
                "current": b.get("current"), "available": b.get("available"), "limit": b.get("limit"),
                "currency": b.get("iso_currency_code")}
        if a["account_id"] in credit:
            line.update({k: credit[a["account_id"]].get(k) for k in LIABILITY_FIELDS})
        out.append(line)
    return out


def snapshot_item(client, store: Store, item_id: str) -> int:
    """Append today's balances for one Item, with Liabilities where it has credit cards.

    The balances come from the Liabilities response when there is one, so the
    statement fields and the current balance are read together; otherwise from
    the accounts the last sync stored.
    """
    item = store.items()[item_id]
    accounts = list(store.ledger(item_id)["accounts"].values())
    credit: dict[str, dict] = {}
    if item.get("liabilities") is not False and any(a.get("type") == "credit" for a in accounts):
        got = liabilities(client, item["access_token"])
        if got is None:
            store.update_item(item_id, liabilities=False)
        else:
            credit, accounts = got
    lines = snapshot(item_id, accounts, credit, datetime.now(UTC).isoformat(timespec="seconds"))
    store.append_balances(lines)
    return len(lines)


SYNC_LOCK_WAIT = 300  # seconds a sync waits for another one to finish before giving up


def sync_all(client, store: Store, lock_wait: float | None = SYNC_LOCK_WAIT, sleep=None) -> tuple[list[str], bool]:
    """Sync and snapshot every Item; one failure doesn't stop the rest.

    One sync at a time: a second waits up to ``lock_wait`` seconds for the
    first, then raises SyncBusy without touching anything.

    The outcome goes to last-sync.json, which the board reads, so a failed
    daily run shows up without anyone reading the journal. A balance snapshot
    that fails after the transactions synced is recorded as just that. Its
    ``retry`` says whether any failure could clear by itself (network, Plaid
    or bank outage, rate limit); ``penny plaid sync`` turns that into its exit
    status.
    """
    with store.sync_lock(lock_wait, sleep):
        lines, outcome, ok, retry = [], {}, True, False
        try:
            items = store.items()
        except Exception as e:  # noqa: BLE001 -- a damaged items.json: say so where the board looks
            items, ok = {}, False
            outcome["items.json"] = f"{type(e).__name__}: {e}"
            lines.append(f"items.json: FAILED {e}")
        for item_id, item in items.items():
            name = item.get("institution") or item_id
            try:
                # A first pull stops short at the complete flag; wait_ready polls past it.
                res = (wait_ready if item.get("cursor") is None else sync_item)(client, store, item_id)
            except Exception as e:  # noqa: BLE001 -- one Item's failure is recorded, the rest still sync
                ok, retry = False, retry or retry_worthy(e)
                outcome[name] = failure_text(e, item_id, store.env)
                lines.append(f"{name}: FAILED {outcome[name]}")
                continue
            done = f"{name}: +{res['added']} ~{res['modified']} -{res['removed']}, {res['rows']} rows"
            try:
                n = snapshot_item(client, store, item_id)
            except Exception as e:  # noqa: BLE001 -- recorded; the transactions already synced
                ok, retry = False, retry or retry_worthy(e)
                outcome[name] = f"transactions ok; balance snapshot failed: {failure_text(e, item_id, store.env)}"
                lines.append(f"{done}, status {res['status'] or '?'}; balance snapshot FAILED "
                             f"{failure_text(e, item_id, store.env)}")
                continue
            outcome[name] = "ok"
            lines.append(f"{done}, {n} balances, status {res['status'] or '?'}")
        store.save_last_sync({"at": datetime.now(UTC).isoformat(timespec="seconds"), "ok": ok, "items": outcome,
                              "retry": retry})
    return lines, ok


def add_item(client, store: Store, public_token: str, institution: str = "") -> str:
    r = client.post("/item/public_token/exchange", {"public_token": public_token})
    store.put_item(r["item_id"], {"access_token": r["access_token"], "institution": institution, "cursor": None,
                                  "linked_at": datetime.now(UTC).isoformat(timespec="seconds")})
    return r["item_id"]


def sandbox_item(client, store: Store) -> str:
    """A Sandbox Item with no browser: Plaid's test bank and user."""
    r = client.post("/sandbox/public_token/create",
                    {"institution_id": SANDBOX_INSTITUTION, "initial_products": ["transactions"],
                     "options": {"transactions": {"days_requested": 730}}})
    return add_item(client, store, r["public_token"], "First Platypus Bank (sandbox)")


def wait_ready(client, store: Store, item_id: str, timeout: float = 120, sleep=None) -> dict:
    """Sync until the first pull's full history has landed, or until timeout.

    Seen live in Sandbox (2026-09-26): NOT_READY, then HISTORICAL_UPDATE_COMPLETE
    carrying only the last 30 days (16 rows), then an immediate empty sync, and
    the other 374 rows about 8 s later. The flag says the history exists, not
    that this call returned it, so keep polling after it until a later sync
    adds rows, for up to a minute.

    Each poll's pages go to raw/ as they arrive (the record of what Plaid sent);
    the cursor is carried in memory between polls, and the ledger and
    items.json are written once, at the end, with every poll's pages. A failure
    part-way leaves the old cursor, so the next sync fetches it all again.
    """
    item = store.items()[item_id]
    token, cursor = item["access_token"], item.get("cursor")
    deadline = time.monotonic() + timeout
    complete_at, pages = None, []
    while True:
        got, cursor, raw = _pull(client, store, item_id, token, cursor)
        pages += got
        status = got[-1].get("transactions_update_status", "")
        if status == "HISTORICAL_UPDATE_COMPLETE":
            if complete_at is not None and any(p.get("added") for p in got):
                break
            complete_at = complete_at or time.monotonic()
        if time.monotonic() > deadline or (complete_at and time.monotonic() - complete_at > 60):
            break
        (sleep or time.sleep)(5)
    return _commit(store, item_id, pages, cursor, raw)


USER_ID_FILE = "client-user-id"
STATEMENTS_DAYS = 729  # Plaid refuses a window over two years (INVALID_FIELD, Sandbox 2026-09-26)


def link_token(client, redirect_uri: str | None, update_token: str | None = None, statements: bool = True,
               client_user_id: str | None = None) -> str:
    """A new-Item token, or with ``update_token`` update mode on that Item:
    adding Statements, or with ``statements=False`` a plain re-login that asks
    for no new product (the fix for ITEM_LOGIN_REQUIRED).

    Update mode keeps the Item, its access token and its slot. Sandbox, 2026-09-26:
    /statements/list on an Item linked without Statements is PRODUCT_NOT_ENABLED,
    and an update-mode token asking for it is accepted.

    ``client_user_id`` is the instance's (``Store.client_user_id``); without
    one, a throwaway random id.
    """
    body = {"user": {"client_user_id": client_user_id or secrets.token_hex(16)}, "client_name": "penny", "language": "en", "country_codes": ["US"]}
    if update_token and not statements:
        body["access_token"] = update_token
    elif update_token:
        today = datetime.now(UTC).date()
        body |= {"access_token": update_token, "products": ["statements"],
                 "statements": {"start_date": (today - timedelta(days=STATEMENTS_DAYS)).isoformat(), "end_date": today.isoformat()}}
    else:
        body |= {"products": ["transactions"], "additional_consented_products": ["liabilities"],
                 "transactions": {"days_requested": 730}}
    if redirect_uri:
        body["redirect_uri"] = redirect_uri
    return client.post("/link/token/create", body)["link_token"]


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Link a card</title>
<style>body{font:16px system-ui;max-width:36rem;margin:3rem auto;padding:0 16px}button{font:inherit;padding:.6rem 1.2rem}</style>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script></head><body>
<h1>%(heading)s (%(env)s)</h1><p><button id="go">Open Plaid Link</button></p><p id="out"></p>
<script>
const out = document.getElementById('out');
const UPDATE = %(update)s;
const STATEMENTS = %(statements)s;
const oauth = new URLSearchParams(location.search).has('oauth_state_id');
async function start() {
  let token = oauth ? sessionStorage.getItem('link_token') : null;
  if (!token) {
    const r = await fetch('link-token', {method: 'POST'});
    if (!r.ok) { out.textContent = 'link token failed: ' + await r.text(); return; }
    token = (await r.json()).link_token;
    sessionStorage.setItem('link_token', token);
  }
  const cfg = {token, onSuccess: async (public_token, meta) => {
      if (UPDATE) {  // update mode: same Item, same access token, nothing to exchange
        if (STATEMENTS) {
          out.textContent = 'Linked. Waiting for Plaid to list the statements (up to 90 s)…';
          const r = await fetch('statements', {method: 'POST'});
          out.textContent = 'Update done. ' + await r.text();
        } else {
          out.textContent = 'Signed in again. The next penny plaid sync picks up where it stopped; close this tab.';
        }
        sessionStorage.removeItem('link_token');
        if (oauth) history.replaceState(null, '', location.pathname);
        return;
      }
      const r = await fetch('exchange', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({public_token, institution: meta.institution ? meta.institution.name : ''})});
      out.textContent = r.ok ? 'Linked ' + (meta.institution ? meta.institution.name : '') + ', '
        + meta.accounts.length + ' account(s). Link another, or close this tab.' : 'exchange failed: ' + await r.text();
      sessionStorage.removeItem('link_token');
      if (oauth) history.replaceState(null, '', location.pathname);
    },
    onExit: (err, meta) => { out.textContent = err ? 'Exited: ' + err.error_code + ' ' + err.display_message : 'Closed.'; }};
  if (oauth) cfg.receivedRedirectUri = location.href;
  Plaid.create(cfg).open();
}
document.getElementById('go').onclick = start;
if (oauth) start();
</script></body></html>"""


STATEMENTS_WAIT = 90  # seconds; Sandbox 2026-09-26 answered within 15 s of Link closing


def statements_summary(client, access_token: str, wait: float = STATEMENTS_WAIT) -> str:
    """Counts only: accounts and how many statements each has, never their contents.

    Right after update mode the list isn't there yet. Sandbox, 2026-09-26: first
    INVALID_ACCESS_TOKEN "could not find statements", then PRODUCT_NOT_READY, then
    the list. Both are retried for ``wait`` seconds before being reported.
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            r = client.post("/statements/list", {"access_token": access_token})
            break
        except PlaidError as e:
            if e.code not in ("PRODUCT_NOT_READY", "INVALID_ACCESS_TOKEN") or time.monotonic() > deadline:
                return f"statements: {e}"
            time.sleep(5)
    return "; ".join(f"{a.get('account_name')}: {len(a.get('statements', []))} statements" for a in r.get("accounts", [])) \
        or "statements: no accounts"


def link_page(env: str, heading: str, update: bool, statements: bool) -> str:
    return PAGE % {"env": env, "heading": heading, "update": "true" if update else "false",
                   "statements": "true" if update and statements else "false"}


def serve_link(root: Path, env: str, host: str, port: int, redirect_uri: str | None, update: str | None = None,
               relogin: str | None = None) -> None:
    """The Link page: a new Item; ``update`` adds Statements to an Item; ``relogin``
    signs in to an Item's bank again (ITEM_LOGIN_REQUIRED) and asks for nothing new."""
    if update and relogin:
        raise SystemExit("--update and --relogin are separate modes; pick one")
    client, store = Client.from_root(root, env), Store(root, env)
    target = update or relogin
    items = store.items()
    if target and target not in items:
        raise SystemExit(f"no item {target} in {store.dir}/items.json")
    update_token = items[target]["access_token"] if target else None
    statements = bool(update)
    name = items[target].get("institution") or target if target else ""
    heading = f"Add Statements to {name}" if update else f"Sign in to {name} again" if relogin else "Link a card"

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] == "/":
                page = link_page(env, heading, bool(target), statements)
                self._send(200, page.encode(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            try:
                if self.path == "/link-token":
                    out = {"link_token": link_token(client, redirect_uri, update_token, statements, store.client_user_id())}
                elif self.path == "/statements" and update_token and statements:
                    msg = statements_summary(client, update_token)
                    print(msg, flush=True)
                    return self._send(200, msg.encode(), "text/plain; charset=utf-8")
                elif self.path == "/exchange" and not update_token:
                    n = int(self.headers.get("Content-Length", 0))
                    req = json.loads(self.rfile.read(n))
                    item_id = add_item(client, store, req["public_token"], req.get("institution", ""))
                    print(f"linked {req.get('institution', '')!r} as item {item_id}", flush=True)
                    out = {"ok": True}
                else:
                    return self._send(404, b"not found", "text/plain")
                self._send(200, json.dumps(out).encode(), "application/json")
            except PlaidError as e:
                self._send(502, str(e).encode(), "text/plain")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    what = (f"update mode for {update}, adding Statements" if update
            else f"update mode for {relogin}, re-login only" if relogin else "new Item")
    print(f"link page on http://{host}:{port}/ ({env}, {what}; redirect_uri {redirect_uri or 'none'}). Ctrl-C when done.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def status(root: Path, env: str) -> list[str]:
    """One line per item and account: names, types and counts, never rows."""
    store, lines = Store(root, env), []
    for item_id, item in store.items().items():
        led = store.ledger(item_id)
        tx = list(led["transactions"].values())
        span = f"{min(t['date'] for t in tx)} → {max(t['date'] for t in tx)}" if tx else "no rows"
        lines.append(f"{item.get('institution') or item_id}: {len(tx)} rows, {span}, synced {item.get('synced_at', 'never')}")
        for acct_id, a in led["accounts"].items():
            k = sum(1 for t in tx if t["account_id"] == acct_id)
            lines.append(f"    {a.get('name')} ({a.get('subtype') or a.get('type')}): {k} rows")
    return lines
