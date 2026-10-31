"""Client branding for the Office (#159): name, logos and accent colour from config/org.toml [brand].

ORGrove's own marks ship with the product (office_static/brand/). The client's are config: a customer
sets them in their org.toml and never edits a page. With no [brand], the Office shows ORGrove only.
"""
import html
import re
import tomllib

from orgrove import manifest

ORG_FILE = manifest.ROOT / "config" / "org.toml"
LOGO_DIR = manifest.ROOT / "config" / "brand"
COLOUR = re.compile(r"#[0-9a-fA-F]{6}")
KEYS = {"client_name", "logo_light", "logo_dark", "accent", "accent_dark"}


def load(path=ORG_FILE):
    """The [brand] table, validated. Missing file or table means no client branding."""
    try:
        with open(path, "rb") as f:
            b = tomllib.load(f).get("brand", {})
    except FileNotFoundError:
        return {}
    if unknown := set(b) - KEYS:
        raise ValueError(f"[brand]: unknown keys {sorted(unknown)}")
    for k in ("accent", "accent_dark"):
        if k in b and not COLOUR.fullmatch(b[k]):
            raise ValueError(f"[brand] {k} must be a #rrggbb colour, got {b[k]!r}")
    for k in ("logo_light", "logo_dark"):
        if k in b and logo(b, k) is None:
            raise ValueError(f"[brand] {k} must be a .png inside config/brand/, got {b[k]!r}")
    if "logo_dark" in b and "logo_light" not in b:
        raise ValueError("[brand] logo_dark needs logo_light")
    return b


def logo(b, key):
    """The client logo file for `key`, only if it is a PNG inside config/brand/."""
    if key not in b:
        return None
    path = (manifest.ROOT / b[key]).resolve()
    if path.suffix != ".png" or LOGO_DIR.resolve() not in path.parents or not path.is_file():
        return None
    return path


def client_html(b, dark=True):
    """The client half of the page header, or nothing. `dark=False` for pages with a fixed light header."""
    name = html.escape(b.get("client_name", ""))
    if "logo_light" in b:
        img = f'<img class="client" src="/brand/client-light.png" alt="{name or "client"}">'
        if dark and "logo_dark" in b:
            img = f'<picture><source media="(prefers-color-scheme: dark)" srcset="/brand/client-dark.png">{img}</picture>'
    elif name:
        img = f'<b class="client-name">{name}</b>'
    else:
        return ""
    return f'<span class="for">client</span>\n    {img}'


def style(b):
    """CSS overriding the pages' accent tokens (--accent; --blue on the 3D page). Empty without an accent."""
    if "accent" not in b:
        return ""
    light, dark = b["accent"], b.get("accent_dark", b["accent"])
    return (f"<style id=\"brand\">:root {{ --accent: {light}; --blue: {light}; }}\n"
            f"@media (prefers-color-scheme: dark) {{ :root {{ --accent: {dark}; }} }}</style>\n")


def render(page, b, token, dark=True):
    """A page's HTML with the CSRF token, the client header and the accent filled in."""
    return (page.replace("__ORGROVE_CSRF__", token)
                .replace("<!--ORGROVE_CLIENT-->", client_html(b, dark))
                .replace("</head>", style(b) + "</head>", 1))
