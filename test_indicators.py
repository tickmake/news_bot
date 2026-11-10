"""indicators: ATR, RSI, volume trend, in-progress bar handling, metrics, gates."""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

from newsbot import config as nb_config
from newsbot import indicators as ind


def _frame(closes, *, spread=1.0, volume=1000.0, days_ago=10, tz="UTC", with_volume=True):
    end = (datetime.now() - timedelta(days=days_ago)).date()
    index = pd.bdate_range(end=end, periods=len(closes), tz=tz)
    data = {
        "Close": [float(c) for c in closes],
        "High": [float(c) + spread for c in closes],
        "Low": [float(c) - spread for c in closes],
    }
    if with_volume:
        data["Volume"] = volume if isinstance(volume, list) else [volume] * len(closes)
    return pd.DataFrame(data, index=index)


def _metrics(**overrides):
    base = {
        "symbol": "AAA", "session": "2026-09-01", "last_close": 100.0, "day_change_pct": 1.0,
        "week_momentum_pct": 3.0, "volume_ratio": 1.5, "drawdown_pct": 2.0, "atr_pct": 2.0,
        "above_ema20": True,
    }
    base.update(overrides)
    return ind.TradeMetrics(**base)


class AtrTests(unittest.TestCase):
    def test_constant_range_gives_that_percentage(self):
        # High-low is 2 on a close of 100 every day, so ATR is 2%.
        self.assertAlmostEqual(ind._compute_atr_percent(_frame([100] * 30)), 2.0)

    def test_gaps_widen_the_true_range(self):
        closes = [100] * 20 + [110] * 10
        self.assertGreater(ind._compute_atr_percent(_frame(closes)), 2.0)

    def test_too_little_history_is_none(self):
        self.assertIsNone(ind._compute_atr_percent(_frame([100] * 14)))

    def test_zero_last_close_is_none(self):
        frame = _frame([100] * 29 + [0])
        self.assertIsNone(ind._compute_atr_percent(frame))

    def test_missing_columns_are_swallowed(self):
        self.assertIsNone(ind._compute_atr_percent(pd.DataFrame({"Close": [1.0] * 30})))
        self.assertIsNone(ind._compute_atr_percent(None))


class RsiTests(unittest.TestCase):
    def test_all_gains_is_100(self):
        self.assertEqual(ind._compute_rsi_14(pd.Series(range(1, 31), dtype=float)), 100.0)

    def test_all_losses_is_0(self):
        self.assertEqual(ind._compute_rsi_14(pd.Series(range(30, 0, -1), dtype=float)), 0.0)

    def test_equal_gains_and_losses_is_50(self):
        series = pd.Series([10.0, 11.0] * 10)
        self.assertAlmostEqual(ind._compute_rsi_14(series), 50.0)

    def test_short_or_broken_input_is_none(self):
        self.assertIsNone(ind._compute_rsi_14(pd.Series([1.0] * 10)))
        self.assertIsNone(ind._compute_rsi_14(None))

    def test_series_last_point_matches_scalar(self):
        series = pd.Series([10, 11, 10.5, 12, 11, 13, 12.5, 14, 13, 15, 14, 16, 15.5, 17, 16, 18, 17.5, 19.0])
        self.assertAlmostEqual(float(ind._rsi_series(series).iloc[-1]), ind._compute_rsi_14(series))

    def test_series_handles_unbroken_gains_and_short_input(self):
        rising = ind._rsi_series(pd.Series(range(1, 31), dtype=float))
        self.assertTrue((rising == 100.0).all())
        self.assertIsNone(ind._rsi_series(pd.Series([1.0] * 5)))
        self.assertIsNone(ind._rsi_series(None))

    def test_series_swallows_unexpected_input(self):
        class NotASeries:
            def __len__(self):
                return 20

        self.assertIsNone(ind._rsi_series(NotASeries()))


