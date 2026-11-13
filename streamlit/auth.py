"""Login for the dashboard: one password, a signed session cookie.

The dashboard shows real trades and deposits, and its port is open to every
device on the Pi's networks (docs/audit/dashboard-security-assessment.md).
`LoginGate` is an ASGI middleware that serve.py puts in front of Streamlit, so
it sees every request, the websocket included, before Streamlit does. Only
`/_stcore/health` is let through without a session, for the Docker health
check.

Standard library only, so the tests run without Streamlit installed. It
writes one file, the session epoch (`STATE_FILE`), so a logout outlives a
restart (`Sessions`). An open websocket is held to its session for as long as
it stays open, not only when it connects (`_Socket`).

    python3 auth.py hash      # type a password; prints a DASHBOARD_PASSWORD_HASH value
    python3 auth.py secret    # prints a DASHBOARD_SESSION_SECRET value

The page is plain HTTP on the home network: the password crosses it once per
login and the cookie with every request. TLS would fix that; this does not.
"""

import asyncio
import base64
import binascii
import contextlib
import getpass
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from collections import deque
from urllib.parse import parse_qs, urlsplit

LOG = logging.getLogger("dashboard.auth")

COOKIE_NAME = "fjord_session"
HEALTH_PATH = "/_stcore/health"
LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"
# The session epoch, on the dashboard's own volume (docker-compose.yml): the one
# file the login writes, so that a logout outlives a restart.
STATE_FILE = "/app/state/session-epoch"
_EPOCH = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)

SCHEME = "pbkdf2_sha256"
ITERATIONS = 600_000          # OWASP's figure for PBKDF2-HMAC-SHA256
MIN_PASSWORD_LENGTH = 12
DEFAULT_SESSION_HOURS = 12.0
MAX_FAILURES = 5              # per client address ...
MAX_FAILURES_ALL = 50         # ... and from all addresses together (a LAN host can take many)
FAILURE_WINDOW_SECONDS = 15 * 60   # ... in this window, then 429 until it passes
MAX_LOGIN_BODY = 4096
_ALL_CLIENTS = "*"
WATCH_SECONDS = 60.0          # an open websocket's session is re-checked at least this often
POLICY_VIOLATION = 1008       # websocket close code: refused, or its session is over

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


def _write_atomically(path: str, text: str) -> None:
    """Replace `path` with `text`: after a crash it holds the old text or the new, never half."""
    directory = os.path.dirname(path) or "."
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".session-epoch.")  # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    # The rename must reach the disk too, or a power cut could bring the old epoch back.
    # Best effort: the new epoch is already in place, and a failure here must not undo it.
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as error:
        LOG.warning("session_state_not_synced path=%s error=%s", path, error)


