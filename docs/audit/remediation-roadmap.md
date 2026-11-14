# Remediation roadmap

Each task can be done on its own. "Archive?" says whether the task changes historical or archived data.

## P0: misleading risk reporting

### P0-1: Gap-risk note does not match the live stop multiple

| | |
|---|---|
| **Files** | `newsbot/risk.py` (`GAP_RISK_NOTE`, the `portfolio_lines` text around :402), `research/gap_risk.py` |
| **Problem** | The live stop is 3×ATR (Pi `.env`), but the note quotes the 2×ATR measurements. |
| **Fix** | Make `gap_risk.py` read the multiple from an argument, rerun it at 3.0, and store the results per multiple. Print the note only when the stored multiple equals `SETTINGS.risk_stop_atr_multiple`. Otherwise print "gap risk not measured for N×ATR". |
| **Test** | With the multiple set to 3.0 and only 2.0 data stored, the output does not contain "one stop in seven". |
| **Done when** | The text always matches the measured multiple. |
| **Complexity** | S |
| **Archive?** | No |

### P0-2: No realised-loss line next to the stated risk

| | |
|---|---|
| **Files** | `newsbot/risk.py` (`size_position`, `PositionSizing`), `newsbot/trade_check.py` |
| **Problem** | Only the theoretical stop loss is shown. |
| **Fix** | Add `gap_loss_p90_pct` (from the P0-1 data) and the fee in account % next to `risk_pct`, in `/check` and in recommendations. |
| **Test** | A sizing at 2% ATR shows the theoretical loss, plus a gap p90, plus the fee. |
| **Depends on** | P0-1 |
| **Complexity** | S |
| **Archive?** | No |

## P1: costs, consistency, forward-test reliability, exposure

### P1-1: Flat 10 bps cost for every market

| | |
|---|---|
| **Files** | `newsbot/config.py`, new `newsbot/costs.py`, `backtest.py`, `forward_test.py`, `history_store.py`, `streamlit/app.py` |
| **Problem** | One flat 10 bps cost is applied to every market. |
| **Fix** | Add `CostModel(id, market, min_fee, pct, spread_bps, fx_pct)` with low, base and adverse scenarios. `forward_outcome` reports the stored-bps return plus a separately labelled `net_<model>` that recomputes the cost from the notional amount. |
| **Tests** | 12.5k NOK, `.OL`, base scenario gives 0.94% plus spread. A US symbol includes FX. A stored row is returned unchanged. |
| **Depends on** | D1 (broker and FX tariffs) |
| **Complexity** | M |
| **Archive?** | No. Existing rows keep `cost_bps_per_side=10`; recalculated figures are added alongside. |

### P1-2: Hold-length default disagrees, and the horizon is not in `rule_id`

| | |
|---|---|
| **Files** | `newsbot/config.py`, `docker-compose.yml:166`, `history_store.current_rule_id` |
| **Problem** | The hold defaults to 5 sessions in code but 20 in `.env.example`, the research and the Pi. `rule_id` does not include the horizon. |
| **Fix** | Set the default to 20 in both places. Add a `-h{N}` suffix to the rule id **for new rows only**. |
| **Test** | `current_rule_id()` contains `h20`. An existing archive row keeps its old id and is still reported. |
| **Complexity** | S |
| **Archive?** | No. Old ids are kept. |
| **Status** | Implemented on `fix/holding-horizon-rule-id`, not yet merged or deployed. Old gate-only ids are matched on their gates in `/forwardtest` and the dashboard, so they stay reported. See `holding-horizon-report.md`. |

### P1-3: Stop is anchored to the signal close, not the entry price

| | |
|---|---|
| **Files** | `newsbot/risk.py` (`size_position`), `newsbot/trade_check.py` |
| **Problem** | The stop is computed from the signal close, but entry happens at the next open. |
| **Fix** | State the stop as "N×ATR below your fill" and give the distance in ATR units. In `/check`, when a fill price is supplied, compute the stop from the fill. |
| **Test** | With the next open at 101, the stop is computed from 101. |
| **Complexity** | S |
| **Archive?** | No |
| **Status** | Implemented on `fix/stop-from-actual-fill`, not yet merged or deployed. `/check TICKER AMOUNT PRICE` places the stop N×ATR below the price given. Without a price, every view calls the stop an estimate from the signal close. See `stop-from-buy-price-report.md`. |

