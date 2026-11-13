# Independent audit: verified findings

Audit date: 2026-11-13. Repository at `d0def12`. The Pi `~/news_bot` checkout is also at `d0def12`.

## Evidence types used in this document

| Tag | Meaning | Source |
|---|---|---|
| **R** | Repository | Code and config files, read at `d0def12` |
| **T** | Test | Runs in a scratchpad venv (Python 3.15): `ruff check news_bot.py newsbot tests` passed. `python -W error::ResourceWarning -m coverage run -m unittest` ran 936 tests, all OK, with 96% coverage of `newsbot`. One `ResourceWarning: unclosed database` appeared at interpreter shutdown; it did not fail the run. |
| **H** | Historical research | `docs/research/2026-10-analytics-findings.md`, read as written and not re-run |
| **L** | Live deployment | Read-only SSH to `tickmake@raspberrypi5-02`: selected non-secret `.env` keys, `docker ps`, `ss -tln`, `ufw status`, and a read-only SQLite query of `signal_archive` through a `:ro` volume mount |
| **X** | Reproduction | Scratchpad scripts `repro.py` and `ind.py` that call the repo's own functions with the live settings |
| **A** | Assumption | Labelled where it is used |

## Corrections to the brief's premises

- **Messaging and AI:** the bot is controlled through Telegram (`newsbot/bot.py` `run_command_long_polling_loop`). Slack is a send-only copy. The only AI is local Ollama `qwen2.5:3b`, which ranks headlines (`newsbot/news_feeds.py`).
- **Markets in the code:** India (`.NS`/`.BO`) is still in `evaluation_universe.csv` and in `config.py`. On the Pi, `INDIA_STOCK_UNIVERSE` is empty.

## The nine hypotheses

### 1. "The screening combinations did not establish an edge"

**Status: confirmed. The project's own research says this.**

- **H:**
  - §6 of the research: none of the 3,456 gate settings is positive in both development and holdout at any hold length.
  - §1–4: factors and earnings drift do not survive.
  - §10: no company score passes the pass rules that were written before the run.
- **R:** the research pipeline is sound in several ways:
  - It enters at the next open (`backtest.py` `_walk_forward_backtest`).
  - It uses a 70/30 development/holdout split.
  - Its pass rules were fixed before the run (§10).
- **Counter-evidence:** none found.
- **Caveat:** all of it is survivor-only data (§5), which would *flatter* the signals. So a negative result is if anything understated.
- **Severity:** high for the product. Any "BUY" framing is unsupported. The bot already uses tiers, not a ranking.

### 2. "Costs are too optimistic"

**Status: confirmed.**

- **R:** `config.py` `trade_backtest_cost_bps = 10.0` is applied to every market by:
  - `backtest.py` `_walk_forward_backtest`: `transaction_cost_pct = 2*bps/100`
  - `forward_test.py` `forward_outcome`
  - `history_store.py` (published picks)
  - the dashboard, which shows the stored `net_return_pct`

  None of these include a minimum fee, spread, slippage or FX cost.
- **L:** the `.env` does not override it, so the forward test runs at 10 bps.
- **X:** with the Pi's sizing (`RISK_ACCOUNT_SIZE=50000`, 25% cap), positions are 7.4k–12.5k NOK. A DNB round trip (2×max(59 NOK, 0.12%)) then costs **0.94–1.59%**, against the **0.20%** modelled. US and EU names also pay a currency exchange fee that is never quantified (`risk.fee_line` only prints the text "+ currency exchange fee").
- **Counter-evidence:**
  - `risk.fee_line` and `/check` (`trade_check.py`) show the real DNB fee.
  - Research §8–10 uses 59 NOK on ~12k NOK.
  - The dashboard caption admits that real fees are higher.

  So the *owner-facing advice* is fee-aware. The *performance numbers* are not.
- **Severity:** P1. Forward-test and pick-performance net returns overstate results by about 0.7–1.4 pp per trade. That is larger than every edge measured in the research.

### 3. "The 3×ATR stop does not match the gap-risk research"

**Status: confirmed for the live system. Incorrect for the repo defaults.**

