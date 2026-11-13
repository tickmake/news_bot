"""The stop from the buy price when one is known, from the signal close otherwise (#172, P1-3).

Without a buy price the stop is N x ATR below the signal close and every view
calls it an estimate. With one (/check TICKER AMOUNT PRICE) it is the same
N x ATR, as a price distance, below that price, and every number shown is
measured from it. Published picks have no buy price, so what they save does
not change.
"""

import json
import math
import sqlite3
from contextlib import closing
from unittest.mock import patch

from newsbot import fills, risk, trade_check
from tests.test_risk_reporting import SENT, TODAY, LiveSettingsCase, StoreCase, _m

# The record layout a published pick saved before this change (risk model 2).
RECORD_KEYS = [
    "model_version", "stop_anchor", "loss_basis", "entry_price", "symbol", "session", "reference_price",
    "atr_pct", "atr_price", "atr_multiple", "stop_price", "stop_distance_pct", "stop_distance_price",
    "exposure_nok", "exposure_pct", "account_nok", "capped", "stop_loss_nok", "stop_loss_pct",
    "gap_measured", "gap_below_stop_pct", "gap_fill_price", "gap_loss_nok", "gap_loss_pct", "fee_nok",
    "fee_pct", "fx_cost_included", "warnings",
]


class StopReferenceTests(LiveSettingsCase):
    """Signal close 100, ATR 2% (2.00), 3xATR: the Pi's stop."""

    def test_a_buy_price_of_101_puts_the_stop_at_95(self):
        trade = risk.trade_risk(_m(close=100.0, atr=2.0), exposure_nok=10_100.0, account_nok=50_000.0,
                                entry_price=101.0)
        self.assertAlmostEqual(trade.stop_price, 95.0)                  # 101 - 3 x 2.00
        self.assertEqual(trade.reference_price, 101.0)
        self.assertEqual(trade.entry_price, 101.0)
        self.assertAlmostEqual(trade.stop_distance_price, 6.0)
        self.assertAlmostEqual(trade.stop_distance_pct, 6.0 / 101.0 * 100)  # 5.94% of 101
        self.assertAlmostEqual(trade.atr_price, 2.0)                    # the ATR is not rescaled
        self.assertEqual(trade.atr_multiple, 3.0)
        # 100 shares at 101 lose 6.00 each at the stop: 600 NOK, 1.2% of 50,000.
        self.assertAlmostEqual(trade.stop_loss_nok, 600.0)
        self.assertAlmostEqual(trade.stop_loss_pct, 1.2)

    def test_without_a_buy_price_the_stop_is_from_the_signal_close(self):
        trade = risk.trade_risk(_m(close=100.0, atr=2.0), exposure_nok=10_000.0, account_nok=50_000.0)
        self.assertAlmostEqual(trade.stop_price, 94.0)
        self.assertEqual(trade.reference_price, 100.0)
        self.assertIsNone(trade.entry_price)
        self.assertAlmostEqual(trade.stop_distance_price, 6.0)
        self.assertAlmostEqual(trade.stop_distance_pct, 6.0)
        self.assertAlmostEqual(trade.stop_loss_nok, 600.0)
        self.assertTrue(trade.gap_measured)

    def test_a_different_buy_price_gives_a_different_stop_at_the_same_distance(self):
        metrics = _m(close=100.0, atr=2.0)
        stops = {}
        for entry in (98.0, 100.0, 101.0, 102.5):
            trade = risk.trade_risk(metrics, entry_price=entry)
            stops[entry] = trade.stop_price
            self.assertAlmostEqual(trade.stop_price, entry - 6.0)
            self.assertAlmostEqual(trade.stop_distance_price, 6.0)
            self.assertAlmostEqual(trade.stop_distance_pct, 6.0 / entry * 100)
        self.assertEqual(len({round(stop, 9) for stop in stops.values()}), 4)
        self.assertNotAlmostEqual(stops[101.0], risk.trade_risk(metrics).stop_price)
        # A buy at the close itself gives the close's stop.
        self.assertAlmostEqual(stops[100.0], risk.trade_risk(metrics).stop_price)

    def test_the_configured_multiple_is_used_as_it_is(self):
        metrics = _m(close=100.0, atr=2.0)
        for multiple, from_close, from_101 in ((2.0, 96.0, 97.0), (3.0, 94.0, 95.0)):
            self.use(risk_stop_atr_multiple=multiple)
            close_based, buy_based = risk.trade_risk(metrics), risk.trade_risk(metrics, entry_price=101.0)
            self.assertAlmostEqual(close_based.stop_price, from_close)
            self.assertAlmostEqual(buy_based.stop_price, from_101)
            for trade in (close_based, buy_based):
                self.assertEqual(trade.atr_multiple, multiple)
                self.assertAlmostEqual(trade.stop_distance_price, multiple * 2.0)
            self.assertAlmostEqual(risk.size_position(metrics, entry_price=101.0).stop_price, from_101)

    def test_sizing_uses_the_same_reference_and_the_same_rule(self):
        self.use(risk_per_trade_pct=0.5)                 # small enough not to hit the 25% cap
        metrics = _m(close=100.0, atr=2.0)
        close_based, buy_based = risk.size_position(metrics), risk.size_position(metrics, entry_price=101.0)
        self.assertAlmostEqual(close_based.stop_price, 94.0)
        self.assertAlmostEqual(close_based.weight_pct, 0.5 / 6.0 * 100)
        self.assertAlmostEqual(buy_based.stop_price, 95.0)
        self.assertAlmostEqual(buy_based.stop_pct, 6.0 / 101.0 * 100)
        self.assertAlmostEqual(buy_based.weight_pct, 0.5 / buy_based.stop_pct * 100)
        self.assertAlmostEqual(buy_based.risk_pct, 0.5)  # the risk budget is unchanged

    def test_an_unusable_buy_price_gives_no_stop(self):
        metrics = _m(close=100.0, atr=2.0)
        for entry in (0.0, -101.0, float("nan"), float("inf"), 6.0, 5.0):  # 6.0 and 5.0: stop at or below 0
            with self.subTest(entry=entry):
                self.assertIsNone(risk.trade_risk(metrics, entry_price=entry))
                self.assertIsNone(risk.size_position(metrics, entry_price=entry))

    def test_a_stop_below_a_buy_price_borrows_no_gap_figure(self):
        trade = risk.trade_risk(_m(close=100.0, atr=2.0), exposure_nok=10_100.0, entry_price=101.0)
        self.assertFalse(trade.gap_measured)
        self.assertIsNone(trade.gap_fill_price)
        self.assertIsNone(risk.gap_study(anchor=risk.BUY_PRICE_ANCHOR))
        self.assertIn("measured only for stops below the signal close", risk.gap_note(risk.BUY_PRICE_ANCHOR))
        self.assertIn("about 1 stop in 7 opened below its price", risk.gap_note())


