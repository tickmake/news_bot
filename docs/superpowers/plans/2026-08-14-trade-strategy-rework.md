# Trade Candidate Scoring Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the additive six-criterion trade-candidate score with mandatory gates plus an ordinal ranking, so a stock with no momentum can no longer be admitted on its risk criteria alone.

**Architecture:** One tangled function (`_analyze_short_term_candidate`) splits into three pure units — `_compute_trade_metrics` (data), `_failed_gates` (admission), `_rank_key` (ordering). Universe construction becomes watchlist-first. `get_trade_candidates` wires them together and renders a diagnostic footer.

**Tech Stack:** Python 3.11 (CI pin), `unittest`, pandas (via yfinance), no new dependencies.

## Global Constraints

- All code lives in `news_bot.py`; all tests in `test_news_bot.py`. This project is a single module with a single test file — do not create new modules.
- Tests use `unittest`, class-per-concern, module-level helper functions. Do not introduce pytest.
- Run tests with `.venv/bin/python -m unittest`. CI uses Python 3.11.
- Use `typing` generics (`List`, `Dict`, `Optional`, `Tuple`) to match the existing file, not PEP 585 builtins.
- Logging uses the `event=key=value` style: `LOGGER.warning("thing_happened symbol=%s detail=%s", ...)`.
- All 129 existing tests must still pass at the end of every task.
- Disclaimer copy, verbatim: `Not investment advice. No guarantee of profit. Use strict risk management.`
- Gate names, verbatim and in this order: `trend`, `momentum_5d`, `momentum_1d`, `volatility`, `drawdown`.
- Spec: `docs/superpowers/specs/2026-08-14-trade-strategy-design.md`

---

### Task 1: Completed-session metrics

**Files:**
- Modify: `news_bot.py` — add after `_compute_atr_percent` (ends ~line 1721), before `_fetch_ticker_history`
- Test: `test_news_bot.py` — new class `TradeMetricsTests`

**Interfaces:**
- Consumes: `_fetch_ticker_history(symbol)`, `_compute_atr_percent(history)` (both exist)
- Produces:
  - `TradeMetrics` frozen dataclass with fields `symbol: str`, `session: str`, `last_close: float`, `day_change_pct: float`, `week_momentum_pct: float`, `volume_ratio: float`, `drawdown_pct: float`, `atr_pct: float`, `above_ema20: bool`
  - `_drop_in_progress_bar(history) -> Any`
  - `_compute_trade_metrics(symbol: str) -> Optional[TradeMetrics]`

- [ ] **Step 1: Add the test helper and the failing tests**

Add `import pandas as pd` to the imports at the top of `test_news_bot.py`, then add this helper at module level (near the existing `_cand` helper at line 612) and the new class at the end of the file:

```python
def _history(closes, volumes=None, last_date=None, tz="America/New_York"):
    """Build a yfinance-shaped OHLCV frame ending on `last_date`."""
    n = len(closes)
    end = last_date or (datetime.now().date() - timedelta(days=1))
    idx = pd.date_range(end=pd.Timestamp(end), periods=n, freq="D", tz=tz)
    close = pd.Series([float(c) for c in closes], index=idx, dtype="float64")
    vols = volumes if volumes is not None else [1_000_000] * n
    return pd.DataFrame(
        {
            "Close": close,
            "High": close * 1.005,
            "Low": close * 0.995,
            "Volume": pd.Series([float(v) for v in vols], index=idx, dtype="float64"),
        }
    )


class TradeMetricsTests(unittest.TestCase):
    def tearDown(self):
        news_bot.HISTORY_CACHE.clear()

    def _cache(self, symbol, history):
        news_bot.HISTORY_CACHE[f"{symbol}|3mo|1d"] = (time.time(), history)

    def test_drops_bar_dated_today(self):
        today = datetime.now(timezone.utc).date()
        history = _history([100.0] * 40, last_date=today, tz="UTC")
        trimmed = news_bot._drop_in_progress_bar(history)
        self.assertEqual(len(trimmed), 39)
        self.assertLess(trimmed.index[-1].date(), today)

    def test_keeps_bar_dated_before_today(self):
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        history = _history([100.0] * 40, last_date=yesterday, tz="UTC")
        trimmed = news_bot._drop_in_progress_bar(history)
        self.assertEqual(len(trimmed), 40)

    def test_returns_none_below_thirty_usable_bars(self):
        self._cache("SHORT", _history([100.0] * 29))
        self.assertIsNone(news_bot._compute_trade_metrics("SHORT"))

    def test_metrics_computed_on_known_fixture(self):
        closes = [100.0] * 35 + [100.0, 101.0, 102.0, 103.0, 104.0]
        self._cache("RISE", _history(closes))
        metrics = news_bot._compute_trade_metrics("RISE")
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.symbol, "RISE")
        self.assertAlmostEqual(metrics.last_close, 104.0, places=4)
        # 104 vs prior close 103
        self.assertAlmostEqual(metrics.day_change_pct, 0.970873786, places=6)
        # 104 vs the close five bars back (100.0)
        self.assertAlmostEqual(metrics.week_momentum_pct, 4.0, places=6)
        self.assertTrue(metrics.above_ema20)
        self.assertAlmostEqual(metrics.drawdown_pct, 0.0, places=6)

    def test_session_reflects_last_kept_bar(self):
        last = datetime.now().date() - timedelta(days=3)
        self._cache("SESS", _history([100.0] * 40, last_date=last))
        metrics = news_bot._compute_trade_metrics("SESS")
        self.assertEqual(metrics.session, last.strftime("%Y-%m-%d"))
```

