# Forward-test integrity fix: plan

Plan date: 2026-11-13, revision 2. Analysis done at `d0def12`. Nothing is implemented here.
Addresses `independent-review.md` section 4.1 (task N1 in its section 9).

**Rule to enforce:** a forward-test entry never uses an opening price that occurred at or before the moment the signal was recorded.

Evidence tags: **R** = read in the repository; **L** = read-only query on the Pi's database (`mode=ro`); **U** = not verified.

### What revision 2 changes

| Topic | Change | Section |
|---|---|---|
| Dashboard | Every view that shows performance is listed, with the rows it may use. One database view defines the selection for both Telegram and the dashboard. | 3.5, 7 |
| Pairing | The holding-period key is `entry_session` everywhere: both comparisons, the minimum-date count and every dashboard chart. | 6 |
| Timezones | `zoneinfo` only; the fetched history's timezone is verified; timezone-naive history is rejected. | 5.3 |
| Tests | Malformed `recorded_at`, naive history, the opening instant, repeated migration and classification, an old version settling a new row, and every dashboard aggregation. | 8 |
| Non-goal | Historical outcomes are never recomputed to make them clean. | 4 |

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

It is a separate column, not part of `rule_id`: the gates did not change, the measurement did.

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

- **Why a side table:** archive rows are never touched to classify them, and a later classifier adds rows under a new version instead of overwriting.
- **Every row is classified**, legacy and new. A new-policy row is `clean` only after its settled outcome has been checked (section 3.3).
- **Written by** the settle job, insert-only (`INSERT OR IGNORE`), once the entry bar exists.

### 3.3 Classification rules (version 1)

| Class | Rule |
|---|---|
| `clean` | The entry session's open is strictly after `recorded_at`, and the stored outcome was computed under the row's own `entry_policy`. |
| `potentially_contaminated` | The entry session's open is at or before `recorded_at`. "Potentially" because the size of the bias depends on how the symbol entered the screen, which the archive does not record. |
| `invalid` | The row cannot be evaluated. Reasons, each stored as text: no exchange entry for the suffix; `recorded_at` empty, unparseable or without an offset; `recorded_at` not after the signal session's date in exchange time; a stored outcome whose `entry_session` is not after `signal_session`; a new-policy row whose outcome has no `entry_policy` key or a different one (settled by an old version). |

A row with no entry bar yet has no classification. It is counted as "awaiting entry" and is in no average.

### 3.4 Outcome JSON for new-policy rows

Two extra keys: `entry_policy` and `entry_open_at` (exchange-local ISO timestamp of the open used). Legacy outcomes keep their existing shape and are never rewritten. The extra keys make "no price earlier than `recorded_at`" checkable from stored data alone.

### 3.5 One database view that defines the selection

The bot and the dashboard are separate images and cannot share Python code. A view gives them one definition:

```sql
CREATE VIEW IF NOT EXISTS signal_archive_evaluated AS
SELECT a.*,
       CASE WHEN a.entry_policy = 'after-recorded-v1' THEN 'clean_test' ELSE 'historical' END AS series,
       COALESCE(q.quality, 'awaiting_entry') AS quality,
       q.reason AS quality_reason,
       q.entry_open_at,
       json_extract(a.outcome_json, '$.entry_session')     AS entry_session,
       json_extract(a.outcome_json, '$.exit_session')      AS exit_session,
       json_extract(a.outcome_json, '$.net_return_pct')    AS net_return_pct,
       json_extract(a.outcome_json, '$.excess_return_pct') AS excess_return_pct
FROM signal_archive a
LEFT JOIN signal_archive_quality q
  ON q.symbol = a.symbol AND q.signal_session = a.signal_session
 AND q.classifier_version = (SELECT MAX(classifier_version) FROM signal_archive_quality)
```

The two selections every consumer uses:

| Name | Filter | Used for |
|---|---|---|
| **Clean evaluation** | `series = 'clean_test' AND quality = 'clean'` | The forward test. The only rows that count toward any verdict. |
| **Historical analysis** | `series = 'historical'`, always split by `quality` | Audit of the period before the fix. Never pooled across classes. |

