"""Nightly copy of what Yahoo says about each stock today (#71).

Yahoo only ever shows today's analyst estimates and company numbers, never
their history. Saving one row per stock per day is the only way to have that
history later, when the scores in Phase 2 need it. Nothing reads it yet.
"""

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import yfinance as yf

from newsbot import market_data, stock_list
from newsbot.config import EU_STOCK_UNIVERSE, NORWAY_STOCK_UNIVERSE, SETTINGS, USA_STOCK_UNIVERSE
from newsbot.history_store import TRADE_HISTORY
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.state import TRADE_HISTORY_LOCK

# A short list on purpose: the full `.info` is ~170 keys and mostly noise.
INFO_FIELDS = (
    "currency", "sector", "industry", "currentPrice", "marketCap", "sharesOutstanding",
    "trailingPE", "forwardPE", "priceToBook", "dividendYield",
    "totalRevenue", "revenueGrowth", "earningsGrowth", "profitMargins", "ebitda",
    "totalDebt", "totalCash", "debtToEquity", "returnOnEquity", "freeCashflow",
    "trailingEps", "forwardEps",
    "targetMeanPrice", "targetHighPrice", "targetLowPrice",
    "numberOfAnalystOpinions", "recommendationMean", "recommendationKey",
)

_TABLE = """
    CREATE TABLE IF NOT EXISTS stock_snapshots (
        symbol TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        data_json TEXT NOT NULL,
        PRIMARY KEY (symbol, snapshot_date)
    )
"""


def _clean(value: Any) -> Any:
    """JSON-safe: NaN and infinity become null, nested tables are cleaned too."""
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):  # numpy scalars
        return _clean(value.item())
    return value


def snapshot_symbols() -> List[str]:
    """The stock list first (#114), then the configured watchlists, then whatever the screener looked at lately."""
    try:
        listed = stock_list.tickers()
    except OSError as exc:
        SYS_LOG.warning("snapshot_stock_list_unreadable detail=%s", exc)
        listed = []
    configured = [*NORWAY_STOCK_UNIVERSE.values(), *EU_STOCK_UNIVERSE.values(), *USA_STOCK_UNIVERSE.values()]
    tracked = TRADE_HISTORY.recently_tracked_symbols(days=14)
    symbols = list(dict.fromkeys(listed + configured + tracked))
    return symbols[:max(0, SETTINGS.snapshot_max_symbols)]


def _snapshot(symbol: str) -> Dict[str, Any]:
    """What Yahoo shows for one stock today. Raises if it shows nothing."""
    info = market_data._fetch_ticker_fundamentals(symbol)
    data: Dict[str, Any] = {"info": {key: info.get(key) for key in INFO_FIELDS}}
    ticker = yf.Ticker(symbol)
    for name in ("earnings_estimate", "revenue_estimate"):
        try:
            table = getattr(ticker, name)
            data[name] = table.to_dict(orient="index") if table is not None else None
        except Exception as exc:
            SYS_LOG.debug("snapshot_%s_failed symbol=%s detail=%s", name, symbol, exc)
            data[name] = None
    if not any(value is not None for value in data["info"].values()) and not any(
        data[name] for name in ("earnings_estimate", "revenue_estimate")
    ):
        raise ValueError("Yahoo returned no data")
    return _clean(data)


def save_snapshots() -> Tuple[int, int]:
    """One row per stock for today. Returns (saved, failed). A rerun overwrites today's row."""
    today = datetime.now().strftime("%Y-%m-%d")
    saved = failed = 0
    for symbol in snapshot_symbols():
        try:
            data = _snapshot(symbol)
            with TRADE_HISTORY_LOCK:
                conn = TRADE_HISTORY._connection()
                conn.execute(_TABLE)
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO stock_snapshots VALUES (?, ?, ?, ?)",
                        (symbol, today, datetime.now(timezone.utc).isoformat(),
                         json.dumps(data, allow_nan=False)),
                    )
        except Exception as exc:
            SYS_LOG.warning("stock_snapshot_failed symbol=%s detail=%s", symbol, exc)
            failed += 1
            continue
        saved += 1
    return saved, failed


def job_save_snapshots() -> bool:
    """Nightly wrapper. Never raises."""
    try:
        saved, failed = save_snapshots()
    except Exception as exc:
        SYS_LOG.error("stock_snapshots_failed detail=%s", exc)
        return False
    if failed:
        SYS_LOG.warning("stock_snapshots saved=%d failed=%d", saved, failed)
    else:
        APP_LOG.info("stock_snapshots saved=%d", saved)
    return not failed