Add `import time` to the test file imports if not already present.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest test_news_bot.TradeMetricsTests -v`
Expected: FAIL — `AttributeError: module 'news_bot' has no attribute '_drop_in_progress_bar'`

- [ ] **Step 3: Implement**

Insert into `news_bot.py` immediately after `_compute_atr_percent`:

```python
@dataclass(frozen=True)
class TradeMetrics:
    """Measured facts about one symbol. Carries no pass/fail judgement."""

    symbol: str
    session: str
    last_close: float
    day_change_pct: float
    week_momentum_pct: float
    volume_ratio: float
    drawdown_pct: float
    atr_pct: float
    above_ema20: bool


def _drop_in_progress_bar(history: Any) -> Any:
    """Drop the final row when it is dated today in the exchange's timezone.

    yfinance returns a tz-aware DatetimeIndex in the exchange's own timezone, so
    "today" is evaluated there rather than in the host's locale. A market that
    already closed today still has its session discarded: this screen ranks
    symbols against each other, and letting some rows advance a session while
    others do not would compare momentum windows offset by a day.
    """
    if history is None or len(history) == 0:
        return history
    index = history.index
    tz = getattr(index, "tz", None)
    today = datetime.now(tz).date() if tz is not None else datetime.now().date()
    if index[-1].date() == today:
        return history.iloc[:-1]
    return history


def _compute_trade_metrics(symbol: str) -> Optional[TradeMetrics]:
    """Measure one symbol, or None when the data cannot support a measurement.

    None means "no usable data" and nothing else. Whether a symbol qualifies is
    _failed_gates' decision; keeping the two separate is what lets the caller
    report why a run found nothing.
    """
    try:
        history = _drop_in_progress_bar(_fetch_ticker_history(symbol))
        close_series = history["Close"].dropna()
        if len(close_series) < 30:
            LOGGER.debug(
                "metrics_skipped symbol=%s reason=insufficient_history bars=%s",
                symbol,
                len(close_series),
            )
            return None

        last_close = float(close_series.iloc[-1])
        prev_close = float(close_series.iloc[-2])
        if prev_close == 0:
            LOGGER.debug("metrics_skipped symbol=%s reason=zero_prev_close", symbol)
            return None
        day_change_pct = ((last_close - prev_close) / prev_close) * 100

        # Safe unconditionally: the length guard above already rules out short series.
        reference_close = float(close_series.iloc[-6])
        if reference_close == 0:
            LOGGER.debug("metrics_skipped symbol=%s reason=zero_reference_close", symbol)
            return None
        week_momentum_pct = ((last_close - reference_close) / reference_close) * 100

        ema_20 = float(close_series.ewm(span=20, adjust=False).mean().iloc[-1])

        volume_ratio = 1.0
        if "Volume" in history:
            volume_series = history["Volume"].dropna()
            if len(volume_series) >= 20:
                avg_volume_20 = float(volume_series.tail(20).mean())
                if avg_volume_20 > 0:
                    volume_ratio = float(volume_series.iloc[-1]) / avg_volume_20

        rolling_high_20 = float(close_series.tail(20).max())
        drawdown_pct = (
            ((rolling_high_20 - last_close) / rolling_high_20) * 100 if rolling_high_20 else 0.0
        )

        atr_pct = _compute_atr_percent(history)
        if atr_pct is None:
            LOGGER.debug("metrics_skipped symbol=%s reason=atr_unavailable", symbol)
            return None

        return TradeMetrics(
            symbol=symbol,
            session=close_series.index[-1].strftime("%Y-%m-%d"),
            last_close=last_close,
            day_change_pct=day_change_pct,
            week_momentum_pct=week_momentum_pct,
            volume_ratio=volume_ratio,
            drawdown_pct=drawdown_pct,
            atr_pct=atr_pct,
            above_ema20=last_close > ema_20,
        )
    except Exception as exc:
        LOGGER.debug("metrics_failed symbol=%s detail=%s", symbol, exc)
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m unittest test_news_bot.TradeMetricsTests -v`
Expected: PASS, 5 tests

Then the full suite: `.venv/bin/python -m unittest -q`
Expected: OK, 134 tests

- [ ] **Step 5: Commit**

```bash
git add news_bot.py test_news_bot.py
git commit -m "feat: measure trade metrics on the last completed session

Splits measurement away from admission. _compute_trade_metrics returns None
only when the data cannot support a measurement, never to mean 'did not
qualify' -- that conflation is why the screen could not report why a run
found nothing.

The in-progress bar is dropped whenever it is dated today in the exchange's
own timezone. A market that already closed today still loses that session:
the screen ranks symbols against each other, and letting some rows advance
while others do not would compare momentum windows offset by a day."
```

---

### Task 2: Admission gates

**Files:**
- Modify: `news_bot.py` — add directly after `_compute_trade_metrics`
- Test: `test_news_bot.py` — new class `TradeGateTests`

**Interfaces:**
- Consumes: `TradeMetrics` from Task 1
- Produces:
  - `TRADE_GATES: Tuple[str, ...]` — `("trend", "momentum_5d", "momentum_1d", "volatility", "drawdown")`
  - `_failed_gates(metrics: TradeMetrics) -> List[str]` — empty list means qualified; first element is the first failing gate

- [ ] **Step 1: Write the failing tests**

Append to `test_news_bot.py`:

```python
def _metrics(**overrides):
    """A qualifying TradeMetrics, with fields overridden per test."""
    base = dict(
        symbol="AAA",
        session="2026-08-13",
        last_close=100.0,
        day_change_pct=1.0,
        week_momentum_pct=3.0,
        volume_ratio=1.5,
        drawdown_pct=2.0,
        atr_pct=2.0,
        above_ema20=True,
    )
    base.update(overrides)
    return news_bot.TradeMetrics(**base)


