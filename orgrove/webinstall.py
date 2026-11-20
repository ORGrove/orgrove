"""The web installer: the same install, driven from a page in your browser (plan phase B, 2026-11-10).

    ./install               opens the page          ./install --cli    the questions in the terminal, as before

One engine, two faces. `orgrove.install.Installer` does every step; this module only gives it a different way to ask
a question and to show what it did. The page is served from this computer only (127.0.0.1), by the standard library:

* the address carries a one-time secret that becomes a cookie; nothing else is served without it;
* the Host header must be this server's own (a web page on another site cannot reach it through DNS tricks), and a
  change (POST) must carry a custom header a foreign page cannot send;
* the Anthropic key is typed into a hidden field, handed to the engine in memory, and is never put in the progress
  list, the state the page reads, a log, a file or a command. The engine stores it in a GitHub secret at once.

The three GitHub App steps run on this same server, so GitHub sends you back into the page instead of to a terminal.
"""
import html
import http.cookies
import json
import re
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orgrove import doctor, install

# The steps the page shows, in order, matched by the start of the engine's own headings.
RAIL = (("Checking this computer", "This computer"), ("Five questions", "Your answers"), ("Repos", "Repos"),
        ("GitHub Apps", "GitHub Apps"), ("Secrets", "Secrets"), ("The project repo", "Your project"),
        ("Configuration", "Settings"), ("Wikis", "Wikis"), ("Checking everything", "Final checks"))
YES_NO = re.compile(r"\((?:Y/n|y/N)\)\s*:?\s*$")
DEFAULT = re.compile(r"\s\[([^\]]*)\]:?\s*$")
# Links the page may make from the engine's text: GitHub and the two places a person gets a key or the CLI.
LINKABLE = re.compile(r"https://(?:github\.com|cli\.github\.com|platform\.claude\.com)/[^\s)>,]*")
THEME_KEY = "orgrove-theme"   # light or dark, kept in this browser only
THEME_BOOT = ("<script>try{var t=localStorage.getItem('" + "orgrove-theme" + "');"
              "if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}</script>")
COOKIE = "orgrove_install"
BRAND = Path(__file__).resolve().parent / "office_static" / "brand"
BRAND_FILES = {"orgrove-dark.png", "orgrove-light.png", "orgrove-symbol.png"}   # the only files it serves


def split_prompt(prompt):
    """(label, default, kind) from the engine's terminal prompt: kind is yes_no, continue (a bare Enter) or text."""
    text = prompt.strip()
    if not text:
        return "", "", "continue"
    if YES_NO.search(text):
        return YES_NO.sub("", text).strip(), "", "yes_no"
    default = ""
    found = DEFAULT.search(text)
    if found:
        default, text = found.group(1), text[:found.start()]
    text = text.rstrip(": ").strip()
    return re.sub(r"^\d+\.\s*", "", text), default, "text"


def classify(line):
    s = line.strip()
    if s.startswith("== "):
        return "step"
    if "✓" in s:
        return "ok"
    if s.startswith("✗") or " ✗ " in s:
        return "fail"
    if s.startswith("!") or s.startswith("?"):
        return "warn"
    return "info"


