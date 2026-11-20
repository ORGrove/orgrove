"""The ORGrove Pro licence (#426): a signed file that says which Pro parts a copy may run, and until when.

    config/licence.json   {"licence": 1, "client": "...", "parts": "all" | ["finance", ...],
                           "issued": "YYYY-MM-DD", "expires": "YYYY-MM-DD", "signature": "<base64>"}

The signature is Ed25519 (RFC 8032) over the canonical JSON of every other field. The check is offline: nothing is
sent anywhere, and there is no telemetry. Python's standard library has no Ed25519 and ORGrove takes no
dependencies, so the arithmetic is RFC 8032's own reference (section 6), checked against its test vectors.

Who needs one: an installed copy (it has `.orgrove-install`, written by bin/package). The publisher's own source
checkout has no marker and runs every part (owner decision, 2026-11-20).

States: valid; grace (up to GRACE_DAYS days after `expires`: the parts still run, the Office and doctor warn);
expired, missing, invalid and early (a clock before `issued`): the Pro parts are off, and their addresses show
the Pro page saying why (orgrove/office.py).

This protects honest customers, not against a determined one: Pro ships as Python source, and anyone can edit
this check. That is a commercial choice (docs/decisions, #426).

Issuing is bin/licence, on the owner's laptop only. The signing key lives outside the repo (KEY_PATH); it never
enters git, and bin/package refuses a copy that has a licence or a key in it.
"""
import base64
import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = "config/licence.json"
INSTALL_MARKER = ".orgrove-install"        # orgrove/update.py MARKER: written into every packaged copy
GRACE_DAYS = 14
FORMAT = 1
# ORGrove's licence public key(s), hex. A key can be added before an old one is retired: any listed key verifies.
PUBLIC_KEYS = ("502d7bf66504782145a9d85d086c48a89e2a636ff8313f823345e4e0b6b64efb",)
KEY_PATH = Path.home() / ".orgrove" / "licence-signing.key"
CONTACT = "info@orgrove.com"

# --- Ed25519, RFC 8032 section 6 ----------------------------------------------------------------------------------
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _h(m):
    return hashlib.sha512(m).digest()


