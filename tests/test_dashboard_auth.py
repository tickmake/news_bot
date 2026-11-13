"""The dashboard's login (streamlit/auth.py): nothing answers without a session.

`LoginGate` is driven with plain ASGI messages (no HTTP client, no Streamlit),
so these run in the bot's CI. The app behind the gate records every call: a
request that reaches it unauthenticated is a hole, whatever status comes back.
"""

import ast
import asyncio
import importlib.util
import io
import logging
import os
import re
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "streamlit")


def _load_auth():
    # By path: the folder is named like the Streamlit package and is not importable as one.
    spec = importlib.util.spec_from_file_location("dashboard_auth", os.path.join(DASHBOARD, "auth.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


auth = _load_auth()
# Failed logins warn by design; keep them out of the test output (assertLogs still sees them).
auth.LOG.addHandler(logging.NullHandler())
auth.LOG.propagate = False

PASSWORD = "correct horse battery"
HASH = auth.hash_password(PASSWORD, iterations=1000)  # fast; the real default is checked below
SECRET = "s" * 40
HOST = "raspberrypi5-02:8002"
ORIGIN = f"http://{HOST}"
LAN = ("192.168.4.10", 50000)

PROTECTED = ["/", "/my-money", "/money-put-in", "/sent-picks", "/_stcore/metrics",
             "/_stcore/host-config", "/_stcore/script-health-check", "/media/abc.png",
             "/static/js/index.js", "/component/x/index.html", "/_stcore/agent/openapi.json",
             "/_stcore/upload_file/session/file", "/auth/login", "/oauth2callback"]


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Inner:
    """The app behind the gate: answers 200 and remembers being called."""

    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append((scope["type"], scope.get("path")))
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"inner"})
        elif scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.accept"})


class Response:
    def __init__(self, messages):
        start = next(m for m in messages if m["type"] == "http.response.start")
        self.status = start["status"]
        self.headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in start["headers"]]
        self.body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")

    def header(self, name):
        return next((v for k, v in self.headers if k == name), None)

    @property
    def text(self):
        return self.body.decode("utf-8")


def _scope_headers(headers):
    return [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]


def http(gate, path, method="GET", headers=None, body=b"", query=b"", scheme="http", client=LAN,
         chunks=None):
    headers = {"host": HOST, **(headers or {})}
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": scheme, "path": path, "raw_path": path.encode(), "query_string": query,
             "root_path": "", "headers": _scope_headers(headers), "client": client,
             "server": ("raspberrypi5-02", 8002)}
    incoming = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
                for i, c in enumerate(chunks)] if chunks else [{"type": "http.request", "body": body}]
    sent = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(gate(scope, receive, send))
    return Response(sent)


def websocket(gate, headers=None, path="/_stcore/stream"):
    """'accepted' or the close code the gate answered the handshake with."""
    headers = {"host": HOST, **(headers or {})}
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": _scope_headers(headers), "client": LAN, "subprotocols": ["streamlit"]}
    incoming = [{"type": "websocket.connect"}]
    sent = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    asyncio.run(gate(scope, receive, send))
    first = sent[0]
    return "accepted" if first["type"] == "websocket.accept" else first["code"]


def make_gate(password_hash=HASH, secret=SECRET, hours=12, clock=None):
    inner = Inner()
    with mock.patch.object(auth.LOG, "warning"), mock.patch.object(auth.LOG, "error"):
        gate = auth.LoginGate(inner, password_hash=password_hash, session_secret=secret,
                              session_hours=hours, clock=clock or Clock())
    return gate, inner


def login(gate, password=PASSWORD, next_path="/", headers=None, client=LAN, scheme="http"):
    form = f"password={password.replace(' ', '+')}&next={next_path}".encode()
    return http(gate, "/login", "POST", headers={"origin": ORIGIN, **(headers or {})}, body=form,
                client=client, scheme=scheme)


def session_cookie(response):
    match = re.match(r"fjord_session=([^;]*)", response.header("set-cookie") or "")
    return match.group(1) if match else None


def signed_in(gate):
    response = login(gate)
    assert response.status == 303, response.status
    return {"cookie": f"fjord_session={session_cookie(response)}"}


