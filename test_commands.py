"""commands: routing, authorisation, polling, heartbeats, webhook preparation."""

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from newsbot import commands as cmd
from newsbot import config as nb_config
from newsbot import logsetup as nb_logsetup
from newsbot import messaging as nb_messaging
from newsbot import state as nb_state


class RoutingTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(nb_messaging, "_broadcast_message", return_value=True)
        self.broadcast = patcher.start()
        self.addCleanup(patcher.stop)

    def _routes(self):
        return {
            "/stocks": ("newsbot.reports.build_weekly_top_picks", "STOCKS"),
            "/watchlist": ("newsbot.screener.build_watchlist_report", "WATCH"),
            "/performance": ("newsbot.backtest_reports.build_trade_performance_report", "PERF"),
            "/pickperformance": ("newsbot.history_store.build_published_pick_report", "PICKS"),
            "/forwardtest": ("newsbot.forward_test.build_forward_test_report", "FORWARD"),
            "/diagnostics": ("newsbot.backtest_reports.build_trade_diagnostics_report", "DIAG"),
            "/health": ("newsbot.commands.build_health_report", "HEALTH"),
        }

    def test_each_report_command_broadcasts_its_builder_output_to_the_asking_chat(self):
        for command, (target, text) in self._routes().items():
            self.broadcast.reset_mock()
            with patch(target, return_value=text):
                cmd._handle_command(command, "42")
            self.broadcast.assert_called_once_with(text, chat_id="42")

    def test_command_matching_ignores_case_and_surrounding_space(self):
        with patch("newsbot.reports.build_weekly_top_picks", return_value="STOCKS"):
            cmd._handle_command("  /STOCKS  ", "1")
        self.broadcast.assert_called_once_with("STOCKS", chat_id="1")

    def test_now_sends_a_briefing_to_the_chat(self):
        with patch.object(cmd, "_send_briefing") as send:
            cmd._handle_command("/now", "7")
        self.assertEqual(send.call_args.args[1], "7")

    def test_news_joins_the_three_news_sections(self):
        with patch("newsbot.news_feeds.get_norwegian_morning_news", return_value=("N\n", [])), \
             patch("newsbot.news_feeds.get_global_news", return_value=("G\n", [])), \
             patch("newsbot.market_data._business_news_section", return_value=("B\n", [])):
            cmd._handle_command("/news", "7")
        self.broadcast.assert_called_once_with("N\nG\nB", chat_id="7")

    def test_recommendations_records_picks_and_charts_after_delivery(self):
        outcome = MagicMock()
        outcome.qualified = list(range(8))
        with patch("newsbot.screener.build_trade_candidates_payload", return_value=("TXT", outcome, {"x": "y"})), \
             patch.object(cmd, "_record_published_picks") as record, \
             patch("newsbot.charts.send_candidate_charts") as charts:
            cmd._handle_command("/recommendations", "7")
        record.assert_called_once_with(list(range(5)))
        charts.assert_called_once_with(outcome, {"x": "y"}, chat_id="7")

    def test_recommendations_does_not_record_picks_when_delivery_failed(self):
        self.broadcast.return_value = False
        outcome = MagicMock()
        outcome.qualified = [1]
        with patch("newsbot.screener.build_trade_candidates_payload", return_value=("TXT", outcome, {})), \
             patch.object(cmd, "_record_published_picks") as record, \
             patch("newsbot.charts.send_candidate_charts") as charts:
            cmd._handle_command("/recommendations", "7")
        record.assert_not_called()
        charts.assert_called_once()  # charts still follow; they decorate the text

    def test_analyze_needs_a_ticker(self):
        cmd._handle_command("/analyze", "7")
        self.assertIn("Usage: /analyze", self.broadcast.call_args.args[0])

    def test_analyze_passes_the_ticker_through(self):
        with patch("newsbot.reports.build_ticker_analysis", return_value="REPORT") as build:
            cmd._handle_command("/analyze  aapl ", "7")
        build.assert_called_once_with("aapl")
        self.broadcast.assert_called_once_with("REPORT", chat_id="7")

    def test_unknown_command_gets_the_help_text(self):
        cmd._handle_command("/nonsense", "7")
        self.assertEqual(self.broadcast.call_args.args[0], cmd._command_help_text())

    def test_help_lists_every_routed_command(self):
        text = cmd._command_help_text()
        for command in ("/now", "/news", "/recommendations", "/stocks", "/watchlist", "/analyze",
                        "/performance", "/diagnostics", "/pickperformance", "/forwardtest", "/health"):
            self.assertIn(command, text)


