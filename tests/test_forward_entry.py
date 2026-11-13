"""Forward-test entry timing, data-quality classes and the evaluated view.

The rule under test (docs/audit/forward-test-fix-plan.md): a forward-test entry
never uses an opening price that occurred at or before the moment the signal
was recorded. Section numbers in the class names are that plan's.
"""

import hashlib
import json
import os
import random
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

from newsbot import (
    archive_quality as nb_quality,
    config as nb_config,
    exchange_hours as nb_hours,
    forward_test as nb_forward,
    history_store as nb_history_store,
    indicators as nb_indicators,
)

UTC = timezone.utc
POLICY = nb_history_store.ENTRY_POLICY_AFTER_RECORDED
ZONES = {
    "AAA": "America/New_York", "EQNR.OL": "Europe/Oslo", "SAP.DE": "Europe/Berlin",
    "MC.PA": "Europe/Paris", "ASML.AS": "Europe/Amsterdam", "ABI.BR": "Europe/Brussels",
    "ENI.MI": "Europe/Rome", "SAN.MC": "Europe/Madrid",
}


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def bars(start, sessions, tz, skip=()):
    """Weekday bars from `start`, without the dates in `skip` (a holiday has no bar)."""
    days = [day for day in pd.bdate_range(start, periods=sessions + len(skip) + 5)
            if day.strftime("%Y-%m-%d") not in skip][:sessions]
    index = pd.DatetimeIndex(days).tz_localize(tz) if tz else pd.DatetimeIndex(days)
    opens = [100.0 + position for position in range(len(index))]
    return pd.DataFrame({"Open": opens, "Close": [value + 0.5 for value in opens]}, index=index)


def signal_row(symbol, signal, recorded, horizon=2, policy=POLICY):
    return {
        "symbol": symbol, "signal_session": signal, "horizon_sessions": horizon,
        "cost_bps_per_side": 10.0, "entry_policy": policy, "outcome_json": None,
        "recorded_at": recorded if isinstance(recorded, str) else recorded.isoformat(),
    }


def entry_date(row, history):
    position = nb_forward.entry_position(row, history)
    return None if position is None else history.index[position].strftime("%Y-%m-%d")


def _metrics(symbol, session):
    return nb_indicators.TradeMetrics(
        symbol=symbol, session=session, last_close=100.0, day_change_pct=1.0,
        week_momentum_pct=3.0, volume_ratio=1.5, drawdown_pct=2.0, atr_pct=2.0,
        above_ema20=True, ema20=98.0, rsi_14=60.0, support_level=95.0,
        resistance_level=102.0, volume_trend="flat",
    )


def legacy_outcome(entry, exit_, net=1.0):
    return json.dumps({
        "entry_session": entry, "exit_session": exit_, "gross_return_pct": net + 0.2,
        "net_return_pct": net, "benchmark_return_pct": None, "excess_return_pct": None,
    })


def new_outcome(symbol, entry, exit_, net=1.0):
    opened = nb_hours.open_at(nb_hours.exchange_for(symbol), date.fromisoformat(entry))
    return json.dumps({
        "entry_session": entry, "exit_session": exit_, "gross_return_pct": net + 0.2,
        "net_return_pct": net, "benchmark_return_pct": None, "excess_return_pct": None,
        "entry_policy": POLICY, "entry_open_at": opened.isoformat(),
    })