class PasswordHashTests(unittest.TestCase):
    def test_a_hash_checks_its_own_password_only(self):
        self.assertTrue(auth.verify_password(PASSWORD, HASH))
        self.assertFalse(auth.verify_password(PASSWORD + "x", HASH))
        self.assertFalse(auth.verify_password("", HASH))

    def test_the_stored_form_survives_compose_interpolation(self):
        self.assertNotIn("$", HASH)
        self.assertRegex(HASH, r"^pbkdf2_sha256:1000:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+$")

    def test_the_default_strength(self):
        self.assertEqual(auth.ITERATIONS, 600_000)

    def test_salts_differ(self):
        self.assertNotEqual(auth.hash_password(PASSWORD, iterations=1000), HASH)

    def test_anything_else_is_not_a_hash(self):
        for stored in ["", "   ", "x", PASSWORD, "pbkdf2_sha256:abc:c2FsdA:aGFzaA",
                       "md5:1000:c2FsdA:aGFzaA", "pbkdf2_sha256:0:c2FsdA:aGFzaA",
                       "pbkdf2_sha256:1000::aGFzaA", "pbkdf2_sha256:1000:c2FsdA:", "a:b:c:d:e"]:
            with self.subTest(stored=stored):
                self.assertIsNone(auth.parse_hash(stored))
                self.assertFalse(auth.verify_password(PASSWORD, stored))


class UnauthenticatedTests(unittest.TestCase):
    def test_no_route_answers_without_a_session(self):
        gate, inner = make_gate()
        for path in PROTECTED:
            for method in ("GET", "POST", "PUT", "DELETE"):
                with self.subTest(path=path, method=method):
                    response = http(gate, path, method)
                    self.assertEqual(response.status, 401)
                    self.assertNotIn(b"inner", response.body)
        self.assertEqual(inner.calls, [])

    def test_a_browser_gets_the_login_form_with_401(self):
        gate, inner = make_gate()
        response = http(gate, "/my-money", headers={"accept": "text/html,*/*"}, query=b"tab=2")
        self.assertEqual(response.status, 401)
        self.assertIn('name="password"', response.text)
        self.assertIn('value="/my-money?tab=2"', response.text)
        self.assertEqual(inner.calls, [])

    def test_the_websocket_is_refused_before_it_opens(self):
        gate, inner = make_gate()
        self.assertEqual(websocket(gate), 1008)
        self.assertEqual(websocket(gate, {"origin": ORIGIN}), 1008)
        self.assertEqual(inner.calls, [])

    def test_health_answers_without_a_session(self):
        gate, inner = make_gate()
        self.assertEqual(http(gate, "/_stcore/health").status, 200)
        self.assertEqual(inner.calls, [("http", "/_stcore/health")])

    def test_lifespan_passes_through(self):
        gate, inner = make_gate()

        async def run():
            async def receive():
                return {"type": "lifespan.startup"}

            async def send(_message):
                return None

            await gate({"type": "lifespan"}, receive, send)

        asyncio.run(run())
        self.assertEqual(inner.calls, [("lifespan", None)])


