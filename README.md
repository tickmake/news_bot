# News Bot

Automated Telegram briefing bot that sends curated daily updates with:

- Norwegian morning headlines
- Global top news
- Business stories
- Live stock movers from public market screeners
- Live mutual fund / ETF movers from public market screeners
- Short-term trade candidates (informational screener)
- Daily greeting and rotating quote (morning) / evening greeting

The bot is built in Python and scheduled with APScheduler.

## Features

- **Telegram delivery** using Bot API with HTML formatting.
- **Readable output** with concise headlines and `(more)` links.
- **Tabular finance sections** rendered via `<pre>` for clarity in Telegram.
- **Twice-daily schedule** at `07:00` and `19:00` (local timezone).
- **Deterministic daily rotation** for greetings/quote (stable within a day).
- **Live public data feeds** for news, stocks, and funds (no hardcoded default symbols).
- **Typed settings validation** via `pydantic-settings`.
- **Fallback-safe behavior** when API data is missing or incomplete.
- **Automatic retries/backoff** for external API calls.
- **Relevance-ranked headlines** by importance, topic weight, and recency — not raw feed order.
- **Cross-outlet and cross-day deduplication**, so one story appears once.
- **Local LLM ranking** via Ollama — no API key, no data leaves your network — with a deterministic heuristic fallback.
- **Telegram command support** (`/now`, `/news`, `/recommendations`, `/stocks`, `/watchlist`, `/analyze`, `/performance`, `/health`).
- **Optional Slack mirror** of every outbound message via an incoming webhook, alongside Telegram.
- **Health ping** support for runtime monitoring.
- **CI test workflow** via GitHub Actions.

## Project Structure

- `news_bot.py` - main bot logic and scheduler
- `test_news_bot.py` - unit tests
- `requirements.txt` - Python dependencies
- `Dockerfile` - container image definition
- `docker-compose.yml` - service orchestration

## Requirements

- Python `3.11+` recommended
- Telegram bot token
- Telegram chat ID

## Quick Start (Local)

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create `.env` with required values:

