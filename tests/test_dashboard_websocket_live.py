"""The real dashboard: `streamlit run serve.py`, a browser-like websocket client.

The gate's own tests (test_dashboard_auth.py) drive it with hand-made ASGI
messages. These run the whole stack instead (uvicorn, Starlette, Streamlit's
websocket handler, the gate in front) and speak Streamlit's own protocol, to
check that an open dashboard stops receiving anything once its session is over.

They need Streamlit, uvicorn and websockets, which only the dashboard image
has, so the bot's test job skips them and CI runs them inside that image.
"""

import asyncio
import http.client
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "streamlit")
PASSWORD = "correct horse battery"
SESSION_SECONDS = 12.0


def _have_the_stack():
    # The repo's streamlit/ folder is importable as a namespace package, so ask
    # for a module only the real Streamlit has.
    try:
        return all(importlib.util.find_spec(name) for name in
                   ("streamlit.web.server.starlette", "uvicorn", "websockets"))
    except ModuleNotFoundError:
        return False


HAVE_STACK = _have_the_stack()
if HAVE_STACK:
    from streamlit.proto.BackMsg_pb2 import BackMsg
    from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _password_hash():
    sys.path.insert(0, DASHBOARD)
    try:
        import auth
    finally:
        sys.path.remove(DASHBOARD)
    return auth.hash_password(PASSWORD, iterations=1000)


def rerun():
    """What the browser sends to run the page: the dashboard answers with data."""
    message = BackMsg()
    message.rerun_script.query_string = ""
    return message.SerializeToString()


@unittest.skipUnless(HAVE_STACK, "needs Streamlit, uvicorn and websockets (the dashboard image)")
class LiveDashboardWebsocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.home = tempfile.TemporaryDirectory()
        env = {**os.environ, "HOME": cls.home.name, "DASHBOARD_PASSWORD_HASH": _password_hash(),
               "DASHBOARD_SESSION_SECRET": "s" * 40,
               "DASHBOARD_SESSION_HOURS": str(SESSION_SECONDS / 3600),
               "DASHBOARD_DB_FILE": os.path.join(cls.home.name, "no.db")}
        cls.log = open(os.path.join(cls.home.name, "server.log"), "w+")
        cls.server = subprocess.Popen(  # noqa: S603 - a fixed command: this interpreter running Streamlit
            [sys.executable, "-m", "streamlit", "run", "serve.py", "--server.port", str(cls.port),
             "--server.address", "127.0.0.1", "--server.headless", "true"],
            cwd=DASHBOARD, env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        while cls.request("GET", "/_stcore/health")[0] != 200:
            if time.monotonic() > deadline or cls.server.poll() is not None:
                cls.tearDownClass()
                raise RuntimeError("the dashboard did not start")
            time.sleep(0.25)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(10)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait()
        cls.log.close()
        cls.home.cleanup()

    @classmethod
    def request(cls, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}
        except OSError:
            return None, {}
        finally:
            connection.close()

    def login(self):
        status, headers = self.request(
            "POST", "/login", {"Origin": self.origin, "Content-Type": "application/x-www-form-urlencoded"},
            urllib.parse.urlencode({"password": PASSWORD, "next": "/"}))
        self.assertEqual(status, 303)
        return headers["set-cookie"].split(";")[0]

    def logout(self, cookie):
        status, _ = self.request("POST", "/logout", {"Origin": self.origin, "Cookie": cookie})
        self.assertEqual(status, 303)

    def open(self, cookie):
        return connect(f"ws://127.0.0.1:{self.port}/_stcore/stream", origin=None, subprotocols=["streamlit"],
                       additional_headers={"Origin": self.origin, "Cookie": cookie})

    async def run_page(self, ws):
        """Rerun the page and read until it finishes: the dashboard's data."""
        await ws.send(rerun())
        kinds = []
        while "script_finished" not in kinds:
            kinds.append(ForwardMsg.FromString(await asyncio.wait_for(ws.recv(), 20)).WhichOneof("type"))
        await asyncio.sleep(0.5)  # anything still on its way belongs to this run
        while True:
            try:
                kinds.append(ForwardMsg.FromString(await asyncio.wait_for(ws.recv(), 0.2)).WhichOneof("type"))
            except TimeoutError:
                return kinds

    async def until_closed(self, ws, seconds):
        """(messages received, close code) until the server closes the socket,
        or 'still open' when it is quiet for `seconds` without closing."""
        received = []
        try:
            while True:
                received.append(await asyncio.wait_for(ws.recv(), seconds))
        except ConnectionClosed as closed:
            return received, closed.rcvd.code if closed.rcvd else None
        except TimeoutError:
            return received, "still open"

    def assertLogClean(self, *cookies):
        """The server's log names no session token and shows no traceback."""
        self.log.flush()
        with open(self.log.name, encoding="utf-8", errors="replace") as handle:
            logged = handle.read()
        self.assertNotIn("Traceback", logged)
        for cookie in cookies:
            self.assertNotIn(cookie.split("=", 1)[1], logged)

    async def try_to_read_after(self, ws):
        """Ask for the page again on a socket whose session is over."""
        try:
            await ws.send(rerun())
        except ConnectionClosed:
            pass
        return await self.until_closed(ws, 5)

    def test_logout_closes_every_open_dashboard_and_sends_nothing_more(self):
        async def scenario():
            laptop, phone = self.login(), self.login()
            async with self.open(laptop) as laptop_ws, self.open(phone) as phone_ws:
                self.assertIn("script_finished", await self.run_page(laptop_ws))
                self.assertIn("script_finished", await self.run_page(phone_ws))
                self.logout(laptop)
                for ws in (laptop_ws, phone_ws):
                    self.assertEqual(await self.until_closed(ws, 5), ([], 1008))
                    self.assertEqual(await self.try_to_read_after(ws), ([], 1008))
            for cookie in (laptop, phone):
                with self.assertRaises(InvalidStatus):
                    async with self.open(cookie):
                        pass
            fresh_cookie = self.login()
            async with self.open(fresh_cookie) as fresh:  # signing in again works
                self.assertIn("script_finished", await self.run_page(fresh))
            self.assertLogClean(laptop, phone, fresh_cookie)

        asyncio.run(scenario())

    def test_an_idle_dashboard_closes_when_its_session_expires(self):
        async def scenario():
            cookie = self.login()
            started = time.monotonic()
            async with self.open(cookie) as ws:
                self.assertIn("script_finished", await self.run_page(ws))
                # Nothing more from either side: the gate has to notice the expiry itself.
                received, code = await self.until_closed(ws, SESSION_SECONDS + 10)
                elapsed = time.monotonic() - started
                self.assertEqual((received, code), ([], 1008))
                self.assertGreater(elapsed, SESSION_SECONDS - 2)
                self.assertLess(elapsed, SESSION_SECONDS + 5)
                self.assertEqual(await self.try_to_read_after(ws), ([], 1008))
            self.assertLogClean(cookie)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
