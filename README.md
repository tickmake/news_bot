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
- **Telegram command support** (`/now`, `/news`, `/recommendations`, `/stocks`, `/watchlist`, `/analyze`, `/performance`, `/diagnostics`, `/pickperformance`, `/forwardtest`, `/health`).
- **Optional Slack mirror** of every outbound message via an incoming webhook, alongside Telegram.
- **Health ping** support for runtime monitoring.
- **CI test workflow** via GitHub Actions.

## Project Structure

- `news_bot.py` - entry point (`python news_bot.py`); the code lives in `newsbot/`
- `newsbot/` - the application, one module per concern:
  - `config`, `state`, `logsetup`, `util` - settings, persisted state and caches, banded logging, shared helpers
  - `messaging` - Telegram and Slack delivery
  - `news_feeds`, `market_data` - news collection and ranking; live quotes and price history
  - `indicators`, `ranking`, `benchmarks`, `history_store`, `screener`, `charts` - the trade screener
  - `backtest`, `backtest_reports` - the walk-forward engine and the `/performance` and `/diagnostics` reports
  - `reports`, `briefing`, `commands`, `bot` - weekly picks and analysis, the briefing and scheduled jobs, command routing and polling, startup
- `tests/` - unit tests (`python -m unittest`)
- `evaluation_universe.csv` - the fixed `broad-v1` evaluation basket
- `requirements.txt` - Python dependencies
- `Dockerfile` - container image definition
- `docker-compose.yml` - service orchestration
- `streamlit/` - the web dashboard, Fjord Signals (Streamlit + Plotly): every page is in `streamlit/app.py`

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
- `NORWAY_NEWS_FEEDS` - comma-separated RSS feed URLs for Norway-focused news (English-language by default)
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
| `BRIEFING_BUDGET_SECONDS` | `900` | Time allowed to compose a briefing; news sections not yet started when it runs out are skipped |
| `NEWS_RANKER_TIMEOUT_SECONDS` | `240` | Per-call timeout |
| `RANKER_NETWORK` | bundled net | Network carrying Ollama |
| `RANKER_NETWORK_EXTERNAL` | `false` | Set `true` to reuse another project's network |
| `NEWS_MAX_AGE_HOURS` | `30` | Freshness ceiling; undated items exempt |
| `NORWAY_NEWS_MAX_AGE_HOURS` | `96` | Same, for the Norway section, whose English outlets publish less often |
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

Tier 2 exists because `/stocks` reads *recorded screening history* rather than
live prices, so it can only speak about symbols re-screened across several
sessions. Discovery alone returns a near-different set of movers every day, so
before this tier a discovered mover was measured once and never looked at
again.

- `TRADE_STICKY_UNIVERSE_DAYS` - how long a seen symbol is carried forward (default `10`)
- `TRADE_DISCOVERY_MIN_SLOTS` - slots always reserved for fresh discovery (default `10`)

Screener names are **discovery only** — they decide which symbols are looked
at, never whether one qualifies. That stays with the five gates below.

### Trade Signal History

Every screening run records what it measured for every symbol it looked at —
qualified or not — in a local SQLite file (stdlib `sqlite3`, no extra
dependency). This is separate from the yfinance history cache above: it's a
rolling memory of the screener's own daily output, used to see streaks (e.g.
a symbol qualifying several sessions running) and weekly picks. `/performance`
uses historical daily bars for a separate walk-forward test.
Successfully delivered candidate lists are also recorded in a separate,
non-pruned table in the same DB for `/pickperformance`; keep this volume backed up.
Every screened symbol is also archived, permanently and once per session (first
observation wins, qualified or not, with its delivered rank and the gate
thresholds it was produced under), for `/forwardtest`.

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
  that, as `rising`, `falling`, `flat`, or `unknown` when data is insufficient.
- **Session streak** — how many recent sessions running (today included)
  the symbol has qualified, once that's 2 or more; backed by the trade
  signal history above.
- A **zone / invalidation / reward:risk** line derived from the same
  20-session support/resistance range the drawdown gate uses: the
  pullback zone between the recent low and the 20-day EMA, the level a
  close below which invalidates the setup, and the resulting reward-to-risk
  ratio. Technical-only, not a recommendation — see the disclaimer below.

