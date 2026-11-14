"""P2-5: the FX cost sensitivity in research/gap_risk.py and research/hold_exit_study.py.

The research scripts are not part of the bot. These tests pin the arithmetic
of the shared cost model and that adding it changed nothing the scripts
already reported. Fixed numbers only: no market data, no network.
"""

import importlib.util
import os
import unittest

import numpy as np
import pandas as pd

from newsbot import fills

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    # By path: research/ is not a package.
    spec = importlib.util.spec_from_file_location(f"research_{name}", os.path.join(ROOT, "research", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gap = _load("gap_risk")
hold = _load("hold_exit_study")
fx = gap.fx_costs
BPS = [bps for _name, bps in fx.FX_SCENARIOS]


def _bars(seed=7, sessions=90):
    """A fixed random walk of daily bars."""
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0005, 0.015, sessions))
    open_ = close * (1 + rng.normal(0, 0.004, sessions))
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.01,
                         "Low": np.minimum(open_, close) * 0.99, "Close": close},
                        index=pd.bdate_range("2024-01-01", periods=sessions))


def _hold_frame():
    rows = []
    for symbol, region, seed in (("EQNR.OL", "Oslo", 1), ("ASML.AS", "EU", 2), ("MSFT", "US", 3)):
        rows.extend(hold._trades(symbol, _bars(seed), region))
    frame = pd.DataFrame(rows, columns=["symbol", "region", "date", "hold", "rule", "gross", "net",
                                        "stopped", "gapped"])
    frame["currency"] = [fx.currency_of(symbol) for symbol in frame["symbol"]]
    return frame


def _gap_frame():
    return pd.DataFrame({
        "symbol": ["EQNR.OL", "MSFT", "ASML.AS", "MSFT", "EQNR.OL"],
        "currency": ["NOK", "USD", "EUR", "USD", "NOK"],
        "realised_pct": [4.0, -6.5, 10.0, -9.0, -6.0],
        "stopped": [False, True, False, True, True],
        "gapped": [False, False, False, True, False],
    })


class ScenarioTests(unittest.TestCase):
    def test_the_four_scenarios_are_the_agreed_ones(self):
        self.assertEqual(fx.FX_SCENARIOS, (("zero-cost baseline", 0.0), ("low", 10.0),
                                           ("medium", 25.0), ("high", 50.0)))

    def test_both_scripts_use_the_same_scenarios_and_the_same_model(self):
        self.assertIs(gap.fx_costs, hold.fx_costs)
        self.assertIs(gap.fx_costs.FX_SCENARIOS, hold.fx_costs.FX_SCENARIOS)
        # Neither script keeps a cost table or formula of its own.
        for script in ("gap_risk.py", "hold_exit_study.py"):
            with open(os.path.join(ROOT, "research", script), encoding="utf-8") as handle:
                source = handle.read()
            self.assertIn("fx_costs.add_fx_columns(", source)
            self.assertNotIn("10_000", source)
            self.assertNotIn("bps =", source)


