# Dashboard security: implementation report

Report date: 2026-11-13. Branch `feat/dashboard-login`, from `main` at `a67ef63`. Follows `dashboard-security-assessment.md` (P1-4).

**Corrected the same day (section 7):** in the first version, a logout did not survive a restart. Its record was kept in memory, but the signing key comes from `.env`, so after a restart or redeploy the old cookies worked again. Logout now writes a new random "session epoch" to a small volume of the dashboard's own. Sections 1 to 6 describe the corrected version.

**Not deployed to the Pi.** Nothing on the Pi was changed: no `.env` edit, no restart, no firewall, nginx or Portainer change. A merge to `main` deploys automatically within 5 minutes. Section 6 says what must be in the Pi's `.env` first.

## 1. What changed

The dashboard now asks for a password. After login, a signed session cookie gives access to everything. Without it:

- every route except the health check answers **401**;
- the websocket that carries the data is **refused** (HTTP 403) before it opens.

| File | Change |
|---|---|
| `streamlit/auth.py` (new) | `LoginGate`: an ASGI middleware, standard library only. It also holds password hashing (PBKDF2-SHA256, 600,000 rounds), the signed session tokens and their epoch file, the failed-login limits, settings from the environment, and a CLI (`hash`, `secret`). |
| `streamlit/serve.py` (new) | `app = st.App("app.py", middleware=[Middleware(auth.LoginGate, **auth.settings_from_env())])`. Uses Streamlit's public ASGI entry point. Gives the login's log lines a handler. |
| `streamlit/Dockerfile` | Copies `auth.py` and `serve.py`; runs `streamlit run serve.py`; adds a HEALTHCHECK on `127.0.0.1:8501/_stcore/health` (urllib; the image has no curl). Creates `/app/state`, owned by `nobody`, for the epoch file. |
| `streamlit/.streamlit/config.toml` | `enableCORS = false` removed. Streamlit's default (true) checks the websocket's Origin again. |
| `streamlit/app.py` | One line: a "Log out" button in the sidebar. No page logic changed. |
| `docker-compose.yml` | The dashboard gets `DASHBOARD_PASSWORD_HASH`, `DASHBOARD_SESSION_SECRET` and `DASHBOARD_SESSION_HOURS` from the host `.env`, and a small named volume, `dashboard_state`, at `/app/state`. The port, the read-only bot-data volume, the hardening and the build are unchanged. |
| `.env.example`, `README.md` | The three settings, how to make them, and the plain-HTTP caveat. |
| `tests/test_dashboard_auth.py` (new) | 62 tests (section 4). |
| `docs/audit/dashboard-security-assessment.md` (new) | Phase 1 findings. |
| `docs/audit/img/dashboard-login.png` (new) | The login page. |

**Not changed:**

- the bot, Telegram, Slack, strategy, gates, ATR, risk, sizing and records;
- `deploy.sh`, backups and the bot's image.

The dashboard still opens the database read-only, on a read-only volume. The only place it can write is its own `dashboard_state` volume, and only the epoch file goes there.

## 2. How the login works

**Password and session**

- **Password:** only a hash is stored, in `DASHBOARD_PASSWORD_HASH`.
  - The format is `pbkdf2_sha256:<rounds>:<salt>:<hash>`. It contains no `$`, so compose passes it through unchanged (tested: `docker-compose config`).
  - `python3 streamlit/auth.py hash` asks for the password twice without echoing it, refuses fewer than 12 characters, and prints only the hash.
  - Checking a password runs in a worker thread, so a login does not pause open dashboards.
- **Session:**
  - **Token:** `epoch.issued.expires.nonce.HMAC-SHA256`, signed with `DASHBOARD_SESSION_SECRET`. Nothing is stored per session.
  - **Epoch:** a random value (32 bytes) in `/app/state/session-epoch`. A token is accepted only while the epoch it carries is the current one.
  - **Lifetime:** `DASHBOARD_SESSION_HOURS`, default 12. The expiry is inside the signed token, so changing the setting does not stretch tokens already issued.
  - **No secret set:** a random key is made at start, so a restart (every deploy) logs you out; this is logged as a warning.