### P1-4: Dashboard on 0.0.0.0 with no login

| | |
|---|---|
| **Files** | `docker-compose.yml` (dashboard `ports`), `README.md` |
| **Problem** | The dashboard is published on 0.0.0.0 with no authentication. |
| **Fix** | Bind to `${DASHBOARD_BIND:-127.0.0.1}:8002:8501` and reach it through the VPN or SSH tunnel. Alternatively put the existing host nginx in front with basic auth. |
| **Test** | CI checks that the compose port string starts with an IP. On the Pi, `ss -tln` shows no `0.0.0.0:8002`. |
| **Depends on** | D3 |
| **Complexity** | S |
| **Archive?** | No |

### P1-5: Ollama has no limits and runs as root

| | |
|---|---|
| **Files** | `docker-compose.yml` (ollama service) |
| **Problem** | Ollama has no resource limits, runs as root and listens on the host. |
| **Fix** | Add `mem_limit` and `cpus`, and bind it to localhost or the internal network only. The Pi runs a separate shared container, so the owner has to decide there. |
| **Complexity** | S |
| **Archive?** | No |
| **Status** | Implemented on `hardening/ollama-runtime`, not yet merged or deployed. This repository owns only the `bundled-ollama` profile, which is off on the Pi. On the Pi the bot uses the `ollama` container of the `poster-bot` project over `poster-bot_default`. With the Pi's settings, the bot and dashboard render identically before and after except for one empty project network the bot now also joins. The bundled services run as uid 10002, with no capabilities, a read-only root filesystem and `no-new-privileges`. A one-shot `ollama-volume-init` (`CHOWN` only, no network) hands root-owned volumes to 10002. `HOME` is `/home/ollama`, since the image keeps models under `/root` (mode 700). Memory is capped at `BUNDLED_OLLAMA_MEM_LIMIT` (default 4g); CPU is uncapped unless `BUNDLED_OLLAMA_CPUS` is set. The services sit on their own `bundled-ollama` network, which only the bot joins; nothing is published. The pull client loses the model volume. The image is pinned to `ollama/ollama:0.32.9` by digest. CI now runs `docker compose config` with and without the profile. |
| **Verified** | Runtime, locally (Docker Desktop, arm64, 4 CPUs, 7.75 GB), with `qwen2.5:3b` and the bot's own `LlmSelector` ranking 14 headlines. **Before:** root, no limits, 5 selections in 12 s; 2.54 GB anonymous memory plus 1.06 GB page cache. **A bare `user:`** exits 1 (`mkdir /.ollama: permission denied`). **After, on the old root-owned volume:** the init chowned everything (key kept), the server ran as uid 10002 with `CapEff 0` and became healthy, the model was still listed, and ranking gave 5 selections in 12.1 s and 6.9 s. Ollama reported `total="4.0 GiB"`, so it reads the limit. The limit was never hit and there were no OOM kills. A second `up`, a plain restart and a fresh volume (non-root download of 1.9 GB) all worked. **At 2g:** no crash, but the model swapped (limit hit 25,762 times) and ranking slowed to 40.7 s. **External path:** a second project with the profile off reached the first project's Ollama over an external `RANKER_NETWORK` through `docker compose run news-notifier` (5 selections). The dashboard network cannot resolve `ollama`. With Ollama unreachable the ranker returns nothing and records the error. Static: 11 new tests in `tests/test_ollama_runtime.py`, of which 8 fail against `main`'s compose file. Compose config is valid locally (v5.7.0) and on the Pi's v5.6.0, rendered read-only from stdin. 1202 tests pass on 3.11, 3.14 and in the 3.15.2 image. Not verified: anything on the Pi's running containers, and the GPU block. |
| **Outside this repository** | The Pi's shared `ollama` (project `poster-bot`) runs as root with no limits and `OLLAMA_KEEP_ALIVE=-1`. It holds `qwen2.5:3b` and `llama3.2:3b` resident: 6.2 GiB of the Pi's 7.75 GiB, with swap full. It publishes no port. A second, host-installed `ollama.service` listens on `*:11434` as the non-root user `ollama` (`OLLAMA_HOST=0.0.0.0:11434`, models in `/mnt/ai-models/ollama`). ufw is active with deny-incoming and no 11434 rule. Neither the LAN nor the containers can reach it: from `daily_news_bot`, `172.20.0.1:11434` and `172.17.0.1:11434` both timed out (2026-11-14). It has no models, and its journal since 2026-10-03 shows no requests. Both are for the owner to decide: limits and keep-alive in poster-bot's `docker-compose.portainer.yml` (the file the shared container was started from), and binding or removing the host service. |

