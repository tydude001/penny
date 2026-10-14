// Capture Costco warehouse receipts and online orders from your own logged-in
// session. A copy of walmart-orders-capture.js with Costco's shape keys; see
// that file's history for why it listens instead of guessing an endpoint.
//
// Costco has no export either; costco.com's Orders & Purchases page is fed by
// an internal GraphQL API. This wraps fetch and XMLHttpRequest, keeps every
// JSON response that looks like order data, and writes the lot to a file when
// you ask. __coReplay() then re-sends the page's own requests over a wider
// date range, so two years need not be opened one receipt at a time.
//
// USE
//   1. Log in to costco.com and open Account > Orders & Purchases.
//   2. DevTools (F12) > Console. If Chrome asks, type `allow pasting`.
//   3. Paste this whole file, press Enter. It prints "capturing".
//   4. Warehouse tab: pick any date range and click "View Receipt" on one
//      trip. Online tab: pick any date range, and open one order's details.
//      Each captured response prints a line. That teaches the replay each
//      request's shape.
//   5. Run  __coReplay("2024-01-01")  (end defaults to today). It re-sends
//      every dated search in three-month steps, then opens each warehouse
//      receipt and online order the searches list. One request at a time, so
//      two years takes a few minutes; it prints progress.
//   6. Run  __coDump()  . A .json file downloads.
//   7. Drop it in this repo's data/ (git-ignored) and say it is there.
//
// Without step 5 it behaves as before: open receipts by hand, then dump.
//
// NOTHING LEAVES YOUR BROWSER. The file is written by your own browser to your
// own downloads, and holds response bodies only. Do not paste DevTools' "Copy
// as fetch" output into a chat -- it carries your session cookies. The replay
// re-sends the headers the page itself set on the request it copies, from
// memory; they are never logged or written to the file.
//
// Re-running the paste twice double-wraps; reload the page first.