class VolumeTrendTests(unittest.TestCase):
    @staticmethod
    def _series(prior, recent):
        return pd.Series([prior] * 15 + [recent] * 5, dtype=float)

    def test_rising_falling_flat(self):
        self.assertEqual(ind._compute_volume_trend(self._series(100, 130)), "rising")
        self.assertEqual(ind._compute_volume_trend(self._series(100, 70)), "falling")
        self.assertEqual(ind._compute_volume_trend(self._series(100, 105)), "flat")

    def test_unknown_when_data_is_unusable(self):
        self.assertEqual(ind._compute_volume_trend(pd.Series([1.0] * 10)), "unknown")
        with_nan = self._series(100, 100)
        with_nan.iloc[3] = float("nan")
        self.assertEqual(ind._compute_volume_trend(with_nan), "unknown")
        self.assertEqual(ind._compute_volume_trend(self._series(0, 50)), "unknown")
        self.assertEqual(ind._compute_volume_trend(None), "unknown")


class UnfinishedBarTests(unittest.TestCase):
    """The settle jobs keep a session once that exchange's day is over (#89)."""

    def _bars(self, tz):
        index = pd.DatetimeIndex([pd.Timestamp("2026-11-09", tz=tz), pd.Timestamp("2026-11-10", tz=tz)])
        return pd.DataFrame({"Close": [1.0, 2.0]}, index=index)

    def test_us_session_is_kept_at_three_in_the_night_oslo_time(self):
        # 03:00 Oslo on 11 Nov is 21:00 on 10 Nov in New York: the day is over.
        oslo_3am = pd.Timestamp("2026-11-11 03:00", tz="Europe/Oslo").to_pydatetime()
        bars = self._bars("America/New_York")
        self.assertEqual(len(ind._drop_unfinished_bar(bars, now=oslo_3am)), 2)

    def test_running_session_is_dropped(self):
        noon = pd.Timestamp("2026-11-10 12:00", tz="Europe/Oslo").to_pydatetime()
        self.assertEqual(len(ind._drop_unfinished_bar(self._bars("Europe/Oslo"), now=noon)), 1)

    def test_after_the_close_the_bar_is_kept(self):
        evening = pd.Timestamp("2026-11-10 18:30", tz="Europe/Oslo").to_pydatetime()
        self.assertEqual(len(ind._drop_unfinished_bar(self._bars("Europe/Oslo"), now=evening)), 2)

    def test_empty_and_naive_fall_back(self):
        self.assertIsNone(ind._drop_unfinished_bar(None))
        frame = _frame([1, 2, 3], days_ago=10, tz=None)
        self.assertEqual(len(ind._drop_unfinished_bar(frame)), 3)


class InProgressBarTests(unittest.TestCase):
    def test_none_and_empty_pass_through(self):
        self.assertIsNone(ind._drop_in_progress_bar(None))
        empty = pd.DataFrame({"Close": []})
        self.assertIs(ind._drop_in_progress_bar(empty), empty)

    def test_todays_bar_is_dropped_in_the_exchange_timezone(self):
        frame = _frame([1, 2, 3], days_ago=0, tz="America/New_York")
        today = datetime.now(frame.index.tz).date()
        frame.index = frame.index[:-1].append(pd.DatetimeIndex([pd.Timestamp(today, tz=frame.index.tz)]))
        self.assertEqual(len(ind._drop_in_progress_bar(frame)), 2)

    def test_naive_index_uses_the_local_date(self):
        frame = _frame([1, 2, 3], days_ago=10, tz=None)
        frame.index = frame.index[:-1].append(pd.DatetimeIndex([pd.Timestamp(datetime.now().date())]))
        self.assertEqual(len(ind._drop_in_progress_bar(frame)), 2)

    def test_completed_sessions_are_kept(self):
        frame = _frame([1, 2, 3], days_ago=10)
        self.assertEqual(len(ind._drop_in_progress_bar(frame)), 3)


