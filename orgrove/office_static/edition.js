// Which optional parts this copy has (orgrove/editions.py): ORGrove Community has no meetings, finance or new look.
// Anything marked data-pro="<part>" is hidden when the part is absent; its address shows the Pro page (office.py).
export const EDITION = await fetch("/api/edition", { cache: "no-store" }).then((r) => r.json()).catch(() => ({}));
for (const el of document.querySelectorAll("[data-pro]")) {
  if (!EDITION[el.dataset.pro]) el.style.setProperty("display", "none", "important");
}
// The licence (orgrove/licence.py, #426): in its grace days or once it has ended, one line at the top says so and
// who to write to. Silent in Community and in a licensed copy.
fetch("/api/licence", { cache: "no-store" }).then((r) => r.json()).then((lic) => {
  if (!lic.show) return;
  const bar = document.createElement("div");
  bar.setAttribute("role", "status");
  bar.textContent = "ORGrove Pro: " + lic.line + ".";
  bar.style.cssText = "position:sticky;top:0;z-index:9999;padding:8px 16px;font:13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;" +
    "background:" + (lic.state === "grace" ? "#fff4d6;color:#5c4400" : "#fde2e1;color:#6b1512") + ";border-bottom:1px solid rgba(0,0,0,.12)";
  document.body.prepend(bar);
}).catch(() => {});