class CostModelTests(unittest.TestCase):
    def test_zero_cost_is_exactly_no_cost(self):
        returns = np.array([-35.0, -0.3, 0.0, 0.1 + 0.2, 12.5])
        cost = fx.fx_cost_pct(returns, [True] * 5, 0.0)
        self.assertTrue((cost == 0.0).all())
        self.assertTrue((returns - cost == returns).all())   # bit for bit, not nearly

    def test_a_nok_stock_is_never_charged(self):
        for bps in BPS:
            self.assertEqual(fx.fx_cost_pct(10.0, fx.is_converted("NOK"), bps), 0.0)
        self.assertFalse(fx.is_converted(fx.currency_of("EQNR.OL")))
        self.assertFalse(fx.is_converted(fx.currency_of("ANY", "nok")))

    def test_both_conversions_are_charged_and_the_second_on_the_proceeds(self):
        # 100 NOK, 25 bps, +10% in the stock's currency, walked through by hand.
        c = 0.0025
        bought = 100.0 * (1 - c)            # NOK -> foreign at the buy
        proceeds = bought * 1.10            # the sale
        back = proceeds * (1 - c)           # foreign -> NOK
        expected = 110.0 - back
        self.assertAlmostEqual(float(fx.fx_cost_pct(10.0, True, 25.0)), expected, places=12)
        self.assertAlmostEqual(expected, 0.5493125, places=7)
        one_leg = 100.0 * c
        self.assertGreater(expected, 2 * one_leg)          # two legs, the second on 110, not 100
        self.assertLess(float(fx.fx_cost_pct(-10.0, True, 25.0)), 2 * one_leg)   # and on 90 after a loss
        # A flat trade pays both legs: 2c less the c*c that the second leg does not see.
        self.assertAlmostEqual(float(fx.fx_cost_pct(0.0, True, 25.0)), (2 * c - c * c) * 100, places=12)

    def test_a_higher_cost_never_leaves_more(self):
        returns = np.array([-99.0, -20.0, 0.0, 3.0, 40.0])
        after = [returns - fx.fx_cost_pct(returns, [True] * 5, bps) for bps in (0, 1, 10, 25, 50, 200)]
        for lower, higher in zip(after, after[1:], strict=False):
            self.assertTrue((higher < lower).all())
        home = [returns - fx.fx_cost_pct(returns, [False] * 5, bps) for bps in (0, 50)]
        self.assertTrue((home[0] == home[1]).all())

    def test_a_cost_below_zero_or_not_a_number_is_refused(self):
        for bad in (-1.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                fx.fx_cost_pct(1.0, True, bad)


class CurrencyTests(unittest.TestCase):
    def test_the_lists_own_column_wins_then_the_suffix(self):
        self.assertEqual(fx.currency_of("FRO.OL", "USD"), "USD")
        self.assertEqual(fx.currency_of("FRO.OL", ""), "NOK")
        self.assertEqual(fx.currency_of("FRO.OL", None), "NOK")
        self.assertEqual(fx.currency_of("MSFT"), "USD")
        self.assertEqual(fx.currency_of("RELIANCE.NS"), "INR")

    def test_the_suffixes_agree_with_the_bots_own_map(self):
        for suffix, currency in fills._SUFFIX_CURRENCY.items():
            self.assertEqual(fx.SUFFIX_CURRENCY[suffix], currency)

    def test_an_unknown_currency_is_named_charged_and_reported(self):
        self.assertEqual(fx.currency_of("ABC.XX"), fx.UNKNOWN)
        self.assertTrue(fx.is_converted(fx.UNKNOWN))
        frame = pd.DataFrame({"symbol": ["ABC.XX", "EQNR.OL"], "currency": [fx.UNKNOWN, "NOK"],
                              "gross": [5.0, 5.0]})
        fx.add_fx_columns(frame, "gross", "gross")
        self.assertGreater(frame[fx.column("fx_cost", 25.0)][0], 0)
        self.assertEqual(frame[fx.column("fx_cost", 25.0)][1], 0)
        text = "\n".join(fx.describe(frame))
        self.assertIn("Currency unknown for 1 trades (ABC.XX): charged as foreign.", text)
        self.assertIn("Charged: 1 of 2 trades. Not charged: 1 priced in NOK.", text)

    def test_a_trade_with_no_currency_at_all_stops_the_run(self):
        frame = pd.DataFrame({"symbol": ["A"], "currency": [None], "gross": [1.0]})
        with self.assertRaises(ValueError):
            fx.add_fx_columns(frame, "gross", "gross")


class NoDoubleCountingTests(unittest.TestCase):
    def test_a_frame_is_never_charged_twice(self):
        frame = fx.add_fx_columns(_gap_frame(), "realised_pct", "realised_pct")
        with self.assertRaises(ValueError):
            fx.add_fx_columns(frame, "realised_pct", "realised_pct")
        with self.assertRaises(ValueError):   # nor by using an after-FX column as the start
            fx.add_fx_columns(frame, "realised_pct", fx.column("after_fx", 25.0))

    def test_scenarios_do_not_stack(self):
        frame = fx.add_fx_columns(_gap_frame(), "realised_pct", "realised_pct")
        converted = frame["currency"].map(fx.is_converted)
        for bps in BPS:
            alone = frame["realised_pct"] - fx.fx_cost_pct(frame["realised_pct"], converted, bps)
            self.assertTrue((frame[fx.column("after_fx", bps)] == alone).all(), bps)

    def test_the_hold_study_takes_the_fee_once_and_the_fx_cost_once(self):
        frame = fx.add_fx_columns(_hold_frame(), "gross", "net")
        for bps in BPS:
            expected = frame["gross"] - 2 * hold.FEE_SIDE_PCT - frame[fx.column("fx_cost", bps)]
            self.assertTrue(np.allclose(frame[fx.column("after_fx", bps)], expected, rtol=0, atol=1e-12), bps)
        # The conversions are sized by the price return, not by the return after the fee.
        foreign = frame[frame["currency"] != "NOK"]
        again = fx.fx_cost_pct(foreign["gross"], True, 50.0)
        self.assertTrue((foreign[fx.column("fx_cost", 50.0)] == again).all())


class HoldExitStudyTests(unittest.TestCase):
    def test_the_fee_is_what_it_was(self):
        self.assertEqual((hold.POSITION_NOK, hold.FEE_MIN_NOK, hold.FEE_PCT), (12_000.0, 59.0, 0.12))
        self.assertAlmostEqual(hold.FEE_SIDE_PCT, 59.0 / 12_000.0 * 100, places=12)
        frame = _hold_frame()
        self.assertGreater(len(frame), 100)
        self.assertTrue((frame["net"] == frame["gross"] - 2 * hold.FEE_SIDE_PCT).all())

    def test_the_fx_section_leaves_the_frame_and_the_old_table_alone(self):
        frame = _hold_frame()
        before, table = frame.copy(), hold._table(frame)
        hold.fx_sensitivity(frame)
        pd.testing.assert_frame_equal(frame, before)
        pd.testing.assert_frame_equal(hold._table(frame), table)

    def test_zero_cost_reproduces_the_net_columns(self):
        frame = _hold_frame()
        table = hold._table(frame)
        charged = fx.add_fx_columns(frame.copy(), "gross", "net")
        self.assertTrue((charged[fx.column("after_fx", 0.0)] == frame["net"]).all())
        zero = hold._fx_table(charged, 0.0)
        for new, old in (("after fees %", "net %"), ("after fees+FX %", "net %"), ("gross %", "gross %"),
                         ("median", "median net %"), ("worst 5%", "worst 5% net"), ("> 0", "net > 0"),
                         ("per 20 sessions", "net per 20 sessions")):
            pd.testing.assert_series_equal(zero[new], table[old], check_names=False)
        self.assertTrue((zero["FX cost %"] == 0).all())
        self.assertTrue((zero["total FX cost NOK"] == 0).all())

    def test_only_the_foreign_stocks_pay_and_more_cost_means_less_left(self):
        charged = fx.add_fx_columns(_hold_frame(), "gross", "net")
        by_currency = charged.groupby("currency")
        self.assertEqual(by_currency[fx.column("fx_cost", 50.0)].sum()["NOK"], 0.0)
        self.assertTrue((charged.loc[charged["currency"] != "NOK", fx.column("fx_cost", 10.0)] > 0).all())
        tables = [hold._fx_table(charged, bps) for bps in BPS]
        for lower, higher in zip(tables, tables[1:], strict=False):
            self.assertTrue((higher["after fees+FX %"] < lower["after fees+FX %"]).all())
            self.assertTrue((higher["total FX cost NOK"] > lower["total FX cost NOK"]).all())
        # 2 of the 3 stocks are foreign, and the table says so.
        self.assertTrue((tables[1]["charged"] * 3 == tables[1]["trades"] * 2).all())

    def test_the_section_names_every_scenario_and_what_each_column_is(self):
        text = "\n".join(hold.fx_sensitivity(_hold_frame()))
        for needle in ("illustrative; not verified broker pricing", "ZERO-COST BASELINE: 0 bps per conversion",
                       "LOW: 10 bps per conversion", "MEDIUM: 25 bps per conversion",
                       "HIGH: 50 bps per conversion", "gross %", "after fees %", "after fees+FX %", "vs 0 bps",
                       "total FX cost NOK", "Currency unknown: 0 trades."):
            self.assertIn(needle, text)


class GapRiskTests(unittest.TestCase):
    def test_the_fx_section_leaves_the_trades_alone(self):
        trades = _gap_frame()
        before = trades.copy()
        gap.fx_sensitivity(trades)
        pd.testing.assert_frame_equal(trades, before)

    def test_the_rows_are_the_hand_worked_numbers(self):
        lines = gap.fx_sensitivity(_gap_frame())
        self.assertEqual(len([line for line in lines if line.startswith(("zero-cost", "low", "medium", "high"))]), 8)
        returns = np.array([4.0, -6.5, 10.0, -9.0, -6.0])
        converted = np.array([False, True, True, True, False])
        gross = returns.mean()
        table = [line.split() for line in lines if line.startswith(("zero-cost", "low", "medium", "high"))][:4]
        for row, bps in zip(table, BPS, strict=True):
            c = bps / 10_000
            cost = np.where(converted, (1 + returns / 100) * (2 * c - c * c) * 100, 0.0)
            self.assertEqual(row[-7:], [f"{bps:g}", f"{gross:.2f}", f"{gross:.2f}", f"{cost.mean():.3f}",
                                        f"{(returns - cost).mean():.2f}",
                                        f"{(returns - cost).mean() - gross:.3f}",
                                        f"{cost.sum() / 100:.1f}"])
        # Zero cost: after FX is the realised return the script already had.
        self.assertEqual(table[0][-4:-1], ["0.000", f"{gross:.2f}", "0.000"])

    def test_the_stop_losses_grow_with_the_cost_for_foreign_entries(self):
        lines = gap.fx_sensitivity(_gap_frame())
        table = [line.split() for line in lines if line.startswith(("zero-cost", "low", "medium", "high"))][4:]
        stopped = [float(row[-3]) for row in table]
        all_stops = [float(row[-1]) for row in table]
        self.assertEqual(stopped[0], 7.75)                 # (6.5 + 9.0) / 2, the two foreign stops
        self.assertEqual(all_stops[0], round((6.5 + 9.0 + 6.0) / 3, 2))
        self.assertEqual(stopped, sorted(stopped))
        self.assertLess(stopped[0], stopped[-1])
        self.assertTrue(all(row[-6] == "3" for row in table))   # three charged entries

    def test_the_gap_measures_do_not_depend_on_currency(self):
        usd = gap.simulate(100.0, 2.0, 3.0, "signal-close", [101.0, 90.0], [100.0, 89.0], [100.5, 89.5])
        self.assertEqual(set(usd), {"stopped", "gapped", "stop_from_reference_pct", "stop_from_entry_pct",
                                    "realised_pct", "below_stop_pct", "overshoot_from_entry_pp"})
        self.assertAlmostEqual(usd["realised_pct"], (90.0 / 101.0 - 1) * 100)
        self.assertAlmostEqual(usd["below_stop_pct"], (1 - 90.0 / 94.0) * 100)


if __name__ == "__main__":
    unittest.main()
