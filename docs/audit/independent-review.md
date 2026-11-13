# Independent review of the trading-assistant audit

Review date: 2026-11-13. Repository and Pi checkout both at `d0def12`.
Scope: the six documents in `docs/audit/`. This review changed nothing except adding this file.

## How this review was done

| Tag | Meaning |
|---|---|
| **R** | Read in the repository at `d0def12`; `file:line` given |
| **T** | Run locally in a scratch venv (Python 3.15): `ruff check news_bot.py newsbot tests` passed; 936 tests OK, 96% coverage, one `ResourceWarning: unclosed database` at shutdown. This matches the audit exactly. |
| **L** | Read-only on the Pi (`tickmake@raspberrypi5-02`): non-secret `.env` keys, `docker ps`, `ss -tln`, and `SELECT`-only queries on the database opened with `mode=ro` |
| **X** | Arithmetic recomputed from the stated inputs |
| **U** | Not verified; reason given |

Classifications: **confirmed**, **partially confirmed**, **incorrect**, **unverified**.

---

## 1. Executive verdict

**The audit is mostly accurate about what it looked at, and it missed the most important defect in the system it was auditing.**

- **Accurate.** Of the audit's critical and high findings, 9 are confirmed, 5 are partially confirmed and 1 proposed fix is wrong. Its live numbers (archive counts, `.env` values, open ports) reproduce exactly.
- **Missed: the forward test records entries that had already happened.** 408 of 1,094 archived signals (37%) were first written after the open they are scored from. For US names found by the evening mover discovery, that inflates the result by about 5 percentage points per trade (section 4.1). The audit calls the forward test's look-ahead status "OK". It is not, and the new 20-session series the audit wants to pre-register is already affected.
- **Too strong on the research.** The audit accepts "no edge" as established. The research shows that no edge was *detected*, in tests too weak to detect a small one, with one test (company scores) biased by leaving dividends out. That is a reason to keep the output labelled as research. It is not evidence about what to change.
- **Europe is not covered.** The audit's "Europe" column describes seven euro-zone suffixes. The UK, Switzerland and the Nordics outside Oslo have no currency handling, no cost model and no fundamentals route, and the roadmap gives them one line in P3.
- **The roadmap is the right size.** It adds no services and one optional dependency. Four of its tasks are mis-specified, and it has no task for six problems that matter more than several it does include.

**Recommendation: APPROVE WITH CONDITIONS** (section 10).

---

## 2. Findings independently confirmed

| # | Audit finding | Class | Evidence |
|---|---|---|---|
| 1 | Flat 10 bps per side in backtest, forward test and published picks | Confirmed | R: `config.py:231`; `forward_test.py:71`; `history_store.py:579`; `backtest.py:422`. L: every archive row has `cost_bps_per_side = 10`. |
| 2 | Real DNB cost on live sizes is 0.94–1.59% round trip | Confirmed | X: recomputed from `max(59, 0.12%)` (`config.py:174-175`) and the Pi's sizing: 12,500 NOK → 0.94%; 11,111 → 1.06%; 7,407 → 1.59%. |
| 3 | Gap note printed beside any stop multiple | Confirmed | R: `risk.py:400-404` prints `{risk_stop_atr_multiple:g}×ATR stop. {GAP_RISK_NOTE}`. `research/gap_risk.py:19` hard-codes 2.0. L: the Pi has `RISK_STOP_ATR_MULTIPLE=3.0`. |
| 4 | Stop is anchored to the signal close | Confirmed | R: `risk.py:72` `stop_price = metrics.last_close * (1 - stop_pct/100)`. X: 6.93% real distance vs 6.0% stated; 15.5% understatement. |
| 5 | A gap loss is never shown as a number | Confirmed | R: `PositionSizing` (`risk.py:42-49`) has no gap or fee field. X: 3.75% of the account in the audit's example. |
| 6 | Live risk settings: 3×ATR, 2% per trade, 25% cap, 100% total, 50,000 NOK | Confirmed | L: read from the Pi's `.env`. |
| 7 | Forward test switched from 5 to 20 sessions; `rule_id` has no horizon | Confirmed | L: 1,036 rows / 28 dates / 924 settled at 5 sessions; 58 rows / 3 dates / 0 settled at 20. R: `history_store.py:139-148`. |
| 8 | `MIN_PAIRED_DATES = 60` counts overlapping dates; no per-market minimum | Confirmed | R: `forward_test.py:29`, `:170-194`. The docstring already says the t value is "a rough guide". |
| 9 | Archive is 72% US | Confirmed | L: 786 of 1,094 rows have no exchange suffix. Settled and qualified: US 153, NO 36, EU 34. |
| 10 | Dashboard published on all interfaces with no login | Confirmed | R: `docker-compose.yml:243`. L: `0.0.0.0:8002` and `[::]:8002` are listening. |
| 11 | Production runs Python 3.15; CI tests 3.11 and 3.14 | Confirmed | R: `Dockerfile:3`, `streamlit/Dockerfile:3`, `ci.yml:15`. |
| 12 | US fundamentals have a guessed date; values are not point-in-time | Confirmed | R: `company_numbers.py:266` (`year_end + 120 days`); `score_inputs.py:165` (`INSERT OR REPLACE`). |
| 13 | No macro or sentiment data | Confirmed | R: as cited by the audit. |
| 14 | No calendar library; one fixed "finished" hour | Confirmed | R: `indicators.py:164` `SESSION_FINISHED_HOUR = 18`; nothing in `requirements.txt`. |
| 15 | Hold default is 5 in code and compose, 20 in `.env.example` | Confirmed, and wider than stated | R: `config.py:227`, `docker-compose.yml:166`, `.env.example:148`. Five more settings differ the same way (section 4.9). |

