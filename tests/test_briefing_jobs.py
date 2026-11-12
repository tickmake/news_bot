"""briefing: intro, pending keys, commit, composition failures, jobs and health ping."""

import unittest
from datetime import datetime
from unittest.mock import patch

from newsbot import briefing as nb_briefing
from newsbot import config as nb_config
from newsbot import messaging as nb_messaging
from newsbot import news_feeds as nb_news_feeds
from newsbot import screener as nb_screener
from newsbot import state as nb_state


class IntroTests(unittest.TestCase):
    def test_morning_has_a_quote_and_the_date(self):
        text = nb_briefing.build_daily_intro(datetime(2026, 10, 5, 7, 0))
        self.assertIn("2026-10-05", text)
        self.assertIn("Quote of the day", text)

    def test_evening_has_no_quote(self):
        text = nb_briefing.build_daily_intro(datetime(2026, 10, 5, 19, 0))
        self.assertNotIn("Quote of the day", text)
        self.assertIn("2026-10-05", text)

    def test_recipient_is_escaped_and_defaults_when_blank(self):
        with patch.object(nb_config.SETTINGS, "recipient_name", "<b>Eve</b>"):
            self.assertIn("&lt;b&gt;Eve", nb_briefing.build_daily_intro(datetime(2026, 10, 5, 7, 0)))
        with patch.object(nb_config.SETTINGS, "recipient_name", "   "):
            self.assertIn("Hello, Sunil!", nb_briefing.build_daily_intro(datetime(2026, 10, 5, 7, 0)))

    def test_same_day_gives_the_same_greeting(self):
        when = datetime(2026, 10, 5, 7, 0)
        self.assertEqual(nb_briefing.build_daily_intro(when), nb_briefing.build_daily_intro(when))


class CommitTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(nb_state.STATE.data, {"sent_headline_keys": {}, "recent_titles": {}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_commit_marks_keys_per_section_and_records_titles(self):
        briefing = nb_briefing.Briefing(
            message="m",
            pending=[("global_news", "k1"), ("global_news", "k2"), ("norway_news", "k3")],
            titles=["Headline A"],
        )
        with patch.object(nb_briefing, "_record_published_picks") as record:
            nb_briefing._commit_briefing(briefing)
        record.assert_called_once_with([])
        sent = nb_state.STATE.data["sent_headline_keys"]
        self.assertEqual(sorted(k for keys in sent["global_news"].values() for k in keys), ["k1", "k2"])
        self.assertEqual([k for keys in sent["norway_news"].values() for k in keys], ["k3"])
        self.assertIn("Headline A", nb_state.STATE.recent_titles(window_days=1))


class CompositionFailureTests(unittest.TestCase):
    def _patches(self, trade):
        return [
            patch.object(nb_news_feeds, "get_norwegian_morning_news", return_value=("N\n", [])),
            patch.object(nb_news_feeds, "get_global_news", return_value=("G\n", [])),
            patch.object(nb_briefing.market_data, "get_business_and_stocks", return_value=("B\n", [])),
            trade,
        ]

    def _compose(self, trade):
        patches = self._patches(trade)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return nb_briefing.compose_briefing(datetime(2026, 10, 5, 7, 0))

    def test_screen_timeout_becomes_a_notice(self):
        from concurrent.futures import TimeoutError as FuturesTimeout

        class _Slow:
            def result(self, timeout=None):
                raise FuturesTimeout()

        class _Pool:
            def __init__(self, *args, **kwargs):
                pass

            def submit(self, *args, **kwargs):
                return _Slow()

            def shutdown(self, *args, **kwargs):
                pass

        with patch.object(nb_briefing, "ThreadPoolExecutor", _Pool):
            briefing = self._compose(patch.object(nb_screener, "build_trade_candidates_payload"))
        self.assertIn("Trade candidates skipped: ran out of time", briefing.message)
        self.assertEqual(briefing.failed_sections, ["Trade candidates"])
        self.assertEqual(briefing.picks, [])

    def test_picks_are_capped_at_five(self):
        from newsbot.indicators import TradeMetrics

        qualified = [
            TradeMetrics(symbol=f"S{i}", session="2026-10-02", last_close=1.0, day_change_pct=1.0,
                         week_momentum_pct=2.0, volume_ratio=None, drawdown_pct=0.0, atr_pct=1.0,
                         above_ema20=True)
            for i in range(8)
        ]
        outcome = nb_screener.ScreenOutcome(qualified=qualified)
        briefing = self._compose(patch.object(
            nb_screener, "build_trade_candidates_payload", return_value=("T\n", outcome, {})))
        self.assertEqual(len(briefing.picks), 5)


class ScheduledJobTests(unittest.TestCase):
    def test_scheduled_job_passes_the_current_slot(self):
        with patch.object(nb_briefing, "job_daily_briefing", return_value=True) as job, \
             patch.object(nb_briefing, "datetime") as fake:
            fake.now.return_value = datetime(2026, 10, 5, 7, 0, 5)
            self.assertTrue(nb_briefing.job_scheduled_briefing())
        job.assert_called_once_with(slot="2026-10-05@07")

    def test_scheduled_job_outside_any_slot_passes_none(self):
        with patch.object(nb_briefing, "job_daily_briefing", return_value=True) as job, \
             patch.object(nb_briefing, "datetime") as fake:
            fake.now.return_value = datetime(2026, 10, 5, 3, 0)
            nb_briefing.job_scheduled_briefing()
        job.assert_called_once_with(slot=None)

    def test_catchup_runs_the_missed_slot(self):
        with patch.object(nb_briefing, "missed_briefing_slot", return_value="2026-10-05@07"), \
             patch.object(nb_briefing, "job_daily_briefing", return_value=True) as job:
            self.assertTrue(nb_briefing.job_catchup_briefing())
        job.assert_called_once_with(slot="2026-10-05@07")

    def test_catchup_with_nothing_missed_does_nothing(self):
        with patch.object(nb_briefing, "missed_briefing_slot", return_value=None), \
             patch.object(nb_briefing, "job_daily_briefing") as job:
            self.assertTrue(nb_briefing.job_catchup_briefing())
        job.assert_not_called()

    def test_force_hour_overrides_the_run_time(self):
        seen = {}

        def compose(now):
            seen["now"] = now
            return nb_briefing.Briefing(message="m")

        with patch.object(nb_briefing, "compose_briefing", side_effect=compose), \
             patch.object(nb_messaging, "_broadcast_message", return_value=True), \
             patch.object(nb_state.STATE, "save"):
            nb_briefing.job_daily_briefing(force_hour=19)
        self.assertEqual((seen["now"].hour, seen["now"].minute), (19, 0))

    def test_lock_timeout_gives_up_without_composing(self):
        from contextlib import contextmanager

        @contextmanager
        def never_held(blocking):
            yield False

        with patch.object(nb_briefing, "_briefing_slot", never_held), \
             patch.object(nb_briefing, "compose_briefing") as compose:
            self.assertFalse(nb_briefing.job_daily_briefing())
        compose.assert_not_called()

    def test_failed_send_marks_the_run_failed_and_skips_the_slot(self):
        briefing = nb_briefing.Briefing(message="m", pending=[("global_news", "k")])
        with patch.object(nb_briefing, "compose_briefing", return_value=briefing), \
             patch.object(nb_messaging, "_broadcast_message", return_value=False), \
             patch.object(nb_state.STATE, "save"), \
             patch.dict(nb_state.STATE.data, {"last_scheduled_slot": "old"}):
            self.assertFalse(nb_briefing.job_daily_briefing(slot="new"))
            self.assertEqual(nb_state.STATE.data["last_scheduled_slot"], "old")
            self.assertEqual(nb_state.STATE.data["last_run_status"], "failed")


class HealthPingTests(unittest.TestCase):
    def setUp(self):
        for p in (
            patch.dict(nb_state.STATE.data, {}),
            patch.object(nb_state.STATE, "save"),
            patch.object(nb_briefing, "build_health_report", return_value="HEALTH"),
        ):
            p.start()
            self.addCleanup(p.stop)
        nb_state.STATE.data.pop("last_health_ping_date", None)

    def test_disabled_does_nothing(self):
        with patch.object(nb_config.SETTINGS, "health_ping_enabled", False), \
             patch.object(nb_messaging, "_broadcast_message") as send:
            self.assertTrue(nb_briefing.job_health_ping())
        send.assert_not_called()

    def test_sent_once_per_day(self):
        with patch.object(nb_config.SETTINGS, "health_ping_enabled", True), \
             patch.object(nb_config.SETTINGS, "telegram_chat_id", "main"), \
             patch.object(nb_config.SETTINGS, "health_ping_chat_id", None), \
             patch.object(nb_messaging, "_broadcast_message", return_value=True) as send:
            self.assertTrue(nb_briefing.job_health_ping())
            self.assertTrue(nb_briefing.job_health_ping())
        send.assert_called_once_with("HEALTH", chat_id="main")
        self.assertEqual(nb_state.STATE.data["last_health_ping_date"], datetime.today().strftime("%Y-%m-%d"))

    def test_dedicated_health_chat_wins(self):
        with patch.object(nb_config.SETTINGS, "health_ping_enabled", True), \
             patch.object(nb_config.SETTINGS, "health_ping_chat_id", "ops"), \
             patch.object(nb_messaging, "_broadcast_message", return_value=True) as send:
            nb_briefing.job_health_ping()
        send.assert_called_once_with("HEALTH", chat_id="ops")

    def test_failed_ping_is_retried_later(self):
        with patch.object(nb_config.SETTINGS, "health_ping_enabled", True), \
             patch.object(nb_messaging, "_broadcast_message", return_value=False):
            self.assertFalse(nb_briefing.job_health_ping())
        self.assertNotIn("last_health_ping_date", nb_state.STATE.data)


if __name__ == "__main__":
    unittest.main()
