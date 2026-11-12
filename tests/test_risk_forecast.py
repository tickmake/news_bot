"""risk_forecast: how much a stock can fall over a 2-4 week hold (#123)."""

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from newsbot import config as nb_config
from newsbot import history_store as nb_history_store
from newsbot import price_store, risk_forecast


def _series(values, start=date(2024, 1, 1)):
    days = [start + timedelta(days=i) for i in range(len(values))]
    return pd.Series(values, index=days, dtype=float)


def _zigzag(n=400):
    # Up 1% then down 0.5%, so every window has a known best and worst.
    values, price = [], 100.0
    for i in range(n):
        price *= 1.01 if i % 2 else 0.995
        values.append(price)
    return values


class ComputeTests(unittest.TestCase):
    def test_too_little_history_gives_nothing(self):
        self.assertIsNone(risk_forecast.compute(_series([100.0] * 50)))
        self.assertIsNone(risk_forecast.compute(None))

    def test_biggest_fall_and_its_dates(self):
        values = _zigzag(300)
        top = values[-1]
        values += [top * 0.7] * 30 + [top * 0.9] * 30   # a 30% fall, then a part recovery
        forecast = risk_forecast.compute(_series(values))
        self.assertAlmostEqual(forecast.max_drop_pct, -30.0, places=6)
        self.assertEqual(forecast.drop_from, date(2024, 1, 1) + timedelta(days=299))
        self.assertEqual(forecast.drop_to, date(2024, 1, 1) + timedelta(days=300))
        labels = [label for label, _ in forecast.bad_moves]
        self.assertEqual(labels, ["2 weeks", "4 weeks"])
        self.assertLess(forecast.bad_moves[1][1], -20.0)   # the fall sits inside the last year

    def test_report_day_moves_are_measured_over_two_sessions(self):
        values = [100.0] * 200
        values[150:] = [110.0] * 50     # +10% on the report day (index 150)
        close = _series(values)
        report = close.index[150]
        forecast = risk_forecast.compute(close, [report])
        self.assertEqual(forecast.earnings_reports, 1)
        self.assertAlmostEqual(forecast.earnings_move_pct, 10.0)
        # A report after the last saved close can't be measured yet.
        self.assertEqual(risk_forecast.compute(close, [close.index[-1]]).earnings_reports, 0)


class LinesTests(unittest.TestCase):
    def test_lines_with_an_amount(self):
        forecast = risk_forecast.Forecast(
            sessions=504, bad_moves=(("2 weeks", -6.2), ("4 weeks", -9.5)), max_drop_pct=-31.4,
            drop_from=date(2025, 3, 3), drop_to=date(2025, 8, 1),
            earnings_move_pct=5.3, earnings_reports=8, normal_two_day_pct=1.8)
        text = risk_forecast.lines(forecast, 12_000)
        self.assertEqual(text[0], "    a bad 2 weeks: -6.2% ≈ -744 NOK on 12,000 (1 in 20 was worse, last year)")
        self.assertIn("a bad 4 weeks: -9.5% ≈ -1,140 NOK", text[1])
        self.assertEqual(text[2], "    biggest fall in 2 years: -31.4% (Mar 2025 to Aug 2025)")
        self.assertEqual(text[3], "    report days: moved ±5.3% on average over the last 8 (a normal 2 days: ±1.8%)")
        self.assertEqual(risk_forecast.lines(None), [])

    def test_short_history_and_no_reports(self):
        forecast = risk_forecast.Forecast(
            sessions=200, bad_moves=(("2 weeks", -3.0),), max_drop_pct=-12.0,
            drop_from=date(2026, 1, 5), drop_to=date(2026, 2, 5))
        text = "\n".join(risk_forecast.lines(forecast))
        self.assertIn("biggest fall in the saved history: -12.0%", text)
        self.assertNotIn("report days", text)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        for p in (patch.object(price_store, "TRADE_HISTORY", self.store),
                  patch.object(nb_config.SETTINGS, "risk_forecast_enabled", True),
                  patch.object(risk_forecast, "_EARNINGS_CACHE", {})):
            p.start()
            self.addCleanup(p.stop)

    def _save(self, symbol, values):
        index = pd.date_range("2024-01-01", periods=len(values), freq="D", tz="Europe/Oslo")
        frame = pd.DataFrame({"Close": values, "Adj Close": values}, index=index)
        price_store.save_prices(symbol, frame, "NOK")

    def test_saved_prices_are_used_without_yahoo(self):
        self._save("EQNR.OL", _zigzag(300))
        self.assertEqual(len(price_store.closes("EQNR.OL")), 300)
        self.assertIsNone(price_store.closes("NOPE.OL"))
        with patch.object(risk_forecast.market_data, "_fetch_ticker_history") as yahoo, \
             patch.object(risk_forecast, "_past_earnings_dates", side_effect=RuntimeError("down")):
            forecast = risk_forecast.build("EQNR.OL")
        yahoo.assert_not_called()
        self.assertIsNotNone(forecast)
        self.assertIsNone(forecast.earnings_move_pct)

    def test_unsaved_stock_falls_back_to_yahoo(self):
        index = pd.date_range("2024-01-01", periods=300, freq="B", tz="America/New_York")
        history = pd.DataFrame({"Close": _zigzag(300)}, index=index)
        with patch.object(risk_forecast.market_data, "_fetch_ticker_history", return_value=history), \
             patch.object(risk_forecast, "_drop_in_progress_bar", side_effect=lambda h: h), \
             patch.object(risk_forecast, "_past_earnings_dates", return_value=[]):
            self.assertIsNotNone(risk_forecast.build("AAPL"))

    def test_disabled_or_failing_gives_none(self):
        with patch.object(nb_config.SETTINGS, "risk_forecast_enabled", False):
            self.assertIsNone(risk_forecast.build("EQNR.OL"))
        with patch.object(risk_forecast, "_closes", side_effect=RuntimeError("db")):
            self.assertIsNone(risk_forecast.build("EQNR.OL"))

    def test_past_report_dates_are_cached_and_skip_the_future(self):
        today = pd.Timestamp.now().normalize()
        frame = pd.DataFrame(index=pd.DatetimeIndex([today + pd.Timedelta(days=20),
                                                     today - pd.Timedelta(days=80),
                                                     today - pd.Timedelta(days=170)]))
        with patch.object(risk_forecast.yf, "Ticker") as ticker:
            ticker.return_value.get_earnings_dates.return_value = frame
            first = risk_forecast._past_earnings_dates("EQNR.OL")
            second = risk_forecast._past_earnings_dates("EQNR.OL")
        self.assertEqual(first, [(today - pd.Timedelta(days=80)).date(), (today - pd.Timedelta(days=170)).date()])
        self.assertEqual(first, second)
        self.assertEqual(ticker.call_count, 1)


if __name__ == "__main__":
    unittest.main()
