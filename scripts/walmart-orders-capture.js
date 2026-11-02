// Capture Walmart order line items from your own logged-in session.
//
// Walmart has no export on a personal account, and its order pages are fed by
// an internal JSON API whose URL changes. Rather than guess the endpoint, this
// listens: it wraps fetch and XMLHttpRequest, keeps every JSON response that
// looks like order data, and writes the lot to a file when you ask.
// __wmReplay() then re-sends the page's own requests -- it follows the order
// list's page cursor back to a date and opens each order it lists -- so a
// year or two need not be scrolled and opened by hand.
//
// USE
//   1. Log in to walmart.com and open Account > Purchase History. Leave the
//      date filter at its default, so the list runs back as far as it can.
//   2. DevTools (F12) > Console. If Chrome asks, type `allow pasting`.
//   3. Paste this whole file, press Enter. It prints "capturing".
//   4. Scroll to the bottom of the order list and load the next page once, then
//      open one order's details. Each captured response prints a line. That
//      teaches the replay both requests' shape. If you have in-store orders
//      too, open one of those as well.
//   5. Run  __wmReplay("2024-11-01")  . It pages the list back to that date,
//      then opens every order listed from then on. One request at a time, so
//      two years takes a few minutes; it prints progress.
//   6. Run  __wmDump()  . A .json file downloads.
//   7. Drop it in this repo's data/ (git-ignored) and say it is there.
//
// Without step 5 it behaves as before: scroll and open orders by hand, then
// dump.
//
// NOTHING LEAVES YOUR BROWSER. The file is written by your own browser to your
// own downloads, and holds response bodies only. Do not paste DevTools' "Copy
// as fetch" output into a chat -- it carries your session cookies. The replay
// re-sends the headers the page itself set on the request it copies, from
// memory; they are never logged or written to the file.
//
// Re-running the paste twice double-wraps; reload the page first.