- **Cookie:** `fjord_session`, with `HttpOnly; SameSite=Lax; Path=/; Max-Age=43200`.
  - `Secure` is added when the request arrives over HTTPS.
  - Over the LAN's plain HTTP a `Secure` cookie would never be sent back, so it is left off there.
- **Logout:** "Log out" in the sidebar opens a confirm page, and its POST ends **every** session, including after restarts and redeploys (section 7).
  - The POST picks a new epoch, writes it to the volume, and only then answers.
  - A logout POST from a browser without a valid session only clears that browser's cookie. A forged form therefore cannot log the owner out.
- **Restarts:** sessions survive a restart or redeploy while nobody has logged out, because the key and the epoch both survive it.

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
| The epoch file can be neither written nor removed at start (for example, the volume is read-only) | **503** for everything except the health check (fail closed), with error lines at start |
| `POST /logout`, but the new epoch could not be saved and the old one could not be removed | **500** "Logged out, but the dashboard could not save the logout…", with the cookie cleared. The logout holds until the next restart. |

- **Redirect target:** after login, `next` must be a local path. `//host`, `https://…`, backslashes, control characters, `/login` and `/logout` all become `/`.
- **Headers on the gate's own pages:** `Cache-Control: no-store`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, and a CSP of `default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'`. The pages have no JavaScript.
- **Logs:** the login logs `login_ok`, `login_failed`, `login_rate_limited`, `login_refused` and `logout`, each with the client address only. A refused websocket is logged at debug level, because a tab left open after logout keeps retrying. The epoch file logs `session_state_loaded` or `session_state_created` at start, and `session_state_invalid`, `_unreadable`, `_unwritable`, `_not_removed` or `_stuck` when something is wrong. These lines give the path and the OS error, never the epoch.

## 3. Requirements against results

| Requirement | Result |
|---|---|
| Protect all pages and sensitive endpoints | Every path except `/_stcore/health` needs a session, including `/_stcore/metrics`, `host-config`, `media`, `static`, `upload_file`, the agent routes and the websocket. Tested route by route, in the unit tests and against the built image. |
| Protect /fill | `/fill`, `/unfill` and `/deposit` are Telegram commands, already limited to the owner's private chat (assessment, section 4). The dashboard cannot write at all. Both are unchanged. |
| Established mechanisms; no unnecessary custom auth | Streamlit's own `st.login` needs an external identity provider and an HTTPS callback, which this LAN host lacks. The gate uses Streamlit's public middleware hook and standard primitives: PBKDF2, HMAC, cookie flags, Origin checks. `auth.py` is 462 lines, including the login page's HTML and CSS; no new dependencies. |
| Secrets in environment variables | The hash and session key come from `.env` through compose. Nothing is in the source or the image. |
| Secure sessions, cookie flags, CSRF | `HttpOnly`, `SameSite=Lax`, an expiry, `Secure` under HTTPS, revocation at logout that survives restarts, an Origin/Referer check on the two form posts, and an Origin check on the websocket. |
| Correct status codes | 401, 403, 405, 413, 429, 500 and 503 as tabled above, and 303 after login or logout. |
| Health checks keep working | `/_stcore/health` stays open. The new dashboard HEALTHCHECK reported `healthy` in the built image, including with the login unconfigured. The bot's heartbeat check is untouched. |
| Telegram and internal communication | Nothing calls the dashboard over HTTP (assessment, section 7). The bot's code and image are unchanged. |
| No secrets in logs or responses | Tested in the unit suite, and checked by searching the built container's log for the password, hash, secret and token: none found. |
| No new services or infrastructure | No new service: the same container and port. One small named volume holds the 44-byte epoch file. |

## 4. Tests and their results

Python 3.15.2 in a scratch virtualenv. Streamlit 1.67.0 for the dashboard checks.

| Check | Result |
|---|---|
| `python -W error::ResourceWarning -m unittest`, full suite | **1,140 tests, OK** (1,076 on `main` + 64 new; 41 in the first version, 23 added by the correction) |
| `ruff check news_bot.py newsbot tests` (CI's scope) | All checks passed |
| `ruff check streamlit/auth.py streamlit/serve.py` | All checks passed |
| `ruff check streamlit` | The same 7 findings in `app.py` as on `main` (compared line by line); none new |
| `docker build ./streamlit`, `docker build .` | Both built |

The suite also prints one "unclosed database" `ResourceWarning` from a bot test. It prints the same on the first version's commit, so it is not from this change.

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
  - A POST ends the laptop and the phone sessions, and a new login works, even in the same millisecond.
  - Neither a cross-site logout nor one without a session revokes anything.
- **Logout across restarts** (a restart is a new gate with the same hash, secret and state file):
  - Without a logout, a session survives two restarts; with another secret it does not.
  - After a logout, three devices (different addresses) are refused, over HTTP and the websocket, in the same process and after two restarts.
  - Sessions started after the logout, before or after the restart, work and survive the next restart. The old one stays refused.
  - An expired session stays expired after a restart, even with a longer `DASHBOARD_SESSION_HOURS`.
  - A refused logout (cross-site, or without a session) leaves the file unchanged, and the session survives a restart.
  - The file holds one 43-character value and a newline, with mode 0600, and none of the password, hash, secret or token. No temporary file is left behind.
- **Concurrency**
  - A session check made while the logout is still writing the file is already refused.
  - Two logouts at once, with the first write held back: the file ends with the newer epoch, the one in memory.
  - A login whose password check spans a logout gets a session of the new epoch; the old session stays refused, before and after a restart.
- **Damaged or unavailable state**
  - Nine kinds of damaged content: empty, a blank line, junk, 10,000 characters, one character short, one too many, the epoch twice, a prefix, a non-ASCII byte. Most of them still contain the old epoch, so a lenient reader would revive the session. Each time the old session is refused, the error is logged, the file is replaced with a valid epoch, and a new login works and survives the next restart.
  - A deleted or unreadable file: the old session is refused and a new login works.
  - A directory where the file should be, or a read-only folder holding the file: **503** (fail closed), health still 200.
  - No volume (missing folder, or a read-only root that refuses even to remove a missing file): it works, logout holds, and every restart starts a new epoch.
  - No state path at all: a warning, and each start is a new epoch.
  - The logout's save fails, for example on a full disk: 303, the old file is removed, and the old sessions stay refused after a restart.
  - The rename fails inside the write: no temporary file and no old epoch are left.
  - The folder sync fails after the rename: the new epoch stays on disk, with a warning.
  - The save fails and the old file cannot be removed either: **500**, which says so and says to change `DASHBOARD_SESSION_SECRET`. The cookie is cleared, the logout holds in the process, and a restart on the still read-only volume gives 503.
- **Not configured:** 503 everywhere except health, with an error logged; an unset secret still works and warns.
- **Secrets:** the password, hash, salt, secret and token appear in no log line and no page.
- **Settings and CLI:** the environment parsing and its fallbacks; `hash` (full strength, verifiable, never prints the password; refuses short or mismatched entries); `secret`.
- **Deployment wiring:**
  - CORS and XSRF stay on, and the agent API stays off.
  - The image runs `serve.py`, copies both files, has the HEALTHCHECK, and creates `/app/state` for `nobody` before `USER nobody`.
  - `serve.py` wraps `app.py` in the gate (checked by parsing it).
  - Compose gives the settings and the `dashboard_state` volume to the dashboard only, at the folder of `auth.STATE_FILE`, and keeps the read-only bot-data volume.

**Guards checked by breaking them on purpose, in a copy of the tree.** Every break below fails at least one test.

| Broken guard (added by the correction) | Tests failing |
|---|---|
| **Logout kept in memory only: the original defect** | 9 |
| The epoch in the token not checked | 23 |
| A missing file gets a fixed epoch instead of a random one | 16 |
| A file that is not an epoch is trusted | 8 |
| A file read leniently (first 43 characters / first line) | 3 / 1 |
| Stuck state does not fail closed | 3 |
| The new epoch takes effect only after the save | 1 |
| No lock around the logout (timing-based; caught in 3 of 3 runs) | 1 |
| The old epoch not removed when the save fails | 5 |
| An unsaved logout reported as saved | 1 |
| Start-up does not check that the file can be written | 19 |
| A failed folder sync undoes the save | 1 |
| A temporary file left after a failed rename | 1 |
| A missing file on a read-only root taken for stuck state | 2 |

| Broken guard (first version) | Tests failing |
|---|---|
| Health exemption widened to all of `/_stcore` | 21 |
| Websocket session not checked | 25 |
| Websocket origin not checked | 4 |
| Token signature not checked | 5 |
| Expiry ignored | 2 |
| Rate limit off | 2 |
| Login origin not checked | 6 |
| Cookie without `HttpOnly` | 1 |
| Fails open when unconfigured | 5 |
| A forged logout revokes | 2 |
| Password written to the log | 1 |
| Unauthenticated request passed to the app | 94 |

In the first version, the one break that failed no test was removing the explicit `//` check in `_safe_next`. That check is redundant: the netloc test after it already sends `//evil.example` to `/`, and the tests check that result. Removing the folder sync itself fails no test either: what it protects against is a power cut, which a unit test cannot cause.