No consumer queries `signal_archive` directly for performance after this change.

---

## 4. Migration approach

1. In `TradeHistoryStore._connection()`, after the existing `CREATE TABLE IF NOT EXISTS`: read `PRAGMA table_info(signal_archive)` and run `ALTER TABLE signal_archive ADD COLUMN entry_policy TEXT` only if the column is absent. Create the side table and the view. All three are idempotent.
2. Existing rows get `entry_policy = NULL` automatically. No `UPDATE` touches them.
3. `archive_signal` writes `after-recorded-v1` for every new row.
4. The settle job classifies rows into the side table, starting with its first run after deploy.
5. `scripts/deploy.sh` already takes an integrity-checked database backup before each deploy; no extra step is needed.

**Legacy rows that are still pending** (112 at 5 sessions, 58 at 20) keep settling under the legacy rule, so each historical series stays internally consistent. They are classified like any other legacy row and never enter the clean evaluation.

**Non-goal: no historical outcome is recomputed.** A contaminated row is not re-settled under the new policy to turn it into a clean one. Its `signal_session`, `recorded_at` and `outcome_json` stay as written, and its class stays `potentially_contaminated`. The clean evaluation is built only from rows recorded after the fix.

---

## 5. Exchange session handling

### 5.1 Which days are sessions

The symbol's own daily bars are the calendar: a weekend or holiday has no bar. This is how the code already counts sessions (`forward_test.py:50-55`). No calendar library is added.

### 5.2 When a session opens

A small table, keyed by Yahoo suffix:

| Suffix | Exchange | Timezone (`zoneinfo` key) | Regular open |
|---|---|---|---|
| (none) | NYSE, Nasdaq | `America/New_York` | 09:30 |
| `.OL` | Oslo Børs | `Europe/Oslo` | 09:00 |
| `.DE` | Xetra | `Europe/Berlin` | 09:00 |
| `.PA` | Euronext Paris | `Europe/Paris` | 09:00 |
| `.AS` | Euronext Amsterdam | `Europe/Amsterdam` | 09:00 |
| `.BR` | Euronext Brussels | `Europe/Brussels` | 09:00 |
| `.MI` | Borsa Italiana | `Europe/Rome` | 09:00 |
| `.MC` | Bolsa de Madrid | `Europe/Madrid` | 09:00 |

- Oslo at 09:00 and New York at 09:30 were confirmed by the owner against published market hours. The other six are to be confirmed against each exchange before implementation (**U**).
- Any other suffix has no entry. Its rows are never settled under the new policy and are classified `invalid`. This covers `.NS`, `.L`, `.SW`, `.ST`, `.CO`, `.HE` until someone adds and tests them.

### 5.3 Timezone rules

1. **`zoneinfo.ZoneInfo` only.** Every opening time is built as `datetime.combine(bar_date, open_time, tzinfo=ZoneInfo(key))`. No fixed UTC offsets, no host-local time.
2. **`recorded_at` must carry an offset.** A naive or unparseable value raises; the row stays pending and is classified `invalid`.
3. **Timezone-naive history is rejected.** If the fetched history's index has no timezone, the new-policy path raises and the row stays pending. It does not fall back to the host's date. (Today `_drop_unfinished_bar` falls back silently, `indicators.py:179-180`; that fallback stays for other callers but is not reachable for a new-policy settlement.)
4. **The history's timezone is verified against the table.** The check passes when the history's timezone and the table's zone give the same UTC offset on the signal date, the entry date and the exit date. On any mismatch the row stays pending and a warning names both zones. Offsets are compared, not names, because the exact zone name Yahoo returns per exchange is **U**; identical offsets on those dates guarantee identical bar dates and opening instants. Implementation records the names from one live fetch per suffix.
5. **US and European DST change on different dates.** In those weeks New York opens at 14:30 Oslo time instead of 15:30. Rule 1 handles it; tests 7 to 9 and 13 in section 8 pin it.

