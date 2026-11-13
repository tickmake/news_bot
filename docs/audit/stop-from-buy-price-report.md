# P1-3: the stop from the buy price (#172)

Report date: 2026-11-13. Branch `fix/stop-from-actual-fill`, from `main` at `b06b6c1` (P1-2 merged).
**Not merged and not deployed.** Nothing on the Pi was read or changed.

This changes where the stop is measured from when a buy price is known, and how the stop is labelled when it is not. It does not change:

- the ATR multiple or any risk setting;
- the sizing rule or the account limits;
- entry, exit, gates or ranking;
- costs or the forward test.

## 1. Root cause

`risk._stop_pct` returned `RISK_STOP_ATR_MULTIPLE × ATR%`, and every caller applied it to `metrics.last_close`, the signal close:

- `size_position` (the stop price and the suggested weight);
- `trade_risk` (the stop, the distances, the losses at the stop and in a gap, used by `/recommendations`, `/analyze`, `/check`, published picks and the dashboard);
- `stop_line` and `loss_lines`, which only render that.

`/check` took two arguments, `TICKER AMOUNT_NOK`, and also priced the shares at the signal close.

So no path could use a buy price. Entry is at the next open (or later), so the real stop distance differed from the one shown. A buy 1% above the close turned a 6.0% stop into 6.9%. Per `risk-reporting-report.md` §8, the study found buys above the close in 57% of entries.

**Fill information already in the bot.** `/fill` saves past trades in `real_trades`, and `/check` reads them only for overlap and the account value. They describe earlier buys, not the order being checked, so they are not used as its buy price.

## 2. The calculation

ATR is a price distance from the signal's bars: `ATR = signal close × ATR% / 100`. One formula, for both references:

    reference      = buy price if given, else the signal close
    stop distance  = N × ATR                         (price; N = RISK_STOP_ATR_MULTIPLE)
    stop price     = reference − N × ATR
    stop distance% = N × ATR / reference × 100
    loss at stop   = position × stop distance%  (= shares × N × ATR)
    risk-rule size = RISK_PER_TRADE_PCT / stop distance%, capped at RISK_MAX_POSITION_PCT

In code: `_stop_pct(metrics, entry_price)` returns the percent, and `size_position` and `trade_risk` apply `reference × (1 − pct/100)`. With no buy price this is the same expression as before, and its output is byte-identical (section 5).

**Example.** Close 100, ATR 2% (2.00), 3×ATR:

| | Reference | Stop | Distance | Loss on 10,100 NOK |
|---|---|---|---|---|
| No buy price | 100.00 signal close | 94.00 (estimate) | 6.00 = 6.0% | 606 NOK (101 shares) |
| Buy price 101 | 101.00 | **95.00** | 6.00 = 5.94% | 600 NOK (100 shares × 6.00) |
| Buy price 101, 2×ATR | 101.00 | 97.00 | 4.00 = 3.96% | 400 NOK |

## 3. Behaviour

**`/check TICKER AMOUNT_NOK`** (unchanged interface) and `/recommendations`, `/analyze`:

    risk: estimated volatility stop 94.00 NOK, 3×ATR below the 100.00 signal close: 6.00 (6.0%) away · ATR 2.00 (2.0%). Once bought it is 3×ATR = 6.00 below your buy price
    loss if bought at the 100.00 close: … at the stop, before costs; your actual risk isn't known until you buy

The `/check` footer adds "Add your buy price to work them out from it: /check NHY.OL 10100 PRICE." The `/recommendations` footer adds "Once bought, the stop is the same distance below the buy price; /check TICKER AMOUNT PRICE works it out."

**`/check TICKER AMOUNT_NOK BUY_PRICE [!]`** (new optional argument):

    100 shares at your buy price of 101.00 NOK = 10,100 NOK (close of 2026-11-10: 100.00 NOK)
    risk: volatility stop 95.00 NOK, 3×ATR below your 101.00 buy price: 6.00 (5.9%) away · ATR 2.00 (2.0%)
    loss from your 101.00 buy price: 1.20% of your money ≈ 600 NOK at the stop, before costs
    if it gaps through: it sells below the stop; how far is not measured for a stop below a buy price

- Shares, cost, fee, the "how far it can fall" money figures and the suggested size all use the buy price.
- The buy price is in the stock's own currency, as in `/fill`, and a decimal comma is accepted.
- It must be a finite number above 0. A price more than 15% from the close (`fills.MAX_PRICE_GAP_PCT`, the `/fill` check) is refused as a likely typo unless the message ends with `!`.
- A buy price so low that the stop would be at or below 0 gives no stop.
- Nothing places an order. The text says a gapped stop sells below its price.

**Gap estimate.** `GAP_STUDIES` holds only stops below the signal close, and the code keys a measurement by its anchor. A stop below a buy price therefore gets "not measured" and no borrowed figure, the same rule as for an unmeasured multiple.

## 4. Files changed

