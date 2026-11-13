"""Login for the dashboard: one password, a signed session cookie.

The dashboard shows real trades and deposits, and its port is open to every
device on the Pi's networks (docs/audit/dashboard-security-assessment.md).
`LoginGate` is an ASGI middleware that serve.py puts in front of Streamlit, so
it sees every request, the websocket included, before Streamlit does. Only
`/_stcore/health` is let through without a session, for the Docker health
check.

Standard library only, so the tests run without Streamlit installed.

    python3 auth.py hash      # type a password; prints a DASHBOARD_PASSWORD_HASH value
    python3 auth.py secret    # prints a DASHBOARD_SESSION_SECRET value

The page is plain HTTP on the home network: the password crosses it once per
login and the cookie with every request. TLS would fix that; this does not.
"""

import asyncio
import base64
import binascii
import getpass
import hashlib
import hmac
import html
import logging
import os
import secrets
import sys
import time
from collections import deque
from urllib.parse import parse_qs, urlsplit

LOG = logging.getLogger("dashboard.auth")

COOKIE_NAME = "fjord_session"
HEALTH_PATH = "/_stcore/health"
LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"

SCHEME = "pbkdf2_sha256"
ITERATIONS = 600_000          # OWASP's figure for PBKDF2-HMAC-SHA256
MIN_PASSWORD_LENGTH = 12
DEFAULT_SESSION_HOURS = 12.0
MAX_FAILURES = 5              # per client address ...
MAX_FAILURES_ALL = 50         # ... and from all addresses together (a LAN host can take many)
FAILURE_WINDOW_SECONDS = 15 * 60   # ... in this window, then 429 until it passes
MAX_LOGIN_BODY = 4096
_ALL_CLIENTS = "*"

