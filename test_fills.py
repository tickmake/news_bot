"""fills: /fill, /fills and /unfill for real trades."""

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from newsbot import commands as cmd
from newsbot import config as nb_config
from newsbot import fills
from newsbot import history_store as nb_history_store
from newsbot import messaging as nb_messaging
from newsbot import state as nb_state


def _history(close):
    index = pd.date_range(end=pd.Timestamp.today().normalize(), periods=5, freq="D", tz="Europe/Oslo")
    return pd.DataFrame({"Close": [close] * 5}, index=index)


class FillTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        for p in (
            patch.object(fills, "TRADE_HISTORY", self.store),
            patch.object(fills.market_data, "_fetch_ticker_history", side_effect=self._fetch),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.prices = {"EQNR.OL": 285.0, "AAPL": 200.0, "USDNOK=X": 11.0, "SAP.DE": 250.0, "EURNOK=X": 11.5}

    def _fetch(self, symbol, period="3mo"):
        if symbol not in self.prices:
            return pd.DataFrame({"Close": []})
        return _history(self.prices[symbol])

    def _rows(self):
        return fills._connection().execute("SELECT * FROM real_trades").fetchall()

    def test_oslo_buy_is_saved_in_nok(self):
        reply = fills.record_fill("buy eqnr.ol 40 285.40 59")
        self.assertIn("Saved", reply)
        self.assertIn("BUY 40 EQNR.OL at 285.40 NOK", reply)
        self.assertIn("worth 11,416 NOK, fee 59 NOK", reply)
        self.assertEqual(len(self._rows()), 1)

    def test_us_and_euro_trades_store_the_day_rate(self):
        self.assertIn("1 USD = 11.0000 NOK", fills.record_fill("BUY AAPL 10 201 59"))
        self.assertIn("1 EUR = 11.5000 NOK", fills.record_fill("BUY SAP.DE 4 250,5 59"))
        rates = [row[6] for row in self._rows()]
        self.assertEqual(rates, [11.0, 11.5])

    def test_typo_guards_refuse_unless_forced(self):
        self.assertIn("no prices for NOPE.OL", fills.record_fill("BUY NOPE.OL 1 10 59"))
        self.assertIn("+75% away", fills.record_fill("BUY EQNR.OL 1 500 59"))
        self.assertIn("log shows 0 held", fills.record_fill("SELL EQNR.OL 5 285 59"))
        self.assertEqual(self._rows(), [])
        self.assertIn("Saved", fills.record_fill("BUY EQNR.OL 1 500 59 !"))
        self.assertEqual(self._rows()[0][9], 1)

    def test_sell_within_holding_is_fine(self):
        fills.record_fill("BUY EQNR.OL 10 285 59")
        self.assertIn("Saved", fills.record_fill("SELL EQNR.OL 10 290 59"))

    def test_bad_input_explains_itself_and_saves_nothing(self):
        self.assertIn("Usage", fills.record_fill(""))
        self.assertIn("BUY or SELL", fills.record_fill("HOLD EQNR.OL 1 285 59"))
        self.assertIn("whole number", fills.record_fill("BUY EQNR.OL 1.5 285 59"))
        self.assertIn("above 0", fills.record_fill("BUY EQNR.OL 0 285 59"))
        self.assertIn("2026-11-10", fills.record_fill("BUY EQNR.OL 1 285 59 10/11"))
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertIn("within the last 300 days", fills.record_fill(f"BUY EQNR.OL 1 285 59 {tomorrow}"))
        self.assertIn("which currency", fills.record_fill("BUY VOD.L 1 70 59"))
        self.assertEqual(self._rows(), [])

    def test_optional_date_is_stored(self):
        day = (date.today() - timedelta(days=2)).isoformat()
        self.assertIn(day, fills.record_fill(f"BUY EQNR.OL 1 285 59 {day}"))

    def test_list_and_unfill(self):
        self.assertIn("No real trades", fills.list_fills())
        self.assertIn("Nothing to delete", fills.delete_last_fill())
        fills.record_fill("BUY EQNR.OL 10 285 59")
        fills.record_fill("BUY AAPL 2 200 59")
        listing = fills.list_fills()
        self.assertLess(listing.index("AAPL"), listing.index("EQNR.OL"))
        self.assertIn("Deleted:\n#2", fills.delete_last_fill())
        self.assertEqual(len(self._rows()), 1)


class FillCommandTests(unittest.TestCase):
    def setUp(self):
        for p in (
            patch.object(nb_messaging, "_broadcast_message"),
            patch.object(nb_messaging, "send_telegram_message", return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_replies_go_to_telegram_only(self):
        with patch.object(fills, "record_fill", return_value="SAVED") as record, \
                patch.object(fills, "list_fills", return_value="LIST"), \
                patch.object(fills, "delete_last_fill", return_value="GONE"):
            cmd._handle_command("/fill BUY EQNR.OL 1 285 59", "7")
            cmd._handle_command("/fills", "7")
            cmd._handle_command("/unfill", "7")
        record.assert_called_once_with("BUY EQNR.OL 1 285 59")
        sent = [call.args[0] for call in nb_messaging.send_telegram_message.call_args_list]
        self.assertEqual(sent, ["SAVED", "LIST", "GONE"])
        nb_messaging._broadcast_message.assert_not_called()

    def test_edited_fill_is_not_run_again(self):
        update = {"update_id": 5, "edited_message": {"chat": {"id": 111}, "text": "/fill BUY X 1 1 1"}}
        response = unittest.mock.MagicMock()
        response.json.return_value = {"result": [update]}
        with patch.object(nb_config.SETTINGS, "command_poll_enabled", True), \
                patch.object(nb_config.SETTINGS, "telegram_token", "T"), \
                patch.object(nb_config.SETTINGS, "telegram_chat_id", "111"), \
                patch.object(nb_state.STATE, "save"), \
                patch.dict(nb_state.STATE.data, {"telegram_update_offset": 0}), \
                patch.object(cmd, "_dispatch_command") as dispatch, \
                patch("requests.get", return_value=response):
            cmd.poll_telegram_commands()
        dispatch.assert_not_called()
        self.assertIn("Edited messages are ignored", nb_messaging.send_telegram_message.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
