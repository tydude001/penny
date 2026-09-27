// Capture Walmart order line items from your own logged-in session.
//
// Walmart has no export on a personal account, and its order pages are fed by
// an internal JSON API whose URL changes. Rather than guess the endpoint, this
// listens: it wraps fetch and XMLHttpRequest, keeps every JSON response that
// looks like order data, and writes the lot to a file when you ask.
//
// USE
//   1. Log in to walmart.com and open Account > Purchase History.
//   2. DevTools (F12) > Console. If Chrome asks, type `allow pasting`.
//   3. Paste this whole file, press Enter. It prints "capturing".
//   4. Scroll the order list and page through it. Open a few order details.
//      Each captured response prints a line. Aim for one quarter of orders.
//   5. Run  __wmDump()  . A .json file downloads.
//   6. Drop it in this repo's data/ (git-ignored) and say it is there.
//
// NOTHING LEAVES YOUR BROWSER. The file is written by your own browser to your
// own downloads. Do not paste DevTools' "Copy as fetch" output into a chat --
// it carries your session cookies. This script never touches them: it rides
// the page's existing session and only reads response bodies.
//
// Re-running the paste twice double-wraps; reload the page first.

(() => {
  if (window.__wmCapture) { console.warn("already capturing; reload to reset"); return; }
  const hits = [];
  window.__wmCapture = hits;

  // Order payloads vary by endpoint, so match on shape rather than URL.
  const LOOKS_LIKE_ORDER = /"(orderId|purchaseOrderId|lineItems|productName|itemPrice|orderGroups)"/;

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

  window.__wmDump = (name) => {
    if (!hits.length) { console.warn("nothing captured yet -- scroll the order list first"); return; }
    const blob = new Blob([JSON.stringify(hits, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name || `walmart-orders-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    console.log(`wrote ${hits.length} responses to ${a.download}`);
  };

  console.log("capturing. scroll the order list, open some orders, then run __wmDump()");
})();