```bash
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Tip: you can copy from `.env.example` and fill in your secrets.

4. Run a one-off briefing:

```bash
set -a && . ./.env && set +a
python -c "import news_bot; print(news_bot.job_daily_briefing())"
```

5. Run scheduler mode:

```bash
set -a && . ./.env && set +a
python news_bot.py
```

## Environment Variables

### Required

- `TELEGRAM_TOKEN` - Telegram bot token
- `TELEGRAM_CHAT_ID` - target chat/channel/group ID

### Optional

- `TZ` - timezone for scheduler (default `Europe/Oslo`)
- `RECIPIENT_NAME` - name shown in greeting (default `Sunil`)
- `TELEGRAM_MESSAGE_MAX_CHARS` - chunk size per Telegram message (default `3900`)
- `STATE_FILE` - local JSON state file path (default `.news_bot_state.json`; under docker compose it points into the `news_bot_data` volume at `/app/data`)
- `COMMAND_POLL_ENABLED` - enable Telegram command handling (default `true`)
- `COMMAND_LONG_POLL_TIMEOUT_SECONDS` - Telegram long-poll hold time; commands respond near-instantly (default `25`)
- `COMMAND_POLL_INTERVAL_MINUTES` - **deprecated**, retained for compatibility but unused (long-polling replaced interval polling)
- `SEND_STARTUP_BRIEFING` - run one immediate briefing on container start (default `false`)
- `HEALTH_PING_ENABLED` - enable daily health ping (default `true`)
- `HEALTH_PING_CHAT_ID` - optional separate chat for health pings
- `NEWS_API_KEY` - optional NewsAPI key for structured news fetch
- `FREENEWS_API_KEY` - optional FreeNews API key
- `FREEN_EWS_API_KEY` - backward-compatible alias for existing envs
- `FINNHUB_API_KEY` - Finnhub API key (recommended primary quote source)
- `FINHUB_API_KEY` - backward-compatible alias for existing envs
- `NEWS_FETCH_PRIORITY` - provider order, e.g. `newsapi,freenews,rss`
- `FREENEWS_API_URL` - FreeNews endpoint URL (default `https://freenewsapi.com/api/v1/news`)
- `FINNHUB_API_URL` - Finnhub base URL (default `https://finnhub.io/api/v1`)
- `GLOBAL_NEWS_FEEDS` - comma-separated RSS feed URLs for global news
- `BUSINESS_NEWS_FEEDS` - comma-separated RSS feed URLs for business news
- `NORWAY_NEWS_FEEDS` - comma-separated RSS feed URLs for Norway-focused news
- `STOCK_SCREENERS` - comma-separated Yahoo predefined screener IDs for equities
- `FUND_SCREENERS` - comma-separated Yahoo predefined screener IDs for funds/ETFs
- `SCREENER_QUOTE_LIMIT` - number of quotes to fetch per screener
- `SCREENER_REQUEST_TIMEOUT_SECONDS` - timeout for screener API calls (default `6`)
- `SCREENER_CACHE_TTL_SECONDS` - in-memory screener cache duration (default `90`)
- `SCREENER_FAILURE_COOLDOWN_SECONDS` - cooldown after screener SSL/network failures (default `300`)
- `FINNHUB_REQUEST_TIMEOUT_SECONDS` - timeout for Finnhub quote calls (default `4`)
- `FINNHUB_CACHE_TTL_SECONDS` - Finnhub quote cache duration (default `120`)
- `FINNHUB_FAILURE_COOLDOWN_SECONDS` - cooldown on Finnhub failures/rate limits (default `180`)
- `FINNHUB_MAX_SYMBOLS_PER_REFRESH` - max symbols per section refreshed via Finnhub (default `16`)
- `SLACK_WEBHOOK_URL` - optional Slack incoming webhook URL; mirrors every outbound message to Slack alongside Telegram (see [Slack Integration](#slack-integration))
- `SLACK_MESSAGE_MAX_CHARS` - chunk size per Slack message (default `3900`)

### Logging

Every line is tagged with one of three bands, so application output is
distinguishable from infrastructure noise at a glance:

```
2026-08-14 19:12:03  APP    INFO   briefing_start at=2026-08-14T19:12:03
2026-08-14 19:12:05  SYS    WARN   rss_fetch_failed feed=https://... detail=...
2026-08-14 19:12:06  INFRA  INFO   apscheduler.scheduler | Scheduler started
```

- **APP** — what the bot exists to do: briefings composed, headlines
  selected, messages sent, commands received.
- **SYS** — the bot's own plumbing: retries, feed and screener failures,
  state file I/O, cache backoff.
- **INFRA** — third-party libraries (APScheduler, yfinance, urllib3),
  carrying their originating logger name.

Bands are colour-coded — APP bold cyan, SYS yellow, INFRA dimmed — and
severity colours the level tag independently, so an error stays obvious
without losing its band. The band tag is also written as text, so the
distinction survives a viewer that does not render ANSI, and
`docker logs > file`.

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | APP and SYS bands |
| `LOG_LEVEL_LIBRARIES` | `WARNING` | INFRA band |
| `LOG_COLOR` | `true` | ANSI escapes on/off |

The two levels are separate because `LOG_LEVEL=DEBUG` would otherwise make
urllib3 emit a line per socket. Setting `LOG_LEVEL=DEBUG` reveals per-symbol
detail that is otherwise computed and discarded — including the reason behind
each symbol counted in the trade screen's `no data` total:

```
SYS    DEBUG  metrics_skipped symbol=AAPL reason=insufficient_history bars=12
```

`LOG_COLOR` defaults to on rather than to TTY detection: a container has no
TTY, so the usual `isatty()` check would disable colour exactly where it is
wanted. Set `LOG_COLOR=false` when piping logs to a file or a parser.

Container logs are capped at 3 files of 10 MB by the `logging:` block in
`docker-compose.yml`. Applying a change to it needs a redeploy, not just a
restart.

### News ranking

The bot ranks headlines rather than taking whatever the feeds list first.
Ranking combines real-world importance, your topic weights, and recency, and
collapses the same story reported by multiple outlets into a single line.

Two ranking paths:

- **Local LLM ranking** (default) — a model running in Ollama on your own
  hardware. Catches cross-outlet paraphrase such as "Fed holds rates steady"
  and "Federal Reserve keeps rates unchanged", which share almost no words.
  No API key, no cost per briefing, and no headline ever leaves your network.
- **Heuristic ranking** (automatic fallback) — recency decay, keyword topic
  weights, and source tier. No network at all, fully deterministic.

The heuristic path is used whenever the LLM path is unavailable *or fails*:
Ollama not running, model not pulled, timeout, or malformed response. A
section always renders.

The LLM does not see the whole candidate pool. The heuristic pre-ranks and
the LLM re-ranks its top `NEWS_RANKER_MAX_CANDIDATES` — so the pool stays
wide enough for cross-outlet deduplication while the slow path only handles
a short, already-good list. Latency scales with that number, not pool size.

**Connecting to Ollama.** By default the bot talks to a host named `ollama`
on the *ranker network*. Two ways to provide it:

- *Reuse an existing Ollama* (recommended if you already run one). Point the
  ranker network at that project's network in `.env`:

  ```bash
  RANKER_NETWORK=poster-bot_default
  RANKER_NETWORK_EXTERNAL=true
  ```

  Find the network name with:
  `docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' ollama`

- *Use the bundled service*, off by default so it never duplicates an Ollama
  you already run:

  ```bash
  docker compose --profile bundled-ollama up -d
  ```

  This starts Ollama and pulls `NEWS_RANKER_MODEL` into a named volume.

Outside Docker, run Ollama yourself and set
`NEWS_RANKER_URL=http://localhost:11434`:

```bash
ollama pull qwen2.5:3b
```

Note that `NEWS_FETCH_PRIORITY` now orders a single pooled candidate set
rather than selecting one provider — every configured provider is fetched on
each send, which is what makes cross-outlet deduplication possible.

| Variable | Default | Purpose |
|---|---|---|
| `NEWS_RANKER_ENABLED` | `true` | Kill switch |
| `NEWS_RANKER_URL` | `http://localhost:11434` | Ollama endpoint (compose sets `http://ollama:11434`) |
| `NEWS_RANKER_MODEL` | `qwen2.5:3b` | Ranking model; must be pulled into Ollama |
| `NEWS_RANKER_MAX_CANDIDATES` | `14` | Shortlist size sent to the LLM |
| `NEWS_RANKER_TIMEOUT_SECONDS` | `240` | Per-call timeout |
| `RANKER_NETWORK` | bundled net | Network carrying Ollama |
| `RANKER_NETWORK_EXTERNAL` | `false` | Set `true` to reuse another project's network |
| `NEWS_MAX_AGE_HOURS` | `30` | Freshness ceiling; undated items exempt |
| `NEWS_CANDIDATE_POOL_SIZE` | `60` | Candidates ranked per section |
| `NEWS_DEDUP_WINDOW_DAYS` | `7` | Cross-day suppression window (includes today) |
| `NEWS_RECENT_TITLE_DAYS` | `3` | Recent titles sent to the ranker |
| `NEWS_TOPIC_WEIGHTS` | unset | Weight overrides, e.g. `markets:2.5,sports:0.1` |

Topic categories are `markets`, `norway`, `india`, `tech` (up-weighted) and
`sports`, `celebrity`, `crime`, `lifestyle`, `shopping` (down-weighted).
Down-weighting is not exclusion — a low-weight headline still appears when
nothing better is available.

There is no per-briefing cost — the trade is hardware instead. Measured on a
Raspberry Pi 5 (CPU only, 4 cores) with a warm model:

| Candidates sent | `llama3.2:3b` | `qwen2.5:3b` |
|---|---|---|
| 10 | ~134 s | ~132 s |
| 14 | ~193 s | ~107 s |
| 20 | ~269 s | ~236 s |

An earlier version of this table reported far lower figures (20 candidates in
~74 s). Those were timing a no-op: the response schema had no `minItems`, so
an empty `selections` array satisfied it and the model returned
`{"selections": []}` in about 4 seconds without ranking anything. The bot then
silently fell through to `HeuristicSelector`. See `_ranker_response_schema`.

Hence the default shortlist of **14** — at 20 candidates `llama3.2:3b` exceeds
the 240 s timeout outright.

The default model is `qwen2.5:3b` on measured dedup behaviour. Across runs at
10, 14 and 20 candidates, `llama3.2:3b` ranked both "Fed holds rates steady"
and "Federal Reserve keeps benchmark rate unchanged" as separate stories every
time; `qwen2.5:3b` collapsed them every time. Cross-outlet paraphrase dedup is
the whole reason this path exists — `HeuristicSelector` cannot do it — so that
outweighs `qwen2.5:3b` being slightly looser about topic down-weighting.

Both are still small models. If ranking quality matters more than keeping
everything on one small box, point `NEWS_RANKER_URL` at a machine running a
7B+ model; note that a 7B needs roughly 6 GB resident, which will not fit
alongside other stacks on an 8 GB Pi. GPU acceleration for the bundled service
is a commented-out block in `docker-compose.yml`.

Run `/health` to see which path is active, the model, call latency, and the
last ranker error.

### Optional Universe Configuration

You can optionally inject your own symbols with comma-separated `Label:SYMBOL` entries.

Supported variables:

- `USA_STOCK_UNIVERSE`
- `INDIA_STOCK_UNIVERSE`
- `NORWAY_STOCK_UNIVERSE`
- `EU_STOCK_UNIVERSE`
- `INDIA_MUTUAL_FUNDS`
- `NORWAY_MUTUAL_FUNDS`

Example:

```bash
USA_STOCK_UNIVERSE="Apple:AAPL,Microsoft:MSFT,NVIDIA:NVDA"
INDIA_STOCK_UNIVERSE="Reliance:RELIANCE.NS,TCS:TCS.NS"
EU_STOCK_UNIVERSE="SAP:SAP.DE,LVMH:MC.PA,ASML Holding:ASML.AS"
INDIA_MUTUAL_FUNDS="Nifty BeES:NIFTYBEES.NS,Gold BeES:GOLDBEES.NS"
```

If these are empty, the bot relies fully on live screener data -- except
`EU_STOCK_UNIVERSE`, which ships with a default list of large-cap eurozone
names (SAP, ASML, LVMH, and others). Yahoo's live screener endpoint is
hardcoded to US-market data regardless of region/lang parameters, so EU
symbols can only reach the trade screen through this configured list. Set
`EU_STOCK_UNIVERSE` to your own `Label:SYMBOL` entries to replace the
default (an unset or empty value falls back to it, same as the other
universe variables above).

### Trade Risk Controls

- `TRADE_MIN_SCORE` - **deprecated and ignored.** Candidates now pass mandatory
  gates rather than reaching a score threshold. Setting it logs a warning at
  startup.
- `TRADE_MIN_WEEK_MOMENTUM_PCT`
- `TRADE_MIN_DAY_CHANGE_PCT`
- `TRADE_MIN_VOLUME_RATIO` - threshold for the `✓` volume-confirmation marker,
  and for the volume-confirmation tier in the ranking below. Volume never
  admits or rejects a candidate.
- `TRADE_MAX_DRAWDOWN_PCT`
- `TRADE_MAX_ATR_PCT`

#### How qualifying candidates are ordered

Everything that passes the gates is shown; ranking only decides the order.
Two quality tiers sort first, then the original ordinal rule:

1. **Not overbought** — RSI 14 below 70 sorts above anything at or over it.
2. **Volume confirmed** — at or above `TRADE_MIN_VOLUME_RATIO` sorts above
   anything below it.
3. 5-day momentum, then volume ratio, then qualifying streak, then lower ATR,
   then symbol (so identical metrics always render identically).

Tiers 1 and 2 are new. RSI was measured and displayed but read by nothing, so
a name at RSI 85 — extended, and the worst moment to open a swing position —
sorted above a healthy pullback purely on momentum, which is exactly the
number a blow-off top maximises. Volume had the mirror-image problem: as a raw
ratio in the second slot it only ever broke *exact* momentum ties, so an
unconfirmed move still outranked a confirmed one whenever momentum differed at
all.

Both are deliberately tiers rather than gates: they sort a name **down**, not
out, so nothing that qualifies today stops qualifying. Make them hard
disqualifiers only if you're willing to have days with zero picks.

### Trade Candidate Performance

Candidate analysis fetches per-symbol history from yfinance. These controls keep a briefing (and `/now`) responsive on a cold cache:

- `TRADE_UNIVERSE_MAX` - max symbols analysed per run (default `30`)
- `TRADE_FETCH_WORKERS` - parallel history fetch workers (default `6`)
- `TRADE_HISTORY_CACHE_TTL_SECONDS` - per-symbol history cache duration (default `600`)
- `TRADE_TOTAL_DEADLINE_SECONDS` - overall deadline for candidate analysis; partial results returned if exceeded (default `45`)

#### Which symbols get screened

Each run fills `TRADE_UNIVERSE_MAX` slots in three tiers, in order:

1. **Configured watchlist** — every symbol in `USA_/INDIA_/NORWAY_/EU_STOCK_UNIVERSE`
   and the mutual-fund lists. Always screened first.
2. **Recently tracked** — symbols screened at least once in the last
   `TRADE_STICKY_UNIVERSE_DAYS` (default `10`), most-often-qualifying first.
3. **Discovery** — fresh movers from Yahoo's predefined screeners, filling
   whatever is left, with at least `TRADE_DISCOVERY_MIN_SLOTS` (default `10`)
   slots reserved so a long-lived tracked set can never freeze the universe.

Tier 2 exists because `/stocks` and `/performance` read *recorded history*
rather than live prices, so they can only speak about symbols re-screened
across several sessions. Discovery alone returns a near-different set of
movers every day, so before this tier a discovered mover was measured once
and never looked at again — meaning neither command could ever evaluate one.

- `TRADE_STICKY_UNIVERSE_DAYS` - how long a seen symbol is carried forward (default `10`)
- `TRADE_DISCOVERY_MIN_SLOTS` - slots always reserved for fresh discovery (default `10`)

Screener names are **discovery only** — they decide which symbols are looked
at, never whether one qualifies. That stays with the five gates below.

### Trade Signal History

Every screening run records what it measured for every symbol it looked at —
qualified or not — in a local SQLite file (stdlib `sqlite3`, no extra
dependency). This is separate from the yfinance history cache above: it's a
rolling memory of the screener's own daily output, used to see streaks (e.g.
a symbol qualifying several sessions running) and, later, whether qualifying
days actually paid off.

- `TRADE_HISTORY_DB_FILE` - SQLite file path (default `.news_bot_trade_history.db`; under docker compose it points into the `news_bot_data` volume at `/app/data`)
- `TRADE_HISTORY_RETENTION_DAYS` - rolling window kept before older rows are pruned (default `14`)

Under docker compose both this DB and `STATE_FILE` are pinned to `/app/data`
on the `news_bot_data` named volume, and the `.env` values above are ignored
(they apply only to a direct, non-container run). Without that volume they
live in the container's writable layer and are wiped by every
`up --force-recreate`, silently resetting streaks, `/analyze` outcomes and
`/stocks` weekly picks to empty until enough sessions re-accumulate.

Candidates must pass every gate: price above its 20-day EMA, 5-day and 1-day
returns above their thresholds, and ATR and drawdown below their ceilings.
Metrics are computed on each symbol's last completed session — an in-progress
bar is never used, so at the 19:00 run markets that closed earlier that day
still report their previous session. Configured watchlist symbols are analysed
before screener movers.

Each qualifying candidate's block also shows:

- **RSI(14)**, flagged `(overbought)` at 70+ or `(oversold)` at 30 or below.
- **Volume trend** — the last 5 sessions' average volume vs. the 15 before
  that, as `rising`, `falling`, or `flat`.
- **Session streak** — how many recent sessions running (today included)
  the symbol has qualified, once that's 2 or more; backed by the trade
  signal history above.
- A **zone / invalidation / reward:risk** line derived from the same
  20-session support/resistance range the drawdown gate uses: the
  pullback zone between the recent low and the 20-day EMA, the level a
  close below which invalidates the setup, and the resulting reward-to-risk
  ratio. Technical-only, not a recommendation — see the disclaimer below.

## Scheduling

The bot schedules:

- **07:00 local time**
- **19:00 local time**

Configured in:

```python
scheduler.add_job(job_daily_briefing, "cron", hour="7,19", minute=0)
```

Additional jobs:

- daily health ping (`12:00`)
- Telegram command handling runs in a background long-polling thread (near-instant response)

## Telegram Commands

After sending `/start` to the bot, you can use:

- `/now` - send full briefing immediately
- `/news` - send only news headlines (Norway + global + business stories) — no live quotes, no trade candidates
- `/recommendations` - send only today's live trade screener candidate picks — no news, no live quote tables
- `/stocks` - send this week's top picks, ranked by how consistently they've qualified — see below
- `/watchlist` - send every configured watchlist symbol with today's read and its verdict — see below
- `/analyze TICKER` - send a deep-dive report for one symbol, e.g. `/analyze AAPL`
- `/performance` - send how the trade screener's past qualifying picks have done
- `/health` - send runtime health report

`/morning` and `/evening` (forcing the greeting/tone of the scheduled
07:00/19:00 briefing on demand) have been removed in favor of the more
focused `/news`, `/recommendations`, and `/stocks` commands above; the
scheduled `07:00`/`19:00` jobs themselves are unaffected. `/now` still sends
the full combined briefing on demand.

### `/news` and `/recommendations`

Two focused alternatives to the full twice-daily briefing, for when you only
want one half of it:

- **`/news`** sends just the headline sections — Norway, global, and business
  news — with no live stock/fund quote tables and no trade candidates mixed
  in.
- **`/recommendations`** sends just the trade screener's current candidate
  picks (identical output to the trade-candidates section of the full
  briefing) — no headlines, no live quote tables. This is a live, single-day
  re-screen — for a view aggregated over the week, see `/stocks` below.

Both are pure on-demand reads: they don't mark headlines as seen, so
triggering `/news` manually never suppresses a headline from the next
scheduled briefing.

### `/watchlist`

Every symbol in your configured watchlist (`USA_/INDIA_/NORWAY_/EU_STOCK_UNIVERSE`
and the mutual-fund lists), screened live and grouped by verdict:

- **Trade-ready today** — passed all five gates, with any qualifying streak.
- **Holding off** — measured fine but failed a gate, showing which one.
- **No usable price data** — the fetch returned nothing usable.

It screens *only* the configured symbols, so it stays fast and its result
doesn't depend on whatever the screeners happened to surface this morning.

This is the point of the command: a watchlist is for seeing how your own
names are doing, **including the ones that aren't trade-worthy today**.
Previously `/watchlist` rendered the market snapshot plus a filtered trade
screen — the same thing `/recommendations` shows — so a configured symbol
that failed a gate appeared nowhere at all.

### `/stocks`

This week's top trading picks — for swing trading, not a single day's
snapshot. Unlike `/recommendations` (today's live re-screen), `/stocks` reads
the rolling trade signal history (SQLite) built up over the last
`TRADE_WEEKLY_TOP_PICKS_LOOKBACK_DAYS` sessions (default `7`) and ranks
symbols by:

1. how many of those sessions they actually qualified on (consistency), then
2. their average weekly momentum across the window.

A symbol only appears if it qualified at least
`TRADE_WEEKLY_TOP_PICKS_MIN_QUALIFYING_DAYS` times (default `2`) in the
window — a one-off qualifying day is filtered out as noise. Like
`/performance`, this is a pure SQLite read (no fresh yfinance calls), so it
needs a few days of the screener actually running before it has anything to
rank; check `/recommendations` or `/watchlist` in the meantime.

The window is counted in **recorded sessions**, not calendar days — a symbol
is ranked over its last `TRADE_WEEKLY_TOP_PICKS_LOOKBACK_DAYS` rows. It used
to window by calendar day, which made a "7 session" window about 5 actual
trading sessions and quietly under-counted every symbol.

- `TRADE_WEEKLY_TOP_PICKS_LOOKBACK_DAYS` - sessions to look back over (default `7`)
- `TRADE_WEEKLY_TOP_PICKS_MIN_QUALIFYING_DAYS` - minimum qualifying sessions in the window to be included (default `2`)
- `TRADE_WEEKLY_TOP_PICKS_COUNT` - max picks returned (default `5`)

### `/analyze TICKER`

An on-demand, single-ticker report. It reuses the same price-history math as
the automated trade screener (EMA20, RSI, ATR, support/resistance, the
entry/exit sketch) and adds yfinance's `.info` snapshot for company profile,
valuation multiples, margins, balance-sheet ratios, cash flow, and beta.

This does **not** attempt every section of a full equity-research writeup.
Sections with no real data source available to this bot — macro series
(rates, inflation, GDP), options positioning, institutional 13F flow, insider
transaction feeds — are explicitly labelled "not available," never
fabricated. yfinance's fundamentals are also frequently incomplete for
smaller or non-US tickers; missing fields render as `n/a` rather than
breaking the report. Requested on demand only — it is not part of the
scheduled 07:00/19:00 briefing, so it adds no load there.

- `TICKER_INFO_CACHE_TTL_SECONDS` - cache duration for yfinance `.info` (default `21600`, 6h — fundamentals move far slower than price)
- `TICKER_ANALYSIS_SUMMARY_MAX_CHARS` - business-summary truncation length (default `500`)

### `/performance`

Whether the trade screener's own past qualifying picks actually paid off:
for each symbol with a qualifying session roughly `TRADE_OUTCOME_LOOKBACK_DAYS`
sessions ago (default `5`) that has been screened again since, it reports the
forward price move, plus an aggregate hit rate and average return. Computed
on demand from the trade signal history (SQLite) above — no yfinance calls
— since the screener already records every session's outcome as it runs;
there is nothing to pre-materialize with a separate scheduled job.

This is best-effort: it only sees a symbol's outcome if that symbol was
screened both on its qualifying day and again near today. Configured
watchlist symbols are screened every session, and discovered movers are
carried forward for `TRADE_STICKY_UNIVERSE_DAYS` after they are first seen
(see [Which symbols get screened](#which-symbols-get-screened)) — without
that carry-forward a discovered mover was measured exactly once and could
never produce an outcome at all.

- `TRADE_OUTCOME_LOOKBACK_DAYS` - sessions after qualifying to evaluate the outcome (default `5`)

## Slack Integration

Set `SLACK_WEBHOOK_URL` to a Slack [incoming webhook](https://api.slack.com/messaging/webhooks)
URL to mirror every outbound message — scheduled briefings, the health ping,
and every `/command` reply above — to a Slack channel alongside Telegram.
Leave it blank (the default) to disable Slack entirely; nothing changes for
Telegram-only setups.

Under docker compose, setting it in `.env` is not enough on its own — the
variable must also be listed in the `news-notifier` service's `environment:`
block in `docker-compose.yml` (it is). The image ships no `.env` of its own,
so any variable missing from that list is simply unset in the container, and
an unset webhook makes the mirror return early *without logging anything* —
the symptom is Slack silently never receiving messages while Telegram works
fine. `/health` reports `Slack mirror: configured` / `not configured`, which
is the quickest way to tell which side of this you are on.

This is a one-way broadcast, not a second control surface: an incoming
webhook can only *post* to Slack, so `/commands` still have to be sent from
Telegram — there is no Slack-side equivalent of `/now`, `/analyze`, etc.

Telegram's HTML formatting (`<b>`, `<i>`, `<pre>`, links) is converted to
Slack's own `mrkdwn` syntax (`*bold*`, `_italic_`, `` ```code``` ``,
`<url|text>`) before sending, and long messages are chunked the same way
Telegram messages are, using `SLACK_MESSAGE_MAX_CHARS`. A Slack send failure
is logged and does not affect Telegram delivery, headline dedup, or the
scheduled jobs' success/failure status — Telegram remains the source of
truth for all of that; Slack is best-effort.

- `SLACK_WEBHOOK_URL` - Slack incoming webhook URL (default empty — disabled)
- `SLACK_MESSAGE_MAX_CHARS` - chunk size per Slack message (default `3900`)

## Testing

Run unit tests:

```bash
source .venv/bin/activate
python -m unittest -v
```

CI runs this same test suite on push/PR via `.github/workflows/ci.yml`.

## Docker

Build and run with Compose:

```bash
docker compose up -d --build
```

Ensure environment values are provided in your shell or an env file before launch.

## Message Sections

Each briefing includes:

1. Time-based greeting (`Hello, <RECIPIENT_NAME>!`)
2. Morning quote (morning runs only)
3. Early Morning Norway News
4. Global News
5. Top Business Stories
6. Market Watch (live stock movers table)
7. Mutual Funds/ETFs (live movers table)
8. Short-Term Trade Candidates (informational table)

## Important Notes

- **No financial guarantees:** The screener is informational only and does not guarantee profits.
- **Market data coverage varies:** screener and RSS availability can differ by region/time.
- **Mutual fund availability:** Norway mutual fund coverage on Yahoo is limited; ETFs are used where needed.
- **Message splitting:** long messages are automatically split into multiple Telegram parts.

## Troubleshooting

- **`chat not found`**
  - Send `/start` to the bot first (or add it to the target group/channel with permissions).
- **No news returned**
  - Validate RSS feed URLs and domain allow/block filters.
- **No finance rows**
  - Some screener IDs may be rate-limited or empty; adjust `STOCK_SCREENERS` / `FUND_SCREENERS`.
  - If Yahoo TLS is unstable in your host network, reduce `SCREENER_REQUEST_TIMEOUT_SECONDS` and rely on cooldown to keep `/now` responsive.
  - Set `FINNHUB_API_KEY` to improve quote freshness and resilience for stock/ETF rows.
- **Network/proxy failures**
  - Check outbound connectivity to `api.telegram.org`, configured RSS sources, and Yahoo endpoints.

## Security

- Do not commit `.env`.
- Keep bot tokens and API keys private.
- Rotate credentials if accidentally exposed.

## License

Internal/private project unless a license is explicitly added.
