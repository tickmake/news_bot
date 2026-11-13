"""The dashboard's sums use clean, finished rows of one series and nothing else.

`streamlit/archive_views.py` holds every aggregation the dashboard pages show
(plan section 8.9). The fixture mixes the rows that may be averaged with rows
that may not, and gives the latter results of +50% or more, so any leak moves
a number a long way.
"""

import importlib.util
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime

import pandas as pd

from newsbot import forward_test as nb_forward
from tests.test_forward_entry import POLICY, StoreCase, legacy_outcome, new_outcome, utc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "streamlit")


def _load_views():
    # By path: the folder is named like the Streamlit package and is not importable as one.
    spec = importlib.util.spec_from_file_location(
        "archive_views", os.path.join(DASHBOARD, "archive_views.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


views = _load_views()
NOW = pd.Timestamp("2026-11-20T12:00:00Z")


class DashboardCase(StoreCase):
    def setUp(self):
        super().setUp()
        early = "2026-11-10T06:00:00+00:00"   # before New York opens: enters Tue 11-10
        late = "2026-11-10T18:00:00+00:00"    # after it: enters Wed 11-11
        # --- the clean test: small, known results ---
        self.insert("P1", "2026-11-09", early, qualified=1, outcome=new_outcome("P1", "2026-11-10", "2026-11-11", 2.0))
        self.insert("S1", "2026-11-09", early, qualified=0, outcome=new_outcome("S1", "2026-11-10", "2026-11-11", 1.0))
        self.insert("S2", "2026-11-09", early, qualified=0, outcome=new_outcome("S2", "2026-11-10", "2026-11-11", 0.0))
        self.insert("P2", "2026-11-09", late, qualified=1, outcome=new_outcome("P2", "2026-11-11", "2026-11-12", 4.0))
        self.insert("S3", "2026-11-09", late, qualified=0, outcome=new_outcome("S3", "2026-11-11", "2026-11-12", 1.0))
        # --- rows that must never reach a clean-test number ---
        self.insert("OLDOK", "2026-11-09", early, policy=None, qualified=1,
                    outcome=legacy_outcome("2026-11-10", "2026-11-11", 50.0))       # historical, clean
        self.insert("OLDBAD", "2026-11-09", late, policy=None, qualified=0,
                    outcome=legacy_outcome("2026-11-10", "2026-11-11", 60.0))       # potentially contaminated
        self.insert("X.NS", "2026-11-09", early, policy=None, qualified=1,
                    outcome=legacy_outcome("2026-11-10", "2026-11-11", 70.0))       # invalid: no such exchange
        self.insert("NEWBAD", "2026-11-09", early, qualified=1,
                    outcome=legacy_outcome("2026-11-10", "2026-11-11", 80.0))       # settled by an older build
        self.insert("GARBLED", "2026-11-09", "garbage", qualified=1)                # invalid: bad timestamp
        self.insert("WAIT", "2026-11-18", "2026-11-19T06:00:00+00:00", qualified=1)  # awaiting entry
        self.insert("STUCK", "2026-11-09", "2026-11-10T06:00:00+00:00", qualified=0)  # awaiting, overdue
        self.insert("HOLD", "2026-11-09", early, qualified=1)                       # entered, hold running
        nb_forward.settle_pending(datetime(2026, 11, 10, 12))  # classifies; nothing can be fetched
        self.store.classify([("HOLD", "2026-11-09", "clean", "entry_after_recording",
                              "2026-11-10T09:30:00-05:00")], 1)
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
            self.frame = views.prepare(pd.read_sql_query(views.SIGNALS_SQL, conn))
            self.pointer = pd.read_sql_query(views.POINTER_SQL, conn)
        self.test_rows = self.frame[self.frame["series"] == views.CLEAN_TEST]
        self.clean = views.selection(self.frame, views.CLEAN_TEST)


class SelectionTests(DashboardCase):
    def test_the_fixture_is_classified_as_intended(self):
        classes = dict(zip(self.frame["symbol"], self.frame["quality"], strict=True))
        self.assertEqual(classes, {
            "P1": "clean", "S1": "clean", "S2": "clean", "P2": "clean", "S3": "clean", "HOLD": "clean",
            "OLDOK": "clean", "OLDBAD": "potentially_contaminated", "X.NS": "invalid",
            "NEWBAD": "invalid", "GARBLED": "invalid", "WAIT": "awaiting_entry", "STUCK": "awaiting_entry"})
        self.assertEqual(list(self.pointer["version"]), [1])
        self.assertEqual(set(self.frame["classifier_version"]), {1})

    def test_clean_selection_is_only_finished_clean_rows_of_the_test(self):
        self.assertEqual(sorted(self.clean["symbol"]), ["P1", "P2", "S1", "S2", "S3"])
        self.assertTrue((self.clean["net_return_pct"] < 10).all())

    def test_historical_classes_are_shown_one_at_a_time(self):
        for quality, symbol, result in (("clean", "OLDOK", 50.0), ("potentially_contaminated", "OLDBAD", 60.0),
                                        ("invalid", "X.NS", 70.0)):
            chosen = views.selection(self.frame, views.HISTORICAL, quality)
            self.assertEqual(list(chosen["symbol"]), [symbol])
            days = views.daily(chosen)
            value = days["picks"].iloc[0] if symbol != "OLDBAD" else days["skipped"].iloc[0]
            self.assertEqual(value, result)

    def test_daily_refuses_to_average_mixed_rows(self):
        finished = self.frame[self.frame["settled"] == 1]
        with self.assertRaisesRegex(ValueError, "must not be averaged together"):
            views.daily(finished)  # two series
        with self.assertRaisesRegex(ValueError, "must not be averaged together"):
            views.daily(finished[finished["series"] == views.HISTORICAL])  # three classes
        with self.assertRaisesRegex(ValueError, "only finished rows"):
            views.daily(self.test_rows[self.test_rows["quality"] == "clean"])  # includes the row in hold
        with self.assertRaisesRegex(ValueError, "only finished rows"):
            views.daily(self.test_rows[self.test_rows["quality"] == "awaiting_entry"])
        with self.assertRaises(ValueError):
            views.stock_averages(finished)


class AggregationTests(DashboardCase):
    """Each number a page shows, against the value worked out by hand from the five clean rows."""

    def test_home_scorecard(self):
        days = views.daily(self.clean)
        self.assertEqual(len(days), 2)  # one signal date, two entry sessions
        by_day = days.set_index(days["day"].dt.strftime("%Y-%m-%d"))
        self.assertEqual((by_day.loc["2026-11-10", "picks"], by_day.loc["2026-11-10", "skipped"]), (2.0, 0.5))
        self.assertEqual((by_day.loc["2026-11-11", "picks"], by_day.loc["2026-11-11", "skipped"]), (4.0, 1.0))
        self.assertEqual(list(days["n_picks"]), [1, 1])
        card = views.scorecard(days)
        self.assertEqual(card, {"days": 2, "picks": 3.0, "skipped": 0.75, "better": 2, "compared": 2})

    def test_home_running_difference(self):
        running = views.running_difference(views.daily(self.clean))
        self.assertEqual(list(running["running"]), [1.5, 4.5])
        self.assertEqual(list(running["day"].dt.strftime("%Y-%m-%d")), ["2026-11-10", "2026-11-11"])

    def test_day_by_day_table(self):
        table = views.by_entry_session(self.test_rows, self.clean)
        table.index = table.index.strftime("%Y-%m-%d")
        first, second = table.loc["2026-11-10"], table.loc["2026-11-11"]
        # Averages come from the clean rows only; the counts still show the invalid one.
        self.assertEqual((first["picks_avg"], first["skipped_avg"]), (2.0, 0.5))
        self.assertEqual((second["picks_avg"], second["skipped_avg"]), (4.0, 1.0))
        self.assertEqual((first["checked"], first["clean"], first["invalid"]), (4, 3, 1))  # NEWBAD entered too
        self.assertEqual((second["checked"], second["clean"], second["invalid"]), (2, 2, 0))
        with self.assertRaisesRegex(ValueError, "hold lengths"):
            views.by_entry_session(self.test_rows.assign(horizon_sessions=[2, 5] * 5), self.clean)

    def test_one_stock_averages(self):
        mine = self.frame[self.frame["symbol"] == "P1"]
        averages = views.stock_averages(views.selection(mine, views.CLEAN_TEST))
        self.assertEqual(averages["picked"], 2.0)
        self.assertTrue(pd.isna(averages["skipped"]))
        # A stock with only excluded rows has no average at all.
        bad = views.stock_averages(views.selection(self.frame[self.frame["symbol"] == "NEWBAD"], views.CLEAN_TEST))
        self.assertTrue(pd.isna(bad["picked"]))

    def test_what_if(self):
        df = self.clean.copy()
        # Filters every row passes: "your filters" picks everything, so there is nothing skipped.
        df["what_if"] = views.what_if_picks(df, trend=True, m5=1.0, m1=0.0, atr=4.5, dd=8.0, vol=0.0)
        self.assertEqual(int(df["what_if"].sum()), 5)
        bot = views.scorecard(views.daily(df, "qualified"))
        self.assertEqual((bot["picks"], bot["skipped"]), (3.0, 0.75))
        yours = views.daily(df, "what_if")
        self.assertEqual(list(yours["picks"]), [1.0, 2.5])
        self.assertTrue(yours["skipped"].isna().all())
        # Filters no row passes.
        df["what_if"] = views.what_if_picks(df, trend=True, m5=5.0, m1=0.0, atr=4.5, dd=8.0, vol=0.0)
        self.assertEqual(int(df["what_if"].sum()), 0)
        # The trend switch and the volume switch.
        below = df.assign(failed_gate="trend")
        self.assertEqual(int(views.what_if_picks(below, True, 1.0, 0.0, 4.5, 8.0, 0.0).sum()), 0)
        self.assertEqual(int(views.what_if_picks(below, False, 1.0, 0.0, 4.5, 8.0, 0.0).sum()), 5)
        self.assertEqual(int(views.what_if_picks(df, True, 1.0, 0.0, 4.5, 8.0, 2.0).sum()), 0)

    def test_state_counts(self):
        self.assertEqual(views.state_counts(self.test_rows, NOW), {
            "clean_finished": 5, "clean_in_hold": 1, "potentially_contaminated": 0,
            "invalid": 2, "awaiting_entry": 2, "overdue": 1})
        historical = self.frame[self.frame["series"] == views.HISTORICAL]
        self.assertEqual(views.state_counts(historical, NOW), {
            "clean_finished": 1, "clean_in_hold": 0, "potentially_contaminated": 1,
            "invalid": 1, "awaiting_entry": 0, "overdue": 0})

    def test_overdue_and_invalid_reasons(self):
        self.assertEqual(list(views.overdue(self.frame, NOW)["symbol"]), ["STUCK"])
        self.assertEqual(len(views.overdue(self.frame, pd.Timestamp("2026-11-12T00:00:00Z"))), 0)
        late = views.overdue(self.frame)  # "now" defaults to the clock
        self.assertTrue(set(late["symbol"]) <= {"STUCK", "WAIT"})
        reasons = dict(zip(*[views.invalid_reasons(self.frame)[c] for c in ("quality_reason", "rows")], strict=True))
        self.assertEqual(reasons, {"bad_recorded_at": 1, "settled_under_another_policy": 1,
                                   "unsupported_exchange": 1})

    def test_data_collected_puts_every_row_in_exactly_one_class(self):
        table = views.collected(self.frame, NOW)
        self.assertEqual(list(table["day"]), ["2026-11-18", "2026-11-09"])
        placed = table[["clean", "potentially_contaminated", "invalid", "awaiting_entry"]].sum(axis=1)
        self.assertEqual(list(placed), list(table["checked"]))
        main = table[table["day"] == "2026-11-09"].iloc[0]
        self.assertEqual(
            (main["checked"], main["clean"], main["potentially_contaminated"], main["invalid"],
             main["awaiting_entry"], main["overdue"], main["with_result"]),
            (12, 7, 1, 3, 1, 1, 9))

    def test_a_leak_would_show(self):
        # The same sums over every finished row are far from the clean ones: the fixture can catch a leak.
        leaked = self.frame[self.frame["settled"] == 1]
        self.assertGreater(leaked["net_return_pct"].mean(), 20)
        self.assertLess(self.clean["net_return_pct"].mean(), 2)

    def test_empty_selection(self):
        nothing = views.selection(self.frame.iloc[0:0], views.CLEAN_TEST)
        days = views.daily(nothing)
        self.assertTrue(days.empty)
        self.assertEqual(views.scorecard(days)["days"], 0)
        running = views.running_difference(days)
        self.assertTrue(running.empty)
        self.assertIn("running", running.columns)


class TelegramAgreementTests(DashboardCase):
    def test_both_surfaces_count_the_same_rows_and_entry_sessions(self):
        report = nb_forward.build_forward_test_report(datetime(2026, 11, 20, 12))
        clean_part = report.split("<b>Before the entry fix</b>")[0]
        sessions = sorted(self.clean["entry_session"].unique())
        self.assertIn(
            f"{len(self.clean)} completed over {len(sessions)} entry sessions ({sessions[0]} to {sessions[-1]})",
            clean_part)
        counts = views.state_counts(self.test_rows, NOW)
        self.assertIn(f"{counts['clean_finished']} clean and finished · {counts['clean_in_hold']} clean and in hold",
                      clean_part)
        self.assertIn(f"{counts['awaiting_entry']} awaiting entry", clean_part)
        self.assertIn(f"{counts['invalid']} invalid (bad_recorded_at 1, settled_under_another_policy 1)", clean_part)
        self.assertIn("<b>Overdue</b>", clean_part)
        self.assertIn(": STUCK", clean_part)
        self.assertIn("Every screened symbol (baseline): 5 signals · avg net +1.60%", clean_part)
        self.assertIn("classifier v1", report)
        self.assertEqual(POLICY, "after-recorded-v1")
        self.assertIn(POLICY, report)


class DashboardSourceTests(DashboardCase):
    """The pages cannot read the archive around the view."""

    def test_the_archive_is_read_in_one_place_through_the_view(self):
        sources = {}
        for name in ("app.py", "archive_views.py"):
            with open(os.path.join(DASHBOARD, name), encoding="utf-8") as handle:
                sources[name] = handle.read()
        both = "\n".join(sources.values())
        # The bare table is never named: only the view and the classifier pointer.
        self.assertEqual(re.findall(r"signal_archive(?!_evaluated|_quality_active)", both), [])
        self.assertEqual(len(re.findall(r"FROM\s+signal_archive_evaluated", both)), 1)
        self.assertNotIn("FROM signal_archive", sources["app.py"])
        self.assertEqual(sources["app.py"].count("views.SIGNALS_SQL"), 1)
        # No page keeps its own copy of the sums.
        self.assertNotRegex(sources["app.py"], r"\ndef (daily|scorecard)\(")

    def test_the_dashboard_image_ships_the_module(self):
        with open(os.path.join(DASHBOARD, "Dockerfile"), encoding="utf-8") as handle:
            self.assertIn("archive_views.py", handle.read())

    def test_no_active_classifier_means_an_empty_view(self):
        with self.store._connection() as conn:
            conn.execute("DELETE FROM signal_archive_quality_active")
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
            self.assertTrue(pd.read_sql_query(views.SIGNALS_SQL, conn).empty)
            self.assertTrue(pd.read_sql_query(views.POINTER_SQL, conn).empty)

    def test_the_view_carries_what_the_pages_need(self):
        # utc() keeps the fixture honest: the "early" stamp really is before New York's open.
        self.assertLess(utc("2026-11-10T06:00"), datetime.fromisoformat("2026-11-10T09:30:00-05:00"))
        for column in ("series", "quality", "quality_reason", "settled", "entry_session",
                       "classifier_version", "entry_policy", "recorded_at", "rule_id"):
            self.assertIn(column, self.frame.columns)
