"""Jira HTTP: the one injectable `transport(method, path, body) -> (status, json)`, and a `Client` that adds
retries and turns statuses into errors. Nothing here puts a credential in a message.

The real transport talks only to `https://<name>.atlassian.net`, never follows a redirect (urllib would resend the
Authorization header to the new host), and reports a `Retry-After` header as `{"_retry_after": seconds}` in the body.
"""
import base64
import json
import re
import time
import urllib.error
import urllib.request

from orgrove.ports import NotFound, PortError

SITE = re.compile(r"https://[a-z0-9][a-z0-9-]{0,62}\.atlassian\.net/?")
API = "/rest/api/3/"
MAX_ATTEMPTS = 3                 # one try and two retries on 429 / 5xx
MAX_WAIT_S = 30.0


class JiraError(PortError):
    """A Jira call that failed. The message names the method and path, never a credential."""


def validate_site(site):
    """The base URL without a trailing slash; anything but an `atlassian.net` site is refused."""
    if not isinstance(site, str) or not SITE.fullmatch(site):
        raise ValueError("Jira site must look like https://<name>.atlassian.net")
    return site.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _default_opener(req, timeout):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)  # noqa: S310  https atlassian.net only, checked by validate_site


def make_transport(site, email, token, opener=_default_opener, timeout=30):
    """A transport for one site. `opener(request, timeout)` is injectable so tests never touch the network."""
    base = validate_site(site)
    auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def transport(method, path, body=None):
        if not path.startswith(API):
            raise ValueError(f"Jira path must start with {API}")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method, headers={  # noqa: S310  base is validated https atlassian.net
            "Authorization": auth, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            resp = opener(req, timeout)
            status, raw, headers = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            status, raw, headers = e.code, e.read(), e.headers
        except (urllib.error.URLError, OSError) as e:
            raise JiraError(f"{method} {path}: could not reach the site ({type(e).__name__})") from None
        payload = None
        if raw:
            try:
                payload = json.loads(raw)
            except ValueError:
                if status < 400:
                    raise JiraError(f"{method} {path}: the answer ({status}) is not JSON") from None
        wait = headers.get("Retry-After") if headers else None
        if wait is not None:
            try:
                payload = {**(payload if isinstance(payload, dict) else {}), "_retry_after": float(wait)}
            except ValueError:
                pass
        return status, payload

    return transport


def _clamp(wait):
    """A Retry-After as seconds within 0..MAX_WAIT_S; NaN and anything unreadable wait the maximum."""
    try:
        seconds = float(wait)
    except (TypeError, ValueError):
        return MAX_WAIT_S
    return MAX_WAIT_S if seconds != seconds else max(0.0, min(seconds, MAX_WAIT_S))


class Client:
    """Retries 429 and 5xx a small number of times, then raises; maps the other failures to errors."""

    def __init__(self, transport, sleep=time.sleep):
        self._transport = transport
        self._sleep = sleep

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        for attempt in range(MAX_ATTEMPTS):
            status, payload = self._transport(method, path, body)
            if status in ok:
                return payload
            if (status == 429 or status >= 500) and attempt + 1 < MAX_ATTEMPTS:
                wait = payload.get("_retry_after") if isinstance(payload, dict) else None
                self._sleep(_clamp(wait) if wait is not None else min(2.0 ** attempt, MAX_WAIT_S))
                continue
            break
        where = f"{method} {path}"
        if status in (401, 403):
            raise JiraError(f"{where}: refused ({status}); check the API token and its Jira permission")
        if status == 404:
            raise NotFound(f"{where}: not found (it is gone, or in another project)")
        detail = ""
        if isinstance(payload, dict) and isinstance(payload.get("errorMessages"), list):
            detail = ": " + "; ".join(str(m)[:200] for m in payload["errorMessages"][:3])
        raise JiraError(f"{where}: failed with {status}{detail}")
