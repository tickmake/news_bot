"""scorecard: my real trades against the index fund."""

import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from newsbot import fills, messaging, scorecard
from newsbot import config as nb_config
from newsbot import history_store as nb_history_store

FUND = "0P00018V9L.IR"


class ScorecardTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        self.today = date.today()

        def close(symbol, day, period="1y"):
            if symbol == FUND:
                # 1,000 on the deposit day, 1,100 today: the fund made 10%.
                return 1_100.0 if day == self.today else 1_000.0
            if symbol == "EURNOK=X" and day != self.today:
                return 11.5  # a rate for the trade day only, none today
            return {"EQNR.OL": 300.0}.get(symbol)

        for p in (
            patch.object(fills, "TRADE_HISTORY", self.store),
            patch.object(fills, "_close_on_or_before", side_effect=close),
            patch.object(nb_config.SETTINGS, "risk_account_size", 0.0),
            patch.object(messaging, "send_telegram_message", return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_nothing_to_report_without_trades_or_money(self):
        self.assertIsNone(scorecard.build_scorecard())
        self.assertIn("No scorecard yet", scorecard.scorecard_reply())
        self.assertTrue(scorecard.job_monthly_scorecard())
        messaging.send_telegram_message.assert_not_called()

    def test_trades_against_the_fund(self):
        fills.record_deposit("10000 2026-11-02")
        fills.record_fill("BUY EQNR.OL 10 280 59 2026-11-03 !")
        text = scorecard.build_scorecard()
        # Cash 10,000 - 2,859 = 7,141, shares 10 x 300 = 3,000: 10,141 in all.
        self.assertIn("My account now: 10,141 NOK, +141 NOK (+1.4%) after all fees", text)
        self.assertIn("Same money in the fund: 11,000 NOK, +1,000 NOK (+10.0%)", text)
        self.assertIn("You are 859 NOK behind the index fund.", text)
        self.assertIn("Fees paid so far: 59 NOK in 1 trades", text)
        self.assertTrue(scorecard.job_monthly_scorecard())
        messaging.send_telegram_message.assert_called_once()

    def test_missing_prices_are_said_not_guessed(self):
        fills.record_deposit("10000 2026-11-02")
        fills.record_fill("BUY SAP.DE 1 250 59 2026-11-03 !")
        self.assertIn("no price for SAP.DE", scorecard.build_scorecard())

    def test_job_never_raises(self):
        with patch.object(scorecard, "build_scorecard", side_effect=RuntimeError("x")):
            self.assertFalse(scorecard.job_monthly_scorecard())


if __name__ == "__main__":
    unittest.main()
