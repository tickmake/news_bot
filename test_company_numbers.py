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


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        patcher = patch.object(cn, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.downloads = 0

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
        ticker.balance_sheet = pd.DataFrame({column: [30.1e9, float("nan")]}, index=["Total Debt", "Ordinary Shares Number"])
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

    def test_us_stocks_skip_esef_and_failures_are_listed(self):
        with patch.object(cn, "_get_json", side_effect=AssertionError("no ESEF for US")), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            summary = cn.load_company_numbers([{"ticker": "AAPL", "isin": "US0378331005", "region": "US"}])
        self.assertEqual(summary["failed"], [])
        with patch.object(cn, "_get_json", side_effect=RuntimeError("down")):
            summary = cn.load_company_numbers([{"ticker": "EQNR.OL", "isin": "NO1", "region": "Oslo"}])
        self.assertEqual(summary["failed"], ["EQNR.OL"])

    def test_job_never_raises(self):
        with patch.object(cn, "load_company_numbers", side_effect=RuntimeError("x")):
            self.assertEqual(cn.job_load_company_numbers(), {})


if __name__ == "__main__":
    unittest.main()
