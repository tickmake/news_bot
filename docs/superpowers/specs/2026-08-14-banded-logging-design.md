# Banded Logging — INFRA / SYSTEM / APP

Date: 2026-08-14
Status: Approved, pending implementation plan

## Goal

Show infrastructure logs and application logs in Portainer's container log
view, visually distinguishable from each other.

## Problem

### The current format mislabels third-party messages

`logging.basicConfig` configures the **root** logger, so every library shares
the app's format string:

```
level=INFO event=Adding job tentatively -- it will be properly scheduled...   APScheduler
level=INFO event=Failed to create TzCache, reason: ...                        yfinance
level=INFO event=telegram_sent chunks=3                                       the app
```

Only the third is an app event. `event=` asserts something untrue about the
first two, and there is no way to tell at a glance which lines came from the
bot.

### Eight debug calls can never emit

`logging.basicConfig(level=logging.INFO)` is hardcoded with no environment
override, so these are unreachable in production:

| Call site | Event |
|---|---|
| `news_bot.py:435` | `domain_parse_failed` |
| `news_bot.py:1753` | `atr_compute_failed` |
| `news_bot.py:1806` | `metrics_skipped reason=insufficient_history` |
| `news_bot.py:1816` | `metrics_skipped reason=zero_prev_close` |
| `news_bot.py:1823` | `metrics_skipped reason=zero_reference_close` |
| `news_bot.py:1844` | `metrics_skipped reason=atr_unavailable` |
| `news_bot.py:1859` | `metrics_failed` |
| `news_bot.py:2026` | `candidate_future_failed` |

Five of these explain the trade screen's `no data N` count. The reason is
computed and discarded, so the briefing reports that a symbol had no usable
data without any way to learn which symbol or why.

### Everything goes to stderr

`basicConfig` defaults to `stderr`, so routine INFO arrives on the error
stream. Pipelines that treat stderr as a failure signal see every successful
run as noise.

### Container logs are unbounded

The container runs `json-file` with `opts=map[]` — no `max-size`, no
`max-file`. Logs grow without limit on the NVMe, and enabling DEBUG
accelerates it.

## Decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Number of bands | Three: INFRA / SYSTEM / APP |
| 2 | Where the band lives | Logger name — two child loggers |
| 3 | Colour | ANSI, plus a textual band marker |
| 4 | Log rotation | Included |

## Design

### Logger topology

```python
LOGGER  = logging.getLogger("news_bot")        # parent; nothing logs to it directly
APP_LOG = logging.getLogger("news_bot.app")    # business events
SYS_LOG = logging.getLogger("news_bot.sys")    # plumbing
```

Band resolution reads `record.name`:

| Logger name | Band |
|---|---|
| `news_bot.app*` | APP |
| `news_bot*` (anything else) | SYSTEM |
| everything else | INFRA |

Bare `news_bot` resolving to SYSTEM means a missed call site lands somewhere
sane rather than being misclassified as a library.

The hierarchy naming is load-bearing, not cosmetic. Two existing tests call
`assertLogs(news_bot.LOGGER, level="WARNING")`; because `news_bot.sys` records
propagate to `news_bot`, they keep passing untouched. Flat names such as
`newsbot_sys` would silently break both.

`assertLogs` captures `record.getMessage()`, not the handler's format string,
so no existing assertion depends on the `event=` prefix. That is what makes
the output shape free to change.

### Band assignment for existing call sites

38 call sites, 34 distinct events.

**APP (10)** — what the bot exists to do:
`briefing_start`, `news_select`, `telegram_sent`, `telegram_send_failed`,
`telegram_skipped`, `briefing_send_failed`, `command_received`,
`ranker_failed`, `trade_candidates_deadline_reached`, `watchlist_truncated`

**SYSTEM (24)** — the bot's own plumbing:
`retry`, `retry_exhausted`, `rss_fetch_failed`, `newsapi_fetch_failed`,
`freenews_fetch_failed`, `provider_failed`, `finnhub_quote_failed`,
`screener_fetch_failed`, `screener_temporarily_skipped`,
`screener_ssl_backoff_active`, `state_load_failed`, `state_save_failed`,
`state_serialize_failed`, `command_poll_failed`,
`telegram_webhook_check_failed`, `telegram_webhook_detected`,
`domain_parse_failed`, `atr_compute_failed`, `metrics_skipped`,
`metrics_failed`, `candidate_future_failed`, `topic_weight_invalid`,
`topic_weight_unknown`, `deprecated_setting`

**INFRA** — no code changes; routed by logger name:
APScheduler, yfinance, urllib3, requests, peewee, charset_normalizer.

`ranker_failed` is APP rather than SYSTEM because it changes what the reader
receives: it is the signal that headlines were ranked by the heuristic instead
of the model.

### Output format

```
2026-08-14 19:12:03  APP    INFO   briefing_start at=2026-08-14T19:12:03
2026-08-14 19:12:05  SYS    WARN   rss_fetch_failed feed=https://... detail=...
2026-08-14 19:12:06  INFRA  INFO   apscheduler.scheduler | Scheduler started
```

Exact column widths, so alignment does not depend on the implementer's taste:

| Field | Width | Separator |
|---|---|---|
| timestamp | `%Y-%m-%d %H:%M:%S` (19) | two spaces |
| band tag | left-justified to 5 (`APP  `, `SYS  `, `INFRA`) | two spaces |
| level tag | left-justified to 5 (`DEBUG`, `INFO `, `WARN `, `ERROR`) | two spaces |
| message | remainder | — |