### Against the built image

The image was run with a test hash, under the compose hardening (read-only, no capabilities), against a demo database of made-up rows. For the correction, the checks below except "No hash configured" were run again on the corrected image, started from the real `docker-compose.yml` under a throwaway project name, with the same results. Section 7.4 has the restart results.

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

**Headless Chrome 156**, with the first version's image and the same database (not rerun for the correction, which changes no page):

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
3. **A websocket that is already open.** The gate checks the cookie only when the websocket opens.
   - A tab connected before logout or expiry keeps working until it reloads or disconnects.
   - So does a script holding a stolen cookie that keeps its connection open.
   - The independent review showed this with real Streamlit: after logout the old cookie gets 401 and a new websocket gets 403, but the already-open one still reruns the app.
   - The fix would be for the gate to watch each open websocket and close it (code 1008) once its token stops being valid. That is a change to the websocket path, not to how a logout is kept, so it was left out of this correction. **It is the main follow-up.**
4. **One shared password.** There are no separate accounts. Logout ends every session.
   - A logout POST from a browser without a valid session revokes nothing. That is what stops a forged logout. But it answers with the same 303 as a real logout.
   - So someone who has lost a phone must **log in first**, then log out, to end the phone's session. The confirm page does not say this today. Changing that page was outside this correction's scope (no UI changes).