---

## 3. Findings challenged or rejected

### 3.1 Forward-test look-ahead is "OK" — **incorrect**

`quant-validation.md` lists "Forward test: next-open entry; first write wins. **OK**". See section 4.1. This is the most serious error in the audit.

### 3.2 P1-8, "ESEF date can be back-dated" — **partially confirmed; the proposed fix is wrong**

- **What the code does.** R: `company_numbers.py:234-236` takes `min(added, year_end + 120 days)`.
- **Why.** R: the comment at `:70-72` explains that `added` is the day filings.xbrl.org indexed the report, "often months later" than publication. EU issuers must publish within four months of year end.
- **The real risk** is narrow: a company that published later than the legal deadline gets a date that is too early.
- **The audit's fix** ("use the filing-added date when known") would hold back every correctly published report by months. That replaces a small look-ahead on late filers with a large staleness on everyone, and the audit gives no count of how many filings are affected either way.
- **What is needed first:** a count of filings where `added` is more than 120 days after year end, and a sample checked against the company's actual publication date. Only then choose a rule.

### 3.3 "The 3×ATR gap-through rate and overshoot have never been measured" — **partially confirmed**

Research §8 ran a 3×ATR stop on 44,807 entries: hit in 29% of 20-session holds, mean +0.31%, worst 5% at -11.9%. What is missing is the gap-through share and overshoot *specifically at 3×*. The P0-1 fix is still right, but the stop-hit rate is already known.

Two further mismatches the audit did not see, both R:
- `research/gap_risk.py:56` measures the stop from the **entry open**; the live stop is from the **signal close**. The measured numbers describe a different stop from the one printed, at any multiple.
- `research/gap_risk.py:21` omits the volume gate (1.2), so "passed the shipped gates" in `risk.py:27-29` is not exact.

### 3.4 "Research §8 assumed 0.5% risk" (D2) — **incorrect**

§8 (lines 264-268) says the opposite: at 0.5% risk a position is about 4,000 NOK and the minimum fee is nearly 3% round trip, and it leaves the setting to the owner. So D2's option "return to 0.5–1% risk" contradicts the audit's own cost finding. X: at 0.5% risk and a 6% stop, the position is 4,167 NOK and the round trip costs 2.8%.

### 3.5 "Survivorship would only make the negative result worse" — **not justified as a general statement**

- For a *level* (does a rule make money), survivor-only data flatters the result. Correct.
- For a *relative* test (picks vs the same stocks, top third vs all), both sides lose the same failed companies. The direction of the bias depends on which side would have held them. The research argues this case by case (§10 for quality, §8 for stops); the audit generalises it.

### 3.6 "Pass rules were fixed before the run" — **unverified**

R: `research/score_backtest.py` and its results arrived in the same squashed commit (`d0def12`), and so did §9 (`e537a3b`). The rules may well have been written first, but the history cannot show it. Counted as asserted, not evidenced.

### 3.7 Universe counts — **incorrect**

The audit says `stock_list.csv` is "41 NO, 46 EU, 8 US" (which sums to 95, not its stated 97). R: the file has **43 Oslo, 44 EU, 10 US**. EU by suffix: `.PA` 15, `.MI` 10, `.AS` 8, `.DE` 7, `.MC` 3, `.BR` 1. `evaluation_universe.csv` is 44 rows with 11 Indian, as stated.

### 3.8 P2-2, "remove the misspelled variables" — **incorrect**