class TradeGateTests(unittest.TestCase):
    def test_qualifying_metrics_fail_nothing(self):
        self.assertEqual(news_bot._failed_gates(_metrics()), [])

    def test_declining_stock_is_rejected(self):
        failed = news_bot._failed_gates(
            _metrics(above_ema20=False, week_momentum_pct=-0.26, day_change_pct=-0.05)
        )
        self.assertEqual(failed[0], "trend")
        self.assertIn("momentum_5d", failed)
        self.assertIn("momentum_1d", failed)

    def test_flat_stock_is_rejected(self):
        failed = news_bot._failed_gates(
            _metrics(above_ema20=False, week_momentum_pct=0.0, day_change_pct=0.0)
        )
        self.assertTrue(failed)

    def test_volume_spike_alone_cannot_admit(self):
        """The primary defect: risk criteria plus volume used to reach the
        passing score with negative momentum. Volume no longer admits at all."""
        failed = news_bot._failed_gates(
            _metrics(
                above_ema20=False,
                week_momentum_pct=-0.26,
                day_change_pct=-0.05,
                volume_ratio=2.73,   # capitulation-sized spike
                drawdown_pct=0.99,   # safely near the 20d high
                atr_pct=1.00,        # comfortably low volatility
            )
        )
        self.assertNotEqual(failed, [])

    def test_high_volatility_is_rejected(self):
        with patch.object(news_bot.SETTINGS, "trade_max_atr_pct", 4.5):
            self.assertEqual(news_bot._failed_gates(_metrics(atr_pct=9.0)), ["volatility"])

    def test_deep_drawdown_is_rejected(self):
        with patch.object(news_bot.SETTINGS, "trade_max_drawdown_pct", 8.0):
            self.assertEqual(news_bot._failed_gates(_metrics(drawdown_pct=20.0)), ["drawdown"])

    def test_gate_order_is_stable(self):
        failed = news_bot._failed_gates(
            _metrics(above_ema20=False, week_momentum_pct=-1.0, atr_pct=99.0)
        )
        self.assertEqual(failed, ["trend", "momentum_5d", "volatility"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest test_news_bot.TradeGateTests -v`
Expected: FAIL — `AttributeError: module 'news_bot' has no attribute '_failed_gates'`

- [ ] **Step 3: Implement**

Insert into `news_bot.py` after `_compute_trade_metrics`:

```python
# Evaluated in this order, so a rejected symbol attributes to its first
# meaningful blocker in the diagnostic footer.
TRADE_GATES: Tuple[str, ...] = (
    "trend",
    "momentum_5d",
    "momentum_1d",
    "volatility",
    "drawdown",
)


def _failed_gates(metrics: TradeMetrics) -> List[str]:
    """Gates this symbol fails. Empty means it qualifies.

    Every gate is mandatory. The previous additive score let three risk
    criteria substitute for the three momentum criteria, so a steadily
    declining stock was admitted on low volatility and low drawdown alone.

    Volume is deliberately absent: it confirms a move but its absence does not
    disqualify one, so it ranks (see _rank_key) rather than admits.
    """
    failed: List[str] = []
    if not metrics.above_ema20:
        failed.append("trend")
    if metrics.week_momentum_pct < SETTINGS.trade_min_week_momentum_pct:
        failed.append("momentum_5d")
    if metrics.day_change_pct <= SETTINGS.trade_min_day_change_pct:
        failed.append("momentum_1d")
    if metrics.atr_pct > SETTINGS.trade_max_atr_pct:
        failed.append("volatility")
    if metrics.drawdown_pct > SETTINGS.trade_max_drawdown_pct:
        failed.append("drawdown")
    return failed
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m unittest test_news_bot.TradeGateTests -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add news_bot.py test_news_bot.py
git commit -m "feat: make every momentum criterion a mandatory gate

The additive score let any three criteria substitute for any other three, so
low ATR plus low drawdown plus a volume spike reached the passing threshold
with negative 1D and 5D returns. Low volatility and low drawdown are
automatically satisfied by stocks that do not move, which biased the screen
toward exactly the names least likely to produce momentum.

Volume no longer admits. It confirms a move but its absence does not
disqualify one, so it ranks instead."
```

---

### Task 3: Ranking key

**Files:**
- Modify: `news_bot.py` — add directly after `_failed_gates`
- Test: `test_news_bot.py` — new class `TradeRankingTests`

**Interfaces:**
- Consumes: `TradeMetrics` (Task 1)
- Produces: `_rank_key(metrics: TradeMetrics) -> Tuple[float, float, float, str]`

- [ ] **Step 1: Write the failing tests**

Append to `test_news_bot.py` (reuses the `_metrics` helper from Task 2):

```python
class TradeRankingTests(unittest.TestCase):
    def test_orders_by_week_momentum_first(self):
        low = _metrics(symbol="LOW", week_momentum_pct=1.0)
        high = _metrics(symbol="HIGH", week_momentum_pct=9.0)
        self.assertEqual(
            [m.symbol for m in sorted([low, high], key=news_bot._rank_key)],
            ["HIGH", "LOW"],
        )

    def test_volume_breaks_momentum_ties(self):
        quiet = _metrics(symbol="QUIET", week_momentum_pct=3.0, volume_ratio=1.0)
        busy = _metrics(symbol="BUSY", week_momentum_pct=3.0, volume_ratio=2.0)
        self.assertEqual(
            [m.symbol for m in sorted([quiet, busy], key=news_bot._rank_key)],
            ["BUSY", "QUIET"],
        )

    def test_lower_atr_breaks_volume_ties(self):
        calm = _metrics(symbol="CALM", week_momentum_pct=3.0, volume_ratio=1.5, atr_pct=1.0)
        wild = _metrics(symbol="WILD", week_momentum_pct=3.0, volume_ratio=1.5, atr_pct=4.0)
        self.assertEqual(
            [m.symbol for m in sorted([calm, wild], key=news_bot._rank_key)],
            ["CALM", "WILD"],
        )

    def test_symbol_makes_full_ties_deterministic(self):
        a = _metrics(symbol="AAA")
        b = _metrics(symbol="BBB")
        self.assertEqual(
            [m.symbol for m in sorted([b, a], key=news_bot._rank_key)],
            ["AAA", "BBB"],
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest test_news_bot.TradeRankingTests -v`
Expected: FAIL — `AttributeError: module 'news_bot' has no attribute '_rank_key'`

- [ ] **Step 3: Implement**

Insert into `news_bot.py` after `_failed_gates`:

```python
def _rank_key(metrics: TradeMetrics) -> Tuple[float, float, float, str]:
    """Ordering for qualifying candidates: momentum, then confirmation, then calm.

    An ordinal rule rather than a weighted composite. Choosing weights needs
    backtesting this codebase has no harness for, and arbitrary weights
    presented as precision are worse than an explicit ordering. Symbol sorts
    last so identical metrics always produce the same output.
    """
    return (
        -metrics.week_momentum_pct,
        -metrics.volume_ratio,
        metrics.atr_pct,
        metrics.symbol,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m unittest test_news_bot.TradeRankingTests -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add news_bot.py test_news_bot.py
git commit -m "feat: rank qualifying candidates by an ordinal key

Momentum, then volume confirmation, then lower volatility, then symbol for
determinism. An ordinal rule rather than a weighted composite: choosing
weights requires backtesting this codebase has no harness for."
```

---

### Task 4: Watchlist-first universe

**Files:**
- Modify: `news_bot.py` — add before `get_trade_candidates` (~line 1808)
- Test: `test_news_bot.py` — new class `CandidateUniverseTests`

**Interfaces:**
- Consumes: `USA_STOCK_UNIVERSE`, `INDIA_STOCK_UNIVERSE`, `NORWAY_STOCK_UNIVERSE`, `INDIA_MUTUAL_FUNDS`, `NORWAY_MUTUAL_FUNDS`, `_build_live_universe(limit)` (all exist)
- Produces:
  - `_configured_watchlist() -> List[Tuple[str, str]]` — `(label, symbol)` pairs, labels suffixed `[USA Stock]` etc.
  - `_build_candidate_universe(max_symbols: int) -> List[Tuple[str, str]]`

- [ ] **Step 1: Write the failing tests**

Append to `test_news_bot.py`:

```python
class CandidateUniverseTests(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch.object(news_bot, "USA_STOCK_UNIVERSE", {"Apple": "AAPL"}),
            patch.object(news_bot, "INDIA_STOCK_UNIVERSE", {"Infosys": "INFY.NS"}),
            patch.object(news_bot, "NORWAY_STOCK_UNIVERSE", {"Equinor": "EQNR.OL"}),
            patch.object(news_bot, "INDIA_MUTUAL_FUNDS", {}),
            patch.object(news_bot, "NORWAY_MUTUAL_FUNDS", {}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_watchlist_survives_healthy_screeners(self):
        """Regression: the watchlist used to be appended after ~67 screener
        names and then truncated away entirely."""
        screener = {f"Mover {i}": f"SCR{i}" for i in range(67)}
        with patch.object(news_bot, "_build_live_universe", return_value=screener):
            universe = news_bot._build_candidate_universe(30)
        symbols = [symbol for _, symbol in universe]
        self.assertEqual(len(symbols), 30)
        for expected in ("AAPL", "INFY.NS", "EQNR.OL"):
            self.assertIn(expected, symbols)

    def test_watchlist_entries_come_first(self):
        screener = {f"Mover {i}": f"SCR{i}" for i in range(10)}
        with patch.object(news_bot, "_build_live_universe", return_value=screener):
            universe = news_bot._build_candidate_universe(10)
        self.assertEqual(
            [symbol for _, symbol in universe[:3]], ["AAPL", "INFY.NS", "EQNR.OL"]
        )

    def test_screener_not_called_when_watchlist_fills_budget(self):
        with patch.object(news_bot, "_build_live_universe") as mock_live:
            universe = news_bot._build_candidate_universe(3)
        mock_live.assert_not_called()
        self.assertEqual(len(universe), 3)

    def test_oversized_watchlist_truncates_and_warns(self):
        with patch.object(news_bot, "USA_STOCK_UNIVERSE", {f"N{i}": f"S{i}" for i in range(10)}):
            with patch.object(news_bot, "_build_live_universe") as mock_live:
                with self.assertLogs(news_bot.LOGGER, level="WARNING") as logs:
                    universe = news_bot._build_candidate_universe(4)
        mock_live.assert_not_called()
        self.assertEqual(len(universe), 4)
        self.assertTrue(any("watchlist_truncated" in line for line in logs.output))

    def test_symbol_in_both_appears_once_with_watchlist_label(self):
        screener = {"Apple Inc Screener Name": "AAPL", "Other": "OTHR"}
        with patch.object(news_bot, "_build_live_universe", return_value=screener):
            universe = news_bot._build_candidate_universe(10)
        apple_labels = [label for label, symbol in universe if symbol == "AAPL"]
        self.assertEqual(len(apple_labels), 1)
        self.assertIn("[USA Stock]", apple_labels[0])

    def test_labels_carry_market_suffix(self):
        with patch.object(news_bot, "_build_live_universe", return_value={}):
            universe = news_bot._build_candidate_universe(10)
        labels = dict((symbol, label) for label, symbol in universe)
        self.assertEqual(labels["AAPL"], "Apple [USA Stock]")
        self.assertEqual(labels["EQNR.OL"], "Equinor [Norway Stock]")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest test_news_bot.CandidateUniverseTests -v`
Expected: FAIL — `AttributeError: module 'news_bot' has no attribute '_build_candidate_universe'`

- [ ] **Step 3: Implement**

Insert into `news_bot.py` immediately before `get_trade_candidates`:

```python
def _configured_watchlist() -> List[Tuple[str, str]]:
    """(label, symbol) pairs for every symbol configured via environment."""
    entries: List[Tuple[str, str]] = []
    for suffix, mapping in (
        ("USA Stock", USA_STOCK_UNIVERSE),
        ("India Stock", INDIA_STOCK_UNIVERSE),
        ("Norway Stock", NORWAY_STOCK_UNIVERSE),
        ("India Fund", INDIA_MUTUAL_FUNDS),
        ("Norway Fund", NORWAY_MUTUAL_FUNDS),
    ):
        for name, symbol in mapping.items():
            entries.append((f"{name} [{suffix}]", symbol))
    return entries


def _build_candidate_universe(max_symbols: int) -> List[Tuple[str, str]]:
    """Configured watchlist first, screener discovery filling what remains.

    The previous order appended the watchlist *after* ~67 screener names and
    then truncated to the cap, so with healthy screeners no configured symbol
    was ever analysed. Whichever side loses a slot now, it is reported.

    Screener names are discovery only: they decide which symbols are looked at,
    never whether one qualifies. That decision belongs to _failed_gates, which
    reads completed-session metrics.
    """
    selected: List[Tuple[str, str]] = []
    seen: set = set()

    for label, symbol in _configured_watchlist():
        if symbol in seen:
            continue
        seen.add(symbol)
        selected.append((label, symbol))

    if len(selected) > max_symbols:
        LOGGER.warning(
            "watchlist_truncated configured=%s analysed=%s detail=raise TRADE_UNIVERSE_MAX to analyse all",
            len(selected),
            max_symbols,
        )
        return selected[:max_symbols]

    # A full budget means no screener call at all.
    if len(selected) >= max_symbols:
        return selected

    for label, symbol in _build_live_universe(limit=max_symbols).items():
        if symbol in seen:
            continue
        seen.add(symbol)
        selected.append((label, symbol))
        if len(selected) >= max_symbols:
            break
    return selected
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m unittest test_news_bot.CandidateUniverseTests -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add news_bot.py test_news_bot.py
git commit -m "fix: analyse the configured watchlist before screener movers

The watchlist was appended after the ~67-entry screener universe and then
truncated to TRADE_UNIVERSE_MAX, so with healthy screeners none of the
configured symbols were ever analysed. Anyone who set USA_STOCK_UNIVERSE or
its siblings had those settings silently do nothing.

Watchlist entries now come first and the screener fills what remains. An
oversized watchlist is truncated with a warning rather than silently, and a
full budget skips the screener call entirely."
```

---

### Task 5: Wire up screening, rendering, and diagnostics

**Files:**
- Modify: `news_bot.py` — replace `_analyze_short_term_candidate` (lines 1745-1805) and `get_trade_candidates` (lines 1808-1890)
- Modify: `test_news_bot.py:47-92` and `test_news_bot.py:315-321` — three existing tests mock `_analyze_short_term_candidate`, which no longer exists
- Test: `test_news_bot.py` — new class `TradeRenderingTests`

**Interfaces:**
- Consumes: `TradeMetrics`, `_compute_trade_metrics` (Task 1), `TRADE_GATES`, `_failed_gates` (Task 2), `_rank_key` (Task 3), `_build_candidate_universe` (Task 4), `_render_pre_table`, `_truncate` (exist)
- Produces:
  - `ScreenOutcome` dataclass — `checked: int`, `analysed: int`, `no_data: int`, `qualified: List[TradeMetrics]`, `failed_counts: Dict[str, int]`, `deadline_hit: bool`
  - `_screen_universe(candidate_items) -> Tuple[ScreenOutcome, Dict[str, str]]`
  - `get_trade_candidates(universe=None, top_n=5) -> str` (signature unchanged)

- [ ] **Step 1: Update the three existing tests that mock the removed function**

Replace `test_news_bot.py:47-92` with:

```python
    @patch("news_bot._compute_trade_metrics")
    def test_get_trade_candidates_formats_ranked_output(self, mock_metrics):
        def side_effect(symbol):
            data = {
                "AAA": news_bot.TradeMetrics(
                    symbol="AAA", session="2026-08-13", last_close=100.0,
                    day_change_pct=1.2, week_momentum_pct=3.5, volume_ratio=1.6,
                    drawdown_pct=2.1, atr_pct=1.4, above_ema20=True,
                ),
                "BBB": news_bot.TradeMetrics(
                    symbol="BBB", session="2026-08-13", last_close=80.0,
                    day_change_pct=0.4, week_momentum_pct=2.1, volume_ratio=1.3,
                    drawdown_pct=3.0, atr_pct=2.0, above_ema20=True,
                ),
                "CCC": None,
            }
            return data.get(symbol)

        mock_metrics.side_effect = side_effect

        result = news_bot.get_trade_candidates(
            universe={"Alpha": "AAA", "Beta": "BBB", "Gamma": "CCC"},
            top_n=2,
        )

        self.assertIn("Short-Term Trade Candidates", result)
        self.assertIn("Alpha", result)
        self.assertIn("AAA", result)
        self.assertIn("Beta", result)
        self.assertIn("BBB", result)
        self.assertNotIn("Gamma", result)
        self.assertIn("Not investment advice. No guarantee of profit.", result)

    @patch("news_bot._compute_trade_metrics")
    def test_get_trade_candidates_no_matches(self, mock_metrics):
        mock_metrics.return_value = None
        result = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=2)
        self.assertIn("No candidates qualified.", result)
```

Replace `test_news_bot.py:315-321` with:

```python
    @patch("news_bot._compute_trade_metrics")
    def test_get_trade_candidates_caps_universe(self, mock_metrics):
        mock_metrics.return_value = None
        big_universe = {f"Name{i}": f"SYM{i}" for i in range(50)}
        with patch.object(news_bot.SETTINGS, "trade_universe_max", 5):
            news_bot.get_trade_candidates(universe=big_universe, top_n=3)
        self.assertEqual(mock_metrics.call_count, 5)
```

- [ ] **Step 2: Write the new failing tests**

Append to `test_news_bot.py`:

```python
class TradeRenderingTests(unittest.TestCase):
    def _qualifying(self, symbol, momentum, volume=1.6):
        return news_bot.TradeMetrics(
            symbol=symbol, session="2026-08-13", last_close=100.0,
            day_change_pct=1.0, week_momentum_pct=momentum, volume_ratio=volume,
            drawdown_pct=1.0, atr_pct=1.0, above_ema20=True,
        )

    def test_score_column_is_gone_and_session_present(self):
        with patch("news_bot._compute_trade_metrics", return_value=self._qualifying("AAA", 5.0)):
            result = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=1)
        self.assertIn("Session", result)
        self.assertIn("2026-08-13", result)
        self.assertNotIn("Score", result)

    def test_volume_confirmation_marker(self):
        with patch.object(news_bot.SETTINGS, "trade_min_volume_ratio", 1.2):
            with patch("news_bot._compute_trade_metrics", return_value=self._qualifying("AAA", 5.0, volume=2.0)):
                confirmed = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=1)
            with patch("news_bot._compute_trade_metrics", return_value=self._qualifying("BBB", 5.0, volume=0.5)):
                unconfirmed = news_bot.get_trade_candidates(universe={"Beta": "BBB"}, top_n=1)
        self.assertIn("2.00✓", confirmed)
        self.assertIn("0.50", unconfirmed)
        self.assertNotIn("0.50✓", unconfirmed)

    def test_diagnostics_attribute_to_first_failing_gate(self):
        rejected = news_bot.TradeMetrics(
            symbol="AAA", session="2026-08-13", last_close=100.0,
            day_change_pct=-1.0, week_momentum_pct=-1.0, volume_ratio=1.0,
            drawdown_pct=1.0, atr_pct=1.0, above_ema20=False,
        )
        with patch("news_bot._compute_trade_metrics", return_value=rejected):
            result = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=1)
        self.assertIn("Checked 1", result)
        self.assertIn("qualified 0", result)
        self.assertIn("trend 1", result)
        # Attributed to `trend` only, though momentum_5d also failed.
        self.assertNotIn("momentum_5d", result)

    def test_diagnostic_counts_balance(self):
        def side_effect(symbol):
            if symbol == "AAA":
                return self._qualifying("AAA", 5.0)
            if symbol == "BBB":
                return None
            return news_bot.TradeMetrics(
                symbol=symbol, session="2026-08-13", last_close=100.0,
                day_change_pct=-1.0, week_momentum_pct=-1.0, volume_ratio=1.0,
                drawdown_pct=1.0, atr_pct=1.0, above_ema20=False,
            )

        with patch("news_bot._compute_trade_metrics", side_effect=side_effect):
            result = news_bot.get_trade_candidates(
                universe={"A": "AAA", "B": "BBB", "C": "CCC"}, top_n=3
            )
        self.assertIn("Checked 3", result)
        self.assertIn("no data 1", result)
        self.assertIn("qualified 1", result)
        self.assertIn("trend 1", result)

    def test_ranked_order_in_output(self):
        def side_effect(symbol):
            return self._qualifying(symbol, {"AAA": 1.0, "BBB": 9.0}[symbol])

        with patch("news_bot._compute_trade_metrics", side_effect=side_effect):
            result = news_bot.get_trade_candidates(universe={"A": "AAA", "B": "BBB"}, top_n=2)
        self.assertLess(result.index("BBB"), result.index("AAA"))
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest test_news_bot.TradeRenderingTests -v`
Expected: FAIL — output still contains `Score`, and no `Session` column

- [ ] **Step 4: Implement**

Delete `_analyze_short_term_candidate` entirely (lines 1745-1805) and replace `get_trade_candidates` (lines 1808-1890) with:

```python
@dataclass
class ScreenOutcome:
    """What one screening run saw. Rendered as the diagnostic footer.

    Invariant: checked - no_data == len(qualified) + sum(failed_counts.values())
    when the deadline did not truncate the run.
    """

    checked: int = 0
    analysed: int = 0
    no_data: int = 0
    qualified: List[TradeMetrics] = field(default_factory=list)
    failed_counts: Dict[str, int] = field(default_factory=dict)
    deadline_hit: bool = False


def _screen_universe(
    candidate_items: List[Tuple[str, str]]
) -> Tuple[ScreenOutcome, Dict[str, str]]:
    outcome = ScreenOutcome(checked=len(candidate_items))
    labels = {symbol: name for name, symbol in candidate_items}
    workers = max(1, min(SETTINGS.trade_fetch_workers, len(candidate_items) or 1))
    deadline = time.time() + max(5, SETTINGS.trade_total_deadline_seconds)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_symbol = {
            pool.submit(_compute_trade_metrics, symbol): symbol
            for _, symbol in candidate_items
        }
        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                metrics = future.result()
            except Exception as exc:
                LOGGER.debug("candidate_future_failed symbol=%s detail=%s", symbol, exc)
                metrics = None

            outcome.analysed += 1
            if metrics is None:
                outcome.no_data += 1
            else:
                failed = _failed_gates(metrics)
                if failed:
                    outcome.failed_counts[failed[0]] = (
                        outcome.failed_counts.get(failed[0], 0) + 1
                    )
                else:
                    outcome.qualified.append(metrics)

            # Checked after the result is consumed: testing first discarded work
            # that had already finished.
            if time.time() >= deadline:
                LOGGER.warning(
                    "trade_candidates_deadline_reached analysed=%s of=%s",
                    outcome.analysed,
                    outcome.checked,
                )
                outcome.deadline_hit = True
                for pending in future_to_symbol:
                    pending.cancel()
                break

    outcome.qualified.sort(key=_rank_key)
    return outcome, labels


def _render_gate_summary() -> str:
    text = (
        f"Gates: trend>EMA20, "
        f"5D>={SETTINGS.trade_min_week_momentum_pct:.1f}%, "
        f"1D>{SETTINGS.trade_min_day_change_pct:.1f}%, "
        f"DD<={SETTINGS.trade_max_drawdown_pct:.1f}%, "
        f"ATR<={SETTINGS.trade_max_atr_pct:.1f}%"
    )
    return text.replace("<=", "≤").replace(">=", "≥").replace(">", "›")


def _render_diagnostics(outcome: ScreenOutcome) -> List[str]:
    checked_text = f"Checked {outcome.checked}"
    if outcome.deadline_hit:
        checked_text += f" (deadline hit, analysed {outcome.analysed} of {outcome.checked})"
    lines = [
        f"{checked_text} · no data {outcome.no_data} · qualified {len(outcome.qualified)}"
    ]
    present = [gate for gate in TRADE_GATES if outcome.failed_counts.get(gate)]
    if present:
        lines.append(
            "Failed: " + ", ".join(f"{gate} {outcome.failed_counts[gate]}" for gate in present)
        )
    return lines


def get_trade_candidates(universe: Optional[Dict[str, str]] = None, top_n: int = 5) -> str:
    universe_max = max(1, SETTINGS.trade_universe_max)
    if universe is None:
        candidate_items = _build_candidate_universe(universe_max)
    else:
        candidate_items = list(universe.items())[:universe_max]

    outcome, labels = _screen_universe(candidate_items)

    lines = [
        "⚠️ Short-Term Trade Candidates (Informational Only):",
        "Not investment advice. No guarantee of profit. Use strict risk management.",
        _render_gate_summary(),
        "Metrics use each symbol's last completed session.",
    ]

    if outcome.qualified:
        rows: List[List[str]] = []
        for idx, metrics in enumerate(outcome.qualified[:top_n], 1):
            confirmed = "✓" if metrics.volume_ratio >= SETTINGS.trade_min_volume_ratio else ""
            rows.append(
                [
                    str(idx),
                    _truncate(labels.get(metrics.symbol, metrics.symbol), 24),
                    metrics.symbol,
                    metrics.session,
                    f"{metrics.last_close:.2f}",
                    f"{metrics.day_change_pct:+.2f}%",
                    f"{metrics.week_momentum_pct:+.2f}%",
                    f"{metrics.volume_ratio:.2f}{confirmed}",
                    f"{metrics.drawdown_pct:.2f}%",
                    f"{metrics.atr_pct:.2f}%",
                ]
            )
        lines.append(
            _render_pre_table(
                "Candidates",
                ["#", "Name", "Symbol", "Session", "Close", "1D", "5D", "Volx", "DD", "ATR"],
                rows,
            )
        )
    else:
        lines.append("No candidates qualified.")

    lines.extend(_render_diagnostics(outcome))
    return "\n".join(lines) + "\n\n"
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m unittest -q`
Expected: OK, 156 tests (129 existing + 5 + 7 + 4 + 6 + 5; the three rewritten
tests replace their originals rather than adding)

- [ ] **Step 6: Commit**

```bash
git add news_bot.py test_news_bot.py
git commit -m "feat: gate, rank, and report the trade candidate screen

Wires the split units together. Every displayed row provably passed every
gate, and the footer reports what the run saw -- checked, no data, qualified,
and the first failing gate per rejection -- so thresholds can be tuned
without reading container logs.

The deadline check moves after the result is consumed; testing it first
discarded work that had already finished.

Drops the '1-2% daily profit' phrasing, which asserted a target inside a
disclaimer."
```

---

### Task 6: Deprecate TRADE_MIN_SCORE and update docs

**Files:**
- Modify: `news_bot.py` — add `_warn_deprecated_settings` before `if __name__ == "__main__":`, and call it there
- Modify: `README.md:248-262`
- Test: `test_news_bot.py` — new class `DeprecatedSettingTests`

**Interfaces:**
- Consumes: `SETTINGS`, `AppSettings`
- Produces: `_warn_deprecated_settings() -> None`

- [ ] **Step 1: Write the failing tests**

Append to `test_news_bot.py`:

```python
class DeprecatedSettingTests(unittest.TestCase):
    def test_warns_when_trade_min_score_is_customised(self):
        with patch.object(news_bot.SETTINGS, "trade_min_score", 5):
            with self.assertLogs(news_bot.LOGGER, level="WARNING") as logs:
                news_bot._warn_deprecated_settings()
        self.assertTrue(any("TRADE_MIN_SCORE" in line for line in logs.output))

    def test_silent_at_default_value(self):
        default = news_bot.AppSettings.model_fields["trade_min_score"].default
        with patch.object(news_bot.SETTINGS, "trade_min_score", default):
            with patch.object(news_bot.LOGGER, "warning") as mock_warn:
                news_bot._warn_deprecated_settings()
        mock_warn.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest test_news_bot.DeprecatedSettingTests -v`
Expected: FAIL — `AttributeError: module 'news_bot' has no attribute '_warn_deprecated_settings'`

- [ ] **Step 3: Implement**

Add to `news_bot.py` immediately before `if __name__ == "__main__":`:

```python
def _warn_deprecated_settings() -> None:
    """Report settings that no longer do anything.

    Reading the default off the model rather than hardcoding it keeps this
    honest if the field's default ever changes. Ignoring a customised value
    silently is the same failure that hid the truncated watchlist.
    """
    default_min_score = AppSettings.model_fields["trade_min_score"].default
    if SETTINGS.trade_min_score != default_min_score:
        LOGGER.warning(
            "deprecated_setting name=TRADE_MIN_SCORE value=%s detail=ignored; "
            "candidates now pass mandatory gates instead of a score threshold",
            SETTINGS.trade_min_score,
        )
```

Then call it inside the `__main__` block, immediately after the `missing_values` check:

```python
    _warn_deprecated_settings()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m unittest test_news_bot.DeprecatedSettingTests -v`
Expected: PASS, 2 tests

- [ ] **Step 5: Update README.md**

Replace the `TRADE_MIN_SCORE` bullet at `README.md:248` with:

```markdown
- `TRADE_MIN_SCORE` - **deprecated and ignored.** Candidates now pass mandatory
  gates rather than reaching a score threshold. Setting it logs a warning at
  startup.
```

Replace the `TRADE_MIN_VOLUME_RATIO` bullet at `README.md:251` with:

```markdown
- `TRADE_MIN_VOLUME_RATIO` - threshold for the `✓` volume-confirmation marker.
  Volume does not admit or reject a candidate; it breaks ties in the ranking.
```

Add after the `TRADE_TOTAL_DEADLINE_SECONDS` bullet at `README.md:262`:

```markdown
Candidates must pass every gate: price above its 20-day EMA, 5-day and 1-day
returns above their thresholds, and ATR and drawdown below their ceilings.
Metrics are computed on each symbol's last completed session — an in-progress
bar is never used, so at the 19:00 run markets that closed earlier that day
still report their previous session. Configured watchlist symbols are analysed
before screener movers.
```

- [ ] **Step 6: Run the full suite and commit**

Run: `.venv/bin/python -m unittest -q`
Expected: OK, 158 tests

```bash
git add news_bot.py test_news_bot.py README.md
git commit -m "docs: deprecate TRADE_MIN_SCORE and document the gate model

The setting keeps its field so existing .env files still load, but it no
longer does anything; a non-default value warns at startup. Silently ignoring
it would repeat the failure that hid the truncated watchlist.

README now states the gate model, the completed-session rule and what it
costs on the evening run, and the narrowed meaning of
TRADE_MIN_VOLUME_RATIO."
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `TradeMetrics` / three-way split | 1 |
| Completed-session rule | 1 |
| Gates table, order, mandatory | 2 |
| Volume ranks not admits | 2, 3 |
| `_rank_key` ordering | 3 |
| Watchlist-first universe | 4 |
| Watchlist truncation warning | 4 |
| Screener skipped when budget full | 4 |
| Dedup by symbol | 4 |
| Output columns, `Session` per row | 5 |
| `Volx` `✓` marker | 5 |
| Disclaimer copy change | 5 |
| Diagnostics, first-failing-gate | 5 |
| Deadline fix | 5 |
| `TRADE_MIN_SCORE` deprecation | 6 |
| README updates | 6 |

**Not carried into tasks, deliberately:** mutual-fund volume behaviour is a documented consequence requiring no code (funds fall through the existing `volume_ratio = 1.0` default); the residual discovery bias is an accepted property of decision 4.

**Type consistency:** `TradeMetrics` field names are identical in Tasks 1, 2, 3, 5 and in the `_metrics` test helper. `TRADE_GATES` strings match `_failed_gates` appends and `_render_diagnostics` lookups. `_build_candidate_universe` returns `List[Tuple[str, str]]` as `(label, symbol)`, consumed that way by `_screen_universe`.

**Test counts:** 129 existing + 5 (T1) + 7 (T2) + 4 (T3) + 6 (T4) + 5 (T5) + 2 (T6) = 158. Task 5 rewrites three existing tests rather than adding, so intermediate counts in steps reflect the running total at that point.
