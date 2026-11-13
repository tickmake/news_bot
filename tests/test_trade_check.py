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
        self.assertIn("fees: 118 NOK to buy and sell (1.0% of 11,970 NOK, 0.24% of 50,000 NOK)", text)
        self.assertIn("    The stock must rise 1.0% before you make anything", text)
        self.assertIn("✅ Allowed in an Aksjesparekonto.", text)
        self.assertIn("Next earnings report: Jan 10, 2027, after a 4-week hold.", text)
        self.assertIn("You hold nothing yet.", text)
        self.assertIn("volatility stop 273.60 NOK, 2×ATR below the 285.00 signal close: 11.40 (4.0%) away"
                      " · ATR 5.70 (2.0%)", text)
        self.assertIn("loss if bought at the 285.00 close: 0.96% of your money ≈ 479 NOK at the stop, "
                      "before costs; your actual risk isn't known until you buy", text)
        # 2xATR: stop 273.60 less the 2.36% measured for that stop sells near 267.14.
        self.assertIn("if it gaps through: sold near 267.14, about 1.50% of your money ≈ 750 NOK from "
                      "that close (estimate, not a maximum)", text)
        self.assertIn("worked out from the 2026-11-10 close, not from your buy price, which isn't known yet",
                      text)
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

    # --- a buy price (#172): the stop and every figure from it -----------------

    def test_a_buy_price_of_101_moves_the_stop_and_every_figure_to_it(self):
        self.metrics["NHY.OL"] = _m("NHY.OL", 100.0)    # ATR 2% = 2.00
        with patch.object(nb_config.SETTINGS, "risk_stop_atr_multiple", 3.0):
            text = self.check("NHY.OL 10100 101")
        self.assertIn("100 shares at your buy price of 101.00 NOK = 10,100 NOK "
                      "(close of 2026-11-10: 100.00 NOK)", text)
        self.assertIn("    risk: volatility stop 95.00 NOK, 3×ATR below your 101.00 buy price: 6.00 (5.9%) away"
                      " · ATR 2.00 (2.0%)", text)
        self.assertIn("    loss from your 101.00 buy price: 1.20% of your money ≈ 600 NOK at the stop, "
                      "before costs", text)
        self.assertIn("if it gaps through: it sells below the stop; how far is not measured", text)
        self.assertIn("worked out from your 101.00 NOK buy price: 3×ATR below it, with the ATR from the "
                      "2026-11-10 close", text)
        # The same risk rule on the same reference: 0.5% / (6.00 / 101) of 50,000.
        self.assertIn("The risk rule suggests about 4,208 NOK.", text)
        for absent in ("estimated volatility stop", "signal close:", "isn't known until you buy",
                       "not from your buy price"):
            self.assertNotIn(absent, text)

    def test_without_a_buy_price_the_stop_is_an_estimate_from_the_signal_close(self):
        self.metrics["NHY.OL"] = _m("NHY.OL", 100.0)
        with patch.object(nb_config.SETTINGS, "risk_stop_atr_multiple", 3.0):
            text = self.check("NHY.OL 10100")
        self.assertIn("101 shares at 100.00 NOK = 10,100 NOK (close of 2026-11-10)", text)
        self.assertIn("    risk: estimated volatility stop 94.00 NOK, 3×ATR below the 100.00 signal close: "
                      "6.00 (6.0%) away · ATR 2.00 (2.0%). Once bought it is 3×ATR = 6.00 below your buy price",
                      text)
        self.assertIn("loss if bought at the 100.00 close: 1.21% of your money ≈ 606 NOK at the stop, before "
                      "costs; your actual risk isn't known until you buy", text)
        self.assertIn("sold near 91.42", text)
        self.assertIn("Add your buy price to work them out from it: /check NHY.OL 10100 PRICE.", text)
        self.assertIn("The risk rule suggests about 4,167 NOK.", text)
        self.assertNotIn("your 100.00 buy price", text)

    def test_two_and_three_atr_stops_from_the_same_buy_price(self):
        self.metrics["NHY.OL"] = _m("NHY.OL", 100.0)
        for multiple, stop, distance in ((2.0, "97.00", "4.00 (4.0%)"), (3.0, "95.00", "6.00 (5.9%)")):
            with patch.object(nb_config.SETTINGS, "risk_stop_atr_multiple", multiple):
                text = self.check("NHY.OL 10100 101")
            self.assertIn(f"volatility stop {stop} NOK, {multiple:g}×ATR below your 101.00 buy price: "
                          f"{distance} away", text)

    def test_a_buy_price_is_in_the_stocks_own_currency(self):
        text = self.check("AAPL 5000 202")             # close 200 USD, ATR 4.00, 2xATR
        self.assertIn("2 shares at your buy price of 202.00 USD ≈ 2,222.00 NOK = 4,444 NOK "
                      "(close of 2026-11-10: 200.00 USD)", text)
        self.assertIn("volatility stop 194.00 USD, 2×ATR below your 202.00 buy price: 8.00 (4.0%) away", text)

    def test_bad_buy_prices_are_refused(self):
        for price, reason in (("0", "must be a number above 0"), ("-101", "must be a number above 0"),
                              ("nan", "must be a number above 0"), ("inf", "must be a number above 0"),
                              ("1e999", "must be a number above 0"), ("abc", "'abc' is not a buy price")):
            with self.subTest(price=price):
                text = self.check(f"EQNR.OL 12000 {price}")
                self.assertIn(reason, text)
                self.assertIn("Usage: /check", text)
                self.assertNotIn("risk:", text)
        self.assertIn("Usage: /check", self.check("EQNR.OL 12000 290 300"))
        # Far from the close it is more likely a typo, as in /fill; ! uses it anyway.
        for price, gap in (("2850", "+900%"), ("239", "-16%")):
            text = self.check(f"EQNR.OL 12000 {price}")
            self.assertIn(f"Not checked: {price} is {gap} away from the close of 285.00 NOK", text)
            self.assertNotIn("risk:", text)
        self.assertIn("below your 330.00 buy price", self.check("EQNR.OL 12000 330 !"))

    def test_a_decimal_comma_and_a_bang_without_a_price(self):
        self.assertIn("below your 287.50 buy price", self.check("EQNR.OL 12000 287,50"))
        self.assertIn("estimated volatility stop 273.60 NOK", self.check("EQNR.OL 12000 !"))

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
