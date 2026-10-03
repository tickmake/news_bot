"""Relative strength vs market and sector, beta, and its wiring into the risk context."""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd

from newsbot import config as nb_config
from newsbot import indicators as nb_indicators
from newsbot import market_data as md
from newsbot import relative_strength as rs
from newsbot import risk


def _series(values, end="2026-10-02", freq="B"):
    index = pd.date_range(end=end, periods=len(values), freq=freq)
    return pd.Series([float(v) for v in values], index=[d.strftime("%Y-%m-%d") for d in index])


def _grow(total_pct, n=70, start=100.0):
    """A smooth path that gains `total_pct` percent over n-1 steps."""
    return _series(start * np.linspace(1.0, 1.0 + total_pct / 100.0, n))


def _fetch_from(table):
    return table.get


def _m(symbol="AAA", atr=2.0):
    return nb_indicators.TradeMetrics(
        symbol=symbol, session="2026-10-02", last_close=100.0, day_change_pct=1.0,
        week_momentum_pct=3.0, volume_ratio=1.5, drawdown_pct=2.0, atr_pct=atr, above_ema20=True,
    )


class WindowParsingTests(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(rs.parse_windows("60, 20,20"), [20, 60])
        self.assertEqual(rs.parse_windows("5,x,1,0,-3"), [5])
        for raw in ("", "x,y", None):
            self.assertEqual(rs.parse_windows(raw), [20, 60])


class SectorMappingTests(unittest.TestCase):
    def test_us_listings_map_to_spdr_etfs(self):
        self.assertEqual(rs.sector_etf("AAPL", "Technology"), "XLK")
        self.assertEqual(rs.sector_etf("JPM", "Financial Services"), "XLF")

    def test_non_us_symbols_and_unknown_sectors_have_no_proxy(self):
        self.assertIsNone(rs.sector_etf("EQNR.OL", "Energy"))
        self.assertIsNone(rs.sector_etf("^GSPC", "Energy"))
        self.assertIsNone(rs.sector_etf("AAPL", "Aerospace"))
        self.assertIsNone(rs.sector_etf("AAPL", None))

    def test_every_mapped_etf_is_distinct(self):
        self.assertEqual(len(set(rs.SECTOR_ETFS.values())), len(rs.SECTOR_ETFS))


class ComputeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(nb_config.SETTINGS, "relative_strength_windows", "20,60")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_excess_returns_against_market_and_sector(self):
        table = {"AAA": _grow(20.0), "SPY": _grow(8.0), "XLK": _grow(12.0)}
        result = rs.compute("AAA", "Technology", _fetch_from(table))
        self.assertEqual((result.market_symbol, result.sector_symbol), ("SPY", "XLK"))
        by_window = {w.sessions: w for w in result.windows}
        self.assertEqual(sorted(by_window), [20, 60])
        # A linear path: the last 60 of 69 steps cover 60/69 of the total gain.
        self.assertGreater(by_window[60].stock_pct, by_window[20].stock_pct)
        self.assertGreater(by_window[60].vs_market, 0)
        self.assertGreater(by_window[60].vs_sector, 0)
        self.assertAlmostEqual(by_window[20].vs_market, by_window[20].stock_pct - by_window[20].market_pct)

    def test_lagging_the_market_is_negative(self):
        table = {"AAA": _grow(2.0), "SPY": _grow(10.0)}
        result = rs.compute("AAA", None, _fetch_from(table))
        self.assertTrue(all(w.vs_market < 0 for w in result.windows))
        self.assertIsNone(result.sector_symbol)

    def test_a_missing_benchmark_keeps_the_rest(self):
        table = {"AAA": _grow(10.0), "XLK": _grow(5.0)}   # SPY unavailable
        result = rs.compute("AAA", "Technology", _fetch_from(table))
        self.assertIsNone(result.market_symbol)
        self.assertIsNone(result.beta)
        self.assertIsNotNone(result.windows[0].vs_sector)
        self.assertIsNone(result.windows[0].vs_market)

    def test_unusable_own_history_gives_none(self):
        for table in ({}, {"AAA": _series([100.0, 101.0])}):
            self.assertIsNone(rs.compute("AAA", None, _fetch_from({**table, "SPY": _grow(5.0)})))

    def test_windows_longer_than_the_history_are_dropped(self):
        table = {"AAA": _grow(10.0, n=30), "SPY": _grow(5.0, n=30)}
        result = rs.compute("AAA", None, _fetch_from(table))
        self.assertEqual([w.sessions for w in result.windows], [20])
        short = {"AAA": _grow(10.0, n=10), "SPY": _grow(5.0, n=10)}
        self.assertIsNone(rs.compute("AAA", None, _fetch_from(short)))

    def test_other_markets_use_their_own_benchmark_on_their_own_calendar(self):
        # Oslo stock vs OSEBX.OL; the index skips every fifth session. Returns are
        # taken between the same two dates, using the last close on or before each.
        stock = _grow(10.0)
        index = _grow(5.0).iloc[[i for i in range(70) if i % 5 != 0]]
        result = rs.compute("EQNR.OL", "Energy", _fetch_from({"EQNR.OL": stock, "OSEBX.OL": index}))
        self.assertEqual(result.market_symbol, "OSEBX.OL")
        self.assertIsNone(result.sector_symbol)
        self.assertTrue(all(w.vs_market is not None for w in result.windows))

    def test_benchmark_that_starts_after_the_window_gives_no_market_return(self):
        stock = _grow(10.0)
        late = _grow(5.0, n=10)
        result = rs.compute("AAA", None, _fetch_from({"AAA": stock, "SPY": late}))
        self.assertIsNone(result.windows[-1].market_pct)

    def test_window_is_exactly_n_sessions_back(self):
        # Closes 100, 101, ... so the 20-session return is close[-1] / close[-21] - 1.
        stock = _series([100.0 + i for i in range(70)])
        result = rs.compute("AAA", None, _fetch_from({"AAA": stock}))
        window = {w.sessions: w for w in result.windows}[20]
        self.assertAlmostEqual(window.stock_pct, (169.0 / 149.0 - 1) * 100)
        self.assertIsNone(window.market_pct)

    def test_a_benchmark_closed_on_the_start_date_uses_its_previous_close(self):
        stock = _series([100.0 + i for i in range(70)])
        market_closes = [200.0 + i for i in range(70)]
        market = _series(market_closes)
        start_date = stock.index[-21]
        market = market.drop(start_date)           # that exchange was shut on the start date
        result = rs.compute("AAA", None, _fetch_from({"AAA": stock, "SPY": market}))
        window = {w.sessions: w for w in result.windows}[20]
        previous = market_closes[list(_series(market_closes).index).index(start_date) - 1]
        self.assertAlmostEqual(window.market_pct, (market_closes[-1] / previous - 1) * 100)

    def test_safe_compute_swallows_errors(self):
        def broken(symbol):
            raise RuntimeError("boom")

        self.assertIsNone(rs.safe_compute("AAA", None, broken))


class BetaTests(unittest.TestCase):
    def test_beta_recovers_the_slope(self):
        rng = np.random.default_rng(3)
        market_returns = rng.normal(0, 0.01, 80)
        market = _series(100 * np.cumprod(1 + market_returns))
        stock = _series(100 * np.cumprod(1 + 1.5 * market_returns))
        self.assertAlmostEqual(rs.compute_beta(stock, market, 60), 1.5, places=2)

    def test_inverse_and_flat_markets(self):
        rng = np.random.default_rng(4)
        r = rng.normal(0, 0.01, 80)
        market = _series(100 * np.cumprod(1 + r))
        inverse = _series(100 * np.cumprod(1 - r))
        self.assertLess(rs.compute_beta(inverse, market, 60), -0.9)
        self.assertIsNone(rs.compute_beta(inverse, _series([100.0] * 81), 60))

    def test_too_few_shared_dates_gives_none(self):
        self.assertIsNone(rs.compute_beta(_grow(5.0, n=20), _grow(5.0, n=20), 60))

    def test_nonpositive_closes_are_not_trusted(self):
        series = _series([100.0, 0.0, 100.0, 100.0])
        self.assertIsNone(rs._at_or_before(series, series.index[1]))
        self.assertIsNone(rs._at_or_before(series, "1999-01-01"))


class RenderTests(unittest.TestCase):
    def _strength(self, **kw):
        windows = kw.pop("windows", (
            rs.WindowStrength(20, 6.0, 2.0, 5.0),
            rs.WindowStrength(60, 10.0, 12.0, None),
        ))
        return rs.RelativeStrength(
            market_symbol=kw.get("market", "SPY"), sector_symbol=kw.get("sector", "XLK"),
            windows=windows, beta=kw.get("beta", 1.25),
        )

    def test_full_line(self):
        line = rs.render_line(self._strength())
        self.assertEqual(
            line,
            "    relative strength 20d / 60d: +4.0 / -2.0 pp vs SPY · +1.0 / n/a pp vs XLK · beta 1.25",
        )

    def test_without_sector_or_beta(self):
        line = rs.render_line(self._strength(sector=None, beta=None))
        self.assertNotIn("XLK", line)
        self.assertNotIn("beta", line)

    def test_nothing_to_compare_gives_no_line(self):
        self.assertIsNone(rs.render_line(None))
        empty = self._strength(windows=(rs.WindowStrength(20, 5.0, None, None),), market=None, sector=None)
        self.assertIsNone(rs.render_line(empty))


class ContextWiringTests(unittest.TestCase):
    def setUp(self):
        for p in (
            patch.object(nb_config.SETTINGS, "risk_analytics_enabled", True),
            patch.object(nb_config.SETTINGS, "relative_strength_enabled", True),
            patch.object(nb_config.SETTINGS, "relative_strength_windows", "20,60"),
            patch.object(nb_config.SETTINGS, "catalysts_enabled", False),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _fetch(self, calls):
        table = {"AAA": _grow(20.0), "BBB": _grow(5.0), "SPY": _grow(8.0), "XLK": _grow(12.0)}

        def fake(symbol, period="3mo", interval="1d"):
            calls.append((symbol, period))
            return pd.DataFrame({"Close": table[symbol].values},
                                index=pd.DatetimeIndex(table[symbol].index, tz="UTC"))

        return fake

    def test_each_distinct_symbol_is_fetched_once_for_six_months(self):
        calls = []
        profiles = {"AAA": {"sector": "Technology"}, "BBB": {"sector": "Technology"}}
        with patch("newsbot.market_data._fetch_ticker_history", side_effect=self._fetch(calls)), \
             patch.object(risk, "_drop_in_progress_bar", side_effect=lambda h: h):
            result = risk._relative_strength([_m("AAA"), _m("BBB")], profiles)
        self.assertEqual(sorted(s for s, _ in calls), ["AAA", "BBB", "SPY", "XLK"])
        self.assertTrue(all(period == "6mo" for _, period in calls))
        self.assertEqual(set(result), {"AAA", "BBB"})

    def test_one_failed_fetch_costs_only_that_comparison(self):
        def fake(symbol, period="3mo", interval="1d"):
            if symbol == "XLK":
                raise RuntimeError("yahoo")
            return self._fetch([])(symbol, period)

        with patch("newsbot.market_data._fetch_ticker_history", side_effect=fake), \
             patch.object(risk, "_drop_in_progress_bar", side_effect=lambda h: h):
            result = risk._relative_strength([_m("AAA")], {"AAA": {"sector": "Technology"}})
        self.assertIsNone(result["AAA"].sector_symbol)
        self.assertEqual(result["AAA"].market_symbol, "SPY")

    def test_build_context_attaches_strength_and_renders_the_line(self):
        calls = []
        with patch.object(md, "_fetch_event_profile", return_value={"sector": "Technology", "next_earnings": None}), \
             patch("newsbot.market_data._fetch_ticker_history", side_effect=self._fetch(calls)), \
             patch.object(risk, "_drop_in_progress_bar", side_effect=lambda h: h), \
             patch.object(risk, "correlated_pairs", return_value=[]):
            context = risk.build_context([_m("AAA")])
        lines = risk.candidate_lines("AAA", context)
        self.assertTrue(any("relative strength 20d / 60d" in line and "vs SPY" in line and "vs XLK" in line
                            for line in lines))

    def test_disabled_flag_skips_the_fetches(self):
        with patch.object(nb_config.SETTINGS, "relative_strength_enabled", False), \
             patch.object(md, "_fetch_event_profile", return_value={}), \
             patch("newsbot.market_data._fetch_ticker_history") as fetch, \
             patch.object(risk, "correlated_pairs", return_value=[]):
            context = risk.build_context([_m("AAA")])
        fetch.assert_not_called()
        self.assertEqual(context.strength, {})

    def test_slow_fetches_do_not_hold_the_screen(self):
        import threading

        release = threading.Event()
        self.addCleanup(release.set)

        def slow(symbol, period="3mo", interval="1d"):
            release.wait(5)

        with patch.object(nb_config.SETTINGS, "risk_context_deadline_seconds", 0), \
             patch("newsbot.market_data._fetch_ticker_history", side_effect=slow), \
             patch.object(risk.time, "monotonic", side_effect=[0.0] + [99.0] * 10):
            started = datetime.now()
            result = risk._relative_strength([_m("AAA")], {})
        self.assertLess((datetime.now() - started).total_seconds(), 3)
        self.assertEqual(result, {})

    def test_text_is_html_safe(self):
        strength = rs.RelativeStrength("SP<Y", None, (rs.WindowStrength(20, 5.0, 1.0, None),), None)
        context = risk.PortfolioRisk(strength={"AAA": strength})
        self.assertIn("SP&lt;Y", "\n".join(risk.candidate_lines("AAA", context)))


class TimestampHelperTests(unittest.TestCase):
    def test_date_closes_filters_short_and_duplicate_histories(self):
        end = (datetime.now() - timedelta(days=10)).date()
        index = pd.bdate_range(end=end, periods=30, tz="America/New_York")
        history = pd.DataFrame({"Close": np.linspace(100, 110, 30)}, index=index)
        with patch("newsbot.market_data._fetch_ticker_history", return_value=history):
            closes = risk._date_closes("X", "6mo")
        self.assertEqual(len(closes), 30)
        self.assertRegex(closes.index[0], r"^\d{4}-\d{2}-\d{2}$")
        with patch("newsbot.market_data._fetch_ticker_history", return_value=history.iloc[:10]):
            self.assertIsNone(risk._date_closes("X"))
        with patch("newsbot.market_data._fetch_ticker_history", return_value=None):
            self.assertIsNone(risk._date_closes("X"))


if __name__ == "__main__":
    unittest.main()