class Session:
    """What the page shows, and the one place the engine thread and the web threads meet. Everything is under a lock."""

    def __init__(self, with_wiki=True):
        self.lock = threading.Condition()
        self.log, self.context = [], []     # what the engine said; what it said since the last question
        self.status = "starting"            # starting running waiting done stopped error
        self.prompt = None                  # the question the engine is waiting on
        self.answer = None
        self.seq = 0
        self.app = None                     # the GitHub App step in progress
        self.error = ""
        self.links = []                     # where to go when it is done: [(text, url)]
        self.rail = {label: "todo" for _, label in RAIL if with_wiki or label != "Wikis"}
        self.quit = threading.Event()

    # --- the engine's side (a worker thread) ------------------------------------------------------------------
    def out(self, text=""):
        with self.lock:
            for raw in str(text).split("\n"):
                line = raw.rstrip()
                if not line.strip():
                    continue
                kind = classify(line)
                self.log.append({"n": len(self.log), "kind": kind, "text": line.strip()[3:] if kind == "step" else line.strip()})
                if kind == "step":
                    self.mark(line.strip()[3:])
                    self.context = []
                else:
                    self.context.append(line.strip())
            self.status = "running" if self.status in ("starting", "waiting") and not self.prompt else self.status
            self.lock.notify_all()

    def mark(self, heading):
        current = None
        for prefix, label in RAIL:
            if heading.startswith(prefix):
                current = label
        if current is None or current not in self.rail:     # a step this edition does not have (Community: no Wikis)
            return
        seen = False
        for _, label in RAIL:
            if label not in self.rail:
                continue
            if label == current:
                self.rail[label], seen = "now", True
            elif not seen and self.rail[label] != "skipped":
                self.rail[label] = "done"

    def _ask(self, prompt, secret):
        label, default, kind = split_prompt(prompt)
        with self.lock:
            self.seq += 1
            self.prompt = {"id": self.seq, "label": label, "default": default, "kind": kind, "secret": secret,
                           "context": list(self.context)}
            self.answer, self.status = None, "waiting"
            self.lock.notify_all()
            while self.answer is None and not self.quit.is_set():
                self.lock.wait(0.5)
            if self.quit.is_set():
                raise KeyboardInterrupt
            value, self.prompt, self.status, self.context = self.answer, None, "running", []
            return value

    def ask(self, prompt=""):
        return self._ask(prompt, False)

    def ask_secret(self, prompt=""):
        return self._ask(prompt, True)

    def interactive(self, cmd):
        """A command that must be run by a person in a terminal (gh's own device login): say so, wait for them."""
        self.out(f"  Run this in a terminal, then come back: {' '.join(cmd)}")
        self._ask("", False)

    # --- the web side ------------------------------------------------------------------------------------------
    def reply(self, prompt_id, value):
        with self.lock:
            if not self.prompt or self.prompt["id"] != prompt_id:
                return False
            self.answer = value if isinstance(value, str) else ""
            self.lock.notify_all()
            return True

    def view(self, since=0):
        """What the page reads. The prompt is described, never its answer: no secret is ever in here."""
        with self.lock:
            return {"status": self.status, "log": self.log[since:], "next": len(self.log), "prompt": self.prompt,
                    "app": self.app, "error": self.error, "links": self.links, "rail": self.rail}


