// POST to the Office server with its CSRF token (#135).
// The token is new on every server start, so a tab opened before a restart holds a stale one.
// On "bad CSRF token" re-read the token from this page's own URL (same origin) and retry once.
(() => {
  let token = document.querySelector('meta[name="orgrove-csrf"]')?.content || "";
  const send = (url, opts) => fetch(url, { ...opts, method: "POST", headers: { ...(opts.headers || {}), "X-ORGrove-CSRF": token } });
  async function refresh() {
    const html = await (await fetch(location.pathname, { cache: "no-store" })).text();
    const m = html.match(/<meta name="orgrove-csrf" content="([^"]+)"/);
    if (m) token = m[1];
    return !!m;
  }
  window.orgrovePost = async (url, opts = {}) => {
    const r = await send(url, opts);
    if (r.status !== 403) return r;
    const out = await r.clone().json().catch(() => ({}));
    if (out.error !== "bad CSRF token" || !(await refresh())) return r;
    return send(url, opts);
  };
})();