_PAGE_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"content-security-policy",
     b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"),
]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    """`pbkdf2_sha256:<iterations>:<salt>:<hash>`. No `$`, so compose leaves it alone."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{SCHEME}:{iterations}:{_b64(salt)}:{_b64(digest)}"


def parse_hash(stored: str):
    """(iterations, salt, digest), or None when `stored` is not a hash this module made."""
    try:
        scheme, iterations, salt, digest = stored.strip().split(":")
        rounds, salt_bytes, digest_bytes = int(iterations), _unb64(salt), _unb64(digest)
    except (AttributeError, ValueError, binascii.Error):
        return None
    if scheme != SCHEME or rounds < 1 or not salt_bytes or not digest_bytes:
        return None
    return rounds, salt_bytes, digest_bytes


def verify_password(password: str, stored: str) -> bool:
    parsed = parse_hash(stored)
    if parsed is None:
        return False
    rounds, salt, digest = parsed
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(candidate, digest)


class Sessions:
    """Signed, expiring session tokens: `issued_ms.expires_ms.nonce.signature`.

    Nothing is stored per session. Logging out moves `not_before` forward, so
    every token issued earlier is refused, on every device.
    """

    def __init__(self, secret: bytes, max_age_seconds: int, clock=time.time):
        self._secret = secret
        self.max_age = max_age_seconds
        self._clock = clock
        self._not_before_ms = 0

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())

    def issue(self) -> str:
        issued = self._now_ms()
        body = f"{issued}.{issued + self.max_age * 1000}.{secrets.token_urlsafe(12)}"
        return f"{body}.{self._sign(body)}"

    def valid(self, token: str) -> bool:
        body, _, signature = token.rpartition(".")
        if not body or not body.isascii():
            return False
        if not hmac.compare_digest(signature.encode("utf-8"), self._sign(body).encode("ascii")):
            return False
        try:
            issued_text, expires_text, _nonce = body.split(".")
            issued, expires = int(issued_text), int(expires_text)
        except ValueError:
            return False
        now = self._now_ms()
        return self._not_before_ms <= issued <= now < expires

    def revoke_all(self) -> None:
        self._not_before_ms = self._now_ms() + 1


class FailureLimiter:
    """Failed logins per client address within a sliding window."""

    def __init__(self, limit=MAX_FAILURES, window=FAILURE_WINDOW_SECONDS, clock=time.time,
                 max_clients=1024):
        self._limit, self._window, self._clock = limit, window, clock
        self._max_clients = max_clients
        self._failures = {}

    def retry_after(self, client: str) -> int:
        """Seconds until `client` may try again; 0 when it may try now."""
        now = self._clock()
        recent = self._failures.get(client)
        if not recent:
            return 0
        while recent and recent[0] <= now - self._window:
            recent.popleft()
        if len(recent) < self._limit:
            return 0
        return int(recent[0] + self._window - now) + 1

    def record_failure(self, client: str) -> None:
        self._failures.setdefault(client, deque()).append(self._clock())
        if len(self._failures) > self._max_clients:
            self._failures.pop(next(iter(self._failures)))

    def reset(self, client: str) -> None:
        self._failures.pop(client, None)


def _headers(scope) -> dict:
    found = {}
    for raw_name, raw_value in scope.get("headers") or []:
        name, value = raw_name.decode("latin-1").lower(), raw_value.decode("latin-1")
        joiner = "; " if name == "cookie" else ", "
        found[name] = f"{found[name]}{joiner}{value}" if name in found else value
    return found


def _cookie(headers: dict, name: str) -> str:
    for part in headers.get("cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def _client(scope) -> str:
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


def _same_origin(url: str, headers: dict) -> bool:
    host = headers.get("host", "").lower()
    return bool(host) and urlsplit(url).netloc.lower() == host


def _form_post_allowed(headers: dict) -> bool:
    """CSRF check for login and logout: the form must come from this site."""
    origin = headers.get("origin")
    if origin is not None:
        return origin != "null" and _same_origin(origin, headers)
    referer = headers.get("referer")
    return referer is not None and _same_origin(referer, headers)


def _websocket_origin_allowed(headers: dict) -> bool:
    """Browsers always send Origin on a websocket; a client without one still needs the cookie."""
    origin = headers.get("origin")
    if origin is None:
        return True
    return origin != "null" and _same_origin(origin, headers)


def _safe_next(value: str) -> str:
    """A path on this site to return to after login, never another site."""
    if (not value or not value.isascii() or not value.startswith("/") or value.startswith("//")
            or "\\" in value or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path in (LOGIN_PATH, LOGOUT_PATH):
        return "/"
    return value


def _page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fjord Signals: {html.escape(title)}</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #f6f7f9; color: #1d2330; margin: 0; }}
main {{ max-width: 22rem; margin: 18vh auto 0; padding: 2rem; background: #fff;
        border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.12); }}
h1 {{ font-size: 1.25rem; margin: 0 0 1rem; }}
label {{ display: block; margin-bottom: .4rem; }}
input[type=password] {{ width: 100%; box-sizing: border-box; padding: .6rem; font-size: 1rem;
        border: 1px solid #c3c8d2; border-radius: 6px; }}
button {{ margin-top: 1rem; padding: .6rem 1.2rem; font-size: 1rem; border: 0; border-radius: 6px;
        background: #1f5fbf; color: #fff; cursor: pointer; }}
p.error {{ color: #b3261e; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #11141a; color: #e6e8ec; }}
  main {{ background: #1b1f27; box-shadow: none; }}
  input[type=password] {{ background: #11141a; color: #e6e8ec; border-color: #3a4150; }}
}}
</style></head><body><main>{body}</main></body></html>
""".encode("utf-8")


def _login_page(next_path: str, error: str = "") -> bytes:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return _page("log in", f"""<h1>Fjord Signals</h1>{message}
<form method="post" action="{LOGIN_PATH}">
<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Log in</button>
</form>""")


