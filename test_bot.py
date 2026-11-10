"""bot.main: argument handling, scheduler wiring, poller thread, shutdown."""

import unittest
from unittest.mock import MagicMock, patch

from newsbot import bot as nb_bot
from newsbot import config as nb_config


class _MainHarness(unittest.TestCase):
    """Runs main() with the scheduler, poller thread and network-facing steps faked."""

    def run_main(self, argv=("news_bot.py",), *, poll=True, startup=False, missing=(),
                 scheduler_start=None, briefing_effect=None):
        scheduler = MagicMock()
        if scheduler_start is not None:
            scheduler.start.side_effect = scheduler_start
        thread = MagicMock()
        patches = {
            "scheduler_cls": patch.object(nb_bot, "BlockingScheduler", return_value=scheduler),
            "executor": patch.object(nb_bot, "APSThreadPoolExecutor"),
            "thread_cls": patch.object(nb_bot.threading, "Thread", return_value=thread),
            "prepare": patch.object(nb_bot, "_prepare_telegram_long_polling"),
            "notify": patch.object(nb_bot, "notify_interrupted_commands"),
            "heartbeat": patch.object(
                nb_bot, "_touch_heartbeat", new_callable=lambda: MagicMock(name="_touch_heartbeat")
            ),
            "briefing": patch.object(nb_bot, "job_daily_briefing", side_effect=briefing_effect),
            "missing": patch.object(nb_bot, "get_missing_required_config", return_value=list(missing)),
            "argv": patch.object(nb_bot.sys, "argv", list(argv)),
            "poll": patch.object(nb_config.SETTINGS, "command_poll_enabled", poll),
            "startup": patch.object(nb_config.SETTINGS, "send_startup_briefing", startup),
            "print": patch("builtins.print"),
        }
        mocks = {}
        for name, p in patches.items():
            mocks[name] = p.start()
            self.addCleanup(p.stop)
        mocks["scheduler"] = scheduler
        mocks["thread"] = thread
        # Kept on the instance so a test can inspect it even when main() raises.
        self.mocks = mocks
        nb_bot.main()
        return mocks


