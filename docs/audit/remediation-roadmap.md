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

### P1-3: Stop is anchored to the signal close, not the entry price

| | |
|---|---|
| **Files** | `newsbot/risk.py` (`size_position`), `newsbot/trade_check.py` |
| **Problem** | The stop is computed from the signal close, but entry happens at the next open. |
| **Fix** | State the stop as "N×ATR below your fill" and give the distance in ATR units. In `/check`, when a fill price is supplied, compute the stop from the fill. |
| **Test** | With the next open at 101, the stop is computed from 101. |
| **Complexity** | S |
| **Archive?** | No |

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

### P1-9: Production Python is not tested in CI

| | |
|---|---|
| **Files** | `.github/workflows/ci.yml`, `README.md` |
| **Problem** | Production runs Python 3.15, which CI never tests. |
| **Fix** | Add 3.15 to the test matrix, or run the unit tests inside the built image in the docker job. |
| **Complexity** | S |
| **Archive?** | No |

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