class StoreCase(unittest.TestCase):
    """A temporary archive, with price fetches served from `self.histories`."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "h.db")
        self.store = nb_history_store.TradeHistoryStore(self.path)
        self.addCleanup(self.store.close)
        self.histories = {}
        self.fetched = []
        for patcher in (
            # Rows are archived with a 2-session hold unless a test says otherwise.
            patch.object(nb_config.SETTINGS, "trade_outcome_lookback_days", 2),
            patch.object(nb_history_store, "TRADE_HISTORY", self.store),
            patch.object(nb_forward, "_drop_unfinished_bar", side_effect=lambda history: history),
            patch("newsbot.market_data._fetch_ticker_history", side_effect=self._fetch),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fetch(self, symbol, period="3mo", interval="1d"):
        self.fetched.append(symbol)
        value = self.histories.get(symbol)
        if isinstance(value, Exception):
            raise value
        return value

    def archive(self, symbol, signal, recorded, qualified=True, horizon=2):
        self.store.archive_signal(
            _metrics(symbol, signal), qualified, None if qualified else "trend", recorded_at=recorded)
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE signal_archive SET horizon_sessions = ? WHERE symbol = ? AND signal_session = ?",
                (horizon, symbol, signal))

    def insert(self, symbol, signal, recorded_at, policy=POLICY, outcome=None, qualified=1,
               horizon=2, rank=None, rule=None):
        """A row exactly as given, including values `archive_signal` would refuse."""
        with self.store._connection() as conn:
            conn.execute(
                """INSERT INTO signal_archive (symbol, signal_session, qualified, rank, failed_gate,
                   last_close, day_change_pct, week_momentum_pct, volume_ratio, atr_pct,
                   drawdown_pct, rsi_14, rule_id, horizon_sessions, cost_bps_per_side,
                   benchmark_symbol, recorded_at, outcome_json, entry_policy)
                   VALUES (?, ?, ?, ?, NULL, 100, 1, 3, 1.5, 2, 2, 60, ?, ?, 10, NULL, ?, ?, ?)""",
                (symbol, signal, qualified, rank, rule or self.store.current_rule_id(), horizon,
                 recorded_at, outcome, policy))

    def view(self):
        return {(row["symbol"], row["signal_session"]): row for row in self.store.evaluated_rows()}

    def quality(self, symbol, signal):
        return self.view()[(symbol, signal)]["quality"]

    def settle(self, today=datetime(2027, 6, 1)):
        return nb_forward.settle_pending(today)


# --- 8.1 entry selection -----------------------------------------------------

# symbol, signal session, recorded at (UTC), first bar, bar dates to leave out, expected entry
ENTRY_CASES = [
    ("1 US, recorded before the open", "AAA", "2026-11-09", "2026-11-10T06:00", "2026-11-02", (), "2026-11-10"),
    ("2 US, recorded after the open", "AAA", "2026-11-09", "2026-11-10T18:00", "2026-11-02", (), "2026-11-11"),
    ("3 US, recorded after the close", "AAA", "2026-11-09", "2026-11-10T22:30", "2026-11-02", (), "2026-11-11"),
    ("4 US, Friday after the close", "AAA", "2026-11-12", "2026-11-13T22:30", "2026-11-02", (), "2026-11-16"),
    ("5 US, evening before Thanksgiving", "AAA", "2026-11-24", "2026-11-25T22:30", "2026-11-16",
     ("2026-11-26",), "2026-11-27"),
    ("6 US, recorded on the holiday", "AAA", "2026-11-25", "2026-11-26T15:00", "2026-11-16",
     ("2026-11-26",), "2026-11-27"),
    ("7 US summer time, Europe winter time", "AAA", "2026-10-26", "2026-10-27T13:45", "2026-10-19", (),
     "2026-10-28"),
    ("8 US, both on winter time", "AAA", "2026-11-02", "2026-11-03T13:45", "2026-10-26", (), "2026-11-03"),
    ("9 US summer time started, Europe not yet", "AAA", "2027-03-12", "2027-03-15T13:45", "2027-03-08", (),
     "2027-03-16"),
    ("10 Oslo, recorded before the open", "EQNR.OL", "2026-11-09", "2026-11-10T06:00", "2026-11-02", (),
     "2026-11-10"),
    ("11 Oslo, recorded after the close", "EQNR.OL", "2026-11-09", "2026-11-10T18:00", "2026-11-02", (),
     "2026-11-11"),
    ("12 Oslo, recorded on Ascension Day", "EQNR.OL", "2026-05-13", "2026-05-14T10:00", "2026-05-04",
     ("2026-05-14",), "2026-05-15"),
    ("13 Oslo, first session on summer time after Easter", "EQNR.OL", "2027-03-25", "2027-03-30T06:30",
     "2027-03-22", ("2027-03-26", "2027-03-29"), "2027-03-30"),
    ("14 Xetra, recorded after the close", "SAP.DE", "2026-11-09", "2026-11-10T18:00", "2026-11-02", (),
     "2026-11-11"),
    ("15 Paris, evening before Labour Day", "MC.PA", "2026-04-29", "2026-04-30T18:00", "2026-04-20",
     ("2026-05-01",), "2026-05-04"),
    ("Amsterdam, recorded before the open", "ASML.AS", "2026-11-09", "2026-11-10T07:59", "2026-11-02", (),
     "2026-11-10"),
    ("Brussels, recorded after the open", "ABI.BR", "2026-11-09", "2026-11-10T08:01", "2026-11-02", (),
     "2026-11-11"),
    ("Milan, recorded after the close", "ENI.MI", "2026-11-09", "2026-11-10T18:00", "2026-11-02", (),
     "2026-11-11"),
    ("Madrid, recorded before the open", "SAN.MC", "2026-11-09", "2026-11-10T06:00", "2026-11-02", (),
     "2026-11-10"),
]


class EntrySelectionTests(unittest.TestCase):
    def test_entry_is_the_first_open_strictly_after_the_recording(self):
        for label, symbol, signal, recorded, start, skip, expected in ENTRY_CASES:
            with self.subTest(label):
                history = bars(start, 15, ZONES[symbol], skip)
                row = signal_row(symbol, signal, utc(recorded))
                self.assertEqual(entry_date(row, history), expected)
                opened = nb_hours.open_at(nb_hours.exchange_for(symbol), date.fromisoformat(expected))
                self.assertGreater(opened, utc(recorded))

    def test_16_the_opening_instant_itself_is_too_late(self):
        for symbol, instant in (("AAA", "2026-11-10T14:30:00"), ("EQNR.OL", "2026-11-10T08:00:00")):
            history = bars("2026-11-02", 15, ZONES[symbol])
            at_open = utc(instant)
            self.assertEqual(entry_date(signal_row(symbol, "2026-11-09", at_open), history), "2026-11-11")
            just_before = at_open - timedelta(microseconds=1)
            self.assertEqual(entry_date(signal_row(symbol, "2026-11-09", just_before), history), "2026-11-10")

    def test_no_later_bar_yet_is_pending(self):
        history = bars("2026-11-02", 6, "America/New_York")  # ends Mon 11-09
        self.assertIsNone(entry_date(signal_row("AAA", "2026-11-09", utc("2026-11-10T06:00")), history))

    def test_outcome_records_the_policy_and_the_open_it_used(self):
        history = bars("2026-11-02", 15, "America/New_York")
        recorded = utc("2026-11-10T18:00")
        outcome = nb_forward.forward_outcome(signal_row("AAA", "2026-11-09", recorded), history, None)
        self.assertEqual(outcome["entry_session"], "2026-11-11")
        self.assertEqual(outcome["exit_session"], "2026-11-12")
        self.assertEqual(outcome["entry_policy"], POLICY)
        self.assertEqual(outcome["entry_open_at"], "2026-11-11T09:30:00-05:00")
        self.assertGreater(datetime.fromisoformat(outcome["entry_open_at"]), recorded)
        # Wed 11-11 is the 8th bar: open 107, and the hold ends at Thursday's close, 108.5.
        self.assertAlmostEqual(outcome["gross_return_pct"], (108.5 / 107.0 - 1) * 100)

    def test_a_row_without_a_policy_keeps_the_old_rule(self):
        history = bars("2026-11-02", 15, "America/New_York")
        row = signal_row("AAA", "2026-11-09", utc("2026-11-10T18:00"), policy=None)
        outcome = nb_forward.forward_outcome(row, history, None)
        self.assertEqual(outcome["entry_session"], "2026-11-10")  # the open that had already happened
        self.assertNotIn("entry_policy", outcome)
        self.assertNotIn("entry_open_at", outcome)

    def test_an_unknown_policy_is_refused(self):
        history = bars("2026-11-02", 15, "America/New_York")
        row = signal_row("AAA", "2026-11-09", utc("2026-11-10T06:00"), policy="something-else")
        with self.assertRaisesRegex(ValueError, "unknown entry policy"):
            nb_forward.entry_position(row, history)

    def test_an_unsupported_exchange_has_no_entry(self):
        row = signal_row("RELIANCE.NS", "2026-11-09", utc("2026-11-10T06:00"))
        with self.assertRaisesRegex(ValueError, "unsupported exchange"):
            nb_forward.entry_position(row, bars("2026-11-02", 15, "Asia/Kolkata"))

    def test_exchange_table(self):
        self.assertIsNone(nb_hours.exchange_for("RELIANCE.NS"))
        self.assertIsNone(nb_hours.exchange_for("VOD.L"))
        self.assertEqual(nb_hours.exchange_for("eqnr.ol").zone.key, "Europe/Oslo")
        opened = nb_hours.open_at(nb_hours.exchange_for("AAPL"), date(2026, 7, 1))
        self.assertEqual(opened.isoformat(), "2026-07-01T09:30:00-04:00")


# --- 8.2 timezone validation -------------------------------------------------


class TimezoneTests(unittest.TestCase):
    ROW = signal_row("AAA", "2026-11-09", utc("2026-11-10T06:00"))

    def test_history_without_a_timezone_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "history has no timezone"):
            nb_forward.entry_position(self.ROW, bars("2026-11-02", 15, None))

    def test_history_in_the_wrong_zone_is_rejected_and_names_both(self):
        with self.assertRaisesRegex(ValueError, "Europe/Oslo.*America/New_York"):
            nb_forward.entry_position(self.ROW, bars("2026-11-02", 15, "Europe/Oslo"))

    def test_a_differently_named_zone_with_the_same_offsets_is_accepted(self):
        row = signal_row("EQNR.OL", "2026-11-09", utc("2026-11-10T06:00"))
        self.assertEqual(entry_date(row, bars("2026-11-02", 15, "Europe/Berlin")), "2026-11-10")

    def test_a_zone_that_agrees_only_part_of_the_year_is_caught_when_it_differs(self):
        # London and Oslo are always an hour apart; a US history read as Oslo differs too.
        row = signal_row("EQNR.OL", "2026-11-09", utc("2026-11-10T06:00"))
        with self.assertRaises(ValueError):
            nb_forward.entry_position(row, bars("2026-11-02", 15, "Europe/London"))

    def test_the_exit_bar_is_checked_too(self):
        history = bars("2026-11-02", 15, "America/New_York")
        calls = []
        original = nb_forward._require_exchange_zone

        def spy(stamp, exchange):
            calls.append(stamp.strftime("%Y-%m-%d"))
            return original(stamp, exchange)

        with patch.object(nb_forward, "_require_exchange_zone", side_effect=spy):
            outcome = nb_forward.forward_outcome(self.ROW, history, None)
        self.assertIn("2026-11-09", calls)  # the signal bar
        self.assertIn(outcome["entry_session"], calls)
        self.assertEqual(calls[-1], outcome["exit_session"])

    def test_the_host_timezone_does_not_change_any_entry(self):
        before = os.environ.get("TZ")

        def restore():
            if before is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = before
            time.tzset()

        self.addCleanup(restore)
        for host in ("Pacific/Kiritimati", "Pacific/Pago_Pago", "Europe/Oslo"):
            os.environ["TZ"] = host
            time.tzset()
            for label, symbol, signal, recorded, start, skip, expected in ENTRY_CASES:
                with self.subTest(host=host, case=label):
                    row = signal_row(symbol, signal, utc(recorded))
                    self.assertEqual(entry_date(row, bars(start, 15, ZONES[symbol], skip)), expected)


class TemporaryFaultTests(StoreCase):
    """A fault of one fetch writes neither a result nor a class, and clears by itself."""

    def setUp(self):
        super().setUp()
        self.archive("AAA", "2026-11-09", utc("2026-11-10T06:00"))

    def assert_nothing_written(self):
        self.assertIsNone(self.store.archive_rows()[0]["outcome_json"])
        self.assertEqual(self.store.quality_rows(nb_quality.CLASSIFIER_VERSION), {})
        self.assertEqual(self.quality("AAA", "2026-11-09"), "awaiting_entry")

    def assert_recovers(self):
        self.histories["AAA"] = bars("2026-11-02", 15, "America/New_York")
        self.assertEqual(self.settle(), (1, 0))
        row = self.view()[("AAA", "2026-11-09")]
        self.assertEqual((row["quality"], row["settled"], row["entry_session"]), ("clean", 1, "2026-11-10"))

    def test_naive_history_then_valid_history(self):
        self.histories["AAA"] = bars("2026-11-02", 15, None)
        with self.assertLogs("news_bot", level="WARNING") as logs:
            self.assertEqual(self.settle(), (0, 1))
        self.assertIn("history has no timezone", "\n".join(logs.output))
        self.assert_nothing_written()
        self.assert_recovers()

    def test_wrong_zone_then_valid_history(self):
        self.histories["AAA"] = bars("2026-11-02", 15, "Europe/Oslo")
        with self.assertLogs("news_bot", level="WARNING") as logs:
            self.settle()
        self.assertIn("Europe/Oslo", "\n".join(logs.output))
        self.assertIn("America/New_York", "\n".join(logs.output))
        self.assert_nothing_written()
        self.assert_recovers()

    def test_failed_fetch_then_valid_history(self):
        self.histories["AAA"] = RuntimeError("yahoo is down")
        with self.assertLogs("news_bot", level="WARNING"):
            self.settle()
        self.assert_nothing_written()
        self.assert_recovers()

    def test_a_fault_before_the_hold_could_be_over_is_not_a_failed_run(self):
        self.histories["AAA"] = bars("2026-11-02", 15, None)
        self.assertEqual(self.settle(today=datetime(2026, 11, 10, 12)), (0, 0))
        self.assert_nothing_written()


# --- 8.3 recorded_at ---------------------------------------------------------

BAD_TIMESTAMPS = ["", "   ", "garbage", "2026-11-10", "2026-11-10T06:00:00"]


class RecordedAtTests(StoreCase):
    def test_parse(self):
        for text in BAD_TIMESTAMPS + [None, 20261110]:
            with self.subTest(text), self.assertRaises(ValueError):
                nb_quality.parse_recorded_at(text)
        self.assertEqual(
            nb_quality.parse_recorded_at("2026-11-10T07:00:00+01:00"), utc("2026-11-10T06:00"))

    def test_a_malformed_timestamp_is_invalid_at_once_and_never_fetched(self):
        for position, text in enumerate(BAD_TIMESTAMPS):
            self.insert(f"NEW{position}", "2026-11-09", text)
            self.insert(f"OLD{position}", "2026-11-09", text, policy=None)
        self.histories.update({f"NEW{i}": bars("2026-11-02", 15, "America/New_York") for i in range(5)})
        self.assertEqual(self.settle(today=datetime(2026, 11, 10, 12)), (0, 0))
        for row in self.store.evaluated_rows():
            with self.subTest(row["symbol"]):
                self.assertEqual((row["quality"], row["quality_reason"]), ("invalid", "bad_recorded_at"))
        self.assertEqual(self.fetched, [])  # nothing is fetched to learn this

    def test_an_invalid_new_row_is_not_retried_or_counted_as_a_failure(self):
        self.insert("AAA", "2026-11-09", "garbage")
        self.histories["AAA"] = bars("2026-11-02", 15, "America/New_York")
        for _pass in range(3):
            self.assertEqual(self.settle(), (0, 0))  # long after the hold would have ended
        self.assertEqual(self.fetched, [])
        self.assertIsNone(self.store.archive_rows()[0]["outcome_json"])
        self.assertTrue(nb_forward.job_settle_forward_test())

    def test_another_offset_gives_the_same_entry(self):
        history = bars("2026-11-02", 15, "America/New_York")
        plain = signal_row("AAA", "2026-11-09", "2026-11-10T18:00:00+00:00")
        shifted = signal_row("AAA", "2026-11-09", "2026-11-10T19:00:00+01:00")
        self.assertEqual(entry_date(plain, history), entry_date(shifted, history))

    def test_recorded_on_the_signal_date_is_valid(self):
        # 17:30 in New York on the signal date itself: after the close, same calendar day.
        after_close = signal_row("AAA", "2026-11-09", utc("2026-11-09T22:30"))
        self.assertIsNone(nb_quality.static_defect(after_close))
        history = bars("2026-11-02", 15, "America/New_York")
        self.assertEqual(entry_date(after_close, history), "2026-11-10")
        self.assertEqual(nb_quality.entry_quality(after_close, date(2026, 11, 10))[0], "clean")
        # A later date is valid too.
        self.assertIsNone(nb_quality.static_defect(signal_row("AAA", "2026-11-09", utc("2026-11-10T05:00"))))

    def test_same_day_at_1900_new_york_enters_the_next_session(self):
        # 19:00 EST on Mon 2026-11-09 is 00:00 UTC on the 10th: still the signal date in New York.
        row = signal_row("AAA", "2026-11-09", utc("2026-11-10T00:00"))
        self.assertEqual(utc("2026-11-10T00:00").astimezone(nb_hours.exchange_for("AAA").zone).isoformat(),
                         "2026-11-09T19:00:00-05:00")
        self.assertIsNone(nb_quality.static_defect(row))
        outcome = nb_forward.forward_outcome(row, bars("2026-11-02", 15, "America/New_York"), None)
        self.assertEqual(outcome["entry_session"], "2026-11-10")
        self.assertEqual(outcome["entry_open_at"], "2026-11-10T09:30:00-05:00")
        self.assertGreater(datetime.fromisoformat(outcome["entry_open_at"]), utc("2026-11-10T00:00"))

    def test_the_previous_exchange_local_date_is_invalid(self):
        # 23:00 in New York on Sun 11-08 is already 04:00 UTC on the 9th. The exchange's
        # date decides, not the UTC date: this is the day before the signal session.
        row = signal_row("AAA", "2026-11-09", utc("2026-11-09T04:00"))
        self.assertEqual(utc("2026-11-09T04:00").date().isoformat(), "2026-11-09")
        self.assertEqual(nb_quality.static_defect(row), "recorded_before_signal_session")
        # One hour later it is midnight on the signal date in New York: valid.
        self.assertIsNone(nb_quality.static_defect(signal_row("AAA", "2026-11-09", utc("2026-11-09T05:00"))))
        # The same for Oslo, where the local date runs ahead of UTC instead of behind.
        self.assertEqual(
            nb_quality.static_defect(signal_row("EQNR.OL", "2026-11-09", utc("2026-11-08T22:59"))),
            "recorded_before_signal_session")
        self.assertIsNone(nb_quality.static_defect(signal_row("EQNR.OL", "2026-11-09", utc("2026-11-08T23:00"))))

    def test_same_day_after_the_oslo_close_enters_the_next_oslo_session(self):
        # 17:30 in Oslo on the signal date; Oslo stops trading before that.
        recorded = utc("2026-11-09T16:30")
        row = signal_row("EQNR.OL", "2026-11-09", recorded)
        self.assertIsNone(nb_quality.static_defect(row))
        outcome = nb_forward.forward_outcome(row, bars("2026-11-02", 15, "Europe/Oslo"), None)
        self.assertEqual(outcome["entry_session"], "2026-11-10")
        self.assertEqual(outcome["entry_open_at"], "2026-11-10T09:00:00+01:00")
        self.assertGreater(datetime.fromisoformat(outcome["entry_open_at"]), recorded)

    def test_a_same_day_signal_is_classified_clean_and_settled_through_the_job(self):
        self.archive("AAA", "2026-11-09", utc("2026-11-09T22:30"))       # 17:30 New York, signal date
        self.archive("EQNR.OL", "2026-11-09", utc("2026-11-09T16:30"))   # 17:30 Oslo, signal date
        self.archive("EARLY", "2026-11-09", utc("2026-11-09T04:00"))     # the day before, New York time
        self.histories["AAA"] = bars("2026-11-02", 15, "America/New_York")
        self.histories["EQNR.OL"] = bars("2026-11-02", 15, "Europe/Oslo")
        self.histories["EARLY"] = bars("2026-11-02", 15, "America/New_York")
        self.assertEqual(self.settle(), (2, 0))
        rows = self.view()
        for symbol in ("AAA", "EQNR.OL"):
            row = rows[(symbol, "2026-11-09")]
            self.assertEqual((row["quality"], row["settled"], row["entry_session"]), ("clean", 1, "2026-11-10"))
            self.assertGreater(datetime.fromisoformat(row["entry_open_at"]),
                               datetime.fromisoformat(row["recorded_at"]))
        early = rows[("EARLY", "2026-11-09")]
        self.assertEqual((early["quality"], early["quality_reason"], early["settled"]),
                         ("invalid", "recorded_before_signal_session", 0))
        self.assertNotIn("EARLY", self.fetched)

    def test_other_static_defects(self):
        ok = utc("2026-11-10T06:00")
        self.assertEqual(nb_quality.static_defect(signal_row("X.NS", "2026-11-09", ok)), "unsupported_exchange")
        self.assertEqual(nb_quality.static_defect(signal_row("AAA", "garbage", ok)), "bad_signal_session")
        same_day = {**signal_row("AAA", "2026-11-09", ok, policy=None),
                    "outcome_json": legacy_outcome("2026-11-09", "2026-11-10")}
        self.assertEqual(nb_quality.static_defect(same_day), "bad_outcome")
        unreadable = {**signal_row("AAA", "2026-11-09", ok, policy=None), "outcome_json": "{}"}
        self.assertEqual(nb_quality.static_defect(unreadable), "bad_outcome")

    def test_the_first_failing_check_gives_the_reason(self):
        self.assertEqual(nb_quality.static_defect(signal_row("X.NS", "garbage", "garbage")), "bad_recorded_at")
        self.assertEqual(
            nb_quality.static_defect(signal_row("X.NS", "garbage", utc("2026-11-10T06:00"))),
            "unsupported_exchange")

    def test_17_an_unsupported_exchange_is_invalid_and_never_fetched(self):
        self.archive("RELIANCE.NS", "2026-11-09", utc("2026-11-10T06:00"))
        self.histories["RELIANCE.NS"] = bars("2026-11-02", 15, "Asia/Kolkata")
        self.assertEqual(self.settle(), (0, 0))
        row = self.view()[("RELIANCE.NS", "2026-11-09")]
        self.assertEqual((row["quality"], row["quality_reason"]), ("invalid", "unsupported_exchange"))
        self.assertEqual(self.fetched, [])
        self.assertEqual(row["settled"], 0)

    def test_archive_signal_refuses_a_naive_timestamp(self):
        with self.assertRaises(ValueError):
            self.store.archive_signal(_metrics("AAA", "2026-11-09"), True, None,
                                      recorded_at=datetime(2026, 11, 10, 6))

    def test_archive_signal_defaults_to_now_in_utc(self):
        self.store.archive_signal(_metrics("AAA", "2026-11-09"), True, None)
        row = self.store.archive_rows()[0]
        recorded = nb_quality.parse_recorded_at(row["recorded_at"])
        self.assertLess(abs((datetime.now(UTC) - recorded).total_seconds()), 60)
        self.assertEqual(row["entry_policy"], POLICY)


# --- 8.4 invariant -----------------------------------------------------------


class InvariantTests(unittest.TestCase):
    def test_no_entry_open_is_ever_at_or_before_the_recording(self):
        rng = random.Random(20261113)  # noqa: S311 - a fixed seed for a repeatable test, not security
        histories = {symbol: bars("2026-01-01", 520, zone) for symbol, zone in ZONES.items()}
        checked = 0
        for symbol, history in histories.items():
            exchange = nb_hours.exchange_for(symbol)
            days = [stamp.date() for stamp in history.index]
            for _trial in range(150):
                position = rng.randrange(1, len(days) - 30)
                signal = days[position]
                # Any time from just after the signal's own day to a week later.
                earliest = datetime.combine(signal + timedelta(days=1), datetime.min.time(), exchange.zone)
                recorded = (earliest + timedelta(seconds=rng.randrange(0, 7 * 86400))).astimezone(UTC)
                row = signal_row(symbol, signal.isoformat(), recorded, horizon=3)
                outcome = nb_forward.forward_outcome(row, history, None)
                opened = datetime.fromisoformat(outcome["entry_open_at"])
                self.assertGreater(opened, recorded, (symbol, signal, recorded))
                # And it is the first such session: the one before had already opened.
                entry = days.index(date.fromisoformat(outcome["entry_session"]))
                if days[entry - 1] > signal:
                    self.assertLessEqual(nb_hours.open_at(exchange, days[entry - 1]), recorded)
                self.assertEqual(
                    nb_quality.entry_quality(row, date.fromisoformat(outcome["entry_session"]))[0], "clean")
                checked += 1
        self.assertEqual(checked, 150 * len(ZONES))


# --- 8.5 migration and preservation ------------------------------------------

OLD_SCHEMA = """
    CREATE TABLE signal_archive (
        symbol TEXT NOT NULL, signal_session TEXT NOT NULL, qualified INTEGER NOT NULL,
        rank INTEGER, failed_gate TEXT, last_close REAL NOT NULL, day_change_pct REAL NOT NULL,
        week_momentum_pct REAL NOT NULL, volume_ratio REAL, atr_pct REAL NOT NULL,
        drawdown_pct REAL NOT NULL, rsi_14 REAL, rule_id TEXT NOT NULL,
        horizon_sessions INTEGER NOT NULL, cost_bps_per_side REAL NOT NULL,
        benchmark_symbol TEXT, recorded_at TEXT NOT NULL, outcome_json TEXT,
        PRIMARY KEY (symbol, signal_session)
    )
