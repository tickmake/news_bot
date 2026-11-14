"""Risk accounting: one calculation, shown the same way everywhere (#161).

The stop, the money in a position, the loss at the stop, the gap estimate and
the fee are worked out once in `risk.trade_risk`. /recommendations, /analyze
and /check render that; a published pick saves it; the dashboard shows the
saved copy. These tests pin the arithmetic to the settings in use, pin that
the losses are said to be modelled from the signal close (the buy price is not
known), tie the gap numbers to the research that measured them, and pin that a
pick sent before the record existed is never given one.
"""

import ast
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from unittest.mock import patch

import pandas as pd

from newsbot import config as nb_config
from newsbot import fills
from newsbot import history_store as nb_history_store
from newsbot import indicators as nb_indicators
from newsbot import market_data as md
from newsbot import reports as nb_reports
from newsbot import risk, trade_check
from newsbot import screener as nb_screener
from tests.store_isolation import use_throwaway_store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date(2026, 11, 13)
SENT = datetime(2026, 11, 13, 6, 0, tzinfo=timezone.utc)


def _load(name, *path):
    # By path: neither streamlit/ nor research/ is a package.
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


views = _load("archive_views", "streamlit", "archive_views.py")
gap_research = _load("gap_risk", "research", "gap_risk.py")


def _m(symbol="AAA", atr=2.0, close=100.0, **overrides):
    base = {
        "symbol": symbol, "session": "2026-11-12", "last_close": close, "day_change_pct": 1.0,
        "week_momentum_pct": 3.0, "volume_ratio": 1.5, "drawdown_pct": 2.0, "atr_pct": atr,
        "above_ema20": True, "ema20": 98.0, "rsi_14": 60.0, "support_level": 95.0,
        "resistance_level": 104.0, "volume_trend": "flat", "avg_volume_20": 2_000_000.0,
    }
    base.update(overrides)
    return nb_indicators.TradeMetrics(**base)