(() => {
  if (window.__coCapture) { console.warn("already capturing; reload to reset"); return; }
  const hits = [];
  const requests = []; // {url, method, headers, body} of every GraphQL POST, in memory only
  window.__coCapture = hits;

  // Order payloads vary by endpoint, so match on shape rather than URL.
  const LOOKS_LIKE_ORDER = /"(receipts|itemArray|itemNumber|itemDescription01|transactionBarcode|warehouseName|orderNumber|lineItems|orderLineItems)"/;

  const keep = (url, text) => {
    if (!text || text.length < 200 || !LOOKS_LIKE_ORDER.test(text)) return;
    let body;
    try { body = JSON.parse(text); } catch { return; }
    hits.push({ url: String(url).split("?")[0], at: new Date().toISOString(), body });
    console.log(`captured #${hits.length}  ${(text.length / 1024) | 0} KB  ${String(url).split("?")[0]}`);
  };

  const note = (url, method, headers, body) => {
    if (typeof body !== "string" || !/graphql/i.test(String(url))) return;
    try { JSON.parse(body); } catch { return; }
    requests.push({ url: String(url), method: method || "POST", headers: { ...headers }, body });
  };

  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    try {
      const [input, init = {}] = args;
      const url = typeof input === "string" ? input : input.url;
      note(url, init.method, Object.fromEntries(new Headers(init.headers || {})), init.body);
    } catch {}
    const res = await origFetch.apply(this, args);
    res.clone().text().then(t => keep(res.url, t)).catch(() => {});
    return res;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  const origSet = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    this.__url = u; this.__method = m; this.__headers = {};
    return origOpen.call(this, m, u, ...rest);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    if (this.__headers) this.__headers[k] = v;
    return origSet.call(this, k, v);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    try { note(this.__url, this.__method, this.__headers, args[0]); } catch {}
    this.addEventListener("load", () => { try { keep(this.__url, this.responseText); } catch {} });
    return origSend.apply(this, args);
  };

  // ---- replay -------------------------------------------------------------

  const DATE_KEY = /^(start|end|from|to)(date)?$|date(from|to)$/i;
  const isStart = k => /^(start|from)|from$/i.test(k);

  // Re-format a Date the way the page wrote the date it replaces:
  // 2024-01-31, 1/31/2024, 9/01/2024 or 01/31/2024 -- each part padded as it was.
  const fmtLike = (sample, d) => {
    const y = d.getFullYear(), m = d.getMonth() + 1, day = d.getDate();
    const p = n => String(n).padStart(2, "0");
    if (/^\d{4}-\d{2}-\d{2}/.test(sample)) return `${y}-${p(m)}-${p(day)}` + sample.slice(10);
    const [sm = "", sd = ""] = sample.split("/");
    return `${sm.length === 2 ? p(m) : m}/${sd.length === 2 ? p(day) : day}/${y}`;
  };

  // Every [object, key] in the variables whose value is a date string.
  const dateSlots = (o, out = []) => {
    if (o && typeof o === "object") for (const [k, v] of Object.entries(o)) {
      if (typeof v === "string" && DATE_KEY.test(k) && /\d/.test(v)) out.push([o, k]);
      else dateSlots(v, out);
    }
    return out;
  };

  const send = async (req, body) => {
    const res = await origFetch(req.url, { method: req.method, headers: req.headers, body, credentials: "include" });
    const text = await res.text();
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    keep(req.url, text);
    return text;
  };

  const pause = ms => new Promise(r => setTimeout(r, ms));

  // Ids a search response lists, for opening each one: warehouse barcodes and
  // online order numbers.
  const idsIn = text => {
    const ids = new Set();
    for (const m of text.matchAll(/"(transactionBarcode|orderNumber)"\s*:\s*"?([\w-]+)"?/g)) ids.add(m[2]);
    return ids;
  };

  window.__coReplay = async (from, to) => {
    const start = new Date(`${from}T00:00:00`), end = to ? new Date(`${to}T00:00:00`) : new Date();
    if (isNaN(start) || isNaN(end) || start > end) { console.warn('usage: __coReplay("2024-01-01"[, "2025-12-31"])'); return; }

    // Search requests: those with dates in their variables, one per query.
    const searches = new Map();
    for (const r of requests) {
      const q = JSON.parse(r.body);
      if (dateSlots(q.variables).length >= 2) searches.set(String(q.query || q.operationName).slice(0, 400), r);
    }
    if (!searches.size) { console.warn("no dated search captured yet -- pick a date range on the Warehouse and Online tabs first"); return; }

    const found = new Set();
    for (const req of searches.values()) {
      for (let a = new Date(start); a <= end;) {
        const b = new Date(a); b.setMonth(b.getMonth() + 3); b.setDate(b.getDate() - 1);
        const stop = b > end ? end : b;
        const q = JSON.parse(req.body);
        for (const [o, k] of dateSlots(q.variables)) o[k] = fmtLike(o[k], isStart(k) ? a : stop);
        try {
          const text = await send(req, JSON.stringify(q));
          const ids = idsIn(text);
          ids.forEach(id => found.add(id));
          console.log(`searched ${a.toISOString().slice(0, 10)} .. ${stop.toISOString().slice(0, 10)}: ${ids.size} listed`);
        } catch (e) { console.warn(`search ${a.toISOString().slice(0, 10)} failed: ${e.message}`); }
        a = new Date(stop); a.setDate(a.getDate() + 1);
        await pause(400);
      }
    }

    // Detail requests: those carrying one id in their body. Swap in each
    // listed id of the same kind (a barcode is 20+ digits, an order number is not).
    // Opening one twice is harmless: penny dedupes by id.
    const tmpl = { long: null, short: null };
    for (const r of requests) {
      const m = r.body.match(/"(barcode|transactionBarcode|orderNumbers?)"\s*:\s*\[?\s*"?([\w-]+)"?/i);
      if (m) tmpl[m[2].length >= 20 ? "long" : "short"] = { req: r, old: m[2] };
    }
    let n = 0;
    for (const id of found) {
      const t = tmpl[id.length >= 20 ? "long" : "short"];
      if (!t) continue;
      try { await send(t.req, t.req.body.split(t.old).join(id)); n++; } catch (e) { console.warn(`open ${id} failed: ${e.message}`); }
      if (n % 10 === 0) console.log(`opened ${n} of ${found.size}`);
      await pause(400);
    }
    if (!tmpl.long) console.warn("no warehouse receipt was opened by hand, so receipts were listed but not itemised -- open one and replay again");
    if (!tmpl.short) console.warn("no online order was opened by hand -- if the search did not carry items, open one and replay again");
    console.log(`replay done: ${found.size} listed, ${n} opened. run __coDump()`);
  };

  window.__coDump = (name) => {
    if (!hits.length) { console.warn("nothing captured yet -- open a receipt first"); return; }
    const blob = new Blob([JSON.stringify(hits, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name || `costco-receipts-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    console.log(`wrote ${hits.length} responses to ${a.download}`);
  };

  console.log("capturing. open some receipts, then __coReplay(\"2024-01-01\") and/or __coDump()");
})();
