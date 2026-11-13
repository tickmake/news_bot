# Dashboard security: implementation report

Report date: 2026-11-13. Branch `feat/dashboard-login`, from `main` at `a67ef63`. Follows `dashboard-security-assessment.md` (P1-4).

**Not deployed to the Pi.** Nothing on the Pi was changed: no `.env` edit, no restart, no firewall, nginx or Portainer change. A merge to `main` deploys automatically within 5 minutes. Section 6 says what must be in the Pi's `.env` first.

## 1. What changed

The dashboard now asks for a password. After login, a signed session cookie gives access to everything. Without it:

- every route except the health check answers **401**;
- the websocket that carries the data is **refused** (HTTP 403) before it opens.

| File | Change |
|---|---|
| `streamlit/auth.py` (new) | `LoginGate`: an ASGI middleware, standard library only. It also holds password hashing (PBKDF2-SHA256, 600,000 rounds), the signed session tokens, the failed-login limits, settings from the environment, and a CLI (`hash`, `secret`). |
| `streamlit/serve.py` (new) | `app = st.App("app.py", middleware=[Middleware(auth.LoginGate, **auth.settings_from_env())])`. Uses Streamlit's public ASGI entry point. Gives the login's log lines a handler. |
| `streamlit/Dockerfile` | Copies `auth.py` and `serve.py`; runs `streamlit run serve.py`; adds a HEALTHCHECK on `127.0.0.1:8501/_stcore/health` (urllib; the image has no curl). |
| `streamlit/.streamlit/config.toml` | `enableCORS = false` removed. Streamlit's default (true) checks the websocket's Origin again. |
| `streamlit/app.py` | One line: a "Log out" button in the sidebar. No page logic changed. |
| `docker-compose.yml` | The dashboard gets `DASHBOARD_PASSWORD_HASH`, `DASHBOARD_SESSION_SECRET` and `DASHBOARD_SESSION_HOURS` from the host `.env`. The port, volume, hardening and build are unchanged. |
| `.env.example`, `README.md` | The three settings, how to make them, and the plain-HTTP caveat. |
| `tests/test_dashboard_auth.py` (new) | 41 tests (section 4). |
| `docs/audit/dashboard-security-assessment.md` (new) | Phase 1 findings. |
| `docs/audit/img/dashboard-login.png` (new) | The login page. |

**Not changed:**

- the bot, Telegram, Slack, strategy, gates, ATR, risk, sizing and records;
- `deploy.sh`, backups and the bot's image.

The dashboard still opens the database read-only, on a read-only volume.

## 2. How the login works

**Password and session**

- **Password:** only a hash is stored, in `DASHBOARD_PASSWORD_HASH`.
  - The format is `pbkdf2_sha256:<rounds>:<salt>:<hash>`. It contains no `$`, so compose passes it through unchanged (tested: `docker-compose config`).
  - `python3 streamlit/auth.py hash` asks for the password twice without echoing it, refuses fewer than 12 characters, and prints only the hash.
  - Checking a password runs in a worker thread, so a login does not pause open dashboards.
- **Session:**
  - **Token:** `issued.expires.nonce.HMAC-SHA256`, signed with `DASHBOARD_SESSION_SECRET`. Nothing is stored per session.
  - **Lifetime:** `DASHBOARD_SESSION_HOURS`, default 12.
  - **No secret set:** a random key is made at start, so a restart (every deploy) logs you out; this is logged as a warning.
- **Cookie:** `fjord_session`, with `HttpOnly; SameSite=Lax; Path=/; Max-Age=43200`.
  - `Secure` is added when the request arrives over HTTPS.
  - Over the LAN's plain HTTP a `Secure` cookie would never be sent back, so it is left off there.
- **Logout:** "Log out" in the sidebar opens a confirm page, and its POST ends **every** session.
  - A logout POST from a browser without a valid session only clears that browser's cookie. A forged form therefore cannot log the owner out.

