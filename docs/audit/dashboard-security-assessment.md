# Dashboard security: assessment

Assessment date: 2026-11-13. Scope: the Fjord Signals dashboard (`streamlit/`, service `dashboard-streamlit`) and every way into the bot's records. Follows up finding 9 of `independent-audit.md` and roadmap item P1-4.

Method: the repository at `origin/main` (`a67ef63`), the installed Streamlit 1.67.0 source, and **read-only** commands on the Pi (`docker ps`, `docker inspect`, `ss`, `ip`, `ufw status`, `iptables -S`, the nginx configuration). Nothing on the Pi was changed. No secret values were printed; for the Telegram chat ids only the sign was checked.

## 1. Summary

- The dashboard has **no authentication**. Anyone who can reach port 8002 sees the real trades, holdings, deposits, sent picks and risk records.
- Port 8002 is published on every interface of the Pi, and **ufw does not apply to it** (Docker-published ports bypass the INPUT chain). It is reachable from both of the Pi's networks: a /22 Wi-Fi network and the 10.1.1.0/24 network.
- It is **probably not reachable from the internet** today. The router answers only 443 from outside, as tested by hairpin NAT. No global IPv6 address is configured. This was not confirmed from outside the network.
- `enableCORS = false` in `streamlit/.streamlit/config.toml` makes Streamlit accept a **websocket from any origin**. So any web page opened on a device on those networks can read the dashboard, through a cross-site websocket or DNS rebinding, without the network itself being exposed.
- **Nothing on the dashboard can write.** The database is opened read-only twice over. `/fill`, `/unfill` and `/deposit` are Telegram commands, already limited to the owner's private chat.
- No part of the system calls the dashboard over HTTP. Adding a login cannot break Telegram, the bot, deploys or backups.

Severity stays **P1**: private financial data, readable without credentials from a shared network and from web pages.

## 2. Current exposure