- Level abbreviated: `DEBUG` / `INFO` / `WARN` / `ERROR`. `WARNING` and
  `CRITICAL` map to `WARN` and `ERROR`; nothing else is expected.
- **Milliseconds are dropped** from the timestamp. They cost four columns on
  every line and this application measures nothing at that resolution —
  where duration matters it is logged explicitly, as `latency_ms=`.
- INFRA lines carry the originating logger name, separated by ` | `, e.g.
  `apscheduler.scheduler | Scheduler started`. "Scheduler started" says
  nothing about who said it, and that is the exact message currently
  mislabelled as `event=`. APP and SYSTEM lines omit the logger name — the
  event token already identifies them.
- The `event=` prefix is removed. It was only ever true for app records.

Output moves from stderr to **stdout**.

### Colour

| Band | Treatment |
|---|---|
| APP | band tag bold cyan |
| SYSTEM | band tag yellow, message default |
| INFRA | whole line dim |

Severity colours the *level* tag independently — `WARN` yellow, `ERROR` bold
red — so an error stays obvious without losing its band. The message body is
never coloured, so it stays legible under any terminal theme.

**Invariant:** the plain text is byte-identical with colour on or off; only
escape sequences differ. Stripping ANSI from coloured output must equal
uncoloured output exactly. Otherwise `grep`, log shipping, and any future
parsing depend on a display setting.

Colour is not the only carrier of the distinction. The textual band marker
means the information survives ANSI stripping, `docker logs > file`, and a
viewer that does not render escapes.

**Whether Portainer EE renders ANSI is unverified.** Confirming it requires
signing in to the Portainer UI. The textual band marker is the hedge; verify
on screen after deploy.

### Configuration

| Setting | Default | Controls |
|---|---|---|
| `LOG_LEVEL` | `INFO` | APP + SYSTEM bands |
| `LOG_LEVEL_LIBRARIES` | `WARNING` | INFRA band |
| `LOG_COLOR` | `true` | ANSI escapes on/off |

Added to `AppSettings` so they are typed and documented like every other
setting.

The two-level split rests on one Python logging property:

```python
logging.getLogger().setLevel(LOG_LEVEL_LIBRARIES)   # libraries inherit this
logging.getLogger("news_bot").setLevel(LOG_LEVEL)   # app + system
```

**Ancestor logger levels do not filter propagated records** — only the
originating logger's effective level and the handler's level do. So `news_bot`
at DEBUG still reaches the root handler with root at WARNING, while urllib3
(no level of its own) inherits WARNING and stays quiet. This is what lets
`LOG_LEVEL=DEBUG` unlock the eight dead debug calls without urllib3 emitting a
line per socket.

`LOG_COLOR` defaults to `true`, deliberately **not** to `sys.stdout.isatty()`.
A container has no TTY, so TTY detection would disable colour in exactly the
place it was asked for.

### Error handling

**Invalid `LOG_LEVEL`.** `logging.getLevelName("BOGUS")` returns the string
`"Level BOGUS"` rather than raising, so a typo would pass through and produce
nonsense. Setup validates against the known level names and falls back to
INFO, emitting a SYSTEM warning naming the value it ignored.

**Repeat installation.** `basicConfig` is removed and a handler installed
directly; called twice — module re-import, test setup — it would duplicate
every line. Setup guards on an already-installed marker and is idempotent.

Configuration runs at module import, not in `__main__`, so
`python -c "import news_bot"`, the test suite, and the container all get the
same setup.

### Log rotation

`docker-compose.yml` gains:

```yaml
logging:
  driver: json-file
  options: { max-size: "10m", max-file: "3" }
```

Caps container logs at 30 MB. Requires a Portainer redeploy to take effect,
and does not alter application behaviour.

## Testing

The formatter is a pure function of a `LogRecord`; most tests build a record,
format it, and assert on the string. No stdout capture needed.

**Band resolution**
- `news_bot.app` → APP, `news_bot.sys` → SYSTEM, `apscheduler.scheduler` → INFRA
- bare `news_bot` → SYSTEM

**Format**
- band tag present and padded; `WARNING` renders as `WARN`
- INFRA lines carry the originating logger name; APP and SYSTEM lines do not
- no `event=` prefix

**Colour**
- `LOG_COLOR=true` produces ANSI escapes; `false` produces none
- stripping ANSI from coloured output equals uncoloured output exactly

**Levels**
- `LOG_LEVEL=DEBUG` emits `news_bot.sys` debug records
- `LOG_LEVEL=DEBUG` does not emit `urllib3` debug records
- `LOG_LEVEL_LIBRARIES=DEBUG` does emit them

**Robustness**
- invalid `LOG_LEVEL` falls back to INFO and warns, naming the ignored value
- setup called twice installs one handler, not two

**Stream**
- the handler writes to stdout, not stderr

Roughly 13 tests.

Not covered by any test: whether Portainer EE renders the ANSI codes. That
needs eyes on the UI. Verify the real output on the Pi after deploy rather
than trusting formatter unit output alone.

## Out of scope

- Shipping logs off the host, or an endpoint to query them remotely
- New log events at call sites that are currently silent; this reclassifies
  and displays what already exists
- Structured JSON output for machine consumption
- The `feeds.apnews.com` DNS failure
