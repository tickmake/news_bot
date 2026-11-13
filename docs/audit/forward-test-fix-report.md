# Forward-test integrity fix: implementation, migration and classification report

Report date: 2026-11-13, updated the same day after review (section 0). Implements revision 3 of `forward-test-fix-plan.md`.
Branch `fix/forward-test-entry-timing`. **Not deployed to the Pi.**

## 0. Correction after review

**The finding.** The static check on `recorded_at` rejected a signal recorded on its own signal date. It classified the row `invalid` whenever the recording's exchange-local date was *not after* the signal date. A screen run after the close on the signal date is legitimate; the entry rule then picks the next session by itself.

**The fix** (`newsbot/archive_quality.py`, one comparison):

| Recording date, in exchange-local time | Before | Now |
|---|---|---|
| Earlier than `signal_session` | invalid | invalid |
| Equal to `signal_session` | **invalid (wrong)** | valid |
| Later than `signal_session` | valid | valid |

The reason code is renamed from `recorded_before_signal_closed` to `recorded_before_signal_session`, which says what it now means.

**Tests.** `test_recorded_before_the_signal_session_was_over`, which asserted the wrong behaviour, is replaced. Five tests cover the corrected rule:

| Test | Case | Expected |
|---|---|---|
| `test_recorded_on_the_signal_date_is_valid` | 17:30 New York on the signal date | valid; enters the next session; clean |
| `test_same_day_at_1900_new_york_enters_the_next_session` | 19:00 New York on the signal date (already the next day in UTC) | valid; enters the next session at 09:30 |
| `test_the_previous_exchange_local_date_is_invalid` | 23:00 New York the evening before (already the signal date in UTC); the same boundary for Oslo | invalid, `recorded_before_signal_session`; one hour later is valid |
| `test_same_day_after_the_oslo_close_enters_the_next_oslo_session` | 17:30 Oslo on the signal date | valid; enters the next Oslo session at 09:00 |
| `test_a_same_day_signal_is_classified_clean_and_settled_through_the_job` | The three cases through the settle job | two rows clean and settled; the early one invalid and never fetched |

The exact-open and one-microsecond-before-open tests (`test_16_the_opening_instant_itself_is_too_late`) are unchanged and pass.

**Effect on existing data: none.** No row in the Pi's archive was recorded on or before its signal date, so the classification in section 5 is identical before and after the correction. The defect would have hit new rows from any screen run between a market's close and its local midnight.

**Not changed by this correction:** historical outcomes, screening gates, `rule_id`, costs, holding periods, stock selection, or any trading parameter. The only code change is the comparison and the reason code.

**Opening times for Paris, Amsterdam and Brussels are now confirmed from a primary source** (section 3).

### Commands run for this update and their results

`$S` is the session scratch folder holding the virtualenv (Python 3.15.2) and the copy of the Pi database.