| Item | Finding | Evidence |
|---|---|---|
| Container | `news_bot_dashboard_streamlit`, Streamlit 1.67.0 on port 8501, as user `nobody`, read-only root filesystem, all capabilities dropped | `streamlit/Dockerfile`, compose |
| Port mapping | `"${DASHBOARD_STREAMLIT_PORT:-8002}:8501"` with no host IP. Docker binds it to `0.0.0.0:8002` and `[::]:8002`. The Pi's `.env` sets no `DASHBOARD_*` value. | `docker ps`, `docker inspect` → `{"HostIp":"","HostPort":"8002"}`, `ss -tln` |
| Interfaces | `wlan0` 192.168.6.22/22 (1,024 addresses; the Mac reaching it was on 192.168.4.x); `eth0` 10.1.1.5/24 (the ssh config's "VPN fallback" address); IPv6: a ULA `fd0f:…/64` and link-local addresses only | `ip -br addr` |
| Host firewall | ufw is active with INPUT policy DROP. Docker rewrites published ports in PREROUTING and filters them in FORWARD, so the INPUT rules never see them. The `DOCKER-USER` chain, where such filtering would go, is empty for IPv4 and IPv6. | `ufw status`, `iptables -S INPUT`, `iptables -S DOCKER-USER`, `ip6tables -S DOCKER-USER` |
| Reverse proxy | Host nginx terminates TLS for weekendtours.org, simplitgroup.com, lumina.simplitgroup.com and posterbot.simplitgroup.com. It proxies to 9280, 9180, 2052 and 8080. **No server block points at 8002.** | `/etc/nginx/sites-enabled`, `/etc/nginx/conf.d` |
| Internet | Outbound traffic leaves from 212.89.110.3; the sites resolve to 212.89.110.2. From the LAN, both addresses answer on 443, while **8002, 8080, 9443 and 22 are closed**. | `nc -z`, `curl` to both public addresses |
| Transport | Plain HTTP. No TLS on 8002. | |
| Cross-origin policy | `enableCORS = false`. Streamlit 1.67 then sends `Access-Control-Allow-Origin: *` and accepts a websocket from any `Origin`. The container log on the Pi prints Streamlit's own warning that this "does not stop a cross-origin WebSocket connection". | `config.toml`, `streamlit/config.py`, `docker logs news_bot_dashboard_streamlit` |

**What is not verified:**

- Inbound reachability from outside the home network. The hairpin test goes through the router's NAT, but it is not the same as a test from a mobile network.
- Who else is on the two networks.
- Whether the router filters IPv6. That matters only if a global IPv6 address appears again; the audit saw one, and there is none today.

## 3. Endpoint inventory and risk classification

### Dashboard (`raspberrypi5-02:8002`)

The pages themselves are only the Streamlit shell. Every number is sent over the websocket after the page loads.

| Route | Method | What it serves | Changes state? | Access today | Risk |
|---|---|---|---|---|---|
| `/` and 10 pages (`latest-screen`, `day-by-day`, `sent-picks`, `one-stock`, `data-collected`, `my-money`, `what-if`, `prices-kept`, `company-numbers`, `money-put-in`) | GET | Streamlit index HTML | no | open | low on its own |
| `/_stcore/stream` | WebSocket | All page content: real trades and fees (`real_trades`), deposits, holdings and results, sent picks with their risk records, screens, forward test | no: widget changes rerun read-only queries | open, **any origin** | **high** (confidentiality) |
| `/_stcore/health` | GET | `ok` | no | open | none: keep open for health checks |
| `/_stcore/metrics` | GET | Prometheus cache metrics: function names, hit counts, timings | no | open | low (operational detail) |
| `/_stcore/host-config` | GET | Frontend settings, allowed parent origins | no | open | low |
| `/_stcore/script-health-check` | GET | Not enabled; returns the index | no | open | none |
| `/_stcore/upload_file/{session}/{file}` | PUT, DELETE | Uploads for `st.file_uploader` | only if an uploader existed; none does, and it needs the XSRF token | open | low |
| `/media/{id}` | GET | Media created by a session (none today: Plotly charts go over the websocket) | no | open | low |
| `/static/*`, `/component/*`, `/_stcore/bidi-components/*` | GET | Streamlit's JS/CSS; custom components (none) | no | open | none |
| `/_stcore/agent/openapi.json`, `/_stcore/agent/interact`, `/_stcore/agent/mcp` | GET, POST… | Streamlit's agent API, which drives the app without a browser | `server.enableAgentApi` is off (the default); `openapi.json` is served and says so | API disabled | **high if ever enabled**: must stay off |
| `/auth/login`, `/auth/logout`, `/oauth2callback` | GET | Streamlit's OIDC login | no `[auth]` configuration, so it is unusable | open | none |

### The bot (`daily_news_bot`)

| Interface | Direction | Authentication | Changes state? |
|---|---|---|---|
| Telegram long poll (`getUpdates`) | outbound only; no listening port | Commands run only from allowlisted chat ids (`commands._authorized_chat_ids`). On the Pi: `TELEGRAM_CHAT_ID` is a private chat and `HEALTH_PING_CHAT_ID` is unset. Others are logged and ignored. | `/fill`, `/unfill`, `/deposit` write `real_trades` and `deposits`; edited copies are refused. `/recommendations` records sent picks. |
| Slack webhook and Web API | outbound only | Slack's own tokens | no |
| Docker HEALTHCHECK | inside the container | none needed | no: reads heartbeat files (`news_bot.py --healthcheck`) |
| Ollama | outbound to the ranker network | none (internal network; not published by this repo) | no |

## 4. What can modify trading records or application state

- **Through the dashboard: nothing.** The connection is opened as `file:…?mode=ro` (`streamlit/app.py:36`), and the volume is mounted `news_bot_data:/bot-data:ro`. Either guard alone blocks writes. Every query is parameterised, and every input is a selectbox, radio, slider or checkbox filled from the database.
- **Through Telegram:** `/fill`, `/unfill` and `/deposit` (real trades and deposits) and `/recommendations` (sent picks). These are protected by the chat-id allowlist, not by anything HTTP. The task's "protect /fill" is therefore already met, and the dashboard work leaves it alone.
- **Scheduled jobs inside the bot** write the screens, the signal archive, prices, snapshots and backups. They have no external trigger.

## 5. Existing authentication and authorisation

| Part | Present? |
|---|---|
| Dashboard login | **None.** The README says "No login, so keep the port on the home network / VPN"; compose has the same comment. |
| Streamlit OIDC (`st.login`) | Available in 1.67 but not configured. It needs an external identity provider and an HTTPS callback URL that the provider accepts. A LAN host on plain HTTP has neither. |
| Telegram commands | Chat-id allowlist (above). |
| Host | ufw (does not cover Docker ports); nginx for other sites; Portainer on `0.0.0.0:9443/8000` with its own login. |

## 6. Secrets and configuration patterns

- **Where secrets live.** All secrets are in the Pi's `.env`, which is not in git and is excluded from both Docker build contexts. Compose passes each variable to the bot through an explicit `environment:` list, and `.env.example` documents each one.
- **The dashboard's environment.** The dashboard service has no `environment:` block today. Its only configuration is `config.toml` plus the optional `DASHBOARD_DB_FILE`.
- **Logs.** The bot's logs pass through `logsetup.redact_secrets` (Telegram, Slack, Bearer and API-key patterns). Streamlit logs only its own messages.
- **Compose interpolation.** Compose interpolates `${…}` from the host `.env`, so a secret that contains `$` gets mangled unless it is quoted. Any new secret format should avoid `$`.
- **Deploys.** A systemd timer on the Pi runs `scripts/deploy.sh` every 5 minutes, so **a merge to main is a deploy**, dashboard included (`docker compose up -d --force-recreate news-notifier dashboard-streamlit`).

## 7. Do Telegram or internal services depend on the dashboard's open endpoints?

**No.** No code in the repository makes an HTTP request to the dashboard: not the bot, not `deploy.sh`, not the backup scripts, not CI. The bot and the dashboard share only the `news_bot_data` volume, read-write for the bot and read-only for the dashboard. The only HTTP caller that must keep working without credentials is a health probe, and no dashboard health check exists yet.

## 8. Recommended security architecture

**A login in front of Streamlit, inside the dashboard container.**

**How it is built**

- A small pure-ASGI middleware, added with Streamlit's public `st.App(script, middleware=[…])` API.
- It needs no new package or service: Starlette and uvicorn already ship with Streamlit, and the middleware uses only the Python standard library.
- It covers every route, including the websocket, except `/_stcore/health`.

**Password and session**

- **Credential:** one dashboard password, stored only as a PBKDF2-SHA256 hash (600,000 iterations) in `DASHBOARD_PASSWORD_HASH`. The format has no `$`.
- **Session:** after login, the user gets an HMAC-SHA256-signed token (issued, expires, nonce) in a cookie that is `HttpOnly`, `SameSite=Lax`, `Path=/` and time-limited (`DASHBOARD_SESSION_HOURS`, default 12). The cookie is also `Secure` when served over HTTPS.
- **Signing key:** `DASHBOARD_SESSION_SECRET`, or a random key per process if it is unset.
- **Logout** clears the cookie and refuses tokens issued before it.

**HTTP responses**

| Case | Response |
|---|---|
| No valid session, browser page | 401 with the login form as the body |
| No valid session, other requests | plain 401 |
| No valid session, or a foreign `Origin`, on the websocket | refused before it is accepted (HTTP 403) |
| Wrong password | 401 |
| Cross-origin login POST | 403 |
| Too many failures | 429 with `Retry-After` |
| Login not configured | **fail closed**: 503 on everything except `/_stcore/health` |

**CSRF.** The only state-changing HTTP requests are login and logout. They are checked against `Origin` (or `Referer`) and protected by SameSite. Everything else travels over the websocket, which requires the cookie (not sent cross-site under `Lax`) and a same-origin `Origin`.

**Defence in depth.** Remove `enableCORS = false`, so Streamlit's own origin check applies to the websocket as well.

**Not chosen, and why**

| Option | Why not |
|---|---|
| HTTP Basic auth | Smaller, but the password crosses the network on every request, there is no logout, and Basic credentials on the websocket handshake are unreliable in some browsers. |
| Streamlit OIDC | Needs an external identity provider and an HTTPS callback. |
| An nginx or Caddy proxy with auth | Host configuration outside the repository, or an extra service. |
| Binding the port to 127.0.0.1 | Breaks the current access by hostname from the LAN and VPN. |

### Does this protect the dashboard under the actual topology?

**What it stops**

- Unauthenticated devices on either network.
- Any web page reading the dashboard through a cross-site websocket.
- DNS rebinding: the cookie belongs to the real host, and both `Origin` and the websocket are checked.

**What it does not stop**

- **Traffic capture on the LAN.** There is no TLS. The password crosses the network once per login and the cookie on every request; someone who can capture Wi-Fi traffic could replay the cookie until it expires. The fix is TLS (Streamlit's `server.sslCertFile`, or access over the VPN only). It is listed as a follow-up.
- **Anyone with Docker or Portainer access on the Pi.** They can read the environment and the database directly.
- **A browser tab already connected** when the session expires or the user logs out. It keeps its websocket until it reloads.
- **Host-level issues outside this repository**: Portainer (`0.0.0.0:9443/8000`, full Docker control) and Docker ports bypassing ufw. Narrowing the bind (`DASHBOARD_STREAMLIT_PORT=<ip>:8002`) or adding `DOCKER-USER` rules are owner decisions on a pet machine.

## 9. Compatibility with the Docker deployment

| Concern | Effect |
|---|---|
| Service, image, port, volume | Unchanged. The image gains two Python files and runs `streamlit run serve.py` instead of `app.py`. |
| Read-only root and `nobody` user | The middleware writes nothing; its state (failure counts, logout time) is kept in memory. |
| New settings | `DASHBOARD_PASSWORD_HASH` (required), `DASHBOARD_SESSION_SECRET` (recommended), `DASHBOARD_SESSION_HOURS` (optional). They are passed from the host `.env` by a new `environment:` block. |
| Auto-deploy | A merge rebuilds the dashboard within 5 minutes. **The hash must be in the Pi's `.env` before the merge.** Otherwise the dashboard answers 503 until it is set and the container is recreated. |
| Health | `/_stcore/health` stays open. A Docker HEALTHCHECK on it is added to the dashboard image (urllib, since the image has no curl). `deploy.sh` waits only for the bot and is unchanged. |
| Telegram, Slack, the bot, backups | Untouched: none of them talk to the dashboard. |
| Rollback | `git revert` and redeploy. No data or schema is involved. |

## 10. Minimal implementation plan

| File | Change |
|---|---|
| `streamlit/auth.py` (new) | Password hashing and checking, token signing, the `LoginGate` middleware, settings from the environment, and a `hash` command. Standard library only. |
| `streamlit/serve.py` (new) | `app = st.App("app.py", middleware=[Middleware(auth.LoginGate, ...)])` |
| `streamlit/Dockerfile` | Copy both files; run `serve.py`; HEALTHCHECK on `/_stcore/health` |
| `streamlit/.streamlit/config.toml` | Drop `enableCORS = false` |
| `streamlit/app.py` | A "Log out" link in the sidebar; no page logic changes |
| `docker-compose.yml` | Dashboard `environment:` with the three variables; the comment updated |
| `.env.example`, `README.md` | The new variables, how to make the hash and secret, the TLS caveat |
| `tests/test_dashboard_auth.py` (new) | See section 11 |
| `docs/audit/dashboard-security-implementation-report.md` (new) | Results |

Not touched: the bot, strategy, gates, ATR, risk, sizing, records, deploy scripts, nginx, Portainer, the firewall.

## 11. Tests required

**Automated tests**

The automated tests run in the existing unit suite. `auth.py` is loaded by path, like `archive_views.py`, and driven through a standard-library ASGI harness, so CI needs no new dependencies. They cover:

- **Authentication**
  - The password hash round-trips; a wrong password, a malformed hash and an empty hash are rejected.
  - Login sets the cookie with the right flags, adding `Secure` only under HTTPS.
  - A tampered, expired, wrong-key or logged-out token is refused.
- **Access, for every protected route**
  - Unauthenticated HTTP gets 401, and the app behind the middleware is never called.
  - An unauthenticated websocket is refused.
  - The health route stays open.
  - With login not configured, every route answers 503 and health stays open.
- **CSRF and origin**
  - A cross-origin or origin-less login POST gets 403.
  - A websocket from a foreign origin is refused even with a valid cookie.
  - `next` cannot redirect off the site.
- **Abuse**
  - A failing client gets 429 with `Retry-After`.
  - An oversized login body gets 413.
- **Secrets**
  - The password, hash, secret and cookie never appear in responses or logs.
- **Configuration guards**
  - CORS stays on.
  - Compose passes the variables.
  - The image runs `serve.py` and has a HEALTHCHECK.

**Manual and integration checks:**

- Build the image and run it with a test hash and a demo database.
- curl the route matrix.
- Check the websocket handshake without a cookie, with a foreign origin, and with a valid session.
- `docker inspect` shows the container healthy.
- Log in through headless Chrome and open every page.
- The existing AppTest smoke test still passes.
- The full suite, Ruff, and both Docker builds pass.