def _add(a, b):
    x = (a[1] - a[0]) * (b[1] - b[0]) % _P
    y = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c = 2 * a[3] * b[3] * _D % _P
    d = 2 * a[2] * b[2] % _P
    e, f, g, h = y - x, d - c, d + c, y + x
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _mul(s, pt):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(pt):
    zinv = pow(pt[2], _P - 2, _P)
    x, y = pt[0] * zinv % _P, pt[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _expand(secret):
    if len(secret) != 32:
        raise ValueError("an Ed25519 secret key is 32 bytes")
    h = _h(secret)
    a = int.from_bytes(h[:32], "little")
    a = (a & ((1 << 254) - 8)) | (1 << 254)
    return a, h[32:]


def public_key(secret):
    return _compress(_mul(_expand(secret)[0], _G))


def sign(secret, msg):
    a, prefix = _expand(secret)
    pub = _compress(_mul(a, _G))
    r = int.from_bytes(_h(prefix + msg), "little") % _L
    big_r = _compress(_mul(r, _G))
    k = int.from_bytes(_h(big_r + pub + msg), "little") % _L
    return big_r + int.to_bytes((r + k * a) % _L, 32, "little")


def verify(public, msg, sig):
    """True only for a valid signature. Never raises on bad input."""
    if not isinstance(public, bytes) or not isinstance(sig, bytes) or len(public) != 32 or len(sig) != 64:
        return False
    pub, big_r = _decompress(public), _decompress(sig[:32])
    s = int.from_bytes(sig[32:], "little")
    if pub is None or big_r is None or s >= _L:
        return False
    k = int.from_bytes(_h(sig[:32] + public + msg), "little") % _L
    return _equal(_mul(s, _G), _add(big_r, _mul(k, pub)))


# --- the licence file ---------------------------------------------------------------------------------------------

def canonical(data):
    """The bytes the signature covers: every field but `signature`, sorted, compact, UTF-8."""
    body = {k: v for k, v in data.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def issue(secret, client, days, parts="all", today=None):
    """A signed licence (a dict, ready for json.dump)."""
    today = today or dt.date.today()
    if not str(client).strip():
        raise ValueError("the licence needs the client's name")
    if int(days) < 1:
        raise ValueError("a licence lasts at least one day")
    if parts != "all":
        parts = sorted(set(parts))
        if not parts:
            raise ValueError("name at least one part, or use all")
    data = {"licence": FORMAT, "client": str(client).strip(), "parts": parts, "issued": today.isoformat(),
            "expires": (today + dt.timedelta(days=int(days))).isoformat()}
    data["signature"] = base64.b64encode(sign(secret, canonical(data))).decode()
    return data


@dataclass(frozen=True)
class Status:
    state: str                # source | valid | grace | expired | missing | invalid | early
    why: str                  # one plain line for doctor, the Office and the Pro page
    client: str = ""
    parts: object = ()        # "all" or a tuple of part names
    expires: str = ""
    days_left: int = 0        # until `expires` (valid) or until the grace ends (grace); 0 otherwise

    @property
    def runs(self):
        """Whether the Pro parts it grants run."""
        return self.state in ("source", "valid", "grace")

    def grants(self, part):
        return self.state == "source" or (self.runs and (self.parts == "all" or part in self.parts))


def _date(value):
    return dt.date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None


def check(data, today, keys=None):
    """The Status of a parsed licence file. Pure: no file, no clock."""
    keys = PUBLIC_KEYS if keys is None else keys
    if not isinstance(data, dict):
        return Status("invalid", f"{FILE} is not a licence")
    try:
        sig = base64.b64decode(data.get("signature", ""), validate=True)
    except (ValueError, TypeError):
        sig = b""
    msg = canonical(data)
    good = False
    for k in keys:
        try:
            good = good or verify(bytes.fromhex(k), msg, sig)
        except ValueError:
            continue
    if not good:
        return Status("invalid", f"{FILE} is not signed by ORGrove (it was changed, or it isn't an ORGrove licence)")
    issued, expires, parts = _date(data.get("issued")), _date(data.get("expires")), data.get("parts")
    if data.get("licence") != FORMAT or not issued or not expires or not (parts == "all" or isinstance(parts, list)):
        return Status("invalid", f"{FILE} is a licence format this copy doesn't read: update ORGrove")
    who = dict(client=str(data.get("client", "")), parts="all" if parts == "all" else tuple(parts),
               expires=expires.isoformat())
    if today < issued - dt.timedelta(days=1):       # a day's slack for time zones
        return Status("early", f"this computer's date ({today}) is before the licence was issued ({issued}): "
                               "check the clock", **who)
    if today <= expires:
        left = (expires - today).days
        return Status("valid", f"licensed to {who['client']} until {expires} ({left} day{'s' * (left != 1)} left)",
                      days_left=left, **who)
    ends = expires + dt.timedelta(days=GRACE_DAYS)
    if today <= ends:
        left = (ends - today).days + 1
        return Status("grace", f"the licence ended on {expires}; ORGrove Pro keeps running for {left} more "
                               f"day{'s' * (left != 1)}. Write to {CONTACT} to renew", days_left=left, **who)
    return Status("expired", f"the licence ended on {expires}: the ORGrove Pro parts are off. Write to {CONTACT} "
                             "to renew", **who)


_cache = {}


def status(root=ROOT, today=None):
    """This copy's licence Status. Read from disk at most once per file change and day (the Office asks often)."""
    root = Path(root)
    if not (root / INSTALL_MARKER).exists():
        return Status("source", "ORGrove's own source checkout: every part runs, no licence needed")
    today = today or dt.date.today()
    path = root / FILE
    try:
        info = path.stat()
        stamp = (info.st_mtime_ns, info.st_size)
    except OSError:
        return Status("missing", f"no licence: put the {FILE} you were sent in this folder, or write to {CONTACT}")
    key = (str(root), stamp, today)
    if key not in _cache:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            _cache[key] = Status("invalid", f"{FILE} can't be read as JSON: use the file you were sent, unchanged")
        else:
            _cache[key] = check(data, today)
        while len(_cache) > 16:
            _cache.pop(next(iter(_cache)))
    return _cache[key]


def load_secret(path=KEY_PATH):
    path = Path(path)
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(f"{path} can be read by others (mode {mode:o}): chmod 600 {path}")
    return bytes.fromhex(path.read_text().strip())


def keygen(path=KEY_PATH):
    """Make the signing key once. Refuses to overwrite one: every licence issued with it would stop verifying."""
    path = Path(path)
    if path.exists():
        raise ValueError(f"{path} exists: a new key would make every licence issued so far invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret.hex() + "\n")
    return public_key(secret).hex()


def main(argv=None):
    """bin/licence: keygen | issue | show. The owner's laptop only (bin/package leaves bin/licence out of every copy)."""
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="bin/licence", description="ORGrove Pro licences (#426)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="make the signing key once; prints the public key for orgrove/licence.py")
    k.add_argument("--key", default=str(KEY_PATH))
    i = sub.add_parser("issue", help="write a signed licence file for a client")
    i.add_argument("--client", required=True)
    i.add_argument("--days", type=int, required=True)
    i.add_argument("--parts", default="all", help="pro, enterprise (= all, the default), or a comma-separated list of parts")
    i.add_argument("--out", default="licence.json")
    i.add_argument("--key", default=str(KEY_PATH))
    s = sub.add_parser("show", help="check a licence file against this copy's public key")
    s.add_argument("file")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "keygen":
            pub = keygen(a.key)
            print(f"Signing key written to {a.key} (mode 600). Back it up offline now: losing it means re-issuing "
                  f"every licence.\nPublic key, for PUBLIC_KEYS in orgrove/licence.py:\n{pub}")
        elif a.cmd == "issue":
            from orgrove import editions
            if a.parts in ("all", "enterprise"):        # Enterprise: every part, each additional floor included
                parts = "all"
            elif a.parts == "pro":                       # Pro: every part sold in Pro (not the Enterprise floors)
                parts = [n for n, part in editions.PARTS.items() if part.edition == "Pro"]
            else:
                parts = [p.strip() for p in a.parts.split(",") if p.strip()]
            if parts != "all" and (bad := sorted(set(parts) - set(editions.PARTS))):
                raise ValueError(f"no such part: {', '.join(bad)} (parts: {', '.join(editions.PARTS)})")
            secret = load_secret(a.key)
            if public_key(secret).hex() not in PUBLIC_KEYS:
                raise ValueError("that signing key's public key isn't in PUBLIC_KEYS: copies would refuse this licence")
            data = issue(secret, a.client, a.days, parts)
            out = Path(a.out)
            if out.exists():
                raise ValueError(f"{out} exists: choose another --out")
            out.write_text(json.dumps(data, indent=2) + "\n")
            print(f"{out}: {check(data, dt.date.today()).why}. Send it to the client; it goes at {FILE} in their copy.")
        else:
            st = check(json.loads(Path(a.file).read_text()), dt.date.today())
            print(f"{st.state}: {st.why}")
            return 0 if st.runs else 1
    except (OSError, ValueError) as e:
        print(f"bin/licence: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