class LoginTests(unittest.TestCase):
    def test_the_form_page(self):
        gate, _ = make_gate()
        response = http(gate, "/login", query=b"next=/sent-picks")
        self.assertEqual(response.status, 200)
        self.assertIn('type="password"', response.text)
        self.assertIn('value="/sent-picks"', response.text)
        self.assertEqual(response.header("cache-control"), "no-store")
        self.assertEqual(response.header("x-frame-options"), "DENY")
        self.assertIn("frame-ancestors 'none'", response.header("content-security-policy"))
        self.assertIn("form-action 'self'", response.header("content-security-policy"))

    def test_the_right_password_sets_a_session_cookie(self):
        gate, inner = make_gate()
        response = login(gate, next_path="/my-money")
        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("location"), "/my-money")
        cookie = response.header("set-cookie")
        for flag in ("HttpOnly", "SameSite=Lax", "Path=/", f"Max-Age={12 * 3600}"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Secure", cookie)  # plain HTTP: a Secure cookie would never come back
        self.assertEqual(inner.calls, [])

    def test_the_cookie_is_secure_under_https(self):
        gate, _ = make_gate()
        self.assertIn("; Secure", login(gate, headers={"origin": f"https://{HOST}"},
                                        scheme="https").header("set-cookie"))

    def test_a_wrong_password_is_401_and_no_cookie(self):
        gate, _ = make_gate()
        response = login(gate, password="guess")
        self.assertEqual(response.status, 401)
        self.assertIsNone(response.header("set-cookie"))
        self.assertIn("Wrong password", response.text)

    def test_a_login_form_from_another_site_is_refused(self):
        gate, _ = make_gate()
        with mock.patch.object(auth, "verify_password", wraps=auth.verify_password) as verify:
            for headers in ({"origin": "http://evil.example"}, {"origin": "null"},
                            {"origin": "http://raspberrypi5-02:9999"}):
                with self.subTest(headers=headers):
                    self.assertEqual(login(gate, headers=headers).status, 403)
            no_origin = http(gate, "/login", "POST", body=f"password={PASSWORD}".encode())
            self.assertEqual(no_origin.status, 403)
            verify.assert_not_called()

    def test_a_same_site_referer_stands_in_for_a_missing_origin(self):
        gate, _ = make_gate()
        response = http(gate, "/login", "POST", headers={"referer": f"{ORIGIN}/login"},
                        body=f"password={PASSWORD.replace(' ', '+')}".encode())
        self.assertEqual(response.status, 303)

    def test_an_oversized_or_malformed_form_is_refused(self):
        gate, _ = make_gate()
        big = http(gate, "/login", "POST", headers={"origin": ORIGIN},
                   chunks=[b"password=" + b"a" * 3000, b"a" * 3000])
        self.assertEqual(big.status, 413)
        many = "&".join(f"f{i}=1" for i in range(20)).encode()
        self.assertEqual(http(gate, "/login", "POST", headers={"origin": ORIGIN}, body=many).status, 400)

    def test_other_methods(self):
        gate, _ = make_gate()
        self.assertEqual(http(gate, "/login", "PUT").status, 405)
        self.assertEqual(http(gate, "/logout", "DELETE").status, 405)

    def test_next_never_leaves_the_site(self):
        for value, expected in [("/my-money", "/my-money"), ("/sent-picks?x=1", "/sent-picks?x=1"),
                                ("//evil.example", "/"), ("https://evil.example/", "/"),
                                ("/\\evil.example", "/"), ("javascript:alert(1)", "/"),
                                ("/ok\r\nSet-Cookie: x=y", "/"), ("/login", "/"), ("/logout", "/"),
                                ("", "/"), ("/café", "/")]:
            with self.subTest(value=value):
                self.assertEqual(auth._safe_next(value), expected)


class RateLimitTests(unittest.TestCase):
    def test_five_failures_lock_the_address_for_the_window(self):
        clock = Clock()
        gate, _ = make_gate(clock=clock)
        for _ in range(5):
            self.assertEqual(login(gate, password="guess").status, 401)
        locked = login(gate)  # even the right password
        self.assertEqual(locked.status, 429)
        self.assertGreater(int(locked.header("retry-after")), 0)
        self.assertEqual(login(gate, client=("192.168.4.11", 1)).status, 303)  # another device
        clock.now += auth.FAILURE_WINDOW_SECONDS + 1
        self.assertEqual(login(gate).status, 303)

    def test_a_success_clears_the_count(self):
        gate, _ = make_gate()
        for _ in range(4):
            login(gate, password="guess")
        self.assertEqual(login(gate).status, 303)
        for _ in range(4):
            self.assertEqual(login(gate, password="guess").status, 401)

    def test_many_addresses_together_are_limited_too(self):
        gate, _ = make_gate()
        for i in range(auth.MAX_FAILURES_ALL):
            login(gate, password="guess", client=(f"10.1.1.{i}", 1))
        self.assertEqual(login(gate, client=("10.1.1.200", 1)).status, 429)


class SessionTests(unittest.TestCase):
    def test_a_session_opens_every_route_and_the_websocket(self):
        gate, inner = make_gate()
        cookie = signed_in(gate)
        for path in PROTECTED:
            with self.subTest(path=path):
                self.assertEqual(http(gate, path, headers=cookie).status, 200)
        self.assertEqual(websocket(gate, {**cookie, "origin": ORIGIN}), "accepted")
        self.assertEqual(websocket(gate, cookie), "accepted")  # non-browser client: no Origin
        self.assertEqual(len(inner.calls), len(PROTECTED) + 2)

    def test_the_cookie_does_not_open_the_websocket_for_another_site(self):
        gate, inner = make_gate()
        cookie = signed_in(gate)
        for origin in ("http://evil.example", "null", "http://raspberrypi5-02:9999"):
            with self.subTest(origin=origin):
                self.assertEqual(websocket(gate, {**cookie, "origin": origin}), 1008)
        self.assertEqual(inner.calls, [])

    def test_forged_tampered_and_foreign_tokens_are_refused(self):
        gate, inner = make_gate()
        token = signed_in(gate)["cookie"].split("=", 1)[1]
        other_gate, _ = make_gate(secret="t" * 40)
        foreign = signed_in(other_gate)["cookie"].split("=", 1)[1]
        body, _, signature = token.rpartition(".")
        issued, expires, nonce = body.split(".")
        longer = f"{issued}.{int(expires) + 10**9}.{nonce}"
        flipped = signature[:-1] + ("B" if signature.endswith("A") else "A")
        for value in ["", ".", "a.b.c.d", "ø.ø", foreign, f"{body}.{flipped}",
                      f"{longer}.{signature}", token + "x", "x" + token]:
            with self.subTest(value=value):
                self.assertEqual(http(gate, "/", headers={"cookie": f"fjord_session={value}"}).status, 401)
        self.assertEqual(inner.calls, [])

    def test_a_session_ends_when_it_expires(self):
        clock = Clock()
        gate, _ = make_gate(clock=clock, hours=1)
        cookie = signed_in(gate)
        clock.now += 3599
        self.assertEqual(http(gate, "/", headers=cookie).status, 200)
        clock.now += 2
        self.assertEqual(http(gate, "/", headers=cookie).status, 401)
        self.assertEqual(websocket(gate, {**cookie, "origin": ORIGIN}), 1008)

    def test_the_cookie_is_found_among_others(self):
        gate, _ = make_gate()
        cookie = signed_in(gate)["cookie"]
        both = {"cookie": f"_streamlit_xsrf=abc; {cookie}; other=1"}
        self.assertEqual(http(gate, "/", headers=both).status, 200)


class LogoutTests(unittest.TestCase):
    def test_logging_out_ends_every_session(self):
        clock = Clock()
        gate, _ = make_gate(clock=clock)
        laptop, phone = signed_in(gate), signed_in(gate)
        self.assertEqual(http(gate, "/logout", headers=laptop).status, 200)  # the confirm page changes nothing
        self.assertEqual(http(gate, "/", headers=laptop).status, 200)
        response = http(gate, "/logout", "POST", headers={**laptop, "origin": ORIGIN})
        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("location"), "/login")
        self.assertIn("Max-Age=0", response.header("set-cookie"))
        self.assertEqual(http(gate, "/", headers=laptop).status, 401)
        self.assertEqual(http(gate, "/", headers=phone).status, 401)
        clock.now += 0.01
        self.assertEqual(http(gate, "/", headers=signed_in(gate)).status, 200)

    def test_a_forged_logout_changes_nothing(self):
        gate, _ = make_gate()
        cookie = signed_in(gate)
        self.assertEqual(http(gate, "/logout", "POST", headers={**cookie, "origin": "http://evil.example"}).status, 403)
        # Same origin but no session (a LAN client forging the header): no revocation.
        self.assertEqual(http(gate, "/logout", "POST", headers={"origin": ORIGIN}).status, 303)
        self.assertEqual(http(gate, "/", headers=cookie).status, 200)


