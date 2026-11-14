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
| **Outside this repository** | The Pi's shared `ollama` (project `poster-bot`) runs as root with no limits and `OLLAMA_KEEP_ALIVE=-1`. It holds `qwen2.5:3b` and `llama3.2:3b` resident: 6.2 GiB of the Pi's 7.75 GiB, with swap full. It publishes no port. A second, host-installed `ollama.service` listens on `*:11434` as the non-root user `ollama` (`OLLAMA_HOST=0.0.0.0:11434`, models in `/mnt/ai-models/ollama`). ufw is active with deny-incoming and no 11434 rule, so the LAN cannot reach it, but local containers can. Both are for the owner to decide: limits and keep-alive in `~/poster-bot/docker-compose.yml`, and binding or removing the host service. `~/poster-bot/docker-compose.yml` mounts `./ollama`, while the running container uses the volume `poster-bot_ollama_models`, so the file and the running container have drifted. |

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
| **Verified** | Read-only on a copy of the Pi's 2026-11-14 backup, against the live filings.xbrl.org metadata. All 427 read filings are still listed, none lacks `date_added`, and none has more than one page. 296 of 355 company-years have one filing; 59 have two to four. The re-dating matched every row of a listed stock: 1,150 of 2,920 rows moved later (median 9 days, up to 1,999), none moved earlier. Values, currencies, sources, fetch times and all 2,380 Yahoo rows are unchanged (compared row by row). Every row was matched by value and currency against its filing's report, re-downloaded (12 minutes, about 300 reports). Every row the metadata alone settles got the same date in an independent recomputation. The two groups the metadata cannot settle (SAF.PA 2020, LDO.MI 2021) matched their first filing. 88 rows of four stocks dropped in #152 stay unchecked, since the weekly load no longer visits them and nothing scores them. A second run changes nothing. Research §10 was rerun as §10b (old result and output kept; the comparison is in `docs/research/2026-10-analytics-findings.md`). No score passes before or after. Tests: 1219 pass on 3.11 and 3.14, and ruff is clean. 20 new or changed tests fail or error against `main`. An independent review found that a lone filing was trusted without a value check, and that an undated filing sorted first. Both were fixed and the copy re-dated again, with the same result. Not verified: the migration and weekly load on the Pi itself. |

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
| P2-4 | `tests/` | Find and close the leaked SQLite connection that triggers the shutdown `ResourceWarning`. |
| P2-5 | `research/gap_risk.py`, `hold_exit_study.py` | Add FX cost to the scripts. Record results per cost scenario. |
| P2-6 | `newsbot/indicators.py`, README | Label ATR and RSI as simple-average (Cutler) variants. |
| P2-7 | `ruff`, CI | Lint `streamlit/`, `research/` and `scripts/`. |

## P3: features (only after P0/P1)

- **NOK portfolio view:** currency exposure across all holdings.
- **Calendars:** use `exchange_calendars` for XNYS, XOSL and XETR. This is a new dependency and needs approval.
- **Minimal macro context:** policy rate, yield-curve slope, VIX/V2X, index trend. Each value carries an observation date and a publication date. Show it as **context only**, never as a gate, until it passes a pre-registered test.
- **EU expansion:** Euronext, Xetra and Nordic in stages. See the matrix in `decision-log.md`.

## Order

P0-1 → P0-2 → P1-2 → P1-3 → P1-4 → P1-9 → P1-1 (after D1) → P1-6 (after D4) → P1-7 → P1-8 → P1-5 → P2 → P3.