R: `FREEN_EWS_API_KEY` and `FINHUB_API_KEY` are deliberate backward-compatible aliases (`config.py:81-85`) and are read (`news_feeds.py:294`, `market_data.py:40`). Removing them from compose silently drops a key for anyone whose `.env` uses the old name. Check the Pi's `.env` key names first.

### 3.9 P1-5 and the Ollama rows — **partially confirmed; aimed at the wrong thing**

- R: the compose `ollama` service publishes no port and sits behind an optional profile (`docker-compose.yml:6-19`).
- L: the Pi's `ollama` container publishes nothing (`11434/tcp`, no host binding). The listener on `*:11434` is a second, host-installed `/usr/local/bin/ollama serve`.
- So `architecture-and-data-flow.md` attributes the open port to the container, and P1-5 edits a compose service that is neither of the two processes running. Which user the host process runs as: **U** (not checked).

### 3.10 "FX is text only" — **partially confirmed**

Only the FX *fee* is unquantified (`risk.py:337`). Holdings are already valued in NOK: each fill stores `fx_to_nok`, and the portfolio report, scorecard, `/check` and dashboard convert at the latest rate (`fills.py:143-151`, `:249-265`). The P3 "NOK portfolio view" partly exists; what is missing is a currency *exposure* line.

### 3.11 Dashboard severity — **confirmed as a defect, overstated as a current leak**

L: `real_trades` and `deposits` are both empty. Nothing private is exposed today. It becomes a real leak with the first `/fill`, which research §8 dates to January 2027. Router and IPv6 reachability remain **U**, as the audit says.

### 3.12 Smaller corrections

- README does document `/check` (`README.md:478`). The other six commands are missing, as stated.
- "60 non-overlapping 20-session dates take about 4.6 years": X gives 4.8.
- `GAP_RISK_NOTE` is quoted loosely but the meaning is right.

---

## 4. Missing findings

Ordered by how much they change the conclusions.

### 4.1 Forward test: the entry open is already past when the signal is recorded (critical)

**Mechanism (R).**
- The screen drops any bar dated today (`indicators.py:139-159`, used at `:195`), so a 19:00 Oslo run produces signals dated yesterday.
- `forward_outcome` enters at the first session after the signal date (`forward_test.py:50-52`). For a signal first recorded at 19:00, that is today's open, hours earlier.
- Published picks are protected by `date > publication_date` (`history_store.py:547-553`). The forward test has no such guard.
- First-write-wins hides this only for symbols the 07:00 run already recorded.

**Size (L).** Rows first recorded on a weekday after that market's opening time (15:30 Oslo time for the US, 09:00 for Norway and Europe), carrying a signal dated earlier. This assumes the recording day was a trading session; for the settled rows the stored entry date confirms it.

| Market | Rows first recorded after the entry open | Of all rows |
|---|---|---|
| US | 210 | 786 |
| Norway | 110 | 145 |
| Europe | 88 | 163 |
| **Total** | **408** | **1,094 (37%)** |

Of the 245 qualified rows, 84 are affected.

**Effect (L), settled 5-session US rows, net return:**

| Group | Rows | Signal dates | Mean | Median |
|---|---|---|---|---|
| Configured, entry in the future | 145 | 25 | -1.46% | -0.81% |
| Discovered, entry in the future | 326 | 25 | -1.07% | -0.52% |
| Discovered, **entry already past** | 181 | 25 | **+3.72%** | +1.98% |

The evening discovery list is Yahoo's intraday movers. A stock is picked *because* it is up today, then scored from this morning's open. That is selection on the outcome. These rows are mostly unqualified, so they inflate the **baseline** that qualified picks are compared against.

**For Norway and Europe** the affected rows are the configured names, so there is no selection effect, but the test still assumes a fill about ten hours before the message was sent. Why the 07:00 run almost never records Oslo and EU rows first is **U** (the staleness rule in `screener.py:232-250` is the likely cause; not traced).

**Consequences.**
- The 5-session results cannot be used in either direction.
- 17 of the 58 rows in the new 20-session series are already affected.
- D4 cannot be frozen until this is fixed and the series restarted.
- Caveat on the size: 25 overlapping signal dates in one six-week period. The direction is not in doubt; the 5-point figure is approximate.

### 4.2 Saved prices mix adjustment bases (high)

R: `price_store.py:82` fetches raw prices and stores Yahoo's `Adj Close` beside them. Rows are written with `INSERT OR IGNORE` and "never changed" (`:8-10`, `:107`). `closes()` returns `COALESCE(adj_close, close)` (`:115`).

An adjusted close is relative to the day it was fetched. After every dividend, rows saved later are on a different basis from rows saved earlier, so the series has a false step at each fetch boundary. `score_inputs` reads this series for momentum. L: the store has 3 distinct fetch dates so far, so the damage is small today and grows with every dividend.