5. **Limits of the epoch file** (section 7.5):
   - A logout that could not be saved, and whose old epoch could not be removed (a read-only or failing disk), answers **500**. If the disk is writable again by the next start, the old sessions come back until they expire (12 hours at most). The 500 page says to change `DASHBOARD_SESSION_SECRET`, which ends them for certain.
   - Anyone who can write the Docker volume, for example by restoring a backup or an SD-card image, can put back an older epoch. Sessions logged out since then would return. Such a person can also read the secret from the container, so this adds no new risk.
   - A power cut in the few milliseconds before the logout's rename reaches the disk can keep the old epoch. The logout's answer is sent only after the rename and its sync, so the user would not have seen the logout confirmed.
   - Expiry still depends on the Pi's clock. Setting the clock back makes expired tokens valid again until it catches up. A logout no longer depends on the clock.
   - One dashboard process per volume is assumed, as in the compose file (a fixed container name). A second process sharing the volume would keep its own epoch in memory.
6. **Per-address limits depend on seeing the real address.**
   - On Docker Desktop every client appears as the Docker gateway.
   - On the Pi, Linux's NAT for published ports is expected to keep the LAN address. This was not verified, because nothing was deployed.
   - If it does not, the per-address limit acts as one shared limit: stricter, with a risk of locking out the owner for 15 minutes.
7. **Not changed, because they are host changes for the owner:**
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
2. Merge. The deploy rebuilds the dashboard with the settings. Compose creates the `dashboard_state` volume by itself; it needs nothing in `.env`.
3. **Check:**
   - `http://raspberrypi5-02:8002` shows the login, and the password opens the dashboard;
   - `docker inspect -f '{{.State.Health.Status}}' news_bot_dashboard_streamlit` reports `healthy`;
   - `docker logs news_bot_dashboard_streamlit` shows `session_state_created` once, then `login_ok`, and no `session_state_` error.

