// Capture Costco warehouse receipt line items from your own logged-in session.
// A copy of walmart-orders-capture.js with Costco's shape keys; see that file's
// history for why it listens instead of calling an endpoint.
//
// Costco has no export either; costco.com's Orders & Purchases page is fed by
// an internal JSON API. Rather than guess the endpoint, this
// listens: it wraps fetch and XMLHttpRequest, keeps every JSON response that
// looks like order data, and writes the lot to a file when you ask.
//
// USE
//   1. Log in to costco.com and open Account > Orders & Purchases >
//      Warehouse (in-store receipts, about two years back).
//   2. DevTools (F12) > Console. If Chrome asks, type `allow pasting`.
//   3. Paste this whole file, press Enter. It prints "capturing".
//   4. Pick a date range, then click "View Receipt" on each trip you want.
//      Each captured response prints a line. The last six months is plenty.
//   5. Run  __coDump()  . A .json file downloads.
//   6. Drop it in this repo's data/ (git-ignored) and say it is there.
//
// NOTHING LEAVES YOUR BROWSER. The file is written by your own browser to your
// own downloads. Do not paste DevTools' "Copy as fetch" output into a chat --
// it carries your session cookies. This script never touches them: it rides
// the page's existing session and only reads response bodies.
//
// Re-running the paste twice double-wraps; reload the page first.

(() => {
  if (window.__coCapture) { console.warn("already capturing; reload to reset"); return; }
  const hits = [];
  window.__coCapture = hits;

  // Order payloads vary by endpoint, so match on shape rather than URL.
  const LOOKS_LIKE_ORDER = /"(receipts|itemArray|itemNumber|itemDescription01|transactionBarcode|warehouseName|orderNumber|lineItems)"/;

  const keep = (url, text) => {
    if (!text || text.length < 200 || !LOOKS_LIKE_ORDER.test(text)) return;
    let body;
    try { body = JSON.parse(text); } catch { return; }
    hits.push({ url: String(url).split("?")[0], at: new Date().toISOString(), body });
    console.log(`captured #${hits.length}  ${(text.length / 1024) | 0} KB  ${String(url).split("?")[0]}`);
  };

  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const res = await origFetch.apply(this, args);
    res.clone().text().then(t => keep(res.url, t)).catch(() => {});
    return res;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) { this.__url = u; return origOpen.call(this, m, u, ...rest); };
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => { try { keep(this.__url, this.responseText); } catch {} });
    return origSend.apply(this, args);
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

  console.log("capturing. open some receipts, then run __coDump()");
})();
