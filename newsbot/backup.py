"""Nightly snapshots of the trade-history database.

Code and docs live in git and can be rebuilt from it. `signal_archive` and
`published_picks` cannot: they accrue one session per trading day, and a
settled outcome needs price history covering its signal session, which renamed
and delisted symbols stop returning. Losing the database does not cost a
rebuild, it costs the elapsed time -- the out-of-sample clock restarts at zero
and the roughly three-month wait for a verdict starts again.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from newsbot import history_store
from newsbot.config import SETTINGS
from newsbot.logsetup import APP_LOG, SYS_LOG

# Dated snapshots, newest sorting last. The suffix is matched exactly so
# pruning can never reach a file this job did not write.
BACKUP_PREFIX = "trade_history-"
BACKUP_SUFFIX = ".db"
_BACKUP_NAME = re.compile(rf"^{re.escape(BACKUP_PREFIX)}\d{{8}}-\d{{6}}{re.escape(BACKUP_SUFFIX)}$")


def _existing_backups(directory: str) -> List[str]:
    """Snapshot filenames this job wrote, oldest first."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(name for name in names if _BACKUP_NAME.match(name))


def _verify(path: str) -> int:
    """Rows in `signal_archive`, after SQLite agrees the file is intact.

    A snapshot is only worth keeping if it opens, so it is checked here rather
    than on the night it is needed. `integrity_check` reads every page, which
    on a database this size costs milliseconds.
    """
    conn = sqlite3.connect(path, timeout=10)
    try:
        verdict = conn.execute("PRAGMA integrity_check").fetchone()
        if not verdict or verdict[0] != "ok":
            raise ValueError(f"integrity_check said {verdict[0] if verdict else 'nothing'}")
        return int(conn.execute("SELECT COUNT(*) FROM signal_archive").fetchone()[0])
    finally:
        conn.close()


def _prune(directory: str, keep: int) -> int:
    """Drop the oldest snapshots beyond `keep`. Returns how many went."""
    if keep <= 0:
        return 0
    stale = _existing_backups(directory)[:-keep]
    removed = 0
    for name in stale:
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError as exc:
            SYS_LOG.warning("backup_prune_failed name=%s detail=%s", name, exc)
    return removed


def backup_trade_history(now: Optional[datetime] = None) -> Optional[str]:
    """Write one verified snapshot and prune old ones. Returns its path.

    Pruning happens only after the new snapshot verifies, so a database that
    has gone bad cannot evict the last good copy of itself -- the failure mode
    that makes a backup rotation worse than no rotation at all.
    """
    directory = SETTINGS.trade_backup_dir.strip()
    if not directory:
        return None

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}")

    try:
        history_store.TRADE_HISTORY.backup_to(path)
        rows = _verify(path)
    except Exception:
        # A half-written or unreadable file is worse than none: it reads as a
        # backup in a directory listing and only reveals itself on restore.
        try:
            os.remove(path)
        except OSError:
            pass
        raise

    removed = _prune(directory, SETTINGS.trade_backup_keep)
    APP_LOG.info(
        "trade_history_backup path=%s archive_rows=%d pruned=%d", path, rows, removed
    )
    return path


def job_backup_trade_history() -> bool:
    """Snapshot the trade-history DB. Never raises.

    Runs after the nightly settle so the snapshot carries that night's
    outcomes rather than lagging them by a day.
    """
    try:
        path = backup_trade_history()
    except Exception as exc:
        SYS_LOG.error("trade_history_backup_failed detail=%s", exc)
        return False
    if path is None:
        SYS_LOG.warning("trade_history_backup_unconfigured")
        return False
    return True