Relative volume compares the last completed session with the prior 20
sessions, excluding that session from its own baseline. If usable volume is
unavailable, the candidate displays `n/a` and `unknown`, and sorts as
unconfirmed rather than assuming a neutral `1.0×` ratio.

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
- `/recommendations` - send today's live trade screener candidate picks, with technical-analysis charts for the top few — no news, no live quote tables
- `/stocks` - send this week's top picks, ranked by how consistently they've qualified — see below
- `/watchlist` - send every configured watchlist symbol with today's read and its verdict — see below
- `/analyze TICKER` - send a deep-dive report for one symbol, e.g. `/analyze AAPL`
- `/performance` - run a walk-forward test of current trade rules against market benchmarks
- `/diagnostics` - break down historical signal outcomes and compare ranking on matching dates
- `/pickperformance` - evaluate fixed-session outcomes of successfully delivered stock picks
- `/health` - send runtime health report
- `/check TICKER AMOUNT_NOK` - before placing an order, e.g. `/check EQNR.OL 12000`: shares and cost in NOK, the DNB fee and the rise needed to cover it, whether it may go in an Aksjesparekonto, earnings and ex-dividend dates in the next 4 weeks, overlap (same sector or moving together) with what `/fill` says you hold, the volatility stop's loss in NOK, how much it can fall over 2 and 4 weeks, and its share of your money. Telegram only. Information, never a buy or sell verdict

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
  re-screen — for a view aggregated over the week, see `/stocks` below. It
  also attaches technical-analysis charts for the top candidates; see below.