(() => {
  if (window.__wmCapture) { console.warn("already capturing; reload to reset"); return; }
  const hits = [];
  const requests = []; // {url, method, headers, body} of every orchestra GraphQL call, in memory only
  window.__wmCapture = hits;

  // Order payloads vary by endpoint, so match on shape rather than URL.
  const LOOKS_LIKE_ORDER = /"(orderId|purchaseOrderId|lineItems|productName|itemPrice|orderGroups)"/;

  const keep = (url, text) => {
    if (!text || text.length < 200 || !LOOKS_LIKE_ORDER.test(text)) return null;
    let body;
    try { body = JSON.parse(text); } catch { return null; }
    hits.push({ url: String(url).split("?")[0], at: new Date().toISOString(), body });
    console.log(`captured #${hits.length}  ${(text.length / 1024) | 0} KB  ${String(url).split("?")[0]}`);
    return body;
  };

  const note = (url, method, headers, body) => {
    url = String(url);
    if (!/\/orchestra\/.*graphql\//i.test(url)) return;
    requests.push({ url: new URL(url, location.href).href, method: (method || "GET").toUpperCase(), headers: { ...headers }, body: typeof body === "string" ? body : null });
  };

  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    let asked = "";
    try {
      const [input, init = {}] = args;
      const isReq = typeof input === "object" && input && "url" in input && "headers" in input;
      const url = typeof input === "string" ? input : String(input.url ?? input);
      asked = url;
      const headers = Object.fromEntries(new Headers(init.headers || (isReq ? input.headers : {})));
      note(url, init.method || (isReq ? input.method : "GET"), headers, init.body);
    } catch {}
    const res = await origFetch.apply(this, args);
    res.clone().text().then(t => keep(res.url || asked, t)).catch(() => {});
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

  // A request's GraphQL variables live in the URL (?variables=, a GET) or in
  // a JSON body (a POST). Read and write them wherever they are.
  const varsOf = req => {
    if (req.body) { try { return JSON.parse(req.body).variables || null; } catch { return null; } }
    const v = new URL(req.url).searchParams.get("variables");
    if (!v) return null;
    try { return JSON.parse(v); } catch { return null; }
  };

  const withVars = (req, vars) => {
    if (req.body) {
      const q = JSON.parse(req.body); q.variables = vars;
      return { ...req, body: JSON.stringify(q) };
    }
    const u = new URL(req.url); u.searchParams.set("variables", JSON.stringify(vars));
    return { ...req, url: u.href };
  };

  const send = async req => {
    const init = { method: req.method, headers: req.headers, credentials: "include" };
    if (req.body && req.method !== "GET") init.body = req.body;
    const res = await origFetch(req.url, init);
    const text = await res.text();
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = keep(req.url, text);
    if (!body) throw new Error("response did not look like order data");
    if (body.errors && !body.data) throw new Error(`GraphQL: ${String(body.errors[0]?.message || "error").slice(0, 120)}`);
    return body;
  };

  const pause = ms => new Promise(r => setTimeout(r, ms));
  const clone = o => JSON.parse(JSON.stringify(o));

  // Replace every string equal to `old` anywhere in `o` with `now`.
  const swap = (o, old, now) => {
    if (Array.isArray(o)) return o.map(v => swap(v, old, now));
    if (o && typeof o === "object") return Object.fromEntries(Object.entries(o).map(([k, v]) => [k, swap(v, old, now)]));
    return o === old ? now : o;
  };

  // The order-list page a response carries: its orders and the next cursor.
  const pageOf = body => {
    const ph = body?.data?.purchaseHistory;
    if (!ph) return null;
    return { orders: ph.orders || [], next: ph.pageInfo?.nextPageCursor || null };
  };

  // Every order the page itself listed so far, id -> {date, inStore}.
  const listed = new Map();
  const list = orders => {
    for (const o of orders) if (o?.id) listed.set(String(o.id), { date: String(o.orderDate || "").slice(0, 10), inStore: !!o.isInStore });
  };

  window.__wmReplay = async (from, to) => {
    const start = String(from || ""), end = to ? String(to) : new Date().toISOString().slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(start) || !/^\d{4}-\d{2}-\d{2}$/.test(end) || start > end) {
      console.warn('usage: __wmReplay("2024-11-01"[, "2026-10-31"])'); return;
    }

    // Seed the list from what the page already loaded, and learn the cursor
    // variable: the one whose value is a cursor an earlier page handed out.
    const cursors = new Set();
    for (const h of hits) {
      const p = h.url.includes("/PurchaseHistory") && pageOf(h.body);
      if (p) { list(p.orders); if (p.next) cursors.add(p.next); }
    }
    const history = requests.filter(r => /\/PurchaseHistory/.test(r.url) && varsOf(r));
    let tmpl = null, key = null;
    for (const r of history) {
      const vars = varsOf(r);
      const k = Object.keys(vars).find(k => typeof vars[k] === "string" && (cursors.has(vars[k]) || /cursor/i.test(k) && vars[k]));
      if (k) { tmpl = r; key = k; }
    }
    if (!tmpl) {
      console.warn("no second page of the order list captured yet -- scroll to the bottom and load the next page, then replay again");
      return;
    }

    // Page back from the newest order until the list passes `from`.
    let cursor = null, pages = 0;
    const first = history.find(r => !varsOf(r)[key]);
    const asked = new Set();
    while (pages < 300) {
      const vars = clone(varsOf(tmpl));
      if (cursor) vars[key] = cursor; else if (!first) vars[key] = varsOf(tmpl)[key];
      const req = cursor || !first ? withVars(tmpl, vars) : first;
      let page;
      try { page = pageOf(await send(req)); } catch (e) { console.warn(`page ${pages + 1} failed: ${e.message}`); break; }
      if (!page) { console.warn(`page ${pages + 1}: no order list in the response`); break; }
      pages++;
      list(page.orders);
      const dates = page.orders.map(o => String(o.orderDate || "").slice(0, 10)).filter(Boolean).sort();
      console.log(`page ${pages}: ${page.orders.length} orders, ${dates[0] || "?"} .. ${dates[dates.length - 1] || "?"}`);
      if (!page.next || asked.has(page.next) || (dates[0] && dates[0] < start)) break;
      asked.add(page.next); cursor = page.next;
      await pause(500);
    }

    // Detail requests: a getOrder call, with the id it opened swapped for each
    // listed one. An in-store order may need an in-store template, so keep one
    // of each kind when the page opened both.
    const opened = new Set(hits.filter(h => h.url.includes("/getOrder/")).map(h => String(h.body?.data?.order?.id || "")));
    const tmpls = {};
    for (const r of requests) {
      if (!/\/getOrder\//.test(r.url) || !varsOf(r)) continue;
      const text = JSON.stringify(varsOf(r));
      const id = [...listed.keys(), ...opened].find(id => id && text.includes(`"${id}"`));
      if (id) tmpls[listed.get(id)?.inStore ? "store" : "online"] = { req: r, old: id };
    }
    if (!tmpls.online && !tmpls.store) {
      console.warn(`listed ${listed.size} orders but none was opened by hand, so none can be itemised -- open one order's details and replay again`);
      return;
    }

    const want = [...listed].filter(([id, o]) => o.date >= start && o.date <= end && !opened.has(id));
    let n = 0, failed = 0;
    for (const [id, o] of want) {
      const t = tmpls[o.inStore ? "store" : "online"] || tmpls.online || tmpls.store;
      try { await send(withVars(t.req, swap(varsOf(t.req), t.old, id))); n++; }
      catch (e) { failed++; console.warn(`open ${id} (${o.date}${o.inStore ? ", in store" : ""}) failed: ${e.message}`); }
      if ((n + failed) % 10 === 0) console.log(`tried ${n + failed} of ${want.length}`);
      await pause(500);
    }
    if (failed && want.some(([, o]) => o.inStore) && !tmpls.store) console.warn("some in-store orders failed -- open one in-store order by hand and replay again");
    console.log(`replay done: ${listed.size} listed over ${pages} pages, ${n} opened, ${failed} failed, ${opened.size} already open. run __wmDump()`);
  };

  window.__wmDump = (name) => {
    if (!hits.length) { console.warn("nothing captured yet -- scroll the order list first"); return; }
    const blob = new Blob([JSON.stringify(hits, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name || `walmart-orders-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    console.log(`wrote ${hits.length} responses to ${a.download}`);
  };

  console.log("capturing. load a second page of orders and open one, then __wmReplay(\"2024-11-01\") and/or __wmDump()");
})();