class AuthorisationTests(unittest.TestCase):
    def test_only_configured_chats_are_allowed(self):
        with patch.object(nb_config.SETTINGS, "telegram_chat_id", "111"), \
             patch.object(nb_config.SETTINGS, "health_ping_chat_id", "222"):
            self.assertEqual(cmd._authorized_chat_ids(), {"111", "222"})

    def test_unset_chats_are_not_wildcards(self):
        with patch.object(nb_config.SETTINGS, "telegram_chat_id", "111"), \
             patch.object(nb_config.SETTINGS, "health_ping_chat_id", None):
            self.assertEqual(cmd._authorized_chat_ids(), {"111"})
        with patch.object(nb_config.SETTINGS, "telegram_chat_id", ""), \
             patch.object(nb_config.SETTINGS, "health_ping_chat_id", ""):
            self.assertEqual(cmd._authorized_chat_ids(), set())


def _updates(*items):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"result": list(items)}
    response.raise_for_status.return_value = None
    return response


def _message(update_id, text, chat="111", key="message"):
    return {"update_id": update_id, key: {"chat": {"id": int(chat)}, "text": text}}


class PollingTests(unittest.TestCase):
    def setUp(self):
        for p in (
            patch.object(nb_config.SETTINGS, "command_poll_enabled", True),
            patch.object(nb_config.SETTINGS, "telegram_token", "TOKEN"),
            patch.object(nb_config.SETTINGS, "telegram_chat_id", "111"),
            patch.object(nb_config.SETTINGS, "health_ping_chat_id", None),
            patch.object(nb_state.STATE, "save"),
            patch.dict(nb_state.STATE.data, {"telegram_update_offset": 0}),
        ):
            p.start()
            self.addCleanup(p.stop)
        dispatch = patch.object(cmd, "_dispatch_command")
        self.dispatch = dispatch.start()
        self.addCleanup(dispatch.stop)

    def test_disabled_or_tokenless_polling_is_a_successful_noop(self):
        with patch("requests.get") as get:
            with patch.object(nb_config.SETTINGS, "command_poll_enabled", False):
                self.assertTrue(cmd.poll_telegram_commands())
            with patch.object(nb_config.SETTINGS, "telegram_token", ""):
                self.assertTrue(cmd.poll_telegram_commands())
        get.assert_not_called()

    def test_authorised_command_is_dispatched_and_the_offset_advances(self):
        with patch("requests.get", return_value=_updates(_message(10, "/health"))):
            self.assertTrue(cmd.poll_telegram_commands())
        self.dispatch.assert_called_once_with("/health", "111")
        self.assertEqual(nb_state.STATE.telegram_update_offset, 10)

    def test_edited_messages_count_too(self):
        with patch("requests.get", return_value=_updates(_message(11, "/news", key="edited_message"))):
            cmd.poll_telegram_commands()
        self.dispatch.assert_called_once_with("/news", "111")

    def test_stranger_and_plain_text_are_ignored_but_acknowledged(self):
        updates = _updates(_message(12, "/health", chat="999"), _message(13, "hello there"), {"update_id": 14})
        with patch("requests.get", return_value=updates):
            self.assertTrue(cmd.poll_telegram_commands())
        self.dispatch.assert_not_called()
        self.assertEqual(nb_state.STATE.telegram_update_offset, 14)

    def test_one_bad_update_does_not_block_the_rest_or_the_offset(self):
        self.dispatch.side_effect = [RuntimeError("boom"), None]
        updates = _updates(_message(20, "/a"), _message(21, "/b"))
        with patch("requests.get", return_value=updates):
            self.assertTrue(cmd.poll_telegram_commands())
        self.assertEqual(self.dispatch.call_count, 2)
        self.assertEqual(nb_state.STATE.telegram_update_offset, 21)

    def test_offset_is_sent_as_the_next_expected_update(self):
        nb_state.STATE.telegram_update_offset = 50
        with patch("requests.get", return_value=_updates()) as get:
            cmd.poll_telegram_commands(long_poll_timeout=25)
        params = get.call_args.kwargs["params"]
        self.assertEqual((params["offset"], params["timeout"]), (51, 25))
        self.assertGreater(get.call_args.kwargs["timeout"], 25)

    def test_negative_long_poll_timeout_is_clamped(self):
        with patch("requests.get", return_value=_updates()) as get:
            cmd.poll_telegram_commands(long_poll_timeout=-5)
        self.assertEqual(get.call_args.kwargs["params"]["timeout"], 0)

    def test_transport_failure_reports_false(self):
        with patch("time.sleep"), patch("requests.get", side_effect=requests.exceptions.ConnectionError("down")):
            self.assertFalse(cmd.poll_telegram_commands())

    def test_http_error_reports_false(self):
        response = _updates()
        response.raise_for_status.side_effect = requests.exceptions.HTTPError("409 conflict")
        with patch("requests.get", return_value=response):
            self.assertFalse(cmd.poll_telegram_commands())

    def test_failure_log_lines_do_not_contain_the_token(self):
        secret = "123456789:AAH-realistic_Token_value_1234567890"
        error = requests.exceptions.ConnectionError(f"https://api.telegram.org/bot{secret}/getUpdates failed")
        with patch("time.sleep"), patch("requests.get", side_effect=error), \
             self.assertLogs("news_bot", level="ERROR") as logs:
            cmd.poll_telegram_commands()
        formatter = nb_logsetup.BandedFormatter(colour=False)
        rendered = "\n".join(formatter.format(record) for record in logs.records)
        self.assertIn("command_poll_failed", rendered)
        self.assertNotIn("AAH-realistic", rendered)


