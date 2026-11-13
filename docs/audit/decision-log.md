# Decisions for the owner

Status: **open** until the owner records a choice here.

## D1: Broker and FX tariffs for the cost model

**Status:** open.

- The repo knows DNB: 0.12% with a minimum of 59 NOK.
- Nordnet appears only in the project memory (Mini 29/49 NOK).
- The DNB FX spread for USD and EUR is unknown.

**Needed:** the broker used for each market, and the FX fee. Without these, P1-1 ships with clearly labelled estimates.

## D2: Live risk settings

**Status:** open.

The Pi uses 3×ATR with **2% risk per trade**, a 25% position cap and 100% maximum exposure. Research §8 assumed 0.5% risk.

- At 2% risk, a gap like the measured worst case loses about 4–5% of the account per position.
- Four positions at 100% exposure put the whole account in four correlated names.

**Options:**
- Keep the current settings.
- Return to 0.5–1% risk.
- Lower `RISK_MAX_TOTAL_PCT`.

## D3: Dashboard access

**Status:** open.

**Options:**
- **(a)** Bind to 127.0.0.1 and use an SSH tunnel or VPN. **Recommended.**
- **(b)** Bind to the LAN IP only.
- **(c)** Put nginx in front with basic auth.

Portainer (8000/9443) and poster-bot (8080) on the same Pi are exposed in the same way. Those are outside this repo but worth fixing in the same change.

## D4: Pre-registered forward-test protocol

**Status:** proposed. To be frozen before looking at any further results.

- **Horizon:** 20 sessions. Benchmark: the same-date screened universe, plus the market index for each suffix.
- **Independence:** use signal dates at least 20 sessions apart, or a date-block bootstrap with 20-session blocks.
- **Minimums:**
  - At least 24 independent dates overall, which is about 2 years.
  - At least 12 independent dates each for US and NO. EU is reported but not judged until it has 12.
- **Pass rule (all must hold):**
  - The mean excess return of the picks, net of the **base** cost model, is above 0 with a 90% CI lower bound above 0.
  - It is positive in both halves.
  - The picks' maximum drawdown is no worse than the benchmark's plus 5 pp.
  - The data-quality check passed on at least 95% of dates.
- **Until it passes:** the output stays "paper / research". No BUY labels.
- **Implication:** at about 12 independent 20-session dates a year, a verdict cannot come before about late 2028. Judge "no edge" earlier only if the 90% CI upper bound is below 0.

## D5: India

**Status:** open. Remove it from the evaluation basket (`broad-v2`), keeping v1 for the record?

## D6: Promote `docs/audit/proposed-CLAUDE.md` to the root `CLAUDE.md`

**Status:** open.

## D7: Survivorship-free data

**Status:** open. Should the project pay for a delisted-prices dataset? Without one, no backtest can be called unbiased.

## Market coverage matrix (current)

| Attribute | US | Norway | Europe |
|---|---|---|---|
| Exchanges | NYSE and Nasdaq, through Yahoo symbols | Oslo Børs (`.OL`) | Live: XETRA `.DE`, Euronext `.AS`/`.PA` (4 names). Research list: also `.MI`, `.MC`, `.BR` and others (46 names). LSE, SIX, Nordics and Warsaw: mapped benchmarks only. |
| Prices | Yahoo; survivors only | Yahoo; survivors only | Yahoo; survivors only |
| Fundamentals | Yahoo only; no filing dates | ESEF with filing dates, from 2020 | ESEF with filing dates, from 2020 |
| Corporate actions | Yahoo adjustment; not validated | Not validated | Not validated |
| Costs | Flat 10 bps; FX missing | Flat 10 bps | Flat 10 bps; FX missing |
| FX | Text only | Not applicable | Text only |
| Calendars | Custom handling | Custom handling | Custom handling |
| Backtest | No edge (survivors) | No edge | No edge |

**Classification:**
- **Partially supported:** US, NO, and the EU names already listed.
- **Need engineering:** the rest of the Euronext/Xetra/Nordic list.
- **Keep disabled:** UK, Switzerland, Spain beyond the current list, Poland.
