"""Signal archive (permanent) and the /forwardtest report."""

import json
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
        self.assertIn("Qualified: 1 signals", report)
        self.assertIn("Every screened symbol (baseline): 2 signals", report)
        self.assertEqual(sum(1 for r in self.store.archive_rows() if r["outcome_json"]), 2)

    def test_other_rule_rows_are_excluded(self):
        self.store.archive_signal(_metrics(), True, None)
        with patch.object(nb_config.SETTINGS, "trade_max_atr_pct", 9.0), \
             patch("newsbot.market_data._fetch_ticker_history", return_value=None):
            report = nb_forward.build_forward_test_report(datetime(2026, 10, 3))
        self.assertIn("recorded under earlier rules (excluded)", report)


class OutcomeGuardTests(unittest.TestCase):
    ROW = {"signal_session": "2026-09-02", "horizon_sessions": 3, "cost_bps_per_side": 10.0}

    def _history(self):
        return _bars("2026-09-01", [10, 10, 11, 12, 13, 14], [10, 10.5, 11.5, 12.5, 13.5, 14.5])

    def test_missing_history_raises(self):
        for history in (None, pd.DataFrame({"Open": [], "Close": []})):
            with self.assertRaises(ValueError):
                nb_forward.forward_outcome(self.ROW, history, None)

    def test_invalid_horizon_raises(self):
        with self.assertRaises(ValueError):
            nb_forward.forward_outcome({**self.ROW, "horizon_sessions": 0}, self._history(), None)

    def test_duplicate_sessions_raise(self):
        history = self._history()
        history.index = history.index[:-1].append(history.index[-2:-1])
        with self.assertRaises(ValueError):
            nb_forward.forward_outcome(self.ROW, history, None)

    def test_history_that_starts_after_the_signal_raises(self):
        with self.assertRaises(ValueError):
            nb_forward.forward_outcome({**self.ROW, "signal_session": "2026-08-01"}, self._history(), None)

    def test_signal_on_the_last_bar_is_pending(self):
        self.assertIsNone(
            nb_forward.forward_outcome({**self.ROW, "signal_session": "2026-09-08"}, self._history(), None)
        )


class SettleTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        patcher = patch.object(nb_history_store, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.drop = patch.object(nb_forward, "_drop_in_progress_bar", side_effect=lambda h: h)
        self.drop.start()
        self.addCleanup(self.drop.stop)

    def _archive(self, symbol="AAA", session="2026-09-02", horizon=3):
        self.store.archive_signal(_metrics(symbol, session), True, None)
        with self.store._connection() as conn:
            conn.execute("UPDATE signal_archive SET horizon_sessions = ?", (horizon,))

    def test_might_be_finished_needs_enough_calendar_days_and_a_valid_date(self):
        row = {"signal_session": "2026-09-02", "horizon_sessions": 5}
        self.assertFalse(nb_forward._might_be_finished(row, datetime(2026, 9, 6)))
        self.assertTrue(nb_forward._might_be_finished(row, datetime(2026, 9, 8)))
        self.assertFalse(nb_forward._might_be_finished({**row, "signal_session": "garbage"}, datetime(2026, 9, 8)))

    def test_unfinished_signals_stay_pending_and_are_not_counted_as_failures(self):
        self._archive(session="2026-09-08")
        short = _bars("2026-09-01", [10] * 6, [10] * 6)
        with patch("newsbot.market_data._fetch_ticker_history", return_value=short):
            self.assertEqual(nb_forward.settle_pending(datetime(2026, 10, 3)), (0, 0))
        self.assertIsNone(self.store.archive_rows()[0]["outcome_json"])

    def test_history_fetch_failure_leaves_the_row_pending_and_counts_it(self):
        self._archive()
        with patch("newsbot.market_data._fetch_ticker_history", side_effect=RuntimeError("yahoo")), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            self.assertEqual(nb_forward.settle_pending(datetime(2026, 10, 3)), (0, 1))
        self.assertIn("forward_history_failed", "\n".join(logs.output))

    def test_a_symbol_is_fetched_once_for_all_its_rows(self):
        self._archive(session="2026-09-02")
        self.store.archive_signal(_metrics("AAA", "2026-09-03"), True, None)
        up = _bars("2026-09-01", [10, 10, 11, 12, 13, 14, 15], [10, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5])
        with self.store._connection() as conn:
            conn.execute("UPDATE signal_archive SET horizon_sessions = 2")
        with patch("newsbot.market_data._fetch_ticker_history", return_value=up) as fetch:
            settled, failed = nb_forward.settle_pending(datetime(2026, 10, 3))
        self.assertEqual((settled, failed), (2, 0))
        self.assertEqual(fetch.call_count, 2)  # AAA and its benchmark, not once per row

    def test_rows_without_a_benchmark_still_settle(self):
        self._archive(symbol="ZZZ.XX")  # no suffix mapping -> no benchmark
        up = _bars("2026-09-01", [10, 10, 11, 12, 13, 14], [10, 10.5, 11.5, 12.5, 13.5, 14.5])
        with patch("newsbot.market_data._fetch_ticker_history", return_value=up):
            self.assertEqual(nb_forward.settle_pending(datetime(2026, 10, 3)), (1, 0))
        self.assertIsNone(json.loads(self.store.archive_rows()[0]["outcome_json"])["benchmark_return_pct"])


class RenderingTests(unittest.TestCase):
    def test_delta_line_shapes(self):
        empty = {"dates": 0}
        self.assertIn("no dates with both groups yet", nb_forward._delta_line("Qualified", empty))
        one_date = {"dates": 1, "mean_delta_pp": 0.5, "se_pp": None, "better_pct": 100.0, "t": None}
        self.assertEqual(nb_forward._delta_line("Q", one_date), "    Q: +0.50 pp over 1 dates")
        flat = {"dates": 3, "mean_delta_pp": 0.0, "se_pp": 0.0, "better_pct": 0.0, "t": None}
        self.assertNotIn(" t ", nb_forward._delta_line("Q", flat))
        full = {"dates": 70, "mean_delta_pp": -0.3, "se_pp": 0.12, "better_pct": 40.0, "t": -2.5}
        text = nb_forward._delta_line("Q", full)
        self.assertIn("-0.30 pp over 70 dates", text)
        self.assertIn("±0.12 se", text)
        self.assertIn("t -2.5", text)

    def test_cohort_line_without_outcomes_or_benchmarks(self):
        self.assertIn("no completed signals yet", nb_forward._cohort_line("X", []))
        rows = [{"outcome": {"net_return_pct": 1.0, "excess_return_pct": None}},
                {"outcome": {"net_return_pct": -3.0, "excess_return_pct": None}}]
        line = nb_forward._cohort_line("X", rows)
        self.assertIn("2 signals", line)
        self.assertIn("avg net -1.00%", line)
        self.assertIn("hit rate 50%", line)
        self.assertNotIn("matched", line)

    def test_paired_with_a_single_date_has_no_standard_error(self):
        rows = [{"signal_session": "d", "outcome": {"net_return_pct": 1.0}}]
        base = [{"signal_session": "d", "outcome": {"net_return_pct": 0.0}}]
        result = nb_forward.paired_vs_baseline(rows, base)
        self.assertEqual((result["dates"], result["se_pp"], result["t"]), (1, None, None))

    def test_identical_deltas_have_no_t_value(self):
        rows = [{"signal_session": f"d{i}", "outcome": {"net_return_pct": 1.0}} for i in range(3)]
        base = [{"signal_session": f"d{i}", "outcome": {"net_return_pct": 0.0}} for i in range(3)]
        result = nb_forward.paired_vs_baseline(rows, base)
        self.assertEqual(result["mean_delta_pp"], 1.0)
        self.assertIsNone(result["t"])


class ReportEdgeTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        patcher = patch.object(nb_history_store, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_archive_failure_is_reported_not_raised(self):
        with patch.object(nb_forward, "settle_pending", side_effect=RuntimeError("db gone")):
            report = nb_forward.build_forward_test_report()
        self.assertIn("Signal archive unavailable", report)

    def test_enough_dates_switches_the_verdict_wording(self):
        # 60+ signal dates, each with one qualified and one rejected symbol, all settled.
        with self.store._connection() as conn:
            for i in range(65):
                date = f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
                for symbol, qualified, ret in (("AAA", 1, 2.0), ("BBB", 0, 1.0)):
                    conn.execute(
                        """INSERT INTO signal_archive (symbol, signal_session, qualified, rank, failed_gate,
                           last_close, day_change_pct, week_momentum_pct, volume_ratio, atr_pct,
                           drawdown_pct, rsi_14, rule_id, horizon_sessions, cost_bps_per_side,
                           benchmark_symbol, recorded_at, outcome_json)
                           VALUES (?, ?, ?, ?, NULL, 1, 0, 0, NULL, 1, 0, NULL, ?, 5, 10, NULL, 'x', ?)""",
                        (symbol, date, qualified, 1 if qualified else None,
                         self.store.current_rule_id(),
                         json.dumps({"net_return_pct": ret, "gross_return_pct": ret,
                                     "benchmark_return_pct": None, "excess_return_pct": None})),
                    )
        with patch.object(nb_forward, "settle_pending", return_value=(0, 0)):
            report = nb_forward.build_forward_test_report()
        self.assertIn("Judge the paired difference against its standard error", report)
        self.assertNotIn("Not enough data", report)

    def test_unavailable_count_is_shown(self):
        self.store.archive_signal(_metrics(), True, None)
        with patch.object(nb_forward, "settle_pending", return_value=(0, 2)):
            report = nb_forward.build_forward_test_report()
        self.assertIn("2 unavailable", report)


if __name__ == "__main__":
    unittest.main()
