"""score_inputs: what a score may use on a date, and nothing it may not (#145)."""

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from newsbot import company_numbers as cn
from newsbot import history_store as nb_history_store
from newsbot import price_store
from newsbot import score_inputs as si

YEARS = (  # year end, public from, net profit
    ("2022-12-31", "2023-04-30", 28.0),
    ("2023-12-31", "2024-04-30", 12.0),
    ("2024-12-31", "2025-04-30", 9.0),
)


class ScoreInputTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        for module in (cn, price_store):
            patcher = patch.object(module, "TRADE_HISTORY", self.store)
            patcher.start()
            self.addCleanup(patcher.stop)
        price_store._connection()
        conn = cn._connection()
        with conn:
            for year_end, known, profit in YEARS:
                conn.executemany(
                    "INSERT INTO company_numbers VALUES ('EQNR.OL', ?, ?, ?, 'USD', 'esef', ?, 'x')",
                    [(year_end, "net_profit", profit, known), (year_end, "shares_outstanding", 2.5, known)])
            # One price a day, rising by 1 NOK, from 2024-11-11.
            first = date(2024, 11, 11)
            conn.executemany(
                "INSERT INTO daily_prices (symbol, session_date, close, currency, fetched_at) "
                "VALUES ('EQNR.OL', ?, ?, 'NOK', 'x')",
                [((first + timedelta(days=n)).isoformat(), 100.0 + n) for n in range(450)])
        days = [date(2024, 1, 1) + timedelta(days=n) for n in range(1000)]
        self.fx = lambda pair: pd.Series([10.0 if d < date(2025, 6, 1) else 11.0 for d in days], index=days)

    def test_a_number_is_never_used_before_it_was_public(self):
        for day in si.month_ends(date(2024, 11, 30), date(2026, 1, 31)):
            row = si.inputs_for("EQNR.OL", day, fx_closes=self.fx)
            for year_end, known, _ in YEARS:
                if date.fromisoformat(known) > day:
                    self.assertNotEqual(row["year_end"], year_end, day)
                    self.assertNotEqual(row["previous_year_end"], year_end, day)

    def test_the_day_before_and_the_day_a_report_becomes_public(self):
        before = si.inputs_for("EQNR.OL", date(2025, 4, 29), fx_closes=self.fx)
        self.assertEqual((before["year_end"], before["numbers"]["net_profit"]), ("2023-12-31", 12.0))
        self.assertEqual(before["previous"]["net_profit"], 28.0)
        on = si.inputs_for("EQNR.OL", date(2025, 4, 30), fx_closes=self.fx)
        self.assertEqual((on["year_end"], on["numbers"]["net_profit"]), ("2024-12-31", 9.0))

    def test_prices_a_month_and_a_year_back_and_the_exchange_rate(self):
        row = si.inputs_for("EQNR.OL", date(2025, 12, 31), fx_closes=self.fx)
        self.assertEqual(row["close"], 100.0 + (date(2025, 12, 31) - date(2024, 11, 11)).days)
        self.assertEqual(row["close"] - row["close_1m"], 30.0)
        self.assertEqual(row["close"] - row["close_12m"], 365.0)
        self.assertEqual((row["numbers_currency"], row["price_currency"], row["fx"]), ("USD", "NOK", 11.0))

    def test_no_price_from_more_than_a_week_before(self):
        row = si.inputs_for("EQNR.OL", date(2024, 11, 30), fx_closes=self.fx)
        self.assertIsNone(row["close_12m"])
        self.assertIsNone(row["close_1m"])   # 2024-10-31 is before the first saved price
        self.assertIsNotNone(row["close"])

    def test_build_writes_every_finished_month_and_can_run_again(self):
        today = date(2025, 3, 15)
        # From the first month anything was known (the 2022 report, public
        # 2023-04-30) to the last finished month; months with nothing are skipped.
        self.assertEqual(si.build(["EQNR.OL"], today, self.fx), 23)
        self.assertEqual(si.build(["EQNR.OL"], today, self.fx), 23)
        with price_store.TRADE_HISTORY_LOCK:
            rows = si._connection().execute(
                "SELECT month_end, year_end, numbers_json FROM score_inputs ORDER BY month_end").fetchall()
        self.assertEqual((rows[0][0], rows[-1][0]), ("2023-04-30", "2025-02-28"))
        self.assertEqual(json.loads(rows[-1][2])["net_profit"], 12.0)

    def test_an_old_exchange_rate_is_not_used(self):
        stale = lambda pair: pd.Series([10.0], index=[date(2025, 1, 1)])  # noqa: E731
        self.assertIsNone(si.inputs_for("EQNR.OL", date(2025, 12, 31), fx_closes=stale)["fx"])

    def test_month_ends(self):
        self.assertEqual(si.month_ends(date(2024, 11, 30), date(2025, 2, 27)),
                         [date(2024, 11, 30), date(2024, 12, 31), date(2025, 1, 31)])

    def test_job_never_raises(self):
        with patch.object(si, "build", side_effect=RuntimeError("x")):
            self.assertEqual(si.job_build_score_inputs(), 0)


if __name__ == "__main__":
    unittest.main()
