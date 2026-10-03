"""Relative strength versus the market and the sector, and beta to the market.

Why it is here: a stock up 5% in a week reads very differently when its market
rose 5% (it just went along) than when the market was flat (it stood out), and a
volatility stop means little without knowing how much of the stock's movement is
the market's. This is context for reading a move. It is not a signal: ten years
of momentum and relative-strength style factors on the evaluation basket showed
no edge over the universe average, and nothing here changes what qualifies.

Returns are compared over the same *dates*, so a US stock against SPY and an Oslo
stock against OSEBX each line up on their own calendars.
"""

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from newsbot.benchmarks import _backtest_benchmark_for
from newsbot.config import SETTINGS
from newsbot.logsetup import SYS_LOG

# Yahoo's sector names to SPDR sector ETFs. Only meaningful for US listings:
# comparing a Norwegian energy stock with XLE would mostly measure the US.
SECTOR_ETFS: Dict[str, str] = {
    "Technology": "XLK",
    "Energy": "XLE",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}

# A fetch that returns completed sessions as a date-indexed Close series.
CloseFetcher = Callable[[str], Optional[Any]]


@dataclass(frozen=True)
class WindowStrength:
    sessions: int
    stock_pct: float
    market_pct: Optional[float]
    sector_pct: Optional[float]

    @property
    def vs_market(self) -> Optional[float]:
        return None if self.market_pct is None else self.stock_pct - self.market_pct

    @property
    def vs_sector(self) -> Optional[float]:
        return None if self.sector_pct is None else self.stock_pct - self.sector_pct


@dataclass(frozen=True)
class RelativeStrength:
    market_symbol: Optional[str]
    sector_symbol: Optional[str]
    windows: Tuple[WindowStrength, ...]
    beta: Optional[float]


def parse_windows(raw: str) -> List[int]:
    windows: List[int] = []
    for part in (raw or "").split(","):
        try:
            value = int(part.strip())
        except ValueError:
            continue
        if value >= 2 and value not in windows:
            windows.append(value)
    return sorted(windows) or [20, 60]


def sector_etf(symbol: str, sector: Optional[str]) -> Optional[str]:
    """The sector benchmark for a US listing, else None (no like-for-like proxy)."""
    if not sector or "." in symbol or symbol.startswith("^"):
        return None
    return SECTOR_ETFS.get(sector)


def _at_or_before(series: Any, stamp: str) -> Optional[float]:
    """The last close on or before `stamp`, for markets that skipped that day."""
    earlier = series[series.index <= stamp]
    if len(earlier) == 0:
        return None
    value = float(earlier.iloc[-1])
    return value if math.isfinite(value) and value > 0 else None


def _window_return(series: Any, start: str, end: str) -> Optional[float]:
    first, last = _at_or_before(series, start), _at_or_before(series, end)
    if first is None or last is None:
        return None
    return (last / first - 1.0) * 100.0


def compute_beta(stock: Any, market: Any, window: int) -> Optional[float]:
    """Slope of the stock's daily returns on the market's over shared dates."""
    joined = stock.pct_change().dropna().to_frame("s").join(market.pct_change().dropna().rename("m"), how="inner")
    joined = joined.tail(window)
    if len(joined) < SETTINGS.relative_strength_beta_min_obs:
        return None
    variance = joined["m"].var()
    if variance is None or not math.isfinite(variance) or variance <= 0:
        return None
    beta = joined["s"].cov(joined["m"]) / variance
    return float(beta) if math.isfinite(beta) else None


def compute(symbol: str, sector: Optional[str], fetch: CloseFetcher) -> Optional[RelativeStrength]:
    """Relative strength for one symbol, or None when its own history is unusable.

    Missing benchmarks leave those fields None rather than dropping the rest.
    """
    stock = fetch(symbol)
    if stock is None or len(stock) < 3:
        return None
    market_symbol = _backtest_benchmark_for(symbol)
    sector_symbol = sector_etf(symbol, sector)
    market = fetch(market_symbol) if market_symbol else None
    sector_series = fetch(sector_symbol) if sector_symbol else None

    end = stock.index[-1]
    windows: List[WindowStrength] = []
    for sessions in parse_windows(SETTINGS.relative_strength_windows):
        if len(stock) <= sessions:
            continue
        start = stock.index[-1 - sessions]
        own = _window_return(stock, start, end)
        if own is None:
            continue
        windows.append(WindowStrength(
            sessions=sessions,
            stock_pct=own,
            market_pct=_window_return(market, start, end) if market is not None else None,
            sector_pct=_window_return(sector_series, start, end) if sector_series is not None else None,
        ))
    if not windows:
        return None
    beta = compute_beta(stock, market, max(w.sessions for w in windows)) if market is not None else None
    return RelativeStrength(
        market_symbol=market_symbol if market is not None else None,
        sector_symbol=sector_symbol if sector_series is not None else None,
        windows=tuple(windows),
        beta=beta,
    )


def _signed(values: List[Optional[float]]) -> str:
    return " / ".join("n/a" if value is None else f"{value:+.1f}" for value in values)


def render_line(strength: Optional[RelativeStrength]) -> Optional[str]:
    """One line, e.g. `relative strength 20d / 60d: +4.2 / +11.0 pp vs SPY · ...`."""
    if strength is None:
        return None
    labels = " / ".join(f"{w.sessions}d" for w in strength.windows)
    parts: List[str] = []
    if strength.market_symbol and any(w.vs_market is not None for w in strength.windows):
        parts.append(f"{_signed([w.vs_market for w in strength.windows])} pp vs {strength.market_symbol}")
    if strength.sector_symbol and any(w.vs_sector is not None for w in strength.windows):
        parts.append(f"{_signed([w.vs_sector for w in strength.windows])} pp vs {strength.sector_symbol}")
    if not parts:
        return None
    line = f"    relative strength {labels}: " + " · ".join(parts)
    if strength.beta is not None:
        line += f" · beta {strength.beta:.2f}"
    return line


def safe_compute(symbol: str, sector: Optional[str], fetch: CloseFetcher) -> Optional[RelativeStrength]:
    try:
        return compute(symbol, sector, fetch)
    except Exception as exc:
        SYS_LOG.debug("relative_strength_failed symbol=%s detail=%s", symbol, exc)
        return None
