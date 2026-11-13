# Forward-test integrity fix: plan

Plan date: 2026-11-13. Repository and Pi at `d0def12`. Nothing is implemented here.
Addresses `independent-review.md` section 4.1 (task N1 in its section 9).

**Rule to enforce:** a forward-test entry never uses an opening price that occurred at or before the moment the signal was recorded.

Evidence tags: **R** = read in the repository; **L** = read-only query on the Pi's database (`mode=ro`); **U** = not verified.

---

## 1. Root cause

### How a signal is calculated, archived and recorded (R)

| Step | Where | What happens |
|---|---|---|
| 1. Trigger | `bot.py:50` | The briefing runs at 07:00 and 19:00 in `TZ` (Europe/Oslo on the Pi). `/recommendations` and similar commands run the same screen at any time. |
| 2. Fetch | `indicators.py:195` | Daily bars are fetched and passed through `_drop_in_progress_bar`, which removes any bar dated today in the exchange's timezone, even if that market has closed. |
| 3. Signal date | `indicators.py:254` | `session` is the date of the last bar with a close. This becomes `signal_session`. |
| 4. Staleness | `screener.py:250-286` | A symbol whose `session` is older than the newest in the screen is marked `stale`. It is still archived, under its older date. |
| 5. Archive | `screener.py:288-304`, `history_store.py:150-181` | One row per symbol and `signal_session`. `recorded_at = datetime.now(timezone.utc).isoformat()`. `ON CONFLICT DO NOTHING`: the first write wins. |
| 6. Settle | `forward_test.py:50-52` | Entry is the first bar dated after `signal_session`. `recorded_at` is never consulted. |

### The defect

Step 6 assumes the signal existed at the close of `signal_session`. It actually exists from `recorded_at`, which can be a day later. Three patterns produce an entry in the past:

1. **Oslo and EU names, every day.** L: at 07:00 the Oslo and EU bars for yesterday are not yet usable, so those symbols show the session before and are marked stale against the US. Example: on 2026-11-13 at 07:00, `EQNR.OL` was screened with session 2026-11-11 and gate `stale`. Yesterday's signal is first archived at 19:00 today, and the entry is today's 09:00 open, ten hours earlier. Whether Yahoo omits the bar or returns it empty at 07:00 is **U**.
2. **US names found by evening discovery.** At 19:00 Oslo time New York has been open for hours. Yahoo's mover screens add stocks that are up today; the signal is dated yesterday; the entry is this morning's open. This selects on the outcome.
3. **Any on-demand screen** run after a market has opened.

Published picks already guard against this (`history_store.py:547-553`: entry date must be after the publication date). The forward test has no equivalent.

### `recorded_at` format (L)

All 1,094 rows are ISO 8601 in UTC with an explicit offset, for example `2026-10-04T05:00:02.417383+00:00`. None is naive. It can be compared directly with an exchange-local opening time.

---

## 2. Existing data impact

Every row was classified by comparing `recorded_at` with the opening time of its entry session (opening times in section 5). For settled rows the stored `entry_session` was used. For pending rows the entry date was estimated as the next weekday after the signal.

| Hold | Market | Clean | Potentially contaminated | Invalid |
|---|---|---|---|---|
| 5 | US | 534 | 203 | 0 |
| 5 | Norway | 25 | 115 | 0 |
| 5 | Europe (`.DE .PA .AS .MI .MC .BR`) | 34 | 88 | 0 |
| 5 | India (`.NS`) | 0 | 0 | 37 |
| 20 | US | 41 | 8 | 0 |
| 20 | Norway | 0 | 5 | 0 |
| 20 | Europe | 0 | 4 | 0 |
| **All** | | **634** | **423** | **37** |

- **Qualified rows:** 151 clean, 86 potentially contaminated, 8 invalid.
- **Estimated entries:** 56 of the 423 are pending rows (39 at 5 sessions, 17 at 20).
- **Not sensitive to the exact opening time.** No row was recorded within three hours of its entry open. The nearest clean row has 7.5 hours of lead.
- **Norway and Europe are mostly lost:** 212 of 271 rows. Their clean subset is too small to read.
- **Dates still usable in the clean subset:** 27 of 28 five-session dates have both a clean qualified row and a clean baseline.

