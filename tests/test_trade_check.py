"""trade_check: /check TICKER AMOUNT before placing an order (#121)."""

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from newsbot import commands as cmd
from newsbot import config as nb_config
from newsbot import fills
from newsbot import history_store as nb_history_store
from newsbot import indicators as nb_indicators
from newsbot import messaging as nb_messaging
from newsbot import risk_forecast, trade_check

TODAY = date(2026, 11, 11)


def _m(symbol, close=100.0, atr=2.0):
    return nb_indicators.TradeMetrics(
        symbol=symbol, session="2026-11-10", last_close=close, day_change_pct=1.0,
        week_momentum_pct=3.0, volume_ratio=1.5, drawdown_pct=2.0, atr_pct=atr, above_ema20=True,
        ema20=98.0, rsi_14=60.0, support_level=95.0, resistance_level=104.0, volume_trend="flat")


FORECAST = risk_forecast.Forecast(
    sessions=504, bad_moves=(("2 weeks", -6.0), ("4 weeks", -9.0)), max_drop_pct=-30.0,
    drop_from=date(2025, 3, 3), drop_to=date(2025, 8, 1), earnings_move_pct=4.5, earnings_reports=8)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        self.metrics = {"EQNR.OL": _m("EQNR.OL", 285.0), "DNB.OL": _m("DNB.OL", 250.0),
                        "AAPL": _m("AAPL", 200.0), "HAFNI.OL": _m("HAFNI.OL", 60.0)}
        self.profiles = {"EQNR.OL": {"next_earnings": TODAY + timedelta(days=60)}}
        self.prices = {"USDNOK=X": 11.0}
        for p in (
            patch.object(fills, "TRADE_HISTORY", self.store),
            patch.object(nb_config.SETTINGS, "risk_account_size", 50_000.0),
            patch.object(trade_check.indicators, "_compute_trade_metrics", side_effect=self.metrics.get),
            patch.object(trade_check.market_data, "_fetch_event_profile",
                         side_effect=lambda s: self.profiles.get(s, {})),
            patch.object(trade_check.market_data, "_next_ex_dividend_date", return_value=None),
            patch.object(trade_check.risk_forecast, "build", return_value=FORECAST),
            patch.object(trade_check.risk, "correlated_pairs", return_value=[]),
            patch.object(fills, "_latest_price", side_effect=self.prices.get),
        ):
            p.start()
            self.addCleanup(p.stop)

    def check(self, argument):
        return trade_check.build_check(argument, today=TODAY)

    def test_oslo_stock_full_message(self):
        text = self.check("eqnr.ol 12000")
        self.assertIn("<b>Check: EQNR.OL for 12,000 NOK</b>", text)
        self.assertIn("42 shares at 285.00 NOK = 11,970 NOK", text)
        self.assertIn("fees: 118 NOK to buy and sell (1.0% of 11,970 NOK)", text)
        self.assertIn("    The stock must rise 1.0% before you make anything", text)
        self.assertIn("✅ Allowed in an Aksjesparekonto.", text)
        self.assertIn("Next earnings report: Jan 10, 2027, after a 4-week hold.", text)
        self.assertIn("You hold nothing yet.", text)
        self.assertIn("2×ATR stop at 273.60 NOK (4.0% down) loses about 479 NOK", text)
        self.assertIn("a bad 4 weeks: -9.0% ≈ -1,077 NOK on 11,970", text)
        self.assertIn("24% of your money (50,000 NOK). The risk rule suggests about 6,250 NOK.", text)
        self.assertIn("⚠️ More than the 20% limit for one stock.", text)
        self.assertIn("not a buy or sell signal", text)

    def test_us_stock_is_not_for_ask_and_converted_to_nok(self):
        text = self.check("AAPL 5000")
        self.assertIn("2 shares at 200.00 USD ≈ 2,200.00 NOK = 4,400 NOK", text)
        self.assertIn("⛔ Not for ASK (US stock)", text)
        self.assertIn("+ currency exchange fee", text)
        self.assertIn("⚠️ The stock must rise 2.7%", text)

    def test_earnings_and_ex_dividend_inside_the_hold(self):
        self.profiles["EQNR.OL"] = {"next_earnings": TODAY + timedelta(days=9)}
        with patch.object(trade_check.market_data, "_next_ex_dividend_date",
                          return_value=TODAY + timedelta(days=3)):
            text = self.check("EQNR.OL 12000")
        self.assertIn("Earnings report in 9 days (Nov 20), inside a 2-4 week hold. "
                      "Its report days moved ±4.5% on average.", text)
        self.assertNotIn("⚠️ Earnings", text)   # tested, did not help (#125)
        self.assertIn("Ex-dividend in 3 days (Nov 14)", text)

    def test_overlap_with_holdings(self):
        fills.record_fill("BUY DNB.OL 20 250 59 2026-11-02 !")
        fills.record_fill("BUY EQNR.OL 10 285 59 2026-11-02 !")
        with patch.object(trade_check.risk, "correlated_pairs",
                          return_value=[("HAFNI.OL", "EQNR.OL", 0.81, 60)]):
            text = self.check("HAFNI.OL 6000")
        self.assertIn("⛔ Not for ASK (company registered outside the EU/EEA, ISIN SG)", text)
        self.assertIn("Moves together with EQNR.OL (correlation 0.81).", text)
        self.assertNotIn("⚠️ Moves together", text)
        self.assertNotIn("Same sector", text)
        text = self.check("EQNR.OL 3000")
        self.assertIn("You already hold 10 shares of EQNR.OL.", text)
        self.assertIn("No overlap", self.check("AAPL 3000"))

    def test_same_sector_from_the_stock_list(self):
        fills.record_fill("BUY AKRBP.OL 10 250 59 2026-11-02 !")
        self.assertIn("⚠️ Same sector (Energy) as AKRBP.OL.", self.check("EQNR.OL 3000"))

    def test_wild_stock_gets_a_too_small_for_the_fee_warning(self):
        self.metrics["EQNR.OL"] = _m("EQNR.OL", 285.0, atr=6.0)    # suggests 50,000 * 0.5/12 = 2,083 NOK
        text = self.check("EQNR.OL 12000")
        self.assertIn("The risk rule suggests about 2,083 NOK.", text)
        self.assertIn("⚠️ At that size the fee is 5.7% round trip", text)
        # With 2% risk and a 25% cap (the Pi's settings) a calm stock gets 12,500 NOK: no warning.
        with patch.object(nb_config.SETTINGS, "risk_per_trade_pct", 2.0), \
             patch.object(nb_config.SETTINGS, "risk_max_position_pct", 25.0):
            text = self.check("DNB.OL 12000")
        self.assertIn("The risk rule suggests about 12,500 NOK.", text)
        self.assertNotIn("At that size", text)

    def test_cash_and_unknown_account(self):
        fills.record_deposit("10000 2026-11-01")
        text = self.check("EQNR.OL 12000")
        self.assertIn("⚠️ More than your cash (10,000 NOK).", text)
        with patch.object(nb_config.SETTINGS, "risk_account_size", 0.0), \
             patch.object(fills, "deposits", return_value=[]):
            self.assertIn("isn't known. Add it with /deposit.", self.check("EQNR.OL 3000"))

    def test_bad_input_and_missing_data(self):
        self.assertIn("Usage: /check", self.check(""))
        self.assertIn("'lots' is not an amount", self.check("EQNR.OL lots"))
        self.assertIn("more than 0 NOK", self.check("EQNR.OL -5"))
        self.assertIn("No prices for NOPE.OL", self.check("NOPE.OL 1000"))
        self.assertIn("costs about 285 NOK, more than 100 NOK", self.check("EQNR.OL 100"))
        self.metrics["VOD.L"] = _m("VOD.L")
        self.assertIn("isn't supported yet", self.check("VOD.L 1000"))
        self.metrics["SAP.DE"] = _m("SAP.DE")
        self.assertIn("No EUR/NOK rate", self.check("SAP.DE 1000"))

    def test_reply_goes_to_telegram_only(self):
        with patch.object(nb_messaging, "send_telegram_message", return_value=True) as send, \
             patch.object(nb_messaging, "_broadcast_message") as broadcast, \
             patch.object(trade_check, "build_check", return_value="CHECKED") as build:
            cmd._handle_command("/check EQNR.OL 12000", "7")
        build.assert_called_once_with("EQNR.OL 12000")
        send.assert_called_once_with("CHECKED", chat_id="7")
        broadcast.assert_not_called()
        self.assertIn("/check TICKER AMOUNT_NOK", cmd._command_help_text())


class ExDividendTests(unittest.TestCase):
    def test_future_dates_only(self):
        md = trade_check.market_data
        future = date.today() + timedelta(days=5)
        with patch.object(md.yf, "Ticker") as ticker:
            ticker.return_value.calendar = {"Ex-Dividend Date": future}
            self.assertEqual(md._next_ex_dividend_date("EQNR.OL"), future)
            ticker.return_value.calendar = {"Ex-Dividend Date": date.today() - timedelta(days=5)}
            self.assertIsNone(md._next_ex_dividend_date("EQNR.OL"))
            ticker.return_value.calendar = {}
            self.assertIsNone(md._next_ex_dividend_date("EQNR.OL"))


if __name__ == "__main__":
    unittest.main()