### P1-6: Forward-test protocol counts overlapping dates

| | |
|---|---|
| **Files** | `newsbot/forward_test.py` |
| **Problem** | `MIN_PAIRED_DATES=60` counts overlapping dates. |
| **Fix** | Count only non-overlapping dates (spacing at least the horizon) or use a block bootstrap. Add per-market minimums and the D4 protocol. Print "paper mode" until the criteria pass. |
| **Test** | 60 consecutive dates at h=20 count as 3 independent dates. |
| **Complexity** | M |
| **Archive?** | No (reporting only) |

### P1-7: Universe is not recorded, and India is still in the research basket

| | |
|---|---|
| **Files** | `evaluation_universe.csv`, `history_store` (`signal_archive`), `screener.py` |
| **Problem** | India is in scope of the research basket but not of the product. The archive does not record where a row came from. |
| **Fix** | Mark India out of scope as `broad-v2` and keep v1. Add a nullable `universe_source` column (configured, sticky or discovery) through `ALTER TABLE ... ADD COLUMN`. |
| **Test** | The migration is idempotent. Old rows are NULL. |
| **Complexity** | S–M |
| **Archive?** | Additive column only |

### P1-8: ESEF date can be backdated

| | |
|---|---|
| **Files** | `newsbot/company_numbers.py` (`known_from`) |
| **Problem** | `min()` can back-date a late filing to year end + 120 days. |
| **Fix** | Use the filing-added date when known, and year end + 120 days only as a fallback. Bump `company_numbers_version`. |
| **Test** | A filing added at year end + 200 days gets `known_from` = the added date. |
| **Archive?** | Changes `score_inputs` history. Rerun research §10 and keep the old result. |
| **Status** | Implemented on `fix/esef-known-from-date`, not merged or deployed. **Rule:** `known_from = date_added` if valid and available, otherwise `year_end + 120 days` (`esef_known_from`). "Valid" means an ISO date on or after the year end. `processed` is no longer a fallback: it is when filings.xbrl.org last ran its checks (Equinor 2021: added 2022-03-29, processed 2023-01-18). The filing list now follows the API's pages. **Version 4:** the version-3 cap no longer runs, and the new table `esef_number_filings` records the filing and raw `date_added` behind each ESEF row. Offline, the migration can only add that table, because saved rows had no filing link and the dates live at filings.xbrl.org. Saved rows are re-dated on the next weekly load (`_check_saved_dates`), which replays ingestion order. The year's read filings are read again in the order ingestion used (oldest `date_added` first, undated ones last). The first that has the item supplied it, if its value and currency match the saved ones. Anything unsure stays unchanged and unchecked: no filing, no match, or a read filing that is no longer listed or has lost its report. It is logged (`esef_dates_unchecked`) and raised as a weekly data warning. Only `known_from` changes. Yahoo rows are untouched. `score_inputs` now rewrites each stock's rows in one transaction, and `company_scores` drops months whose inputs are gone, so moved dates leave no stale rows. Rollback to the previous code is safe: `company_numbers` keeps its columns. **Known limits:** if a filing was really read later than this order says (its report appeared late), an earlier filing with the same number takes the row. That number was on file from that earlier date, but the stored filing link may name the wrong one. Two stocks sharing one LEI would leave the second's old rows unchecked for good, because `esef_filings_read` is keyed by filing only. No such pair is listed today. Stocks no longer on the list are never re-dated. |
| **Verified** | Read-only on a copy of the Pi's 2026-11-14 backup, against the live filings.xbrl.org metadata. All 427 read filings are still listed, none lacks `date_added`, and none has more than one page. 296 of 355 company-years have one filing; 59 have two to four. The re-dating matched every row of a listed stock: 1,150 of 2,920 rows moved later (median 9 days, up to 1,999), none moved earlier. Values, currencies, sources, fetch times and all 2,380 Yahoo rows are unchanged (compared row by row). Every row was matched by value and currency against its filing's report, re-downloaded (12 minutes, about 300 reports). Every row the metadata alone settles got the same date in an independent recomputation. The two groups the metadata cannot settle (SAF.PA 2020, LDO.MI 2021) matched their first filing. 88 rows of four stocks dropped in #152 stay unchecked, since the weekly load no longer visits them and nothing scores them. A second run changes nothing. Research §10 was rerun as §10b (old result and output kept; the comparison is in `docs/research/2026-10-analytics-findings.md`). No score passes before or after. Tests: 1219 pass on 3.11 and 3.14, and ruff is clean. 20 new or changed tests fail or error against `main`. An independent review found that a lone filing was trusted without a value check, and that an undated filing sorted first. Both were fixed and the copy re-dated again, with the same result. A second review found that filings were ordered by the text of `date_added`, so a malformed or pre-year-end one could be read before a valid one and date a number by the fallback. Order and date now share one validity check; 13 new tests, 1232 pass on 3.11, 3.14 and 3.15. On the live metadata of the 72 listed stocks with filings (410 filings) the order is the same as before for every stock, so the re-dating and §10b are unchanged. One filing has an unusable date (RMS.PA 24181: period end 2026-12-31, added 2026-03-24). Not verified: the migration and weekly load on the Pi itself. |

