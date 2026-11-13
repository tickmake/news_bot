"""P1-2: the hold defaults to 20 sessions everywhere, and a new rule id carries it.

Rows archived before the change keep the gate-only rule id they were written
with, and the forward-test report and the dashboard keep showing them, split by
the hold they were recorded with, exactly as before.
"""

import importlib.util
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from unittest.mock import patch

import pandas as pd

from newsbot import config as nb_config, forward_test as nb_forward, history_store as nb_history_store
from tests.test_dashboard_views import views
from tests.test_forward_entry import StoreCase, legacy_outcome, new_outcome, utc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLD = "TRADE_OUTCOME_LOOKBACK_DAYS"
# The format every row was archived with before P1-2, at the default gates.
LEGACY_ID = "mom1-day0-atr4.5-dd8-vol1.2"


def settings(env_file=None, **env):
    """The settings a fresh process gets from exactly this environment and env file."""
    environ = {name: value for name, value in os.environ.items() if name.upper() != HOLD}
    environ.update(env)
    with patch.dict(os.environ, environ, clear=True):
        return nb_config.AppSettings(_env_file=env_file)


def compose_value(name, host_env):
    """What `docker compose` passes the bot for `name`: `${VAR:-default}` uses
    the default when VAR is unset or empty in the host environment / .env."""
    with open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8") as handle:
        entries = re.findall(rf"^\s*- {name}=(.*)$", handle.read(), re.M)
    if len(entries) != 1:
        raise AssertionError(f"expected one {name} entry in docker-compose.yml, found {entries}")
    return re.sub(r"\$\{(\w+):-([^}]*)\}",
                  lambda match: host_env.get(match.group(1)) or match.group(2), entries[0])