Splits have the same problem on raw closes. The only check fires above a 40% one-day move (`data_checks.py:18`), which misses a 3-for-2 split.

### 4.3 The company-score backtest leaves out dividends (high)

R: `research/score_backtest.py:49` reads raw `open` and `close`. Returns exclude dividends, which penalises high-yield stocks. That is most of what a value score selects, and much of Oslo. §10's "value is a near miss" and "quality is backwards" both lean on this.

### 4.4 Rows that can never settle bias the forward test upward (medium)

R: history is fetched with `period="2y"` (`forward_test.py:113`); a delisted or renamed symbol raises and stays pending for ever (`:41-42`, `:49-50`). The failures are the companies that did worst. L: 112 five-session rows are still pending, the oldest from 2026-10-05, which is well past its settlement date.

### 4.5 Benchmarks are not comparable across markets (medium)

R: `config.py:235-237`. `^GDAXI` and `OSEBX.OL` are total-return indices; `SPY` (adjusted) includes dividends; `^FCHI`, `^AEX`, `^IBEX`, `FTSEMIB.MI` are price-only. A German pick is measured against a higher bar than a French one. The audit lists this as "unverified"; it is a fact of the index definitions and should be a finding. Separately, a benchmark missing either the entry or exit date returns `None` silently (`benchmarks.py:48-50`). L: 2 of 924 settled rows.

### 4.6 Deploy has no rollback and loops on failure (high, operations)

R: `scripts/deploy.sh:145` only prints the rollback command. The success marker is not written on failure, so the 5-minute timer rebuilds the broken commit again, tags the broken running image as a new "rollback" image, and writes another copy of `.env`. L: 16 `.env.bak-*` files plus `.env-bkp` sit in the Pi checkout, each a copy of the secrets. Nothing in the repository documents any protection on `main`, and whatever is pushed there runs on the Pi within five minutes with access to `.env`.

### 4.7 Nobody is told when something fails (high, operations)

R: no `EVENT_JOB_ERROR` listener in `bot.py`; a failed backup only logs (`backup.py:112`); an unhealthy container is only marked. The only signal of a dead bot is the 12:00 ping *not* arriving.

### 4.8 Backups are never test-restored (medium)

R: backups use the online-backup API and pass `integrity_check` (good), and are pulled off the box. L: 14 copies present, newest from this morning. There is no restore procedure and no restore test. The archive is the one thing in this system that cannot be regenerated.

### 4.9 `.env.example` contradicts the code defaults on six settings (medium)

R: risk per trade 0.5 vs 2.0; stop multiple 2.0 vs 3.0; position cap 20 vs 25; total 60 vs 100; earnings window 7 vs 28; hold 5 vs 20. P1-2 fixes one of the six.

### 4.10 Smaller items

- **Telegram authorisation is by chat, not sender** (`commands.py:104-111`, `:226`). Harmless in a private chat; anyone in a group chat could `/fill`.
- **Overselling inflates cash.** `fills.py:258-265` removes only the shares held but credits the full sale.
- **Supply chain.** No hash-pinned dependencies, actions pinned by tag, `ollama/ollama:latest`. With auto-deploy from `main`, this is the main route in.
- **The dashboard image is not built, tested or audited in CI.**
- **SQLite is in rollback-journal mode** (L: `journal_mode = delete`), so dashboard reads briefly block the bot's writes. Mitigated by a 60-second cache and a 5-second timeout. Acceptable; worth knowing.
- **Total exposure is a warning, not a limit.** `RISK_MAX_TOTAL_PCT` never scales a size (`risk.py:249-255`).

---

## 5. Quantitative research concerns

