"""company_numbers: annual-report numbers with the date they became known."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from newsbot import company_numbers as cn
from newsbot import history_store as nb_history_store


def _fact(concept, period, value, unit="iso4217:USD", **extra):
    return {"value": str(value), "dimensions": {"concept": concept, "entity": "x", "period": period, "unit": unit, **extra}}


YEAR = "2024-01-01T00:00:00/2025-01-01T00:00:00"
DOC = {"facts": {
    "a": _fact("ifrs-full:Revenue", YEAR, 103.77e9),
    "b": _fact("ifrs-full:Revenue", "2023-01-01T00:00:00/2024-01-01T00:00:00", 107e9),   # comparative year
    "c": _fact("ifrs-full:Revenue", YEAR, 50e9, **{"ifrs-full:SegmentsAxis": "x"}),       # a segment, not the total
    "d": _fact("ifrs-full:Revenue", "2024-10-01T00:00:00/2025-01-01T00:00:00", 25e9),    # a quarter
    "e": _fact("ifrs-full:Equity", "2025-01-01T00:00:00", 42.38e9),
    "f": _fact("ifrs-full:ProfitLoss", YEAR, 8.83e9),
}}


class EsefTests(unittest.TestCase):
    def test_only_the_whole_group_for_the_filings_own_year(self):
        values, year_end = cn.esef_values(DOC, "2024-12-31")
        self.assertEqual(year_end, "2024-12-31")
        self.assertEqual(values["revenue"], (103.77e9, "USD"))
        self.assertEqual(values["equity"], (42.38e9, "USD"))
        self.assertEqual(values["net_profit"], (8.83e9, "USD"))
        self.assertNotIn("cash", values)

    def test_shareholders_part_beats_the_total_with_minority_owners(self):
        doc = {"facts": {**DOC["facts"],
                         "g": _fact("ifrs-full:ProfitLossAttributableToOwnersOfParent", YEAR, 7.09e9),
                         "h": _fact("ifrs-full:EquityAttributableToOwnersOfParent", "2025-01-01T00:00:00", 33.7e9)}}
        values, _ = cn.esef_values(doc, "2024-12-31")
        self.assertEqual(values["net_profit"], (7.09e9, "USD"))
        self.assertEqual(values["equity"], (33.7e9, "USD"))


class LoadTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(cn.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        patcher = patch.object(cn, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.downloads = 0
        self.shares = float("nan")

    def _get(self, url):
        if "gleif" in url:
            return {"data": [{"id": "LEI1"}]}
        if url.endswith("/filings?page%5Bsize%5D=50"):
            return {"data": [{"id": "f1", "attributes": {"period_end": "2024-12-31", "json_url": "/r.json",
                                                         "date_added": "2025-05-08 11:12:47"}}]}
        self.downloads += 1
        return DOC

    def _yahoo(self):
        ticker = MagicMock()
        ticker.info = {"financialCurrency": "USD"}
        column = pd.Timestamp("2024-12-31")
        ticker.income_stmt = pd.DataFrame({column: [1.0, 2.0, 3.0]}, index=["Total Revenue", "Operating Income", "Net Income"])
        ticker.balance_sheet = pd.DataFrame({column: [30.1e9, self.shares]}, index=["Total Debt", "Ordinary Shares Number"])
        ticker.cashflow = pd.DataFrame()
        return ticker

    def _rows(self):
        return {(r[2]): (r[3], r[5], r[6]) for r in self.store._connection().execute(
            "SELECT * FROM company_numbers ORDER BY item")}

    def test_esef_first_yahoo_fills_gaps_and_filings_are_read_once(self):
        row = {"ticker": "EQNR.OL", "isin": "NO0010096985", "region": "Oslo"}
        with patch.object(cn, "_get_json", side_effect=self._get), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            self.assertEqual(cn.load_company_numbers([row]), {"saved": 5, "failed": [], "esef_dates_unchecked": 0})
            cn.load_company_numbers([row])
        self.assertEqual(self.downloads, 1)
        rows = self._rows()
        # Added 2025-05-08, after the 2025-04-30 deadline: known from the day it was added (P1-8).
        self.assertEqual(rows["revenue"], (103.77e9, "esef", "2025-05-08"))
        self.assertEqual(rows["operating_profit"], (2.0, "yahoo", "2025-04-30"))
        self.assertEqual(rows["total_debt"], (30.1e9, "yahoo", "2025-04-30"))
        self.assertNotIn("shares_outstanding", rows)

    def test_share_counts_have_no_currency(self):
        self.shares = 2.6e9
        with patch.object(cn, "_get_json", side_effect=cn.NotInEsef("404")), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            cn.load_company_numbers([{"ticker": "IFX.DE", "isin": "DE1", "region": "EU"}])
        row = self.store._connection().execute(
            "SELECT value, currency FROM company_numbers WHERE item = 'shares_outstanding'").fetchone()
        self.assertEqual(row, (2.6e9, None))
        revenue = self.store._connection().execute(
            "SELECT currency FROM company_numbers WHERE item = 'revenue'").fetchone()
        self.assertEqual(revenue, ("USD",))

    def test_version_2_redoes_profit_and_equity_once(self):
        conn = self.store._connection()
        for table in cn._TABLES[:2]:
            conn.execute(table)
        with conn:   # a version-1 database, before #111
            conn.executemany("INSERT INTO company_numbers VALUES (?, '2024-12-31', ?, 1.0, ?, ?, '2025-03-06', 'x')", [
                ("ACA.PA", "net_profit", "EUR", "esef"), ("ACA.PA", "equity", "EUR", "esef"),
                ("ACA.PA", "revenue", "EUR", "esef"), ("IFX.DE", "net_profit", "EUR", "yahoo"),
                ("IFX.DE", "shares_outstanding", "EUR", "yahoo")])
            conn.execute("INSERT INTO esef_filings_read VALUES ('f1', 'ACA.PA')")
        conn = cn._connection()
        left = sorted(conn.execute("SELECT symbol, item, currency FROM company_numbers").fetchall())
        self.assertEqual(left, [("ACA.PA", "revenue", "EUR"), ("IFX.DE", "net_profit", "EUR"),
                                ("IFX.DE", "shares_outstanding", None)])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM esef_filings_read").fetchone(), (0,))
        # Once migrated, new rows are left alone.
        with conn:
            conn.execute("INSERT INTO company_numbers VALUES ('ACA.PA', '2024-12-31', 'equity', 2.0, 'EUR', 'esef', 'd', 'x')")
            conn.execute("INSERT INTO esef_filings_read VALUES ('f1', 'ACA.PA')")
        conn = cn._connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_numbers WHERE item = 'equity'").fetchone(), (1,))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM esef_filings_read").fetchone(), (1,))

    def test_us_stocks_skip_esef_and_failures_are_listed(self):
        with patch.object(cn, "_get_json", side_effect=AssertionError("no ESEF for US")), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            summary = cn.load_company_numbers([{"ticker": "AAPL", "isin": "US0378331005", "region": "US"}])
        self.assertEqual(summary["failed"], [])
        # ESEF down but Yahoo fine: not a failure, Yahoo still saves.
        with patch.object(cn, "_get_json", side_effect=RuntimeError("down")), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            summary = cn.load_company_numbers([{"ticker": "EQNR.OL", "isin": "NO1", "region": "Oslo"}])
        self.assertEqual(summary["failed"], [])
        with patch.object(cn, "_get_json", side_effect=RuntimeError("down")), \
                patch.object(cn.yf, "Ticker", side_effect=RuntimeError("down too")):
            summary = cn.load_company_numbers([{"ticker": "EQNR.OL", "isin": "NO1", "region": "Oslo"}])
        self.assertEqual(summary["failed"], ["EQNR.OL"])

    def test_not_in_esef_falls_back_to_yahoo_quietly(self):
        with patch.object(cn, "_get_json", side_effect=cn.NotInEsef("404")), \
                patch.object(cn.yf, "Ticker", return_value=self._yahoo()):
            summary = cn.load_company_numbers([{"ticker": "IFX.DE", "isin": "DE1", "region": "EU"}])
        self.assertEqual(summary, {"saved": 4, "failed": [], "esef_dates_unchecked": 0})

    def test_rate_limit_waits_and_retries(self):
        responses = [MagicMock(status_code=429), MagicMock(status_code=429), MagicMock(status_code=200)]
        responses[2].json.return_value = {"ok": 1}
        with patch.object(cn.requests, "get", side_effect=responses):
            self.assertEqual(cn._get_json("u"), {"ok": 1})
        self.assertIn(unittest.mock.call(10), self.sleep.call_args_list)
        self.assertIn(unittest.mock.call(30), self.sleep.call_args_list)
        with patch.object(cn.requests, "get", return_value=MagicMock(status_code=404)):
            with self.assertRaises(cn.NotInEsef):
                cn._get_json("u")

    def test_job_never_raises(self):
        with patch.object(cn, "load_company_numbers", side_effect=RuntimeError("x")):
            self.assertEqual(cn.job_load_company_numbers(), {})


# P1-8: an ESEF number is known from the day its filing was added, and the
# 120-day deadline stands in only when that date is missing or unusable.
FY = "2025-01-01T00:00:00/2026-01-01T00:00:00"
FY_END = "2026-01-01T00:00:00"


def _filing(filing_id, added, json_url=None, period_end="2025-12-31"):
    attrs = {"period_end": period_end, "json_url": json_url or f"/{filing_id}.json"}
    if added is not None:
        attrs["date_added"] = added
    return {"id": filing_id, "attributes": attrs}


def _doc(revenue=None, cash=None):
    facts = {}
    if revenue is not None:
        facts["r"] = _fact("ifrs-full:Revenue", FY, revenue, unit="iso4217:EUR")
    if cash is not None:
        facts["c"] = _fact("ifrs-full:CashAndCashEquivalents", FY_END, cash, unit="iso4217:EUR")
    return {"facts": facts}


class FakeEsef:
    """filings.xbrl.org and GLEIF, from fixed answers."""

    def __init__(self, filings, docs):
        self.filings, self.docs, self.downloads = filings, docs, []

    def __call__(self, url):
        if "gleif" in url:
            return {"data": [{"id": "LEI1"}]}
        if "/filings?" in url:
            return {"data": self.filings}
        path = url.removeprefix("https://filings.xbrl.org")
        self.downloads.append(path)
        return self.docs[path]


class KnownFromTests(unittest.TestCase):
    def test_the_added_date_is_used_however_late(self):
        for added, expected in (
                ("2026-07-19 08:00:00.123", "2026-07-19"),   # year end + 200 days
                ("2026-05-19 11:12:47", "2026-05-19"),       # + 139 days: not back-dated to day 120
                ("2026-04-30 00:00:00", "2026-04-30"),       # exactly + 120 days
                ("2026-04-20 10:00:00", "2026-04-20"),       # before the deadline
                ("2026-03-15", "2026-03-15")):
            with self.subTest(added=added):
                self.assertEqual(cn.esef_known_from("2025-12-31", added), expected)

    def test_a_missing_or_unusable_date_falls_back_to_120_days(self):
        for added in (None, "", "not a date", "2026-13-45 00:00:00", "19.05.2026", 12345,
                      "2025-06-30 00:00:00"):   # before the year end: not a real filing date
            with self.subTest(added=added):
                self.assertEqual(cn.esef_known_from("2025-12-31", added), "2026-04-30")


class IngestionOrderTests(unittest.TestCase):
    """Filings are ordered by the same date `esef_known_from` would use."""

    def _order(self, *filings):
        return [f["id"] for f in cn._ingestion_order(list(filings))]

    def test_a_usable_date_is_the_one_known_from_uses(self):
        for added in ("2026-05-19", "2026-05-19 11:12:47", "2026-05-19 11:12:47.5", "2026-05-19T11:12:47+00:00",
                      "2025-12-31 00:00:00"):   # the year end itself
            with self.subTest(added=added):
                self.assertEqual(cn._added_at("2025-12-31", added).date().isoformat(),
                                 cn.esef_known_from("2025-12-31", added))
        for added in (None, "", "not a date", "2026-13-45 00:00:00", "19.05.2026", 12345, "2025-06-30 00:00:00"):
            with self.subTest(added=added):
                self.assertIsNone(cn._added_at("2025-12-31", added))
        self.assertIsNone(cn._added_at(None, "2026-05-19"))   # no year end to check it against

    def test_valid_dates_are_ordered_by_time_not_by_text(self):
        # As text, "2026-05-19 09..." sorts before "2026-05-19T08...".
        self.assertEqual(self._order(_filing("late", "2026-09-01 10:00:00"),
                                     _filing("nine", "2026-05-19 09:00:00"),
                                     _filing("eight", "2026-05-19T08:00:00"),
                                     _filing("march", "2026-03-15")),
                         ["march", "eight", "nine", "late"])

    def test_missing_malformed_and_early_dates_come_after_every_valid_one(self):
        # Each of these sorts before "2026-..." as text, or is not text at all.
        self.assertEqual(self._order(_filing("none", None),
                                     _filing("dots", "19.05.2026"),
                                     _filing("early", "2025-06-30 00:00:00"),
                                     _filing("number", 12345),
                                     _filing("empty", ""),
                                     _filing("b", "2026-09-01 10:00:00"),
                                     _filing("a", "2026-05-19 10:00:00")),
                         ["a", "b", "none", "dots", "early", "number", "empty"])   # the rest as listed

    def test_a_date_is_checked_against_its_own_filings_year_end(self):
        # 2025-06-30 is before the 2025 year end, but a real date for the 2024 report.
        self.assertEqual(self._order(_filing("y2025", "2025-06-30"),
                                     _filing("y2024", "2025-06-30", period_end="2024-12-31"),
                                     _filing("no_year", "2025-01-01", period_end=None)),
                         ["y2024", "y2025", "no_year"])

    def test_equal_dates_keep_the_listed_order(self):
        same = "2026-05-19 10:00:00"
        self.assertEqual(self._order(_filing("c", same), _filing("a", same), _filing("b", same)), ["c", "a", "b"])
        self.assertEqual(self._order(_filing("b", "2026-05-19"), _filing("a", "2026-05-19 00:00:00")), ["b", "a"])


class StoredDateTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(cn.time, "sleep"), patch.object(cn, "YEARS_WANTED", 50)):
            p.start()
            self.addCleanup(p.stop)
        self.store = nb_history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.store.close)
        patcher = patch.object(cn, "TRADE_HISTORY", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _load(self, api, symbol="ACA.PA"):
        with patch.object(cn, "_get_json", side_effect=api):
            return cn.load_esef(symbol, "FR1")

    def _numbers(self):
        return sorted(self.store._connection().execute("SELECT * FROM company_numbers").fetchall())

    def _filings_of(self):
        return {(r[0], r[1], r[2]): (r[3], r[4]) for r in self.store._connection().execute(
            "SELECT * FROM esef_number_filings")}

    def _version_3_db(self, rows, read=("f1",)):
        """A database as version 3 left it: deadline-capped ESEF dates, no filing per row."""
        conn = self.store._connection()
        for table in cn._TABLES[:3]:
            conn.execute(table)
        with conn:
            conn.execute("INSERT INTO company_numbers_version VALUES (3)")
            conn.executemany("INSERT INTO company_numbers VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            conn.executemany("INSERT INTO esef_filings_read VALUES (?, 'ACA.PA')", [(f,) for f in read])

    def test_new_filings_are_dated_by_when_they_were_added(self):
        api = FakeEsef([_filing("f1", "2026-05-19 11:12:47.5")], {"/f1.json": _doc(revenue=10.0)})
        self.assertEqual(self._load(api), 1)
        (row,) = self._numbers()
        self.assertEqual(row[:7], ("ACA.PA", "2025-12-31", "revenue", 10.0, "EUR", "esef", "2026-05-19"))
        self.assertEqual(self._filings_of(), {("ACA.PA", "2025-12-31", "revenue"): ("f1", "2026-05-19 11:12:47.5")})

    def test_a_filing_without_a_date_counts_from_120_days(self):
        api = FakeEsef([_filing("f1", None)], {"/f1.json": _doc(revenue=10.0)})
        self._load(api)
        self.assertEqual(self._numbers()[0][6], "2026-04-30")
        self.assertEqual(self._filings_of()[("ACA.PA", "2025-12-31", "revenue")], ("f1", None))

    def test_two_filings_for_a_year_the_first_added_supplies_each_item(self):
        # Listed newest first; the original lacks cash, the correction adds it.
        api = FakeEsef([_filing("f2", "2026-09-01 10:00:00"), _filing("f1", "2026-05-19 10:00:00")],
                       {"/f1.json": _doc(revenue=10.0), "/f2.json": _doc(revenue=11.0, cash=5.0)})
        self._load(api)
        numbers = {r[2]: (r[3], r[6]) for r in self._numbers()}
        self.assertEqual(numbers, {"revenue": (10.0, "2026-05-19"), "cash": (5.0, "2026-09-01")})
        self.assertEqual({k[2]: v[0] for k, v in self._filings_of().items()}, {"revenue": "f1", "cash": "f2"})

    def test_version_3_moves_to_4_and_leaves_rows_to_be_checked(self):
        rows = [("ACA.PA", "2025-12-31", "revenue", 10.0, "EUR", "esef", "2026-04-30", "t1"),
                ("ACA.PA", "2025-12-31", "operating_profit", 3.0, "EUR", "yahoo", "2026-04-30", "t2")]
        self._version_3_db(rows)
        conn = cn._connection()
        self.assertEqual(conn.execute("SELECT version FROM company_numbers_version").fetchall(), [(4,)])
        self.assertEqual(self._numbers(), sorted(rows))   # nothing rewritten without the filing list
        self.assertEqual(cn.unchecked_esef_rows(), 1)
        self.assertEqual(cn.unchecked_esef_rows(["EQNR.OL"]), 0)

    def test_a_back_dated_row_is_redated_from_its_filing_once(self):
        esef = ("ACA.PA", "2025-12-31", "revenue", 10.0, "EUR", "esef", "2026-04-30", "2026-11-10T22:54:02+00:00")
        yahoo = ("ACA.PA", "2025-12-31", "operating_profit", 3.0, "EUR", "yahoo", "2026-04-30", "t2")
        other = ("ENEL.MI", "2025-12-31", "revenue", 7.0, "EUR", "esef", "2026-04-30", "t3")
        self._version_3_db([esef, yahoo, other])
        api = FakeEsef([_filing("f1", "2026-05-19 11:12:47")], {"/f1.json": _doc(revenue=10.0)})
        self.assertEqual(self._load(api), 0)
        self.assertEqual(self._numbers(), sorted([esef[:6] + ("2026-05-19",) + esef[7:], yahoo, other]))
        self.assertEqual(api.downloads, ["/f1.json"])   # read again to match the saved value
        self.assertEqual(self._filings_of(), {("ACA.PA", "2025-12-31", "revenue"): ("f1", "2026-05-19 11:12:47")})
        self.assertEqual(cn.unchecked_esef_rows(), 1)   # ENEL.MI's turn comes with its own filings
        # A second run changes nothing.
        before = (self._numbers(), self._filings_of())
        self._load(api)
        self.assertEqual((self._numbers(), self._filings_of()), before)
        self.assertEqual(cn._check_saved_dates("ACA.PA", api.filings), 0)
        self.assertEqual(api.downloads, ["/f1.json"])

    def test_several_filings_for_a_year_are_matched_in_ingestion_order(self):
        self._version_3_db([
            ("ACA.PA", "2025-12-31", "revenue", 10.0, "EUR", "esef", "2026-04-30", "t"),
            ("ACA.PA", "2025-12-31", "cash", 5.0, "EUR", "esef", "2026-04-30", "t")], read=("f1", "f2"))
        api = FakeEsef([_filing("f2", "2026-09-01 10:00:00"), _filing("f1", "2026-05-19 10:00:00")],
                       {"/f1.json": _doc(revenue=10.0), "/f2.json": _doc(revenue=11.0, cash=5.0)})
        self._load(api)
        self.assertEqual({r[2]: r[6] for r in self._numbers()}, {"revenue": "2026-05-19", "cash": "2026-09-01"})
        self.assertEqual({k[2]: v[0] for k, v in self._filings_of().items()}, {"revenue": "f1", "cash": "f2"})
        self.assertEqual(sorted(api.downloads), ["/f1.json", "/f2.json"])

    def test_rows_without_a_sure_filing_stay_unchecked_and_unchanged(self):
        rows = [("ACA.PA", "2025-12-31", "revenue", 99.0, "EUR", "esef", "2026-04-30", "t"),   # matches no filing
                ("ACA.PA", "2024-12-31", "revenue", 8.0, "EUR", "esef", "2025-04-30", "t")]    # no filing for that year
        self._version_3_db(rows, read=("f1", "f2"))
        api = FakeEsef([_filing("f1", "2026-05-19 10:00:00"), _filing("f2", "2026-09-01 10:00:00")],
                       {"/f1.json": _doc(revenue=10.0), "/f2.json": _doc(revenue=11.0)})
        with self.assertLogs(cn.SYS_LOG, "WARNING") as logs:
            self._load(api)
        self.assertEqual(self._numbers(), sorted(rows))
        self.assertEqual(self._filings_of(), {})
        self.assertIn("reason=no_matching_filing", logs.output[0])
        self.assertEqual(cn.unchecked_esef_rows(), 2)

    def test_a_read_filing_no_longer_listed_leaves_every_row_unchecked(self):
        rows = [("ACA.PA", "2025-12-31", "revenue", 10.0, "EUR", "esef", "2026-04-30", "t")]
        self._version_3_db(rows, read=("f1", "f0"))   # f0 is gone; it may have supplied the row
        api = FakeEsef([_filing("f1", "2026-05-19 10:00:00")], {"/f1.json": _doc(revenue=10.0)})
        with self.assertLogs(cn.SYS_LOG, "WARNING") as logs:
            self._load(api)
        self.assertIn("reason=read_filings_gone ids=f0", logs.output[0])
        self.assertEqual(self._numbers(), rows)
        with patch.object(cn, "_get_json", side_effect=api), \
                patch.object(cn.yf, "Ticker", side_effect=RuntimeError("no Yahoo")):
            summary = cn.load_company_numbers([{"ticker": "ACA.PA", "isin": "FR1", "region": "EU"}])
        self.assertEqual(summary["esef_dates_unchecked"], 1)

    def test_a_single_filing_must_hold_the_saved_number(self):
        # Cash came from f2, which no longer has a report; f1 never had cash and
        # must not date it from May.
        self._version_3_db([("ACA.PA", "2025-12-31", "cash", 5.0, "EUR", "esef", "2026-04-30", "t")],
                           read=("f1", "f2"))
        api = FakeEsef([_filing("f1", "2026-05-19 10:00:00"), _filing("f2", "2026-09-01 10:00:00")],
                       {"/f1.json": _doc(revenue=10.0)})
        api.filings[1]["attributes"]["json_url"] = None
        with self.assertLogs(cn.SYS_LOG, "WARNING") as logs:
            self._load(api)
        self.assertIn("reason=read_filings_gone ids=f2", logs.output[0])
        self.assertEqual(self._numbers()[0][6], "2026-04-30")
        # With f2 gone from the read list too, f1 is the only candidate, and it lacks cash.
        with self.store._connection() as conn:
            conn.execute("DELETE FROM esef_filings_read WHERE filing_id = 'f2'")
        with self.assertLogs(cn.SYS_LOG, "WARNING") as logs:
            self._load(api)
        self.assertIn("reason=no_matching_filing", logs.output[0])
        self.assertEqual(self._numbers()[0][6], "2026-04-30")
        self.assertEqual(self._filings_of(), {})

    def test_a_different_currency_is_not_a_match(self):
        self._version_3_db([("ACA.PA", "2025-12-31", "revenue", 10.0, "USD", "esef", "2026-04-30", "t")])
        api = FakeEsef([_filing("f1", "2026-05-19 10:00:00")], {"/f1.json": _doc(revenue=10.0)})
        with self.assertLogs(cn.SYS_LOG, "WARNING"):
            self._load(api)
        self.assertEqual(cn.unchecked_esef_rows(), 1)

    def test_an_undated_filing_never_beats_a_dated_one(self):
        # Same year, same number: the dated filing supplies it, not the undated one at day 120.
        api = FakeEsef([_filing("f2", None), _filing("f1", "2026-09-01 10:00:00")],
                       {"/f1.json": _doc(revenue=10.0), "/f2.json": _doc(revenue=10.0, cash=5.0)})
        self._load(api)
        numbers = {r[2]: r[6] for r in self._numbers()}
        self.assertEqual(numbers, {"revenue": "2026-09-01", "cash": "2026-04-30"})
        self.assertEqual({k[2]: v[0] for k, v in self._filings_of().items()}, {"revenue": "f1", "cash": "f2"})

    def test_a_malformed_date_never_beats_a_valid_one(self):
        # "19.05.2026" sorts before "2026-..." as text, and would date revenue at day 120.
        api = FakeEsef([_filing("f2", "19.05.2026"), _filing("f1", "2026-09-01 10:00:00")],
                       {"/f1.json": _doc(revenue=10.0), "/f2.json": _doc(revenue=10.0, cash=5.0)})
        self._load(api)
        self.assertEqual({r[2]: r[6] for r in self._numbers()}, {"revenue": "2026-09-01", "cash": "2026-04-30"})
        self.assertEqual(self._filings_of(), {("ACA.PA", "2025-12-31", "revenue"): ("f1", "2026-09-01 10:00:00"),
                                              ("ACA.PA", "2025-12-31", "cash"): ("f2", "19.05.2026")})

    def test_a_date_before_the_year_end_never_beats_a_valid_one(self):
        api = FakeEsef([_filing("f2", "2025-06-30 00:00:00"), _filing("f1", "2026-09-01 10:00:00")],
                       {"/f1.json": _doc(revenue=10.0), "/f2.json": _doc(revenue=11.0, cash=5.0)})
        self._load(api)
        self.assertEqual({r[2]: (r[3], r[6]) for r in self._numbers()},
                         {"revenue": (10.0, "2026-09-01"), "cash": (5.0, "2026-04-30")})
        self.assertEqual({k[2]: v[0] for k, v in self._filings_of().items()}, {"revenue": "f1", "cash": "f2"})

    def test_two_valid_dates_are_read_oldest_first_whatever_their_format(self):
        api = FakeEsef([_filing("f2", "2026-05-19 09:00:00"), _filing("f1", "2026-05-19T08:00:00")],
                       {"/f1.json": _doc(revenue=10.0), "/f2.json": _doc(revenue=11.0)})
        self._load(api)
        self.assertEqual(api.downloads, ["/f1.json", "/f2.json"])
        self.assertEqual([(r[3], r[6]) for r in self._numbers()], [(10.0, "2026-05-19")])
        self.assertEqual(self._filings_of()[("ACA.PA", "2025-12-31", "revenue")][0], "f1")

    def test_filings_added_at_the_same_time_are_read_as_listed(self):
        same = "2026-05-19 10:00:00"
        api = FakeEsef([_filing("f2", same), _filing("f1", same)],
                       {"/f1.json": _doc(revenue=10.0), "/f2.json": _doc(revenue=11.0)})
        self._load(api)
        self.assertEqual(api.downloads, ["/f2.json", "/f1.json"])
        self.assertEqual(self._numbers()[0][3], 11.0)

    def _saved_rows_follow_the_valid_filing(self, bad):
        # Both filings hold the saved revenue. The valid one supplied it; the
        # other, listed first, must not date it at day 120.
        self._version_3_db([
            ("ACA.PA", "2025-12-31", "revenue", 10.0, "EUR", "esef", "2026-04-30", "t"),
            ("ACA.PA", "2025-12-31", "cash", 5.0, "EUR", "esef", "2026-04-30", "t")], read=("f1", "f2"))
        api = FakeEsef([_filing("f2", bad), _filing("f1", "2026-09-01 10:00:00")],
                       {"/f1.json": _doc(revenue=10.0), "/f2.json": _doc(revenue=10.0, cash=5.0)})
        with patch.object(cn, "_get_json", side_effect=api):
            self.assertEqual(cn._check_saved_dates("ACA.PA", api.filings), 0)
        self.assertEqual({r[2]: r[6] for r in self._numbers()}, {"revenue": "2026-09-01", "cash": "2026-04-30"})
        self.assertEqual(self._filings_of(), {("ACA.PA", "2025-12-31", "revenue"): ("f1", "2026-09-01 10:00:00"),
                                              ("ACA.PA", "2025-12-31", "cash"): ("f2", bad)})

    def test_saved_rows_are_not_matched_to_a_malformed_date_first(self):
        self._saved_rows_follow_the_valid_filing("19.05.2026")

    def test_saved_rows_are_not_matched_to_a_date_before_the_year_end_first(self):
        self._saved_rows_follow_the_valid_filing("2025-06-30 00:00:00")

    def test_saved_rows_are_not_matched_to_an_undated_filing_first(self):
        self._saved_rows_follow_the_valid_filing(None)

    def test_saved_rows_are_matched_oldest_first_whatever_the_format(self):
        self._version_3_db([("ACA.PA", "2025-12-31", "revenue", 10.0, "EUR", "esef", "2026-04-30", "t")],
                           read=("f1", "f2"))
        api = FakeEsef([_filing("f2", "2026-05-19 09:00:00"), _filing("f1", "2026-05-19T08:00:00")],
                       {"/f1.json": _doc(revenue=10.0), "/f2.json": _doc(revenue=11.0)})
        with patch.object(cn, "_get_json", side_effect=api):
            self.assertEqual(cn._check_saved_dates("ACA.PA", api.filings), 0)
        self.assertEqual(self._filings_of()[("ACA.PA", "2025-12-31", "revenue")][0], "f1")
        self.assertEqual(api.downloads, ["/f1.json"])

    def test_a_failed_check_does_not_stop_new_filings(self):
        api = FakeEsef([_filing("f1", "2026-05-19 10:00:00")], {"/f1.json": _doc(revenue=10.0)})
        with patch.object(cn, "_check_saved_dates", side_effect=RuntimeError("down")), \
                self.assertLogs(cn.SYS_LOG, "WARNING"):
            self.assertEqual(self._load(api), 1)

    def test_every_page_of_filings_is_read(self):
        pages = {
            "https://filings.xbrl.org/api/entities/L/filings?page%5Bsize%5D=50":
                {"data": [_filing("f1", None)], "links": {"next": "https://filings.xbrl.org/p2"}},
            "https://filings.xbrl.org/p2": {"data": [_filing("f2", None)], "links": {}},
        }
        with patch.object(cn, "_get_json", side_effect=pages.__getitem__):
            self.assertEqual([f["id"] for f in cn.esef_filings("L")], ["f1", "f2"])
        pages["https://filings.xbrl.org/p2"]["links"] = {"next": "https://elsewhere.example/p3"}
        with patch.object(cn, "_get_json", side_effect=pages.__getitem__), self.assertRaises(RuntimeError):
            cn.esef_filings("L")


if __name__ == "__main__":
    unittest.main()
