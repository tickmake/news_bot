"""Process entry point: scheduler, command poller and startup."""

import sys
import threading
from typing import Optional

from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from newsbot.briefing import job_catchup_briefing, job_health_ping, job_scheduled_briefing, job_daily_briefing
from newsbot.commands import (
    _prepare_telegram_long_polling,
    _touch_heartbeat,
    run_command_long_polling_loop,
    run_healthcheck,
)
from newsbot.config import SETTINGS, get_missing_required_config


def main() -> None:
    if "--healthcheck" in sys.argv[1:]:
        raise SystemExit(run_healthcheck())

    missing_values = list(get_missing_required_config())
    if missing_values:
        print("Missing required environment variables: " + ", ".join(missing_values) + ".")
        raise SystemExit(1)

    _prepare_telegram_long_polling()

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
