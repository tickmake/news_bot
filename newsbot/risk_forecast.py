"""How much a stock can fall over a normal hold (#123).

Which way a price moves next can't be predicted (docs/research, sections 1-6),
but how much it swings can, fairly well: calm stocks stay calm and wild ones
stay wild. So this says what a bad two or four weeks has looked like for this
stock, its biggest fall, and how hard it moves on report days. Two to four
weeks is the hold the owner uses (#122 found shorter holds lose to fees).

Every number comes from saved prices (`daily_prices`, #75), or from Yahoo for
a stock that isn't in the stock list. Every part degrades to "not available"
rather than raising.
"""

import time
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from typing import Any, Dict, List, Optional, Tuple

import yfinance as yf

from newsbot import market_data, price_store
from newsbot.config import SETTINGS
from newsbot.indicators import _drop_in_progress_bar
from newsbot.logsetup import SYS_LOG
from newsbot.util import _with_retry

HOLDS = ((10, "2 weeks"), (20, "4 weeks"))
LOOKBACK_SESSIONS = 252     # one year for the "bad spell" percentile
DROP_SESSIONS = 504         # two years for the biggest fall
TWO_YEARS_SESSIONS = 480    # enough to call it "2 years" (holidays, Yahoo's 2y)
MIN_SESSIONS = 120
BAD_QUANTILE = 0.05         # "1 in 20"
EARNINGS_REPORTS = 8
_EARNINGS_TTL_SECONDS = 24 * 3600
_EARNINGS_CACHE: Dict[str, Tuple[float, List[date]]] = {}


@dataclass(frozen=True)
class Forecast:
    sessions: int
    bad_moves: Tuple[Tuple[str, float], ...]   # (label, 5th-percentile return %)
    max_drop_pct: float
    drop_from: date
    drop_to: date
    earnings_move_pct: Optional[float] = None
    earnings_reports: int = 0
    normal_two_day_pct: Optional[float] = None


def _closes(symbol: str) -> Optional[Any]:
    """Adjusted daily closes by date: saved prices first, Yahoo otherwise."""
    saved = price_store.closes(symbol)
    if saved is not None and len(saved) >= MIN_SESSIONS:
        return saved
    history = _drop_in_progress_bar(market_data._fetch_ticker_history(symbol, period="2y"))
    if history is None or "Close" not in history:
        return None
    close = history["Close"].dropna()
    close.index = [stamp.date() for stamp in close.index]
    close = close[~close.index.duplicated()]
    return close if len(close) >= MIN_SESSIONS else None


def _past_earnings_dates(symbol: str) -> List[date]:
    """Report dates before today, newest first, cached for a day."""
    cached = _EARNINGS_CACHE.get(symbol)
    if cached and time.time() - cached[0] <= _EARNINGS_TTL_SECONDS:
        return cached[1]
    frame = _with_retry(lambda: yf.Ticker(symbol).get_earnings_dates(limit=EARNINGS_REPORTS + 8),
                        f"yf_earnings_dates_{symbol}", retries=1)
    today = datetime.now().date()
    dates = sorted({stamp.date() for stamp in getattr(frame, "index", []) if stamp.date() < today},
                   reverse=True)[:EARNINGS_REPORTS]
    _EARNINGS_CACHE[symbol] = (time.time(), dates)
    return dates


def _earnings_moves(close: Any, report_dates: List[date]) -> List[float]:
    """Size of the move from the close before a report to the close after it.

    Two sessions, so it catches the reaction whether the report came before
    the open or after the close.
    """
    days = list(close.index)
    moves = []
    for day in report_dates:
        before = [i for i, d in enumerate(days) if d < day]
        if not before or before[-1] + 2 >= len(days):
            continue
        start = before[-1]
        after = start + 2 if days[start + 1] == day else start + 1
        moves.append(abs(close.iloc[after] / close.iloc[start] - 1) * 100)
    return moves


def compute(close: Any, report_dates: Optional[List[date]] = None) -> Optional[Forecast]:
    """The forecast from a date-indexed close series. Pure."""
    if close is None or len(close) < MIN_SESSIONS:
        return None
    recent = close.tail(LOOKBACK_SESSIONS + max(n for n, _ in HOLDS))
    bad = []
    for sessions, label in HOLDS:
        returns = (recent / recent.shift(sessions) - 1).dropna() * 100
        if len(returns) < 20:
            return None
        bad.append((label, float(returns.quantile(BAD_QUANTILE))))

    window = close.tail(DROP_SESSIONS)
    drawdown = window / window.cummax() - 1
    bottom = drawdown.idxmin()
    peak = window.loc[:bottom].idxmax()

    moves = _earnings_moves(close, report_dates or [])
    two_day = (close.tail(LOOKBACK_SESSIONS) / close.tail(LOOKBACK_SESSIONS).shift(2) - 1).dropna().abs() * 100
    return Forecast(
        sessions=len(window),
        bad_moves=tuple(bad),
        max_drop_pct=float(drawdown.min() * 100),
        drop_from=peak,
        drop_to=bottom,
        earnings_move_pct=sum(moves) / len(moves) if moves else None,
        earnings_reports=len(moves),
        normal_two_day_pct=float(two_day.median()) if len(two_day) else None,
    )


def build(symbol: str) -> Optional[Forecast]:
    """Fetch and compute. Never raises; None when there isn't enough data."""
    if not SETTINGS.risk_forecast_enabled:
        return None
    try:
        close = _closes(symbol)
    except Exception as exc:
        SYS_LOG.warning("risk_forecast_prices_failed symbol=%s detail=%s", symbol, exc)
        return None
    if close is None:
        return None
    try:
        reports = _past_earnings_dates(symbol)
    except Exception as exc:
        SYS_LOG.debug("risk_forecast_earnings_failed symbol=%s detail=%s", symbol, exc)
        reports = []
    return compute(close, reports)


def _nok(pct: float, amount: Optional[float]) -> str:
    return f" ≈ {amount * pct / 100:+,.0f} NOK on {amount:,.0f}" if amount else ""


def lines(forecast: Optional[Forecast], amount: Optional[float] = None) -> List[str]:
    """The forecast as indented message lines. Empty when there is no forecast."""
    if forecast is None:
        return []
    out = []
    for label, pct in forecast.bad_moves:
        out.append(f"    a bad {label}: {pct:+.1f}%{_nok(pct, amount)} (1 in 20 was worse, last year)")
    out.append(
        f"    biggest fall in {'2 years' if forecast.sessions >= TWO_YEARS_SESSIONS else 'the saved history'}: "
        f"{forecast.max_drop_pct:.1f}% ({forecast.drop_from:%b %Y} to {forecast.drop_to:%b %Y})"
    )
    if forecast.earnings_move_pct is not None:
        normal = (f" (a normal 2 days: ±{forecast.normal_two_day_pct:.1f}%)"
                  if forecast.normal_two_day_pct else "")
        out.append(
            f"    report days: moved ±{forecast.earnings_move_pct:.1f}% on average "
            f"over the last {forecast.earnings_reports}{normal}"
        )
    return [escape(line, quote=False) for line in out]
