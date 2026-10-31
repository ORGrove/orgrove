// Which optional parts this copy has (orgrove/editions.py): ORGrove Community has no meetings, finance or new look.
// Anything marked data-pro="<part>" is hidden when the part is absent; its address shows the Pro page (office.py).
export const EDITION = await fetch("/api/edition", { cache: "no-store" }).then((r) => r.json()).catch(() => ({}));
for (const el of document.querySelectorAll("[data-pro]")) {
  if (!EDITION[el.dataset.pro]) el.style.setProperty("display", "none", "important");
}
