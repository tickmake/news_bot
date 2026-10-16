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
    # 03:00: the last US session closed hours ago, so its bar is final and the
    # in-progress-bar guard will not discard it. Each run settles every row that
    # has become computable, not just yesterday's, so a night the Pi spends down
    # costs nothing -- the backlog clears on the next run.
    scheduler.add_job(job_settle_forward_test, "cron", hour=3, minute=0)
    # 03:30: half an hour after the settle job, so the snapshot carries that
    # night's outcomes. The two are separate jobs rather than one chained call
    # because a settle that fails should still be followed by a backup -- the
    # rows already in the archive are what is irreplaceable.
    scheduler.add_job(job_backup_trade_history, "cron", hour=3, minute=30)
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
