"""P2-4: tests that render /analyze must not open the bot's real database.

The suite used to end with "ResourceWarning: unclosed database". `/analyze`
reads company numbers and prices through `company_numbers.TRADE_HISTORY` and
`price_store.TRADE_HISTORY`, names those modules took at import. Fixtures
swapped only `history_store.TRADE_HISTORY`, so the real store opened the
configured database and nothing closed it. These tests watch the store's own
`_conn`, not the garbage collector.
"""

import importlib
import os
import pkgutil
import tempfile
import unittest
from unittest.mock import patch

import newsbot
from newsbot import company_numbers, fills, history_store, price_store, snapshots
from newsbot import reports as nb_reports
from tests.store_isolation import HOLDERS, use_throwaway_store


def _analyze():
    with patch("newsbot.indicators._compute_trade_metrics", return_value=None), \
            patch("newsbot.market_data._fetch_ticker_fundamentals", return_value={}):
        return nb_reports.build_ticker_analysis("ZZZZ")


class ThrowawayStoreTests(unittest.TestCase):
    def test_every_module_holding_the_store_is_listed(self):
        holders = set()
        for info in pkgutil.iter_modules(newsbot.__path__, "newsbot."):
            module = importlib.import_module(info.name)
            if isinstance(vars(module).get("TRADE_HISTORY"), history_store.TradeHistoryStore):
                holders.add(module)
        self.assertEqual(holders, set(HOLDERS))

    def test_swapping_history_store_alone_leaves_analyze_on_the_other_store(self):
        # The old fixture. Stand-ins take both roles, so the real store is never opened here.
        folder = tempfile.mkdtemp()
        real = history_store.TradeHistoryStore(os.path.join(folder, "real.db"))
        swapped = history_store.TradeHistoryStore(os.path.join(folder, "swapped.db"))
        self.addCleanup(real.close)
        self.addCleanup(swapped.close)
        with patch.object(company_numbers, "TRADE_HISTORY", real), \
                patch.object(price_store, "TRADE_HISTORY", real), \
                patch.object(history_store, "TRADE_HISTORY", swapped):
            _analyze()
        self.assertIsNotNone(real._conn)   # opened behind the fixture's back: the leak

    def test_analyze_reads_only_the_throwaway_store(self):
        real = history_store.TradeHistoryStore(os.path.join(tempfile.mkdtemp(), "real.db"))
        self.addCleanup(real.close)
        for module in HOLDERS:
            patcher = patch.object(module, "TRADE_HISTORY", real)
            patcher.start()
            self.addCleanup(patcher.stop)
        cleanups = []
        store = use_throwaway_store(cleanups.append)
        try:
            self.assertIn("ZZZZ", _analyze())
            self.assertIsNotNone(store._conn)   # the report card did read a database
            self.assertIsNone(real._conn)       # and not the one behind the fixture
            for module in (company_numbers, fills, price_store):
                with history_store.TRADE_HISTORY_LOCK:
                    self.assertIs(module._connection(), store._conn, module.__name__)
            self.assertIs(snapshots.TRADE_HISTORY, store)
        finally:
            for cleanup in reversed(cleanups):
                cleanup()
        self.assertIsNone(store._conn)   # closed, not left to the garbage collector
        self.assertIsNone(real._conn)
        for module in HOLDERS:
            self.assertIs(module.TRADE_HISTORY, real, module.__name__)

    def test_the_store_keeps_what_was_committed_until_it_is_closed(self):
        cleanups = []
        store = use_throwaway_store(cleanups.append)
        with history_store.TRADE_HISTORY_LOCK:
            conn = company_numbers._connection()
            with conn:
                conn.execute("INSERT INTO company_numbers VALUES ('A', '2025-12-31', 'revenue', 1.0, 'EUR', "
                             "'esef', '2026-03-01', 't')")
            with self.assertRaises(RuntimeError), conn:
                conn.execute("DELETE FROM company_numbers")
                raise RuntimeError("stop")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_numbers").fetchone(), (1,))
        for cleanup in reversed(cleanups):
            cleanup()
        self.assertIsNone(store._conn)
        store.close()   # closing twice is harmless


if __name__ == "__main__":
    unittest.main()