### 5.4 Selecting the entry

For each bar after `signal_session`, in order:

```
open_at = datetime.combine(bar_date, open_time, tzinfo=ZoneInfo(key))
if open_at > recorded_at:   # both timezone-aware; strictly after
    entry = this bar
    break
```

| Case | Result |
|---|---|
| Recorded before the open | Same-day open |
| Recorded exactly at the opening instant | Next session |
| Recorded after the open, or after the close | Next session |
| Friday after the close | Monday, or the next session with a bar |
| Recorded on a holiday | Next session with a bar |
| Early-close days | The open is unaffected |
| No later bar yet | Pending |

### 5.5 Known limits

- A delayed or halted open still prints as the bar's `Open`, at a time later than the table says. The rule stays safe: the real price is later than assumed, never earlier.
- A session missing from Yahoo's data moves the entry one session late. That is conservative, not contaminating.
- A signal recorded one minute before the open counts as clean although nobody could act on it. No lead margin is proposed, because the scheduled runs are hours from any open. A margin is a one-line change if on-demand screens make it matter.

---

## 6. Pairing and date semantics

**One rule everywhere: an observation belongs to its `entry_session`, not its `signal_session`.**

Under the new policy, two rows with the same `signal_session` can enter on different days. Example: a US signal dated Monday recorded Tuesday 07:00 enters Tuesday; another US signal dated Monday recorded Tuesday 19:00 enters Wednesday. They cover different holding periods and must not be averaged or paired as one date.

| Where | Today | After the fix |
|---|---|---|
| Qualified versus baseline (`paired_vs_baseline`) | Grouped by `signal_session` | Grouped by `entry_session` |
| Top five versus baseline | Grouped by `signal_session` | Grouped by `entry_session` |
| `MIN_PAIRED_DATES` | Signal dates with both groups | Entry sessions that have at least one clean qualified row **and** a clean baseline, in the clean evaluation only |
| Report header ("N signals over M sessions") | Signal dates | Entry sessions, with the first and last entry date |
| Dashboard `daily()` and every chart built on it | Grouped by signal `day` | Grouped by `entry_session` and hold |
| Dashboard running-difference line | X axis = signal day | X axis = entry session |

Further rules:

- **Same filter on both sides.** The cohort and its baseline are taken from the same selection (section 3.5) before grouping. A contaminated baseline row can never be paired with a clean pick.
- **Legacy rows use the same key.** The historical section also groups by `entry_session` (stored in each legacy outcome), so there is one code path. Historical figures may shift slightly from what was shown before; the section is labelled as historical either way.
- **Rows without an entry yet** have no `entry_session` and are in no group.
- **"Top five as delivered"** means rank 5 or better within its own screen run. Two runs can share an entry session, so a group can hold more than five such rows. The report says so.
- **Known limit:** two markets with the same entry session can exit a day apart around a local holiday. This is unchanged from today and is noted in the report footer.

---

## 7. Report and dashboard changes

### 7.1 Telegram `/forwardtest`

1. **Main section: clean evaluation.** Current rule, current hold, clean-evaluation rows only. States the policy name and the first entry date.
2. **Historical section, per hold.** Row counts by class. Averages for the `clean` class only, labelled "historical, before the entry fix, not part of the test". Contaminated and invalid rows appear as counts.
3. **Counts line** in both sections: clean / potentially contaminated / invalid / awaiting entry.
4. **Footer** changes from "entry is the next session's open" to "entry is the first open after the signal was recorded".

### 7.2 Dashboard (`streamlit/app.py`)

All archive reads go through one loader on `signal_archive_evaluated`. A series selector appears on every performance page: **Clean test** (default) or **Historical**. Choosing Historical adds a class selector that defaults to `clean`; the classes are shown one at a time, never pooled.

Every place that reads the archive today (R):