def _logout_page() -> bytes:
    return _page("log out", f"""<h1>Log out?</h1>
<p>This ends the session on every device.</p>
<form method="post" action="{LOGOUT_PATH}"><button type="submit">Log out</button></form>""")


async def _respond(send, status: int, body: bytes, content_type=b"text/plain; charset=utf-8",
                   extra=()) -> None:
    headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode()),
               *_PAGE_HEADERS, *extra]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _respond_page(send, status: int, page: bytes, extra=()) -> None:
    await _respond(send, status, page, b"text/html; charset=utf-8", extra)


async def _refuse_websocket(receive, send) -> None:
    # A close before the accept makes the server answer the handshake with 403.
    message = await receive()
    if message.get("type") == "websocket.connect":
        await send({"type": "websocket.close", "code": 1008})


async def _read_body(receive, limit: int):
    """The request body, or None when it is larger than `limit`."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            return b"".join(chunks)
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


class LoginGate:
    """ASGI middleware: every request needs a valid session, except the health check.

    Without a usable `password_hash` it fails closed: 503 for everything but the
    health check.
    """

    def __init__(self, app, password_hash: str = "", session_secret: str = "",
                 session_hours: float = DEFAULT_SESSION_HOURS, clock=time.time):
        self.app = app
        self._hash = password_hash.strip()
        self.configured = parse_hash(self._hash) is not None
        if not self.configured:
            LOG.error("dashboard_login_not_configured: every page answers 503 until "
                      "DASHBOARD_PASSWORD_HASH is set (python3 auth.py hash)")
        if session_secret:
            if len(session_secret) < 32:
                LOG.warning("dashboard_session_secret_short: use 32 or more characters (python3 auth.py secret)")
            key = session_secret.encode("utf-8")
        else:
            LOG.warning("dashboard_session_secret_unset: sessions end when the dashboard restarts")
            key = secrets.token_bytes(32)
        self.sessions = Sessions(key, int(session_hours * 3600), clock)
        self.limiter = FailureLimiter(clock=clock)
        self.limiter_all = FailureLimiter(limit=MAX_FAILURES_ALL, clock=clock)

    def _has_session(self, headers: dict) -> bool:
        token = _cookie(headers, COOKIE_NAME)
        return bool(token) and self.sessions.valid(token)

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):  # lifespan
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if kind == "http" and path == HEALTH_PATH:
            return await self.app(scope, receive, send)
        if not self.configured:
            if kind == "websocket":
                return await _refuse_websocket(receive, send)
            return await _respond(send, 503, b"Dashboard login is not configured.\n")

        headers = _headers(scope)
        if kind == "websocket":
            if self._has_session(headers) and _websocket_origin_allowed(headers):
                return await self.app(scope, receive, send)
            # Debug only: a tab left open after logout retries for as long as it stays open.
            LOG.debug("websocket_refused client=%s", _client(scope))
            return await _refuse_websocket(receive, send)
        if path == LOGIN_PATH:
            return await self._login(scope, receive, send, headers)
        if path == LOGOUT_PATH:
            return await self._logout(scope, receive, send, headers)
        if self._has_session(headers):
            return await self.app(scope, receive, send)

        if scope.get("method") == "GET" and "text/html" in headers.get("accept", ""):
            query = scope.get("query_string", b"").decode("latin-1")
            return await _respond_page(send, 401, _login_page(_safe_next(f"{path}?{query}" if query else path)))
        return await _respond(send, 401, b"Login required.\n")

    async def _login(self, scope, receive, send, headers):
        method = scope.get("method")
        if method == "GET":
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            return await _respond_page(send, 200, _login_page(_safe_next((query.get("next") or [""])[0])))
        if method != "POST":
            return await _respond(send, 405, b"Method not allowed.\n", extra=[(b"allow", b"GET, POST")])

        client = _client(scope)
        if not _form_post_allowed(headers):
            LOG.warning("login_refused reason=cross_origin client=%s", client)
            return await _respond(send, 403, b"Cross-site login refused.\n")
        wait = max(self.limiter.retry_after(client), self.limiter_all.retry_after(_ALL_CLIENTS))
        if wait:
            LOG.warning("login_rate_limited client=%s retry_after=%d", client, wait)
            return await _respond(send, 429, b"Too many failed logins. Try again later.\n",
                                  extra=[(b"retry-after", str(wait).encode())])
        body = await _read_body(receive, MAX_LOGIN_BODY)
        if body is None:
            return await _respond(send, 413, b"Request too large.\n")
        try:
            form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=8)
        except ValueError:
            return await _respond(send, 400, b"Bad login form.\n")
        password = (form.get("password") or [""])[0]
        next_path = _safe_next((form.get("next") or [""])[0])

        # PBKDF2 takes a good part of a second on the Pi: off the event loop, so
        # open dashboards keep streaming meanwhile.
        if not await asyncio.to_thread(verify_password, password, self._hash):
            self.limiter.record_failure(client)
            self.limiter_all.record_failure(_ALL_CLIENTS)
            LOG.warning("login_failed client=%s", client)
            return await _respond_page(send, 401, _login_page(next_path, "Wrong password."))

        self.limiter.reset(client)
        LOG.info("login_ok client=%s", client)
        cookie = (f"{COOKIE_NAME}={self.sessions.issue()}; Path=/; Max-Age={self.sessions.max_age}; "
                  "HttpOnly; SameSite=Lax")
        if scope.get("scheme") == "https":
            cookie += "; Secure"
        return await _respond(send, 303, b"", extra=[(b"location", next_path.encode("ascii")),
                                                     (b"set-cookie", cookie.encode("ascii"))])

    async def _logout(self, scope, receive, send, headers):
        method = scope.get("method")
        if method == "GET":
            return await _respond_page(send, 200, _logout_page())
        if method != "POST":
            return await _respond(send, 405, b"Method not allowed.\n", extra=[(b"allow", b"GET, POST")])
        if not _form_post_allowed(headers):
            LOG.warning("logout_refused reason=cross_origin client=%s", _client(scope))
            return await _respond(send, 403, b"Cross-site logout refused.\n")
        # Only a signed-in browser ends every session; anyone else just loses
        # their own (invalid) cookie, so a forged form cannot log the owner out.
        if self._has_session(headers):
            self.sessions.revoke_all()
            LOG.info("logout client=%s", _client(scope))
        cookie = f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        return await _respond(send, 303, b"", extra=[(b"location", LOGIN_PATH.encode()),
                                                     (b"set-cookie", cookie.encode("ascii"))])


def settings_from_env(environ=os.environ) -> dict:
    """LoginGate's keyword arguments, from DASHBOARD_PASSWORD_HASH and friends."""
    hours_text = environ.get("DASHBOARD_SESSION_HOURS", "").strip()
    try:
        hours = float(hours_text) if hours_text else DEFAULT_SESSION_HOURS
        if not 0 < hours <= 24 * 30:
            raise ValueError
    except ValueError:
        LOG.warning("dashboard_session_hours_invalid: using %g", DEFAULT_SESSION_HOURS)
        hours = DEFAULT_SESSION_HOURS
    return {
        "password_hash": environ.get("DASHBOARD_PASSWORD_HASH", "").strip(),
        "session_secret": environ.get("DASHBOARD_SESSION_SECRET", "").strip(),
        "session_hours": hours,
    }


def main(argv) -> int:
    command = argv[1:]
    if command == ["secret"]:
        print(secrets.token_urlsafe(32))
        return 0
    if command == ["hash"]:
        password = getpass.getpass("Dashboard password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Use at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
            return 1
        if getpass.getpass("Same again: ") != password:
            print("The two entries differ.", file=sys.stderr)
            return 1
        print(hash_password(password))
        return 0
    print("usage: python3 auth.py hash | secret", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