> **A screen, not a signal.** The candidates are stocks that pass a set of
> price-based gates today. Walk-forward testing (`/performance`) on the fixed
> `broad-v1` basket found no measurable edge over simply holding a liquid
> stock, and eight alternative gate sets showed none either (issue #12). Use
> the list to decide what to look at, not what to buy. The test covers about
> two years, one market regime, a survivorship-biased basket and 5-session
> holds, so it cannot rule out small effects or other horizons.

#### Charts

`/recommendations` follows its text with a single PNG covering the top
`TRADE_CHART_COUNT` candidates (default `3`). Each gets two stacked panels:

- **Price** — close, EMA20, the 20-session support/resistance band the
  drawdown and entry/exit sketch are both measured against, and volume bars
  (green/red by direction) compressed along the bottom as context.
- **RSI 14** — with the 30 and 70 lines marked.

The chart is rendered locally with matplotlib and uploaded through Telegram's
`sendPhoto`, which takes a multipart upload — so the image never has to be
reachable from the internet and no port needs exposing.

**Both channels, by different routes.** Telegram takes the multipart
`sendPhoto` above. Slack cannot: `_slack_post` is an incoming webhook, and a
webhook can only post text. Slack's Web API upload endpoints handle it
instead — still ordinary outbound HTTPS calls, so no port is exposed and no
Socket Mode is involved, but they authenticate with a **bot token** rather
than a webhook URL. See [Chart uploads to Slack](#chart-uploads-to-slack)
below. With no bot token configured, Slack keeps receiving the text only and
nothing else changes. Set `TRADE_CHART_ENABLED=false` for text everywhere.

matplotlib is imported lazily inside `render_candidate_charts`, so an install
without it — or any drawing failure — loses the charts and nothing else: the
text always sends first, and never depends on the image.

- `TRADE_CHART_ENABLED` - attach charts to `/recommendations` (default `true`)
- `TRADE_CHART_COUNT` - how many top candidates to chart (default `3`)
- `TRADE_CHART_LOOKBACK_DAYS` - sessions drawn per chart (default `90`)

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
window — a one-off qualifying day is filtered out as noise. This is a pure
SQLite read (no fresh yfinance calls), so it needs a few days of the screener
actually running before it has anything to rank; check `/recommendations` or
`/watchlist` in the meantime.

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

Runs an on-demand walk-forward backtest of the current gates and ranking rules
over split- and dividend-adjusted historical daily bars for the **evaluation
stock universe** (fund lists are excluded). By default this uses the currently
configured stock lists; the opt-in fixed basket below broadens coverage without
changing live picks. A signal is measured at a
completed close, entered at the next session's open, and exited at the close after
`TRADE_OUTCOME_LOOKBACK_DAYS` market sessions. All liquid qualifiers are
evaluated alongside the top five ranked on each signal date, so you can assess
whether ranking adds value. The live recommendation rules remain unchanged.

**Development versus holdout:** `TRADE_BACKTEST_HOLDOUT_START` sets a fixed
`YYYY-MM-DD` boundary. When blank, the last 25% of available signal dates are
reserved automatically, without consulting returns. Freeze the displayed
boundary before tuning; the automatic date moves as new data arrives. Only
development trades whose exits are strictly before the boundary count, so a
development return never uses holdout prices. Holdout indicators can still use
earlier bars for warm-up. Trades without enough future sessions are counted as
pending, not treated as wins or losses. No parameter optimization happens
automatically, and a date split cannot guarantee you have never inspected it.

**Historical liquidity:** the evaluation requires at least
`TRADE_BACKTEST_MIN_AVG_VOLUME` shares/day averaged over the 20 sessions *before*
each signal (default `100000`). Missing baseline data fails that check; `0`
disables it. This is a currency-independent volume proxy, not a guarantee of
low spreads, adequate turnover, or executable fills. Use a stable, representative
stock list chosen before tuning; today's discovery movers are not reconstructed
as a historical universe. Misclassified funds placed in a stock list are still
the user's responsibility.

Returns are adjusted by an estimated round-trip transaction cost, calculated
as twice `TRADE_BACKTEST_COST_BPS`. Benchmark returns and average excess return
are reported only when matching benchmark bars exist for the stock's entry and
exit dates. The default benchmark map uses broad exchange indices, with `SPY`
for symbols without an exchange suffix. Customize `TRADE_BACKTEST_BENCHMARKS`
to match the markets in your watchlist; unknown suffixes, invalid benchmark
symbols, or unavailable bars reduce benchmark coverage rather than silently
substituting an unrelated series. Index proxies can differ in dividend
treatment; use a suitable total-return or adjusted ETF benchmark where possible.

The report separates development/holdout results for all qualifiers and the top
five. It includes mean and median net return, average win/loss, worst net
return, and average/worst adverse price excursion from entry to the lowest
intraday low during the hold. Excursions are price-only and do not simulate
stops. Holdout results are also broken down by benchmark market and signal
quarter, including benchmark coverage. Excess return is calculated only on the
matched subset; unmatched trades are not counted as benchmark wins.

**Baseline:** the report also holds *every* liquid stock on every signal date
with no gates, using the same next-open entry, fixed-session exit, costs,
holdout purge and benchmark matching. It then pairs the screener against that
baseline by signal date (each date weighted equally), for all qualifiers and
for the top picks. This answers whether the gates beat simply holding a liquid
stock, which the average return alone cannot. The comparison of the top picks
against the other *qualifiers* lives in `/diagnostics`.

**Validate before tuning:** the same report also evaluates unchanged picks in
calendar-quarter blocks at the configured cost, plus 15 and 40 bps per side
(defaults: 10, 25, and 50 bps per side). Costs are recalculated from gross returns;
signals, ranking, liquidity, and holding horizon are identical across scenarios.
This section excludes holds whose exits reach a later quarter, in addition to
the existing development/holdout purge, so block outcomes do not use a later
quarter's prices. The original overall results remain unchanged. Each block
shows dates, stock/signal counts, net return and matched benchmark excess for
all qualifiers and the top five. Partial quarters and unavailable benchmark
matches must not be interpreted as full-period or whole-universe performance.

These are descriptive chronological blocks, not independent cross-validation
folds, a parameter search, or a profitability pass/fail test. Inspect consistency
across periods and cost assumptions before proposing a rule change. The
April-September 2026 holdout used during development has already been inspected;
it is exploratory evidence for subsequent changes, not fresh validation.
Nothing in this evaluation changes live recommendation thresholds.

This is a validation aid, not a portfolio simulation: trades overlap and the
per-trade averages do not represent an investable equity curve. Testing the
current watchlist also has survivorship and universe-selection bias, and the
cost assumption may not match actual spreads, fees, taxes, or slippage. Do not
tune thresholds and judge them on the same sample; reserve a later period for
out-of-sample validation. Trade counts are not independent sample sizes; the
report also shows distinct signal dates and stocks, and avoids misleading
independent-trade confidence intervals. The assumed next-open fills are not a
simulation of the bot's actual briefing delivery time.

- `TRADE_OUTCOME_LOOKBACK_DAYS` - backward-compatible setting name for the
  holding horizon in trading sessions (default `5`)
- `TRADE_BACKTEST_PERIOD` - historical bars fetched for the backtest (default `2y`)
- `TRADE_BACKTEST_UNIVERSE` - `configured` (default) or the fixed `broad-v1` basket
- `TRADE_BACKTEST_UNIVERSE_MAX` - evaluation-only symbol limit (default `100`);
  replaces use of the live `TRADE_UNIVERSE_MAX` cap in historical evaluation
- `TRADE_BACKTEST_COST_BPS` - estimated cost per side in basis points (default `10`)
- `TRADE_BACKTEST_HOLDOUT_START` - fixed holdout boundary, `YYYY-MM-DD`; blank
  reserves the final 25% of available signal dates
- `TRADE_BACKTEST_MIN_AVG_VOLUME` - minimum prior 20-session average share
  volume for evaluation (default `100000`; `0` disables)
- `TRADE_BACKTEST_BENCHMARKS` - comma-separated `suffix:ticker` mappings and
  `DEFAULT:ticker` fallback (default uses SPY and common exchange indices)

### Broader evaluation universe

Set `TRADE_BACKTEST_UNIVERSE=broad-v1` and restart to evaluate the fixed basket in
[evaluation_universe.csv](./evaluation_universe.csv) with `/performance` and
`/diagnostics`. The basket was frozen on **2026-10-02** with 44 single-listed
stocks: 11 US, 14 continental European, 11 Indian, and 8 Norwegian names.
It spans 11 broad sector labels. The US and India portions each include one
name per sector; Europe retains the original 14 names, and Norway adds
energy, banking, telecom, food, materials, and industrial exposure. Sector
labels are descriptive manual classifications, not a maintained index feed.

Names were chosen as a small, recognizable cross-market basket, not selected by
backtest returns. This is neither an exhaustive nor statistically representative
universe, and not every market has every sector. It does **not** reconstruct
historical index membership, include delisted stocks, or eliminate survivorship
and discretionary-selection bias. Keep this manifest fixed during experiments;
version a changed basket rather than removing stocks because they lost money.
Funds and duplicate listings are not intentionally included. Missing or invalid
price histories are disclosed, never replaced by retrospectively chosen winners.

Both reports show usable/tested/configured coverage by market and sector,
missing stock histories, unusable stock histories, and missing benchmark
symbols. `/performance` additionally breaks holdout returns down by the basket's
markets and sectors. All markets compete in one top-five ranking; these are
per-signal local-price returns, **not** a currency-converted global portfolio.
Exchange holidays can differ. The historical liquidity filter still applies
per signal and is a shares/day proxy, not a cross-market turnover measure.
Global ranks compare exchange-local calendar dates, not synchronized UTC closes;
they are not an executable cross-market allocation simulation.

The basket does not alter `/recommendations`, `/stocks`, `/watchlist`, discovery,
or the delivered-pick ledger. No new dependencies are needed; the Docker image
includes the manifest. The independent evaluation cap defaults to 100 so the
44-stock basket is not cut down by the live screener's default cap of 30.
If you lower it, selection follows manifest order and omitted counts/coverage
are shown; do not treat a truncated basket as the full four-market test.
Benchmark mappings remain configurable. An unavailable Italian benchmark is
reported rather than silently substituted; choose a verified suitable proxy
before interpreting benchmark-relative results.

### `/diagnostics`

Uses the same historical evaluation, liquidity filter, costs, universe, and
development/holdout purge as `/performance`, without changing live gates or
ranking. This is **not** a report of delivered recommendations.

For each cohort, it reports all completed qualifiers grouped by exact rank,
five-session momentum (`<3%`, `3-6%`, `>=6%`), volume confirmation (the configured
`TRADE_MIN_VOLUME_RATIO`, with unknown separate), RSI (`<70`, `>=70`, unknown),
ATR (`<2%`, `>=2%`), qualifying streak (`1`, `2-3`, `>=4`), stock, and quarter.
Metrics and streaks are captured at signal time using only earlier/current bars.
These broad diagnostic buckets are fixed descriptions, not new selection
thresholds or an automatic parameter search. Each slice shows signal/date/stock
counts, mean/median net return, hit rate, average win/loss, average/worst adverse
excursion, and matched benchmark excess/coverage.

The paired ranking comparison first computes the average return of the top five
minus that of the non-selected qualifiers **on each common signal date**, then
averages those differences with equal weight per date. Positive values favor
selected picks. It excludes observed dates without both groups or without outcomes
for every liquid qualifier (including pending or purged outcomes); it never
backfills a missing selected pick with a lower-ranked stock. Benchmark-relative
pairs additionally require matching benchmark returns for every qualifier on the
date. Dates with no completed outcomes do not appear in this comparison; global
pending/purge counts are shown separately.

Groups overlap and indicators are correlated. A good-looking slice, particularly
one containing few stocks or dates, is not proof of a profitable rule. Use stock
and quarter slices to spot concentration, and development data to formulate a
small number of hypotheses. An already inspected holdout is exploratory evidence,
not untouched validation for changes motivated by these results. The command does
not tune rules or claim statistical significance. Large universes can produce
multiple Telegram message parts.

### Risk analytics and event risk

Every backtest so far (issue #12, plus a ten-year check of momentum, reversal,
low-volatility, 52-week-high and earnings-surprise signals on the evaluation
basket) found no edge over holding the same stocks, and the bot's own gates did
worse than the average stock at a 10-session hold. A ranked list therefore
says less than it seems to. What holds up whatever the signal is how much to
risk, whether the picks are really one bet, and whether a known event sits inside
the holding period, so each candidate and `/analyze` now carry:

- **A volatility stop and a position size.** The stop is `RISK_STOP_ATR_MULTIPLE`
  (2) times the stock's ATR below the close. The size is
  `RISK_PER_TRADE_PCT` (0.5%) of the portfolio divided by that stop distance, so a
  calmer stock gets a larger position and a wilder one a smaller one, each losing
  the same fraction if the stop is hit. No position exceeds `RISK_MAX_POSITION_PCT`
  (20%). Set `RISK_ACCOUNT_SIZE` to see an amount too. This stop is separate from
  the "invalidation" level in the sketch, which is the 20-session low. A gap through
  the stop loses more than planned.
- **Whether it may go in an Aksjesparekonto** (#107): a `⛔ Not for ASK` line when
  `stock_list.csv` says `ask_allowed=no`, for any US listing, and `ASK: check first`
  for a stock outside the list.
- **How much it can fall over a 2-4 week hold** (`/analyze` and `/check`, #123): the
  1-in-20 bad 2 and 4 weeks over the last year, the biggest fall in 2 years, and the
  average move on the last 8 report days. From the saved `daily_prices`; Yahoo only
  for stocks outside the list. `RISK_FORECAST_ENABLED=false` turns it off.
- **An earnings flag** when the next report is within `RISK_EARNINGS_WARN_DAYS`
  (7) calendar days, which covers the default 5-session hold. Dates come from
  yfinance's calendar, not `.info` (whose earnings timestamp is often the previous
  report). A missing date means unknown, not "none".
- **A "Taken together" section** under the candidates: pairs whose last 60 daily
  returns correlate at `RISK_CORR_THRESHOLD` (0.7) or more (compared on shared
  trading dates, so US and European names line up), picks that share a sector, and
  the total at work and at risk if every stop is hit, with a warning above
  `RISK_MAX_TOTAL_PCT` (60%).

- **Relative strength and beta.** The stock's return over the last 20 and 60 completed
  sessions (`RELATIVE_STRENGTH_WINDOWS`) minus its market's, and for US listings
  minus its SPDR sector ETF's (Yahoo sector names map to XLK, XLE, XLF and so on),
  plus 60-session beta to the market. Markets are the same proxies `/performance`
  uses (SPY, OSEBX.OL, ^GDAXI, ^NSEI, ...). Non-US stocks get the market comparison
  only, since a US sector ETF would mostly measure the US. Returns are taken between
  the same dates, using the last close on or before each, so markets with different
  holidays still line up. This is context for reading a move (did it outrun its
  market, or just go along?), not a ranking: it does not change what qualifies.
  Set `RELATIVE_STRENGTH_ENABLED=false` to switch it off.

- **Headlines that mention the pick.** Up to `CATALYSTS_MAX_PER_PICK` (2) headlines from the
  last `CATALYSTS_MAX_AGE_HOURS` (36) hours, taken from the business and global feeds
  the briefing already reads (no new source or credential; the feed pool is cached
  20 minutes). A headline counts only if it names the company as a whole word (legal
  suffixes stripped, so `Apple Inc.` matches "Apple"; a multi-word name's first
  word is also tried when it is safe, so "Meta Platforms" matches "Meta") or writes the
  ticker the way news does, `$NVDA` or `(NVDA)`. Bare tickers are never matched, and
  names that are ordinary words ("Target", "Block", "Visa") are skipped, so it errs on the
  side of silence. It is there to answer "why is it moving?", not to score sentiment.

The extra lookups (sector, earnings date, six months of history for relative strength) run in parallel within
`RISK_CONTEXT_DEADLINE_SECONDS` and are cached for 12 hours; if they fail, the risk
lines for that pick are simply missing and the screen is unaffected. Switch it all
off with `RISK_ANALYTICS_ENABLED=false`.

### Briefing resilience

Each briefing section (Norway news, global news, business & markets, trade
candidates) is isolated: if one fails or the time budget runs out, the briefing
still goes out with a one-line notice in that section's place, and `/health`
shows `Last run status: partial: <sections>`. The trade screen runs alongside the
news sections. Only one briefing is composed at a time, so `/now` during a
scheduled run is told to wait instead of sending a duplicate. A command that a
restart cuts short is reported when the bot comes back up, with a prompt to send it
again.

### `/forwardtest`

The out-of-sample read. `/performance` replays history, so every rule it judges
was chosen with that data in view; `/forwardtest` judges only signals the bot
recorded live before their outcome existed (`signal_archive`, never pruned).
It settles any outcome that has become computable (close after
`TRADE_OUTCOME_LOOKBACK_DAYS` sessions, costs and benchmark frozen at
recording), then compares **qualified** and **top 5 as delivered** with
**every symbol screened**, paired by entry session with sessions weighted
equally, reporting the standard error and a rough t value.

**Entry is the first open after the signal was recorded.** A signal exists from
the moment the bot wrote it, not from the close it is dated: the 19:00 run
records yesterday's Oslo signal after today's Oslo session has opened, so that
row enters tomorrow. Opening times per exchange are in `newsbot/exchange_hours.py`;
a symbol on an exchange that is not listed there is never scored.

Rows recorded before this rule (2026-11) could be scored from an open that had
already happened. They are kept, classified, and shown separately, never mixed
into the test:

| Class | Meaning |
|---|---|
| clean | the entry open came after the signal was recorded |
| potentially contaminated | it came at or before (rows from before the fix only) |
| invalid | cannot be evaluated: unsupported exchange, broken timestamp, or settled by an older build |
| awaiting entry | the next session has not opened yet; "overdue" after 7 days |

Only clean, finished rows recorded under the new rule count toward the test.
The dashboard reads the same definition (the `signal_archive_evaluated` view),
so it cannot show a row the Telegram report leaves out.
`scripts/forward_test_integrity.py` fingerprints the archive before and after a
deploy and fails if any recorded signal or existing outcome changed. Details:
`docs/audit/forward-test-fix-plan.md`.

Signals made under different gate thresholds are never mixed: each row carries
a rule id, and only the current one is reported. Below 60 paired entry sessions
the report says it cannot conclude anything. The baseline is the bot's own
screened set (watchlist plus mover discovery, not liquidity-filtered), not the
whole market; holds overlap and averages are not portfolio returns. Nothing is
backfilled.

### Market-data alerting

The container healthcheck only proves the bot's threads are alive. If Yahoo
throttles the bot or changes an endpoint, every screen would come back empty
while the container stays "healthy". Each trade screen now records how many symbols
returned usable data and the date of the newest price, and `/health` shows it
(`Market data: ok / suspect / DEGRADED`).

A screen is **bad** when more than `MARKET_DATA_ALERT_RATIO` (default 0.5) of
the analysed symbols have no usable data, or the newest price session is more than
`MARKET_DATA_MAX_STALE_DAYS` (default 6) calendar days old. Screens of fewer than
5 symbols are ignored, so a slow run cut short by its deadline is not an outage.
Two consecutive bad screens send one Telegram alert (a total outage alerts at
once), a reminder follows every 24 hours while it persists, and one all-clear is
sent on recovery. Alerts go to `HEALTH_PING_CHAT_ID` (or the main chat) and are
mirrored to Slack. Set `MARKET_DATA_ALERTS_ENABLED=false` to record without
alerting.

### `/pickperformance`

Tracks the picks actually shown in successful **Telegram text deliveries** from
`/recommendations`, `/now`, and scheduled briefings. Failed sends, chart uploads,
Slack-only delivery, and other screening commands do not create ledger entries.
Only the displayed top five are recorded, using the same screening result as
the message. No past deliveries are inferred or backfilled from screening history.
Repeated delivery of the same stock and signal session counts once; the first
successful delivery fixes its UTC publication time, rank, metric/rule snapshot,
holding horizon, cost, and benchmark. Different signal sessions remain separate.
Persistence failures are logged explicitly; they cannot undo a delivered message.

Returns are **hypothetical post-publication outcomes, not executed trades**.
Adjusted daily bars are used to enter at the first session's open strictly after
the publication's exchange-local calendar date (and after the signal session).
The exit is the close of the frozen `TRADE_OUTCOME_LOOKBACK_DAYS`-th held session.
This conservative convention never reuses an open from before delivery, even if
the message arrived before that day's market open. The current day's bar is
excluded, so outcomes may appear one session late. Missing exchange timezones or
unusable prices are reported as unavailable; insufficient future bars are pending,
not losses. Provider data can still omit sessions or contain later adjustments.

The report covers the **latest 100 unique published signals**, with aggregate
mean/median net return, hit rate, matched benchmark coverage and excess return,
and details for up to ten completed signals. Costs subtract twice the frozen
`TRADE_BACKTEST_COST_BPS`. Benchmark returns use the same entry/exit dates; no
missing benchmark is treated as zero. Completed outcomes, including missing
benchmark matches, are stored once and not rewritten after setting changes or
data revisions. Pending/unavailable outcomes are retried on later reports.
Full available history is requested only for unevaluated symbols and reused
within a report and by the existing history cache.

The ledger shares `TRADE_HISTORY_DB_FILE` and the existing Docker data volume;
no new deployment settings or dependencies are needed. It is not removed by
`TRADE_HISTORY_RETENTION_DAYS`. Only the report window is capped, not ledger
retention. An unevaluated signal older than that window will no longer be
evaluated by this command. Overlapping picks are not independent observations
or a portfolio equity curve. `/performance` remains the historical current-rule
backtest; live recommendation thresholds are unchanged.

## Slack Integration

Set `SLACK_WEBHOOK_URL` to a Slack [incoming webhook](https://api.slack.com/messaging/webhooks)
URL to mirror every outbound message — scheduled briefings, the health ping,
and every `/command` reply above — to a Slack channel alongside Telegram.
Leave it blank (the default) to disable Slack entirely; nothing changes for
Telegram-only setups.

### Chart uploads to Slack

The incoming webhook can only post text. To get the `/recommendations` charts
into Slack as well, the bot needs the Web API:

1. Create a Slack app in the workspace (or reuse one).
2. Give its **bot token** the `files:write` scope, and install the app.
3. Invite the bot to the target channel — an uninvited bot gets
   `not_in_channel` and the upload fails.
4. Set `SLACK_BOT_TOKEN` (`xoxb-…`) and `SLACK_CHANNEL_ID` (the channel *ID*,
   e.g. `C0123ABCDEF`, not its name).

Leave either blank and chart upload is a silent no-op — Slack keeps receiving
the text rendering exactly as before.

This does **not** reopen the inbound-command question. Uploading a file is an
outbound call, so unlike Slash Commands or the Events API it needs no exposed
port and no Socket Mode connection; the bot still cannot *receive* anything
from Slack, and all commands are still triggered from Telegram.

Uploads use the three-step external flow (`files.getUploadURLExternal` → PUT
the bytes → `files.completeUploadExternal`), since `files.upload` was retired
in 2025. Slack answers application errors with HTTP 200 and `ok: false`, so
the client checks `ok` rather than trusting the status code — otherwise a
missing scope or an uninvited bot would read as success.

- `SLACK_BOT_TOKEN` - bot token with `files:write` (default empty, disabled)
- `SLACK_CHANNEL_ID` - channel ID to upload into (default empty, disabled)

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

CI (`.github/workflows/ci.yml`) runs on Python 3.11 (the Docker image) and 3.14:
ruff with the rules in `ruff.toml` (pyflakes plus bugbear, bandit security
checks and a few others; style-only families are left out), `pip-audit` on the
pinned requirements, and the suite under coverage with `ResourceWarning` treated as
an error. Coverage has a floor of 95% (`.coveragerc`); it is about 97% today, with
`benchmarks.py` and `history_store.py` (90%) the weakest. A separate job builds the Docker image and
checks the hardened runtime below.

## Docker

Build and run with Compose:

```bash
docker compose up -d --build
```

Ensure environment values are provided in your shell or an env file before launch.

**Hardening.** The base image is pinned by digest (Dependabot proposes updates)
and `.dockerignore` keeps `.env`, databases and tests out of the build context.
The bot runs as an unprivileged `app` user (uid 10001): `docker-entrypoint.sh`
starts as root only to `chown` the data volume, then drops privileges with
`setpriv`. That step is what lets a volume created by an older, root-running
image keep working. Compose also sets a read-only root filesystem (writable:
the `news_bot_data` volume and a `/tmp` tmpfs), drops every capability except
the three the entrypoint needs (which are gone once it has dropped to `app`), and
sets `no-new-privileges`. If you add a feature that writes elsewhere, give it a
path under `/app/data` rather than loosening `read_only`.

## Deploying to the Pi

On the Pi, from the checkout, one command:

```bash
cd ~/news_bot && scripts/deploy.sh
```

It stops at the first failure. In order, it:

1. tags the running bot image as `rollback-<date>-<commit>`;
2. copies `.env` and takes a checked database backup (as the app user);
3. runs `git pull --ff-only` and rebuilds the bot and the dashboard;
4. waits for the bot to be healthy, then shows the new commit and any log errors;
5. prints the one command that rolls back.

With nothing new on `main` it does nothing; `FORCE=1` rebuilds anyway.

## Backups off the Pi

The bot saves a checked copy of its database every night at 04:00 into
`TRADE_BACKUP_HOST_DIR` on the Pi. To survive losing the Pi itself, copy those
files to another machine with `scripts/pull_backups.sh`. It runs on the other
machine and *pulls*, so the Pi holds no key that could delete the copies. Each
new copy is checked with `PRAGMA integrity_check`, and the newest 60 are kept.

Install once on the receiving machine (`ssh raspberrypi5-02 true` must work):

```
crontab -e
30 5 * * * /path/to/news_bot/scripts/pull_backups.sh >> ~/news_bot_backups/pull.log 2>&1
```

## Dashboard

Fjord Signals, at `http://raspberrypi5-02:8002`: the scorecard (picks vs
skipped stocks), day by day, the latest screen, sent picks, a research page
per stock (price, key numbers, yearly company numbers, the bot's history with
it), data collected per day, your `/fill` trades and holdings, and a what-if
tool for the screen's filters. Built with Streamlit + Plotly; every page is in
`streamlit/app.py`. It reads the bot's database read-only, with queries cached
for a minute. No login, so keep the port on the home network / VPN.

Deploy on the Pi: `git pull && docker compose up -d --build dashboard-streamlit`.

To work on it locally, copy a backup DB next to the app and point the app at it:

```bash
cd streamlit
pip install -r requirements.txt
DASHBOARD_DB_FILE=~/news_bot_backups/<newest>.db streamlit run app.py
```

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