class WebAppFlow(install.AppFlow):
    """GitHub's App Manifest flow on the wizard's own server: GitHub's pre-filled Create page, its install page, then
    back into the wizard. Same handler logic as the terminal flow; only the server and the pages around it differ."""

    def __init__(self, web, owner, app_manifest, repos_hint, on_created, exchange=None, port=0, lookup=None, poll=3.0, step=None, **_):
        self.web = web
        self.step = step or {}
        self.lookup, self.poll = lookup or self.installed_on_github, poll
        self.owner, self.repos_hint, self.on_created = owner, repos_hint, on_created
        self.exchange = exchange or (lambda code: install.github("POST", f"/app-manifests/{code}/conversions"))
        self.state = secrets.token_urlsafe(16)
        self.result = {}
        self.done = threading.Event()
        # GitHub appends its own query to setup_url, so the guard (the flow's unguessable state) rides in the path
        self.manifest = {**app_manifest, "redirect_url": f"{web.base}/created", "setup_url": f"{web.base}/installed/{self.state}"}

    BACK = "go back to the installer"

    def installed_on_github(self):
        """This App's installation id on the owner, asked of GitHub itself (None until there is one). The installer
        must not depend on the browser being sent back to it: a closed tab or a failed redirect would leave it
        waiting for an App that is already installed (2026-11-10)."""
        slug = self.result.get("slug")
        if not slug:
            return None
        ok, data = install.gh("api", f"orgs/{self.owner}/installations")
        if not ok or not isinstance(data, dict):
            return None
        for found in doctor.real_installations(data):
            if found.get("app_slug") == slug:
                return str(found["id"])
        return None

    def page(self, req, body, status=200):
        # GitHub's steps open in their own tab: the installer's tab keeps polling and carries on by itself, so these
        # pages never send the person "back" (a second wizard in this tab would only confuse). They say what is next.
        done = "✓ Installed" in body
        tail = ("<button type=button onclick=\"window.close()\">Close this tab</button>" if done else
                "<p class=mute>Keep this tab for GitHub's next step. The installer, in your other tab, carries on by itself.</p>")
        web_page(req, f"<div class=card>{body}{tail}</div>", status)

    def install_url(self):
        return f"https://github.com/apps/{self.result['slug']}/installations/new"

    def handle(self, req, path, q):
        if path == "/" and self.result.get("slug"):
            # The App exists: GitHub refuses a second Create (the name is taken) and the person is left clicking a button
            # that does nothing (2026-11-11). From here the only useful step is Install.
            return self.page(req, f"<p>✓ <b>{html.escape(self.result['slug'])}</b> is already created.</p>"
                                  f"<p>Next, install it: <a href=\"{html.escape(self.install_url())}\">Install it on GitHub</a></p>")
        super().handle(req, path, q)
        if path == "/created" and self.result.get("slug") and self.web.session.app:
            self.web.session.app["install"] = self.install_url()     # the installer's own card now offers Install, not Create
        if path.startswith("/installed/") and "installation" in self.result or self.result.get("error"):
            self.done.set()

    def run(self, open_url=None, out=print):
        name = self.manifest["name"]
        self.web.session.app = {"name": name, "start": "/app/start", "repos": list(self.repos_hint), **self.step}
        self.web.flow = self
        out(f"  GitHub App {name}: click Continue to GitHub in the page, then Install.")
        while not self.done.wait(self.poll):        # the redirect from GitHub, or GitHub's own answer, whichever comes first
            try:
                found = self.lookup() if self.result.get("slug") else None
            except Exception:  # noqa: BLE001 (a failed check is "not yet": the next one asks again)
                found = None
            if found:
                self.result.setdefault("installation", found)
                self.done.set()
        self.web.flow, self.web.session.app = None, None
        if self.result.get("error"):
            raise SystemExit(f"GitHub didn't finish creating the App: {self.result['error']}. Try again.")
        return self.result


def web_page(req, body, status=200):
    req.send_response(status)
    req.send_header("Content-Type", "text/html; charset=utf-8")
    req.send_header("Cache-Control", "no-store")
    req.end_headers()
    req.wfile.write(f"<!doctype html><meta charset=utf-8><title>ORGrove installer</title>{THEME_BOOT}"
                    f"<meta name=viewport content='width=device-width,initial-scale=1'><style>{CSS}</style>"
                    f"<body><main class=narrow>{body}</main>".encode())