**If the merge comes first:** the dashboard answers 503 until the two lines are added and `docker compose up -d dashboard-streamlit` is run. The bot is unaffected.

**Rollback:** `git revert` the merge commit, and the timer deploys the previous dashboard. No data, schema or bot behaviour is involved.

- The two `.env` lines can stay; the old dashboard ignores them.
- The old dashboard does not use the `dashboard_state` volume either. It can be left alone, or removed with `docker volume rm <project>_dashboard_state`; removing it only logs everyone out.

## 7. Correction: a logout now survives restarts

### 7.1 The defect

In the first version, `Sessions.revoke_all()` recorded a logout as an in-memory time: tokens issued before it were refused. The tokens are signed with `DASHBOARD_SESSION_SECRET`, which comes from `.env` and is the same after every restart. The recorded time was not, so after a restart:

1. You log in and get a cookie.
2. You log out. Every session is refused.
3. The dashboard restarts: a redeploy, which happens on every merge, a Pi reboot, or a crash.
4. The record of the logout is gone, while the key is unchanged.
5. Every cookie issued before the logout, and not yet expired, works again.

Reproduced before the fix, in the unit harness and on the first version's built image:

| Old cookie, after… | First version | Corrected |
|---|---|---|
| logout | 401, websocket 403 | 401, websocket 403 |
| logout and `docker restart` | **200, websocket 101 (open)** | 401, websocket 403 |

### 7.2 What logout now does

- **Epoch:** every token carries the epoch it was issued in, a random 32-byte value. It is part of the signed text, so it cannot be changed without the key: `epoch.issued.expires.nonce.signature`.
- **Check:** a token is valid only while its epoch is the current one, and it has not expired.
- **Logout** picks a new random epoch and writes it to `/app/state/session-epoch`, then answers.
  - The write goes to a temporary file in the same folder, is synced, and then renamed over the old file, and the folder is synced. After a crash the file holds either the old epoch or the new one, never half of one.
- **Start-up** reads the file. A valid epoch means the sessions issued before the restart still work, unless there was a logout.
- **The volume:** `/app/state` is a small named volume, `dashboard_state`, mounted only into the dashboard. The image creates the folder for `nobody`, and a new volume takes that owner. So the container can write it with its root filesystem read-only and every capability dropped.
- **No new setting:** the path is fixed by the compose mount.

**Not stored:** the file holds the epoch alone, 43 characters and a newline, mode 0600. It holds no password, hash, secret or token, and no per-session data. Knowing the epoch does not help forge a token, since that still needs the secret.

### 7.3 Why it survives restarts, and why it fails safe

- **After a logout:** a restart, recreate or rebuild reads the new epoch back. Tokens from the old epoch match neither the epoch in memory nor the one on disk, at any time and with any clock.
- **Clock:** revocation no longer depends on it. A logout recorded as a time could miss tokens issued "later" by a clock that was ahead, for example while a Pi without a real-time clock catches up after boot. Expiry still uses the clock.
- **Lost state means a new epoch,** never an old one. Each failure below can sign everyone out, but cannot bring a session back:

| The state file is… | Effect |
|---|---|
| fine | Its epoch is used, and written back to prove the folder is writable |
| missing (first start, or deleted) | A new random epoch, saved. Every session before it ends. |
| unreadable, not ASCII, or not exactly one epoch | Error logged; a new epoch replaces the file. Every session before it ends. |
| impossible to write, and nothing on disk (no volume) | Runs on an epoch in memory, with an error logged. Every restart ends every session. |
| impossible to write or remove (for example, a read-only volume) | **Fails closed: 503** with an error logged, because a logout could not be kept |
| not saved during a logout, but the old epoch could be removed | The logout holds, and the next start makes a new epoch. 303. |
| not saved during a logout, and the old epoch could not be removed | The logout holds until a restart. **500**, saying so; section 5, item 5. |

- **Concurrency:**
  - The logout sets the new epoch in memory before it touches the file. Any check from then on refuses the old tokens.
  - The save runs on the event loop and blocks it for one small sync, so no request is handled between the new epoch and its write. The answer goes out only after the save.
  - A lock covers the change and the write. Two logouts at once, even from threads, cannot leave an older epoch on disk than in memory.
  - A login that finishes after a logout gets the new epoch. The password was right after the logout, so that is a new session, not an old one.
