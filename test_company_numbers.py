"""company_numbers: annual-report numbers with the date they became known."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from newsbot import company_numbers as cn
from newsbot import history_store as nb_history_store


def _fact(concept, period, value, unit="iso4217:USD", **extra):
    return {"value": str(value), "dimensions": {"concept": concept, "entity": "x", "period": period, "unit": unit, **extra}}


YEAR = "2024-01-01T00:00:00/2025-01-01T00:00:00"
DOC = {"facts": {
    "a": _fact("ifrs-full:Revenue", YEAR, 103.77e9),
    "b": _fact("ifrs-full:Revenue", "2023-01-01T00:00:00/2024-01-01T00:00:00", 107e9),   # comparative year
    "c": _fact("ifrs-full:Revenue", YEAR, 50e9, **{"ifrs-full:SegmentsAxis": "x"}),       # a segment, not the total
    "d": _fact("ifrs-full:Revenue", "2024-10-01T00:00:00/2025-01-01T00:00:00", 25e9),    # a quarter
    "e": _fact("ifrs-full:Equity", "2025-01-01T00:00:00", 42.38e9),
    "f": _fact("ifrs-full:ProfitLoss", YEAR, 8.83e9),
}}


class EsefTests(unittest.TestCase):
    def test_only_the_whole_group_for_the_filings_own_year(self):
        values, year_end = cn.esef_values(DOC, "2024-12-31")
        self.assertEqual(year_end, "2024-12-31")
        self.assertEqual(values["revenue"], (103.77e9, "USD"))
        self.assertEqual(values["equity"], (42.38e9, "USD"))
        self.assertEqual(values["net_profit"], (8.83e9, "USD"))
        self.assertNotIn("cash", values)

    def test_shareholders_part_beats_the_total_with_minority_owners(self):
        doc = {"facts": {**DOC["facts"],
                         "g": _fact("ifrs-full:ProfitLossAttributableToOwnersOfParent", YEAR, 7.09e9),
                         "h": _fact("ifrs-full:EquityAttributableToOwnersOfParent", "2025-01-01T00:00:00", 33.7e9)}}
        values, _ = cn.esef_values(doc, "2024-12-31")
        self.assertEqual(values["net_profit"], (7.09e9, "USD"))
        self.assertEqual(values["equity"], (33.7e9, "USD"))


class LoadTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(cn.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        patcher = patch.object(cn, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.downloads = 0
        self.shares = float("nan")

    def _get(self, url):
        if "gleif" in url:
            return {"data": [{"id": "LEI1"}]}
        if url.endswith("/filings?page%5Bsize%5D=50"):
            return {"data": [{"id": "f1", "attributes": {"period_end": "2024-12-31", "json_url": "/r.json",
                                                         "date_added": "2025-05-08 11:12:47"}}]}
        self.downloads += 1
        return DOC

    def _yahoo(self):
        ticker = MagicMock()
        ticker.info = {"financialCurrency": "USD"}
        column = pd.Timestamp("2024-12-31")
        ticker.income_stmt = pd.DataFrame({column: [1.0, 2.0, 3.0]}, index=["Total Revenue", "Operating Income", "Net Income"])
        ticker.balance_sheet = pd.DataFrame({column: [30.1e9, self.shares]}, index=["Total Debt", "Ordinary Shares Number"])
        ticker.cashflow = pd.DataFrame()
        return ticker

    def _rows(self):
        return {(r[2]): (r[3], r[5], r[6]) for r in self.store._connection().execute(
            "SELECT * FROM company_numbers ORDER BY item")}

    def test_esef_first_yahoo_fills_gaps_and_filings_are_read_once(self):
        row = {"ticker": "EQNR.OL", "isin": "NO0010096985", "region": "Oslo"}
        with patch.object(cn, "_get_json", side_effect=self._get), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            self.assertEqual(cn.load_company_numbers([row]), {"saved": 5, "failed": []})
            cn.load_company_numbers([row])
        self.assertEqual(self.downloads, 1)
        rows = self._rows()
        self.assertEqual(rows["revenue"], (103.77e9, "esef", "2025-05-08"))
        self.assertEqual(rows["operating_profit"], (2.0, "yahoo", "2025-04-30"))
        self.assertEqual(rows["total_debt"], (30.1e9, "yahoo", "2025-04-30"))
        self.assertNotIn("shares_outstanding", rows)

    def test_share_counts_have_no_currency(self):
        self.shares = 2.6e9
        with patch.object(cn, "_get_json", side_effect=cn.NotInEsef("404")), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            cn.load_company_numbers([{"ticker": "IFX.DE", "isin": "DE1", "region": "EU"}])
        row = self.store._connection().execute(
            "SELECT value, currency FROM company_numbers WHERE item = 'shares_outstanding'").fetchone()
        self.assertEqual(row, (2.6e9, None))
        revenue = self.store._connection().execute(
            "SELECT currency FROM company_numbers WHERE item = 'revenue'").fetchone()
        self.assertEqual(revenue, ("USD",))

    def test_version_2_redoes_profit_and_equity_once(self):
        conn = self.store._connection()
        for table in cn._TABLES[:2]:
            conn.execute(table)
        with conn:   # a version-1 database, before #111
            conn.executemany("INSERT INTO company_numbers VALUES (?, '2024-12-31', ?, 1.0, ?, ?, '2025-03-06', 'x')", [
                ("ACA.PA", "net_profit", "EUR", "esef"), ("ACA.PA", "equity", "EUR", "esef"),
                ("ACA.PA", "revenue", "EUR", "esef"), ("IFX.DE", "net_profit", "EUR", "yahoo"),
                ("IFX.DE", "shares_outstanding", "EUR", "yahoo")])
            conn.execute("INSERT INTO esef_filings_read VALUES ('f1', 'ACA.PA')")
        conn = cn._connection()
        left = sorted(conn.execute("SELECT symbol, item, currency FROM company_numbers").fetchall())
        self.assertEqual(left, [("ACA.PA", "revenue", "EUR"), ("IFX.DE", "net_profit", "EUR"),
                                ("IFX.DE", "shares_outstanding", None)])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM esef_filings_read").fetchone(), (0,))
        # Once migrated, new rows are left alone.
        with conn:
            conn.execute("INSERT INTO company_numbers VALUES ('ACA.PA', '2024-12-31', 'equity', 2.0, 'EUR', 'esef', 'd', 'x')")
            conn.execute("INSERT INTO esef_filings_read VALUES ('f1', 'ACA.PA')")
        conn = cn._connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_numbers WHERE item = 'equity'").fetchone(), (1,))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM esef_filings_read").fetchone(), (1,))

    def test_us_stocks_skip_esef_and_failures_are_listed(self):
        with patch.object(cn, "_get_json", side_effect=AssertionError("no ESEF for US")), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            summary = cn.load_company_numbers([{"ticker": "AAPL", "isin": "US0378331005", "region": "US"}])
        self.assertEqual(summary["failed"], [])
        # ESEF down but Yahoo fine: not a failure, Yahoo still saves.
        with patch.object(cn, "_get_json", side_effect=RuntimeError("down")), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            summary = cn.load_company_numbers([{"ticker": "EQNR.OL", "isin": "NO1", "region": "Oslo"}])
        self.assertEqual(summary["failed"], [])
        with patch.object(cn, "_get_json", side_effect=RuntimeError("down")), \
                patch.object(cn.yf, "Ticker", side_effect=RuntimeError("down too")):
            summary = cn.load_company_numbers([{"ticker": "EQNR.OL", "isin": "NO1", "region": "Oslo"}])
        self.assertEqual(summary["failed"], ["EQNR.OL"])

    def test_not_in_esef_falls_back_to_yahoo_quietly(self):
        with patch.object(cn, "_get_json", side_effect=cn.NotInEsef("404")), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            summary = cn.load_company_numbers([{"ticker": "IFX.DE", "isin": "DE1", "region": "EU"}])
        self.assertEqual(summary, {"saved": 4, "failed": []})

    def test_rate_limit_waits_and_retries(self):
        responses = [MagicMock(status_code=429), MagicMock(status_code=429), MagicMock(status_code=200)]
        responses[2].json.return_value = {"ok": 1}
        with patch.object(cn.requests, "get", side_effect=responses):
            self.assertEqual(cn._get_json("u"), {"ok": 1})
        self.assertIn(unittest.mock.call(10), self.sleep.call_args_list)
        self.assertIn(unittest.mock.call(30), self.sleep.call_args_list)
        with patch.object(cn.requests, "get", return_value=MagicMock(status_code=404)):
            with self.assertRaises(cn.NotInEsef):
                cn._get_json("u")

    def test_job_never_raises(self):
        with patch.object(cn, "load_company_numbers", side_effect=RuntimeError("x")):
            self.assertEqual(cn.job_load_company_numbers(), {})


if __name__ == "__main__":
    unittest.main()