- **R:** the code defaults agree with the research: `config.py` `risk_stop_atr_multiple = 2.0` and `research/gap_risk.py` `STOP_ATR_MULTIPLE = 2.0`.
- **L:** the Pi's `.env` sets `RISK_STOP_ATR_MULTIPLE=3.0`, `RISK_PER_TRADE_PCT=2.0`, `RISK_MAX_POSITION_PCT=25` and `RISK_MAX_TOTAL_PCT=100`.
- **R:** `risk.py:402` prints `"{multiple}×ATR stop. {GAP_RISK_NOTE}"`. So live output reads "3×ATR stop. About one stop in seven gaps through…". Those numbers were measured on a 2×ATR, ~4% stop (`risk.py:27-38`, research §7). The 3×ATR gap-through rate and overshoot have never been measured.
- **R:** research §8 *recommended* 3×ATR at 0.5% risk. The live risk is 2%, four times the 0.5% research §8 assumed.
- **Severity:** P0 (misleading risk text). The number is stated as measured fact for a stop it was not measured on.

#### 3b. New finding: the stop is anchored to the signal close, not the fill

**Status: confirmed.**

- **R:** `risk.size_position` sets `stop_price = last_close*(1-stop_pct)`. Every backtest and the forward test enter at the *next open*.
- **X:** with the signal close at 100, ATR 2% and the next open at 101, the stated stop distance is 6.0% but the real one is 6.93%. The real risk is about 15% larger than stated.
- **Severity:** P1.

#### 3c. New finding: a gap through the stop is reported only as a sentence

**Status: confirmed.**

- **X:** a 12.5k NOK position with its stop at 94 and an open at 85 loses 1,875 NOK, which is **3.75%** of the account. The stated risk is 1.50%. Fees add another 118 NOK.
- **R:** nothing computes a gap-adjusted or fee-adjusted loss next to the theoretical one.
- **Severity:** P1.

### 4. "The universes differ"

**Status: confirmed.** There are four distinct universes:

| Universe | Where it is defined | Contents |
|---|---|---|
| Live screen | `screener._build_candidate_universe` | The Pi's env lists (US 6, NO 5, EU 4), plus sticky symbols, plus Yahoo discovery, capped at 30 |
| Backtest | `configured` (env lists), or `broad-v1` = `evaluation_universe.csv` | `evaluation_universe.csv` is 44 stocks, including 11 Indian |
| Research and scores | `stock_list.csv` | 97 stocks: 41 NO, 46 EU, 8 US |
| Gate research | `fetch_basket.py` | `evaluation_universe.csv` |

- **L:** the archive is 72% US rows (786 of 1,094), even though the env lists are mostly European. Discovery adds US names.
- **Survivorship:** there is no correction anywhere in the app. §5 measures how big the gap is.
- **Severity:**
  - P1: research results may not describe what the live screen picks.
  - P2: India is still in the research basket although it is out of scope.

#### 4b. New finding: the hold length is inconsistent, and the forward test switched horizon mid-stream

**Status: confirmed.**

- **R:** `TRADE_OUTCOME_LOOKBACK_DAYS` defaults to 5 in `config.py` and in compose, but is 20 in `.env.example` and in the research.
- **L:** the Pi `.env` sets it to 20. The archive shows the switch:

| `rule_id` | Horizon | Rows | Signal dates | Settled | First date | Last date |
|---|---|---|---|---|---|---|
| `mom1-day0-atr4.5-dd8-vol1.2` | 5 | 1,036 | 28 | 924 | 2026-10-02 | 2026-11-10 |
| `mom1-day0-atr4.5-dd8-vol1.2` | 20 | 58 | 3 | 0 | 2026-11-10 | 2026-11-12 |

- The `rule_id` does not include the horizon, so the two series share an ID. `forward_test` filters on both, so they are not mixed. But the 5-session series measures the hold that research §8 shows loses most to fees.
- **Severity:** P1.

### 5. "The forward test is not mature"

**Status: confirmed.**

- **L:** at the horizon actually used now (20 sessions), there are 3 signal dates and 0 settled outcomes. At 5 sessions there are 28 dates. With 5-session holds those collapse to about 6 non-overlapping windows. Qualified and settled rows: US 153, NO 36, EU 34.
- **R:** `forward_test.MIN_PAIRED_DATES = 60` with equal date weights, no correction for overlap and no per-market minimum.
- **Arithmetic:** at 20 sessions, 60 non-overlapping dates take about 4.6 years. 60 overlapping dates take about 3 months but carry about 3 independent observations.
- **Severity:** P1. The 60-date gate can declare a result from heavily overlapping data.