| # | Command | Result |
|---|---|---|
| 1 | `$S/venv/bin/ruff check news_bot.py newsbot tests scripts/forward_test_integrity.py streamlit/archive_views.py` | `All checks passed!`, exit 0 |
| 2 | `$S/venv/bin/python -B -W error::ResourceWarning -m coverage run -m unittest` | `Ran 1021 tests`, `OK` |
| 3 | `$S/venv/bin/coverage report` | TOTAL 96%; `archive_quality.py`, `forward_test.py`, `exchange_hours.py` 100% |
| 4 | `docker build -q -t news-bot-fix-ci .` | Built, exit 0 |
| 5 | `docker build -q -t news-bot-dashboard-fix-ci ./streamlit` | Built, exit 0 |
| 6 | `docker run --rm --entrypoint sh -v $S/src:/src -w /src -e HOME=/tmp news-bot-fix-ci -c "python -B -W error::ResourceWarning -m unittest"` (`$S/src` is a copy of the working tree) | `Ran 1021 tests`, `OK`, Python 3.15.2 |
| 7 | `python scripts/forward_test_integrity.py snapshot $S/pi/before.db > $S/pi/before.json` | Snapshot of the untouched copy (file sha256 `b468221f18004fbe…`) |
| 8 | `TRADE_HISTORY_DB_FILE=$S/pi/after.db python $S/dryrun_a.py` (migrate and classify a second copy with every price fetch disabled) | Migration 2 ms; classification 17 ms |
| 9 | `python scripts/forward_test_integrity.py snapshot $S/pi/after.db > $S/pi/stage_a.json` then `… compare $S/pi/before.json $S/pi/stage_a.json` | `"problems":[]`, exit 0 |
| 10 | `TRADE_HISTORY_DB_FILE=$S/pi/after.db python $S/dryrun_b.py` (one real settle pass with live prices) | `(0, 0)` in 9.7 s |
| 11 | `python scripts/forward_test_integrity.py snapshot $S/pi/after.db > $S/pi/after.json` then `… compare $S/pi/before.json $S/pi/after.json` | `"problems":[]`, exit 0 |
| 12 | `docker run --rm --read-only --tmpfs /tmp -e HOME=/tmp -v $S/pi/after.db:/bot-data/.news_bot_trade_history.db:ro -v $S/smoke.py:/smoke.py:ro news-bot-dashboard-fix-ci python /smoke.py` | 19 page states, 0 errors |

Integrity figures from steps 9 and 11 are the same as in section 4: 1,094 rows and 924 outcomes before and after, identity checksum `a860b3984539cc7e…` and outcome checksum `6cc9cb992a01218c…` identical, `integrity_check` ok, 0 new-policy outcomes at or before their recording. The classification is the same as in section 5.

The one shutdown `ResourceWarning` noted in section 3 is still present and still predates this branch.

## 1. Summary

- **The rule is enforced.** A signal recorded under the new policy enters at the first session whose open is strictly after `recorded_at`. Every new outcome stores the open it used, so the rule can be checked from the stored row.
- **No historical row was rewritten.** A dry run on a copy of the Pi's database left every recorded signal and all 924 existing outcomes byte-identical (section 4).
- **Every existing row is classified.** 613 clean, 422 potentially contaminated, 37 invalid, 22 awaiting entry (section 5).
- **Telegram and the dashboard read one definition.** Both use the `signal_archive_evaluated` view. Contaminated, invalid, awaiting-entry and in-hold rows are in no average on either.
- **Checks run:** 1,021 unit tests pass (96% coverage), on the host and inside the built bot image; Ruff passes; both images build; every dashboard page runs in the built dashboard image for every series and class.

## 2. What changed

| File | Change |
|---|---|
| `newsbot/exchange_hours.py` (new) | Timezone and regular open for the eight supported exchanges. Anything else is unsupported. |
| `newsbot/archive_quality.py` (new) | The classifier: static checks, entry check, reason codes, classifier version 1. |
| `newsbot/history_store.py` | Additive schema (`entry_policy`, `signal_archive_quality`, the one-row `signal_archive_quality_active` pointer), the view (recreated at every start), `archive_signal` writes `after-recorded-v1`, insert-only `classify`, `activate_classifier`. |
| `newsbot/forward_test.py` | Entry selection by `recorded_at` for new rows and by signal date for old ones; timezone checks; two-stage classification in the settle job; pairing by entry session; the report reads the view and has a clean-test section and a historical section. |
| `newsbot/bot.py` | Opens the archive at startup so the view exists before the dashboard reads it. |
| `streamlit/archive_views.py` (new) | Every dashboard selection and aggregation, with no Streamlit import, so it is tested. |
| `streamlit/app.py` | One loader on the view; a series/class selector on every performance page; each page states series, policy, classifier version, filter settings, hold and row counts. |
| `scripts/forward_test_integrity.py` (new) | Read-only before/after fingerprint of the archive. |
| `.github/workflows/ci.yml` | Builds the dashboard image. |
| `README.md` | The `/forwardtest` section describes the new entry rule and the classes. |
| Tests | `tests/test_forward_entry.py` (65 tests), `tests/test_dashboard_views.py` (19), updates to `tests/test_forward_test.py` and `tests/test_bot.py`. |

