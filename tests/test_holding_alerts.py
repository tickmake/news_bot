"""holding_alerts: the evening alert for stocks I own."""

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from newsbot import catalysts, fills, holding_alerts, market_data, messaging
from newsbot import history_store as nb_history_store
from newsbot import state as nb_state

TODAY = date(2026, 11, 10)


class HoldingAlertTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        self.price = {"EQNR.OL": 300.0}
        self.profile = {"sector": None, "next_earnings": None}
        self.headlines = {}
        for p in (
            patch.object(fills, "TRADE_HISTORY", self.store),
            patch.object(fills, "_latest_price", side_effect=self.price.get),
            patch.object(market_data, "_fetch_ticker_fundamentals", return_value={"longName": "Equinor ASA"}),
            patch.object(market_data, "_fetch_event_profile", side_effect=lambda s: self.profile),
            patch.object(catalysts, "find", side_effect=lambda symbols, names: self.headlines),
            patch.object(messaging, "send_telegram_message", return_value=True),
            patch.object(nb_state.STATE, "save"),
            patch.dict(nb_state.STATE.data, {"holding_alerts_sent": []}),
        ):
            p.start()
            self.addCleanup(p.stop)
        with patch.object(fills, "_close_on_or_before", return_value=300.0):
            fills.record_fill("BUY EQNR.OL 10 300 59")

    def _sent(self):
        return messaging.send_telegram_message.call_args.args[0]

    def test_quiet_day_sends_nothing(self):
        self.assertEqual(holding_alerts.send_holding_alerts(TODAY), 0)
        messaging.send_telegram_message.assert_not_called()

    def test_each_kind_is_sent_once(self):
        self.price["EQNR.OL"] = 240.0
        self.profile = {"next_earnings": TODAY + timedelta(days=3)}
        self.headlines = {"EQNR.OL": [catalysts.Headline("Equinor finds oil", "reuters.com", 2.0)]}
        self.assertEqual(holding_alerts.send_holding_alerts(TODAY), 3)
        text = self._sent()
        self.assertIn("EQNR.OL</b> is -20% below your average buy price (240.00 vs 300.00 NOK)", text)
        self.assertIn("earnings in 3 days", text)
        self.assertIn("Equinor finds oil", text)
        self.assertIn("never sells", text)
        # The next evening nothing has changed, so nothing is repeated.
        self.assertEqual(holding_alerts.send_holding_alerts(TODAY), 0)
        self.assertEqual(messaging.send_telegram_message.call_count, 1)

    def test_small_fall_is_quiet(self):
        self.price["EQNR.OL"] = 260.0
        self.assertEqual(holding_alerts.send_holding_alerts(TODAY), 0)

    def test_failed_send_is_retried_next_time(self):
        self.price["EQNR.OL"] = 240.0
        messaging.send_telegram_message.return_value = False
        self.assertEqual(holding_alerts.send_holding_alerts(TODAY), 0)
        messaging.send_telegram_message.return_value = True
        self.assertEqual(holding_alerts.send_holding_alerts(TODAY), 1)

    def test_sold_stock_is_not_watched(self):
        with patch.object(fills, "_close_on_or_before", return_value=300.0):
            fills.record_fill("SELL EQNR.OL 10 300 59")
        self.price["EQNR.OL"] = 100.0
        self.assertEqual(holding_alerts.find_alerts(TODAY), [])

    def test_job_never_raises(self):
        with patch.object(holding_alerts, "send_holding_alerts", side_effect=RuntimeError("x")):
            self.assertFalse(holding_alerts.job_holding_alerts())


if __name__ == "__main__":
    unittest.main()