- **Tokens:** the format gained the epoch field. The signing (HMAC-SHA256 with `DASHBOARD_SESSION_SECRET`) and the password hashing are unchanged.

**Considered and not chosen:**

- **Saving the logout time instead of an epoch.** It is smaller in the code, but revocation would still depend on the clock. A missing file would also have needed special handling to fail safe, which a random epoch gets by default.
- **Writing to the bot's data volume.** The dashboard mounts that volume read-only, so it cannot change trades or records. Making it writable to save 44 bytes would trade that away.
- **Ending every session at every restart.** It needs no file, but logs the owner out on every merge, since a merge deploys. It is kept as the fallback when there is no volume.

### 7.4 Verification of the correction

- **Unit tests:**
  - 64 in `tests/test_dashboard_auth.py`, 23 of them new (section 4), and the full suite: 1,140, OK.
  - Ruff clean.
  - All 27 deliberate breaks (15 for the correction, 12 from the first version) fail tests. That includes putting back the original in-memory logout, which fails 9.
- **Built image, from the real `docker-compose.yml`** under a throwaway project name. The image was built by compose, with the compose hardening (read-only root, `nobody`, no capabilities):

| Step | Session A (logged out) / B (other device) | Websocket |
|---|---|---|
| Before logout; after `docker restart`; after recreate | 200 / 200 | 101 |
| After logout | 401 / 401 | 403 |
| After logout and `docker restart` / `up -d --force-recreate` / `up -d --build --force-recreate` / stop and start | 401 / 401 each time | 403 |
| A new login C after all that; C after another restart | 200; 200 | 101 |
| The state file was overwritten with junk, then a restart: C | 401; error logged; the file replaced with a valid epoch owned by `nobody`, mode 0600 | 403 |
| The file was deleted, then a restart: the session from before | 401 | 403 |
| The volume mounted read-only | 503 for pages and login, health 200, error lines | 403 |
| No volume at all (the image run without compose) | Login works; logout holds; a restart ends every session | 101, then 403 |

  - **The new volume:** owned by `nobody` (65534), as the image's folder is. The root filesystem stays read-only, and the gate is built once per process (one start-up line).
  - **Container log:** the password, hash, secret, every token and every epoch searched for: **0** found. Health check **healthy**.
  - **The earlier matrix** (section 4) gave the same results on the corrected image.
- **Removed afterwards:** the test project's containers, volumes and images.

### 7.5 Independent challenge

A separate review was given the code and asked only to make an old session valid after a logout and a simulated restart. It reviewed the code without seeing these results.

**Held:**

- 1 to 3 restarts after a logout, over HTTP and the websocket.
- An OS error at each step of the save (temporary file, write, sync, rename, folder open, folder sync).
- A crash after the rename.
- 8 threads × 40 logouts racing session checks: no old session accepted.
- A login racing a logout.
- Replayed logouts.
- The clock moved forwards and backwards between login, logout and restart.
- 1,296 tampered tokens, including a logged-out token with the current epoch swapped in.
- Two gates in one process: not possible. Streamlit imports `serve.py` once and Starlette builds the middleware once.

**Found and fixed in this correction:**

- A failed folder sync, after the new epoch was already in place, removed the file it had just written. That logged everyone out at the next restart; now it logs a warning instead.
- On a read-only root without the volume, Linux refuses even to remove a missing file. That was taken for stuck state and gave 503; it now runs on an epoch in memory. Found by the container test.
- The 500 page now says how to end the old sessions for certain.
- The damaged-file tests now include damage that keeps the old epoch within reach, and the write is now broken inside the function in the tests, not only mocked out as a whole.

**Found and not fixed** (section 5): the open websocket (item 3), the silent logout without a session (item 4), and an unsaved logout followed by a recovered disk (item 5). Also noted: the review's process killed after the temporary file was written, but before the rename, kept the old epoch. That is expected, because no logout answer had been sent yet. A FIFO placed at the state path would hang the start-up; doing that needs write access to the volume.
