"""A throwaway trade-history store for tests, in every module that holds one (P2-4).

`history_store.TRADE_HISTORY` is the bot's one database. Some modules look it
up on `history_store` each time; others took their own name for it at import
(`from newsbot.history_store import TRADE_HISTORY`). Swapping only
`history_store.TRADE_HISTORY` leaves those on the real store: a test that
reaches them opens the configured database, and nothing ever closes it.
"""

import os
import tempfile
from typing import Callable, Optional
from unittest.mock import patch

from newsbot import company_numbers, fills, history_store, price_store, snapshots

# Every module with a `TRADE_HISTORY` of its own. test_store_isolation fails
# when a module joins them and is not listed here.
HOLDERS = (history_store, company_numbers, fills, price_store, snapshots)


def use_throwaway_store(add_cleanup: Callable, path: Optional[str] = None) -> history_store.TradeHistoryStore:
    """Put a store on a temporary file in every holder until cleanup, then close it.

    `add_cleanup` is a test's `addCleanup`, a class's `addClassCleanup` or
    `unittest.addModuleCleanup`.
    """
    store = history_store.TradeHistoryStore(path or os.path.join(tempfile.mkdtemp(), "h.db"))
    add_cleanup(store.close)
    for module in HOLDERS:
        patcher = patch.object(module, "TRADE_HISTORY", store)
        patcher.start()
        add_cleanup(patcher.stop)
    return store