| View | Reads today | After the fix |
|---|---|---|
| `signals()` (`:42-55`) | `signal_archive`, all rows | The single loader on the view. Adds `series`, `quality`, `entry_policy`, `entry_session`. |
| `daily()` (`:58-71`) | Every completed row, by signal day | Only the selected series and class, by `entry_session`. Refuses a frame that mixes series or classes. |
| `scorecard()` / `metric_row()` (`:74-91`) | Output of `daily()` | Unchanged logic; input is already filtered. |
| Home: scorecards and running line (`:104-113`) | All rows, per hold | Clean evaluation by default. Empty state: "The clean test started on DATE; first results after N sessions." Historical is behind the selector. |
| Day by day: chart and table (`:147-166`) | All rows | Selected series and class. Count columns added per class. |
| One stock, "The bot and this stock" (`:353-368`) | All rows for the symbol | Averages from the selected series and class only. The row table shows every row with its class, so nothing is hidden. |
| What-if filters (`:409-459`) | All completed rows except `stale` | Selected series and class only. |
| Data collected (`:371-384`) | Counts per signal date | Still counts by signal date (it is a collection log, not performance). Adds columns: clean, potentially contaminated, invalid, awaiting entry. |
| Sent picks (`:169-195`) | `published_picks` | Unchanged. That table has its own entry guard and is not part of this defect. |
| Latest screen (`:122-144`) | `trade_metrics_history` | Unchanged. No performance figures. |

Rules for every performance view:

- **No contaminated, invalid or awaiting-entry row** in any average, hit rate, comparison, running total or chart of the clean evaluation.
- **Each page states what it shows:** series, policy name, rule, hold, and the four class counts.
- **Rules are not pooled.** The dashboard does not filter on `rule_id` today, while Telegram does. If more than one rule exists in the selection, the page asks which one.

