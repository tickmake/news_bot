"""Risk analytics: sizing, event flags, correlation, clustering, rendering, lookups."""

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from newsbot import config as nb_config
from newsbot import history_store as nb_history_store
from newsbot import indicators as nb_indicators
from newsbot import market_data as md
from newsbot import reports as nb_reports
from newsbot import risk
from newsbot import risk_forecast as nb_risk_forecast
from newsbot import screener as nb_screener
from newsbot import state as nb_state

TODAY = date(2026, 10, 5)


def _m(symbol="AAA", atr=2.0, close=100.0, **overrides):
    base = {
        "symbol": symbol, "session": "2026-10-02", "last_close": close, "day_change_pct": 1.0,
        "week_momentum_pct": 3.0, "volume_ratio": 1.5, "drawdown_pct": 2.0, "atr_pct": atr,
        "above_ema20": True, "ema20": 98.0, "rsi_14": 60.0, "support_level": 95.0,
        "resistance_level": 104.0, "volume_trend": "flat",
    }
    base.update(overrides)
    return nb_indicators.TradeMetrics(**base)


def _history(returns, start_price=100.0, days_ago=10, tz="UTC"):
    prices = start_price * np.cumprod(1 + np.asarray(returns))
    end = (datetime.now() - timedelta(days=days_ago)).date()
    index = pd.bdate_range(end=end, periods=len(prices), tz=tz)
    return pd.DataFrame({"Close": prices}, index=index)