"""
OLD_COLUMNS = (
    "symbol, signal_session, qualified, rank, failed_gate, last_close, day_change_pct, "
    "week_momentum_pct, volume_ratio, atr_pct, drawdown_pct, rsi_14, rule_id, horizon_sessions, "
    "cost_bps_per_side, benchmark_symbol, recorded_at, outcome_json"
)


def row_hashes(path, where="1 = 1"):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            f"SELECT {OLD_COLUMNS} FROM signal_archive WHERE {where} ORDER BY symbol, signal_session"  # noqa: S608
        ).fetchall()
    finally:
        conn.close()
    return {(row[0], row[1]): hashlib.sha256(repr(row).encode()).hexdigest() for row in rows}


class MigrationTests(unittest.TestCase):
    """A database written by the build before the fix, opened by this one."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "old.db")
        conn = sqlite3.connect(self.path)
        conn.execute(OLD_SCHEMA)
        rule = nb_history_store.TradeHistoryStore.current_rule_id()
        rows = [
            # settled, recorded before its entry open: clean
            ("AAA", "2026-10-05", "2026-10-06T05:00:00+00:00", legacy_outcome("2026-10-06", "2026-10-07", 1.5)),
            # settled, recorded after its entry open: potentially contaminated
            ("EQNR.OL", "2026-10-05", "2026-10-06T17:00:00+00:00", legacy_outcome("2026-10-06", "2026-10-07", 9.0)),
            # settled, no supported exchange: invalid
            ("RELIANCE.NS", "2026-10-05", "2026-10-06T05:00:00+00:00", legacy_outcome("2026-10-06", "2026-10-07", 3.0)),
            # pending, recorded after the open the old rule will use
            ("BBB", "2026-11-09", "2026-11-10T18:00:00+00:00", None),
            # pending, recorded before it
            ("CCC", "2026-11-09", "2026-11-10T06:00:00+00:00", None),
        ]
        for symbol, signal, recorded, outcome in rows:
            conn.execute(
                f"INSERT INTO signal_archive ({OLD_COLUMNS}) VALUES "  # noqa: S608
                "(?, ?, 1, 1, NULL, 100, 1, 3, 1.5, 2, 2, 60, ?, 2, 10, NULL, ?, ?)",
                (symbol, signal, rule, recorded, outcome))
        conn.commit()
        conn.close()
        self.before = row_hashes(self.path)

    def open(self):
        store = nb_history_store.TradeHistoryStore(self.path)
        store._connection()
        self.addCleanup(store.close)
        return store

    def schema(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(signal_archive)")]
            objects = dict(conn.execute(
                "SELECT name, type FROM sqlite_master WHERE name LIKE 'signal_archive%' AND type IN ('table', 'view')"))
            policies = [row[0] for row in conn.execute("SELECT entry_policy FROM signal_archive")]
            pointer = conn.execute("SELECT id, version FROM signal_archive_quality_active").fetchall()
        finally:
            conn.close()
        return columns, objects, policies, pointer

    def test_migration_is_additive_and_can_run_again_and_again(self):
        for _start in range(3):
            self.open().close()
            columns, objects, policies, pointer = self.schema()
            self.assertEqual(columns.count("entry_policy"), 1)
            self.assertEqual(columns[:-1], [name.strip() for name in OLD_COLUMNS.split(",")])
            self.assertEqual(objects, {
                "signal_archive": "table", "signal_archive_quality": "table",
                "signal_archive_quality_active": "table", "signal_archive_evaluated": "view"})
            self.assertEqual(policies, [None] * 5)
            self.assertEqual(pointer, [(1, 1)])
            self.assertEqual(row_hashes(self.path), self.before)

    def test_a_failed_migration_leaves_the_database_as_it_was(self):
        with patch.object(nb_history_store, "_EVALUATED_VIEW", "CREATE VIEW broken AS SELECT nothing FROM"):
            store = nb_history_store.TradeHistoryStore(self.path)
            with self.assertRaises(sqlite3.Error):
                store._connection()
        conn = sqlite3.connect(self.path)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(signal_archive)")]
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]
        finally:
            conn.close()
        self.assertNotIn("entry_policy", columns)
        self.assertNotIn("signal_archive_quality", tables)
        self.assertEqual(row_hashes(self.path), self.before)

    def _settle(self, store, histories):
        with patch.object(nb_history_store, "TRADE_HISTORY", store), \
             patch.object(nb_forward, "_drop_unfinished_bar", side_effect=lambda history: history), \
             patch("newsbot.market_data._fetch_ticker_history",
                   side_effect=lambda symbol, period="3mo", interval="1d": histories.get(symbol)):
            return nb_forward.settle_pending(datetime(2027, 6, 1))

    def test_settled_rows_are_untouched_and_pending_rows_keep_the_old_rule(self):
        store = self.open()
        # Any attempt to change what a signal was, or a result already recorded, fails loudly.
        with store._connection() as conn:
            conn.execute(
                "CREATE TRIGGER no_identity_change BEFORE UPDATE OF signal_session, recorded_at "
                "ON signal_archive BEGIN SELECT RAISE(ABORT, 'archive identity changed'); END")
            conn.execute(
                "CREATE TRIGGER no_outcome_rewrite BEFORE UPDATE OF outcome_json ON signal_archive "
                "WHEN OLD.outcome_json IS NOT NULL BEGIN SELECT RAISE(ABORT, 'outcome rewritten'); END")
        histories = {symbol: bars("2026-11-02", 15, "America/New_York") for symbol in ("BBB", "CCC")}
        self.assertEqual(self._settle(store, histories), (2, 0))
        for _again in range(3):
            self.assertEqual(self._settle(store, histories), (0, 0))

        after = row_hashes(self.path)
        for key in (("AAA", "2026-10-05"), ("EQNR.OL", "2026-10-05"), ("RELIANCE.NS", "2026-10-05")):
            self.assertEqual(after[key], self.before[key])
        rows = {row["symbol"]: row for row in store.evaluated_rows()}
        for symbol in ("BBB", "CCC"):
            outcome = json.loads(rows[symbol]["outcome_json"])
            # The old rule: first session after the signal date, whenever it was recorded.
            self.assertEqual(outcome["entry_session"], "2026-11-10")
            self.assertNotIn("entry_policy", outcome)
            self.assertIsNone(rows[symbol]["entry_policy"])
            self.assertEqual(rows[symbol]["series"], "historical")
        # Only outcome_json changed on the pending rows, and only from NULL.
        unchanged = OLD_COLUMNS.replace(", outcome_json", "")
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            now = conn.execute(f"SELECT {unchanged} FROM signal_archive ORDER BY symbol").fetchall()  # noqa: S608
        finally:
            conn.close()
        self.assertEqual(len(now), 5)
        self.assertEqual({symbol: rows[symbol]["quality"] for symbol in rows}, {
            "AAA": "clean", "EQNR.OL": "potentially_contaminated", "RELIANCE.NS": "invalid",
            "BBB": "potentially_contaminated", "CCC": "clean"})
        # A contaminated row stays contaminated, with the result it always had.
        self.assertEqual(json.loads(rows["EQNR.OL"]["outcome_json"])["net_return_pct"], 9.0)
        self.assertEqual(rows["RELIANCE.NS"]["quality_reason"], "unsupported_exchange")

    def test_settled_rows_are_classified_without_any_fetch(self):
        store = self.open()
        fetched = []

        def fetch(symbol, period="3mo", interval="1d"):
            fetched.append(symbol)
            return None

        with patch.object(nb_history_store, "TRADE_HISTORY", store), \
             patch("newsbot.market_data._fetch_ticker_history", side_effect=fetch):
            nb_forward.settle_pending(datetime(2026, 11, 10))  # before the pending rows could finish
        rows = {row["symbol"]: row["quality"] for row in store.evaluated_rows()}
        self.assertEqual((rows["AAA"], rows["EQNR.OL"], rows["RELIANCE.NS"]),
                         ("clean", "potentially_contaminated", "invalid"))
        self.assertEqual(sorted(set(fetched)), ["BBB", "CCC"])  # only the rows still awaiting entry