class PollingLoopTests(unittest.TestCase):
    def test_successful_polls_beat_and_failures_back_off_until_stopped(self):
        stop = threading.Event()
        results = iter([True, False, True])

        def poll(long_poll_timeout):
            try:
                return next(results)
            except StopIteration:
                stop.set()
                return True

        waits = []
        with patch.object(cmd, "poll_telegram_commands", side_effect=poll), \
             patch.object(cmd, "_touch_heartbeat") as beat, \
             patch.object(stop, "wait", side_effect=lambda seconds: waits.append(seconds) or False):
            cmd.run_command_long_polling_loop(stop)
        self.assertEqual(beat.call_count, 3)  # two good polls plus the one that set the stop flag
        self.assertEqual(waits, [5])

    def test_without_a_stop_event_failures_sleep(self):
        calls = {"n": 0}

        def poll(long_poll_timeout):
            calls["n"] += 1
            if calls["n"] > 1:
                raise SystemExit
            return False

        with patch.object(cmd, "poll_telegram_commands", side_effect=poll), \
             patch.object(cmd, "_touch_heartbeat") as beat, \
             patch("time.sleep") as sleep:
            with self.assertRaises(SystemExit):
                cmd.run_command_long_polling_loop(None)
        sleep.assert_called_once_with(5)
        beat.assert_not_called()


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for p in (
            patch.object(nb_config.SETTINGS, "state_file", os.path.join(self.dir, "state.json")),
            patch.object(nb_config.SETTINGS, "command_poll_enabled", True),
            patch.object(nb_config.SETTINGS, "telegram_token", "t"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_beats_live_next_to_the_state_file(self):
        cmd._touch_heartbeat("scheduler")
        self.assertTrue(os.path.exists(os.path.join(self.dir, ".news_bot_heartbeat_scheduler")))

    def test_write_failure_is_swallowed(self):
        with patch("builtins.open", side_effect=OSError("disk full")):
            cmd._touch_heartbeat("scheduler")  # must not raise

    def test_missing_heartbeats_are_reported(self):
        self.assertEqual(
            cmd.heartbeat_problems(), ["scheduler: no heartbeat", "poller: no heartbeat"]
        )

    def test_fresh_beats_are_healthy(self):
        cmd._touch_heartbeat("scheduler")
        cmd._touch_heartbeat("poller")
        self.assertEqual(cmd.heartbeat_problems(), [])

    def test_stale_beats_are_reported_with_their_age(self):
        cmd._touch_heartbeat("scheduler")
        cmd._touch_heartbeat("poller")
        future = time.time() + 400
        problems = cmd.heartbeat_problems(now=future)
        self.assertEqual(len(problems), 1)  # poller allows 300s, scheduler 900s
        self.assertTrue(problems[0].startswith("poller: last beat"))

    def test_poller_is_not_required_when_polling_is_off(self):
        cmd._touch_heartbeat("scheduler")
        with patch.object(nb_config.SETTINGS, "command_poll_enabled", False):
            self.assertEqual(cmd.heartbeat_problems(), [])

    def test_healthcheck_exit_codes(self):
        with patch("builtins.print") as printed:
            self.assertEqual(cmd.run_healthcheck(), 1)
            self.assertTrue(printed.call_args.args[0].startswith("unhealthy:"))
        cmd._touch_heartbeat("scheduler")
        cmd._touch_heartbeat("poller")
        with patch("builtins.print") as printed:
            self.assertEqual(cmd.run_healthcheck(), 0)
            printed.assert_called_with("healthy")


class WebhookPreparationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(nb_config.SETTINGS, "telegram_token", "TOKEN")
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch("time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    @staticmethod
    def _info(url):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"result": {"url": url}}
        return response

    def test_no_token_means_no_calls(self):
        with patch.object(nb_config.SETTINGS, "telegram_token", ""), patch("requests.get") as get:
            cmd._prepare_telegram_long_polling()
        get.assert_not_called()

    def test_an_active_webhook_is_removed_without_dropping_updates(self):
        with patch("requests.get", return_value=self._info("https://example.com/hook")), \
             patch("requests.post", return_value=MagicMock(status_code=200)) as post:
            cmd._prepare_telegram_long_polling()
        self.assertTrue(post.call_args.args[0].endswith("/deleteWebhook"))
        self.assertEqual(post.call_args.kwargs["json"], {"drop_pending_updates": False})

    def test_no_webhook_means_nothing_to_delete(self):
        with patch("requests.get", return_value=self._info("")), patch("requests.post") as post:
            cmd._prepare_telegram_long_polling()
        post.assert_not_called()

    def test_failures_are_logged_not_raised(self):
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("down")), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            cmd._prepare_telegram_long_polling()
        self.assertIn("telegram_webhook_check_failed", "\n".join(logs.output))


class DispatchEdgeTests(unittest.TestCase):
    def test_failure_notice_that_itself_fails_is_contained(self):
        with patch.object(cmd, "_handle_command", side_effect=RuntimeError("x")), \
             patch.object(nb_messaging, "send_telegram_message", side_effect=RuntimeError("telegram down")), \
             patch.object(nb_state.STATE, "save"):
            cmd._COMMANDS_IN_FLIGHT.add(("9", "/now"))
            cmd._run_command_safely("/now", "9", "/now", ("9", "/now"))
        self.assertNotIn(("9", "/now"), cmd._COMMANDS_IN_FLIGHT)

    def test_interrupted_notice_skips_malformed_entries(self):
        with patch.dict(nb_state.STATE.data, {"inflight_commands": [["1"], 7, None, ["5", "/news"]]}), \
             patch.object(nb_messaging, "send_telegram_message", return_value=True) as send, \
             patch.object(nb_state.STATE, "save"):
            self.assertEqual(cmd.notify_interrupted_commands(), 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["chat_id"], "5")


if __name__ == "__main__":
    unittest.main()