| Topic | Assessment |
|---|---|
| **Look-ahead** | Backtest: none found; entries are next-open and indicators are causal (`backtest.py:434-449`). Forward test: **present** (4.1). Fundamentals: narrow risk on late filers (3.2); Yahoo values are restated, not as first reported. |
| **Survivorship** | Present everywhere and honestly stated by the research. Direction is clear for levels, unclear for relative tests (3.5). A paid dataset (D7) is not justified while nothing is close to passing. |
| **Data leakage** | Research: a single 70/30 date split; trades crossing the boundary are dropped (`backtest.py:454`). No leak found. The 5-session forward results have now been looked at, so that series is no longer a clean test. |
| **Corporate actions** | Live and forward test use freshly fetched adjusted prices; entry and exit come from one fetch, so stored outcomes are internally consistent and include dividends with no withholding tax. The saved price store is not consistent (4.2). |
| **Transaction costs** | Confirmed understated by 0.7–1.4 points per round trip for performance numbers. `/check` and research §8–10 use the real fee. |
| **Slippage and FX** | Neither is modelled anywhere. DNB's FX margin is unknown (D1). No spread data exists for Oslo small caps; the audit's 5–20 bps scenarios are guesses and should be labelled as such. |
| **Benchmarks** | Mixed total-return and price-return indices (4.5). The forward test's baseline is "every screened symbol", which is contaminated (4.1). |
| **Overlapping trades** | Research takes every h-th observation for t values (`factor_study.py:65`), which is sound. §8 overlaps and reports no t values, yet says it "settles" the hold length. The forward test does not correct for overlap (confirmed). |
| **Statistical significance** | Nothing reaches t = 2 on an independent sample. The largest holdout figures are t 1.6–1.8. |
| **Multiple testing** | §6 tests 3,456 correlated combinations with a sign-of-the-mean rule and no power analysis. "Zero pass" is informative; "there is nothing to tune toward" is a stronger claim than the test supports. |
| **Out-of-sample** | One split, looked at once per study as far as the history shows. No walk-forward. Acceptable for a negative result. |
| **Forward-test maturity** | 0 settled results at 20 sessions; about 6 independent windows at 5 sessions, and those are contaminated. Immature and currently invalid. |

### "No edge" versus "not detected"

- §10 has 42–66 months and about 30 stocks per third. With t between -1.3 and +1.6, the data cannot tell a zero edge from a real one of a few tenths of a point per month. It also omits dividends (4.3).
- §6 is the strongest negative result. It is about these five gates on 44 large caps, at 10 bps.
- The conclusion that survives all of this: **nothing has been shown to work, so nothing should be labelled as a recommendation.** The audit's stronger wording ("the audit finds no reason to doubt that conclusion") should not be used to justify removing or changing a gate, score or setting.

### D4 as proposed will almost certainly end without an answer

- L: the paired difference across 25 five-session dates has a standard deviation of about 2.3 points. Scaled to 20 sessions that is roughly 4.5 points (assumption: variance grows with the hold).
- X: with 24 independent dates, a 90% interval excludes zero only if the mean excess is above about **1.6 points per 20 sessions**. The largest effect anywhere in the research is 0.3.
- So after two years the likeliest outcome is "inconclusive", in both directions. The owner should decide knowing that.

D4 also leaves these undefined:
- which of its two benchmarks the pass rule uses;
- how "maximum drawdown" is computed for overlapping picks that are not a portfolio;
- what happens on dates with no qualified pick in a market (Norway has five names; most dates will have none);
- the "base" cost model, which depends on D1 and does not exist yet.

### On the live risk settings (D2)

No evidence supports changing them, and none supports keeping them. The trade-off is arithmetic, not research: lower risk per trade means smaller positions and a higher fee share; the 25% cap already sets the size for any stock with ATR below 2.7%. This review recommends no change to trading parameters.

---

## 6. US, Norway and Europe coverage assessment

"Europe" in the audit means seven euro-zone suffixes. Per exchange, from the code (R):

| Exchange (Yahoo suffix) | In a universe | Currency mapped | Benchmark mapped | Fundamentals | Status |
|---|---|---|---|---|---|
| NYSE, Nasdaq (none) | Live 6; list 10; basket 11 | USD by default | SPY | Yahoo only, guessed dates | **Partial** |
| Oslo (`.OL`) | Live 5; list 43; basket 8 | NOK | OSEBX.OL (total return) | ESEF + Yahoo | **Partial** (best covered) |
| Xetra (`.DE`) | Live 2; list 7 | EUR | ^GDAXI (total return) | ESEF often missing; Yahoo | **Partial** |
| Euronext Paris (`.PA`) | Live 1; list 15 | EUR | ^FCHI (price) | ESEF + Yahoo | **Partial** |
| Euronext Amsterdam (`.AS`) | Live 1; list 8 | EUR | ^AEX (price) | ESEF + Yahoo | **Partial** |
| Borsa Italiana (`.MI`) | List 10 | EUR | FTSEMIB.MI | ESEF + Yahoo | **Research list only** |
| Madrid (`.MC`) | List 3 | EUR | ^IBEX | ESEF + Yahoo | **Research list only** |
| Euronext Brussels (`.BR`) | List 1 | EUR | ^BFX | ESEF + Yahoo | **Research list only** |
| Euronext Lisbon, Dublin (`.LS`, `.IR`) | None | EUR | None | Untested | **Unsupported** |
| Vienna (`.VI`) | None | EUR | None | Untested | **Unsupported** |
| Stockholm (`.ST`) | None | SEK | ^OMX | Untested | **Unsupported** (mappings only) |
| Copenhagen (`.CO`) | None | DKK | ^OMXC25 | Untested | **Unsupported** (mappings only) |
| Helsinki (`.HE`) | None | EUR | ^OMXH25 | Untested | **Unsupported** (mappings only) |
| London (`.L`) | None | **None** | ^FTSE | Not ESEF after Brexit; Yahoo only | **Unsupported** |
| SIX Swiss (`.SW`) | None | **None** | ^SSMI | Not ESEF; Yahoo only | **Unsupported** |
| Warsaw (`.WA`) | None | None | None | Untested | **Unsupported** |