def _exists(path: str) -> bool:
    """False only when `path` is certainly not there."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


class Sessions:
    """Signed, expiring session tokens: `epoch.issued_ms.expires_ms.nonce.signature`.

    Nothing is stored per session. A token is accepted only in the epoch it was
    issued in, and logging out starts a new epoch, so every token issued
    earlier is refused, on every device.

    The epoch is a random value kept in `state_path`, so that a logout outlives
    a restart: the signing key comes from .env and survives one, which an
    in-memory logout would not. Whenever that file cannot be trusted to hold
    the latest logout (missing, unreadable, not an epoch, not writable), a new
    epoch is used instead. Trouble with the file can end every session; it
    cannot bring an old one back. Without a `state_path`, every start is a new
    epoch.
    """

    def __init__(self, secret: bytes, max_age_seconds: int, clock=time.time, state_path=None):
        self._secret = secret
        self.max_age = max_age_seconds
        self._clock = clock
        self._state_path = state_path
        self._lock = threading.Lock()
        self.ready = True  # False: an old epoch is stuck on disk; the gate then answers 503
        self.epoch = self._load_epoch()

    def _load_epoch(self) -> str:
        path = self._state_path
        if path is None:
            return secrets.token_urlsafe(32)
        stored, found = "", "created"
        try:
            with open(path, encoding="ascii") as handle:
                stored = handle.read(256).strip()
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:  # ValueError: not ASCII
            LOG.error("session_state_unreadable path=%s error=%s: every session ends", path, error)
        else:
            if _EPOCH.fullmatch(stored):
                found = "loaded"
            else:
                LOG.error("session_state_invalid path=%s: every session ends", path)
                stored = ""
        epoch = stored or secrets.token_urlsafe(32)
        # Writing it back proves that a logout could be saved too.
        outcome = self._save(epoch)
        if outcome == "saved":
            LOG.info("session_state_%s path=%s", found, path)
        elif outcome == "stale":
            LOG.error("session_state_stuck path=%s: cannot write or remove it, so a logout could "
                      "not be kept; every page answers 503 until the volume is writable", path)
            self.ready = False
        return epoch

    def _save(self, epoch: str) -> str:
        """'saved'; 'cleared' when it could not be, but no older epoch is left on
        disk either (a restart then starts a new one); 'stale' when the older one
        is still there, which a restart would trust."""
        try:
            _write_atomically(self._state_path, epoch + "\n")
            return "saved"
        except OSError as error:
            LOG.error("session_state_unwritable path=%s error=%s: sessions end at the next restart",
                      self._state_path, error)
        try:
            os.unlink(self._state_path)
        except OSError as error:
            # A read-only filesystem refuses even to remove a file that is not there.
            if _exists(self._state_path):
                LOG.error("session_state_not_removed path=%s error=%s", self._state_path, error)
                return "stale"
        return "cleared"

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())

    def issue(self) -> str:
        issued = self._now_ms()
        body = f"{self.epoch}.{issued}.{issued + self.max_age * 1000}.{secrets.token_urlsafe(12)}"
        return f"{body}.{self._sign(body)}"

    def claims(self, token: str):
        """(epoch, expires_ms) of a token that is valid now, else None."""
        body, _, signature = token.rpartition(".")
        if not body or not body.isascii():
            return None
        if not hmac.compare_digest(signature.encode("utf-8"), self._sign(body).encode("ascii")):
            return None
        try:
            epoch, issued_text, expires_text, _nonce = body.split(".")
            issued, expires = int(issued_text), int(expires_text)
        except ValueError:
            return None
        if not hmac.compare_digest(epoch, self.epoch):
            return None
        if not issued <= self._now_ms() < expires:
            return None
        return epoch, expires

    def valid(self, token: str) -> bool:
        return self.claims(token) is not None

    def current(self, epoch: str, expires_ms: int) -> bool:
        """Whether a session `claims` accepted earlier is still in force."""
        return epoch == self.epoch and self._now_ms() < expires_ms

    def revoke_all(self) -> bool:
        """End every session issued so far.

        In force in this process before the file is touched. False when it could
        not be saved and the old epoch is still on disk, where a restart would
        trust it again.
        """
        with self._lock:
            self.epoch = secrets.token_urlsafe(32)
            return self._state_path is None or self._save(self.epoch) != "stale"


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


class _Socket:
    """One open dashboard websocket, held to the session it was opened with.

    It keeps that session's epoch and expiry, never the token. Every message to
    the browser is checked first, so once the session is over (logout or
    expiry) nothing more is delivered: the socket is closed with 1008 instead,
    and the app is told that the browser left, so Streamlit ends its session
    there. Streamlit's page then tries to reconnect, which the gate refuses
    until the browser signs in again.
    """

    def __init__(self, sessions, epoch, expires_ms, receive, send, client):
        self._sessions = sessions
        self.epoch, self.expires_ms, self.client = epoch, expires_ms, client
        self._receive, self._send = receive, send
        self._lock = asyncio.Lock()  # one message to the server at a time; none after the close
        self._closed = False
        self._opening = True  # the browser's opening message is still to be read
        self.over = asyncio.get_running_loop().create_future()  # done once the session is over

    @property
    def live(self) -> bool:
        return not self.over.done() and self._sessions.current(self.epoch, self.expires_ms)

    def end(self) -> None:
        """The session is over: from now on nothing more reaches the browser."""
        if not self.over.done():
            self.over.set_result(None)

    async def shut(self) -> None:
        """End the session and close the socket, once."""
        self.end()
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        # Called with the lock held.
        if self._closed:
            return
        self._closed = True
        LOG.info("websocket_closed reason=%s client=%s",
                 "logout" if self.epoch != self._sessions.epoch else "expired", self.client)
        try:
            await self._send({"type": "websocket.close", "code": POLICY_VIOLATION})
        except Exception as error:  # the browser had already gone
            LOG.debug("websocket_close_failed client=%s error=%s", self.client, type(error).__name__)

    async def send(self, message) -> None:
        """The app's messages to the browser, only while the session lasts."""
        async with self._lock:
            if self._closed:
                return
            if message.get("type") == "websocket.close":
                self._closed = True
                return await self._send(message)
            if not self.live:
                self.end()
                return await self._close()  # instead of the message: before the accept, a 403
            await self._send(message)

    async def receive(self):
        """The browser's messages to the app, until the session is over.

        The app waits here while the page is idle, so a logout or expiry must
        interrupt the wait: it then gets a disconnect, and stops.
        """
        if self._opening:
            # `websocket.connect` carries no data, and Starlette insists on
            # reading it before it accepts; the accept is checked in `send`.
            self._opening = False
            return await self._receive()
        if self.live:
            pending = asyncio.ensure_future(self._receive())
            try:
                await asyncio.wait({pending, self.over}, return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                pending.cancel()
                raise
            if self.live:  # so `pending` is what finished
                return pending.result()
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                pending.exception()  # read, so asyncio does not warn; the message is dropped
        await self.shut()
        return {"type": "websocket.disconnect", "code": POLICY_VIOLATION}


class LoginGate:
    """ASGI middleware: every request needs a valid session, except the health check.

    Without a usable `password_hash`, or when a logout could not be kept across
    a restart (`Sessions.ready`), it fails closed: 503 for everything but the
    health check.

    A websocket needs a valid session to open and keeps needing one while it
    is open: a logout closes every open websocket, on every device, and each
    one closes by itself when its session expires (`_Socket`, `_watch`).
    """

    watch_seconds = WATCH_SECONDS

    def __init__(self, app, password_hash: str = "", session_secret: str = "",
                 session_hours: float = DEFAULT_SESSION_HOURS, clock=time.time, state_path=None):
        self.app = app
        self._clock = clock
        self._sockets = set()  # the open websockets, so a logout can close them all
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
        if state_path is None:
            LOG.warning("dashboard_session_state_unset: sessions end when the dashboard restarts")
        self.sessions = Sessions(key, int(session_hours * 3600), clock, state_path)
        self.limiter = FailureLimiter(clock=clock)
        self.limiter_all = FailureLimiter(limit=MAX_FAILURES_ALL, clock=clock)
        self._closed = (b"Dashboard login is not configured.\n" if not self.configured else
                        b"Dashboard login cannot save its sessions.\n" if not self.sessions.ready else None)

    def _claims(self, headers: dict):
        token = _cookie(headers, COOKIE_NAME)
        return self.sessions.claims(token) if token else None

    def _has_session(self, headers: dict) -> bool:
        return self._claims(headers) is not None

    async def _serve_websocket(self, scope, receive, send, claims):
        # Registered before the app runs, with no pause since the session was
        # checked: a logout has either refused this socket already or will find it.
        socket = _Socket(self.sessions, *claims, receive, send, _client(scope))
        self._sockets.add(socket)
        watcher = asyncio.ensure_future(self._watch(socket))
        try:
            return await self.app(scope, socket.receive, socket.send)
        finally:
            self._sockets.discard(socket)
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _watch(self, socket: _Socket) -> None:
        """Close `socket` once its session is over, even if neither side sends anything."""
        while socket.live:
            left = socket.expires_ms / 1000 - self._clock()
            await asyncio.wait({socket.over}, timeout=min(self.watch_seconds, max(left, 0.001)))
        await socket.shut()

    def _end_sockets(self) -> None:
        """After a logout: every open websocket's session is over, on every device.
        Each one's `_watch` then closes it, and its `receive` tells the app."""
        for socket in self._sockets:
            socket.end()

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):  # lifespan
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if kind == "http" and path == HEALTH_PATH:
            return await self.app(scope, receive, send)
        if self._closed:
            if kind == "websocket":
                return await _refuse_websocket(receive, send)
            return await _respond(send, 503, self._closed)

        headers = _headers(scope)
        if kind == "websocket":
            claims = self._claims(headers)
            if claims and _websocket_origin_allowed(headers):
                return await self._serve_websocket(scope, receive, send, claims)
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
        cookie = (b"set-cookie", f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax".encode("ascii"))
        # Only a signed-in browser ends every session; anyone else just loses
        # their own (invalid) cookie, so a forged form cannot log the owner out.
        if self._has_session(headers):
            # Synchronous on purpose: no request is checked between the new epoch
            # and its write to disk, and the answer goes out only once it is saved.
            kept = self.sessions.revoke_all()
            self._end_sockets()
            LOG.info("logout client=%s", _client(scope))
            if not kept:
                return await _respond(send, 500, b"Logged out, but the dashboard could not save the logout: "
                                      b"a restart could let old sessions back in. To end them for certain, "
                                      b"change DASHBOARD_SESSION_SECRET and restart the dashboard. "
                                      b"See the dashboard log.\n", extra=[cookie])
        return await _respond(send, 303, b"", extra=[(b"location", LOGIN_PATH.encode()), cookie])


def settings_from_env(environ=os.environ) -> dict:
    """LoginGate's keyword arguments, from DASHBOARD_PASSWORD_HASH and friends.

    The state file is not a setting: it lives where compose mounts the volume.
    """
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
        "state_path": STATE_FILE,
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
