"""Market benchmark lookup and matched benchmark returns."""

import math
from typing import Any, Dict, Optional

from newsbot.config import SETTINGS
from newsbot.logsetup import SYS_LOG


def _backtest_benchmark_for(symbol: str) -> Optional[str]:
    """Resolve a market proxy from the configured suffix map.

    DEFAULT is used only for symbols without an exchange suffix. An unknown
    suffix has no benchmark rather than inheriting an unrelated market proxy.
    Users can override the map because Yahoo's index symbols vary by exchange
    and the defaults are only broad-market approximations.
    """
    mappings: Dict[str, str] = {}
    for entry in SETTINGS.trade_backtest_benchmarks.split(","):
        if ":" not in entry:
            continue
        suffix, ticker = entry.split(":", 1)
        suffix, ticker = suffix.strip().upper(), ticker.strip()
        if suffix and ticker:
            mappings[suffix] = ticker
    for suffix in sorted((key for key in mappings if key != "DEFAULT"), key=len, reverse=True):
        if symbol.upper().endswith(suffix):
            return mappings[suffix]
    if "." not in symbol:
        return mappings.get("DEFAULT")
    return None


def _matched_benchmark_return(
    history: Any, entry_date: str, exit_date: str,
    positions: Optional[Dict[str, int]] = None,
) -> Optional[float]:
    if history is None or len(history) == 0:
        return None
    if positions is None:
        positions = {
            date.strftime("%Y-%m-%d"): index
            for index, date in enumerate(history.index)
        }
    if len(positions) != len(history) or any(column not in history for column in ("Open", "Close")):
        SYS_LOG.warning("benchmark_history_unusable reason=duplicate_dates_or_missing_prices")
        return None
    entry, exit_position = positions.get(entry_date), positions.get(exit_date)
    if entry is None or exit_position is None:
        return None
    try:
        entry_price = float(history.iloc[entry]["Open"])
        exit_price = float(history.iloc[exit_position]["Close"])
    except (TypeError, ValueError) as exc:
        SYS_LOG.warning("benchmark_history_unusable reason=nonnumeric_prices detail=%s", exc)
        return None
    if (
        not math.isfinite(entry_price) or not math.isfinite(exit_price)
        or entry_price <= 0 or exit_price <= 0
    ):
        SYS_LOG.warning("benchmark_history_unusable reason=invalid_prices")
        return None
    return (exit_price / entry_price - 1.0) * 100.0