### 6. "US and Norwegian fundamentals differ in quality"

**Status: confirmed.**

- **R:** `company_numbers.py`:
  - **NO/EU:** ESEF/IFRS data from filings.xbrl.org, with a real filing date. `known_from = min(added, year_end+120d)`. Taking the minimum can **back-date** a late filing to year end + 120 days, which is a look-ahead risk.
  - **US:** Yahoo only, `known_from = year_end+120d` as a guess, with no EDGAR filing date.
- **R:** `score_inputs.build` rewrites history with `INSERT OR REPLACE` using today's (restated) Yahoo values. The dates are lagged but the values are not point-in-time.
- **R:** only annual data, starting with the 2020 accounts.
- **Severity:** P1 for any score research. There is low live impact because the scores are descriptive only (§10).

### 7. "Macro and sentiment are incomplete"

**Status: confirmed. Both are absent, not incomplete.**

- **R:** `reports.py` says there is no macro data. `catalysts.py` says it is "not a sentiment score". Ollama only orders headlines; it does not produce a score.
- **Counter-point:** this is *safer* than an unvalidated sentiment number.
- **Severity:** P2/P3. It becomes a defect only if presented as present.

### 8. "The docs are inconsistent"

**Status: confirmed.**

- **R:** the README says the Docker image runs Python 3.11. Both Dockerfiles use `python:3.15-slim`, and CI tests 3.11 and 3.14 only.
- **R:** the README scheduling section lists 2 jobs; there are 13. The README command list misses `/check`, `/fill`, `/fills`, `/unfill`, `/deposit`, `/portfolio` and `/scorecard`. The project-structure list misses about 20 modules.
- **R:** `PROJECT_MEMORY.md` is dated 2026-10-03 and predates the scores, fills, dashboard and auto-deploy.
- **R:** compose passes misspelled duplicate variables (`FREEN_EWS_API_KEY`, `FINHUB_API_KEY`).
- **R:** `.coveragerc.bak` is committed.
- **Severity:** P2. The Python 3.15 vs CI gap is P1-adjacent, because the production interpreter is never unit-tested.

### 9. "Dashboard security"

**Status: confirmed.**

- **R:** compose publishes `"${DASHBOARD_STREAMLIT_PORT:-8002}:8501"` with no host IP and no authentication. The dashboard shows the real trades, deposits and fees from `fills.py` tables.
- **L:** the port listens on `0.0.0.0:8002` and `[::]:8002`. The Pi has two networks: `eth0` 10.1.1.5/24 and `wlan0` 192.168.6.22/22 with a global IPv6 address.
  - `ufw` is active and allows only 22/80/443, but Docker-published ports bypass ufw's INPUT chain. So 8002 is reachable from both networks.
  - Portainer (8000/9443) and poster-bot (8080) are exposed in the same way. They are outside this repo, so this is noted only.
  - Ollama listens on `*:11434` on the host, run as root.
- **Unverified:**
  - whether the router forwards these ports or the IPv6 firewall blocks inbound traffic (needs `nft`/`ip6tables` as root and the router config);
  - who else is on `wlan0` 192.168.4.0/22.
- **Severity:** P1. Private financial data is unauthenticated on a /22 Wi-Fi network and on a global IPv6 address.

## Other observations

- **Indicators (X):**
  - `indicators._compute_atr_percent` equals an independent 14-bar *simple* mean of True Range (2.496052 vs 2.496052). Wilder's ATR gives 2.6276.
  - `_compute_rsi_14` uses SMA (48.96). Wilder's RSI gives 42.43.
  - These are internally consistent and documented in the code, but differ from charting platforms. P3: label them.
- **Ollama on the Pi:** it is a shared container named `ollama`, not the compose `news_bot_ollama`. Ranking takes 106–209 s per section (from memory notes; not re-measured).

## Unknowns (not inspected)

- Router and IPv6 firewall rules.
- Who can reach `wlan0`.
- DNB's actual FX spread for USD and EUR.
- Real bid/ask spreads on Oslo small caps.
- Whether the Yahoo split and dividend adjustment matches across `.OL` and US data (not reproduced).
- Exchange calendars: the code uses its own handling (`_drop_unfinished_bar`); no `exchange_calendars` cross-check was run.
- The research was not re-run. Its numbers are taken as documented.