class HealthcheckAndConfigTests(_MainHarness):
    def test_healthcheck_flag_exits_with_the_check_result(self):
        for result in (0, 1):
            with patch.object(nb_bot, "run_healthcheck", return_value=result):
                with self.assertRaises(SystemExit) as raised:
                    self.run_main(argv=("news_bot.py", "--healthcheck"))
            self.assertEqual(raised.exception.code, result)

    def test_healthcheck_does_not_start_anything(self):
        with patch.object(nb_bot, "run_healthcheck", return_value=0), \
             patch.object(nb_bot, "BlockingScheduler") as scheduler_cls, \
             self.assertRaises(SystemExit):
            self.run_main(argv=("news_bot.py", "--healthcheck"))
        scheduler_cls.assert_not_called()

    def test_missing_required_config_exits_nonzero_and_names_the_variables(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_main(missing=("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"))
        self.assertEqual(raised.exception.code, 1)
        printed = " ".join(str(c.args[0]) for c in self.mocks["print"].call_args_list)
        self.assertIn("TELEGRAM_TOKEN", printed)
        self.assertIn("TELEGRAM_CHAT_ID", printed)
        self.mocks["scheduler_cls"].assert_not_called()


class SchedulerWiringTests(_MainHarness):
    def _jobs(self, mocks):
        def name(func):
            return getattr(func, "__name__", None) or func._mock_name

        return {name(call.args[0]): call for call in mocks["scheduler"].add_job.call_args_list}

    def test_jobs_are_registered_with_expected_triggers(self):
        mocks = self.run_main()
        jobs = self._jobs(mocks)
        briefing = jobs["job_scheduled_briefing"]
        self.assertEqual(briefing.args[1], "cron")
        self.assertEqual(briefing.kwargs, {"hour": "7,19", "minute": 0})
        health = jobs["job_health_ping"]
        self.assertEqual((health.args[1], health.kwargs["hour"]), ("cron", 12))
        alerts = jobs["job_holding_alerts"]
        self.assertEqual((alerts.args[1], alerts.kwargs),
                         ("cron", {"day_of_week": "mon-fri", "hour": 22, "minute": 30}))
        monthly = jobs["job_monthly_scorecard"]
        self.assertEqual((monthly.args[1], monthly.kwargs), ("cron", {"day": 1, "hour": 8, "minute": 0}))
        settle = jobs["job_settle_forward_test"]
        self.assertEqual((settle.args[1], settle.kwargs), ("cron", {"hour": 3, "minute": 0}))
        prices = jobs["job_load_prices"]
        self.assertEqual((prices.args[1], prices.kwargs),
                         ("cron", {"day_of_week": "tue-sat", "hour": 2, "minute": 0}))
        numbers = jobs["job_load_company_numbers"]
        self.assertEqual((numbers.args[1], numbers.kwargs),
                         ("cron", {"day_of_week": "sun", "hour": 5, "minute": 0}))
        estimates = jobs["job_save_snapshots"]
        self.assertEqual((estimates.args[1], estimates.kwargs), ("cron", {"hour": 2, "minute": 30}))
        picks = jobs["job_settle_published_picks"]
        self.assertEqual((picks.args[1], picks.kwargs), ("cron", {"hour": 3, "minute": 30}))
        snapshot = jobs["job_backup_trade_history"]
        self.assertEqual(
            (snapshot.args[1], snapshot.kwargs), ("cron", {"hour": 4, "minute": 0})
        )
        beat = jobs["_touch_heartbeat"]
        self.assertEqual((beat.args[1], beat.kwargs["minutes"], beat.kwargs["args"]),
                         ("interval", 1, ["scheduler"]))
        self.assertEqual(jobs["job_catchup_briefing"].args[1], "date")

    def test_scheduler_is_configured_for_single_late_tolerant_runs(self):
        mocks = self.run_main()
        kwargs = mocks["scheduler_cls"].call_args.kwargs
        self.assertEqual(kwargs["timezone"], nb_config.SETTINGS.tz)
        self.assertEqual(kwargs["job_defaults"]["max_instances"], 1)
        self.assertTrue(kwargs["job_defaults"]["coalesce"])
        self.assertEqual(kwargs["job_defaults"]["misfire_grace_time"], 3600)
        mocks["executor"].assert_called_once_with(max_workers=2)

    def test_startup_order_prepares_polling_before_notifying_and_beats_once(self):
        mocks = self.run_main()
        mocks["prepare"].assert_called_once_with()
        mocks["notify"].assert_called_once_with()
        mocks["heartbeat"].assert_called_once_with("scheduler")
        mocks["scheduler"].start.assert_called_once_with()


class PollerAndStartupTests(_MainHarness):
    def test_poller_thread_is_a_named_daemon_when_enabled(self):
        mocks = self.run_main(poll=True)
        kwargs = mocks["thread_cls"].call_args.kwargs
        self.assertTrue(kwargs["daemon"])
        self.assertEqual(kwargs["name"], "telegram-command-poller")
        self.assertIs(kwargs["target"], nb_bot.run_command_long_polling_loop)
        mocks["thread"].start.assert_called_once_with()

    def test_no_poller_thread_when_polling_disabled(self):
        mocks = self.run_main(poll=False)
        mocks["thread_cls"].assert_not_called()

    def test_startup_briefing_only_runs_when_enabled(self):
        self.assertFalse(self.run_main(startup=False)["briefing"].called)
        mocks = self.run_main(startup=True)
        mocks["briefing"].assert_called_once_with()

    def test_startup_briefing_runs_before_the_scheduler_starts(self):
        order = []
        self.run_main(
            startup=True,
            briefing_effect=lambda: order.append("briefing"),
            scheduler_start=lambda: order.append("scheduler"),
        )
        self.assertEqual(order, ["briefing", "scheduler"])


class ShutdownTests(_MainHarness):
    def test_stop_event_is_set_when_the_scheduler_exits_normally(self):
        mocks = self.run_main()
        stop_event = mocks["thread_cls"].call_args.kwargs["args"][0]
        self.assertTrue(stop_event.is_set())

    def test_keyboard_interrupt_and_system_exit_are_a_clean_shutdown(self):
        for exc in (KeyboardInterrupt, SystemExit):
            def stop(exc=exc):
                raise exc()
            mocks = self.run_main(scheduler_start=stop)
            self.assertTrue(mocks["thread_cls"].call_args.kwargs["args"][0].is_set())

    def test_unexpected_scheduler_error_propagates_but_still_stops_the_poller(self):
        def boom():
            raise RuntimeError("scheduler broke")

        with self.assertRaises(RuntimeError):
            self.run_main(scheduler_start=boom)
        self.assertTrue(self.mocks["thread_cls"].call_args.kwargs["args"][0].is_set())


if __name__ == "__main__":
    unittest.main()