class NotConfiguredTests(unittest.TestCase):
    def test_without_a_usable_hash_everything_but_health_is_503(self):
        for stored in ("", "not-a-hash"):
            with self.subTest(stored=stored):
                inner = Inner()
                with self.assertLogs(auth.LOG, "ERROR") as logs:
                    gate = auth.LoginGate(inner, password_hash=stored, session_secret=SECRET)
                self.assertIn("dashboard_login_not_configured", logs.output[0])
                for path in ("/", "/login", "/logout", "/my-money"):
                    self.assertEqual(http(gate, path).status, 503)
                self.assertEqual(login(gate).status, 503)
                self.assertEqual(websocket(gate, {"origin": ORIGIN}), 1008)
                self.assertEqual(http(gate, "/_stcore/health").status, 200)
                self.assertEqual(inner.calls, [("http", "/_stcore/health")])

    def test_an_unset_secret_still_works_and_says_restarts_log_out(self):
        with self.assertLogs(auth.LOG, "WARNING") as logs:
            gate = auth.LoginGate(Inner(), password_hash=HASH)
        self.assertIn("dashboard_session_secret_unset", "\n".join(logs.output))
        self.assertEqual(http(gate, "/", headers=signed_in(gate)).status, 200)


class SecretsStayPrivateTests(unittest.TestCase):
    def test_no_password_hash_secret_or_token_in_logs_or_pages(self):
        gate, _ = make_gate()
        with self.assertLogs(auth.LOG, "INFO") as logs:
            wrong = login(gate, password="nearly-" + PASSWORD)
            right = login(gate)
        token = session_cookie(right)
        logged = "\n".join(logs.output)
        self.assertIn("login_failed", logged)
        self.assertIn("login_ok", logged)
        for secret in (PASSWORD, "nearly-", HASH, HASH.split(":")[3], SECRET, token):
            self.assertNotIn(secret, logged)
        for page in (wrong, http(gate, "/login"), http(gate, "/", headers={"accept": "text/html"})):
            for secret in (PASSWORD, HASH, SECRET):
                self.assertNotIn(secret, page.text)


