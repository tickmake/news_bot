"""Signal archive (permanent) and the /forwardtest report."""

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd

from newsbot import (
    config as nb_config,
    forward_test as nb_forward,
    history_store as nb_history_store,
    indicators as nb_indicators,
)


def _metrics(symbol="AAA", session="2026-09-01", **overrides):
    base = {
        "symbol": symbol, "session": session, "last_close": 100.0, "day_change_pct": 1.0,
        "week_momentum_pct": 3.0, "volume_ratio": 1.5, "drawdown_pct": 2.0, "atr_pct": 2.0,
        "above_ema20": True, "ema20": 98.0, "rsi_14": 60.0, "support_level": 95.0,
        "resistance_level": 102.0, "volume_trend": "flat",
    }
    base.update(overrides)
    return nb_indicators.TradeMetrics(**base)


def _bars(start, opens, closes):
    index = pd.date_range(start, periods=len(opens), freq="B", tz="America/New_York")
    return pd.DataFrame({"Open": opens, "Close": closes}, index=index)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(
            os.path.join(tempfile.mkdtemp(), "h.db")
        )
        self.addCleanup(self.store.close)

    def test_records_qualified_and_rejected_rows(self):
        self.store.archive_signal(_metrics("AAA"), True, None)
        self.store.archive_signal(_metrics("BBB"), False, "trend")
        rows = {row["symbol"]: row for row in self.store.archive_rows()}
        self.assertEqual(rows["AAA"]["qualified"], 1)
        self.assertEqual(rows["BBB"]["failed_gate"], "trend")
        self.assertEqual(rows["AAA"]["rule_id"], self.store.current_rule_id())

    def test_first_observation_wins(self):
        self.store.archive_signal(_metrics(last_close=100.0), True, None)
        self.store.archive_signal(_metrics(last_close=999.0), False, "trend")
        (row,) = self.store.archive_rows()
        self.assertEqual(row["last_close"], 100.0)
        self.assertEqual(row["qualified"], 1)

    def test_ranks_are_set_once(self):
        a, b = _metrics("AAA"), _metrics("BBB")
        for metrics in (a, b):
            self.store.archive_signal(metrics, True, None)
        self.store.archive_ranks([b, a])
        self.store.archive_ranks([a, b])
        ranks = {row["symbol"]: row["rank"] for row in self.store.archive_rows()}
        self.assertEqual(ranks, {"BBB": 1, "AAA": 2})

    def test_prune_leaves_the_archive_alone(self):
        self.store.record(_metrics(session="2020-01-02"), True, None)
        self.store.archive_signal(_metrics(session="2020-01-02"), True, None)
        self.store.prune(14, today="2026-10-03")
        self.assertEqual(self.store.history("AAA", days=99999, today="2020-02-01"), [])
        self.assertEqual(len(self.store.archive_rows()), 1)

    def test_rule_id_changes_with_gates(self):
        before = self.store.current_rule_id()
        with patch.object(nb_config.SETTINGS, "trade_max_atr_pct", 9.0):
            self.assertNotEqual(self.store.current_rule_id(), before)


class OutcomeTests(unittest.TestCase):
    ROW = {"signal_session": "2026-09-02", "horizon_sessions": 3, "cost_bps_per_side": 10.0}

    def test_enters_next_open_exits_horizon_close(self):
        history = _bars("2026-09-01", [10, 10, 11, 12, 13, 14], [10, 10.5, 11.5, 12.5, 13.5, 14.5])
        outcome = nb_forward.forward_outcome(self.ROW, history, None)
        # Signal 09-02 -> enter 09-03 open (11); hold 3 sessions -> 09-07 close (13.5).
        self.assertEqual(outcome["entry_session"], "2026-09-03")
        self.assertEqual(outcome["exit_session"], "2026-09-07")
        self.assertAlmostEqual(outcome["gross_return_pct"], (13.5 / 11 - 1) * 100)
        self.assertAlmostEqual(outcome["net_return_pct"], outcome["gross_return_pct"] - 0.2)
        self.assertIsNone(outcome["excess_return_pct"])

    def test_unfinished_hold_is_pending(self):
        history = _bars("2026-09-01", [10, 10, 11], [10, 10.5, 11.5])
        self.assertIsNone(nb_forward.forward_outcome(self.ROW, history, None))

    def test_bad_prices_raise(self):
        history = _bars("2026-09-01", [10, 10, 0, 12, 13, 14], [10, 10.5, 11.5, 12.5, 13.5, 14.5])
        with self.assertRaises(ValueError):
            nb_forward.forward_outcome(self.ROW, history, None)

    def test_benchmark_excess(self):
        history = _bars("2026-09-01", [10, 10, 11, 12, 13, 14], [10, 10.5, 11.5, 12.5, 13.5, 14.5])
        bench = _bars("2026-09-01", [100] * 6, [100, 100, 100, 100, 101, 101])
        outcome = nb_forward.forward_outcome(self.ROW, history, bench)
        self.assertAlmostEqual(outcome["benchmark_return_pct"], 1.0)
        self.assertAlmostEqual(outcome["excess_return_pct"], outcome["net_return_pct"] - 1.0)


