"""scores: the four 0-100 company scores (#146-#151)."""

import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from newsbot import company_numbers as cn
from newsbot import history_store as nb_history_store
from newsbot import scores

NOW = {"net_profit": 10.0, "equity": 50.0, "operating_cash_flow": 15.0, "total_assets": 100.0,
       "total_debt": 20.0, "cash": 5.0, "operating_profit": 12.0, "current_assets": 30.0,
       "current_liabilities": 20.0, "shares_outstanding": 10.0, "gross_profit": 40.0, "revenue": 80.0}
BEFORE = {**NOW, "net_profit": 8.0, "revenue": 64.0, "total_debt": 30.0, "shares_outstanding": 11.0,
          "operating_cash_flow": 9.0, "gross_profit": 30.0}


def _row(**changes):
    row = {"close": 20.0, "close_1m": 22.0, "close_12m": 11.0, "fx": 2.0,
           "numbers": dict(NOW), "previous": dict(BEFORE)}
    row.update(changes)
    return row


class MetricTests(unittest.TestCase):
    def test_numbers_in_the_prices_currency(self):
        # Market value: 20 NOK x 10 shares / 2 NOK per USD = 100 USD.
        m = scores.metrics(_row(), "Energy")
        self.assertAlmostEqual(m["earnings_yield"], 10.0)
        self.assertAlmostEqual(m["book_yield"], 50.0)
        self.assertAlmostEqual(m["cash_flow_yield"], 15.0)
        self.assertAlmostEqual(m["return_on_equity"], 20.0)
        self.assertAlmostEqual(m["sales_growth"], 25.0)
        self.assertAlmostEqual(m["profit_growth"], 25.0)
        self.assertAlmostEqual(m["momentum_12_1"], 100.0)   # 11 -> 22, the last month left out
        self.assertAlmostEqual(m["debt_years"], 15.0 / 12.0)
        self.assertEqual(m["health"], 8.0)   # short-term bills: same ratio both years, not easier

    def test_a_loss_has_no_earnings_yield_and_no_growth_from_a_loss(self):
        m = scores.metrics(_row(numbers={**NOW, "net_profit": -5.0},
                                previous={**BEFORE, "net_profit": -1.0}), "Energy")
        self.assertIsNone(m["earnings_yield"])
        self.assertIsNone(m["profit_growth"])

    def test_banks_skip_the_debt_check_and_missing_numbers_stay_missing(self):
        self.assertIsNone(scores.metrics(_row(), "Financial Services")["debt_years"])
        m = scores.metrics(_row(fx=None, previous={}, close_12m=None), "Energy")
        for name in ("earnings_yield", "book_yield", "health", "sales_growth", "momentum_12_1"):
            self.assertIsNone(m[name], name)


class RankTests(unittest.TestCase):
    def test_percentiles_with_ties_and_direction(self):
        self.assertEqual(scores.percentiles({"a": 1, "b": 2, "c": 3}), {"a": 0, "b": 50, "c": 100})
        self.assertEqual(scores.percentiles({"a": 1, "b": 2, "c": 3}, higher_is_better=False),
                         {"a": 100, "b": 50, "c": 0})
        self.assertEqual(scores.percentiles({"a": 1, "b": 1, "c": 3}), {"a": 25, "b": 25, "c": 100})
        self.assertEqual(scores.percentiles({"a": 1, "b": 2}), {})

    def test_a_small_sector_is_ranked_against_its_region(self):
        raw = {f"S{i}": {"momentum_12_1": float(i)} for i in range(6)}
        groups = {f"S{i}": ("Energy" if i < 5 else "Utilities", "Oslo") for i in range(6)}
        result = scores.score_month(raw, groups)
        self.assertEqual(result["S4"]["momentum"], 100)      # best of 5 in Energy
        self.assertEqual(result["S5"]["momentum"], 100)      # best of all 6 in Oslo
        self.assertIn("Oslo (sector too small)", result["S5"]["peers"]["momentum"])
        self.assertIsNone(result["S0"]["quality"])           # nothing to rank: no score


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        patcher = patch.object(cn, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        listed = [{"ticker": f"S{i}.OL", "sector": "Energy", "region": "Oslo"} for i in range(5)]
        patcher = patch.object(scores.stock_list, "load", return_value=listed)
        patcher.start()
        self.addCleanup(patcher.stop)
        conn = scores._connection()
        with conn:
            for i in range(5):
                numbers = {**NOW, "net_profit": 5.0 + i}
                conn.execute(
                    "INSERT INTO score_inputs VALUES (?, '2026-10-31', '2026-10-30', 20, 22, ?, 'NOK', "
                    "'2025-12-31', ?, '2024-12-31', ?, 'USD', 2.0, 'x')",
                    (f"S{i}.OL", 11.0 + i, json.dumps(numbers), json.dumps(BEFORE)))

    def test_build_and_the_analyze_section(self):
        self.assertEqual(scores.build(), 5)
        best = scores.latest("S0.OL")
        self.assertEqual(best["month_end"], "2026-10-31")
        self.assertEqual(best["momentum"], 100)               # smallest 12-month base: biggest rise
        self.assertEqual(scores.latest("S4.OL")["value"] > best["value"], True)
        text = "\n".join(scores.build_scores_section("S0.OL"))
        self.assertIn("Momentum: <b>100/100</b> (Energy, 5 stocks)", text)
        for word in ("buy", "sell"):
            self.assertNotIn(word, text.lower())
        self.assertIn("no scores for this stock yet", "\n".join(scores.build_scores_section("X")))

    def test_scores_go_with_their_inputs(self):
        scores.build()
        conn = scores._connection()
        with conn:
            conn.execute("DELETE FROM score_inputs WHERE symbol = 'S4.OL'")
        self.assertEqual(scores.build(), 4)
        left = conn.execute("SELECT symbol FROM company_scores ORDER BY symbol").fetchall()
        self.assertEqual(left, [("S0.OL",), ("S1.OL",), ("S2.OL",), ("S3.OL",)])

    def test_estimate_change_from_snapshots(self):
        conn = scores._connection()
        with conn:
            conn.execute("CREATE TABLE stock_snapshots (symbol TEXT, snapshot_date TEXT, "
                         "fetched_at TEXT, data_json TEXT)")
            for day, eps in (("2026-10-20", 2.0), ("2026-11-10", 2.2)):
                conn.execute("INSERT INTO stock_snapshots VALUES ('S0.OL', ?, 'x', ?)",
                             (day, json.dumps({"info": {"forwardEps": eps}})))
        self.assertAlmostEqual(scores.estimate_change("S0.OL", date(2026, 11, 12)), 10.0)
        self.assertIsNone(scores.estimate_change("S1.OL", date(2026, 11, 12)))

    def test_job_never_raises(self):
        with patch.object(scores.score_inputs, "build", side_effect=RuntimeError("x")):
            self.assertEqual(scores.job_build_scores(), 0)


if __name__ == "__main__":
    unittest.main()