class LiveSettingsCase(unittest.TestCase):
    """The settings the Pi runs: 3xATR, 2% risk, 25% cap, 50,000 NOK."""

    def setUp(self):
        self.use(risk_stop_atr_multiple=3.0, risk_per_trade_pct=2.0, risk_max_position_pct=25.0,
                 risk_max_total_pct=100.0, risk_account_size=50_000.0, risk_analytics_enabled=True,
                 broker_fee_min_nok=59.0, broker_fee_pct=0.12,
                 trade_backtest_min_avg_volume=100_000.0,
                 relative_strength_enabled=False, catalysts_enabled=False,
                 risk_forecast_enabled=False)

    def use(self, **values):
        for name, value in values.items():
            patcher = patch.object(nb_config.SETTINGS, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class StopTests(LiveSettingsCase):
    def test_the_stop_uses_the_configured_multiple_from_the_signal_close(self):
        trade = risk.pick_risk(_m(atr=2.0, close=100.0))
        self.assertEqual(risk.STOP_ANCHOR, "signal_close")
        self.assertEqual(trade.atr_multiple, 3.0)
        self.assertEqual(trade.reference_price, 100.0)
        self.assertAlmostEqual(trade.atr_price, 2.0)
        self.assertAlmostEqual(trade.stop_distance_pct, 6.0)
        self.assertAlmostEqual(trade.stop_distance_price, 6.0)
        self.assertAlmostEqual(trade.stop_price, 94.0)

    def test_the_multiple_is_read_from_the_settings_not_assumed(self):
        for multiple, stop in ((2.0, 96.0), (3.0, 94.0), (2.5, 95.0)):
            self.use(risk_stop_atr_multiple=multiple)
            trade = risk.pick_risk(_m(atr=2.0, close=100.0))
            self.assertAlmostEqual(trade.stop_price, stop)
            self.assertEqual(trade.atr_multiple, multiple)

    def test_the_code_default_is_still_two_and_half_a_percent(self):
        # Not changed here: the Pi overrides both in its .env.
        defaults = nb_config.AppSettings(_env_file=None)
        self.assertEqual(defaults.risk_stop_atr_multiple, 2.0)
        self.assertEqual(defaults.risk_per_trade_pct, 0.5)

    def test_only_long_positions_the_stop_is_always_below_the_close(self):
        # The bot has no short trades: /fill only sells shares that are held.
        for atr in (0.3, 2.0, 4.5, 12.0):
            trade = risk.pick_risk(_m(atr=atr))
            self.assertLess(trade.stop_price, trade.reference_price)
            self.assertGreater(trade.stop_distance_price, 0)

    def test_the_stop_matches_the_one_sizing_uses(self):
        metrics = _m(atr=1.7, close=283.4)
        sized, trade = risk.size_position(metrics), risk.pick_risk(metrics)
        self.assertEqual(trade.stop_price, sized.stop_price)
        self.assertEqual(trade.stop_distance_pct, sized.stop_pct)


class SizingAndLossTests(LiveSettingsCase):
    def test_displayed_loss_is_the_position_times_the_stop_distance(self):
        metrics = _m(atr=2.0)                       # 2% / 6% = 33% wanted, capped at 25%
        sized, trade = risk.size_position(metrics), risk.pick_risk(metrics)
        self.assertTrue(trade.capped)
        self.assertAlmostEqual(trade.exposure_nok, 12_500.0)
        self.assertAlmostEqual(trade.exposure_pct, 25.0)
        self.assertEqual(trade.exposure_nok, sized.amount)
        self.assertAlmostEqual(trade.stop_loss_nok, 750.0)             # 12,500 x 6%
        self.assertAlmostEqual(trade.stop_loss_pct, 1.5)
        self.assertAlmostEqual(trade.stop_loss_pct, sized.risk_pct)
        self.assertAlmostEqual(trade.stop_loss_nok, trade.account_nok * trade.stop_loss_pct / 100)

    def test_an_uncapped_position_risks_exactly_the_budget(self):
        trade = risk.pick_risk(_m(atr=4.0))         # 2% / 12% = 16.7% of the account
        self.assertFalse(trade.capped)
        self.assertAlmostEqual(trade.stop_loss_pct, 2.0)
        self.assertAlmostEqual(trade.stop_loss_nok, 1_000.0)
        self.assertAlmostEqual(trade.exposure_nok, 50_000 * 2.0 / 12.0)

    def test_exposure_and_loss_are_separate_numbers(self):
        lines = risk.candidate_lines("AAA", risk.assess([_m()], {}, [], TODAY))
        self.assertEqual("    size: 25% of the portfolio ≈ 12,500 NOK (capped at 25%)", lines[1])
        self.assertEqual(lines[2], "    loss if bought at the 100.00 close: 1.50% of the portfolio ≈ 750 NOK "
                                   "at the stop, before costs; your actual risk isn't known until you buy")

    def test_without_an_account_size_only_shares_of_the_portfolio_are_shown(self):
        self.use(risk_account_size=0.0)
        trade = risk.pick_risk(_m())
        self.assertIsNone(trade.exposure_nok)
        self.assertIsNone(trade.stop_loss_nok)
        self.assertIsNone(trade.fee_nok)
        self.assertAlmostEqual(trade.stop_loss_pct, 1.5)
        lines = risk.candidate_lines("AAA", risk.assess([_m()], {}, [], TODAY))
        self.assertEqual("    size: 25% of the portfolio (capped at 25%)", lines[1])
        self.assertNotIn("NOK", lines[2] + lines[3])
        self.assertIn("fees: at least 118 NOK", lines[4])

    def test_check_and_recommendations_agree_for_the_same_money(self):
        pick = risk.pick_risk(_m("EQNR.OL", atr=2.0, close=250.0))      # 12,500 NOK = 50 shares
        with patch.object(trade_check.indicators, "_compute_trade_metrics",
                          return_value=_m("EQNR.OL", atr=2.0, close=250.0)), \
             patch.object(trade_check, "_account_value", return_value=(50_000.0, 50_000.0)), \
             patch.object(trade_check, "_profile", return_value={}), \
             patch.object(trade_check.market_data, "_next_ex_dividend_date", return_value=None), \
             patch.object(fills, "current_holdings", return_value={}):
            text = trade_check.build_check("EQNR.OL 12500", today=TODAY)
        self.assertIn(risk.stop_line(pick, "NOK"), text)
        for line in risk.loss_lines(pick, "your money"):
            self.assertIn(line, text)
        self.assertIn(risk.fee_line("EQNR.OL", pick.exposure_nok, pick.account_nok), text)
        self.assertIn("volatility stop 235.00 NOK, 3×ATR below the 250.00 signal close: 15.00 (6.0%) away",
                      text)
        self.assertIn("loss if bought at the 250.00 close: 1.50% of your money ≈ 750 NOK at the stop", text)
        self.assertIn("not from your buy price, which isn't known yet", text)


class GapTests(LiveSettingsCase):
    def test_the_gap_sale_is_the_stop_less_the_measured_shortfall(self):
        study = risk.gap_study()
        self.assertEqual((study.multiple, study.anchor), (3.0, "signal_close"))
        trade = risk.pick_risk(_m(atr=2.0, close=100.0))
        self.assertTrue(trade.gap_measured)
        self.assertEqual(trade.gap_below_stop_pct, study.gap_below_stop_p90_pct)
        self.assertAlmostEqual(trade.gap_fill_price, 94.0 * (1 - 2.75 / 100))       # 91.415
        # Bought at the 100.00 reference and sold at 91.415: 8.585% of the position.
        self.assertAlmostEqual(trade.gap_loss_nok, 12_500 * 0.08585)
        self.assertAlmostEqual(trade.gap_loss_pct, 25.0 * 0.08585)
        self.assertGreater(trade.gap_loss_nok, trade.stop_loss_nok)

    def test_units_are_not_mixed(self):
        # The first version added the shortfall, measured in points of the buy
        # price, to the stop distance, measured from the signal close.
        trade = risk.pick_risk(_m(atr=2.0, close=100.0))
        loss_pct_of_position = trade.gap_loss_nok / trade.exposure_nok * 100
        self.assertAlmostEqual(loss_pct_of_position,
                               (1 - trade.gap_fill_price / trade.reference_price) * 100)
        self.assertNotAlmostEqual(loss_pct_of_position,
                                  trade.stop_distance_pct + trade.gap_below_stop_pct, places=3)

    def test_it_is_called_an_estimate_and_a_real_gap_can_be_worse(self):
        # The audit's example: stop at 94, the stock opens at 85.
        trade = risk.pick_risk(_m(atr=2.0, close=100.0))
        opened_at = 85.0
        realised = trade.exposure_nok * (trade.reference_price - opened_at) / trade.reference_price
        self.assertAlmostEqual(realised, 1_875.0)
        self.assertAlmostEqual(realised / trade.account_nok * 100, 3.75)
        self.assertGreater(realised, trade.gap_loss_nok)
        lines = risk.loss_lines(trade)
        self.assertEqual(lines[1], "    if it gaps through: sold near 91.42, about 2.15% of the portfolio "
                                   "≈ 1,073 NOK from that close (estimate, not a maximum)")
        self.assertNotIn("most you can lose", " ".join(lines).lower())

    def test_a_stop_that_was_never_measured_gets_no_borrowed_numbers(self):
        self.use(risk_stop_atr_multiple=2.5)
        self.assertIsNone(risk.gap_study())
        trade = risk.pick_risk(_m())
        self.assertFalse(trade.gap_measured)
        self.assertIsNone(trade.gap_fill_price)
        self.assertIsNone(trade.gap_loss_nok)
        self.assertIsNone(trade.gap_loss_pct)
        self.assertEqual(risk.loss_lines(trade)[1], "    if it gaps through: not measured for a 2.5×ATR stop")
        footer = "\n".join(risk.portfolio_lines(risk.assess([_m()], {}, [], TODAY)))
        self.assertIn("Gap risk has not been measured for a 2.5×ATR stop", footer)
        self.assertNotIn("1 stop in", footer)

    def test_each_measured_stop_quotes_its_own_numbers(self):
        notes = {}
        for multiple in (2.0, 3.0):
            self.use(risk_stop_atr_multiple=multiple)
            notes[multiple] = risk.gap_note()
            self.assertIn(f"less than {risk.gap_study().gap_below_stop_p90_pct:.1f}% below in nine of ten",
                          notes[multiple])
            self.assertIn("not a maximum", notes[multiple])
        self.assertNotEqual(notes[2.0], notes[3.0])

    def test_only_stops_placed_the_way_the_bot_places_them_count(self):
        # The 2xATR figures of research section 7 were for a stop below the buy
        # price. They are not in the table, so they can never be quoted.
        self.assertTrue(all(study.anchor == risk.STOP_ANCHOR for study in risk.GAP_STUDIES))


class BuyPriceTests(LiveSettingsCase):
    """The buy price is not known when the risk is shown. Above or below the close
    it changes the actual loss, and must never change the stop or the gap sale."""

    def _gapped(self, entry):
        # Signal close 100, ATR 2%: stop 94. Bought at `entry`, it opens at 90 next day.
        return gap_research.simulate(100.0, 2.0, 3.0, "signal-close",
                                     opens=[entry, 90.0], lows=[entry - 0.5, 89.0], closes=[entry, 91.0])

    def test_the_measured_shortfall_does_not_depend_on_the_buy_price(self):
        for entry in (98.0, 100.0, 102.0):
            trade = self._gapped(entry)
            self.assertTrue(trade["gapped"])
            self.assertAlmostEqual(trade["below_stop_pct"], (1 - 90 / 94) * 100)       # 4.26% of 94
            self.assertAlmostEqual(trade["realised_pct"], (90 / entry - 1) * 100)
        # The old measure moves with the buy price, which is why it could not be applied before a buy.
        self.assertNotAlmostEqual(self._gapped(98.0)["overshoot_from_entry_pp"],
                                  self._gapped(102.0)["overshoot_from_entry_pp"])

    def test_the_bot_reproduces_the_research_loss_at_the_reference_and_only_there(self):
        study = risk.GapStudy(multiple=3.0, anchor="signal_close", entries=1, stopped_pct=100.0,
                              gapped_of_stops_pct=100.0, gap_below_stop_mean_pct=(1 - 90 / 94) * 100,
                              gap_below_stop_p90_pct=(1 - 90 / 94) * 100,
                              gap_below_stop_worst_pct=(1 - 90 / 94) * 100)
        with patch.object(risk, "GAP_STUDIES", (study,)):
            trade = risk.pick_risk(_m(atr=2.0, close=100.0))
        self.assertAlmostEqual(trade.stop_price, 94.0)
        self.assertAlmostEqual(trade.gap_fill_price, 90.0)
        modelled = trade.gap_loss_nok / trade.exposure_nok * 100
        actual = {entry: -self._gapped(entry)["realised_pct"] for entry in (98.0, 100.0, 102.0)}
        self.assertAlmostEqual(modelled, actual[100.0])                 # 10.0%: bought at the close
        self.assertGreater(actual[102.0], modelled)                     # 11.8%: paid more
        self.assertLess(actual[98.0], modelled)                         # 8.2%: paid less

    def test_the_stop_is_measured_from_the_close_whatever_the_buy_price(self):
        stops = {entry: (1 - gap_research.simulate(100.0, 2.0, 3.0, "signal-close", [entry, entry],
                                                    [entry, entry], [entry, entry])["stop_from_entry_pct"]
                              / 100) * entry
                 for entry in (98.0, 102.0)}
        self.assertAlmostEqual(stops[98.0], 94.0)
        self.assertAlmostEqual(stops[102.0], 94.0)
        above = gap_research.simulate(100.0, 2.0, 3.0, "signal-close", [102.0], [101.0], [101.5])
        below = gap_research.simulate(100.0, 2.0, 3.0, "signal-close", [98.0], [97.0], [97.5])
        self.assertGreater(above["stop_from_entry_pct"], 6.0)          # 7.84% from 102
        self.assertLess(below["stop_from_entry_pct"], 6.0)             # 4.08% from 98

    def test_an_open_at_or_below_the_stop_is_no_trade(self):
        self.assertIsNone(gap_research.simulate(100.0, 2.0, 3.0, "signal-close", [94.0, 95.0],
                                                [93.0, 94.0], [94.5, 95.0]))
        self.assertIsNotNone(gap_research.simulate(100.0, 2.0, 3.0, "signal-close", [94.01, 95.0],
                                                   [94.01, 94.5], [94.5, 95.0]))

    def test_a_stop_hit_without_a_gap_sells_at_the_stop(self):
        trade = gap_research.simulate(100.0, 2.0, 3.0, "signal-close", [101.0, 96.0], [100.0, 93.0],
                                      [100.5, 95.0])
        self.assertTrue(trade["stopped"])
        self.assertFalse(trade["gapped"])
        self.assertEqual(trade["below_stop_pct"], 0.0)
        self.assertAlmostEqual(trade["realised_pct"], (94 / 101 - 1) * 100)

    def test_every_view_says_the_actual_risk_is_not_known(self):
        trade = risk.pick_risk(_m())
        self.assertIn("your actual risk isn't known until you buy", risk.loss_lines(trade)[0])
        footer = "\n".join(risk.portfolio_lines(risk.assess([_m()], {}, [], TODAY)))
        self.assertIn("the actual risk of a position isn't known until it is bought", footer)
        saved = risk.snapshot(trade)
        self.assertEqual(saved["loss_basis"], "if_bought_at_reference_price")
        self.assertIsNone(saved["entry_price"])


class ResearchMethodTests(unittest.TestCase):
    """The gap study screens sessions the way the live screen does."""

    def _bars(self, sessions=160, seed=3):
        import numpy as np
        rng = np.random.default_rng(seed)
        close = 100 * np.cumprod(1 + rng.normal(0.003, 0.015, sessions))
        spread = np.abs(rng.normal(0, 0.01, sessions)) * close
        index = pd.bdate_range("2025-01-02", periods=sessions)
        return pd.DataFrame({"Open": close * (1 + rng.normal(0, 0.004, sessions)), "High": close + spread,
                             "Low": close - spread, "Close": close,
                             "Volume": rng.integers(1_000_000, 3_000_000, sessions).astype(float)}, index=index)

    def test_the_ema_is_seeded_where_the_live_screen_seeds_it(self):
        close = self._bars()["Close"]
        ema = gap_research.screen_ema20(close)
        for position in (10, 61, 62, 63, 100, 159):
            window = close.iloc[max(0, position - gap_research.SCREEN_SESSIONS + 1):position + 1]
            expected = window.ewm(span=20, adjust=False).mean().iloc[-1]
            self.assertAlmostEqual(ema.iloc[position], expected, places=9)

    def test_the_study_uses_the_screens_ema_unless_asked_not_to(self):
        # A seed ten years back differs by about 0.2% from one 3 months back, too
        # little to flip a gate on test data, so the wiring is checked directly.
        bars = self._bars()
        with patch.object(gap_research, "screen_ema20", wraps=gap_research.screen_ema20) as ema:
            gap_research.qualifying(bars)
            self.assertEqual(ema.call_count, 1)
            gap_research.qualifying(bars, full_history_ema=True)
            self.assertEqual(ema.call_count, 1)

    def test_the_gates_match_the_live_screen_session_by_session(self):
        bars = self._bars()
        passes, atr_pct = gap_research.qualifying(bars)
        defaults = nb_config.AppSettings(_env_file=None)
        self.assertEqual(gap_research.GATES["week"], defaults.trade_min_week_momentum_pct)
        self.assertEqual(gap_research.GATES["day"], defaults.trade_min_day_change_pct)
        self.assertEqual(gap_research.GATES["atr"], defaults.trade_max_atr_pct)
        self.assertEqual(gap_research.GATES["dd"], defaults.trade_max_drawdown_pct)
        compared = qualified = 0
        for position in range(len(bars)):
            history = bars.iloc[max(0, position - gap_research.SCREEN_SESSIONS + 1):position + 1]
            with patch.object(md, "_fetch_ticker_history", return_value=history), \
                 patch.object(nb_indicators, "_drop_in_progress_bar", side_effect=lambda h: h):
                metrics = nb_indicators._compute_trade_metrics("X")
            live = metrics is not None and not nb_indicators._failed_gates(metrics)
            self.assertEqual(bool(passes.iloc[position]), live, position)
            if metrics is not None:
                self.assertAlmostEqual(metrics.atr_pct, atr_pct.iloc[position], places=9)
                compared += 1
            qualified += live
        self.assertGreater(compared, 100)
        self.assertGreater(qualified, 10)


class ReproducibilityTests(unittest.TestCase):
    def test_every_gap_row_is_the_saved_research_output(self):
        for study in risk.GAP_STUDIES:
            path = os.path.join(ROOT, "docs", "research", "data",
                                f"gap_risk_{study.multiple:.1f}x_{study.anchor.replace('_', '-')}.txt")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn(f"research/gap_risk.py basket.pkl {study.multiple:.1f} "
                          f"{study.anchor.replace('_', '-')}\n", text)
            last = [line for line in text.splitlines() if line.startswith("GapStudy(")][-1]
            call = ast.parse(last).body[0].value
            saved = {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords}
            self.assertEqual(saved, {name: getattr(study, name) for name in saved})
            self.assertEqual(set(saved), set(risk.GapStudy.__dataclass_fields__))


class CostTests(LiveSettingsCase):
    def test_fee_is_the_broker_table_for_a_buy_and_a_sell(self):
        trade = risk.pick_risk(_m("EQNR.OL"))
        self.assertAlmostEqual(trade.fee_nok, 118.0)                    # 2 x the 59 NOK minimum
        self.assertAlmostEqual(trade.fee_pct, 0.236)
        self.assertIsNone(trade.fx_cost_included)                       # Oslo: nothing to exchange
        self.assertEqual(risk.fee_line("EQNR.OL", trade.exposure_nok, trade.account_nok),
                         "    fees: 118 NOK to buy and sell (0.9% of 12,500 NOK, 0.24% of 50,000 NOK)")

    def test_the_percentage_rate_takes_over_on_a_large_position(self):
        self.assertAlmostEqual(risk.round_trip_fee(100_000.0), 240.0)

    def test_currency_exchange_cost_is_named_but_never_added(self):
        trade = risk.pick_risk(_m("AAPL"))
        self.assertIs(trade.fx_cost_included, False)
        self.assertAlmostEqual(trade.fee_nok, 118.0)
        self.assertTrue(risk.fee_line("AAPL", 12_500.0, 50_000.0).endswith("+ currency exchange fee"))

    def test_no_amount_means_no_fee_figure(self):
        self.use(risk_account_size=0.0)
        trade = risk.pick_risk(_m("EQNR.OL"))
        self.assertIsNone(trade.fee_nok)
        self.assertIsNone(trade.fee_pct)
        self.assertEqual(risk.fee_line("EQNR.OL", None), "    fees: at least 118 NOK to buy and sell")

    def test_costs_are_not_folded_into_the_loss_at_the_stop(self):
        trade = risk.pick_risk(_m())
        self.assertAlmostEqual(trade.stop_loss_nok, trade.exposure_nok * trade.stop_distance_pct / 100)
        self.assertIn("before costs", risk.loss_lines(trade)[0])


class MissingAtrTests(LiveSettingsCase):
    def test_no_usable_atr_means_no_risk_numbers(self):
        for atr in (None, 0.0, -1.0, float("nan"), float("inf")):
            metrics = _m(atr=atr)
            self.assertIsNone(risk.trade_risk(metrics, exposure_nok=10_000.0, account_nok=50_000.0))
            self.assertIsNone(risk.pick_risk(metrics))
            self.assertIsNone(risk.snapshot(risk.pick_risk(metrics)))

    def test_a_bad_close_or_a_bad_multiple_means_no_stop(self):
        self.assertIsNone(risk.pick_risk(_m(close=0.0)))
        self.assertIsNone(risk.pick_risk(_m(close=float("nan"))))
        self.use(risk_stop_atr_multiple=0.0)
        self.assertIsNone(risk.pick_risk(_m()))

    def test_the_lines_fall_back_to_fees_only(self):
        context = risk.assess([_m(atr=0.0)], {}, [], TODAY)
        self.assertEqual(context.trades, {})
        self.assertEqual(risk.candidate_lines("AAA", context),
                         ["    fees: at least 118 NOK to buy and sell + currency exchange fee",
                          "    ⛔ Not for ASK (US stock)"])

    def test_check_says_there_is_no_stop(self):
        with patch.object(trade_check.indicators, "_compute_trade_metrics",
                          return_value=_m("EQNR.OL", atr=float("nan"), close=250.0)), \
             patch.object(trade_check, "_account_value", return_value=(50_000.0, 50_000.0)), \
             patch.object(trade_check, "_profile", return_value={}), \
             patch.object(trade_check.market_data, "_next_ex_dividend_date", return_value=None), \
             patch.object(fills, "current_holdings", return_value={}):
            text = trade_check.build_check("EQNR.OL 12500", today=TODAY)
        self.assertIn("No stop: the stock's daily swing (ATR) isn't available.", text)
        self.assertNotIn("loss at the stop", text)

    def test_bad_amounts_are_dropped_not_used(self):
        trade = risk.trade_risk(_m(), exposure_nok=float("nan"), account_nok=-5.0)
        self.assertIsNone(trade.exposure_nok)
        self.assertIsNone(trade.account_nok)
        self.assertIsNone(trade.stop_loss_nok)
        self.assertEqual(risk.loss_lines(trade), [])


class LiquidityTests(LiveSettingsCase):
    def test_a_normally_traded_stock_has_no_warning(self):
        self.assertEqual(risk.pick_risk(_m()).warnings, ())

    def test_thin_trading_is_flagged_with_the_backtests_floor(self):
        trade = risk.pick_risk(_m(avg_volume_20=40_000.0))
        self.assertEqual(len(trade.warnings), 1)
        self.assertIn("Thin trading: about 40,000 shares a day, under the 100,000", trade.warnings[0])
        lines = risk.candidate_lines("AAA", risk.assess([_m(avg_volume_20=40_000.0)], {}, [], TODAY))
        self.assertTrue(any(line.startswith("    ⚠️ Thin trading") for line in lines))

    def test_missing_volume_is_said_not_assumed_fine(self):
        trade = risk.pick_risk(_m(volume_ratio=None, avg_volume_20=None))
        self.assertEqual(trade.warnings, ("No volume data: how easily this stock trades was not checked.",))

    def test_the_floor_can_be_switched_off(self):
        self.use(trade_backtest_min_avg_volume=0.0)
        self.assertEqual(risk.pick_risk(_m(avg_volume_20=10.0)).warnings, ())

    def test_a_warning_never_changes_the_size_or_the_gates(self):
        thin, normal = _m(avg_volume_20=40_000.0), _m()
        self.assertEqual(risk.size_position(thin), risk.size_position(normal))
        self.assertEqual(nb_indicators._failed_gates(thin), nb_indicators._failed_gates(normal))
        self.assertEqual(nb_indicators._failed_gates(thin), [])

    def test_the_average_volume_is_measured_from_the_prior_20_sessions(self):
        index = pd.bdate_range(end="2026-11-12", periods=40, tz="America/New_York")
        history = pd.DataFrame({
            "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": [100.0 + i * 0.1 for i in range(40)],
            "Volume": [50_000.0] * 39 + [900_000.0]}, index=index)
        with patch.object(md, "_fetch_ticker_history", return_value=history), \
             patch.object(nb_indicators, "_drop_in_progress_bar", side_effect=lambda h: h):
            metrics = nb_indicators._compute_trade_metrics("THIN")
        self.assertAlmostEqual(metrics.avg_volume_20, 50_000.0)
        self.assertAlmostEqual(metrics.volume_ratio, 18.0)


class StoreCase(LiveSettingsCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(tempfile.mkdtemp(), "h.db")
        self.store = use_throwaway_store(self.addCleanup, self.path)

    def table(self):
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
            return views.pick_risk(pd.read_sql_query(views.PICK_RISK_SQL, conn))

    def raw_rows(self):
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
            return conn.execute("SELECT * FROM published_picks ORDER BY symbol, signal_session").fetchall()

    def insert_old_pick(self, symbol="OLD", outcome=None):
        """A row as the bot wrote it before the risk record existed."""
        snapshot = {"rank": 1, "metrics": {"symbol": symbol, "last_close": 50.0, "atr_pct": 2.0},
                    "gates": {"trade_max_atr_pct": 4.5}}
        with self.store._connection() as conn:
            conn.execute(
                "INSERT INTO published_picks (symbol, signal_session, published_at, horizon_sessions,"
                " cost_bps_per_side, benchmark_symbol, snapshot_json, outcome_json)"
                " VALUES (?, '2026-10-01', '2026-10-02T05:00:00+00:00', 5, 10.0, 'SPY', ?, ?)",
                (symbol, json.dumps(snapshot), json.dumps(outcome) if outcome else None))


class DashboardConsistencyTests(StoreCase):
    def test_the_dashboard_shows_the_numbers_the_message_showed(self):
        metrics = _m("EQNR.OL", atr=2.0, close=250.0)
        self.store.publish([metrics], SENT)
        trade = risk.assess([metrics], {}, [], TODAY).trades["EQNR.OL"]
        row = self.table().iloc[0]
        self.assertEqual(row["record"], views.RISK_RECORDED)
        for name in views.RISK_FIELDS:
            self.assertAlmostEqual(row[name], getattr(trade, name), msg=name)
        message = "\n".join(risk.candidate_lines("EQNR.OL", risk.assess([metrics], {}, [], TODAY)))
        for shown in (f"volatility stop {row['stop_price']:,.2f}",
                      f"{row['atr_multiple']:g}×ATR below the {row['reference_price']:,.2f} signal close",
                      f"{row['stop_distance_price']:,.2f} ({row['stop_distance_pct']:.1f}%) away",
                      f"size: {row['exposure_pct']:.0f}% of the portfolio ≈ {row['exposure_nok']:,.0f} NOK",
                      f"loss if bought at the {row['reference_price']:,.2f} close: {row['stop_loss_pct']:.2f}% "
                      f"of the portfolio ≈ {row['stop_loss_nok']:,.0f} NOK at the stop",
                      f"sold near {row['gap_fill_price']:,.2f}, about {row['gap_loss_pct']:.2f}% of the "
                      f"portfolio ≈ {row['gap_loss_nok']:,.0f} NOK from that close",
                      f"fees: {row['fee_nok']:,.0f} NOK to buy and sell"):
            self.assertIn(shown, message)
        self.assertEqual(row["gap"], "estimate: sold 2.75% below the stop (nine in ten did better)")
        self.assertEqual(row["actual"], "not known: no buy price is linked to a sent pick")
        self.assertEqual(row["costs"], "estimated broker fee")

    def test_the_dashboard_knows_the_record_layout_the_bot_writes(self):
        self.assertEqual(views.RISK_MODEL_VERSION, risk.RISK_MODEL_VERSION)

    def test_a_record_of_another_layout_is_listed_without_numbers(self):
        frame = pd.DataFrame([{"sent_on": "2026-11-13", "symbol": "V1", "signal_session": "2026-11-12",
                               "snapshot_json": json.dumps({"risk": {"model_version": 1, "stop_price": 94.0,
                                                                     "gap_overshoot_pp": 2.55}})}])
        row = views.pick_risk(frame).iloc[0]
        self.assertEqual(row["record"], "risk model v1: not shown")
        self.assertTrue(pd.isna(row["stop_price"]))

    def test_the_record_names_its_model_and_anchor(self):
        self.store.publish([_m("AAPL", avg_volume_20=40_000.0)], SENT)
        saved = json.loads(self.store.published_picks()[0]["snapshot_json"])["risk"]
        self.assertEqual(saved["model_version"], risk.RISK_MODEL_VERSION)
        self.assertEqual(saved["stop_anchor"], "signal_close")
        self.assertEqual(saved["loss_basis"], "if_bought_at_reference_price")
        self.assertIsNone(saved["entry_price"])
        self.assertEqual(saved["atr_multiple"], 3.0)
        row = self.table().iloc[0]
        self.assertEqual(row["costs"], "estimated broker fee; currency exchange not included")
        self.assertIn("Thin trading", row["warnings"])

    def test_an_unmeasured_stop_is_labelled_on_the_dashboard_too(self):
        self.use(risk_stop_atr_multiple=2.5)
        self.store.publish([_m()], SENT)
        row = self.table().iloc[0]
        self.assertEqual(row["gap"], "not measured for 2.5×ATR")
        self.assertTrue(pd.isna(row["gap_loss_nok"]))
        self.assertTrue(pd.isna(row["gap_fill_price"]))

    def test_no_risk_record_when_the_risk_lines_were_not_sent(self):
        self.use(risk_analytics_enabled=False)
        self.store.publish([_m()], SENT)
        self.assertIsNone(json.loads(self.store.published_picks()[0]["snapshot_json"])["risk"])
        self.assertEqual(self.table().iloc[0]["record"], views.RISK_NOT_RECORDED)

    def test_a_pick_without_a_usable_atr_is_still_published(self):
        self.store.publish([_m(atr=0.0)], SENT)
        self.assertEqual(len(self.store.published_picks()), 1)
        self.assertEqual(self.table().iloc[0]["record"], views.RISK_NOT_RECORDED)


class HistoryTests(StoreCase):
    def test_old_picks_stay_byte_for_byte_and_show_not_recorded(self):
        self.insert_old_pick("OLD", outcome={"net_return_pct": 1.25, "excess_return_pct": 0.4})
        before = self.raw_rows()
        self.store.publish([_m("NEW")], SENT)
        # Publishing the old pick again, with today's settings, must not touch it.
        self.store.publish([_m("OLD", close=50.0, session="2026-10-01")], SENT)
        self.store.close()
        reopened = nb_history_store.TradeHistoryStore(self.path)
        self.addCleanup(reopened.close)
        reopened.published_picks()
        after = [row for row in self.raw_rows() if row[0] == "OLD"]
        self.assertEqual(after, before)
        table = self.table().set_index("symbol")
        self.assertEqual(table.loc["OLD", "record"], views.RISK_NOT_RECORDED)
        self.assertTrue(table.loc["OLD", views.RISK_FIELDS].isna().all())
        self.assertEqual(table.loc["NEW", "record"], views.RISK_RECORDED)

    def test_a_saved_record_does_not_follow_later_setting_changes(self):
        self.store.publish([_m("AAA")], SENT)
        before = self.raw_rows()
        self.use(risk_stop_atr_multiple=2.0, risk_per_trade_pct=0.5, risk_account_size=10_000.0)
        self.store.publish([_m("AAA")], SENT)
        self.assertEqual(self.raw_rows(), before)
        row = self.table().iloc[0]
        self.assertEqual(row["atr_multiple"], 3.0)
        self.assertAlmostEqual(row["stop_price"], 94.0)

    def test_no_schema_change_and_opening_twice_changes_nothing(self):
        self.insert_old_pick()

        def schema():
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
                return conn.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()

        first, rows = schema(), self.raw_rows()
        for _ in range(2):
            self.store.close()
            self.store.published_picks()       # reopens and runs every CREATE / ALTER again
        self.assertEqual(schema(), first)
        self.assertEqual(self.raw_rows(), rows)
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(published_picks)")]
        self.assertEqual(columns, ["symbol", "signal_session", "published_at", "horizon_sessions",
                                   "cost_bps_per_side", "benchmark_symbol", "snapshot_json",
                                   "outcome_json"])

    def test_a_garbled_snapshot_is_shown_as_not_recorded(self):
        frame = pd.DataFrame([{"sent_on": "2026-10-02", "symbol": "BAD", "signal_session": "2026-10-01",
                               "snapshot_json": "not json"},
                              {"sent_on": "2026-10-02", "symbol": "NULL", "signal_session": "2026-10-01",
                               "snapshot_json": None}])
        self.assertEqual(list(views.pick_risk(frame)["record"]), [views.RISK_NOT_RECORDED] * 2)
        self.assertEqual(list(views.pick_risk(frame.iloc[0:0]).columns), views.RISK_COLUMNS)


class OneStopTests(StoreCase):
    def test_analyze_shows_one_stop_not_two(self):
        with patch("newsbot.indicators._compute_trade_metrics", return_value=_m("AAA")), \
             patch.object(md, "_fetch_ticker_fundamentals", return_value={"longName": "Alpha"}), \
             patch.object(md, "_fetch_event_profile", return_value={}):
            report = nb_reports.build_ticker_analysis("AAA")
        self.assertIn("invalidation below 94.00 (stop)", report)
        self.assertIn("volatility stop 94.00, 3×ATR below the 100.00 signal close", report)
        self.assertNotIn("(20-day low)", report)

    def test_recommendations_carry_every_risk_line_per_pick(self):
        outcome = nb_screener.ScreenOutcome(checked=2, analysed=2, qualified=[_m("AAA"), _m("EQNR.OL")])
        with patch.object(nb_screener, "_screen_universe", return_value=(outcome, {})), \
             patch.object(md, "_fetch_event_profile", return_value={}), \
             patch.object(risk, "correlated_pairs", return_value=[]), \
             patch.object(nb_history_store.TRADE_HISTORY, "archive_ranks"):
            text, _, _ = nb_screener.build_trade_candidates_payload(universe={"A": "AAA", "E": "EQNR.OL"})
        for needle in ("volatility stop 94.00, 3×ATR below the 100.00 signal close", "size: 25% of the portfolio",
                       "loss if bought at the 100.00 close: 1.50% of the portfolio ≈ 750 NOK at the stop",
                       "sold near 91.42", "(estimate, not a maximum)", "your actual risk isn't known until you buy",
                       "fees: 118 NOK to buy and sell"):
            self.assertEqual(text.count(needle), 2, needle)
        self.assertIn("3×ATR stop below the signal close", text)
        self.assertIn("Amounts use a portfolio of 50,000 NOK (RISK_ACCOUNT_SIZE).", text)
        self.assertIn("about 1 stop in 7 opened below its price", text)


if __name__ == "__main__":
    unittest.main()