### Two corrections to `independent-review.md`

That file is not edited by this plan. Its section 4.1 should be read with these:

1. It reports 408 affected rows (37%). The per-exchange method here gives **423 (39%)**. The earlier count used a fixed 15:30 Oslo-time US open and a simpler rule.
2. It counts 163 "Europe" rows. **37 of those are Indian (`.NS`)** rows from October. The archive has 126 European rows. India has no supported exchange entry, so those rows are invalid for this test.

The direction and rough size of the review's finding are unchanged.

---

## 3. Proposed data model

### 3.1 One new column on `signal_archive`

| Column | Type | Meaning |
|---|---|---|
| `entry_policy` | `TEXT`, nullable | How the entry session is chosen. `NULL` = legacy (first session after `signal_session`). `after-recorded-v1` = first session whose open is strictly after `recorded_at`. Set once at insert, like `horizon_sessions` and `cost_bps_per_side`. |

It is a separate column, not part of `rule_id`: the gates did not change, the measurement did. The report already filters on rule and horizon (`forward_test.py:243-244`); it gains a third filter.

### 3.2 One new side table for the classification

```sql
CREATE TABLE IF NOT EXISTS signal_archive_quality (
    symbol TEXT NOT NULL,
    signal_session TEXT NOT NULL,
    classifier_version INTEGER NOT NULL,
    quality TEXT NOT NULL,          -- clean | potentially_contaminated | invalid
    reason TEXT NOT NULL,
    entry_open_at TEXT,             -- ISO 8601 with offset; NULL when unknown
    classified_at TEXT NOT NULL,
    PRIMARY KEY (symbol, signal_session, classifier_version)
)
```

- **Why a side table:** archive rows are never touched to classify them, and a later, better classifier adds rows under a new version instead of overwriting.
- **Why stored at all:** the dashboard is a separate image that reads with SQL. A join is simpler and safer than a second copy of the logic.
- **Written by** the settle job, insert-only (`INSERT OR IGNORE`), once the entry bar exists.

### 3.3 Classification rules (version 1)

| Class | Rule |
|---|---|
| `clean` | The entry session's open is strictly after `recorded_at`. |
| `potentially_contaminated` | The entry session's open is at or before `recorded_at`. "Potentially" because the size of the bias depends on how the symbol entered the screen, which the archive does not record. |
| `invalid` | The row cannot be evaluated: no exchange entry for the suffix; `recorded_at` missing, unparseable or without an offset; `recorded_at` not after the signal session's date in exchange time; a stored outcome whose `entry_session` is not after `signal_session`; or an outcome computed under a different policy from the row's `entry_policy`. |

### 3.4 Outcome JSON for new-policy rows

Two extra keys: `entry_policy` and `entry_open_at` (exchange-local ISO timestamp of the open used). Legacy outcomes keep their existing shape and are never rewritten. The extra keys make "no price earlier than `recorded_at`" checkable from stored data alone.

---

## 4. Migration approach

1. In `TradeHistoryStore._connection()`, after the existing `CREATE TABLE IF NOT EXISTS`: read `PRAGMA table_info(signal_archive)` and run `ALTER TABLE signal_archive ADD COLUMN entry_policy TEXT` only if the column is absent. Create the side table. Both are idempotent.
2. Existing rows get `entry_policy = NULL` automatically. No `UPDATE` touches them.
3. `archive_signal` writes `after-recorded-v1` for every new row.
4. The settle job classifies legacy rows into the side table on its first run after deploy.
5. `scripts/deploy.sh` already takes an integrity-checked database backup before each deploy; no extra step is needed.

**Legacy rows that are still pending** (112 at 5 sessions, 58 at 20) keep settling under the legacy rule, so each historical series stays internally consistent. They are classified like any other legacy row and never enter the new series.

---

## 5. Exchange session handling

### 5.1 Which days are sessions

The symbol's own daily bars are the calendar: a weekend or holiday has no bar. This is how the code already counts sessions (`forward_test.py:50-55`). No calendar library is added.

### 5.2 When a session opens

A small table, keyed by Yahoo suffix:

| Suffix | Exchange | Timezone | Regular open |
|---|---|---|---|
| (none) | NYSE, Nasdaq | America/New_York | 09:30 |
| `.OL` | Oslo Børs | Europe/Oslo | 09:00 |
| `.DE` | Xetra | Europe/Berlin | 09:00 |
| `.PA` | Euronext Paris | Europe/Paris | 09:00 |
| `.AS` | Euronext Amsterdam | Europe/Amsterdam | 09:00 |
| `.BR` | Euronext Brussels | Europe/Brussels | 09:00 |
| `.MI` | Borsa Italiana | Europe/Rome | 09:00 |
| `.MC` | Bolsa de Madrid | Europe/Madrid | 09:00 |

- Opening times are from memory and must be confirmed against each exchange's published hours before implementation (**U**).
- Any other suffix has no entry. Its rows are never settled under the new policy and are classified `invalid`. This covers `.NS`, `.L`, `.SW`, `.ST`, `.CO`, `.HE` until someone adds and tests them.
- The table's timezone must equal the timezone on the fetched history. A mismatch raises and the row stays pending.

### 5.3 Selecting the entry

For each bar after `signal_session`, in order:

```
open_at = datetime.combine(bar_date, open_time, tzinfo=exchange_zone)
if open_at > recorded_at:   # both timezone-aware
    entry = this bar
    break
```

| Case | Result |
|---|---|
| Recorded before the open | Same-day open |
| Recorded at or after the open | Next session |
| Recorded after the close | Next session |
| Friday after the close | Monday, or the next session with a bar |
| Recorded on a holiday | Next session with a bar |
| Daylight saving | `zoneinfo` gives the correct offset per date, including the weeks when the US and Europe differ |
| Early-close days | The open is unaffected |
| No later bar yet | Pending |

### 5.4 Known limits

- A delayed or halted open still prints as the bar's `Open`, at a time later than the table says. The rule stays safe: the real price is later than assumed, never earlier.
- A session missing from Yahoo's data moves the entry one session late. That is conservative, not contaminating.
- A signal recorded one minute before the open counts as clean although nobody could act on it. No lead margin is proposed, because the scheduled runs are hours from any open. A margin is a one-line change if on-demand screens make it matter.

---

## 6. Report and statistics changes

1. **Main section: the clean series.** Rows with the current rule, the current horizon and `entry_policy = after-recorded-v1`. `MIN_PAIRED_DATES` counts only these. The section states its start date.
2. **Historical section, per horizon.** Row counts by class. Statistics for the clean subset only, labelled "historical, before the entry fix, not part of the test". Contaminated and invalid rows are shown as counts, never averaged into anything.
3. **Comparable cohorts.** The baseline and the qualified cohort are filtered by the same policy and class before pairing. For the new series they are paired by **entry session**, not signal date: a 07:00 row and a 19:00 row with the same signal date now enter on different days, and pairing them by signal date would compare different holding windows.
4. **Footer wording** changes from "entry is the next session's open" to "entry is the first open after the signal was recorded".
5. **Dashboard** (`streamlit/app.py:50`, `:377`): join the side table, default to clean rows, show the class counts.

---

## 7. Test plan

All tests use synthetic bars with a timezone-aware index, as `tests/test_forward_test.py` already does. No network.

### Entry selection