Currency map: `fills.py:31-35`. Benchmark map: `config.py:235-237`. "Live" is the Pi's `.env`.

Against the required topics:

| Topic | US | Norway | Europe | In the roadmap? |
|---|---|---|---|---|
| Exchange-level coverage | Partial | Partial | Four exchanges live, three on the list, nine unsupported | P3, one line, no criteria |
| Calendars and timezones | Timezone from Yahoo; one fixed 18:00 "finished" hour; no holidays or half days | Same | Same | P3; names only XNYS, XOSL, XETR |
| Currency conversion | Holdings valued in NOK | Not needed | EUR, SEK, DKK mapped; GBP, CHF missing; **London quotes in pence, unhandled** | No |
| FX fee and exposure | Not quantified | Not applicable | Not quantified | P1-1 (fee); P3 (exposure) |
| Market-specific costs | DNB foreign-market tariff not modelled | DNB 0.12%, 59 NOK minimum | Not modelled; **no UK stamp duty (0.5%), no French or Italian transaction tax** | P1-1 has one `pct` per market; no tax field |
| Corporate actions | Adjusted at fetch; store inconsistent (4.2) | Same | Same | No |
| Fundamentals | Yahoo; no filing date; no EDGAR | ESEF from 2020 accounts | ESEF where the issuer files; German coverage thin (`company_numbers.py:281`); none for UK or Switzerland | P1-8 only |
| Liquidity and execution | No spread, depth or minimum-turnover rule | Same; Oslo small caps are the real risk | Same | No |
| Cross-market portfolio risk | 60-session correlation and sector warnings; nothing by currency or country | Same | Same | P3, one line |
| Provider licensing and reliability | Yahoo is unofficial and its terms restrict use; yfinance is pinned and version-sensitive | Same | Same; filings.xbrl.org rate-limits | **No. Not mentioned in any of the six documents.** |

Two more points:
- The mover discovery uses Yahoo's US screeners (`.env.example:121`). That is why the archive is 72% US while the watchlist is mostly European. No task addresses it.
- French transaction tax applies today to at least one live name (LVMH, `MC.PA`), so the missing tax field is a present gap, not a future one.

**Conclusion.** The system is a US-plus-Oslo-plus-four-euro-exchanges research tool. The audit's "partially supported" label for Europe is fair only for those four. The roadmap does not make the UK, Switzerland or the Nordics supportable, and should say they stay disabled until each has a currency entry, a cost entry with local taxes, a calendar and a fundamentals source.

---

## 7. Architecture and security assessment

**The architecture is appropriate and should not grow.** One scheduler process, one read-only dashboard, one SQLite file, one Pi. The audit correctly proposes no new services.

What is already good (R, L):
- Bot: uid 10001, read-only root, three capabilities, `no-new-privileges`. Dashboard: `nobody`, read-only, all capabilities dropped, database opened `mode=ro` on a `:ro` mount.
- Backups use the online-backup API, are integrity-checked, pruned only after a good copy, and pulled off the box.
- Secrets are git-ignored and redacted in logs (`logsetup.py:63-68`). No shell calls; SQL is parameterised.
- RSS is parsed with `defusedxml`. The model's output can only reorder or hide headlines; it cannot write text, prices or calls.

| Roadmap item | Judgement |
|---|---|
| P1-4 bind the dashboard to localhost | Proportionate. Do it before the first `/fill`. |
| P1-9 test on 3.15 | Proportionate. Running the tests inside the built image is the better of the two options: it tests what ships. |
| P1-5 Ollama limits | Mis-aimed (3.9). The real item is the host-installed Ollama on `*:11434`, outside this repository. |
| P1-1 `costs.py` | Proportionate if it stays a table of numbers per exchange with a version. Three scenarios times every market is more than a 50,000 NOK account needs: one honest base case plus one adverse case is enough. |
| P1-7 `universe_source` column | Proportionate, and needed to analyse 4.1. |
| P3 `exchange_calendars` | Defer. A new dependency for three calendars; a "session finished" rule per exchange and a recorded entry timestamp solve the actual defect. |
| D7 paid delisted-price data | Not justified now. |

