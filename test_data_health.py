"""Market-data health: assessment, alert transitions, /health line."""

import unittest
from datetime import datetime
from unittest.mock import patch

from newsbot import (
    config as nb_config,
    data_health as nb_dh,
    indicators as nb_indicators,
    reports as nb_reports,
    screener as nb_screener,
    state as nb_state,
)

TODAY = datetime(2026, 10, 5, 12, 0)  # a Monday


def _m(symbol, session="2026-10-02"):
    return nb_indicators.TradeMetrics(
        symbol=symbol, session=session, last_close=1.0, day_change_pct=0.0,
        week_momentum_pct=0.0, volume_ratio=None, drawdown_pct=0.0, atr_pct=1.0,
        above_ema20=True,
    )


def _outcome(analysed, no_data, session="2026-10-02"):
    good = analysed - no_data
    outcome = nb_screener.ScreenOutcome(checked=analysed, analysed=analysed, no_data=no_data)
    outcome.qualified = [_m(f"Q{i}", session) for i in range(good // 2)]
    outcome.rejected = [(_m(f"R{i}", session), ["trend"]) for i in range(good - good // 2)]
    return outcome


class AssessTests(unittest.TestCase):
    def test_healthy_screen(self):
        self.assertEqual(nb_dh.assess(30, 2, {"US": "2026-10-02"}, TODAY), (False, []))

    def test_too_many_empty_symbols(self):
        bad, reasons = nb_dh.assess(30, 20, {"US": "2026-10-02"}, TODAY)
        self.assertTrue(bad)
        self.assertIn("20 of 30", reasons[0])

    def test_exactly_at_threshold_is_not_bad(self):
        self.assertFalse(nb_dh.assess(10, 5, {"US": "2026-10-02"}, TODAY)[0])

    def test_stale_prices(self):
        bad, reasons = nb_dh.assess(30, 0, {"US": "2026-09-25"}, TODAY)
        self.assertTrue(bad)
        self.assertIn("days old", reasons[0])

    def test_long_weekend_is_not_stale(self):
        self.assertFalse(nb_dh.assess(30, 0, {"US": "2026-10-01"}, TODAY)[0])

    def test_tiny_sample_is_never_bad(self):
        self.assertEqual(nb_dh.assess(3, 3, None, TODAY), (False, []))

    def test_a_fresh_market_cannot_mask_a_stale_one(self):
        """The 2026-10-13 shape: New York current, Oslo a week behind."""
        bad, reasons = nb_dh.assess(
            30, 0, {"US": "2026-10-02", "OL": "2026-09-25"}, TODAY
        )
        self.assertTrue(bad)
        self.assertEqual(len(reasons), 1)
        self.assertIn("OL:", reasons[0])

    def test_every_stale_market_is_named(self):
        bad, reasons = nb_dh.assess(
            30, 0, {"DE": "2026-09-25", "OL": "2026-09-25"}, TODAY
        )
        self.assertTrue(bad)
        self.assertEqual([reason.split(":")[0] for reason in reasons], ["DE", "OL"])

    def test_an_unparsable_session_is_skipped_rather_than_blamed(self):
        self.assertEqual(nb_dh.assess(30, 0, {"OL": "garbage"}, TODAY), (False, []))


class MarketGroupingTests(unittest.TestCase):
    def test_suffix_names_the_market_and_bare_symbols_are_us(self):
        self.assertEqual(nb_dh._market_of("EQNR.OL"), "OL")
        self.assertEqual(nb_dh._market_of("AAPL"), "US")
        self.assertEqual(nb_dh._market_of("WEIRD."), "US")

    def test_each_market_reports_its_own_newest_session(self):
        outcome = _outcome(0, 0)
        outcome.qualified = [_m("AAPL", "2026-10-12"), _m("EQNR.OL", "2026-10-09")]
        outcome.rejected = [(_m("SAP.DE", "2026-10-09"), ["trend"]),
                            (_m("MSFT", "2026-10-09"), ["trend"])]
        self.assertEqual(
            nb_dh._sessions_by_market(outcome),
            {"US": "2026-10-12", "OL": "2026-10-09", "DE": "2026-10-09"},
        )
        self.assertEqual(nb_dh._newest_session(outcome), "2026-10-12")


class TransitionTests(unittest.TestCase):
    def setUp(self):
        nb_state.STATE.data.pop("market_data_health", None)
        self.addCleanup(lambda: nb_state.STATE.data.pop("market_data_health", None))
        patches = [
            patch.object(nb_state.STATE, "save"),
            patch.object(nb_dh.messaging, "_broadcast_message", return_value=True),
            patch.object(nb_config.SETTINGS, "telegram_chat_id", "chat"),
        ]
        self.save, self.send, _ = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def _record(self, outcome, hour=12):
        nb_dh.record_screen(outcome, datetime(2026, 10, 5, hour, 0))

    def test_single_bad_screen_does_not_alert(self):
        self._record(_outcome(30, 20))
        self.send.assert_not_called()
        self.assertIn("suspect", nb_dh.describe())

    def test_second_consecutive_bad_screen_alerts_once(self):
        self._record(_outcome(30, 20))
        self._record(_outcome(30, 20), hour=13)
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("Market data problem", self.send.call_args.args[0])
        self._record(_outcome(30, 20), hour=14)  # still bad, inside reminder window
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("DEGRADED", nb_dh.describe())

    def test_total_outage_alerts_immediately(self):
        self._record(_outcome(30, 30))
        self.assertEqual(self.send.call_count, 1)

    def test_good_screen_resets_the_streak(self):
        self._record(_outcome(30, 20))
        self._record(_outcome(30, 1), hour=13)
        self._record(_outcome(30, 20), hour=14)
        self.send.assert_not_called()

    def test_recovery_sends_an_all_clear(self):
        self._record(_outcome(30, 30))
        self._record(_outcome(30, 1), hour=13)
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("recovered", self.send.call_args.args[0])
        self.assertIn("ok", nb_dh.describe())
        self._record(_outcome(30, 1), hour=14)
        self.assertEqual(self.send.call_count, 2)

    def test_reminder_after_a_day(self):
        self._record(_outcome(30, 30))
        nb_dh.record_screen(_outcome(30, 30), datetime(2026, 10, 6, 13, 0))
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("still failing", self.send.call_args.args[0])

    def test_small_sample_leaves_alert_state_alone(self):
        self._record(_outcome(30, 30))
        self._record(_outcome(2, 0), hour=13)
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("DEGRADED", nb_dh.describe())

    def test_disabled_alerts_still_record(self):
        with patch.object(nb_config.SETTINGS, "market_data_alerts_enabled", False):
            self._record(_outcome(30, 30))
        self.send.assert_not_called()
        self.assertIn("DEGRADED", nb_dh.describe())

    def test_alert_failure_never_raises(self):
        self.send.side_effect = RuntimeError("telegram down")
        self._record(_outcome(30, 30))  # must not raise


class IntegrationTests(unittest.TestCase):
    def test_health_report_includes_data_line(self):
        nb_state.STATE.data.pop("market_data_health", None)
        self.assertIn("Market data: no screen run yet", nb_reports.build_health_report())

    def test_screen_universe_records_health(self):
        with patch.object(nb_dh, "record_screen") as record, \
             patch("newsbot.indicators._compute_trade_metrics", return_value=None), \
             patch.object(nb_screener.history_store.TRADE_HISTORY, "prune"):
            outcome, _ = nb_screener._screen_universe([("A", "A"), ("B", "B")])
        record.assert_called_once_with(outcome)


if __name__ == "__main__":
    unittest.main()