class PairedTests(unittest.TestCase):
    @staticmethod
    def _rows(spec):
        return [
            {"signal_session": date, "outcome": {"net_return_pct": value}}
            for date, value in spec
        ]

    def test_paired_delta_weights_dates_equally(self):
        cohort = self._rows([("d1", 2.0), ("d2", 1.0), ("d2", 3.0)])
        baseline = self._rows([("d1", 1.0), ("d1", 1.0), ("d2", 0.0), ("d3", 5.0)])
        result = nb_forward.paired_vs_baseline(cohort, baseline)
        # d1: 2-1 = +1; d2: 2-0 = +2; d3 has no cohort row and is ignored.
        self.assertEqual(result["dates"], 2)
        self.assertAlmostEqual(result["mean_delta_pp"], 1.5)
        self.assertEqual(result["better_pct"], 100.0)

    def test_no_overlap_gives_empty_result(self):
        result = nb_forward.paired_vs_baseline(self._rows([("a", 1.0)]), self._rows([("b", 1.0)]))
        self.assertEqual(result["dates"], 0)
        self.assertIsNone(result["mean_delta_pp"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(
            os.path.join(tempfile.mkdtemp(), "h.db")
        )
        self.addCleanup(self.store.close)
        patcher = patch.object(nb_history_store, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_empty_archive_message(self):
        self.assertIn("No signals archived yet", nb_forward.build_forward_test_report())

    def test_report_settles_and_flags_thin_sample(self):
        winner, loser = _metrics("AAA", "2026-09-02"), _metrics("BBB", "2026-09-02")
        self.store.archive_signal(winner, True, None)
        self.store.archive_signal(loser, False, "trend")
        self.store.archive_ranks([winner])
        up = _bars("2026-09-01", [10, 10, 11, 12, 13, 14], [10, 10.5, 11.5, 12.5, 13.5, 14.5])
        flat = _bars("2026-09-01", [10] * 6, [10] * 6)

        def fake_history(symbol, period="3mo", interval="1d"):
            return up if symbol == "AAA" else flat

        with patch("newsbot.market_data._fetch_ticker_history", side_effect=fake_history), \
             patch.object(nb_forward, "_drop_in_progress_bar", side_effect=lambda h: h), \
             patch.object(nb_config.SETTINGS, "trade_outcome_lookback_days", 3):
            # Archive rows carry the horizon at recording time.
            with self.store._connection() as conn:
                conn.execute("UPDATE signal_archive SET horizon_sessions = 3")
            report = nb_forward.build_forward_test_report(datetime(2026, 10, 3))
        self.assertIn("2 completed", report)
        self.assertIn("Not enough data to conclude", report)
        self.assertIn("Top 5 as delivered: 1 signals", report)
        self.assertEqual(sum(1 for r in self.store.archive_rows() if r["outcome_json"]), 2)

    def test_other_rule_rows_are_excluded(self):
        self.store.archive_signal(_metrics(), True, None)
        with patch.object(nb_config.SETTINGS, "trade_max_atr_pct", 9.0), \
             patch("newsbot.market_data._fetch_ticker_history", return_value=None):
            report = nb_forward.build_forward_test_report(datetime(2026, 10, 3))
        self.assertIn("recorded under earlier rules (excluded)", report)


if __name__ == "__main__":
    unittest.main()