Missing from the architecture view, all cheap:
- alert on a failed job, failed backup or unhealthy container (one Telegram message);
- stop the deploy loop on failure, and stop copying `.env` on every run;
- a restore test (copy the newest backup, open it, count archive rows);
- protect `main`, since a push is a deploy;
- authorise commands by sender id as well as chat id.

**Reproducibility.** Research scripts hard-code paths under `/Users/sunil/` and read a pickle or a copied database. Results cannot be regenerated from the repository alone. Acceptable for a personal project, but each result should record the commit and the data date.

---

## 8. Contradictions across existing documents

1. **Look-ahead.** `quant-validation.md` marks the forward test "OK"; `independent-audit.md` §4b and §5 describe the same archive as unreliable for other reasons. Neither sees 4.1.
2. **Risk per trade.** `independent-audit.md` §3 and D2 say research assumed 0.5%; the same audit's cost table shows why 0.5% is unworkable (3.4).
3. **Ollama.** `architecture-and-data-flow.md` says the shared container is on host `*:11434`; `independent-audit.md` says Ollama "listens on the host, run as root"; P1-5 edits the compose service. Three descriptions, none matching what runs (3.9).
4. **Universe counts.** 41/46/8 in `independent-audit.md`; "46 names" for Europe in `decision-log.md`; the file has 43/44/10.
5. **Priority labels.** Hypothesis 1 is "high for the product" with no task. Hypothesis 6 is P1 "for score research" with "low live impact", yet becomes P1-8. Python 3.15 is "P2 … P1-adjacent" in the audit and P1-9 in the roadmap.
6. **Calendars.** Listed as "unverified" in the audit, "absent" in the architecture document and P3 in the roadmap, with no finding of actual harm.
7. **"Each task can be done on its own"** (roadmap, first line) against the stated dependencies of P0-2, P1-1, P1-4 and P1-6.
8. **P1-2's own test.** New rows get an `-h20` suffix, old rows "are still reported". `forward_test.py:243` filters on exact `rule_id`, so the 58 existing 20-session rows would drop out of the current report. The horizon is already a stored column and already filtered; the suffix is unnecessary.
9. **Proposed CLAUDE.md** says costs "are per market and versioned" as a present fact. They are not; that is P1-1.

**Findings with no task:** hypothesis 1 (no BUY framing; partly D4); hypothesis 7; Yahoo restated values (`INSERT OR REPLACE`); survivorship (D7 only); delisted names and ticker changes; split and dividend validation; benchmark reconciliation; Portainer and poster-bot exposure; per-market forward-test minimums (inside P1-6 without a test).

**Tasks with no finding:** P2-3 (dashboard healthcheck and data-age banners); P2-7 (lint more directories).

**Tasks missing acceptance criteria or complexity:** P1-5, P1-8, P1-9 and every P2 item.

**Decisions not made:** all seven. D1, D3 and D4 block tasks. D2 has no task at all. D5 blocks half of P1-7. No document says who decides or by when.

---

## 9. Changes required to the remediation roadmap

### Add

| ID | Task | Done when |
|---|---|---|
| **N1 (P0)** | Forward test must not enter before the signal was recorded. Apply the published-picks rule (`history_store.py:547-553`) using `recorded_at`. Existing rows are not rewritten: add a flag column and report clean rows separately. | A row recorded at 19:00 Oslo enters at the next session's open. Test: a US row recorded after 15:30 Oslo on day D enters on D+1. The report shows clean and flagged counts. |
| **N2 (P1)** | Price store: stop mixing adjustment bases. Keep raw bars as saved; store dividends and splits as their own rows and compute adjusted series on read, or refetch the adjusted column in full. Lower the split check below 40%. | Momentum from the store matches a fresh full fetch for a stock that paid a dividend between two saves. |
| **N3 (P1)** | Re-run §10 with dividends included. Keep the old table beside the new one. | Both tables are in the research document, labelled. |
| **N4 (P1)** | Alerts: Telegram message on any job exception, failed backup, and unhealthy container. | A job that raises produces one message. |
| **N5 (P1)** | Deploy: on a failed health check, restore the tagged image and stop retrying that commit. Stop writing `.env` copies; remove the existing ones on the Pi after the owner confirms. | A deliberately broken commit leaves the old container running and sends one alert. |
| **N6 (P1)** | Restore test, monthly: open the newest off-box backup and compare archive row counts. Write the restore steps in the README. | The test runs and reports. |
| **N7 (P2)** | Unsettleable rows: settle delisted names at the last traded price with a flag, and report how many rows are stuck. | No row stays pending past twice its horizon without a reason shown. |
| **N8 (P2)** | Align `.env.example`, compose and `config.py` on all six settings (replaces the hold-only half of P1-2). | One test compares the three sources. |
| **N9 (P2)** | Record benchmark type (total or price return) per suffix and show it in reports. | Each report line names the benchmark type. |
| **N10 (P2)** | Command authorisation by sender id; fix the oversell cash credit in `fills.py`. | Tests for both. |
| **N11 (P2)** | Build and audit the dashboard image in CI. | CI fails on a broken `streamlit/` build. |
| **N12 (P2)** | One page on data providers: what each is used for, its terms, and what breaks if it goes away. | The page exists and is linked from the README. |

