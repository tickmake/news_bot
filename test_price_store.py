"""price_store: our own copy of daily prices."""

import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from newsbot import history_store as nb_history_store
from newsbot import price_store


def _bars(dates, closes):
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="Europe/Oslo") for d in dates])
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes,
                         "Adj Close": [c * 0.9 for c in closes], "Volume": [1000] * len(closes)}, index=index)


class PriceStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        patcher = patch.object(price_store, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.periods = []

    def _fake_fetch(self, data):
        def fetch(symbol, period):
            self.periods.append((symbol, period))
            if symbol not in data:
                raise RuntimeError("no data")
            return data[symbol], "NOK"
        return patch.object(price_store, "_fetch", side_effect=fetch)

    def _rows(self):
        return self.store._connection().execute(
            "SELECT symbol, session_date, close, adj_close, currency FROM daily_prices ORDER BY 1, 2").fetchall()

    def test_first_load_takes_two_years_then_only_catches_up(self):
        with self._fake_fetch({"EQNR.OL": _bars(["2026-11-09", "2026-11-10"], [380.0, 382.0])}):
            summary = price_store.load_prices(["EQNR.OL"])
        self.assertEqual(summary["new_rows"], 2)
        self.assertEqual(self.periods, [("EQNR.OL", "2y")])
        with self._fake_fetch({"EQNR.OL": _bars(["2026-11-10", "2026-11-11"], [999.0, 385.0])}):
            summary = price_store.load_prices(["EQNR.OL"])
        self.assertEqual(self.periods[-1], ("EQNR.OL", "1mo"))
        self.assertEqual(summary["new_rows"], 1)
        rows = self._rows()
        # A saved bar is never rewritten, even if Yahoo later says otherwise.
        self.assertEqual(rows[1][:3], ("EQNR.OL", "2026-11-10", 382.0))
        self.assertEqual(rows[2][2:], (385.0, 385.0 * 0.9, "NOK"))

    def test_failures_and_quiet_symbols_are_reported(self):
        with self._fake_fetch({"EQNR.OL": _bars(["2026-11-10"], [380.0])}):
            price_store.load_prices(["EQNR.OL"])
            summary = price_store.load_prices(["EQNR.OL", "GONE.OL"])
        self.assertEqual(summary["failed"], ["GONE.OL"])
        self.assertEqual(summary["no_new_price"], ["EQNR.OL"])

    def test_bad_bars_are_skipped(self):
        bars = _bars(["2026-11-09", "2026-11-10"], [380.0, float("nan")])
        self.assertEqual(price_store.save_prices("EQNR.OL", bars, "NOK"), 1)

    def test_job_never_raises(self):
        with patch.object(price_store, "load_prices", side_effect=RuntimeError("x")):
            self.assertEqual(price_store.job_load_prices(), {})


    def test_previous_ticker_fills_the_history_before_the_new_ticker(self):
        old = _bars(["2026-11-04", "2026-11-05", "2026-11-10"], [58.0, 58.5, 999.0])
        new = _bars(["2026-11-10", "2026-11-11"], [62.0, 63.0])
        with patch.object(price_store.stock_list, "previous_tickers", return_value={"CDLR.OL": "CADLR.OL"}), \
                self._fake_fetch({"CDLR.OL": new, "CADLR.OL": old}):
            summary = price_store.load_prices(["CDLR.OL"])
            self.assertEqual(summary["new_rows"], 4)
            # The new ticker's own bar wins over the old ticker's on the same day.
            self.assertEqual([(r[1], r[2]) for r in self._rows()], [
                ("2026-11-04", 58.0), ("2026-11-05", 58.5), ("2026-11-10", 62.0), ("2026-11-11", 63.0)])
            self.assertTrue(all(r[0] == "CDLR.OL" for r in self._rows()))

    def test_previous_ticker_is_skipped_once_the_history_is_long(self):
        with patch.object(price_store, "SHORT_HISTORY_SESSIONS", 2), \
                patch.object(price_store.stock_list, "previous_tickers", return_value={"CDLR.OL": "CADLR.OL"}), \
                self._fake_fetch({"CDLR.OL": _bars(["2026-11-10", "2026-11-11"], [62.0, 63.0])}):
            price_store.load_prices(["CDLR.OL"])
        self.assertNotIn(("CADLR.OL", "2y"), self.periods)

    def test_a_missing_previous_ticker_does_not_fail_the_load(self):
        with patch.object(price_store.stock_list, "previous_tickers", return_value={"CDLR.OL": "CADLR.OL"}), \
                self._fake_fetch({"CDLR.OL": _bars(["2026-11-11"], [63.0])}):
            summary = price_store.load_prices(["CDLR.OL", "EQNR.OL"])
        self.assertEqual((summary["new_rows"], summary["failed"]), (1, ["EQNR.OL"]))
        self.assertIn(("CADLR.OL", "2y"), self.periods)


if __name__ == "__main__":
    unittest.main()