### P1-9: Production Python is not tested in CI

| | |
|---|---|
| **Files** | `.github/workflows/ci.yml`, `README.md` |
| **Problem** | Production runs Python 3.15, which CI never tests. |
| **Fix** | Add 3.15 to the test matrix, or run the unit tests inside the built image in the docker job. |
| **Complexity** | S |
| **Archive?** | No |
| **Status** | Implemented on `ci/test-production-python-version`, not yet merged. CI-only, so nothing to deploy. The docker job runs the whole unit suite inside the production image it has just built, on the image's own Python and packages: `python -W error::ResourceWarning -m unittest -v` as the `app` user, read-only root filesystem, tmpfs at `/tmp`, checkout mounted read-only. The step first compares the image's Python with the Dockerfile's `FROM python:X.Y` tag and fails on a mismatch. The 3.11 and 3.14 matrix is unchanged. Cause of the gap: Dependabot moved the image from 3.11 to 3.15 (`af1e358`) and nothing tied the CI matrix to the Dockerfile. |
| **Verified** | Locally, on a clean copy of the branch and the exact step script from `ci.yml`: image Python 3.15.2, 1173 tests, OK, 2 skipped (the Streamlit-only live tests, which the dashboard image runs). An injected failing test and a `ResourceWarning` both fail the step; a Dockerfile tag that does not match the image fails it too. A probe test inside the run saw `/usr/local/bin/python` 3.15.2, uid 10001, and a read-only `/src`. 3.11 and 3.14: ruff clean, 1173 tests OK, coverage 97%. Dashboard image builds and its 2 live tests pass. In CI (run 43503809011, PR #191 merged with `main`), all three jobs passed: the image ran Python 3.15.2 with 1191 tests OK and 2 skipped; 3.11.18 and 3.14.10 gave the same result with 97% coverage, and ruff and pip-audit were clean. |

## P2: docs, observability, data quality

| ID | Files | Fix |
|---|---|---|
| P2-1 | `README.md`, `PROJECT_MEMORY.md` | Update the job and command lists, module list and Python version. Point the authoritative instructions to `CLAUDE.md`. |
| P2-2 | `docker-compose.yml` | Remove the misspelled `FREEN_EWS_API_KEY` and `FINHUB_API_KEY` after confirming nothing reads them. Delete `.coveragerc.bak`. |
| P2-3 | `streamlit/app.py`, compose | Add a dashboard healthcheck, plus banners showing data age per table. |
| P2-4 | `tests/` | Find and close the leaked SQLite connection that triggers the shutdown `ResourceWarning`. **Done on `fix/sqlite-resourcewarning-leak`, not yet merged; see below.** |
| P2-5 | `research/gap_risk.py`, `hold_exit_study.py` | Add FX cost to the scripts. Record results per cost scenario. **Done on `feat/research-fx-cost-modelling`, not yet merged; see below.** |
| P2-6 | `newsbot/indicators.py`, README | Label ATR and RSI as simple-average (Cutler) variants. **Done on `docs/indicator-methodology`, not yet merged.** Docstrings only in `indicators.py` (the code is identical apart from docstrings, compared as parsed code): ATR is the 14-period arithmetic mean of True Range as a percentage of the latest close; RSI is 14-period Cutler's RSI (simple averages of gains and losses); neither uses Wilder smoothing; `_rsi_series` uses the same convention as `_compute_rsi_14`. README has a new "Indicator methodology" section. 1259 tests pass on 3.11, 3.14 and 3.15 (2 skipped); ruff is clean. Found and not fixed: `_compute_atr_percent` drops missing High, Low and Close values separately, so a bar missing only one of them shifts the columns against each other (a missing close in the last 14 bars gave 2.36% where dropping the whole bar gives 2.60%, on a test series; a missing high gives no ATR). Not known to occur in Yahoo's data. |
| P2-7 | `ruff`, CI | Lint `streamlit/`, `research/` and `scripts/`. |

### P2-4: A test left the bot's database open

| | |
|---|---|
| **Files** | `tests/store_isolation.py` (new), `tests/test_store_isolation.py` (new), `tests/test_news_bot.py`, `tests/test_risk.py`, `tests/test_risk_reporting.py`. No production code. |
| **Root cause** | The leaked connection was the one inside the process-wide `history_store.TRADE_HISTORY`, opened on the configured `TRADE_HISTORY_DB_FILE`. Four modules take their own name for that store at import (`from newsbot.history_store import TRADE_HISTORY`): `company_numbers`, `price_store`, `fills` and `snapshots`. The fixtures swapped only `history_store.TRADE_HISTORY` for a throwaway store and closed that one. `/analyze` builds its report card through `company_numbers._connection()` and `price_store._connection()`, so seven tests that call `build_ticker_analysis` opened the real store behind the fixture (two in `test_news_bot.TickerAnalysisTests`, four in `test_risk.IntegrationTests`, one in `test_risk_reporting.OneStopTests`). Nothing closed it, and Python reported it while shutting down, where a warning-as-error cannot fail the run. The same tests also created the configured database file. |
| **Fix** | `use_throwaway_store` in `tests/store_isolation.py` puts one temporary store in all five modules that hold the name, and registers its `close()` and the un-patching as cleanups. The three fixtures above now use it. No warning filter was added and the CI command is unchanged. |
| **Tests** | Four in `tests/test_store_isolation.py`: every `newsbot` module holding a `TRADE_HISTORY` is on the helper's list (a new holder fails this test); swapping `history_store` alone still leaves `/analyze` on the other store (the old fixture, shown with stand-in stores); with the helper `/analyze` reads only the throwaway store, which is closed afterwards and every name restored; commits and rollbacks on the store behave as before. They read the store's own `_conn`, never the garbage collector. |
| **Verified** | Before, on `main` (`8c556de`), Python 3.14.10: `python -W error::ResourceWarning -m unittest` ran 1232 tests OK and printed `ResourceWarning: unclosed database` at shutdown. A tracing run named the connection and the seven tests. After: 1236 tests, OK, 2 skipped (the Streamlit-only live tests), no warning, on 3.11.11, 3.14.10 and 3.15.2, and on 3.15.2 inside the production image with the CI step's own command (`--read-only`, `--user app`). The same trace finds no test reaching the process-wide store, no connection left open, and the configured database file is no longer created. `ruff check news_bot.py newsbot tests` is clean; coverage 97%. |
| **Limits** | The four modules still take the store by name, so a new test that reaches them without the helper would open the real store again. The listing test only catches a new module, not a new test. `tests/test_dashboard_websocket_live.py` runs only in the dashboard image and was not run here. |

### P2-5: FX cost in the gap and hold research

Research only. Nothing the bot runs was changed.

| | |
|---|---|
| **Files** | `research/fx_costs.py` (new, shared), `research/gap_risk.py`, `research/hold_exit_study.py`, `tests/test_research_fx_costs.py` (new), `research/README.md`, `docs/research/2026-10-analytics-findings.md` (pointers only), three new output files in `docs/research/data/`. |
| **Before** | Neither script converted currency or charged for it. `gap_risk.py` had no costs at all: its returns are price ratios in the stock's own currency. `hold_exit_study.py` had one cost, the broker fee: `net = gross - 2 x 0.49%` (the 59 NOK minimum on 12,000 NOK, each way), also on returns in the stock's own currency. So there was nothing to count twice. |
| **Convention** | A stock not priced in NOK needs two conversions: NOK to its currency to buy, and back after the sale. Each loses `bps` of the amount converted. With `c = bps / 10,000` and an own-currency return `r`, the cost in percent of the NOK put in is `(1 + r) x (2c - c²) x 100`: the second conversion is on the sale proceeds. NOK stocks pay nothing. A stock with an unknown currency is charged as foreign and counted in the output (0 in both runs). The cost comes off once, after the script's own costs: `gap_risk` realised return minus FX; `hold_exit_study` `net` minus FX. The fee constants and the `net` column are untouched. Every scenario starts from the same pre-FX number, and a table that already has FX columns is refused. |
| **Scenarios** | Per conversion: 0 bps (baseline), 10 (low), 25 (medium), 50 (high). **These are illustrative round numbers for a sensitivity table. They are not DNB's or any broker's verified pricing.** Defined once, in `fx_costs.FX_SCENARIOS`. |
| **Output** | Both scripts print everything they printed before, unchanged, and then an "FX COST SENSITIVITY" section: the assumption, trades by currency, and per scenario the gross return, the return after existing trading costs, the FX cost (mean and total), the return after FX and the change against 0 bps. The old output files were not replaced. |
| **Tests** | 23 in `tests/test_research_fx_costs.py`, on fixed numbers, no network: 0 bps is exactly no cost and reproduces the old `net` table; NOK stocks are never charged; both legs are charged, checked against a hand walk-through (100 NOK, 25 bps, +10% leaves a cost of 0.5493125); more cost never leaves more; a frame cannot be charged twice and scenarios do not stack; the fee is taken once and is what it was; unknown and missing currencies; both scripts share one scenario table and one model. Full suite: 1259 tests, OK, 2 skipped (the Streamlit-only live tests), on Python 3.11.11, 3.14.10 and 3.15.2 with `-W error::ResourceWarning`, and on 3.15.2 inside the production image with the CI step's command; coverage 97%. `ruff check news_bot.py newsbot tests` is clean, and so are the three research files (not part of the CI lint). |
| **Runs** | Both on the saved data of the original runs, not refetched. `gap_risk.py` at 3xATR and 2xATR below the signal close reproduces `gap_risk_3.0x_signal-close.txt` and `gap_risk_2.0x_signal-close.txt` line for line before the new section. `hold_exit_study.py` reproduces its old output line for line (and section 8's table). A second run gives the same output. New files: `gap_risk_3.0x_signal-close_fx.txt`, `gap_risk_2.0x_signal-close_fx.txt`, `hold_exit_study_fx.txt`. |
| **Limits** | The cost levels are assumptions until the broker's real FX pricing is checked, including whether it is a flat percentage, has a minimum, or can be avoided with a currency account. The exchange rate's own move during the hold is still not modelled; returns stay in the stock's own currency. The broker fee is still the Norwegian minimum for every stock, as before. The fee and the FX cost are each taken on the full position; their effect on each other (about 0.005 points) is ignored. The gap basket includes 11 Indian stocks, charged as foreign like the rest. |

**Hold study** (95 stocks, 44,807 entries per row, 60% of them in EUR or USD). Mean per trade, %:

| FX cost per conversion | 0 bps | 10 bps | 25 bps | 50 bps |
|---|---|---|---|---|
| FX cost, all trades | 0.00 | 0.12 | 0.30 | 0.61 |
| 20 sessions, time: gross 1.50, after fees 0.51, after fees and FX | +0.51 | +0.39 | +0.21 | -0.09 |
| 20 sessions, wide (3xATR): gross 1.30, after fees 0.31, after fees and FX | +0.31 | +0.19 | +0.01 | -0.29 |
| 5 sessions, time, per 20 sessions | -2.70 | -3.18 | -3.90 | -5.10 |
| 20 sessions, time, NOK stocks | +0.61 | +0.61 | +0.61 | +0.61 |
| 20 sessions, time, EUR stocks | +0.33 | +0.12 | -0.18 | -0.68 |
| 20 sessions, time, USD stocks | +0.97 | +0.77 | +0.46 | -0.04 |
| Total FX cost, 20 sessions, time, 12,000 NOK a trade (NOK) | 0 | 652,850 | 1,630,901 | 3,257,720 |

What changes: the order of the rules and of the hold lengths does not. "Time" is the best rule at every hold in every scenario, and 20 sessions beats 10 beats 5. Short holds get worse faster, since four 5-session trades pay the conversions four times. The levels do change: a foreign stock pays about twice the per-conversion cost on each round trip, on top of the 1% fee. The average EUR trade is below zero from 25 bps, and the average of all trades from 50 bps. Oslo stocks are not affected, so they keep an edge of 0.2 to 1.0 points per trade over foreign ones in this model.

**Gap study** (44 stocks; 3xATR: 26,438 entries, 21,826 charged). Mean per entry, % of the NOK put in:

| FX cost per conversion | 0 bps | 10 bps | 25 bps | 50 bps |
|---|---|---|---|---|
| 3xATR: FX cost, all entries | 0.000 | 0.166 | 0.416 | 0.831 |
| 3xATR: realised return (gross = after trading costs, 0.78) after FX | 0.78 | 0.61 | 0.36 | -0.05 |
| 3xATR: mean loss when stopped, all entries | 5.99 | 6.15 | 6.37 | 6.75 |
| 2xATR: realised return (0.61) after FX | 0.61 | 0.44 | 0.19 | -0.22 |
| 2xATR: mean loss when stopped, all entries | 4.15 | 4.30 | 4.53 | 4.92 |

What changes: nothing in the gap result. How often a stop is hit or gapped, and how far below the stop the sale was, are price ratios in the stock's own currency, and the `GapStudy(...)` rows the bot uses are the same. What a stop loses in NOK is larger by the FX cost: 0.16 to 0.76 points on average at 3xATR, about 1 point for a foreign stock at 50 bps.

## P3: features (only after P0/P1)

- **NOK portfolio view:** currency exposure across all holdings.
- **Calendars:** use `exchange_calendars` for XNYS, XOSL and XETR. This is a new dependency and needs approval.
- **Minimal macro context:** policy rate, yield-curve slope, VIX/V2X, index trend. Each value carries an observation date and a publication date. Show it as **context only**, never as a gate, until it passes a pre-registered test.
- **EU expansion:** Euronext, Xetra and Nordic in stages. See the matrix in `decision-log.md`.

## Order

P0-1 → P0-2 → P1-2 → P1-3 → P1-4 → P1-9 → P1-1 (after D1) → P1-6 (after D4) → P1-7 → P1-8 → P1-5 → P2 → P3.