| File | Change |
|---|---|
| `newsbot/risk.py` | `BUY_PRICE_ANCHOR`. `_stop_pct`, `size_position`, `trade_risk` take an optional `entry_price`. `TradeRisk.entry_price`. `gap_study`/`gap_note` take the anchor. `stop_line`/`loss_lines`: "estimated … signal close" vs "below your … buy price". The portfolio footer gets one sentence. `snapshot` labels the anchor from `entry_price`. |
| `newsbot/trade_check.py` | Optional `BUY_PRICE` and `!`. Validation, the typo check, shares/cost/risk/size from the buy price, and both footers. |
| `newsbot/commands.py` | `/help` line for `/check`. |
| `README.md` | `/check` usage. The stop is an estimate until bought. |
| `docs/audit/remediation-roadmap.md`, `risk-reporting-report.md` | Status, and a pointer from limitation 1. |
| `tests/test_stop_from_buy_price.py` | New: 12 tests (risk arithmetic, wording, records). |
| `tests/test_trade_check.py` | 6 new `/check` tests. No existing assertion edited. |

**Not changed:** `config.py`, `.env.example`, `docker-compose.yml` (`RISK_STOP_ATR_MULTIPLE` is still 2.0 in code and compose and 3.0 in `.env.example`); `/fill`, `/unfill`, `/deposit`; any schema.

## 5. Historical compatibility

- **Byte-identical output without a buy price.** A script built the published-pick risk record, `size_position` and the `/check`-style `trade_risk` record for 24 cases. The cases cover multiples 2.0, 2.5 and 3.0, with and without an account size, and four stocks including a thin one and one without volume. The JSON from `main` (`b06b6c1`) and from this branch are identical: 38,940 bytes, `cmp` clean.
- **Record layout.** A new pick still saves the same 28 keys in the same order, with `stop_anchor: "signal_close"` and `entry_price: null`. `RISK_MODEL_VERSION` stays 2, and the dashboard reads it as before. A test pins the layout.
- **Nothing is rewritten.** `/check` with a buy price writes nothing. A test compares every table before and after `/check` and a re-publish of a sent pick: no row changes. Saved records are never recomputed.
- **No schema change.**

## 6. Tests and verification

| Check | Result |
|---|---|
| Existing risk, `/check` and command tests, unedited (`test_trade_check`, `test_risk`, `test_risk_reporting`, `test_commands`) | 162 passed before the new tests were added |
| New tests | 18 passed (12 + 6) |
| Full suite, as CI (`python -W error::ResourceWarning -m coverage run -m unittest`), Python 3.15.2 | 1,191 run, OK, 2 skipped; coverage 97% (`risk.py` + `trade_check.py` 98%) |
| `ruff check news_bot.py newsbot tests` | All checks passed |
| `docker build .` and `docker build ./streamlit` | Both built |
| Independent challenge in the built image | Close 285.00, ATR 1.7%, buy 288.40, 3×ATR. By hand: stop 273.865, distance 14.535 (5.04%), 41 shares, loss 595.94 NOK (1.19%). `/check` printed stop 273.86, 14.54 (5.0%), 596 NOK, 1.19%, with an exact assert to 1e-9. |
| Mutation check | Ten pieces of the fix reverted in turn; each made at least one test fail. |
| Config unchanged | `git diff` on `config.py`, `.env.example`, `docker-compose.yml` is empty. The built image's code default is still 2.0. |

The new tests cover:

1. A buy price of 101 gives stop 95.00.
2. No buy price gives 94.00 from the close.
3. Buy prices 98 / 100 / 101 / 102.5 give four stops, all 6.00 below.
4. The 2× and 3× settings are used as set, both from the close and from 101.
5. Distance and percent are against the right reference, including sizing and the loss in NOK.
6. Bad prices are refused: 0, −101, nan, inf, 1e999, "abc", a price more than 15% from the close without `!`, and too many words. A stop at or below 0 gives none.
7. The `/check` text with and without a buy price, including a USD stock.
8. Existing tests unedited and passing.
9. Saved records and tables unchanged.

## 7. Limitations

- **The ATR is from the signal's bars** and not re-measured on the buy day. The stop distance in price is N × that ATR, as the spec says.
  - The research's `entry-open` anchor instead places the stop at `entry × (1 − N × ATR%)`, which is N × ATR% of the entry. The two differ by (entry − close) × N × ATR%, about 0.06 on the example.
  - Neither is measured for gaps.
- **No gap estimate for a stop below a buy price.** To get one, run `research/gap_risk.py` with `entry-open` under the #161 method and add the row with anchor `buy_price`. That is a research run, not done here.
- **The suggested size follows the buy price.** With a buy price the risk rule uses the stop distance from it, the same rule on the same reference. That is slightly more than from the close when the buy is above the close (8.42% vs 8.33% of the account in the example), unless the cap binds. Without a buy price nothing changes.
- **`/recommendations` and published picks still have no buy price** and keep the signal-close estimate, now labelled as one. Linking `/fill` buys to picks is a feature, not this fix (`risk-reporting-report.md` §8.2).
- **`/check` does not use a recorded `/fill` price automatically.** A past buy is not the order being checked; give the price explicitly.
- **The 15% typo check is the `/fill` one.** A genuine gap of more than 15% needs `!`.
- **Test output.** A `ResourceWarning` line printed at interpreter shutdown also appears on unmodified `main`, and the suite still passes.