| # | Market | Recorded at (UTC) | Bars present | Expected entry |
|---|---|---|---|---|
| 1 | US | Tue 2026-11-10 06:00 (07:00 Oslo) | Mon–Fri | Tue 11-10 (before 09:30 New York) |
| 2 | US | Tue 2026-11-10 18:00 (13:00 New York) | Mon–Fri | Wed 11-11 |
| 3 | US | Tue 2026-11-10 22:30 (after close) | Mon–Fri | Wed 11-11 |
| 4 | US | Fri 2026-11-13 22:30 | No weekend bars | Mon 11-16 |
| 5 | US | Wed 2026-11-25 22:30 | No bar Thu 11-26 (Thanksgiving) | Fri 11-27 |
| 6 | US | Thu 2026-11-26 15:00 (on the holiday) | No bar Thu 11-26 | Fri 11-27 |
| 7 | US, DST | Tue 2026-10-27 13:45 (New York still on summer time; open 13:30 UTC) | Mon–Fri | Wed 10-28 |
| 8 | US, DST | Tue 2026-11-03 13:45 (winter time; open 14:30 UTC) | Mon–Fri | Tue 11-03 |
| 9 | US, DST | Mon 2027-03-15 13:45 (US summer time started 03-14; open 13:30 UTC) | Mon–Fri | Tue 03-16 |
| 10 | Oslo | Tue 2026-11-10 06:00 (07:00 Oslo) | Mon–Fri | Tue 11-10 |
| 11 | Oslo | Tue 2026-11-10 18:00 (19:00 Oslo) | Mon–Fri | Wed 11-11 |
| 12 | Oslo | Thu 2026-05-14 10:00 (Ascension Day) | No bar Thu 05-14 | Fri 05-15 |
| 13 | Oslo, DST | Tue 2027-03-30 06:30 (summer time from 03-28; open 07:00 UTC) | Tue–Fri | Tue 03-30 |
| 14 | Xetra | Tue 2026-11-10 18:00 | Mon–Fri | Wed 11-11 |
| 15 | Paris | Thu 2026-04-30 18:00 | No bar Fri 05-01 (Labour Day) | Mon 05-04 |
| 16 | Any | Exactly the opening instant | Mon–Fri | Next session (strictly after) |
| 17 | `.NS` | Any | Any | Raises; row stays pending; classified `invalid` |
| 18 | US | History timezone differs from the table | Any | Raises; row stays pending |

Holiday dates in rows 5, 12 and 15 are fixtures: the test supplies bars without that date. They do not depend on a calendar being correct.

### Invariant

- For every settled new-policy outcome, `entry_open_at > recorded_at`. Run over all rows produced by the tests above, plus a randomised test: random `recorded_at` across two years and all eight exchanges.

### Preservation

- Build a store with legacy rows, some settled and some pending. Hash every original column of every row. Run the migration twice and one settle pass. Assert: the hashes of settled legacy rows are identical; pending legacy rows changed only in `outcome_json`, from `NULL`; `entry_policy` is `NULL` on all of them.
- A legacy pending row settles with the legacy entry, not the new one.
- The migration is idempotent on a database that already has the column.

### Classification

- A legacy row recorded before its entry open is `clean`; after it, `potentially_contaminated`; unknown suffix, naive timestamp or inconsistent outcome is `invalid`.
- Reclassifying inserts nothing new under the same version.

### Report

- Contaminated and invalid rows change no number in the clean section.
- Legacy rows never count toward `MIN_PAIRED_DATES`.
- A date where only the baseline has clean rows contributes no pair.
- Two rows with the same signal date and different entry sessions are not paired with each other.
- Existing tests for rule and horizon exclusion still pass.

### Before merge

`ruff check news_bot.py newsbot tests` and the full unit suite, also inside the built image (Python 3.15).

---

## 8. Backward compatibility

- **Readers.** `archive_rows()` builds dicts from `SELECT *`, so a new column is harmless. `backup.py:49` only counts rows. The dashboard selects named columns.
- **Old code against the new schema.** `archive_signal` in the old code names its columns, so `entry_policy` is `NULL` and the row is legacy, which is exactly how old code would settle it.
- **The one unsafe mix:** a row written as `after-recorded-v1` and later settled by old code would get a legacy entry under a new-policy label. The outcome's own `entry_policy` key catches this: on mismatch the row is classified `invalid` and excluded.
- **`rule_id`, `horizon_sessions`, `cost_bps_per_side`** are unchanged. The roadmap's `-h{N}` suffix (P1-2) is not needed and is not part of this.
- **Published picks** keep their own date-level guard. It is stricter than this rule and is left alone.

---

## 9. Rollback plan

1. Redeploy the previous image (`scripts/deploy.sh` tags one before every deploy) or revert the commit.
2. Leave the new column and the side table in place. Old code ignores both.
3. Rows written while the fix was live keep `entry_policy = after-recorded-v1`. Any of them settled by old code are excluded as `invalid` when the fix returns.
4. Nothing needs restoring from backup, because no legacy row is modified. If the database itself is damaged, restore the pre-deploy backup; the cost is the rows archived since.