**Responses**

| Request | Answer |
|---|---|
| `GET /_stcore/health` | passed to Streamlit: `200 ok`, for the Docker health check |
| Any other path, no valid session | **401**. A browser page request gets the login form as the body; everything else gets plain text. |
| Websocket, no valid session, or `Origin` from another site (or `null`) | refused before accept → **403** |
| `GET /login` | 200, the form |
| `POST /login`, right password | **303** to the page asked for, with `Set-Cookie` |
| `POST /login`, wrong password | **401**, form again |
| `POST /login` or `/logout` from another site, or with neither `Origin` nor `Referer` | **403** (CSRF check) |
| `POST /login`, body over 4 KB / unreadable form | **413** / **400** |
| 5 failures from one address, or 50 from all, within 15 minutes | **429** with `Retry-After`, even for the right password, until the window passes |
| No usable hash configured | **503** for everything except the health check (fail closed), with one error line at start |

- **Redirect target:** after login, `next` must be a local path. `//host`, `https://…`, backslashes, control characters, `/login` and `/logout` all become `/`.
- **Headers on the gate's own pages:** `Cache-Control: no-store`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, and a CSP of `default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'`. The pages have no JavaScript.
- **Logs:** the login logs `login_ok`, `login_failed`, `login_rate_limited`, `login_refused` and `logout`, each with the client address only. A refused websocket is logged at debug level, because a tab left open after logout keeps retrying.

## 3. Requirements against results

| Requirement | Result |
|---|---|
| Protect all pages and sensitive endpoints | Every path except `/_stcore/health` needs a session, including `/_stcore/metrics`, `host-config`, `media`, `static`, `upload_file`, the agent routes and the websocket. Tested route by route, in the unit tests and against the built image. |
| Protect /fill | `/fill`, `/unfill` and `/deposit` are Telegram commands, already limited to the owner's private chat (assessment, section 4). The dashboard cannot write at all. Both are unchanged. |
| Established mechanisms; no unnecessary custom auth | Streamlit's own `st.login` needs an external identity provider and an HTTPS callback, which this LAN host lacks. The gate uses Streamlit's public middleware hook and standard primitives: PBKDF2, HMAC, cookie flags, Origin checks. `auth.py` is 462 lines, including the login page's HTML and CSS; no new dependencies. |
| Secrets in environment variables | The hash and session key come from `.env` through compose. Nothing is in the source or the image. |
| Secure sessions, cookie flags, CSRF | `HttpOnly`, `SameSite=Lax`, an expiry, `Secure` under HTTPS, revocation at logout, an Origin/Referer check on the two form posts, and an Origin check on the websocket. |
| Correct status codes | 401, 403, 405, 413, 429 and 503 as tabled above, and 303 after login or logout. |
| Health checks keep working | `/_stcore/health` stays open. The new dashboard HEALTHCHECK reported `healthy` in the built image, including with the login unconfigured. The bot's heartbeat check is untouched. |
| Telegram and internal communication | Nothing calls the dashboard over HTTP (assessment, section 7). The bot's code and image are unchanged. |
| No secrets in logs or responses | Tested in the unit suite, and checked by searching the built container's log for the password, hash, secret and token: none found. |
| No new services or infrastructure | None: the same container, port and volume. |

## 4. Tests and their results

Python 3.15.2 in a scratch virtualenv. Streamlit 1.67.0 for the dashboard checks.

| Check | Result |
|---|---|
| `python -W error::ResourceWarning -m unittest`, full suite | **1,117 tests, OK** (1,076 on `main` + 41 new) |
| `ruff check news_bot.py newsbot tests` (CI's scope) | All checks passed |
| `ruff check streamlit/auth.py streamlit/serve.py` | All checks passed |
| `ruff check streamlit` | The same 7 findings in `app.py` as on `main` (compared line by line); none new |
| `docker build ./streamlit`, `docker build .` | Both built |

**What the new tests cover** (`tests/test_dashboard_auth.py`). They drive the middleware with raw ASGI messages, so CI needs no new packages.

- **Passwords**
  - A hash round-trips.
  - A wrong password, ten malformed hashes and an empty hash are all refused.
  - The format contains no `$`.
  - Defaults: 600,000 rounds.
- **Access without a session**
  - 14 paths × 4 methods each get 401, and the app behind the gate is never called.
  - A browser gets the form, with `next`.
  - The websocket is refused, with or without an Origin.
  - Health and lifespan pass through.
- **Login**
  - The form page and its headers.
  - Cookie flags; `Secure` only under HTTPS.
  - A wrong password gets 401 and no cookie.
  - A cross-site, `null` or missing Origin gets 403, and the password is never checked.
  - A same-site Referer is accepted when Origin is missing.
  - 413, 400 and 405 cases.
  - 11 `next` cases.
- **Limits**
  - 5 failures lock an address; another device can still log in; the lock lifts after the window.
  - A success clears the count.
  - 50 failures across addresses lock everyone.
- **Sessions**
  - A session opens every route and the websocket.
  - A foreign Origin is refused even with the cookie.
  - Tokens that are forged, tampered with, extended, signed with another key or not ASCII are refused.
  - Expiry works to the second.
  - The cookie is found among other cookies.
- **Logout**
  - The confirm page changes nothing.
  - A POST ends the laptop and the phone sessions, and a new login works.
  - Neither a cross-site logout nor one without a session revokes anything.
- **Not configured:** 503 everywhere except health, with an error logged; an unset secret still works and warns.
- **Secrets:** the password, hash, salt, secret and token appear in no log line and no page.
- **Settings and CLI:** the environment parsing and its fallbacks; `hash` (full strength, verifiable, never prints the password; refuses short or mismatched entries); `secret`.
- **Deployment wiring:** CORS and XSRF stay on and the agent API stays off; the image runs `serve.py`, copies both files and has the HEALTHCHECK; `serve.py` wraps `app.py` in the gate (checked by parsing it); compose gives the settings to the dashboard and keeps the read-only volume.

**Guards checked by breaking them on purpose, in a copy of the tree:**

| Broken guard | Tests failing |
|---|---|
| Health exemption widened to all of `/_stcore` | 21 |
| Websocket session not checked | 2 |
| Websocket origin not checked | 4 |
| Token signature not checked | 5 |
| Expiry ignored | 1 |
| Rate limit off | 2 |
| Login origin not checked | 5 |
| Cookie without `HttpOnly` | 1 |
| Fails open when unconfigured | 2 |
| A forged logout revokes | 1 |
| Password written to the log | 1 |
| Unauthenticated request passed to the app | 69 |

The one break that failed no test was removing the explicit `//` check in `_safe_next`. That check is redundant: the netloc test after it already sends `//evil.example` to `/`, and the tests check that result.