class DefaultHoldTests(unittest.TestCase):
    def test_the_default_is_20_sessions_without_an_override(self):
        self.assertEqual(settings().trade_outcome_lookback_days, 20)

    def test_an_environment_override_is_kept(self):
        self.assertEqual(settings(**{HOLD: "5"}).trade_outcome_lookback_days, 5)

    def test_an_env_file_value_is_kept(self):
        # The Pi's case: the value lives in its .env, which nothing here rewrites.
        path = os.path.join(tempfile.mkdtemp(), ".env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"{HOLD}=7\n")
        self.assertEqual(settings(env_file=path).trade_outcome_lookback_days, 7)

    def test_compose_defaults_to_20_and_passes_an_override_through(self):
        for host_env, expected in (({}, 20), ({HOLD: ""}, 20), ({HOLD: "5"}, 5), ({HOLD: "20"}, 20)):
            with self.subTest(host_env=host_env):
                passed = compose_value(HOLD, host_env)
                self.assertEqual(settings(**{HOLD: passed}).trade_outcome_lookback_days, expected)

    def test_env_example_sets_20(self):
        example = settings(env_file=os.path.join(ROOT, ".env.example"))
        self.assertEqual(example.trade_outcome_lookback_days, 20)


class RuleIdTests(unittest.TestCase):
    def test_default_settings_give_the_h20_suffix(self):
        with patch.object(nb_history_store, "SETTINGS", settings()):
            self.assertEqual(nb_history_store.TradeHistoryStore.gate_rule_id(), LEGACY_ID)
            self.assertEqual(nb_history_store.TradeHistoryStore.current_rule_id(), f"{LEGACY_ID}-h20")

    def test_the_suffix_comes_from_the_configured_hold(self):
        with patch.object(nb_history_store, "SETTINGS", settings(**{HOLD: "5"})):
            self.assertEqual(nb_history_store.TradeHistoryStore.current_rule_id(), f"{LEGACY_ID}-h5")

    def test_the_rule_id_changes_with_the_hold_and_the_gates_do_not(self):
        ids = {}
        for hold in (5, 20):
            with patch.object(nb_config.SETTINGS, "trade_outcome_lookback_days", hold):
                ids[hold] = (nb_history_store.TradeHistoryStore.gate_rule_id(),
                             nb_history_store.TradeHistoryStore.current_rule_id())
        self.assertEqual(ids[5][0], ids[20][0])
        self.assertNotEqual(ids[5][1], ids[20][1])
        self.assertTrue(ids[20][1].endswith("-h20"))


def _integrity_script():
    path = os.path.join(ROOT, "scripts", "forward_test_integrity.py")
    spec = importlib.util.spec_from_file_location("forward_test_integrity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HistoricalRowsTests(StoreCase):
    """An archive written before P1-2 (gate-only ids), then used by this build."""

    EARLY = "2026-11-10T06:00:00+00:00"  # before New York opens: enters 11-10

    def setUp(self):
        super().setUp()
        hold = patch.object(nb_config.SETTINGS, "trade_outcome_lookback_days", 20)
        hold.start()
        self.addCleanup(hold.stop)
        old = nb_history_store.TradeHistoryStore.gate_rule_id()
        self.assertEqual(old, LEGACY_ID)
        # Clean test, recorded between the entry fix and P1-2: gate-only id, 20-session hold.
        self.insert("OLDPICK", "2026-11-09", self.EARLY, qualified=1, horizon=20, rule=old,
                    outcome=new_outcome("OLDPICK", "2026-11-10", "2026-12-08", 2.0))
        self.insert("OLDSKIP", "2026-11-09", self.EARLY, qualified=0, horizon=20, rule=old,
                    outcome=new_outcome("OLDSKIP", "2026-11-10", "2026-12-08", 1.0))
        # Before the entry fix: one row per hold the Pi has run with.
        self.insert("HIST5", "2026-10-05", "2026-10-06T05:00:00+00:00", policy=None, horizon=5, rule=old,
                    outcome=legacy_outcome("2026-10-06", "2026-10-12", 1.5))
        self.insert("HIST20", "2026-10-05", "2026-10-06T05:00:00+00:00", policy=None, horizon=20, rule=old,
                    outcome=legacy_outcome("2026-10-06", "2026-11-02", 2.5))
        # Different gates: excluded before P1-2 and after it.
        self.insert("OTHERGATES", "2026-11-09", self.EARLY, qualified=1, horizon=20,
                    rule="mom9-day0-atr4.5-dd8-vol1.2",
                    outcome=new_outcome("OTHERGATES", "2026-11-10", "2026-12-08", 9.0))
        self.integrity = _integrity_script()
        self.before = self.integrity.snapshot(self.path)
        self.old = {row["symbol"]: row["rule_id"] for row in self.store.archive_rows()}
        # New rows, archived by this build.
        self.archive("NEWPICK", "2026-11-16", utc("2026-11-17T06:00"), horizon=20)
        self.archive("NEWSKIP", "2026-11-16", utc("2026-11-17T06:00"), qualified=False, horizon=20)
        self.archive("NEWH5", "2026-11-16", utc("2026-11-17T06:00"), horizon=5)

    def rule_ids(self):
        return {row["symbol"]: row["rule_id"] for row in self.store.archive_rows()}

    def test_new_rows_carry_the_hold_in_their_id(self):
        ids = self.rule_ids()
        self.assertEqual(ids["NEWPICK"], f"{LEGACY_ID}-h20")
        self.assertEqual(ids["NEWSKIP"], f"{LEGACY_ID}-h20")
        self.assertEqual(ids["NEWH5"], f"{LEGACY_ID}-h5")

    def test_old_rows_keep_their_ids_through_restart_settle_rearchive_and_report(self):
        self.store.close()
        self.store = nb_history_store.TradeHistoryStore(self.path)  # a restart runs the migration again
        self.addCleanup(self.store.close)
        with patch.object(nb_history_store, "TRADE_HISTORY", self.store):
            self.settle(today=datetime(2026, 11, 20))
            # A rerun that screens an already archived (symbol, session): first observation wins.
            self.archive("OLDPICK", "2026-11-09", utc("2026-11-20T06:00"), horizon=20)
            nb_forward.build_forward_test_report(datetime(2026, 11, 20))
        ids = self.rule_ids()
        for symbol, rule_id in self.old.items():
            self.assertEqual(ids[symbol], rule_id)
        self.assertEqual(self.integrity.compare(self.before, self.integrity.snapshot(self.path)), [])

    def test_the_forward_test_report_still_shows_old_rows(self):
        report = nb_forward.build_forward_test_report(datetime(2026, 11, 20))
        self.assertIn(f"Rule {LEGACY_ID}-h20 · hold 20 sessions", report)
        # Old and new clean-test rows at the current hold are one test; the old ones are finished.
        self.assertIn("4 archived signals · 2 completed over 1 entry sessions", report)
        self.assertIn("Qualified: 1 signals", report)
        # Historical rows are shown per hold, as they were before P1-2.
        self.assertIn("<b>Hold 5 sessions</b>: 1 signals", report)
        self.assertIn("<b>Hold 20 sessions</b>: 1 signals", report)
        # Only the other gates and the other hold are left out.
        self.assertIn("2 recorded under earlier rules or hold lengths (excluded)", report)

    def test_the_dashboard_keeps_old_and_new_rows_in_one_filter_setting(self):
        nb_forward.settle_pending(datetime(2026, 11, 20))
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
            frame = views.prepare(pd.read_sql_query(views.SIGNALS_SQL, conn))
        gates = views.filter_settings(frame["rule_id"])
        self.assertEqual(sorted(gates.unique()), ["mom1-day0-atr4.5-dd8-vol1.2", "mom9-day0-atr4.5-dd8-vol1.2"])
        mine = frame[gates == LEGACY_ID]
        test = mine[mine["series"] == views.CLEAN_TEST]
        self.assertEqual(set(test.loc[test["horizon_sessions"] == 20, "symbol"]),
                         {"OLDPICK", "OLDSKIP", "NEWPICK", "NEWSKIP"})
        self.assertEqual(set(test.loc[test["horizon_sessions"] == 5, "symbol"]), {"NEWH5"})
        historical = mine[mine["series"] == views.HISTORICAL]
        self.assertEqual(dict(zip(historical["symbol"], historical["horizon_sessions"], strict=True)),
                         {"HIST5": 5, "HIST20": 20})
        # The clean test's start date is still the first old row's, not the first new one's.
        self.assertEqual(test["recorded_at"].min(), self.EARLY)
        # Finished old rows still reach the averages.
        clean = views.selection(test[test["horizon_sessions"] == 20], views.CLEAN_TEST)
        self.assertEqual(set(clean["symbol"]), {"OLDPICK", "OLDSKIP"})
        self.assertEqual(views.daily(clean)[["picks", "skipped"]].values.tolist(), [[2.0, 1.0]])


class FilterSettingsTests(unittest.TestCase):
    def test_only_a_trailing_hold_is_dropped(self):
        rule_ids = pd.Series([LEGACY_ID, f"{LEGACY_ID}-h20", f"{LEGACY_ID}-h5",
                              "mom-1-day-0.5-atr4.5-dd8-vol1.2", "mom1-day0-atr4.5-dd8-vol1e+06"])
        self.assertEqual(views.filter_settings(rule_ids).tolist(), [
            LEGACY_ID, LEGACY_ID, LEGACY_ID, "mom-1-day-0.5-atr4.5-dd8-vol1.2", "mom1-day0-atr4.5-dd8-vol1e+06"])

    def test_an_empty_frame_stays_empty(self):
        self.assertTrue(views.filter_settings(pd.Series([], dtype=object)).empty)


if __name__ == "__main__":
    unittest.main()