Not changed: screening gates, stock selection, `rule_id`, sizing, costs, or any trading parameter.

## 3. Verification

| Check | Result |
|---|---|
| Ruff (`news_bot.py newsbot tests`, plus the two new scripts/modules) | Passed |
| Unit suite on the host, Python 3.15, `-W error::ResourceWarning` | 1,021 tests, all pass. 936 before this change. |
| Coverage of `newsbot` | 96% (unchanged). `forward_test.py`, `archive_quality.py`, `exchange_hours.py` at 100%. |
| Unit suite inside the built bot image (Python 3.15.2) | 1,021 tests, all pass |
| Bot image build | Built |
| Dashboard image build | Built |
| Hardened bot runtime (read-only root, dropped capabilities, uid 10001) | Imports work; timezone data is present; the schema and view are created at startup |
| Dashboard pages in the built dashboard image, against the migrated copy | All 7 pages run with no exception, for the clean test and each of the three historical classes (19 page states) |
| Dashboard against a database the bot has not migrated yet | Shows "The bot has not prepared the forward-test data yet" instead of failing |

One `ResourceWarning: unclosed database` still appears at interpreter shutdown. It was there before this change (roadmap P2-4) and does not fail the run.

### Three checks that were open in the plan

**Opening times.** Checked against published hours:

