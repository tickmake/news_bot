"""data_checks: a broken nightly load produces one Slack warning."""

import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from newsbot import data_checks, messaging, price_store
from newsbot import history_store as nb_history_store
from newsbot import state as nb_state

LISTED = [
    {"ticker": "EQNR.OL", "region": "Oslo", "currency": "NOK"},
    {"ticker": "DNB.OL", "region": "Oslo", "currency": "NOK"},
    {"ticker": "SPLIT.OL", "region": "Oslo", "currency": "NOK"},
    {"ticker": "CUR.OL", "region": "Oslo", "currency": "NOK"},
]


def _bars(closes, start="2026-11-09"):
    index = pd.DatetimeIndex([pd.Timestamp(start, tz="Europe/Oslo") + pd.Timedelta(days=i) for i in range(len(closes))])
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes,
                         "Adj Close": closes, "Volume": [1] * len(closes)}, index=index)


class DataCheckTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        for p in (
            patch.object(price_store, "TRADE_HISTORY", self.store),
            patch.object(data_checks.stock_list, "load", return_value=LISTED),
            patch.object(messaging, "send_slack_message", return_value=True),
            patch.object(nb_state.STATE, "save"),
            patch.dict(nb_state.STATE.data, {"data_warnings_sent": []}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _load(self, failed=()):
        price_store.save_prices("EQNR.OL", _bars([380.0, 382.0]), "NOK")
        price_store.save_prices("DNB.OL", _bars([250.0]), "NOK")              # one day behind
        price_store.save_prices("SPLIT.OL", _bars([100.0, 10.0]), "NOK")      # a 10:1 split
        price_store.save_prices("CUR.OL", _bars([50.0]), "NOK")
        price_store.save_prices("CUR.OL", _bars([5.0], start="2026-11-10"), "EUR")
        return {"new_rows": 7, "failed": list(failed), "no_new_price": []}

    def test_broken_load_produces_one_slack_warning(self):
        summary = self._load(failed=["GONE.OL"])
        with patch.object(price_store, "job_load_prices", return_value=summary):
            self.assertTrue(data_checks.job_load_prices_and_check())
        messaging.send_slack_message.assert_called_once()
        text = messaging.send_slack_message.call_args.args[0]
        self.assertIn("GONE.OL: prices could not be loaded", text)
        self.assertIn("DNB.OL: no new price since 2026-11-09", text)
        self.assertIn("SPLIT.OL: price moved -90% in one day", text)
        self.assertIn("CUR.OL: currency changed from NOK to EUR", text)
        self.assertIn("CUR.OL: prices are in EUR, the stock list says NOK", text)
        self.assertNotIn("EQNR.OL", text)
        # Nothing new the next night: no second message.
        with patch.object(price_store, "job_load_prices", return_value=summary):
            data_checks.job_load_prices_and_check()
        messaging.send_slack_message.assert_called_once()

    def test_clean_load_is_quiet(self):
        price_store.save_prices("EQNR.OL", _bars([380.0, 382.0]), "NOK")
        listed = [LISTED[0]]
        self.assertEqual(data_checks.price_problems({"failed": []}, listed), [])

    def test_crashed_loads_warn(self):
        with patch.object(price_store, "job_load_prices", return_value={}):
            data_checks.job_load_prices_and_check()
        self.assertIn("price load stopped", messaging.send_slack_message.call_args.args[0])
        with patch.object(data_checks.company_numbers, "job_load_company_numbers",
                          return_value={"saved": 3, "failed": ["SAP.DE"]}):
            data_checks.job_load_company_numbers_and_check()
        self.assertIn("SAP.DE: company numbers could not be loaded", messaging.send_slack_message.call_args.args[0])

    def test_unchecked_esef_dates_warn(self):
        with patch.object(data_checks.company_numbers, "job_load_company_numbers",
                          return_value={"saved": 0, "failed": [], "esef_dates_unchecked": 7}):
            data_checks.job_load_company_numbers_and_check()
        self.assertIn("7 annual-report numbers could not be matched", messaging.send_slack_message.call_args.args[0])

    def test_failed_slack_send_is_retried(self):
        messaging.send_slack_message.return_value = False
        self.assertEqual(data_checks.warn("t", [("k", "x")]), 0)
        messaging.send_slack_message.return_value = True
        self.assertEqual(data_checks.warn("t", [("k", "x")]), 1)


if __name__ == "__main__":
    unittest.main()