To make this testable, the pure functions (`daily`, `scorecard`, the loader's filtering, the what-if selection) move from `app.py` into a small module with no Streamlit import. `app.py` keeps the pages.

**One difference that stays:** Telegram compares picks with *every screened symbol*; the dashboard compares picks with *skipped* symbols. The two differences will not be equal. Row counts and entry-session counts must be (acceptance criterion 5). Aligning the two definitions is a separate decision.

---

## 8. Test plan

All tests use synthetic bars with a timezone-aware index, as `tests/test_forward_test.py` already does. No network.

### 8.1 Entry selection

| # | Market | Recorded at (UTC) | Bars present | Expected entry |
|---|---|---|---|---|
| 1 | US | Tue 2026-11-10 06:00 (07:00 Oslo) | Mon–Fri | Tue 11-10 (before 09:30 New York) |
| 2 | US | Tue 2026-11-10 18:00 (13:00 New York) | Mon–Fri | Wed 11-11 |
| 3 | US | Tue 2026-11-10 22:30 (after close) | Mon–Fri | Wed 11-11 |
| 4 | US | Fri 2026-11-13 22:30 | No weekend bars | Mon 11-16 |
| 5 | US | Wed 2026-11-25 22:30 | No bar Thu 11-26 (Thanksgiving) | Fri 11-27 |
| 6 | US | Thu 2026-11-26 15:00 (on the holiday) | No bar Thu 11-26 | Fri 11-27 |
| 7 | US, DST mismatch | Tue 2026-10-27 13:45 (Europe on winter time, New York still on summer time; open 13:30 UTC) | Mon–Fri | Wed 10-28 |
| 8 | US, DST | Tue 2026-11-03 13:45 (both on winter time; open 14:30 UTC) | Mon–Fri | Tue 11-03 |
| 9 | US, DST mismatch | Mon 2027-03-15 13:45 (New York on summer time from 03-14, Europe not until 03-28; open 13:30 UTC) | Mon–Fri | Tue 03-16 |
| 10 | Oslo | Tue 2026-11-10 06:00 (07:00 Oslo) | Mon–Fri | Tue 11-10 |
| 11 | Oslo | Tue 2026-11-10 18:00 (19:00 Oslo) | Mon–Fri | Wed 11-11 |
| 12 | Oslo | Thu 2026-05-14 10:00 (Ascension Day) | No bar Thu 05-14 | Fri 05-15 |
| 13 | Oslo, DST | Tue 2027-03-30 06:30 (summer time from 03-28; open 07:00 UTC) | Tue–Fri | Tue 03-30 |
| 14 | Xetra | Tue 2026-11-10 18:00 | Mon–Fri | Wed 11-11 |
| 15 | Paris | Thu 2026-04-30 18:00 | No bar Fri 05-01 (Labour Day) | Mon 05-04 |
| 16 | US and Oslo | Exactly the opening instant, to the microsecond | Mon–Fri | Next session; one microsecond earlier gives the same day |
| 17 | `.NS` | Any | Any | Raises; row stays pending; classified `invalid` |

Holiday dates in rows 5, 12 and 15 are fixtures: the test supplies bars without that date.

### 8.2 Timezone validation

- History with a timezone-naive index: the new-policy settlement raises, the row stays pending, nothing is written.
- History in the wrong zone (US symbol, `Europe/Oslo` index): raises; row stays pending; the log line names both zones.
- History in a differently named zone with identical offsets on the signal, entry and exit dates: accepted.
- The host's `TZ` is set to three different zones in turn; every result in 8.1 is unchanged.

### 8.3 `recorded_at`

- Empty string, garbage text, a date with no time, and a naive timestamp: each raises on settlement for a new-policy row, and classifies as `invalid` with its own reason for either policy.
- A timestamp with a non-UTC offset (for example `+01:00`) gives the same entry as the equivalent UTC value.
- `recorded_at` earlier than the signal session's own date: `invalid`.

### 8.4 Invariant

- For every settled new-policy outcome, `entry_open_at > recorded_at`. Checked over all rows produced above, plus a randomised test: random `recorded_at` across two years and all eight exchanges.

### 8.5 Migration and preservation

- Build a store with legacy rows, some settled and some pending. Hash every original column of every row. Run the migration, then again, then a third time on a freshly opened connection. Assert: one `entry_policy` column, one side table, one view; the hashes of settled legacy rows are identical; `entry_policy` is `NULL` on all of them.
- After one settle pass, pending legacy rows changed only in `outcome_json`, from `NULL`, and settled with the legacy entry, not the new one.
- No statement issued by the classifier or the settle job updates `signal_session`, `recorded_at`, or a non-null `outcome_json`. Asserted by the hash test and by a trigger in the test database that fails on any such update.
- A contaminated legacy row is still contaminated, with its original outcome, after any number of settle passes. Nothing re-settles it.

### 8.6 Classification

- A legacy row recorded before its entry open is `clean`; after it, `potentially_contaminated`; unknown suffix or inconsistent outcome is `invalid`.
- Running the classifier twice inserts nothing the second time and changes no existing row.
- A row with no entry bar yet has no classification and shows as `awaiting_entry` in the view.
- **An old application version settles a new-policy row.** Simulated by writing a legacy-shaped outcome (no `entry_policy` key, entry by signal date) onto an `after-recorded-v1` row. Assert: classified `invalid` with reason "settled under another policy"; excluded from the clean evaluation; the outcome is not rewritten.

### 8.7 Pairing

- **Same signal session, different entry sessions.** Two qualified rows and four baseline rows share one `signal_session`; half enter on day D, half on D+1, with different returns. Assert: two paired observations, not one; each difference uses only rows from its own entry session; `MIN_PAIRED_DATES` counts two.
- The same fixture through the top-five comparison and through the dashboard's `daily()`: two rows out, matching the Telegram groups.
- An entry session where only the baseline has clean rows contributes no pair.
- A contaminated baseline row with an extreme return in the same entry session leaves the paired difference unchanged.
- Legacy rows never count toward `MIN_PAIRED_DATES`.

### 8.8 Report

- Contaminated, invalid and awaiting-entry rows change no number in the clean section.
- The historical section shows counts for all three classes and averages for `clean` only.
- Existing tests for rule and hold exclusion still pass.

### 8.9 Dashboard aggregations

One fixture: clean-evaluation rows with small known returns, plus legacy-clean, contaminated, invalid and awaiting-entry rows whose returns are extreme (for example +50%), so any leak changes the result visibly. For **each** aggregation, assert that the output equals the value computed by hand from the clean-evaluation rows alone:

- home scorecard, per hold;
- home running-difference line;
- day-by-day chart values and table averages;
- one-stock "average when picked" and "average when skipped";
- what-if: both scorecards and the running line;
- data-collected: the four class counts sum to the rows checked.

Also:

- With Historical selected, each class in turn gives the hand-computed value for that class only.
- `daily()` raises when given a frame that mixes series or classes.
- A structural test: `signal_archive` appears in `app.py` only inside the one loader, and only as `signal_archive_evaluated`.

### 8.10 Before merge

`ruff check` and the full unit suite, also inside the built image (Python 3.15). The dashboard image is built in CI for this change.

---

## 9. Backward compatibility

- **Readers.** `archive_rows()` builds dicts from `SELECT *`, so a new column is harmless. `backup.py:49` only counts rows. A view does not affect either.
- **Old code against the new schema.** `archive_signal` in the old code names its columns, so `entry_policy` is `NULL` and the row is legacy, which is exactly how old code would settle it.
- **The one unsafe mix:** a row written as `after-recorded-v1` and later settled by old code. Caught by the outcome's missing policy key, classified `invalid`, excluded (test in 8.6).
- **Old dashboard image against the new schema.** It keeps reading `signal_archive` directly and would show contaminated rows as before. The bot and dashboard deploy together from one compose file, so this only arises in a partial rollback.
- **`rule_id`, `horizon_sessions`, `cost_bps_per_side`** are unchanged. The roadmap's `-h{N}` suffix (P1-2) is not needed and is not part of this.
- **Published picks** keep their own date-level guard. It is stricter than this rule and is left alone.

---

## 10. Rollback plan

1. Redeploy the previous images (`scripts/deploy.sh` tags one before every deploy) or revert the commit. Roll back the bot and the dashboard together.
2. Leave the new column, the side table and the view in place. Old code ignores all three.
3. Rows written while the fix was live keep `entry_policy = after-recorded-v1`. Any of them settled by old code are excluded as `invalid` when the fix returns.
4. Nothing needs restoring from backup, because no legacy row is modified. If the database itself is damaged, restore the pre-deploy backup; the cost is the rows archived since.

Rollback trigger: any legacy row whose hash changed, any new-policy outcome with `entry_open_at <= recorded_at`, or the settle job failing for every row.

---

## 11. Acceptance criteria

1. No settled new-policy row has `entry_open_at <= recorded_at`. Checked by a test and by a read-only query on the Pi a week after deploy.
2. On the Pi, a checksum over `symbol, signal_session, recorded_at, outcome_json` for the 924 rows settled before the deploy is identical before and after.
3. After the first settle run, the side table holds one version-1 row for every legacy row with a known entry, and the totals match section 2 (634 / 423 / 37, allowing for the 56 estimated entries).
4. `/forwardtest` shows a clean evaluation starting at the deploy date with zero legacy rows in it, and a separate historical section with class counts.
5. For the same database, Telegram and the dashboard report the same number of clean-evaluation rows, the same number of entry sessions and the same first entry date.
6. Every dashboard performance view defaults to the clean evaluation and states the series, policy, rule, hold and class counts. No view can show an average that pools classes or series.
7. The first Oslo row recorded at 19:00 after deploy enters at the next day's open. The first US row recorded at 07:00 enters the same day.
8. Timezone-naive history and malformed `recorded_at` leave a row pending and never produce an outcome.
9. All tests in section 8 pass on Python 3.15 in the built image.
10. `.NS` rows are reported as invalid and nothing raises.

---

## 12. Recommended minimal implementation

One pull request, in this order:

1. **Exchange table** (suffix → `zoneinfo` key, open time) with a lookup that returns nothing for unknown suffixes. Confirm the six unverified opening times first.
2. **Entry selection** in `forward_outcome`: choose by `recorded_at` when `entry_policy = after-recorded-v1`, by `signal_session` when `NULL`. Timezone checks from section 5.3. Two extra outcome keys for the new policy.
3. **Schema:** add `entry_policy`; create `signal_archive_quality` and the `signal_archive_evaluated` view; `archive_signal` writes the new policy.
4. **Classifier:** a pure function plus the insert-only write in the settle job.
5. **Pairing:** group by `entry_session` in `_by_date`, the report header and `MIN_PAIRED_DATES`.
6. **Telegram report:** read the view; clean and historical sections; counts line; footer.
7. **Dashboard:** move the pure functions out of `app.py`; one loader on the view; series and class selectors; the per-view changes in section 7.2.
8. **Tests** from section 8, and the dashboard image build in CI.

Deliberately left out:

- A calendar library.
- Any change to the screen, the gates or `rule_id`.
- Recomputing any historical outcome.
- Aligning the dashboard's "skipped" comparison with Telegram's "every screened symbol".
- The overlap correction for `MIN_PAIRED_DATES` (roadmap P1-6); it builds on this.
- A `universe_source` column (roadmap P1-7). It cannot be backfilled, so every day without it is lost; adding it in the same migration is cheap and worth the owner's consideration, but it is not needed for this fix.

**After deploy:** the 20-session clean evaluation restarts from the first screen that archives new rows. The 58 existing 20-session rows stay in the historical section. The pre-registered protocol (decision D4) should be frozen only after this is live.

---

## 13. Risks

| Risk | Effect | Mitigation |
|---|---|---|
| **Oslo and EU signals will be about 40 hours old at entry.** A signal from Monday's close is recorded Tuesday 19:00 and enters Wednesday 09:00. | The clean test will measure what the bot can really deliver for these markets, which may be worse than the legacy numbers suggested. | This is the honest result, not a defect of the fix. Making European signals timelier (using the finished same-day bar at 19:00) is a signal change: it needs its own decision and a new `rule_id`, and must not be slipped into this series. |
| **The clean evaluation starts from zero.** | The dashboard home page is empty for the first weeks; no 20-session result before roughly mid-December 2026; no verdict for much longer (review section 5). | Accepted. An explicit empty state says when results will appear. The historical clean class stays one click away, labelled. |
| **An opening time in the table is wrong.** | A row could be classed clean when it is not. | Confirm each time against the exchange before merge. No existing row is within three hours of an open, so the historical classification is robust to small errors. |
| **Yahoo returns an unexpected timezone for an exchange.** | New-policy rows for that exchange stay pending. | Fails closed, with a warning that names both zones. Record the names from a live fetch during implementation. |
| **Rollback while new-policy rows are pending.** | Those rows get a legacy entry. | Detected by the outcome's policy key; excluded as `invalid`. |
| **Partial rollback: new bot, old dashboard.** | The dashboard shows contaminated history as valid while Telegram does not. | Deploy and roll back both images together. |
| **Pairing by entry session changes how dates are counted.** | New figures are not directly comparable with paired figures quoted before the fix, including the historical section's. | Intended. Both sections are labelled, and the report says which key is used. |
| **The dashboard refactor is larger than the fix itself.** | More code moves than the entry rule strictly needs. | It is what makes the "no contaminated rows in any view" rule testable. The dashboard has no tests today. |
| **Evening discovery still shapes the baseline.** Movers found at 19:00 now enter at the next open with no look-ahead, but they are a different kind of stock from the watchlist. | The baseline's make-up depends on the time of day. | Not a look-ahead issue. `universe_source` would allow the report to split it. |
| **Yahoo omits a real session.** | Entry one session late. | Conservative. The existing bad-price check still applies. |
| **On-demand screens just before an open.** | A clean row that nobody could act on. | Add a lead margin if it occurs; none exists in the data today. |
| **Classification of the 56 pending legacy rows is estimated here.** | Final counts may differ slightly from section 2. | The settle job classifies each from its real entry bar. |
