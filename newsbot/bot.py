"""Process entry point: scheduler, command poller and startup."""

import sys
import threading
from typing import Optional

from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from newsbot.backup import job_backup_trade_history
from newsbot.briefing import job_catchup_briefing, job_health_ping, job_scheduled_briefing, job_daily_briefing
from newsbot.commands import (
    _prepare_telegram_long_polling,
    notify_interrupted_commands,
    _touch_heartbeat,
    run_command_long_polling_loop,
    run_healthcheck,
)
from newsbot.config import SETTINGS, get_missing_required_config
from newsbot.forward_test import job_settle_forward_test
from newsbot.scores import job_build_scores
from newsbot.history_store import job_settle_published_picks
from newsbot.data_checks import job_load_company_numbers_and_check, job_load_prices_and_check
from newsbot.holding_alerts import job_holding_alerts
from newsbot.scorecard import job_monthly_scorecard
from newsbot.snapshots import job_save_snapshots


def main() -> None:
    if "--healthcheck" in sys.argv[1:]:
        raise SystemExit(run_healthcheck())

    missing_values = list(get_missing_required_config())
    if missing_values:
        print("Missing required environment variables: " + ", ".join(missing_values) + ".")
        raise SystemExit(1)

    _prepare_telegram_long_polling()
    notify_interrupted_commands()

    # A small thread pool with single-instance jobs prevents overlapping runs
    # from racing on shared state, caches, and the state file.
    scheduler = BlockingScheduler(
        timezone=SETTINGS.tz,
        executors={"default": APSThreadPoolExecutor(max_workers=2)},
        # misfire_grace_time defaults to 1s, so a briefing that was due while
        # both workers were busy would be dropped rather than run late.
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600},
    )
    scheduler.add_job(job_scheduled_briefing, "cron", hour="7,19", minute=0)
    scheduler.add_job(job_health_ping, "cron", hour=12, minute=0)
    # 22:30, weekdays: after the Oslo, European and US closes.
    scheduler.add_job(job_holding_alerts, "cron", day_of_week="mon-fri", hour=22, minute=30)
    scheduler.add_job(job_monthly_scorecard, "cron", day=1, hour=8, minute=0)
    # 06:30 and 18:30 (#89). In the small hours Yahoo often has no finished bar
    # yet for the day before: at 02:00-03:00 US results stayed pending although
    # the session had closed, and European ones failed on empty bars that were
    # fine by afternoon. By 06:30 (00:30 in New York) the bars are in; the 18:30
    # run retries whatever was still missing. Each run settles every row that
    # has become computable, so a missed run costs nothing.
    scheduler.add_job(job_settle_forward_test, "cron", hour="6,18", minute=30)
    # 06:00 Tuesday to Saturday, after each weekday's sessions: our own copy of
    # daily prices for the stock list (#75). Midnight in New York; at 02:00 the
    # US bar for the day was often still missing (#89).
    scheduler.add_job(job_load_prices_and_check, "cron", day_of_week="tue-sat", hour=6, minute=0)
    # Sunday 05:00: company numbers from annual reports change a few times a
    # year, so weekly is plenty, and it stays clear of the weekday jobs (#76).
    scheduler.add_job(job_load_company_numbers_and_check, "cron", day_of_week="sun", hour=5, minute=0)
    # Sunday 07:30: score inputs per month end (#145), then the four company
    # scores (#146-#150), after that morning's company numbers (05:00).
    scheduler.add_job(job_build_scores, "cron", day_of_week="sun", hour=7, minute=30)
    # 02:30: analyst estimates and company numbers, before the 04:00 backup.
    scheduler.add_job(job_save_snapshots, "cron", hour=2, minute=30)
    # Same idea for the picks actually sent to Slack/Telegram; a quarter of an
    # hour later so the two jobs do not hit the price provider at the same time.
    scheduler.add_job(job_settle_published_picks, "cron", hour="6,18", minute=45)
    # 04:00, before the host copies backups to the NAS at 04:30. Outcomes the
    # morning settle adds are in the next night's copy; they can be worked out
    # again anyway. Separate jobs rather than one chained call because a settle
    # that fails should still be followed by a backup -- the rows already in
    # the archive are what is irreplaceable.
    scheduler.add_job(job_backup_trade_history, "cron", hour=4, minute=0)
    scheduler.add_job(_touch_heartbeat, "interval", minutes=1, args=["scheduler"])
    _touch_heartbeat("scheduler")
    # One-off, run by the scheduler as soon as it starts: sends a briefing the
    # previous process missed by being down at its slot.
    scheduler.add_job(job_catchup_briefing, "date")

    command_stop_event = threading.Event()
    command_thread: Optional[threading.Thread] = None
    if SETTINGS.command_poll_enabled:
        command_thread = threading.Thread(
            target=run_command_long_polling_loop,
            args=(command_stop_event,),
            name="telegram-command-poller",
            daemon=True,
        )
        command_thread.start()

    if SETTINGS.send_startup_briefing:
        print("--------------------------------------------------")
        print("RUNNING AN INSTANT TEST BRIEFING NOW...")
        print("--------------------------------------------------")
        job_daily_briefing()
    else:
        print("Startup briefing skipped (SEND_STARTUP_BRIEFING=false).")

    print("\nInitialization finished. Bot is waiting for schedule and commands.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        command_stop_event.set()