class SettingsTests(unittest.TestCase):
    def test_settings_come_from_the_environment(self):
        env = {"DASHBOARD_PASSWORD_HASH": f" {HASH} ", "DASHBOARD_SESSION_SECRET": SECRET,
               "DASHBOARD_SESSION_HOURS": "8"}
        self.assertEqual(auth.settings_from_env(env),
                         {"password_hash": HASH, "session_secret": SECRET, "session_hours": 8.0})

    def test_bad_hours_fall_back_to_twelve(self):
        for hours in ("abc", "0", "-1", "100000"):
            with self.subTest(hours=hours), self.assertLogs(auth.LOG, "WARNING"):
                self.assertEqual(auth.settings_from_env({"DASHBOARD_SESSION_HOURS": hours})["session_hours"], 12.0)
        self.assertEqual(auth.settings_from_env({})["session_hours"], 12.0)


class CommandLineTests(unittest.TestCase):
    def run_main(self, args, entries=()):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(auth.getpass, "getpass", side_effect=list(entries)), \
                redirect_stdout(out), redirect_stderr(err):
            code = auth.main(["auth.py", *args])
        return code, out.getvalue().strip(), err.getvalue()

    def test_hash_prints_a_usable_hash_and_never_the_password(self):
        code, out, _ = self.run_main(["hash"], [PASSWORD, PASSWORD])  # full strength: ~0.5 s
        self.assertEqual(code, 0)
        self.assertEqual(auth.parse_hash(out)[0], auth.ITERATIONS)
        self.assertTrue(auth.verify_password(PASSWORD, out))
        self.assertNotIn(PASSWORD, out)

    def test_hash_refuses_short_or_mismatched_passwords(self):
        self.assertEqual(self.run_main(["hash"], ["short"])[0], 1)
        self.assertEqual(self.run_main(["hash"], [PASSWORD, PASSWORD + "!"])[0], 1)

    def test_secret_and_usage(self):
        code, out, _ = self.run_main(["secret"])
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(out), 32)
        self.assertEqual(self.run_main([])[0], 2)


class DeploymentWiringTests(unittest.TestCase):
    """The pieces that put the gate in front of the dashboard in the container."""

    def read(self, *path):
        with open(os.path.join(ROOT, *path), encoding="utf-8") as handle:
            return handle.read()

    def test_cors_stays_on(self):
        with open(os.path.join(DASHBOARD, ".streamlit", "config.toml"), "rb") as handle:
            server = tomllib.load(handle).get("server", {})
        self.assertNotEqual(server.get("enableCORS", True), False)
        self.assertNotEqual(server.get("enableXsrfProtection", True), False)
        self.assertNotIn("enableAgentApi", server)

    def test_the_image_runs_serve_py_and_checks_health(self):
        dockerfile = self.read("streamlit", "Dockerfile")
        self.assertRegex(dockerfile, r'CMD \["streamlit", "run", "serve.py"\]')
        self.assertRegex(dockerfile, r"COPY [^\n]*\bauth\.py\b[^\n]*\bserve\.py\b")
        self.assertRegex(dockerfile, r"HEALTHCHECK [^\n]*\\\n[^\n]*/_stcore/health")

    def test_serve_py_wraps_app_py_in_the_gate(self):
        tree = ast.parse(self.read("streamlit", "serve.py"))
        assigned = {node.targets[0].id: node.value for node in tree.body
                    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
        app = assigned["app"]
        self.assertEqual(ast.unparse(app.func), "st.App")
        self.assertEqual(app.args[0].value, "app.py")
        middleware = next(k.value for k in app.keywords if k.arg == "middleware")
        self.assertIn("Middleware(auth.LoginGate, **auth.settings_from_env())", ast.unparse(middleware))

    def test_compose_passes_the_login_settings_to_the_dashboard_only(self):
        compose = self.read("docker-compose.yml")
        dashboard = compose[compose.index("  dashboard-streamlit:"):compose.index("\nnetworks:")]
        for name in ("DASHBOARD_PASSWORD_HASH", "DASHBOARD_SESSION_SECRET", "DASHBOARD_SESSION_HOURS"):
            self.assertIn(f"- {name}=${{{name}", dashboard)
        self.assertIn("news_bot_data:/bot-data:ro", dashboard)
        self.assertEqual(compose.count("DASHBOARD_PASSWORD_HASH="), 1)
        self.assertIn("DASHBOARD_PASSWORD_HASH=", self.read(".env.example"))


if __name__ == "__main__":
    unittest.main()