Rollback trigger: any legacy row whose hash changed, any new-policy outcome with `entry_open_at <= recorded_at`, or the settle job failing for every row.

---

## 10. Acceptance criteria

1. No settled new-policy row has `entry_open_at <= recorded_at`. Checked by a test and by a read-only query on the Pi a week after deploy.
2. On the Pi, a checksum over `symbol, signal_session, recorded_at, outcome_json` for the 924 rows settled before the deploy is identical before and after.
3. After the first settle run, the side table holds one version-1 row for every legacy row with a known entry, and the totals match section 2 (634 / 423 / 37, allowing for the 56 estimated entries).
4. `/forwardtest` shows a clean series starting at the deploy date with zero legacy rows in it, and a separate historical section with class counts.
5. The first Oslo row recorded at 19:00 after deploy enters at the next day's open. The first US row recorded at 07:00 enters the same day.
6. All tests in section 7 pass on Python 3.15 in the built image.
7. `.NS` rows are reported as invalid and nothing raises.

---

## 11. Recommended minimal implementation

One pull request, in this order:

1. **Exchange table** (suffix → timezone, open time) with a lookup that returns nothing for unknown suffixes.
2. **Entry selection** in `forward_outcome`: choose by `recorded_at` when `entry_policy = after-recorded-v1`, by `signal_session` when `NULL`. Add the two outcome keys for the new policy.
3. **Schema:** add `entry_policy`; create `signal_archive_quality`; `archive_signal` writes the new policy.
4. **Classifier:** a pure function plus the insert-only write in the settle job.
5. **Report:** three-way filter, clean and historical sections, pairing by entry session.
6. **Dashboard:** join and default filter.
7. **Tests** from section 7.

Deliberately left out:

- A calendar library.
- Any change to the screen, the gates or `rule_id`.
- The overlap correction for `MIN_PAIRED_DATES` (roadmap P1-6); it builds on this.
- A `universe_source` column (roadmap P1-7). It cannot be backfilled, so every day without it is lost; adding it in the same migration is cheap and worth the owner's consideration, but it is not needed for this fix.

**After deploy:** the 20-session clean evaluation restarts from the first screen that archives new rows. The 58 existing 20-session rows stay in the historical section. The pre-registered protocol (decision D4) should be frozen only after this is live.

---

## 12. Risks

| Risk | Effect | Mitigation |
|---|---|---|
| **Oslo and EU signals will be about 40 hours old at entry.** A signal from Monday's close is recorded Tuesday 19:00 and enters Wednesday 09:00. | The clean test will measure what the bot can really deliver for these markets, which may be worse than the legacy numbers suggested. | This is the honest result, not a defect of the fix. Making European signals timelier (using the finished same-day bar at 19:00) is a signal change: it needs its own decision and a new `rule_id`, and must not be slipped into this series. |
| **The clean series starts from zero.** | No 20-session result before roughly mid-December 2026, and no verdict for much longer (review section 5). | Accepted. The historical clean subset stays visible, labelled. |
| **An opening time in the table is wrong.** | A row could be classed clean when it is not. | Confirm each time against the exchange before merge. No existing row is within three hours of an open, so the historical classification is robust to small errors. |
| **Rollback while new-policy rows are pending.** | Those rows get a legacy entry. | Detected by the outcome's policy key; excluded as `invalid`. |
| **Pairing by entry session changes how dates are counted.** | The new series is not directly comparable with the legacy paired figures. | Intended. The report labels the two sections separately. |
| **Evening discovery still shapes the baseline.** Movers found at 19:00 now enter at the next open with no look-ahead, but they are a different kind of stock from the watchlist. | The baseline's make-up depends on the time of day. | Not a look-ahead issue. `universe_source` would allow the report to split it. |
| **Yahoo omits a real session.** | Entry one session late. | Conservative. The existing bad-price check still applies. |
| **On-demand screens just before an open.** | A clean row that nobody could act on. | Add a lead margin if it occurs; none exists in the data today. |
| **Classification of the 56 pending legacy rows is estimated here.** | Final counts may differ slightly from section 2. | The settle job classifies each from its real entry bar. |