### Change

| ID | Change |
|---|---|
| P0-1 | Also measure the stop from the same anchor the live stop uses, and include the volume gate (3.3). |
| P0-2 | Depends on a per-multiple gap table that P0-1 has to create first. State that. |
| P1-1 | Key the model by **exchange**, not region. Add a transaction-tax field (UK, France, Italy). Two scenarios, not three. Label spread and FX numbers as estimates until D1 is answered. |
| P1-2 | Drop the `-h{N}` suffix (section 8, item 8). Keep the default change, folded into N8. |
| P1-5 | Replace with: decide what to do about the host-installed Ollama on `*:11434` (owner, outside this repository). Pin the compose image tag. |
| P1-6 | Depends on N1. Add the per-market test the roadmap omits. Restart the 20-session series after N1; do not count the 58 existing rows. |
| P1-8 | Replace the fix with a measurement task (3.2). Change the rule only after the count is known. |
| P2-2 | Keep the two alias variables. Delete only `.coveragerc.bak`. |
| P3 calendars | Defer. Reconsider only if N1 and a per-exchange close time do not resolve stale-bar cases. |
| P3 EU expansion | Add entry criteria per exchange: currency, cost with taxes, calendar, fundamentals source, benchmark type, at least one test. Until then the exchange is listed as unsupported. |

### Decisions

| ID | Change |
|---|---|
| D2 | Remove "research assumed 0.5%". Present it as a fee-versus-risk trade-off with the table from `quant-validation.md`. No recommendation; no parameter change in this roadmap. |
| D4 | Do not freeze yet. First: N1 done, D1 answered, one benchmark named, drawdown defined, the no-pick-date rule written. State the detectable effect (about 1.6 points per 20 sessions) so the owner chooses the two-year wait knowingly. |
| D7 | Close as "not now". |
| D8 (new) | Is Europe beyond the four live exchanges a requirement for 2027? If yes, P3 EU expansion moves up and P1-1 grows. If no, say so in the proposed CLAUDE.md. |

### Revised order

N1 → P0-1 → P0-2 → P1-4 → N4 → N5 → P1-9 (with N11) → N8 → P1-3 → N2 → N3 → P1-1 (after D1) → P1-7 → P1-6 (after N1 and D4) → N6 → P1-8 (measurement) → P2 and N7, N9–N12 → P3.

### Proposed CLAUDE.md

Sound overall. Before promoting it (D6): change "costs are per market and versioned" to a rule about the future; add "the forward test never enters before `recorded_at`"; add the list of supported exchanges from section 6.

---

## 10. Final go/no-go recommendation

**APPROVE WITH CONDITIONS.**

The roadmap is right in scale and mostly right in content. It may proceed once these conditions are met:

1. **N1 is added as the first task**, and no forward-test number is quoted or pre-registered until it ships and the 20-session series restarts.
2. **Four tasks are corrected before work starts on them:** P1-2 (no suffix), P1-5 (wrong target), P1-8 (measure before changing), P2-2 (keep the aliases).
3. **N4, N5 and P1-4 are done before the first real trade is logged**, currently planned for January 2027.
4. **The owner answers D1 and D3**, and D4 is rewritten as in section 9 before it is frozen.
5. **No strategy, gate, score or risk-parameter change is made on the strength of this audit or this review.** The evidence supports keeping the output labelled as research and nothing more.
6. **Europe is described honestly:** four exchanges partially supported, three on the research list, nine unsupported, until the P3 entry criteria are met.

Not approved as written: the P3 calendar dependency, D7 (paid data), and any reading of "no edge" as proof.

### What this review did not verify

- Router and IPv6 firewall rules, and who can reach the Pi's Wi-Fi network.
- DNB's actual FX margin and foreign-market tariffs.
- Real spreads on Oslo small caps.
- The research results themselves: none of the ten studies was re-run.
- Why the 07:00 run rarely records Oslo and EU rows first (4.1).
- How many ESEF filings are affected by the `min()` rule (3.2).
- Branch protection on GitHub (only the repository's own documents were checked).
- Which user the host-installed Ollama runs as.
