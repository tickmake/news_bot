"""Per-symbol indicators, trade metrics, gates and ranking."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional, Tuple

import pandas as pd

from newsbot import market_data
from newsbot.config import SETTINGS
from newsbot.logsetup import SYS_LOG


def _compute_atr_percent(history: Any) -> Optional[float]:
    try:
        high = history["High"].dropna()
        low = history["Low"].dropna()
        close = history["Close"].dropna()
        if len(close) < 15 or len(high) < 15 or len(low) < 15:
            return None
        trs: List[float] = []
        for idx in range(1, len(close)):
            tr = max(
                float(high.iloc[idx] - low.iloc[idx]),
                abs(float(high.iloc[idx] - close.iloc[idx - 1])),
                abs(float(low.iloc[idx] - close.iloc[idx - 1])),
            )
            trs.append(tr)
        if len(trs) < 14:
            return None
        atr = sum(trs[-14:]) / 14.0
        last_close = float(close.iloc[-1])
        if last_close == 0:
            return None
        return (atr / last_close) * 100
    except Exception as exc:
        SYS_LOG.debug("atr_compute_failed detail=%s", exc)
        return None


def _compute_rsi_14(close_series: Any) -> Optional[float]:
    """14-period RSI using a simple (not Wilder-smoothed) average of gains and
    losses, matching the plain 14-bar averaging `_compute_atr_percent` already
    uses above -- one averaging convention for the whole module rather than
    two silently different ones.
    """
    try:
        if len(close_series) < 15:
            return None
        deltas = close_series.diff().dropna()
        if len(deltas) < 14:
            return None
        gains = deltas.clip(lower=0.0)
        losses = -deltas.clip(upper=0.0)
        avg_gain = float(gains.tail(14).mean())
        avg_loss = float(losses.tail(14).mean())
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
    except Exception as exc:
        SYS_LOG.debug("rsi_compute_failed detail=%s", exc)
        return None


def _rsi_series(close_series: Any) -> Any:
    """RSI 14 as a series, for plotting.

    Same simple-average convention as `_compute_rsi_14` above, so the last
    point of this series equals that function's scalar -- a chart that
    disagreed with the RSI printed next to it would be worse than no chart.
    A zero average loss maps to 100 (unbroken gains), matching it too.
    """
    try:
        if close_series is None or len(close_series) < 15:
            return None
        deltas = close_series.diff()
        gains = deltas.clip(lower=0.0)
        losses = -deltas.clip(upper=0.0)
        avg_gain = gains.rolling(14).mean()
        avg_loss = losses.rolling(14).mean()
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return rsi.where(avg_loss != 0, 100.0).dropna()
    except Exception as exc:
        SYS_LOG.debug("rsi_series_failed detail=%s", exc)
        return None


def _compute_volume_trend(volume_series: Any) -> str:
    """Recent 5-session average volume vs. the 15 sessions before that.

    A coarse direction only -- "rising"/"falling"/"flat" -- not a signal on
    its own; it feeds the candidate's supply/demand read alongside the
    volume-ratio tie-breaker that already exists in `_rank_key`.
    """
    try:
        recent_sessions = volume_series.tail(20)
        if len(recent_sessions) < 20 or recent_sessions.isna().any():
            return "unknown"
        recent_avg = float(recent_sessions.tail(5).mean())
        prior_avg = float(recent_sessions.head(15).mean())
        if prior_avg <= 0:
            return "unknown"
        change = (recent_avg - prior_avg) / prior_avg
        if change >= 0.15:
            return "rising"
        if change <= -0.15:
            return "falling"
        return "flat"
    except Exception as exc:
        SYS_LOG.debug("volume_trend_compute_failed detail=%s", exc)
        return "unknown"


@dataclass(frozen=True)
class TradeMetrics:
    """Measured facts about one symbol. Carries no pass/fail judgement."""

    symbol: str
    session: str
    last_close: float
    day_change_pct: float
    week_momentum_pct: float
    volume_ratio: Optional[float]
    drawdown_pct: float
    atr_pct: float
    above_ema20: bool
    # Added for swing/day-trade signal enrichment. Defaulted so the many
    # existing call sites that build a TradeMetrics without these (mostly
    # tests) keep working unchanged.
    ema20: float = 0.0
    rsi_14: Optional[float] = None
    support_level: float = 0.0
    resistance_level: float = 0.0
    volume_trend: str = "unknown"


def _drop_in_progress_bar(history: Any) -> Any:
    """Drop the final row when it is dated today in the exchange's timezone.

    yfinance returns a tz-aware DatetimeIndex in the exchange's own timezone, so
    "today" is evaluated there rather than in the host's locale. A market that
    already closed today still has its session discarded: this screen ranks
    symbols against each other, and letting some rows advance a session while
    others do not would compare momentum windows offset by a day.

    If the index is tz-naive, there is no exchange timezone to fall back on,
    so "today" is evaluated in the host's local date instead — the host's
    `TZ` then silently determines which bar gets dropped.
    """
    if history is None or len(history) == 0:
        return history
    index = history.index
    tz = getattr(index, "tz", None)
    today = datetime.now(tz).date() if tz is not None else datetime.now().date()
    if index[-1].date() == today:
        return history.iloc[:-1]
    return history


def _compute_trade_metrics(symbol: str) -> Optional[TradeMetrics]:
    """Measure one symbol, or None when the data cannot support a measurement.

    None means "no usable data" and nothing else. Whether a symbol qualifies is
    _failed_gates' decision; keeping the two separate is what lets the caller
    report why a run found nothing.
    """
    try:
        history = _drop_in_progress_bar(market_data._fetch_ticker_history(symbol))
        close_series = history["Close"].dropna()
        if len(close_series) < 30:
            SYS_LOG.debug(
                "metrics_skipped symbol=%s reason=insufficient_history bars=%s",
                symbol,
                len(close_series),
            )
            return None

        last_close = float(close_series.iloc[-1])
        prev_close = float(close_series.iloc[-2])
        if prev_close == 0:
            SYS_LOG.debug("metrics_skipped symbol=%s reason=zero_prev_close", symbol)
            return None
        day_change_pct = ((last_close - prev_close) / prev_close) * 100

        # Safe unconditionally: the length guard above already rules out short series.
        reference_close = float(close_series.iloc[-6])
        if reference_close == 0:
            SYS_LOG.debug("metrics_skipped symbol=%s reason=zero_reference_close", symbol)
            return None
        week_momentum_pct = ((last_close - reference_close) / reference_close) * 100

        ema_20 = float(close_series.ewm(span=20, adjust=False).mean().iloc[-1])

        volume_ratio: Optional[float] = None
        volume_trend = "unknown"
        if "Volume" in history:
            volume_series = history["Volume"]
            if len(volume_series) >= 21:
                # Compare the completed session with the *previous* 20
                # sessions; including today's volume in its own baseline
                # suppresses the very spike this ratio is meant to reveal.
                prior_volumes = volume_series.iloc[-21:-1]
                last_volume = volume_series.iloc[-1]
                if not prior_volumes.isna().any() and pd.notna(last_volume):
                    avg_volume_20 = float(prior_volumes.mean())
                    if avg_volume_20 > 0:
                        volume_ratio = float(last_volume) / avg_volume_20
            volume_trend = _compute_volume_trend(volume_series)

        resistance_level = float(close_series.tail(20).max())
        support_level = float(close_series.tail(20).min())
        drawdown_pct = (
            ((resistance_level - last_close) / resistance_level) * 100
            if resistance_level
            else 0.0
        )

        atr_pct = _compute_atr_percent(history)
        if atr_pct is None:
            SYS_LOG.debug("metrics_skipped symbol=%s reason=atr_unavailable", symbol)
            return None

        rsi_14 = _compute_rsi_14(close_series)

        return TradeMetrics(
            symbol=symbol,
            session=close_series.index[-1].strftime("%Y-%m-%d"),
            last_close=last_close,
            day_change_pct=day_change_pct,
            week_momentum_pct=week_momentum_pct,
            volume_ratio=volume_ratio,
            drawdown_pct=drawdown_pct,
            atr_pct=atr_pct,
            above_ema20=last_close > ema_20,
            ema20=ema_20,
            rsi_14=rsi_14,
            support_level=support_level,
            resistance_level=resistance_level,
            volume_trend=volume_trend,
        )
    except Exception as exc:
        SYS_LOG.debug("metrics_failed symbol=%s detail=%s", symbol, exc)
        return None


# Evaluated in this order, so a rejected symbol attributes to its first
# meaningful blocker in the diagnostic footer.
TRADE_GATES: Tuple[str, ...] = (
    "trend",
    "momentum_5d",
    "momentum_1d",
    "volatility",
    "drawdown",
)


def _failed_gates(metrics: TradeMetrics) -> List[str]:
    """Gates this symbol fails. Empty means it qualifies.

    Every gate is mandatory. The previous additive score let three risk
    criteria substitute for the three momentum criteria, so a steadily
    declining stock was admitted on low volatility and low drawdown alone.

    Volume is deliberately absent: it confirms a move but its absence does not
    disqualify one, so it ranks (see _rank_key) rather than admits.
    """
    failed: List[str] = []
    if not metrics.above_ema20:
        failed.append("trend")
    if metrics.week_momentum_pct < SETTINGS.trade_min_week_momentum_pct:
        failed.append("momentum_5d")
    if metrics.day_change_pct <= SETTINGS.trade_min_day_change_pct:
        failed.append("momentum_1d")
    if metrics.atr_pct > SETTINGS.trade_max_atr_pct:
        failed.append("volatility")
    if metrics.drawdown_pct > SETTINGS.trade_max_drawdown_pct:
        failed.append("drawdown")
    return failed
