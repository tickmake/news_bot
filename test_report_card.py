"""report_card: the /analyze report card built from saved company numbers (#124)."""

import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from newsbot import company_numbers as cn
from newsbot import history_store as nb_history_store
from newsbot import report_card as rc

GOOD = {"net_profit": 10.0, "operating_cash_flow": 15.0, "total_assets": 100.0, "total_debt": 20.0,
        "cash": 5.0, "operating_profit": 12.0, "current_assets": 30.0, "current_liabilities": 20.0,
        "shares_outstanding": 50.0, "gross_profit": 40.0, "revenue": 80.0}
WEAKER = {"net_profit": 8.0, "operating_cash_flow": 9.0, "total_assets": 100.0, "total_debt": 30.0,
          "cash": 5.0, "operating_profit": 10.0, "current_assets": 25.0, "current_liabilities": 20.0,
          "shares_outstanding": 55.0, "gross_profit": 30.0, "revenue": 75.0}


class HealthCheckTests(unittest.TestCase):
    def test_a_year_better_on_every_count_scores_9(self):
        checks = rc.health_checks(GOOD, WEAKER)
        self.assertEqual(len(checks), 9)
        self.assertTrue(all(c.passed for c in checks), [c.name for c in checks if not c.passed])

    def test_a_year_worse_on_every_count_scores_low(self):
        checks = rc.health_checks(WEAKER, GOOD)
        failed = {c.name for c in checks if c.passed is False}
        self.assertIn("Debt falling", failed)
        self.assertIn("No new shares", failed)
        self.assertIn("Gross margin rising", failed)

    def test_missing_numbers_are_not_available_never_guessed(self):
        thin = {"net_profit": 10.0, "operating_cash_flow": 15.0}
        checks = rc.health_checks(thin, {"net_profit": 8.0})
        by_name = {c.name: c.passed for c in checks}
        self.assertTrue(by_name["Made a profit"])
        self.assertTrue(by_name["Cash flow bigger than profit"])
        self.assertIsNone(by_name["Return on assets rising"])
        self.assertIsNone(by_name["Gross margin rising"])
        self.assertIsNone(by_name["No new shares"])

    def test_debt_in_years_of_profit(self):
        self.assertAlmostEqual(rc.debt_years(GOOD), 15.0 / 12.0)
        self.assertIsNone(rc.debt_years({**GOOD, "operating_profit": -1.0}))
        self.assertLess(rc.debt_years({**GOOD, "cash": 50.0}), 0)


class ReportCardTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        patcher = patch.object(cn, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        conn = cn._connection()
        with conn:
            for year_end, known, numbers in (("2024-12-31", "2025-03-01", GOOD),
                                             ("2023-12-31", "2024-03-01", WEAKER),
                                             ("2022-12-31", "2023-03-01", WEAKER)):
                conn.executemany(
                    "INSERT INTO company_numbers VALUES ('EQNR.OL', ?, ?, ?, 'USD', 'yahoo', ?, 'x')",
                    [(year_end, item, value, known) for item, value in numbers.items()])
            conn.execute("CREATE TABLE stock_snapshots (symbol TEXT, snapshot_date TEXT, "
                         "fetched_at TEXT, data_json TEXT)")
            for symbol, pe in (("EQNR.OL", 8.0), ("SHEL", 12.0), ("XOM", 14.0), ("BP", 10.0)):
                info = {"sector": "Energy", "trailingPE": pe, "priceToBook": 2.0}
                conn.execute("INSERT INTO stock_snapshots VALUES (?, '2025-06-01', 'x', ?)",
                             (symbol, json.dumps({"info": info})))

    def test_card_shows_score_checks_debt_and_sector(self):
        text = "\n".join(rc.build_report_card("EQNR.OL", as_of=date(2025, 7, 1)))
        self.assertIn("Health check: 9 of 9", text)
        self.assertIn("year to 2024-12-31", text)
        self.assertIn("about 1.2 years of operating profit", text)
        self.assertIn("P/E: 8.0; sector 11.0 (middle of 4 stocks", text)
        for word in ("buy", "sell"):
            self.assertNotIn(word, text.lower())

    def test_only_numbers_known_on_the_date_are_used(self):
        # Before the 2024 accounts were public, the card compares 2023 with 2022.
        text = "\n".join(rc.build_report_card("EQNR.OL", as_of=date(2025, 1, 15)))
        self.assertIn("year to 2023-12-31", text)
        self.assertIn("Price vs sector:</b> not available", text)   # no snapshot yet

    def test_unknown_stock_says_not_available(self):
        text = "\n".join(rc.build_report_card("NOPE", as_of=date(2025, 7, 1)))
        self.assertIn("fewer than two years", text)
        self.assertIn("not available", text)


if __name__ == "__main__":
    unittest.main()
