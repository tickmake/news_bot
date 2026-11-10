"""snapshots: the nightly copy of analyst estimates and company numbers."""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from newsbot import history_store as nb_history_store
from newsbot import snapshots


def _ticker(empty=False):
    ticker = MagicMock()
    ticker.earnings_estimate = None if empty else pd.DataFrame(
        {"avg": [1.8, float("nan")], "numberOfAnalysts": [6, 0]}, index=["0q", "+1q"])
    ticker.revenue_estimate = None
    return ticker


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        for p in (
            patch.object(snapshots, "TRADE_HISTORY", self.store),
            patch.object(snapshots, "NORWAY_STOCK_UNIVERSE", {"Equinor": "EQNR.OL"}),
            patch.object(snapshots, "EU_STOCK_UNIVERSE", {"SAP": "SAP.DE"}),
            patch.object(snapshots, "USA_STOCK_UNIVERSE", {}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _rows(self):
        return self.store._connection().execute(
            "SELECT symbol, data_json FROM stock_snapshots ORDER BY symbol").fetchall()

    def test_symbols_are_configured_first_then_tracked_without_duplicates(self):
        with patch.object(self.store, "recently_tracked_symbols", return_value=["AAPL", "EQNR.OL"]), \
                patch.object(snapshots.SETTINGS, "snapshot_max_symbols", 3):
            self.assertEqual(snapshots.snapshot_symbols(), ["EQNR.OL", "SAP.DE", "AAPL"])

    def test_one_row_per_stock_nan_saved_as_null_and_failures_counted(self):
        info = {"EQNR.OL": {"targetMeanPrice": 408.4, "trailingPE": float("nan"), "junk": 1}, "SAP.DE": {}}
        with patch.object(snapshots.market_data, "_fetch_ticker_fundamentals", side_effect=info.get), \
                patch.object(snapshots.yf, "Ticker", side_effect=lambda s: _ticker(empty=s == "SAP.DE")):
            self.assertEqual(snapshots.save_snapshots(), (1, 1))
            # A rerun the same day replaces the row instead of adding one.
            self.assertEqual(snapshots.save_snapshots(), (1, 1))
        rows = self._rows()
        self.assertEqual([row[0] for row in rows], ["EQNR.OL"])
        data = json.loads(rows[0][1])
        self.assertEqual(data["info"]["targetMeanPrice"], 408.4)
        self.assertIsNone(data["info"]["trailingPE"])
        self.assertNotIn("junk", data["info"])
        self.assertEqual(data["earnings_estimate"]["0q"]["numberOfAnalysts"], 6)
        self.assertIsNone(data["earnings_estimate"]["+1q"]["avg"])

    def test_job_never_raises(self):
        with patch.object(snapshots, "save_snapshots", side_effect=RuntimeError("x")):
            self.assertFalse(snapshots.job_save_snapshots())
        with patch.object(snapshots, "save_snapshots", return_value=(5, 0)):
            self.assertTrue(snapshots.job_save_snapshots())


if __name__ == "__main__":
    unittest.main()
