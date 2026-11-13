# P1-2: hold-length default and the hold in `rule_id`

Report date: 2026-11-13. Branch `fix/holding-horizon-rule-id`, from `main` at `601486a`.
**Not merged and not deployed.** Nothing on the Pi was read or changed in this session: a read-only look at the Pi's archive was refused by the session's permissions, so the Pi facts below come from `independent-audit.md` §4b (archive as of 2026-11-12) and `risk-reporting-report.md` §1 (Pi `.env`, 2026-11-13).

This change does not touch entry timing, exits, stops, risk settings, gates, sizing, costs or the forward-test protocol. It changes one default and how new archive rows are labelled.

## 1. Root cause

- **Default.** The hold was 5 sessions in `config.py` and in the compose default (`${TRADE_OUTCOME_LOOKBACK_DAYS:-5}`), but 20 in `.env.example`, in the research (§8), in D4 and on the Pi. A deployment without the variable silently measured 5-session holds, the hold research shows loses most to fees.
- **`rule_id`.** It was built from the five gate thresholds only. The hold lived in a separate column, `horizon_sessions`, so the Pi's archive holds two series under one id: `mom1-day0-atr4.5-dd8-vol1.2` at 5 sessions (1,036 rows) and at 20 sessions (58 rows).

## 2. Where the hold is used

One setting controls it: `SETTINGS.trade_outcome_lookback_days`, read from the environment variable `TRADE_OUTCOME_LOOKBACK_DAYS` (legacy name, kept).

| Consumer | What the setting decides |
|---|---|
| `history_store.archive_signal` | `horizon_sessions` of each new archive row, and now the `-h{N}` of its `rule_id` |
| `history_store` published picks | `horizon_sessions` of each sent pick; `/pickperformance` reports only picks at the current hold |
| `forward_test.build_forward_test_report` | Which clean-test rows are "the test" (stored hold = setting) |
| `forward_test.forward_outcome` | Nothing: settling uses each row's **stored** `horizon_sessions` |
| `backtest_reports._run_walk_forward_backtest` | The `/performance` backtest horizon |
| `risk.py` | The "inside a typical N-session hold" wording of the earnings flag |

So the setting fixes the backtest horizon directly. For the forward test, it decides what new rows record and which rows are reported as current; an archived row is always scored with its own stored hold.

## 3. Changes

| File | Change |
|---|---|
| `newsbot/config.py` | `trade_outcome_lookback_days` default 5 → 20. The `risk_earnings_warn_days` comment no longer calls 5 the default (value unchanged). |
| `docker-compose.yml` | `TRADE_OUTCOME_LOOKBACK_DAYS=${TRADE_OUTCOME_LOOKBACK_DAYS:-20}` |
| `newsbot/history_store.py` | New `gate_rule_id()` (the old format, unchanged). `current_rule_id()` = `gate_rule_id()` + `-h{trade_outcome_lookback_days}`. |
| `newsbot/forward_test.py` | The report's current rule matches the new id **or** the gate-only id; holds are still split by `horizon_sessions`. |
| `streamlit/archive_views.py`, `streamlit/app.py` | `filter_settings()` drops a trailing `-h{N}`; the "Filter settings" choice is made on the gates, and pages still split by hold. |
| `README.md` | Default 20; the rule-id format and how old ids are treated; the earnings-window note. |
| `tests/test_holding_horizon.py` | New: 14 tests (section 5). |
| `tests/test_forward_test.py`, `tests/test_forward_entry.py` | Fixtures now archive under the hold they test, instead of archiving under one hold and `UPDATE`-ing `horizon_sessions` to another (a row the code can no longer produce). The pre-fix database fixture uses the gate-only id that build wrote. |

`.env.example` already said 20 and is unchanged. The Pi's `.env` (`TRADE_OUTCOME_LOOKBACK_DAYS=20`) is not touched.

### Effective configuration

| Source | Before | After |
|---|---|---|
| `config.py` default (no variable anywhere) | 5 | **20** |
| Compose, variable unset or empty in host env / `.env` | 5 | **20** |
| Compose, variable set (e.g. 5) | 5 | 5 (kept) |
| `.env.example` | 20 | 20 |
| Pi `.env` (recorded) | 20 | 20, untouched |

Precedence: process environment (compose `environment:`) over the `.env` file over the code default. Under compose the `environment:` entry is always present, so its default is what a host without the variable gets; a value in `.env` is interpolated into it.

### Rule-id format

| | Example (default gates) |
|---|---|
| Before P1-2, and every existing row | `mom1-day0-atr4.5-dd8-vol1.2` |
| New rows at the 20-session default | `mom1-day0-atr4.5-dd8-vol1.2-h20` |
| New rows with `TRADE_OUTCOME_LOOKBACK_DAYS=5` | `mom1-day0-atr4.5-dd8-vol1.2-h5` |

## 4. Historical rows

