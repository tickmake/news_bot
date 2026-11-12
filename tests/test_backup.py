"""Nightly snapshots of the trade-history database."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from newsbot import (
    backup as nb_backup,
    config as nb_config,
    history_store as nb_history_store,
    indicators as nb_indicators,
)


def _metrics(symbol="AAA", session="2026-09-01", **overrides):
    base = {
        "symbol": symbol, "session": session, "last_close": 100.0, "day_change_pct": 1.0,
        "week_momentum_pct": 3.0, "volume_ratio": 1.5, "drawdown_pct": 2.0, "atr_pct": 2.0,
        "above_ema20": True, "ema20": 98.0, "rsi_14": 60.0, "support_level": 95.0,
        "resistance_level": 102.0, "volume_trend": "flat",
    }
    base.update(overrides)
    return nb_indicators.TradeMetrics(**base)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(
            os.path.join(tempfile.mkdtemp(), "h.db")
        )
        self.addCleanup(self.store.close)
        patcher = patch.object(nb_history_store, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backup_dir = tempfile.mkdtemp()
        self._configure(dir_=self.backup_dir)

    def _configure(self, dir_=None, keep=14):
        patcher = patch.object(
            nb_config.SETTINGS, "trade_backup_dir", self.backup_dir if dir_ is None else dir_
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        keeper = patch.object(nb_config.SETTINGS, "trade_backup_keep", keep)
        keeper.start()
        self.addCleanup(keeper.stop)

    def _snapshots(self):
        return nb_backup._existing_backups(self.backup_dir)

    def test_the_snapshot_carries_the_archive_rows(self):
        self.store.archive_signal(_metrics("AAA"), True, None)
        self.store.archive_signal(_metrics("BBB"), False, "trend")

        path = nb_backup.backup_trade_history()

        self.assertTrue(os.path.exists(path))
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT symbol FROM signal_archive ORDER BY symbol").fetchall()
        self.assertEqual([row[0] for row in rows], ["AAA", "BBB"])

    def test_the_snapshot_is_named_for_the_moment_it_was_taken(self):
        path = nb_backup.backup_trade_history(now=datetime(2026, 10, 16, 3, 30, 5, tzinfo=timezone.utc))
        self.assertEqual(os.path.basename(path), "trade_history-20261016-033005.db")

    def test_writes_to_a_directory_that_does_not_exist_yet(self):
        nested = os.path.join(self.backup_dir, "deeper")
        self._configure(dir_=nested)
        self.assertTrue(os.path.exists(nb_backup.backup_trade_history()))

    def test_an_unconfigured_directory_writes_nothing(self):
        self._configure(dir_="   ")
        self.assertIsNone(nb_backup.backup_trade_history())
        self.assertEqual(self._snapshots(), [])

    def test_a_failed_copy_leaves_no_partial_file_behind(self):
        with patch.object(self.store, "backup_to", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                nb_backup.backup_trade_history()
        self.assertEqual(self._snapshots(), [])

    def test_a_snapshot_that_fails_verification_is_discarded(self):
        with patch.object(nb_backup, "_verify", side_effect=ValueError("integrity_check said bad")):
            with self.assertRaises(ValueError):
                nb_backup.backup_trade_history()
        self.assertEqual(self._snapshots(), [])

    def test_a_corrupt_database_cannot_evict_the_last_good_copy(self):
        """The failure mode that makes rotation worse than no rotation."""
        self._configure(keep=1)
        good = nb_backup.backup_trade_history(now=datetime(2026, 10, 14, 3, 30, tzinfo=timezone.utc))
        with patch.object(nb_backup, "_verify", side_effect=ValueError("malformed")):
            with self.assertRaises(ValueError):
                nb_backup.backup_trade_history(now=datetime(2026, 10, 15, 3, 30, tzinfo=timezone.utc))
        self.assertEqual(self._snapshots(), [os.path.basename(good)])

    def test_verification_rejects_a_file_sqlite_will_not_open(self):
        broken = os.path.join(self.backup_dir, "broken.db")
        with open(broken, "wb") as handle:
            handle.write(b"SQLite format 3\x00" + b"\x00" * 200)
        with self.assertRaises(sqlite3.DatabaseError):
            nb_backup._verify(broken)

    def test_verification_rejects_a_file_sqlite_calls_malformed(self):
        """integrity_check can open a file and still condemn it."""
        path = nb_backup.backup_trade_history()

        class _Condemning:
            def execute(self, sql, *args):
                self.sql = sql
                return self

            def fetchone(self):
                return ("*** in database main ***\nPage 3 is never used",)

            def close(self):
                pass

        with patch.object(nb_backup.sqlite3, "connect", return_value=_Condemning()):
            with self.assertRaises(ValueError) as caught:
                nb_backup._verify(path)
        self.assertIn("Page 3 is never used", str(caught.exception))

    def test_verification_rejects_an_empty_integrity_verdict(self):
        class _Mute:
            def execute(self, sql, *args):
                return self

            def fetchone(self):
                return None

            def close(self):
                pass

        with patch.object(nb_backup.sqlite3, "connect", return_value=_Mute()):
            with self.assertRaises(ValueError) as caught:
                nb_backup._verify("whatever.db")
        self.assertIn("nothing", str(caught.exception))

    def test_verification_counts_the_rows_it_checked(self):
        self.store.archive_signal(_metrics("AAA"), True, None)
        path = nb_backup.backup_trade_history()
        self.assertEqual(nb_backup._verify(path), 1)


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def _write(self, *names):
        for name in names:
            with open(os.path.join(self.directory, name), "w") as handle:
                handle.write("x")

    def test_keeps_the_newest_and_drops_the_rest(self):
        self._write(
            "trade_history-20261001-033000.db",
            "trade_history-20261002-033000.db",
            "trade_history-20261003-033000.db",
        )
        self.assertEqual(nb_backup._prune(self.directory, keep=2), 1)
        self.assertEqual(
            nb_backup._existing_backups(self.directory),
            ["trade_history-20261002-033000.db", "trade_history-20261003-033000.db"],
        )

    def test_keeps_everything_when_under_the_limit(self):
        self._write("trade_history-20261001-033000.db")
        self.assertEqual(nb_backup._prune(self.directory, keep=14), 0)

    def test_a_keep_of_zero_prunes_nothing_rather_than_everything(self):
        """A misconfigured 0 must not read as "delete every backup"."""
        self._write("trade_history-20261001-033000.db")
        self.assertEqual(nb_backup._prune(self.directory, keep=0), 0)
        self.assertEqual(len(nb_backup._existing_backups(self.directory)), 1)

    def test_never_touches_files_this_job_did_not_write(self):
        self._write(
            "trade_history-20261001-033000.db",
            "trade_history-20261002-033000.db",
            "important.db",
            "trade_history-manual.db",
            "trade_history-20261002-033000.db.tmp",
        )
        nb_backup._prune(self.directory, keep=1)
        survivors = sorted(os.listdir(self.directory))
        self.assertEqual(
            survivors,
            [
                "important.db",
                "trade_history-20261002-033000.db",
                "trade_history-20261002-033000.db.tmp",
                "trade_history-manual.db",
            ],
        )

    def test_a_missing_directory_lists_as_empty(self):
        self.assertEqual(nb_backup._existing_backups(os.path.join(self.directory, "nope")), [])

    def test_an_undeletable_snapshot_warns_without_stopping_the_prune(self):
        self._write(
            "trade_history-20261001-033000.db",
            "trade_history-20261002-033000.db",
            "trade_history-20261003-033000.db",
        )
        real_remove = os.remove

        def fussy(path):
            if path.endswith("20261001-033000.db"):
                raise OSError("read-only file system")
            return real_remove(path)

        with patch.object(nb_backup.os, "remove", side_effect=fussy), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            self.assertEqual(nb_backup._prune(self.directory, keep=1), 1)
        self.assertIn("backup_prune_failed", "\n".join(logs.output))


class BackupJobTests(unittest.TestCase):
    """The nightly job wrapper: it must report honestly and never raise."""

    def test_a_clean_run_logs_the_path_and_the_row_count(self):
        with patch.object(nb_backup, "backup_trade_history", return_value="/app/backups/x.db"):
            self.assertTrue(nb_backup.job_backup_trade_history())

    def test_an_unexpected_error_is_logged_rather_than_raised(self):
        with patch.object(nb_backup, "backup_trade_history", side_effect=OSError("no space")), \
             self.assertLogs("news_bot", level="ERROR") as logs:
            self.assertFalse(nb_backup.job_backup_trade_history())
        self.assertIn("trade_history_backup_failed", "\n".join(logs.output))

    def test_an_unconfigured_backup_dir_is_a_failed_run_not_a_silent_one(self):
        """Silence here would read as "backed up" for months."""
        with patch.object(nb_backup, "backup_trade_history", return_value=None), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            self.assertFalse(nb_backup.job_backup_trade_history())
        self.assertIn("trade_history_backup_unconfigured", "\n".join(logs.output))


class BackupLoggingTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(
            os.path.join(tempfile.mkdtemp(), "h.db")
        )
        self.addCleanup(self.store.close)
        patcher = patch.object(nb_history_store, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.directory = tempfile.mkdtemp()
        for setting, value in (("trade_backup_dir", self.directory), ("trade_backup_keep", 14)):
            item = patch.object(nb_config.SETTINGS, setting, value)
            item.start()
            self.addCleanup(item.stop)

    def test_the_log_line_names_the_rows_it_protected(self):
        self.store.archive_signal(_metrics("AAA"), True, None)
        with self.assertLogs("news_bot", level="INFO") as logs:
            nb_backup.backup_trade_history()
        self.assertIn("trade_history_backup", "\n".join(logs.output))
        self.assertIn("archive_rows=1", "\n".join(logs.output))


class StoreBackupTests(unittest.TestCase):
    def setUp(self):
        self.store = nb_history_store.TradeHistoryStore(
            os.path.join(tempfile.mkdtemp(), "h.db")
        )
        self.addCleanup(self.store.close)

    def test_backup_to_produces_an_openable_copy(self):
        self.store.archive_signal(_metrics("AAA"), True, None)
        target = os.path.join(tempfile.mkdtemp(), "copy.db")
        self.store.backup_to(target)
        conn = sqlite3.connect(target)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM signal_archive").fetchone()[0], 1
        )

    def test_backup_to_raises_rather_than_logging_and_continuing(self):
        """The caller must be able to tell a missed backup from a clean one."""
        with self.assertRaises(sqlite3.Error):
            self.store.backup_to(os.path.join(tempfile.mkdtemp(), "nope", "copy.db"))


if __name__ == "__main__":
    unittest.main()