| Exchange | Table | Published | Source |
|---|---|---|---|
| Xetra | 09:00 | Opening auction from 08:55; continuous trading starts 09:00 to 09:04 by segment | Deutsche Börse circular; Eurex Xetra page |
| Borsa Italiana | 09:00 | Opening auction 07:30 to 09:00; continuous trading from 09:00 | borsaitaliana.it |
| Bolsa de Madrid | 09:00 | Opening auction 08:30 to 09:00; open market from 09:00 | Sociedad de Bolsas Circular 1/2026 |
| Euronext Paris, Amsterdam, Brussels | 09:00 | Continuously traded equities: "09:00 Random – 17:30". "The opening uncrossing will randomly occur between CET 09:00:00 and 09:00:30." | **Primary:** Euronext, *Appendix of Trading Manual 4-01*, "Effective date: 12 November 2026", sheet "Appendix to 4-01 Manual OPTQ" (`euronext.com/en/media/1927/download`, linked as "Cash markets trading hours" from Euronext's trading-hours page) |
| Oslo Børs | 09:00 | The same appendix lists Oslo equities as "09:00 Random – 16:20" | Same primary source; also confirmed by the owner |
| New York | 09:30 | Confirmed by the owner | |

How the Euronext appendix was read: the spreadsheet was downloaded from euronext.com and its rows for Paris, Amsterdam and Brussels with nature "Equities" were counted by trading mode.

| Market | Continuous groups opening "09:00 Random" | Other equity groups |
|---|---|---|
| Paris | 33 | 1 continuous group opening 15:30; 34 double-fixing groups at 11:30; 6 call or closed groups |
| Amsterdam | 21 | 7 double-fixing groups at 11:30 |
| Brussels | 16 | 23 double-fixing groups at 11:30 |

Two caveats, neither of which weakens the rule:

- **The open is struck up to 30 seconds after 09:00.** The table says 09:00:00, which is no later than the real open.
- **Illiquid shares trade by double fixing at 11:30 and 16:30,** and one Paris group opens at 15:30. For those the table's 09:00 is early, so a signal recorded between 09:00 and 11:30 enters a session later than it strictly had to. That is conservative, never contaminating. No equity group on these three markets opens before 09:00.

The rule only needs the table's time to be *no later than* the real open. An exchange that prints its opening price a little after 09:00 (Xetra) is still safe: the real price is later than assumed, never earlier.

**The timezone Yahoo returns.** One live fetch per exchange, two years of bars each:

| Symbol | Yahoo's zone | Table's zone | Bars | Offset mismatches |
|---|---|---|---|---|
| AAPL | America/New_York | America/New_York | 501 | 0 |
| EQNR.OL | Europe/Oslo | Europe/Oslo | 500 | 0 |
| SAP.DE | Europe/Berlin | Europe/Berlin | 506 | 0 |
| MC.PA | Europe/Paris | Europe/Paris | 510 | 0 |
| ASML.AS | Europe/Amsterdam | Europe/Amsterdam | 510 | 0 |
| ABI.BR | Europe/Brussels | Europe/Brussels | 510 | 0 |
| ENI.MI | Europe/Rome | Europe/Rome | 505 | 0 |
| SAN.MC | Europe/Madrid | Europe/Madrid | 510 | 0 |

A new-policy outcome computed on each live history entered after its recording time.

**Daylight saving.** Tested in both mismatch periods (late October 2026 and mid-March 2027) and confirmed in the bot image: on 2026-10-27 New York opens at 13:30 UTC and Oslo at 08:00 UTC.

## 4. Database integrity: before and after

**Method.** The Pi's newest backup (`trade_history-20261113-135214.db`, taken by the Pi itself) was copied to a scratch folder; the Pi was only read. `scripts/forward_test_integrity.py` took a fingerprint. The new code then ran on a second copy in two stages, with a fingerprint after each.

- **Stage A:** migration and classification with every price fetch disabled.
- **Stage B:** one real settle pass with live prices.

| | Before | After stage A | After stage B |
|---|---|---|---|
| `PRAGMA integrity_check` | ok | ok | ok |
| Archive rows | 1,094 | 1,094 | 1,094 |
| Rows with an outcome | 924 | 924 | 924 |
| Checksum of what every signal recorded (17 columns, all rows) | `a860b3984539cc7e…` | identical | identical |
| Checksum of the 924 existing outcomes | `6cc9cb992a01218c…` | identical | identical |
| Rows deleted | | 0 | 0 |
| Rows whose recorded values changed | | 0 | 0 |
| Existing outcomes changed | | 0 | 0 |
| `entry_policy` on existing rows | (no column) | NULL on all 1,094 | NULL on all 1,094 |
| Columns added | | `entry_policy` | |
| Tables added | | `signal_archive_quality`, `signal_archive_quality_active` | |
| Views added | | `signal_archive_evaluated` | |
| Active classifier version | | 1 | 1 |
| New-policy outcomes with an open at or before `recorded_at` | | 0 | 0 |
| Other tables | | row counts unchanged | row counts unchanged |

`compare` reported no problems at either stage.

**Timing on the copy:** migration 2 ms; classifying the 924 settled rows 14 ms with no fetch; the real settle pass 14 seconds.

**No legacy row settled during the dry run,** so "pending rows change only in `outcome_json`, from NULL" was not exercised on real data. It is covered by the unit tests, including a database trigger that fails on any update to `signal_session`, `recorded_at` or an existing outcome.

## 5. Classification of the existing archive

After stage B, classifier version 1:

| Hold | Market | Clean | Potentially contaminated | Invalid | Awaiting entry | Total |
|---|---|---|---|---|---|---|
| 5 | US | 533 | 202 | 0 | 2 | 737 |
| 5 | Norway | 25 | 115 | 0 | 0 | 140 |
| 5 | Europe | 34 | 88 | 0 | 0 | 122 |
| 5 | India (`.NS`) | 0 | 0 | 37 | 0 | 37 |
| 20 | US | 21 | 8 | 0 | 20 | 49 |
| 20 | Norway | 0 | 5 | 0 | 0 | 5 |
| 20 | Europe | 0 | 4 | 0 | 0 | 4 |
| **All** | | **613** | **422** | **37** | **22** | **1,094** |

- **Qualified rows:** 148 clean, 86 potentially contaminated, 8 invalid, 3 awaiting entry.
- **Invalid:** all 37 are `unsupported_exchange` (Indian rows from October). No row has a malformed timestamp.
- **Awaiting entry:** 20 are signals from 2026-11-12 whose entry session had not finished when the dry run ran. Two are overdue: **WBD** (signal 2026-10-05) and **KVUE** (signal 2026-10-30). Yahoo returns no bar after their signal date. They were silently pending before; they are now named in both reports.
- **Against the plan's estimate** (634 / 423 / 37): the plan guessed entry dates for pending rows; here 22 of them are not classifiable yet, and the rest are classified from their real entry bar.
- **All 1,094 rows are historical.** The clean test has no rows until the first screen after deploy.

For audit only, the historical clean class at the 5-session hold: 520 finished rows over 25 entry sessions; qualified minus baseline +0.38 points, standard error 0.59. That is noise, and the outcomes had been seen before the classification existed. It is not a result.

## 6. Where the implementation differs from the plan

| Plan | Implementation | Why |
|---|---|---|
| Four static reason codes | A fifth, `bad_signal_session` | A row whose signal date cannot be parsed must be classified, not raise. No such row exists today. |
| Tripwire test "forced by a deliberately wrong exchange table" | The test replaces the entry selection with a broken one | The selection and the classifier read the same table, so a wrong table cannot make them disagree. |
| Day-by-day page: selected series and class | Also a hold selector | Without it the table averaged the 5-session and 20-session rows of one day together. |
| (not in the plan) | The bot opens the archive at startup | Otherwise the dashboard would show "not prepared yet" until the first scheduled job, up to hours after a deploy. |
| (not in the plan) | A price-fetch fault on a row whose hold cannot be over yet is logged, not counted as a failed run | Awaiting-entry rows are now fetched at every pass; counting each hiccup would fail the job routinely. Faults on rows that could be finished are counted as before. |

Classifier activation is exactly as the plan says: `activate_classifier` exists and is tested (refused while incomplete, logged, reversible), and nothing calls it automatically.

## 7. A correction to `independent-review.md`

Section 4.4 of the review says 112 five-session rows were stuck, "well past" their settlement date. That was wrong. 110 of them were recent signals whose hold had not ended. **Two rows are stuck** (WBD, KVUE). The underlying point stands, at a much smaller size.

## 8. Before deploying

1. **One instruction could not be followed.** The request says to include "the three checks above". Only two re-review points reached this session (invalid versus awaiting entry; the classifier version in the view), and both are in revision 3 and implemented. If there is a third, it has not been applied.
2. **Opening times** are confirmed from primary sources for Xetra, Milan, Madrid, Paris, Amsterdam, Brussels and Oslo; New York by the owner.
3. **Deploy both images together.** A new bot with an old dashboard would show contaminated history as valid.
4. **On the Pi, around the deploy:**
   ```
   python3 scripts/forward_test_integrity.py snapshot <db> > before.json     # before
   scripts/deploy.sh
   python3 scripts/forward_test_integrity.py snapshot <db> > after.json      # after the first settle run
   python3 scripts/forward_test_integrity.py compare before.json after.json  # must print no problems
   ```
   The database lives in a Docker volume; run the script inside the bot container or against a backup copy.
5. **After deploy,** the clean test starts from zero. On the Pi the hold is 20 sessions, so the first finished clean rows arrive about four weeks later. The dashboard home page shows an explicit empty state until then, with the historical classes one click away.
6. **Oslo and European signals will enter about 40 hours after the close they are dated,** because yesterday's bar is first usable at the 19:00 run. That is what the bot can deliver today, and the test now measures it.

## 9. Rollback

Redeploy the previous images. The added column, tables and view stay and are ignored by the old code. No backup restore is needed, because no existing row was modified. A new-policy row that an old build settles is reported invalid by the view when the fix returns.