class WebInstaller:
    """The server, the session and the engine thread. `make_installer(session)` returns the Installer to run."""

    def __init__(self, make_installer, port=0, open_url=webbrowser.open, out=print):
        self.make_installer, self.open_url, self.out = make_installer, open_url, out
        self.session = Session(with_wiki=install.has("wiki"))   # Community has no wiki: no step that never finishes
        self.token = secrets.token_urlsafe(24)
        self.flow = None
        self.installer, self.last_check = None, False
        web = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                web.dispatch(self, "GET")

            def do_POST(self):
                web.dispatch(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.engine = None

    # --- the address and the checks ---------------------------------------------------------------------------
    @property
    def start_url(self):
        return f"{self.base}/?t={self.token}"

    def host_ok(self, req):
        host = (req.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def authed(self, req):
        jar = http.cookies.SimpleCookie(req.headers.get("Cookie") or "")
        got = jar[COOKIE].value if COOKIE in jar else ""
        return secrets.compare_digest(got.encode(), self.token.encode())

    @staticmethod
    def send(req, status, body, ctype="application/json", extra=()):
        data = body if isinstance(body, bytes) else body.encode()
        req.send_response(status)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(len(data)))
        req.send_header("Cache-Control", "no-store")
        req.send_header("X-Content-Type-Options", "nosniff")
        req.send_header("Referrer-Policy", "no-referrer")
        req.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                        "form-action 'self' https://github.com; frame-ancestors 'none'")
        for k, v in extra:
            req.send_header(k, v)
        req.end_headers()
        req.wfile.write(data)

    def json(self, req, status, obj):
        self.send(req, status, json.dumps(obj))

    def dispatch(self, req, method):
        u = urllib.parse.urlparse(req.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if not self.host_ok(req):
            return self.send(req, 403, "wrong host", "text/plain")
        path = u.path
        # GitHub's own redirects back to us: guarded by the flow's unguessable state, not by the cookie
        if method == "GET" and self.flow and (path in ("/app/start", "/created") or path.startswith("/installed/")):
            return self.flow.handle(req, "/" if path == "/app/start" else path, q)
        if method == "GET" and path == "/" and q.get("t"):
            if not secrets.compare_digest(q["t"].encode(), self.token.encode()):
                return self.send(req, 403, "this link isn't from this installer", "text/plain")
            return self.send(req, 302, "", "text/plain", [
                ("Location", "/"), ("Set-Cookie", f"{COOKIE}={self.token}; Path=/; HttpOnly; SameSite=Lax")])
        if not self.authed(req):
            return self.send(req, 403, "open the address the installer printed in the terminal", "text/plain")
        if method == "GET" and path == "/":
            return self.send(req, 200, PAGE, "text/html; charset=utf-8")
        if method == "GET" and path.startswith("/brand/"):
            name = path[len("/brand/"):]
            if name in BRAND_FILES and (BRAND / name).is_file():     # a fixed list: no path from the request is ever opened
                return self.send(req, 200, (BRAND / name).read_bytes(), "image/png")
            return self.send(req, 404, "not found", "text/plain")
        if method == "GET" and path == "/api/state":
            return self.json(req, 200, self.session.view(int(q.get("since", "0") or 0)))
        if method == "POST":
            if req.headers.get("X-ORGrove") != "1":     # a foreign page cannot send a custom header without a preflight
                return self.send(req, 403, "missing header", "text/plain")
            try:
                length = int(req.headers.get("Content-Length") or 0)
                body = json.loads(req.rfile.read(min(length, 20000)) or b"{}")
            except (ValueError, OSError):
                return self.json(req, 400, {"error": "bad request"})
            if path == "/api/answer":
                ok = self.session.reply(body.get("id"), body.get("value"))
                return self.json(req, 200 if ok else 409, {"ok": ok})
            if path == "/api/retry":      # again, the same thing that stopped: the install, or the check
                return self.json(req, 200, {"ok": self.start_engine(check=self.last_check)})
            if path == "/api/check":
                return self.json(req, 200, {"ok": self.start_engine(check=True)})
            if path == "/api/quit":
                self.session.quit.set()
                self.json(req, 200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return None
        return self.send(req, 404, "not found", "text/plain")

    # --- the engine ---------------------------------------------------------------------------------------------
    def start_engine(self, check=False):
        s = self.session
        with s.lock:
            if self.engine and self.engine.is_alive():
                return False
            s.status, s.error, self.last_check = "starting", "", check
            if not check:
                s.links = []
        self.engine = threading.Thread(target=self._run_engine, args=(check,), daemon=True)
        self.engine.start()
        return True

    def _run_engine(self, check):
        s = self.session
        try:
            inst = self.make_installer(s)
            if check and self.installer:
                inst.s = dict(self.installer.s)
                inst.check()
            else:
                self.installer = inst
                inst.run()
            with s.lock:
                state = (self.installer or inst).s
                s.status = "done"
                if state.get("project") and state.get("owner"):
                    repo = f"{state['owner']}/{state['project']}"
                    s.links = [["Give the agents their first ticket", f"https://github.com/{repo}/issues/new"],
                               ["Your project's Actions", f"https://github.com/{repo}/actions"]]
                for label in s.rail:
                    if s.rail[label] in ("now", "todo"):
                        s.rail[label] = "done"
        except SystemExit as e:        # the engine's own plain-words stop
            self._stopped(str(e.code if e.code not in (None, 0) else "stopped"))
        except KeyboardInterrupt:
            self._stopped("stopped")
        except Exception as e:  # noqa: BLE001 (shown to the person; never a key: nothing here holds one)
            self._stopped(f"something unexpected went wrong: {type(e).__name__}: {str(e)[:200]}")

    def _stopped(self, message):
        s = self.session
        with s.lock:
            s.error, s.status, s.prompt = message, "error", None
            s.log.append({"n": len(s.log), "kind": "fail", "text": message})

    # --- running it ---------------------------------------------------------------------------------------------
    def serve(self):
        self.out(f"ORGrove installer: open {self.start_url}")
        self.out("(it opens by itself; keep this window open, Ctrl+C stops it. Everything happens in the page.)")
        self.open_url(self.start_url)
        self.start_engine()
        try:
            self.server.serve_forever()
        finally:
            self.session.quit.set()
            self.server.server_close()


def run(port=0, open_url=webbrowser.open, out=print, **installer_kwargs):
    """`./install`: the web installer around the real Installer."""
    holder = {}

    def make(session):
        web = holder["web"]
        return install.Installer(ask=session.ask, ask_secret=session.ask_secret, out=session.out,
                                 open_url=open_url_noop, interactive=session.interactive,
                                 app_flow=lambda *a, **k: WebAppFlow(web, *a, **k), **installer_kwargs)

    def open_url_noop(url):   # the page shows every link itself; a second browser tab would only confuse
        return True

    holder["web"] = WebInstaller(make, port=port, open_url=open_url, out=out)
    holder["web"].serve()


CSS = """
/* The ORGrove palette, taken from the website's own tokens (site/app/globals.css): purple for actions and progress,
   amber where a person decides (a question, a GitHub step), green only for a finished check in the log. */
:root{--bg:#0a0b10;--card:#12141c;--ink:#eceef6;--mute:#a1a8bd;--line:#242838;--brand:#9a8dff;--brand-ink:#0a0b10;--human:#f0a020;--ok:#4fd1a1;--warn:#f0a020;--bad:#ff7a70;--glow-a:rgba(128,112,255,.18);--glow-b:rgba(240,160,32,.08);color-scheme:dark}
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]){--bg:#f7f7fb;--card:#fff;--ink:#12141c;--mute:#434a5c;--line:#dde0ea;--brand:#5040c0;--brand-ink:#fff;--human:#c47f00;--ok:#0b7350;--warn:#8a5200;--bad:#b3261e;--glow-a:rgba(80,64,192,.1);--glow-b:rgba(240,160,32,.08);color-scheme:light}}
:root[data-theme="light"]{--bg:#f7f7fb;--card:#fff;--ink:#12141c;--mute:#434a5c;--line:#dde0ea;--brand:#5040c0;--brand-ink:#fff;--human:#c47f00;--ok:#0b7350;--warn:#8a5200;--bad:#b3261e;--glow-a:rgba(80,64,192,.1);--glow-b:rgba(240,160,32,.08);color-scheme:light}
*{box-sizing:border-box}body{margin:0;min-height:100vh;color:var(--ink);font:16px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif;
background:radial-gradient(60% 50% at 70% 0,var(--glow-a),transparent 70%),radial-gradient(40% 40% at 5% 90%,var(--glow-b),transparent 70%),var(--bg)}
main{max-width:60rem;margin:0 auto;padding:1.5rem 1rem 4rem}main.narrow{max-width:34rem;padding-top:3rem}
h1{font-size:1.35rem;margin:0}.brand{display:flex;align-items:center;gap:.7rem;margin-bottom:1.5rem}.brand small{color:var(--mute)}.brand img{display:block}.brand .theme{margin:0 0 0 auto;padding:.35rem .7rem;font-size:1rem;line-height:1}
.layout{display:grid;grid-template-columns:13rem 1fr;gap:1.25rem}@media(max-width:720px){.layout{grid-template-columns:1fr}}
.rail{list-style:none;margin:0;padding:0}.rail li{display:flex;gap:.6rem;align-items:center;padding:.35rem 0;color:var(--mute)}
.rail .dot{width:.9rem;height:.9rem;border-radius:50%;border:2px solid var(--line);flex:none}.rail .now{color:var(--ink);font-weight:600}.rail .now .dot{border-color:var(--human);background:var(--human)}
.rail .done .dot{border-color:var(--brand);background:var(--brand)}.rail .done{color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:1.25rem 1.4rem;margin-bottom:1rem}
.card.you{border-left:4px solid var(--human)}.card h2{margin:.1rem 0 .4rem;font-size:1.15rem}.mute{color:var(--mute)}label{display:block;font-weight:600;margin:.2rem 0 .4rem}
input[type=text],input[type=password]{width:100%;padding:.65rem .8rem;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit}
input:focus{outline:2px solid var(--brand);outline-offset:1px}
button,.btn{display:inline-block;background:var(--brand);color:var(--brand-ink);border:0;border-radius:10px;padding:.65rem 1.1rem;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;margin:.6rem .5rem 0 0}
button.ghost,.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
.bar{height:.4rem;background:var(--line);border-radius:99px;overflow:hidden;margin:.2rem 0 1rem}.bar i{display:block;height:100%;background:var(--brand);width:0;transition:width .4s}
.log{font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;max-height:22rem;overflow:auto}
.log .ok{color:var(--ok)}.log .warn{color:var(--warn)}.log .fail{color:var(--bad)}.log .step{font-weight:700;margin-top:.5rem}
details summary{cursor:pointer;color:var(--mute)}.err{color:var(--bad);font-weight:600}.hint{margin:.2rem 0 .8rem}.hint div{margin:.15rem 0}
a{color:var(--brand)}code{font:13px ui-monospace,Menlo,monospace;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:.05rem .35rem}
"""

PAGE = """<!doctype html><meta charset=utf-8><title>ORGrove installer</title>
<meta name=viewport content='width=device-width,initial-scale=1'><link rel=icon href=/brand/orgrove-symbol.png>""" + THEME_BOOT + """<style>""" + CSS + """</style>
<body><main>
<div class=brand><img id=logo src="/brand/orgrove-dark.png" alt="ORGrove" height=30><small>installer</small>
 <button id=theme class="ghost theme" type=button aria-label="Switch between light and dark"></button></div>
<div class=layout>
 <nav><ul class=rail id=rail></ul></nav>
 <section>
  <div class=bar><i id=bar></i></div>
  <div id=main></div>
  <details id=details class=card><summary>Details: what it did</summary><div class=log id=log></div></details>
 </section>
</div></main>
<script>
"use strict";
let since = 0, shownPrompt = 0, busy = false;
const $ = id => document.getElementById(id);
const TOKEN = /`[^`]+`|https:\\/\\/(?:github\\.com|cli\\.github\\.com|platform\\.claude\\.com)\\/[^\\s)>,`]*/g;
function linked(parent, text) {            // text only, plus links to a short list of sites and `code`: never parsed as HTML
  let last = 0; text.replace(TOKEN, (m, i) => {
    parent.append(document.createTextNode(text.slice(last, i)));
    if (m[0] === '`') { parent.append(el('code', '', m.slice(1, -1))); }
    else { const a = document.createElement('a'); a.href = m; a.target = '_blank'; a.rel = 'noopener'; a.textContent = m; parent.append(a); }
    last = i + m.length; return m;
  });
  parent.append(document.createTextNode(text.slice(last)));
}
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }
async function post(path, body) {
  const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-ORGrove': '1'}, body: JSON.stringify(body || {})});
  return r.ok;
}
function renderRail(rail) {
  const ul = $('rail'); ul.replaceChildren(); let done = 0, total = 0;
  for (const [name, st] of Object.entries(rail)) {
    total++; if (st === 'done') done++;
    const li = el('li', st); li.append(el('span', 'dot'), el('span', '', name)); ul.append(li);
  }
  $('bar').style.width = Math.round(100 * done / total) + '%';
}
function context(card, lines) {
  if (!lines || !lines.length) return;
  const d = el('div', 'hint mute'); for (const l of lines) { const p = el('div'); linked(p, l); d.append(p); } card.append(d);
}
function renderPrompt(p) {
  if (shownPrompt === p.id && $('main').firstChild) return;
  shownPrompt = p.id;
  const card = el('div', 'card you'); card.append(el('h2', '', p.label || 'One moment'));
  context(card, p.context);
  const send = value => { if (busy) return; busy = true; post('/api/answer', {id: p.id, value}).then(() => busy = false); card.replaceChildren(el('p', 'mute', 'Working…')); };
  if (p.kind === 'yes_no') {
    const y = el('button', '', 'Yes'), n = el('button', 'ghost', 'No'); y.onclick = () => send('y'); n.onclick = () => send('n'); card.append(y, n);
  } else if (p.kind === 'continue') {
    const b = el('button', '', 'Continue'); b.onclick = () => send('x'); card.append(b);
  } else {
    const input = el('input'); input.type = p.secret ? 'password' : 'text'; input.value = p.default || ''; input.autocomplete = 'off';
    input.spellcheck = false; input.autocapitalize = 'off';
    const b = el('button', '', 'Continue'); const go = () => send(input.value);
    b.onclick = go; input.onkeydown = e => { if (e.key === 'Enter') go(); };
    card.append(input, b);
    if (p.secret) card.append(el('p', 'mute hint', 'Hidden as you type. It goes straight into a GitHub secret and is never saved or shown.'));
    setTimeout(() => input.focus(), 50);
  }
  $('main').replaceChildren(card);
}
let opened = {}, shownApp = '';        // GitHub steps whose tab was opened; what the card shows now
function renderApp(a) {
  const key = a.name + (a.install ? ':created' : opened[a.name] ? ':open' : '');
  if (shownApp === key) return; shownApp = key; shownPrompt = 0;
  const card = el('div', 'card you');
  if (a.n) card.append(el('p', 'mute', 'GitHub App ' + a.n + ' of ' + a.of + ' · ' + a.role));
  card.append(el('h2', '', (a.install ? 'Install the GitHub App ' : 'Create the GitHub App ') + a.name));
  if (a.why) card.append(el('p', 'mute', a.why));
  if (a.done && a.done.length) card.append(el('p', '', a.done.map(d => '✓ ' + d + ' is created and installed').join('  ·  ')));
  const pick = 'choose Only select repositories, tick ' + a.repos.join(' and ') + ', and press Install.';
  const p = el('p');
  if (a.install) p.textContent = 'Step 2 of 2 for this App. It is created; now install it. On GitHub ' + pick;
  else {
    const steps = 'Step 1 of 2 for this App. On GitHub click Create GitHub App. Then GitHub asks where to install it: ' + pick;
    p.textContent = opened[a.name]
      ? 'GitHub is open in another tab. ' + steps + ' You do not need to come back and click anything.'
      : 'The button opens GitHub in a new tab, so this page stays here. ' + steps;
  }
  card.append(p);
  const b = el('a', 'btn', a.install ? 'Install on GitHub' : opened[a.name] ? 'Open GitHub again' : 'Continue to GitHub');
  b.href = a.install || a.start; b.target = '_blank'; b.rel = 'noopener';
  if (!a.install) b.onclick = () => { opened[a.name] = true; setTimeout(() => renderApp(a), 50); };
  card.append(b);
  if (opened[a.name] || a.install) card.append(el('p', 'mute', 'Waiting for GitHub. If GitHub shows Configure instead of Install, this App is already installed and there is nothing more to do: this page carries on by itself within a few seconds.'));
  $('main').replaceChildren(card);
}
function renderDone(s) {
  const card = el('div', 'card'); card.append(el('h2', '', 'ORGrove is installed'));
  card.append(el('p', '', 'Give the agents their first ticket and watch them work.'));
  for (const [t, u] of s.links) { const a = el('a', 'btn', t); a.href = u; a.target = '_blank'; a.rel = 'noopener'; card.append(a); }
  const c = el('button', 'ghost', 'Check my install again'); c.onclick = () => post('/api/check').then(() => { shownPrompt = 0; }); card.append(c);
  const q = el('button', 'ghost', 'Close'); q.onclick = () => post('/api/quit').then(() => document.body.replaceChildren(el('p', 'mute', 'You can close this tab.'))); card.append(q);
  $('main').replaceChildren(card);
}
function renderError(s) {
  const card = el('div', 'card'); card.append(el('h2', '', 'It stopped, and nothing is lost'));
  const m = el('p', 'err'); linked(m, s.error); card.append(m);
  card.append(el('p', 'mute', 'Fix what it says, then press Try again: it continues where it stopped.'));
  const b = el('button', '', 'Try again'); b.onclick = () => post('/api/retry').then(() => { shownPrompt = 0; $('main').replaceChildren(el('p', 'mute', 'Starting again…')); }); card.append(b);
  $('main').replaceChildren(card);
}
function renderLog(lines) {
  const log = $('log');
  for (const l of lines) { const d = el('div', l.kind); linked(d, l.text); log.append(d); }
  if (lines.length) log.scrollTop = log.scrollHeight;
}
// What the page shows for a state, and whether the screen on show is out of date. Kept as plain functions so a test can run them.
function viewOf(s) { return s.prompt ? 'prompt' : s.app ? 'app' : s.status === 'done' ? 'done' : s.status === 'error' ? 'error' : 'working'; }
function staleScreen(last, now) { return now === 'working' && last !== 'working'; }   // the card of a step that has ended
function workingText(last) { return last === 'app' ? '✓ That App is installed. Working on the next step…' : 'Working…'; }
let lastView = '';
async function tick() {
  try {
    const r = await fetch('/api/state?since=' + since, {headers: {'X-ORGrove': '1'}});
    if (r.status === 403) { $('main').replaceChildren(el('p', 'err', 'Open the address the installer printed in the terminal.')); return; }
    const s = await r.json(); since = s.next; renderLog(s.log); renderRail(s.rail);
    if (!s.app) shownApp = '';
    const v = viewOf(s);
    if (v === 'prompt') renderPrompt(s.prompt);
    else if (v === 'app') renderApp(s.app);
    else if (v === 'done') renderDone(s);
    else if (v === 'error') renderError(s);
    else if (staleScreen(lastView, v) || !$('main').firstChild || shownPrompt) { shownPrompt = 0; $('main').replaceChildren(el('p', 'mute', workingText(lastView))); }
    lastView = v;
  } catch (e) { /* the server is gone or busy: try again */ }
}
// light or dark: the system's choice until the person picks one; the logo has a version for each background
const root = document.documentElement, system = matchMedia('(prefers-color-scheme: light)');
const theme = () => root.dataset.theme || (system.matches ? 'light' : 'dark');
function paint() {
  const t = theme(); $('logo').src = '/brand/orgrove-' + t + '.png';
  $('theme').textContent = t === 'light' ? '\u263E' : '\u2600';
  $('theme').title = t === 'light' ? 'Switch to dark' : 'Switch to light';
}
$('theme').onclick = () => {
  const next = theme() === 'light' ? 'dark' : 'light'; root.dataset.theme = next;
  try { localStorage.setItem('orgrove-theme', next); } catch (e) { /* a private window: it still switches, it just isn't remembered */ }
  paint();
};
system.addEventListener('change', paint); paint();
setInterval(tick, 700); tick();
</script>"""