### Against the built image

The image was run with a test hash, under the compose hardening (read-only, no capabilities), against a demo database of made-up rows.

| Check | Result |
|---|---|
| `/`, `/my-money`, `/_stcore/metrics`, `/_stcore/host-config`, `/static/x.js`, `/media/x`, `/_stcore/agent/openapi.json` without a session | all **401** |
| `/_stcore/health` without a session | **200** |
| Login: wrong password / cross-origin / no Origin / right password | **401 / 403 / 403 / 303** to `/my-money`, with `HttpOnly; SameSite=Lax; Max-Age=43200` |
| The same routes with the session | **200** |
| Websocket: no cookie / cookie + `Origin: http://evil.example` / bad cookie / cookie + same origin | **403 / 403 / 403 / 101** |
| Logout: confirm page / POST / old cookie afterwards | **200 / 303 → /login / 401** |
| Six wrong passwords from one address | 401 ×5, then **429**, `Retry-After: 900`; the right password then also gets 429 |
| No hash configured | `/`, `/login`, `/my-money` **503**; health **200** |
| `docker inspect` health | **healthy** |
| Container log | Only the login lines and Streamlit's start-up; the password, hash, secret and token each found **0** times. The `enableCORS` warning that the live Pi prints is gone. |

**Headless Chrome 156** with the same image and database:

- Before login, `/my-money` showed the form.
- After login it landed on "My money".
- The cookie read back as `httpOnly=True, sameSite=Lax, secure=False`.
- All 11 pages loaded with the sidebar's "Log out".
- After logout, `/my-money` showed the form again.

10 of the 11 pages rendered without an exception. "Data collected" raised a Plotly "wide-form data with columns of different type" error. The same error appears when **`main`'s** `app.py` runs against that demo database, without the login, so it comes from the made-up data, not from this change. (The first runs also failed on tables missing from the demo database; those were added.)

**Streamlit AppTest**, Sent picks page: no exceptions; the "Risk when sent" table has 5 rows, 3 recorded; "Log out" is in the sidebar.

## 5. What this does not protect against

1. **Traffic capture on the LAN.** The dashboard is still plain HTTP.
   - The password crosses the network once per login, and the session cookie with every request.
   - Someone who can capture traffic on the Wi-Fi could replay the cookie until it expires.
   - The fix is TLS: Streamlit's `server.sslCertFile`/`sslKeyFile` with a certificate the browsers trust, or reaching the dashboard over the VPN only. That needs a certificate decision and is not done here.
2. **Anyone with Docker or Portainer access on the Pi.** They can read the container environment and the database directly.
   - The bot's `env_file: .env` also loads the two dashboard settings into the bot container, as it does every other `.env` key.
   - Portainer is published on `0.0.0.0:9443/8000` and controls Docker. It is the bigger host risk, and outside this repository.
3. **A tab that is already open.** A tab connected before logout or expiry keeps its websocket until it reloads.
4. **One shared password.** There are no separate accounts. Logout ends every session.
5. **Per-address limits depend on seeing the real address.**
   - On Docker Desktop every client appears as the Docker gateway.
   - On the Pi, Linux's NAT for published ports is expected to keep the LAN address. This was not verified, because nothing was deployed.
   - If it does not, the per-address limit acts as one shared limit: stricter, with a risk of locking out the owner for 15 minutes.
6. **Not changed, because they are host changes for the owner:**
   - Docker ports bypass ufw.
   - The port is still published on every interface (`DASHBOARD_STREAMLIT_PORT=<ip>:8002` would narrow it).
   - Internet reachability was checked from the LAN only.

## 6. Deployment and rollback

Not deployed. **Merge = deploy** (the Pi's timer runs `deploy.sh` every 5 minutes).

1. **Before merging**, on any machine:
   ```bash
   python3 streamlit/auth.py hash      # type the password twice
   python3 streamlit/auth.py secret
   ```
   On the Pi, add the two lines to `~/news_bot/.env`:
   ```
   DASHBOARD_PASSWORD_HASH=pbkdf2_sha256:600000:...
   DASHBOARD_SESSION_SECRET=...
   ```
2. Merge. The deploy rebuilds the dashboard with the settings.
3. **Check:**
   - `http://raspberrypi5-02:8002` shows the login, and the password opens the dashboard;
   - `docker inspect -f '{{.State.Health.Status}}' news_bot_dashboard_streamlit` reports `healthy`;
   - `docker logs news_bot_dashboard_streamlit` shows `login_ok`.

**If the merge comes first:** the dashboard answers 503 until the two lines are added and `docker compose up -d dashboard-streamlit` is run. The bot is unaffected.

**Rollback:** `git revert` the merge commit, and the timer deploys the previous dashboard. No data, schema or bot behaviour is involved. The two `.env` lines can stay; the old dashboard ignores them.