class ComputeTradeMetricsTests(unittest.TestCase):
    def _compute(self, frame):
        with patch("newsbot.market_data._fetch_ticker_history", return_value=frame):
            return ind._compute_trade_metrics("AAA")

    def test_steady_uptrend_measurements(self):
        closes = [100 + i for i in range(40)]
        metrics = self._compute(_frame(closes, volume=[1000] * 39 + [2000]))
        self.assertEqual(metrics.symbol, "AAA")
        self.assertEqual(metrics.last_close, 139.0)
        self.assertAlmostEqual(metrics.day_change_pct, 1 / 138 * 100)
        self.assertAlmostEqual(metrics.week_momentum_pct, (139 / 134 - 1) * 100)
        self.assertTrue(metrics.above_ema20)
        self.assertEqual(metrics.resistance_level, 139.0)
        self.assertEqual(metrics.support_level, 120.0)
        self.assertAlmostEqual(metrics.drawdown_pct, 0.0)
        self.assertAlmostEqual(metrics.volume_ratio, 2.0)
        self.assertEqual(metrics.volume_trend, "rising")
        self.assertIsNotNone(metrics.rsi_14)
        self.assertRegex(metrics.session, r"^\d{4}-\d{2}-\d{2}$")

    def test_downtrend_is_below_its_average_and_drawn_down(self):
        metrics = self._compute(_frame([200 - i for i in range(40)]))
        self.assertFalse(metrics.above_ema20)
        self.assertGreater(metrics.drawdown_pct, 0)
        self.assertLess(metrics.week_momentum_pct, 0)

    def test_missing_volume_column_leaves_ratio_unknown(self):
        metrics = self._compute(_frame([100 + i for i in range(40)], with_volume=False))
        self.assertIsNone(metrics.volume_ratio)
        self.assertEqual(metrics.volume_trend, "unknown")

    def test_nan_in_the_volume_baseline_leaves_ratio_unknown(self):
        volume = [1000.0] * 30
        volume[25] = float("nan")  # inside the 20 sessions before the last one
        metrics = self._compute(_frame([100 + i for i in range(30)], volume=volume))
        self.assertIsNone(metrics.volume_ratio)

    def test_zero_average_volume_leaves_ratio_unknown(self):
        metrics = self._compute(_frame([100 + i for i in range(40)], volume=0.0))
        self.assertIsNone(metrics.volume_ratio)

    def test_unusable_data_gives_none(self):
        self.assertIsNone(self._compute(_frame([100] * 29)))  # under 30 bars
        self.assertIsNone(self._compute(_frame([100] * 28 + [0, 100])))  # zero previous close

    def test_zero_reference_close_gives_none(self):
        closes = [100.0] * 40
        closes[-6] = 0.0
        self.assertIsNone(self._compute(_frame(closes)))

    def test_atr_unavailable_gives_none(self):
        frame = _frame([100 + i for i in range(40)]).drop(columns=["High", "Low"])
        self.assertIsNone(self._compute(frame))

    def test_fetch_failure_gives_none(self):
        with patch("newsbot.market_data._fetch_ticker_history", side_effect=RuntimeError("yahoo")):
            self.assertIsNone(ind._compute_trade_metrics("AAA"))
        self.assertIsNone(self._compute(None))


class GateTests(unittest.TestCase):
    def test_a_clean_candidate_qualifies(self):
        self.assertEqual(ind._failed_gates(_metrics()), [])

    def test_each_gate_fails_on_its_own(self):
        cases = {
            "trend": {"above_ema20": False},
            "momentum_5d": {"week_momentum_pct": 0.5},
            "momentum_1d": {"day_change_pct": 0.0},
            "volatility": {"atr_pct": 5.0},
            "drawdown": {"drawdown_pct": 9.0},
        }
        for gate, override in cases.items():
            self.assertEqual(ind._failed_gates(_metrics(**override)), [gate], gate)

    def test_gates_are_reported_in_declared_order(self):
        failed = ind._failed_gates(_metrics(above_ema20=False, atr_pct=9.0, day_change_pct=-1.0))
        self.assertEqual(failed, ["trend", "momentum_1d", "volatility"])
        self.assertEqual([g for g in ind.TRADE_GATES if g in failed], failed)

    def test_thresholds_come_from_settings(self):
        with patch.object(nb_config.SETTINGS, "trade_max_atr_pct", 10.0):
            self.assertEqual(ind._failed_gates(_metrics(atr_pct=9.0)), [])

    def test_boundaries(self):
        s = nb_config.SETTINGS
        self.assertEqual(ind._failed_gates(_metrics(week_momentum_pct=s.trade_min_week_momentum_pct)), [])
        self.assertEqual(ind._failed_gates(_metrics(atr_pct=s.trade_max_atr_pct)), [])
        self.assertEqual(ind._failed_gates(_metrics(drawdown_pct=s.trade_max_drawdown_pct)), [])


if __name__ == "__main__":
    unittest.main()