class IntegrityScriptTests(MigrationTests):
    """scripts/forward_test_integrity.py: the before/after check run around a deploy."""

    @staticmethod
    def _script():
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "scripts", "forward_test_integrity.py")
        spec = importlib.util.spec_from_file_location("forward_test_integrity", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_migrated_and_settled_archive_compares_intact(self):
        script = self._script()
        before = script.snapshot(self.path)
        self.assertNotIn("classification", before)
        store = self.open()
        histories = {symbol: bars("2026-11-02", 15, "America/New_York") for symbol in ("BBB", "CCC")}
        self._settle(store, histories)
        store.close()
        after = script.snapshot(self.path)
        self.assertEqual(script.compare(before, after), [])
        summary = script._summary(before, after)
        self.assertEqual((summary["settled"], summary["newly_settled"], summary["rows_added"]), ([3, 5], 2, 0))
        self.assertEqual(summary["columns_added"], ["entry_policy"])
        self.assertEqual(after["active_classifier_version"], [1])
        self.assertEqual(after["entries_not_after_recording"], 0)
        self.assertEqual(sum(row["rows"] for row in after["classification"]), 5)

    def test_tampering_is_named(self):
        script = self._script()
        before = script.snapshot(self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE signal_archive SET recorded_at = '2026-10-06T04:00:00+00:00' WHERE symbol = 'AAA'")
            conn.execute("UPDATE signal_archive SET outcome_json = '{}' WHERE symbol = 'EQNR.OL'")
            conn.execute("DELETE FROM signal_archive WHERE symbol = 'CCC'")
            conn.commit()
        problems = "\n".join(script.compare(before, script.snapshot(self.path)))
        self.assertIn("1 archive rows disappeared", problems)
        self.assertIn("1 rows changed what was recorded", problems)
        self.assertIn("1 existing outcomes changed or vanished", problems)
        self.assertIn("table signal_archive lost rows: 5 -> 4", problems)

    def test_command_line(self):
        script = self._script()
        folder = os.path.dirname(self.path)
        first, second = os.path.join(folder, "a.json"), os.path.join(folder, "b.json")
        for target in (first, second):
            with open(target, "w", encoding="utf-8") as handle, patch("sys.stdout", handle):
                self.assertEqual(script.main(["x", "snapshot", self.path]), 0)
        with patch("sys.stdout", open(os.devnull, "w", encoding="utf-8")) as sink:
            self.assertEqual(script.main(["x", "compare", first, second]), 0)
            sink.close()
        with patch("sys.stderr", open(os.devnull, "w", encoding="utf-8")) as sink:
            self.assertEqual(script.main(["x"]), 2)
            sink.close()


# --- 8.6 classification ------------------------------------------------------


class ClassificationTests(StoreCase):
    def test_entry_quality(self):
        clean = signal_row("AAA", "2026-11-09", utc("2026-11-10T06:00"), policy=None)
        self.assertEqual(
            nb_quality.entry_quality(clean, date(2026, 11, 10)),
            ("clean", "entry_after_recording", "2026-11-10T09:30:00-05:00"))
        late = signal_row("AAA", "2026-11-09", utc("2026-11-10T18:00"), policy=None)
        self.assertEqual(nb_quality.entry_quality(late, date(2026, 11, 10))[:2],
                         ("potentially_contaminated", "entry_before_recording"))
        # Under the new policy the same thing means the selection is broken.
        broken = signal_row("AAA", "2026-11-09", utc("2026-11-10T18:00"))
        self.assertEqual(nb_quality.entry_quality(broken, date(2026, 11, 10))[:2],
                         ("invalid", "entry_not_after_recording"))
        with self.assertRaises(ValueError):
            nb_quality.entry_quality(signal_row("X.NS", "2026-11-09", utc("2026-11-10T06:00")), date(2026, 11, 10))

    def test_a_class_is_written_once(self):
        self.insert("AAA", "2026-10-05", "2026-10-06T05:00:00+00:00", policy=None,
                    outcome=legacy_outcome("2026-10-06", "2026-10-07"))
        self.settle()
        first = self.store.quality_rows(1)
        with closing(sqlite3.connect(self.path)) as conn:
            stamp = conn.execute("SELECT classified_at FROM signal_archive_quality").fetchall()
        self.settle()
        self.settle()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT classified_at FROM signal_archive_quality").fetchall(), stamp)
        self.assertEqual(self.store.quality_rows(1), first)
        # Insert-only at the store level too: a second, different verdict is ignored.
        self.assertEqual(self.store.classify([("AAA", "2026-10-05", "invalid", "x", None)], 1), 0)
        self.assertEqual(self.store.quality_rows(1)[("AAA", "2026-10-05")]["quality"], "clean")
        self.assertEqual(self.store.classify([], 1), 0)

    def test_awaiting_entry_then_in_hold_then_finished(self):
        self.archive("AAA", "2026-11-09", utc("2026-11-10T18:00"), horizon=3)
        self.histories["AAA"] = bars("2026-11-02", 7, "America/New_York")  # ends Tue 11-10
        self.settle(today=datetime(2026, 11, 10, 20))
        row = self.view()[("AAA", "2026-11-09")]
        self.assertEqual((row["quality"], row["settled"], row["entry_open_at"]), ("awaiting_entry", 0, None))

        self.histories["AAA"] = bars("2026-11-02", 8, "America/New_York")  # Wed 11-11 exists now
        self.settle(today=datetime(2026, 11, 11, 23))
        row = self.view()[("AAA", "2026-11-09")]
        self.assertEqual((row["quality"], row["settled"]), ("clean", 0))  # in hold
        self.assertEqual(row["entry_open_at"], "2026-11-11T09:30:00-05:00")
        self.assertIsNone(row["net_return_pct"])

        self.histories["AAA"] = bars("2026-11-02", 15, "America/New_York")
        self.assertEqual(self.settle(), (1, 0))
        row = self.view()[("AAA", "2026-11-09")]
        self.assertEqual((row["quality"], row["settled"], row["entry_session"]), ("clean", 1, "2026-11-11"))
        self.assertEqual(json.loads(row["outcome_json"])["entry_open_at"], row["entry_open_at"])

    def test_a_hold_longer_than_the_bars_available_stays_pending(self):
        # Enough calendar days have passed, but a holiday-heavy stretch left too few sessions.
        self.archive("AAA", "2026-11-09", utc("2026-11-10T06:00"), horizon=5)
        self.histories["AAA"] = bars("2026-11-02", 9, "America/New_York")  # ends Thu 11-12
        self.assertEqual(self.settle(today=datetime(2026, 11, 20)), (0, 0))
        row = self.view()[("AAA", "2026-11-09")]
        self.assertEqual((row["quality"], row["settled"]), ("clean", 0))

    def test_a_row_in_hold_is_not_fetched_again_until_it_could_be_finished(self):
        self.archive("AAA", "2026-11-09", utc("2026-11-10T18:00"), horizon=20)
        self.histories["AAA"] = bars("2026-11-02", 8, "America/New_York")
        self.settle(today=datetime(2026, 11, 11, 23))
        self.fetched.clear()
        self.settle(today=datetime(2026, 11, 12, 23))
        self.assertEqual(self.fetched, [])

    def test_the_tripwire_a_new_row_whose_entry_is_not_after_the_recording(self):
        self.archive("AAA", "2026-11-09", utc("2026-11-10T18:00"))
        self.histories["AAA"] = bars("2026-11-02", 15, "America/New_York")
        # A broken selection: it answers Tuesday, whose open had already happened.
        with patch.object(nb_forward, "_entry_after_recording", return_value=6), \
             self.assertLogs("news_bot", level="ERROR") as logs:
            self.assertEqual(self.settle(), (0, 0))
        self.assertIn("forward_entry_not_after_recording", "\n".join(logs.output))
        row = self.view()[("AAA", "2026-11-09")]
        self.assertEqual((row["quality"], row["quality_reason"]), ("invalid", "entry_not_after_recording"))
        self.assertIsNone(row["outcome_json"])
        # And it stays out: the next pass does not settle it either.
        self.assertEqual(self.settle(), (0, 0))
        self.assertIsNone(self.store.archive_rows()[0]["outcome_json"])

    def test_an_older_build_settles_a_new_policy_row(self):
        self.archive("AAA", "2026-11-09", utc("2026-11-10T18:00"), horizon=20)
        self.histories["AAA"] = bars("2026-11-02", 8, "America/New_York")
        self.settle(today=datetime(2026, 11, 11, 23))
        self.assertEqual(self.quality("AAA", "2026-11-09"), "clean")
        stored = self.store.quality_rows(1)
        # What the build before the fix writes: entry by signal date, no policy key.
        legacy = json.loads(legacy_outcome("2026-11-10", "2026-12-08", 50.0))
        self.store.complete_archive_row("AAA", "2026-11-09", legacy)

        row = self.view()[("AAA", "2026-11-09")]
        self.assertEqual((row["quality"], row["quality_reason"]), ("invalid", "settled_under_another_policy"))
        self.assertEqual(self.store.quality_rows(1), stored)  # the stored class is not rewritten
        self.settle()
        self.assertEqual(json.loads(self.store.archive_rows()[0]["outcome_json"]), legacy)  # nor the outcome
        with patch.object(nb_config.SETTINGS, "trade_outcome_lookback_days", 20):
            report = nb_forward.build_forward_test_report(datetime(2027, 6, 1))
        self.assertIn("1 invalid (settled_under_another_policy 1)", report)
        self.assertIn("0 clean and finished", report)

    def test_overdue_is_reported_but_never_becomes_invalid(self):
        now = datetime(2026, 11, 20, 12, tzinfo=UTC)
        self.archive("OLDER", "2026-11-11", now - timedelta(days=8))
        self.archive("NEWER", "2026-11-13", now - timedelta(days=6))
        with self.assertLogs("news_bot", level="WARNING"):
            report = nb_forward.build_forward_test_report(now.replace(tzinfo=None))
        self.assertIn("2 awaiting entry", report)
        self.assertRegex(report, r"<b>Overdue</b>[^\n]*: OLDER\n")
        self.assertNotIn("NEWER", report.split("<b>Overdue</b>")[1].split("\n")[0])
        self.assertEqual({row["quality"] for row in self.store.evaluated_rows()}, {"awaiting_entry"})

    def test_many_overdue_symbols_are_summarised(self):
        rows = [{"quality": "awaiting_entry", "symbol": f"S{i:02d}", "settled": 0,
                 "recorded_at": "2026-01-01T00:00:00+00:00", "quality_reason": None} for i in range(11)]
        rows.append({"quality": "awaiting_entry", "symbol": "BAD", "settled": 0,
                     "recorded_at": "garbage", "quality_reason": None})
        line = nb_forward._counts_line(rows, datetime(2026, 6, 1, tzinfo=UTC))
        self.assertIn("S00, S01, S02, S03, S04, S05, S06, S07 and 3 more", line)
        self.assertNotIn("BAD", line)


class ClassifierVersionTests(StoreCase):
    """8.6a: the view reads the version the pointer names, never 'the latest'."""

    def setUp(self):
        super().setUp()
        self.insert("AAA", "2026-10-05", "2026-10-06T05:00:00+00:00", policy=None,
                    outcome=legacy_outcome("2026-10-06", "2026-10-07"))
        self.insert("BBB", "2026-10-05", "2026-10-06T17:00:00+00:00", policy=None,
                    outcome=legacy_outcome("2026-10-06", "2026-10-07"))
        self.insert("CCC", "2026-10-05", "2026-10-06T05:00:00+00:00", policy=None,
                    outcome=legacy_outcome("2026-10-06", "2026-10-07"))
        self.settle()
        self.version_one = self.classes()
        self.assertEqual(self.version_one, {"AAA": "clean", "BBB": "potentially_contaminated", "CCC": "clean"})

    def classes(self):
        return {row["symbol"]: row["quality"] for row in self.store.evaluated_rows()}

    def versions(self):
        return {row["classifier_version"] for row in self.store.evaluated_rows()}

    def test_a_partial_newer_version_changes_nothing(self):
        self.store.classify([("AAA", "2026-10-05", "invalid", "stricter_rule", None)], 2)
        self.assertEqual(self.classes(), self.version_one)  # nothing turns into awaiting_entry
        self.assertEqual(self.versions(), {1})

    def test_activation_is_refused_until_every_row_is_reached(self):
        self.store.classify([("AAA", "2026-10-05", "invalid", "stricter_rule", None)], 2)
        with self.assertLogs("news_bot", level="WARNING") as logs:
            self.assertFalse(self.store.activate_classifier(2))
        self.assertIn("classifier_activation_refused from=1 to=2 rows_missing=2", "\n".join(logs.output))
        self.assertEqual(self.store.active_classifier_version(), 1)
        self.assertEqual(self.classes(), self.version_one)

    def test_activation_when_complete_then_rollback(self):
        self.store.classify([
            ("AAA", "2026-10-05", "invalid", "stricter_rule", None),
            ("BBB", "2026-10-05", "potentially_contaminated", "entry_before_recording", None),
            ("CCC", "2026-10-05", "clean", "entry_after_recording", None),
        ], 2)
        with self.assertLogs("news_bot", level="INFO") as logs:
            self.assertTrue(self.store.activate_classifier(2))
        self.assertIn("classifier_activated from=1 to=2 rows_changed_class=1", "\n".join(logs.output))
        self.assertEqual(self.classes(), {"AAA": "invalid", "BBB": "potentially_contaminated", "CCC": "clean"})
        self.assertEqual(self.versions(), {2})
        self.assertTrue(self.store.activate_classifier(2))  # already there

        # Rollback is one update, and is never refused.
        self.store.classify([("NEW", "2026-10-05", "clean", "entry_after_recording", None)], 2)
        self.assertTrue(self.store.activate_classifier(1))
        self.assertEqual(self.classes(), self.version_one)
        self.assertEqual(self.versions(), {1})

    def test_settling_never_moves_the_pointer(self):
        # Even when this build's classifier is newer and has reached every row.
        with self.store._connection() as conn:
            conn.execute("UPDATE signal_archive_quality_active SET version = 0")
        self.settle()
        self.assertEqual(self.store.active_classifier_version(), 0)
        self.assertEqual(set(self.classes().values()), {"awaiting_entry"})  # nothing is classed under v0
        with self.assertLogs("news_bot", level="INFO") as logs:
            self.assertTrue(self.store.activate_classifier(1))  # the deliberate step
        self.assertIn("classifier_activated from=0 to=1", "\n".join(logs.output))
        self.assertEqual(self.classes(), self.version_one)

    def test_no_active_version_shows_nothing(self):
        with self.store._connection() as conn:
            conn.execute("DELETE FROM signal_archive_quality_active")
        self.assertIsNone(self.store.active_classifier_version())
        self.assertEqual(self.store.evaluated_rows(), [])
        report = nb_forward.build_forward_test_report(datetime(2027, 6, 1))
        self.assertIn("Classifier not active", report)
        self.assertNotIn("avg net", report)
        self.assertIsNone(self.store.active_classifier_version())  # a settle pass does not refill it
        # Nor does a restart: it was emptied on purpose.
        self.store.close()
        self.assertIsNone(self.store.active_classifier_version())
        # Activating is the deliberate way back.
        self.assertTrue(self.store.activate_classifier(1))
        self.assertEqual(self.classes(), self.version_one)

    def test_a_restart_replaces_the_view_and_keeps_the_pointer(self):
        with self.store._connection() as conn:
            conn.execute("UPDATE signal_archive_quality_active SET version = 2")
            conn.execute("DROP VIEW signal_archive_evaluated")
            conn.execute("CREATE VIEW signal_archive_evaluated AS SELECT 'stale' AS quality")
        self.store.close()
        self.assertEqual(self.store.active_classifier_version(), 2)  # reopened, pointer untouched
        with closing(sqlite3.connect(self.path)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(signal_archive_evaluated)")]
            counts = (conn.execute("SELECT COUNT(*) FROM signal_archive").fetchone()[0],
                      conn.execute("SELECT COUNT(*) FROM signal_archive_quality").fetchone()[0])
        self.assertIn("classifier_version", columns)
        self.assertIn("series", columns)
        self.assertEqual(counts, (3, 3))

    def test_the_bot_prepares_the_archive_when_it_starts(self):
        fresh = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "new.db"))
        self.addCleanup(fresh.close)
        with patch.object(nb_history_store, "TRADE_HISTORY", fresh):
            self.assertTrue(nb_forward.prepare_archive())
        with closing(sqlite3.connect(fresh.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM signal_archive_evaluated").fetchone()[0], 0)
        broken = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "missing", "x.db"))
        with patch.object(nb_history_store, "TRADE_HISTORY", broken), \
             self.assertLogs("news_bot", level="ERROR") as logs:
            self.assertFalse(nb_forward.prepare_archive())
        self.assertIn("forward_archive_prepare_failed", "\n".join(logs.output))


# --- 8.7 pairing, 8.8 report -------------------------------------------------


class PairingTests(StoreCase):
    """One signal date, two entry sessions: two observations, never one."""

    def setUp(self):
        super().setUp()
        early, late = utc("2026-11-10T06:00"), utc("2026-11-10T18:00")
        # Recorded before Tuesday's open: enter Tuesday.
        self.archive("PICK1", "2026-11-09", early, qualified=True)
        self.archive("BASE1", "2026-11-09", early, qualified=False)
        self.archive("BASE2", "2026-11-09", early, qualified=False)
        # Recorded after it: enter Wednesday.
        self.archive("PICK2", "2026-11-09", late, qualified=True)
        self.archive("BASE3", "2026-11-09", late, qualified=False)
        self.archive("BASE4", "2026-11-09", late, qualified=False)
        self.store.archive_ranks([_metrics("PICK1", "2026-11-09"), _metrics("PICK2", "2026-11-09")])
        for symbol in ("BASE1", "BASE2", "BASE3", "BASE4"):
            self.histories[symbol] = self._rising(by=1.0)
        self.histories["PICK1"] = self._rising(by=1.10)
        self.histories["PICK2"] = self._rising(by=1.30)

    @staticmethod
    def _rising(by):
        # Every open is 100, so the only return is entry open -> exit close.
        history = bars("2026-11-02", 15, "America/New_York")
        history["Open"] = 100.0
        history["Close"] = 100.0 * by
        return history

    def test_two_entry_sessions_give_two_paired_observations(self):
        self.assertEqual(self.settle(), (6, 0))
        rows = self.view()
        self.assertEqual(rows[("PICK1", "2026-11-09")]["entry_session"], "2026-11-10")
        self.assertEqual(rows[("PICK2", "2026-11-09")]["entry_session"], "2026-11-11")
        done = nb_forward._finished_clean(list(rows.values()))
        qualified = [row for row in done if row["qualified"]]
        result = nb_forward.paired_vs_baseline(qualified, done)
        self.assertEqual(result["dates"], 2)
        # Each difference uses only the rows of its own entry session (baseline includes the pick).
        tuesday = (10.0 - 0.2) - ((10.0 - 0.2) + 2 * -0.2) / 3
        wednesday = (30.0 - 0.2) - ((30.0 - 0.2) + 2 * -0.2) / 3
        self.assertAlmostEqual(result["mean_delta_pp"], (tuesday + wednesday) / 2)
        top = nb_forward.paired_vs_baseline([row for row in qualified if row["rank"] <= 5], done)
        self.assertEqual(top["dates"], 2)

        report = nb_forward.build_forward_test_report(datetime(2027, 6, 1))
        self.assertIn("6 archived signals · 6 completed over 2 entry sessions (2026-11-10 to 2026-11-11)", report)
        self.assertIn("2 paired entry sessions so far", report)
        self.assertIn("pp over 2 entry sessions", report)

    def test_grouping_by_signal_date_would_have_made_it_one(self):
        self.settle()
        done = nb_forward._finished_clean(list(self.view().values()))
        self.assertEqual(len({row["signal_session"] for row in done}), 1)
        self.assertEqual(len(nb_forward._by_date(done)), 2)

    def test_a_session_with_only_baseline_rows_gives_no_pair(self):
        self.archive("BASE5", "2026-11-10", utc("2026-11-11T18:00"), qualified=False)
        self.histories["BASE5"] = self._rising(by=1.0)
        self.settle()
        done = nb_forward._finished_clean(list(self.view().values()))
        self.assertEqual(len(nb_forward._by_date(done)), 3)
        result = nb_forward.paired_vs_baseline([row for row in done if row["qualified"]], done)
        self.assertEqual(result["dates"], 2)

    def test_rows_recorded_before_the_fix_never_reach_the_test(self):
        self.settle()
        before = nb_forward.build_forward_test_report(datetime(2027, 6, 1))
        clean_section = before.split("<b>Before the entry fix</b>")[0]
        # Same entry sessions, extreme results: contaminated, invalid and clean-but-old rows.
        self.insert("OLDBAD", "2026-11-09", "2026-11-10T18:00:00+00:00", policy=None, qualified=0,
                    outcome=legacy_outcome("2026-11-10", "2026-11-11", 500.0))
        self.insert("OLDOK", "2026-11-09", "2026-11-10T06:00:00+00:00", policy=None, qualified=1, rank=1,
                    outcome=legacy_outcome("2026-11-10", "2026-11-11", 700.0))
        self.insert("X.NS", "2026-11-09", "2026-11-10T06:00:00+00:00", policy=None, qualified=0,
                    outcome=legacy_outcome("2026-11-10", "2026-11-11", 900.0))
        self.archive("WAITING", "2027-05-31", utc("2027-06-01T06:00"))  # no bar for it yet
        self.histories["WAITING"] = bars("2026-11-02", 15, "America/New_York")
        after = nb_forward.build_forward_test_report(datetime(2027, 6, 1))
        after_clean = after.split("<b>Before the entry fix</b>")[0]
        for line in ("Every screened symbol (baseline): 6 signals", "Qualified: 2 signals",
                     "2 paired entry sessions so far"):
            self.assertIn(line, clean_section)
            self.assertIn(line, after_clean)
        self.assertEqual(
            [line for line in clean_section.splitlines() if "avg net" in line or " pp over " in line],
            [line for line in after_clean.splitlines() if "avg net" in line or " pp over " in line])
        self.assertIn("1 awaiting entry", after_clean)

        historical = after.split("<b>Before the entry fix</b>")[1]
        self.assertIn("Hold 2 sessions</b>: 3 signals", historical)
        self.assertIn("1 clean and finished", historical)
        self.assertIn("1 potentially contaminated", historical)
        self.assertIn("1 invalid (unsupported_exchange 1)", historical)
        # Averages there come from the clean class alone: +700, not +500 or +900.
        self.assertIn("Every screened symbol (baseline): 1 signals · avg net +700.00%", historical)
        self.assertNotIn("+500.00%", after)
        self.assertNotIn("+900.00%", after)

    def test_header_names_the_policy_and_the_classifier(self):
        self.settle()
        report = nb_forward.build_forward_test_report(datetime(2027, 6, 1))
        self.assertIn("entry policy after-recorded-v1 · classifier v1", report)
        self.assertIn("Entry is the first open after the signal was recorded", report)


if __name__ == "__main__":
    unittest.main()