- **No row is rewritten and nothing is migrated.** The only writer of `rule_id` is `archive_signal`, whose `ON CONFLICT DO NOTHING` keeps the first observation, so re-screening an archived (symbol, session) keeps its old id.
- **Telegram `/forwardtest`.** A gate-only row is treated as recorded under the current gates, which it was; its hold comes from `horizon_sessions`, exactly as before. Old clean-test rows at 20 sessions stay in the test, and old historical rows stay in "Before the entry fix", per hold. A new row with a different `-h{N}` is excluded, as rows with another hold always were.
- **Dashboard.** The "Filter settings" list is built from the gate part. Old and new rows of one gate set are one choice, and every page still separates holds.
- **Why not compare ids exactly.** `independent-review.md` item 8 predicted the problem: the 58 Pi rows at 20 sessions, every historical row, and the clean-test rows recorded since #159 would drop out of the Telegram report. On the dashboard it was confirmed by rendering `main`'s `app.py` against a mixed archive. The default choice became the new id alone, hid the two finished old rows, and said "The clean test started on 2026-11-17" when it started on 2026-11-10.
- **Why not derive `…-h{N}` for old rows at read time.** That would move historical rows from one selection (holds 5 and 20) into two. Old rows stay in the group they had.

Expected on the Pi after a deploy: new rows `mom1-day0-atr4.5-dd8-vol1.2-h20`. The report header names that id. The clean test keeps the gate-only rows recorded since #159 plus new ones. The historical section is unchanged: hold 5 has 1,036 rows and hold 20 has 58 (counts as of 2026-11-12).

## 5. Tests and verification

| Check | Result |
|---|---|
| New tests `tests/test_holding_horizon.py` | 14 passed |
| Focused: `test_holding_horizon`, `test_forward_test`, `test_forward_entry`, `test_dashboard_views` | 135 passed |
| Full suite, as CI (`python -W error::ResourceWarning -m coverage run -m unittest`), Python 3.15.2 | 1,173 run, OK, 2 skipped; coverage 97% |
| `ruff check news_bot.py newsbot tests` (CI scope) | All checks passed |
| `docker build .` and `docker build ./streamlit` | Both built |
| In the built image: no variable / `TRADE_OUTCOME_LOOKBACK_DAYS=5` | hold 20, `…-vol1.2-h20` / hold 5, `…-vol1.2-h5` |
| `docker-compose config` (v5.7.0) with `.env` empty / `=` / `=5` / `=20` | 20 / 20 / 5 / 20 |
| Dashboard home rendered with Streamlit `AppTest` in the dashboard image, mixed old/new archive | Branch: one filter setting, 2 clean finished + 2 awaiting, scorecard shown. `main`: split selector, 0 finished, wrong start date. |

The new tests cover, against the effective settings rather than file text:

1. Default is 20 with no variable and no env file.
2. An environment override (5) and a `.env` file value (7) are kept.
3. The compose default resolves to 20, and an override passes through it.
4. `.env.example` loads as 20.
5. Default settings give `mom1-day0-atr4.5-dd8-vol1.2-h20`, and the gate part equals the old format byte for byte.
6. The id changes with the hold; the gate part does not.
7. New rows carry `-h20` / `-h5`.
8. After a restart (migration rerun), a settle pass, a re-screen of an archived (symbol, session) and a report, old rows keep their ids. `scripts/forward_test_integrity.py compare` reports no change.
9. The report still counts old clean-test rows with new ones, and shows historical rows at holds 5 and 20. It excludes only the other gates and the other hold.
10. The dashboard data keeps old and new rows in one filter setting, and the clean test's start date is the first old row's. Finished old rows reach the averages.

**Mutation check.** Each piece of the fix was reverted in turn, and at least one new test failed every time. The reverts were: exact-id match in the report, config default 5, compose default 5, no suffix, and no gate grouping on the dashboard.

## 6. Limitations and risks

- **Rollback.** Code from before this change compares ids exactly. After new rows exist, a rollback would put them under "excluded" in Telegram and under a second filter-setting choice on the dashboard. Nothing is lost, and they return on redeploy.
- **Ad-hoc SQL and research** that group by `rule_id` alone will see two ids for one experiment. Group by the gate part plus `horizon_sessions`.
- **The Telegram header** names only the new id, while the report also includes gate-only rows of the same gates. This is documented in the README, not in the message.
- **`app.py` `choose()`** has no unit test, because it needs Streamlit. It was checked once with `AppTest` in the dashboard image, which is not part of CI.
- **Pre-existing, not changed here.** The "One stock" page averages its selected rows across holds, and historical rows of one gate set have holds 5 and 20.
- **Other defaults still disagree** with `.env.example`: risk per trade 0.5 vs 2.0, stop multiple 2.0 vs 3.0, position cap 20 vs 25, total 60 vs 100, earnings window 7 vs 28 (review N8). These are risk settings and out of scope. Note that a 7-day earnings window does not cover the 20-session default hold.
- **Not verified on live data.** The Pi archive was not read in this session.
- **Test output.** A `ResourceWarning` line printed at interpreter shutdown also appears on unmodified `main`, and the suite still passes.