class StopWordingTests(LiveSettingsCase):
    def test_with_a_buy_price_the_lines_name_it(self):
        trade = risk.trade_risk(_m(close=100.0, atr=2.0), exposure_nok=10_100.0, account_nok=50_000.0,
                                entry_price=101.0)
        self.assertEqual(risk.stop_line(trade, "NOK"),
                         "    risk: volatility stop 95.00 NOK, 3×ATR below your 101.00 buy price: 6.00 (5.9%) "
                         "away · ATR 2.00 (2.0%)")
        lines = risk.loss_lines(trade, "your money")
        self.assertEqual(lines[0], "    loss from your 101.00 buy price: 1.20% of your money ≈ 600 NOK at the "
                                   "stop, before costs")
        self.assertIn("sells below the stop; how far is not measured", lines[1])
        text = "\n".join([risk.stop_line(trade), *lines])
        for absent in ("estimated", "signal close", "isn't known until you buy"):
            self.assertNotIn(absent, text)

    def test_without_one_the_stop_is_called_an_estimate_from_the_close(self):
        trade = risk.trade_risk(_m(close=100.0, atr=2.0), exposure_nok=10_000.0, account_nok=50_000.0)
        line = risk.stop_line(trade, "NOK")
        self.assertEqual(line, "    risk: estimated volatility stop 94.00 NOK, 3×ATR below the 100.00 signal "
                               "close: 6.00 (6.0%) away · ATR 2.00 (2.0%). Once bought it is 3×ATR = 6.00 "
                               "below your buy price")
        self.assertIn("your actual risk isn't known until you buy", risk.loss_lines(trade)[0])
        self.assertNotIn("your 100.00 buy price", line)
        footer = "\n".join(risk.portfolio_lines(risk.assess([_m()], {}, [], TODAY)))
        self.assertIn("3×ATR stop below the signal close", footer)
        self.assertIn("Once bought, the stop is the same distance below the buy price", footer)


class HistoricalRecordTests(StoreCase):
    def raw(self):
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
            return {table: conn.execute(f'SELECT * FROM "{table}"').fetchall()  # noqa: S608
                    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def test_a_published_pick_saves_the_same_record_as_before(self):
        self.store.publish([_m("AAA")], SENT)
        saved = json.loads(self.store.published_picks()[0]["snapshot_json"])["risk"]
        self.assertEqual(list(saved), RECORD_KEYS)
        self.assertEqual((saved["stop_anchor"], saved["entry_price"]), ("signal_close", None))
        self.assertEqual((saved["reference_price"], saved["atr_multiple"]), (100.0, 3.0))
        self.assertAlmostEqual(saved["stop_price"], 94.0)

    def test_check_with_a_buy_price_changes_no_saved_record(self):
        self.store.publish([_m("AAA"), _m("EQNR.OL", close=250.0)], SENT)
        with patch.object(fills, "TRADE_HISTORY", self.store), \
             patch.object(trade_check.indicators, "_compute_trade_metrics", return_value=_m("AAA")), \
             patch.object(trade_check.market_data, "_fetch_event_profile", return_value={}), \
             patch.object(trade_check.market_data, "_next_ex_dividend_date", return_value=None), \
             patch.object(trade_check.risk_forecast, "build", return_value=None), \
             patch.object(trade_check.risk, "correlated_pairs", return_value=[]), \
             patch.object(fills, "_latest_price", return_value=11.0):
            fills.record_fill("BUY EQNR.OL 5 250 59 2026-11-12 !")   # NOK, forced: no lookups
            before = self.raw()          # with the /fill and /deposit tables, as on the Pi
            text = trade_check.build_check("AAA 12000 101", today=TODAY)
            self.store.publish([_m("AAA")], SENT)          # the same pick again: first one wins
        self.assertIn("below your 101.00 buy price", text)
        self.assertEqual(self.raw(), before)

    def test_a_record_with_a_buy_price_would_say_so(self):
        # No caller saves one (a pick is published before anyone buys); if one did, it is labelled.
        saved = risk.snapshot(risk.trade_risk(_m(), entry_price=101.0))
        self.assertEqual((saved["stop_anchor"], saved["entry_price"]), ("buy_price", 101.0))
        self.assertTrue(math.isclose(saved["reference_price"], 101.0))