class SettingsCase(unittest.TestCase):
    def setUp(self):
        # Relative strength fetches six months of history for the stock, its market
        # proxy and its sector ETF. These tests are about the other risk analytics
        # and must never reach yfinance; test_relative_strength.py covers that part.
        self.use(relative_strength_enabled=False, catalysts_enabled=False, risk_forecast_enabled=False)

    def use(self, **values):
        for name, value in values.items():
            patcher = patch.object(nb_config.SETTINGS, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class SizingTests(SettingsCase):
    def test_weight_is_risk_budget_over_stop_distance(self):
        sized = risk.size_position(_m(atr=2.0))
        self.assertAlmostEqual(sized.stop_pct, 4.0)
        self.assertAlmostEqual(sized.stop_price, 96.0)
        self.assertAlmostEqual(sized.weight_pct, 12.5)      # 0.5% risk / 4% stop
        self.assertAlmostEqual(sized.risk_pct, 0.5)
        self.assertFalse(sized.capped)
        self.assertIsNone(sized.amount)

    def test_wilder_stocks_get_smaller_positions(self):
        calm, wild = risk.size_position(_m(atr=1.5)), risk.size_position(_m(atr=4.0))
        self.assertGreater(calm.weight_pct, wild.weight_pct)
        self.assertAlmostEqual(calm.risk_pct, wild.risk_pct)

    def test_cap_limits_a_very_calm_stock(self):
        sized = risk.size_position(_m(atr=0.3))
        self.assertEqual(sized.weight_pct, 20.0)
        self.assertTrue(sized.capped)
        self.assertLess(sized.risk_pct, 0.5)

    def test_account_size_adds_an_amount(self):
        self.use(risk_account_size=100_000.0)
        self.assertAlmostEqual(risk.size_position(_m(atr=2.0)).amount, 12_500.0)

    def test_unusable_inputs_give_none(self):
        for atr in (0.0, -1.0, float("nan"), None):
            self.assertIsNone(risk.size_position(_m(atr=atr)))
        self.use(risk_per_trade_pct=0.0)
        self.assertIsNone(risk.size_position(_m()))

    def test_settings_change_the_result(self):
        self.use(risk_per_trade_pct=1.0, risk_stop_atr_multiple=1.0)
        self.assertAlmostEqual(risk.size_position(_m(atr=2.0)).weight_pct, 20.0)


class EarningsFlagTests(SettingsCase):
    def test_inside_the_window_is_flagged_with_friendly_text(self):
        cases = {0: "earnings today", 1: "earnings tomorrow", 3: "earnings in 3 days"}
        for days, text in cases.items():
            flag = risk.earnings_flag({"next_earnings": TODAY + timedelta(days=days)}, TODAY)
            self.assertTrue(flag.text.startswith(text), flag.text)

    def test_window_edges(self):
        self.use(risk_earnings_warn_days=7)
        self.assertIsNotNone(risk.earnings_flag({"next_earnings": TODAY + timedelta(days=7)}, TODAY))
        self.assertIsNone(risk.earnings_flag({"next_earnings": TODAY + timedelta(days=8)}, TODAY))
        self.assertIsNone(risk.earnings_flag({"next_earnings": TODAY - timedelta(days=1)}, TODAY))

    def test_unknown_is_not_a_flag(self):
        for profile in (None, {}, {"next_earnings": None}, {"next_earnings": "soon"}):
            self.assertIsNone(risk.earnings_flag(profile, TODAY))

    def test_defaults_to_the_current_date(self):
        tomorrow = datetime.now().date() + timedelta(days=1)
        self.assertEqual(risk.earnings_flag({"next_earnings": tomorrow}).days, 1)


class CorrelationTests(SettingsCase):
    def setUp(self):
        super().setUp()
        rng = np.random.default_rng(7)
        self.base = rng.normal(0, 0.01, 80)
        self.histories = {
            "A": _history(self.base),
            "B": _history(self.base * 1.2 + rng.normal(0, 0.0005, 80), 50.0),   # moves with A
            "C": _history(rng.normal(0, 0.01, 80)),                              # independent
            "SHORT": _history(self.base[-15:]),
            "FEW": _history(self.base[-25:]),                                     # 24 returns, under the minimum
            "HALF": _history(0.5 * self.base + np.sqrt(0.75) * rng.normal(0, 0.01, 80)),  # correlation ~0.5
            "TOKYO": _history(self.base, tz="Asia/Tokyo"),                       # same dates, other exchange tz
        }
        patcher = patch("newsbot.market_data._fetch_ticker_history", side_effect=lambda s, **k: self.histories[s])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_the_comoving_pair_is_reported(self):
        pairs = risk.correlated_pairs(["A", "B", "C"])
        self.assertEqual([(a, b) for a, b, _, _ in pairs], [("A", "B")])
        self.assertGreater(pairs[0][2], 0.9)
        self.assertEqual(pairs[0][3], 60)  # the correlation window

    def test_threshold_is_configurable(self):
        self.use(risk_corr_threshold=0.999999)
        self.assertEqual(risk.correlated_pairs(["A", "C"]), [])

    def test_moderate_correlation_is_below_the_threshold(self):
        self.assertEqual(risk.correlated_pairs(["A", "HALF"]), [])
        self.use(risk_corr_threshold=0.3)
        self.assertEqual(len(risk.correlated_pairs(["A", "HALF"])), 1)

    def test_too_few_sessions_is_skipped_not_guessed(self):
        self.assertEqual(risk.correlated_pairs(["A", "SHORT"]), [])   # too short to compute returns
        self.assertEqual(risk.correlated_pairs(["A", "FEW"]), [])     # under RISK_CORR_MIN_OBS
        self.use(risk_corr_min_obs=20)
        self.assertEqual(len(risk.correlated_pairs(["A", "FEW"])), 1)

    def test_exchanges_in_different_timezones_still_line_up_by_date(self):
        pairs = risk.correlated_pairs(["A", "TOKYO"])
        self.assertEqual(len(pairs), 1)
        self.assertGreater(pairs[0][2], 0.99)

    def test_failed_history_is_skipped(self):
        with patch("newsbot.market_data._fetch_ticker_history", side_effect=RuntimeError("yahoo")):
            self.assertEqual(risk.correlated_pairs(["A", "B"]), [])

    def test_different_calendars_align_on_shared_dates(self):
        a = _history(self.base)
        b = _history(self.base)
        b = b.iloc[::2]  # only every other session, like a market with more holidays
        self.histories["A2"], self.histories["B2"] = a, b
        self.use(risk_corr_min_obs=5)
        # Fewer shared dates, but the pair is still evaluated on those only.
        pairs = risk.correlated_pairs(["A2", "B2"])
        self.assertTrue(pairs == [] or all(count <= 40 for _, _, _, count in pairs))


class AssessTests(SettingsCase):
    def test_pair_sector_and_total_warnings(self):
        picks = [_m("AAA", atr=1.0), _m("BBB", atr=1.0), _m("CCC", atr=1.0)]
        profiles = {s: {"sector": "Technology", "next_earnings": None} for s in ("AAA", "BBB")}
        profiles["CCC"] = {"sector": "Energy", "next_earnings": None}
        result = risk.assess(picks, profiles, [("AAA", "BBB", 0.82, 58)], TODAY)
        text = "\n".join(result.warnings)
        self.assertIn("AAA and BBB moved together (correlation 0.82 over 58 sessions)", text)
        self.assertIn("40% of the portfolio", text)               # 20% + 20% (both capped)
        self.assertIn("AAA, BBB are all Technology", text)
        self.assertEqual(result.total_weight_pct, 60.0)
        self.assertEqual(result.sector_groups, {"Technology": ["AAA", "BBB"]})

    def test_total_warning_only_above_the_limit(self):
        picks = [_m("AAA", atr=2.0), _m("BBB", atr=2.0)]
        quiet = risk.assess(picks, {}, [], TODAY)
        self.assertEqual(quiet.warnings, [])
        self.use(risk_max_total_pct=20.0)
        loud = risk.assess(picks, {}, [], TODAY)
        self.assertTrue(any("to work" in w for w in loud.warnings))
        self.assertAlmostEqual(loud.max_loss_pct, 1.0)

    def test_events_and_unknown_profiles_are_tracked(self):
        picks = [_m("AAA"), _m("BBB")]
        profiles = {"AAA": {"sector": "Energy", "next_earnings": TODAY + timedelta(days=2)}}
        result = risk.assess(picks, profiles, [], TODAY)
        self.assertEqual(list(result.events), ["AAA"])
        self.assertEqual(result.unknown_profiles, 1)

    def test_unsizeable_picks_are_left_out_of_totals(self):
        result = risk.assess([_m("AAA", atr=0.0), _m("BBB", atr=2.0)], {}, [], TODAY)
        self.assertEqual(list(result.sizing), ["BBB"])


class BuildContextTests(SettingsCase):
    def test_disabled_or_empty_does_nothing(self):
        with patch.object(md, "_fetch_event_profile") as fetch:
            self.use(risk_analytics_enabled=False)
            self.assertEqual(risk.build_context([_m()]).sizing, {})
            self.use(risk_analytics_enabled=True)
            self.assertEqual(risk.build_context([]).sizing, {})
        fetch.assert_not_called()

    def test_end_to_end_with_mocked_lookups(self):
        self.use(risk_analytics_enabled=True)
        profiles = {"AAA": {"sector": "Energy", "next_earnings": datetime.now().date() + timedelta(days=2)},
                    "BBB": {"sector": "Energy", "next_earnings": None}}
        with patch.object(md, "_fetch_event_profile", side_effect=lambda s: profiles[s]), \
             patch.object(risk, "correlated_pairs", return_value=[("AAA", "BBB", 0.9, 50)]):
            context = risk.build_context([_m("AAA"), _m("BBB")])
        self.assertEqual(set(context.sizing), {"AAA", "BBB"})
        self.assertIn("AAA", context.events)
        self.assertTrue(any("moved together" in w for w in context.warnings))

    def test_a_failing_profile_lookup_degrades_to_unknown(self):
        self.use(risk_analytics_enabled=True)
        with patch.object(md, "_fetch_event_profile", side_effect=RuntimeError("boom")), \
             patch.object(risk, "correlated_pairs", return_value=[]):
            context = risk.build_context([_m("AAA")])
        self.assertEqual(context.unknown_profiles, 1)
        self.assertIn("AAA", context.sizing)

    def test_a_failing_correlation_step_is_contained(self):
        self.use(risk_analytics_enabled=True)
        with patch.object(md, "_fetch_event_profile", return_value={}), \
             patch.object(risk, "correlated_pairs", side_effect=RuntimeError("x")):
            context = risk.build_context([_m("AAA"), _m("BBB")])
        self.assertEqual(context.pairs, [])

    def test_deadline_stops_waiting_for_slow_lookups(self):
        import threading

        release = threading.Event()
        self.addCleanup(release.set)
        self.use(risk_analytics_enabled=True, risk_context_deadline_seconds=0)

        def slow(symbol):
            release.wait(5)
            return {}

        with patch.object(md, "_fetch_event_profile", side_effect=slow), \
             patch.object(risk, "correlated_pairs", return_value=[]), \
             patch.object(risk.time, "monotonic", side_effect=[0.0, 99.0, 99.0, 99.0]):
            started = datetime.now()
            context = risk.build_context([_m("AAA")])
        self.assertLess((datetime.now() - started).total_seconds(), 3)
        self.assertEqual(context.profiles, {})


class RenderingTests(SettingsCase):
    def _context(self, **kw):
        return risk.assess([_m("AAA", atr=2.0)], kw.get("profiles", {}), kw.get("pairs", []), TODAY)

    def test_candidate_lines_show_stop_size_and_flag(self):
        context = self._context(profiles={"AAA": {"sector": None, "next_earnings": TODAY + timedelta(days=2)}})
        with patch.object(risk, "datetime") as fake:
            fake.now.return_value = datetime(2026, 10, 5)
            context = risk.assess([_m("AAA", atr=2.0)],
                                  {"AAA": {"next_earnings": TODAY + timedelta(days=2)}}, [], TODAY)
        lines = risk.candidate_lines("AAA", context)
        self.assertIn("volatility stop 96.00, 2×ATR below the 100.00 close: 4.00 (4.0%) away", lines[0])
        self.assertEqual("    size: 12% of the portfolio", lines[1])
        self.assertIn("loss at the stop: 0.50% of the portfolio, before costs", lines[2])
        self.assertIn("fees: at least 118 NOK to buy and sell + currency exchange fee", lines[3])
        self.assertEqual("    ⛔ Not for ASK (US stock)", lines[4])
        self.assertIn("⚠️ earnings in 2 days", lines[5])

    def test_capped_size_says_so_and_amount_is_shown(self):
        self.use(risk_account_size=50_000.0)
        context = risk.assess([_m("AAA", atr=0.3)], {}, [], TODAY)
        lines = risk.candidate_lines("AAA", context)
        self.assertEqual("    size: 20% of the portfolio ≈ 10,000 NOK (capped at 20%)", lines[1])
        self.assertIn("loss at the stop: 0.12% of the portfolio ≈ 60 NOK", lines[2])

    def test_fee_line_shows_the_minimum_as_a_share_of_the_position(self):
        self.assertEqual(risk.fee_line("EQNR.OL", 4_500.0),
                         "    fees: 118 NOK to buy and sell (2.6% of 4,500 NOK)")
        # Above ~49,000 NOK the 0.12% rate beats the 59 NOK minimum.
        self.assertIn("144 NOK to buy and sell (0.2% of 60,000 NOK) + currency exchange fee",
                      risk.fee_line("AAPL", 60_000.0))
        self.assertEqual(risk.fee_line("EQNR.OL", None), "    fees: at least 118 NOK to buy and sell")

    def test_without_context_or_sizing_only_the_fee_and_ask_show(self):
        fee = "    fees: at least 118 NOK to buy and sell + currency exchange fee"
        self.assertEqual(risk.candidate_lines("AAA", None), [fee, "    ⛔ Not for ASK (US stock)"])
        self.assertEqual(risk.candidate_lines("ZZZ", self._context()), [fee, "    ⛔ Not for ASK (US stock)"])
        self.assertEqual(risk.candidate_lines("EQNR.OL", None), ["    fees: at least 118 NOK to buy and sell"])

    def test_portfolio_section_totals_and_disclaimer(self):
        lines = risk.portfolio_lines(risk.assess([_m("AAA"), _m("BBB")], {}, [], TODAY))
        text = "\n".join(lines)
        self.assertIn("<b>Taken together</b>", text)
        self.assertIn("25% of the portfolio at work", text)
        self.assertIn("1.0% at risk if every stop is hit", text)
        self.assertIn("not been shown to beat holding the same stocks", text)

    def test_portfolio_section_lists_warnings_and_unknown_data(self):
        context = risk.assess([_m("AAA"), _m("BBB")], {}, [("AAA", "BBB", 0.85, 55)], TODAY)
        text = "\n".join(risk.portfolio_lines(context))
        self.assertIn("⚠️ AAA and BBB moved together", text)
        self.assertIn("Totals:", text)
        self.assertIn("unavailable for 2 pick(s)", text)

    def test_empty_when_nothing_could_be_sized(self):
        self.assertEqual(risk.portfolio_lines(None), [])
        self.assertEqual(risk.portfolio_lines(risk.PortfolioRisk()), [])

    def test_html_in_warnings_is_escaped(self):
        context = risk.assess([_m("A<b>"), _m("B")], {"A<b>": {"sector": "R&D"}, "B": {"sector": "R&D"}}, [], TODAY)
        text = "\n".join(risk.portfolio_lines(context))
        self.assertIn("A&lt;b&gt;", text)
        self.assertIn("R&amp;D", text)


class EventProfileLookupTests(unittest.TestCase):
    def setUp(self):
        nb_state.EVENT_PROFILE_CACHE.clear()
        nb_state.TICKER_INFO_CACHE.clear()
        self.addCleanup(nb_state.EVENT_PROFILE_CACHE.clear)
        self.addCleanup(nb_state.TICKER_INFO_CACHE.clear)
        sleeper = patch("time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    @staticmethod
    def _ticker(calendar=None, info=None, calendar_error=None):
        ticker = MagicMock()
        type(ticker).info = property(lambda self: info if info is not None else {})
        if calendar_error:
            type(ticker).calendar = property(lambda self: (_ for _ in ()).throw(calendar_error))
        else:
            type(ticker).calendar = property(lambda self: calendar)
        return ticker

    def test_next_earnings_picks_the_earliest_upcoming_date(self):
        today = datetime.now().date()
        cal = {"Earnings Date": [today - timedelta(days=30), today + timedelta(days=20),
                                 datetime.now() + timedelta(days=9), "junk"]}
        with patch("yfinance.Ticker", return_value=self._ticker(calendar=cal)):
            self.assertEqual(md._next_earnings_date("X"), today + timedelta(days=9))

    def test_no_calendar_or_only_past_dates_is_none(self):
        today = datetime.now().date()
        for cal in (None, {}, {"Earnings Date": []}, {"Earnings Date": [today - timedelta(days=3)]}, "weird"):
            with patch("yfinance.Ticker", return_value=self._ticker(calendar=cal)):
                self.assertIsNone(md._next_earnings_date("X"), cal)

    def test_profile_combines_sector_and_date_and_is_cached(self):
        soon = datetime.now().date() + timedelta(days=4)
        ticker = self._ticker(calendar={"Earnings Date": [soon]}, info={"sector": " Energy "})
        with patch("yfinance.Ticker", return_value=ticker) as ctor:
            first = md._fetch_event_profile("EQNR.OL")
            second = md._fetch_event_profile("EQNR.OL")
        self.assertEqual((first["sector"], first["next_earnings"]), ("Energy", soon))
        self.assertIs(first, second)
        self.assertEqual(ctor.call_count, 2)  # info + calendar once each, not twice

    def test_missing_sector_is_none(self):
        with patch("yfinance.Ticker", return_value=self._ticker(calendar={}, info={"sector": "  "})):
            self.assertIsNone(md._fetch_event_profile("X")["sector"])

    def test_calendar_failure_is_incomplete_and_retried_sooner(self):
        ticker = self._ticker(info={"sector": "Tech"}, calendar_error=RuntimeError("no calendar"))
        with patch("yfinance.Ticker", return_value=ticker):
            profile = md._fetch_event_profile("X")
        self.assertFalse(profile["complete"])
        self.assertIsNone(profile["next_earnings"])
        self.assertEqual(profile["sector"], "Tech")
        self.assertEqual(profile["_ttl"], md._EVENT_PROFILE_RETRY_SECONDS)

    def test_expired_entries_are_refetched(self):
        ticker = self._ticker(calendar={}, info={})
        with patch("yfinance.Ticker", return_value=ticker) as ctor:
            md._fetch_event_profile("X")
            stamp, profile = nb_state.EVENT_PROFILE_CACHE["event|X"]
            nb_state.EVENT_PROFILE_CACHE["event|X"] = (stamp - 10**6, profile)
            md._fetch_event_profile("X")
        # Sector info is cached for hours, so the refetch only re-asks for the calendar.
        self.assertEqual(ctor.call_count, 3)


class IntegrationTests(SettingsCase):
    def setUp(self):
        super().setUp()
        # Rendering a candidate reads its qualifying streak from the trade history;
        # give it a throwaway store so no real database is opened or leaked.
        store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(store.close)
        patcher = patch.object(nb_history_store, "TRADE_HISTORY", store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_candidate_block_includes_risk_lines(self):
        context = risk.assess([_m("AAA")], {}, [], TODAY)
        lines = nb_screener._render_candidate(_m("AAA"), "Alpha", context)
        self.assertTrue(any("volatility stop" in line for line in lines))
        plain = nb_screener._render_candidate(_m("AAA"), "Alpha")
        self.assertFalse(any("volatility stop" in line for line in plain))

    def test_recommendations_text_has_per_pick_and_portfolio_sections(self):
        self.use(risk_analytics_enabled=True)
        qualified = [_m("AAA"), _m("BBB")]
        outcome = nb_screener.ScreenOutcome(checked=2, analysed=2, qualified=qualified)
        profiles = {"AAA": {"sector": "Energy", "next_earnings": datetime.now().date() + timedelta(days=1)},
                    "BBB": {"sector": "Energy", "next_earnings": None}}
        with patch.object(nb_screener, "_screen_universe", return_value=(outcome, {})), \
             patch.object(md, "_fetch_event_profile", side_effect=lambda s: profiles[s]), \
             patch.object(risk, "correlated_pairs", return_value=[]), \
             patch.object(nb_history_store.TRADE_HISTORY, "archive_ranks"):
            text, _, _ = nb_screener.build_trade_candidates_payload(universe={"A": "AAA", "B": "BBB"})
        self.assertEqual(text.count("volatility stop"), 2)
        self.assertIn("earnings tomorrow", text)
        self.assertIn("<b>Taken together</b>", text)
        self.assertIn("AAA, BBB are all Energy", text)
        self.assertEqual(text.count("fees: "), 2)
        self.assertIn("Watch list only, not a buy list.", text.splitlines()[1])

    def test_disabled_feature_leaves_the_text_unchanged(self):
        self.use(risk_analytics_enabled=False)
        outcome = nb_screener.ScreenOutcome(checked=1, analysed=1, qualified=[_m("AAA")])
        with patch.object(nb_screener, "_screen_universe", return_value=(outcome, {})), \
             patch.object(nb_history_store.TRADE_HISTORY, "archive_ranks"):
            text, _, _ = nb_screener.build_trade_candidates_payload(universe={"A": "AAA"})
        self.assertNotIn("volatility stop", text)
        self.assertNotIn("Taken together", text)
        self.assertIn("fees: at least 118 NOK", text)

    def test_analyze_has_a_risk_section(self):
        self.use(risk_analytics_enabled=True)
        profile = {"sector": "Energy", "next_earnings": datetime.now().date() + timedelta(days=3)}
        with patch("newsbot.indicators._compute_trade_metrics", return_value=_m("AAA")), \
             patch.object(md, "_fetch_ticker_fundamentals", return_value={"longName": "Alpha"}), \
             patch.object(md, "_fetch_event_profile", return_value=profile):
            report = nb_reports.build_ticker_analysis("aaa")
        self.assertIn("Risk &amp; Events", report)
        self.assertIn("volatility stop", report)
        self.assertIn("earnings in 3 days", report)

    def test_analyze_shows_how_much_it_can_fall(self):
        self.use(risk_analytics_enabled=True)
        forecast = nb_risk_forecast.Forecast(
            sessions=504, bad_moves=(("2 weeks", -6.2), ("4 weeks", -9.5)), max_drop_pct=-31.4,
            drop_from=date(2025, 3, 3), drop_to=date(2025, 8, 1))
        with patch("newsbot.indicators._compute_trade_metrics", return_value=_m("AAA")), \
             patch.object(md, "_fetch_ticker_fundamentals", return_value={}), \
             patch.object(md, "_fetch_event_profile", return_value={}), \
             patch.object(nb_risk_forecast, "build", return_value=forecast):
            report = nb_reports.build_ticker_analysis("AAA")
        self.assertIn("How much it can fall over a 2-4 week hold:", report)
        self.assertIn("a bad 4 weeks: -9.5%", report)

    def test_analyze_without_a_known_date_says_so(self):
        self.use(risk_analytics_enabled=True)
        with patch("newsbot.indicators._compute_trade_metrics", return_value=_m("AAA")), \
             patch.object(md, "_fetch_ticker_fundamentals", return_value={}), \
             patch.object(md, "_fetch_event_profile", return_value={"sector": None, "next_earnings": None}):
            report = nb_reports.build_ticker_analysis("AAA")
        self.assertIn("Next earnings date: not available", report)

    def test_analyze_far_off_earnings_are_shown_without_a_warning(self):
        self.use(risk_analytics_enabled=True)
        far = datetime.now().date() + timedelta(days=40)
        with patch("newsbot.indicators._compute_trade_metrics", return_value=_m("AAA")), \
             patch.object(md, "_fetch_ticker_fundamentals", return_value={}), \
             patch.object(md, "_fetch_event_profile", return_value={"next_earnings": far}):
            report = nb_reports.build_ticker_analysis("AAA")
        self.assertIn(f"Next earnings: {far:%b %d, %Y}", report)
        self.assertNotIn("⚠️ earnings", report)


class CorrelatedClusterTests(SettingsCase):
    """Sizing treats each pick as its own bet; clustering says when it is not."""

    def test_transitive_pairs_become_one_cluster(self):
        pairs = [("AAA", "BBB", 0.9, 90), ("BBB", "CCC", 0.85, 90)]
        clusters = risk.correlated_clusters(["AAA", "BBB", "CCC", "DDD"], pairs)
        self.assertEqual(clusters, [["AAA", "BBB", "CCC"]])

    def test_a_symbol_with_no_pair_is_not_a_cluster(self):
        self.assertEqual(risk.correlated_clusters(["AAA", "BBB"], []), [])

    def test_pairs_naming_unknown_symbols_are_ignored(self):
        clusters = risk.correlated_clusters(["AAA"], [("AAA", "ZZZ", 0.9, 90)])
        self.assertEqual(clusters, [])

    def test_largest_cluster_comes_first(self):
        pairs = [("AAA", "BBB", 0.9, 90), ("CCC", "DDD", 0.9, 90), ("DDD", "EEE", 0.9, 90)]
        clusters = risk.correlated_clusters(["AAA", "BBB", "CCC", "DDD", "EEE"], pairs)
        self.assertEqual(clusters[0], ["CCC", "DDD", "EEE"])

    def test_cluster_loss_adds_up_the_whole_group(self):
        picks = [_m("AAA"), _m("BBB"), _m("CCC")]
        pairs = [("AAA", "BBB", 0.9, 90), ("BBB", "CCC", 0.88, 90)]
        context = risk.assess(picks, {}, pairs, TODAY)
        expected = sum(context.sizing[s].risk_pct for s in ("AAA", "BBB", "CCC"))
        self.assertAlmostEqual(context.cluster_loss_pct, expected, places=6)
        self.assertAlmostEqual(context.cluster_weight_pct,
                               sum(context.sizing[s].weight_pct
                                   for s in ("AAA", "BBB", "CCC")), places=6)
        self.assertLess(context.cluster_loss_pct, context.max_loss_pct + 1e-9)

    def test_three_way_cluster_is_called_out_in_words(self):
        picks = [_m("AAA"), _m("BBB"), _m("CCC")]
        pairs = [("AAA", "BBB", 0.9, 90), ("BBB", "CCC", 0.88, 90)]
        context = risk.assess(picks, {}, pairs, TODAY)
        self.assertTrue(any("all move together" in warning for warning in context.warnings))

    def test_a_mere_pair_is_left_to_the_pair_warning(self):
        """Two names already get a pair warning; repeating it with arithmetic
        adds nothing, so the cluster sentence starts at three."""
        context = risk.assess([_m("AAA"), _m("BBB")], {},
                              [("AAA", "BBB", 0.9, 90)], TODAY)
        self.assertEqual(context.clusters, [["AAA", "BBB"]])
        self.assertFalse(any("all move together" in w for w in context.warnings))


class StopConsistencyTests(SettingsCase):
    """One candidate block must not show two different stops."""

    def setUp(self):
        super().setUp()
        store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(store.close)
        patcher = patch.object(nb_history_store, "TRADE_HISTORY", store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reward_risk_is_measured_to_the_sizing_stop(self):
        metrics = _m("AAA", atr=2.0, close=100.0)
        context = risk.assess([metrics], {}, [], TODAY)
        sized = context.sizing["AAA"]
        sketch = nb_screener._entry_exit_sketch(metrics, context)
        self.assertIn(f"invalidation below {sized.stop_price:,.2f} (stop)", sketch)
        expected = (metrics.resistance_level - metrics.last_close) / (
            metrics.last_close - sized.stop_price)
        self.assertIn(f"reward:risk {expected:.1f}:1", sketch)

    def test_the_block_shows_only_one_stop_price(self):
        metrics = _m("AAA", atr=2.0, close=100.0)
        context = risk.assess([metrics], {}, [], TODAY)
        lines = nb_screener._render_candidate(metrics, "Alpha", context)
        block = "\n".join(lines)
        sized = context.sizing["AAA"]
        self.assertEqual(block.count(f"{sized.stop_price:,.2f}"), 2)
        # The 20-day low still appears as the zone floor, never as the stop.
        self.assertNotIn(f"invalidation below {metrics.support_level:,.2f}", block)

    def test_without_sizing_it_falls_back_to_the_low_and_labels_it(self):
        metrics = _m("AAA")
        sketch = nb_screener._entry_exit_sketch(metrics, None)
        self.assertIn(f"invalidation below {metrics.support_level:,.2f} (20-day low)", sketch)

    def test_no_room_to_the_high_survives_the_new_denominator(self):
        metrics = _m("AAA", close=110.0, resistance_level=104.0)
        context = risk.assess([metrics], {}, [], TODAY)
        sketch = nb_screener._entry_exit_sketch(metrics, context)
        self.assertIn("no room to the 20-day high", sketch)
        # The yardstick is named once: a live screen rendered
        # "no room to the 20-day high to the 20-day high".
        self.assertEqual(sketch.count("to the 20-day high"), 1)

    def test_a_ratio_names_what_it_is_measured_against(self):
        metrics = _m("AAA", atr=2.0, close=100.0)
        context = risk.assess([metrics], {}, [], TODAY)
        sketch = nb_screener._entry_exit_sketch(metrics, context)
        self.assertRegex(sketch, r"reward:risk \d+\.\d:1 to the 20-day high")
        self.assertEqual(sketch.count("to the 20-day high"), 1)

    def test_footer_states_the_measured_gap_frequency(self):
        context = risk.assess([_m("AAA")], {}, [], TODAY)
        footer = "\n".join(nb_screener.risk_analytics.portfolio_lines(context))
        self.assertIn("about 1 stop in 7 opened below its price", footer)
        self.assertIn("2×ATR stop below the signal close", footer)


if __name__ == "__main__":
    unittest.main()
