"""Per-section isolation, the briefing time budget, the briefing lock, interrupted commands."""

import threading
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from newsbot import (
    briefing as nb_briefing,
    commands as nb_commands,
    config as nb_config,
    market_data as nb_market_data,
    messaging as nb_messaging,
    news_feeds as nb_news_feeds,
    screener as nb_screener,
    state as nb_state,
)

NOW = datetime(2026, 10, 3, 7, 0)


def _ok_patches(**overrides):
    targets = {
        "norway": patch.object(nb_news_feeds, "get_norwegian_morning_news", return_value=("NORWAY\n", [])),
        "global": patch.object(nb_news_feeds, "get_global_news", return_value=("GLOBAL\n", [])),
        "business": patch.object(nb_market_data, "get_business_and_stocks", return_value=("BIZ\n", [])),
        "trade": patch.object(
            nb_screener, "build_trade_candidates_payload",
            return_value=("TRADE\n", nb_screener.ScreenOutcome(), {}),
        ),
    }
    for key, value in overrides.items():
        targets[key] = value
    return targets


class SectionIsolationTests(unittest.TestCase):
    def _compose(self, **overrides):
        patches = _ok_patches(**overrides)
        for p in patches.values():
            p.start()
            self.addCleanup(p.stop)
        return nb_briefing.compose_briefing(NOW)

    def test_one_failing_news_section_does_not_sink_the_briefing(self):
        briefing = self._compose(
            **{"global": patch.object(nb_news_feeds, "get_global_news", side_effect=RuntimeError("feed down"))}
        )
        self.assertIn("NORWAY", briefing.message)
        self.assertIn("BIZ", briefing.message)
        self.assertIn("TRADE", briefing.message)
        self.assertIn("Global news unavailable", briefing.message)
        self.assertEqual(briefing.failed_sections, ["Global news"])

    def test_failing_screener_is_replaced_by_a_notice_and_no_picks(self):
        briefing = self._compose(
            trade=patch.object(nb_screener, "build_trade_candidates_payload", side_effect=RuntimeError("yf"))
        )
        self.assertIn("Trade candidates unavailable", briefing.message)
        self.assertIn("NORWAY", briefing.message)
        self.assertEqual(briefing.picks, [])
        self.assertEqual(briefing.failed_sections, ["Trade candidates"])

    def test_clean_run_reports_no_failures(self):
        self.assertEqual(self._compose().failed_sections, [])

    def test_exhausted_budget_skips_remaining_news_sections(self):
        calls = {"n": 0}

        def clock():
            # First reading sets the deadline; every later one is long past it.
            calls["n"] += 1
            return 0.0 if calls["n"] == 1 else 10_000.0

        # Only briefing's own `time` is replaced, not the process-wide module.
        with patch.object(nb_briefing, "time", SimpleNamespace(monotonic=clock)):
            briefing = self._compose()
        self.assertIn("skipped: ran out of time", briefing.message)
        self.assertEqual(
            briefing.failed_sections, ["Norway news", "Global news", "Business & markets"]
        )


class JobStatusTests(unittest.TestCase):
    def test_partial_briefing_is_sent_and_status_says_partial(self):
        briefing = nb_briefing.Briefing(message="m", failed_sections=["Global news"])
        with patch.object(nb_briefing, "compose_briefing", return_value=briefing), \
             patch.object(nb_messaging, "_broadcast_message", return_value=True), \
             patch.object(nb_state.STATE, "save"):
            self.assertTrue(nb_briefing.job_daily_briefing())
        self.assertEqual(nb_state.STATE.data["last_run_status"], "partial: Global news")

    def test_compose_crash_is_contained_and_recorded(self):
        with patch.object(nb_briefing, "compose_briefing", side_effect=RuntimeError("boom")), \
             patch.object(nb_state.STATE, "save"):
            self.assertFalse(nb_briefing.job_daily_briefing())
        self.assertEqual(nb_state.STATE.data["last_run_status"], "failed")

    def test_send_exception_is_contained(self):
        with patch.object(nb_briefing, "compose_briefing", return_value=nb_briefing.Briefing(message="m")), \
             patch.object(nb_messaging, "_broadcast_message", side_effect=RuntimeError("net")), \
             patch.object(nb_state.STATE, "save"):
            self.assertFalse(nb_briefing.job_daily_briefing())


class BriefingLockTests(unittest.TestCase):
    def test_now_during_a_running_briefing_is_told_to_wait_not_duplicated(self):
        started, release = threading.Event(), threading.Event()

        def slow_compose(now=None):
            started.set()
            release.wait(5)
            return nb_briefing.Briefing(message="m")

        with patch.object(nb_briefing, "compose_briefing", side_effect=slow_compose) as compose, \
             patch.object(nb_messaging, "_broadcast_message", return_value=True), \
             patch.object(nb_messaging, "send_telegram_message") as send, \
             patch.object(nb_state.STATE, "save"):
            worker = threading.Thread(target=nb_briefing.job_daily_briefing)
            worker.start()
            self.assertTrue(started.wait(2))
            nb_briefing._send_briefing(NOW, "chat")
            self.assertIn("already being prepared", send.call_args.args[0])
            self.assertEqual(compose.call_count, 1)
            release.set()
            worker.join(5)
        self.assertFalse(nb_briefing._BRIEFING_LOCK.locked())

    def test_lock_is_released_after_a_failure(self):
        with patch.object(nb_briefing, "compose_briefing", side_effect=RuntimeError("x")), \
             patch.object(nb_state.STATE, "save"):
            nb_briefing.job_daily_briefing()
        self.assertFalse(nb_briefing._BRIEFING_LOCK.locked())


class InterruptedCommandTests(unittest.TestCase):
    def setUp(self):
        nb_state.STATE.data["inflight_commands"] = []
        self.addCleanup(lambda: nb_state.STATE.data.pop("inflight_commands", None))

    def test_running_command_is_persisted_then_cleared(self):
        release, started = threading.Event(), threading.Event()

        def slow(command, chat_id):
            started.set()
            release.wait(5)

        with patch("newsbot.commands._handle_command", side_effect=slow), \
             patch.object(nb_state.STATE, "save"):
            nb_commands._dispatch_command("/performance", "42")
            self.assertTrue(started.wait(2))
            self.assertEqual(nb_state.STATE.data["inflight_commands"], [["42", "/performance"]])
            release.set()
            for _ in range(100):
                if not nb_state.STATE.data["inflight_commands"]:
                    break
                time.sleep(0.02)
        self.assertEqual(nb_state.STATE.data["inflight_commands"], [])

    def test_startup_reports_and_clears_interrupted_commands(self):
        nb_state.STATE.data["inflight_commands"] = [["42", "/performance"], ["7", "/analyze <x>"]]
        with patch.object(nb_messaging, "send_telegram_message", return_value=True) as send, \
             patch.object(nb_state.STATE, "save"):
            self.assertEqual(nb_commands.notify_interrupted_commands(), 2)
        self.assertEqual(nb_state.STATE.data["inflight_commands"], [])
        self.assertIn("/performance", send.call_args_list[0].args[0])
        self.assertIn("&lt;x&gt;", send.call_args_list[1].args[0])

    def test_nothing_interrupted_sends_nothing(self):
        with patch.object(nb_messaging, "send_telegram_message") as send:
            self.assertEqual(nb_commands.notify_interrupted_commands(), 0)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
