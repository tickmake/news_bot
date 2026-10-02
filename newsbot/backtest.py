"""Walk-forward backtest engine and its summaries."""

import math
import re
import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from newsbot.benchmarks import _backtest_benchmark_for, _matched_benchmark_return
from newsbot.config import SETTINGS
from newsbot.indicators import TradeMetrics, _failed_gates
from newsbot.logsetup import SYS_LOG
from newsbot.ranking import _rank_key


def _backtest_metrics_by_session(symbol: str, history: Any) -> Tuple[Any, Dict[str, TradeMetrics]]:
    """Calculate the screener's daily features using data available at each close."""
    if history is None or len(history) == 0:
        return None, {}

    columns = ("Open", "High", "Low", "Close", "Volume")
    if any(column not in history for column in columns[:4]):
        SYS_LOG.warning("backtest_history_unusable symbol=%s reason=missing_ohlc", symbol)
        return None, {}
    frame = history.copy()
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        SYS_LOG.warning("backtest_history_unusable symbol=%s reason=unordered_or_duplicate_sessions", symbol)
        return None, {}
    for column in columns:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    prices = frame[list(columns[:4])]
    if (
        prices.isna().any().any()
        or prices.isin([float("inf"), float("-inf")]).any().any()
        or (prices <= 0).any().any()
        or (frame["High"] < frame["Low"]).any()
    ):
        # Dropping invalid rows would turn missing prices into shorter trading
        # horizons and silently report the wrong entry/exit sessions.
        SYS_LOG.warning("backtest_history_unusable symbol=%s reason=invalid_ohlc", symbol)
        return None, {}
    if len(frame) < 30:
        return frame, {}

    close = frame["Close"]
    high = frame["High"]
    low = frame["Low"]
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_pct = true_range.rolling(14, min_periods=14).mean() / close * 100.0
    ema20 = close.ewm(span=20, adjust=False).mean()
    gains = close.diff().clip(lower=0.0)
    losses = -close.diff().clip(upper=0.0)
    avg_gain = gains.rolling(14, min_periods=14).mean()
    avg_loss = losses.rolling(14, min_periods=14).mean()
    rs = avg_gain / avg_loss
    rsi = (100.0 - (100.0 / (1.0 + rs))).where(avg_loss != 0.0, 100.0)
    volume = frame["Volume"] if "Volume" in frame else pd.Series(float("nan"), index=frame.index)
    average_prior_volume = volume.shift(1).rolling(20, min_periods=20).mean()
    volume_ratio = volume / average_prior_volume
    recent_volume = volume.rolling(5, min_periods=5).mean()
    prior_volume = volume.shift(5).rolling(15, min_periods=15).mean()
    volume_change = (recent_volume - prior_volume) / prior_volume
    support = close.rolling(20, min_periods=20).min()
    resistance = close.rolling(20, min_periods=20).max()

    metrics_by_session: Dict[str, TradeMetrics] = {}
    for position in range(29, len(frame)):
        values = (
            close.iloc[position],
            close.iloc[position - 1],
            close.iloc[position - 5],
            atr_pct.iloc[position],
            ema20.iloc[position],
            support.iloc[position],
            resistance.iloc[position],
        )
        if any(
            pd.isna(value) or not math.isfinite(float(value))
            for value in values
        ):
            continue
        last_close, prior_close, week_close, atr_value, ema_value, support_value, resistance_value = (
            float(value) for value in values
        )
        if prior_close == 0 or week_close == 0 or resistance_value == 0:
            continue
        ratio = volume_ratio.iloc[position]
        rsi_value = rsi.iloc[position]
        volume_delta = volume_change.iloc[position]
        if (
            pd.isna(volume_delta)
            or pd.isna(prior_volume.iloc[position])
            or prior_volume.iloc[position] <= 0
        ):
            trend = "unknown"
        elif volume_delta >= 0.15:
            trend = "rising"
        elif volume_delta <= -0.15:
            trend = "falling"
        else:
            trend = "flat"
        session = frame.index[position].strftime("%Y-%m-%d")
        metrics_by_session[session] = TradeMetrics(
            symbol=symbol,
            session=session,
            last_close=last_close,
            day_change_pct=(last_close - prior_close) / prior_close * 100.0,
            week_momentum_pct=(last_close - week_close) / week_close * 100.0,
            volume_ratio=(
                float(ratio) if pd.notna(ratio) and math.isfinite(float(ratio)) else None
            ),
            drawdown_pct=(resistance_value - last_close) / resistance_value * 100.0,
            atr_pct=atr_value,
            above_ema20=last_close > ema_value,
            ema20=ema_value,
            rsi_14=float(rsi_value) if pd.notna(rsi_value) else None,
            support_level=support_value,
            resistance_level=resistance_value,
            volume_trend=trend,
        )
    return frame, metrics_by_session


def _summarize_backtest_trades(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-trade descriptive statistics; overlapping signals are not independent."""
    returns = [trade["net_return_pct"] for trade in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    matched = [trade for trade in trades if trade["excess_return_pct"] is not None]
    dates = sorted({trade["signal_session"] for trade in trades})
    return {
        "trade_count": len(trades),
        "signal_days": len(dates),
        "symbol_count": len({trade["symbol"] for trade in trades}),
        "signal_start": dates[0] if dates else None,
        "signal_end": dates[-1] if dates else None,
        "hit_rate_pct": len(wins) / len(returns) * 100.0 if returns else None,
        "avg_net_return_pct": statistics.mean(returns) if returns else None,
        "median_net_return_pct": statistics.median(returns) if returns else None,
        "avg_win_pct": statistics.mean(wins) if wins else None,
        "avg_loss_pct": statistics.mean(losses) if losses else None,
        "worst_return_pct": min(returns) if returns else None,
        "avg_adverse_excursion_pct": (
            statistics.mean(trade["adverse_excursion_pct"] for trade in trades)
            if trades else None
        ),
        "worst_adverse_excursion_pct": (
            min(trade["adverse_excursion_pct"] for trade in trades) if trades else None
        ),
        "benchmark_count": len(matched),
        "avg_matched_net_return_pct": (
            statistics.mean(trade["net_return_pct"] for trade in matched) if matched else None
        ),
        "avg_benchmark_return_pct": (
            statistics.mean(trade["benchmark_return_pct"] for trade in matched) if matched else None
        ),
        "avg_excess_return_pct": (
            statistics.mean(trade["excess_return_pct"] for trade in matched) if matched else None
        ),
        "excess_hit_rate_pct": (
            sum(trade["excess_return_pct"] > 0 for trade in matched) / len(matched) * 100.0
            if matched else None
        ),
    }


def _backtest_cohort_summary(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    def compare(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "all": _summarize_backtest_trades(rows),
            "top": _summarize_backtest_trades([trade for trade in rows if trade["selected"]]),
        }

    result = compare(trades)
    result["by_market"] = {
        market: compare([trade for trade in trades if trade["market"] == market])
        for market in sorted({trade["market"] for trade in trades})
    }
    result["by_quarter"] = {
        quarter: compare([trade for trade in trades if trade["quarter"] == quarter])
        for quarter in sorted({trade["quarter"] for trade in trades})
    }
    return result


def _backtest_diagnostics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Descriptive slices, not optimized thresholds or causal estimates."""
    groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for trade in trades:
        features = trade["signal_metrics"]
        volume, rsi = features["volume_ratio"], features["rsi_14"]
        momentum, atr = features["week_momentum_pct"], features["atr_pct"]
        streak = trade["qualifying_streak"]
        buckets = {
            "rank": str(trade["rank"]),
            "momentum": "<3%" if momentum < 3 else "3-6%" if momentum < 6 else ">=6%",
            "volume": (
                "unknown" if volume is None else
                "confirmed" if volume >= SETTINGS.trade_min_volume_ratio else "unconfirmed"
            ),
            "RSI": "unknown" if rsi is None else "<70" if rsi < 70 else ">=70",
            "ATR": "<2%" if atr < 2 else ">=2%",
            "streak": "1" if streak == 1 else "2-3" if streak < 4 else ">=4",
            "stock": trade["symbol"],
            "quarter": trade["quarter"],
        }
        for dimension, bucket in buckets.items():
            groups.setdefault(dimension, {}).setdefault(bucket, []).append(trade)
        by_date.setdefault(trade["signal_session"], []).append(trade)

    net_deltas: List[float] = []
    excess_deltas: List[float] = []
    incomplete_dates = no_comparison_dates = 0
    for rows in by_date.values():
        # Do not compare a date if a qualifier's outcome was pending or purged.
        if len(rows) != rows[0]["qualifier_count"]:
            incomplete_dates += 1
            continue
        selected = [row for row in rows if row["selected"]]
        others = [row for row in rows if not row["selected"]]
        if not selected or not others:
            no_comparison_dates += 1
            continue
        net_deltas.append(
            statistics.mean(row["net_return_pct"] for row in selected)
            - statistics.mean(row["net_return_pct"] for row in others)
        )
        if all(row["excess_return_pct"] is not None for row in rows):
            excess_deltas.append(
                statistics.mean(row["excess_return_pct"] for row in selected)
                - statistics.mean(row["excess_return_pct"] for row in others)
            )
    return {
        "groups": {
            dimension: {
                bucket: _summarize_backtest_trades(rows)
                for bucket, rows in sorted(
                    buckets.items(),
                    key=lambda item: int(item[0]) if dimension == "rank" else item[0],
                )
            }
            for dimension, buckets in groups.items()
        },
        "paired_ranking": {
            "paired_dates": len(net_deltas),
            "avg_net_delta_pct": statistics.mean(net_deltas) if net_deltas else None,
            "median_net_delta_pct": statistics.median(net_deltas) if net_deltas else None,
            "selected_better_pct": (
                sum(delta > 0 for delta in net_deltas) / len(net_deltas) * 100
                if net_deltas else None
            ),
            "benchmark_paired_dates": len(excess_deltas),
            "avg_excess_delta_pct": statistics.mean(excess_deltas) if excess_deltas else None,
            "incomplete_dates": incomplete_dates,
            "no_comparison_dates": no_comparison_dates,
        },
    }


def _backtest_cost_validation(
    trades: List[Dict[str, Any]], cost_bps_per_side: float,
) -> Dict[str, Any]:
    """Stress identical picks, excluding holds crossing chronological block boundaries."""
    rows = [
        trade for trade in trades
        if trade["quarter"] == (
            f"{trade['exit_session'][:4]}-Q"
            f"{(int(trade['exit_session'][5:7]) - 1) // 3 + 1}"
        )
    ]
    scenarios = []
    for extra_cost in (0.0, 15.0, 40.0):
        cost = cost_bps_per_side + extra_cost
        adjusted = []
        for trade in rows:
            net = trade["gross_return_pct"] - 2.0 * cost / 100.0
            adjusted.append({
                **trade,
                "net_return_pct": net,
                "excess_return_pct": (
                    net - trade["benchmark_return_pct"]
                    if trade["benchmark_return_pct"] is not None else None
                ),
            })
        scenarios.append({
            "cost_bps_per_side": cost,
            "cohorts": {
                cohort: _backtest_cohort_summary([
                    trade for trade in adjusted if trade["cohort"] == cohort
                ])
                for cohort in ("development", "holdout")
            },
        })
    return {
        "quarter_crossing_excluded": len(trades) - len(rows),
        "scenarios": scenarios,
    }


def _backtest_baseline_summary(
    baseline: List[Dict[str, Any]], qualifiers: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Every liquid stock with no gates, and how the screener compares to it.

    The paired delta is by signal date with each date weighted equally, over
    dates that have both a baseline and at least one qualifier (or selected
    pick), so a busy date cannot dominate. Positive favours the screener.
    """
    def by_date(rows: List[Dict[str, Any]]) -> Dict[str, float]:
        grouped: Dict[str, List[float]] = {}
        for trade in rows:
            grouped.setdefault(trade["signal_session"], []).append(trade["net_return_pct"])
        return {date: statistics.mean(values) for date, values in grouped.items()}

    base_by_date = by_date(baseline)

    def paired(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        mine = by_date(rows)
        deltas = [mine[date] - base_by_date[date] for date in mine if date in base_by_date]
        return {
            "paired_dates": len(deltas),
            "mean_delta_pp": statistics.mean(deltas) if deltas else None,
            "better_pct": sum(delta > 0 for delta in deltas) / len(deltas) * 100.0 if deltas else None,
        }

    return {
        "stats": _summarize_backtest_trades(baseline),
        "qualifiers_vs_baseline": paired(qualifiers),
        "top_vs_baseline": paired([trade for trade in qualifiers if trade["selected"]]),
    }


def _walk_forward_backtest(
    histories: Dict[str, Any],
    benchmark_histories: Dict[str, Any],
    *,
    horizon_sessions: int,
    top_n: int = 5,
    cost_bps_per_side: float = 10.0,
    holdout_start: Optional[str] = None,
    min_avg_volume: float = 0.0,
) -> Dict[str, Any]:
    """Backtest current gates/ranking without using future bars in a signal.

    A signal is formed at a completed close, entered at the next session's
    open, and exited at that session's close after `horizon_sessions` bars.
    """
    if horizon_sessions < 1:
        raise ValueError("horizon_sessions must be at least 1")
    if top_n < 1:
        raise ValueError("top_n must be at least 1")
    if not math.isfinite(cost_bps_per_side) or cost_bps_per_side < 0:
        raise ValueError("cost_bps_per_side must be finite and nonnegative")
    if not math.isfinite(min_avg_volume) or min_avg_volume < 0:
        raise ValueError("min_avg_volume must be finite and nonnegative")
    if holdout_start:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", holdout_start):
            raise ValueError("holdout_start must be YYYY-MM-DD")
        datetime.strptime(holdout_start, "%Y-%m-%d")

    frames: Dict[str, Any] = {}
    session_positions: Dict[str, Dict[str, int]] = {}
    liquidity_by_session: Dict[Tuple[str, str], bool] = {}
    metrics_by_day: Dict[str, List[TradeMetrics]] = {}
    streak_by_signal: Dict[Tuple[str, str], int] = {}
    for symbol, history in histories.items():
        frame, daily_metrics = _backtest_metrics_by_session(symbol, history)
        if frame is None or not daily_metrics:
            continue
        frames[symbol] = frame
        session_positions[symbol] = {
            row.strftime("%Y-%m-%d"): index for index, row in enumerate(frame.index)
        }
        avg_volume = (
            frame["Volume"].shift(1).rolling(20, min_periods=20).mean()
            if "Volume" in frame else None
        )
        streak = 0
        for session, metrics in daily_metrics.items():
            position = session_positions[symbol][session]
            prior_volume = float(avg_volume.iloc[position]) if avg_volume is not None else float("nan")
            liquidity_by_session[(session, symbol)] = (
                min_avg_volume == 0
                or (math.isfinite(prior_volume) and prior_volume >= min_avg_volume)
            )
            qualifies = not _failed_gates(metrics)
            streak = streak + 1 if qualifies else 0
            streak_by_signal[(session, symbol)] = streak
            metrics_by_day.setdefault(session, []).append(metrics)

    benchmark_frames: Dict[str, Any] = {}
    benchmark_positions: Dict[str, Dict[str, int]] = {}
    for symbol, history in benchmark_histories.items():
        if history is None or len(history) == 0 or "Open" not in history or "Close" not in history:
            continue
        benchmark_frames[symbol] = history
        benchmark_positions[symbol] = {
            row.strftime("%Y-%m-%d"): index for index, row in enumerate(history.index)
        }

    signal_dates = sorted(metrics_by_day)
    split = holdout_start
    if not split and signal_dates:
        split = signal_dates[min(len(signal_dates) - 1, int(len(signal_dates) * 0.75))]
    trades: List[Dict[str, Any]] = []
    baseline_trades: List[Dict[str, Any]] = []
    liquidity_rejected = 0
    pending_outcomes = 0
    purged_outcomes = 0
    transaction_cost_pct = 2.0 * cost_bps_per_side / 100.0

    def _simulate(metrics: TradeMetrics, signal_session: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """One hold from a signal: ("ok", trade) or ("pending"|"purged"|"skip", None).

        Shared by the screener's qualifiers and the no-gates baseline so both
        are measured with identical entry, exit, cost, purge and benchmark rules.
        """
        frame = frames[metrics.symbol]
        signal_position = session_positions[metrics.symbol].get(signal_session)
        if signal_position is None:
            return "skip", None
        entry_position = signal_position + 1
        exit_position = signal_position + horizon_sessions
        if exit_position >= len(frame):
            return "pending", None
        entry_row, exit_row = frame.iloc[entry_position], frame.iloc[exit_position]
        entry_price = float(entry_row["Open"])
        exit_price = float(exit_row["Close"])
        if (
            not math.isfinite(entry_price)
            or not math.isfinite(exit_price)
            or entry_price <= 0
            or exit_price <= 0
        ):
            return "skip", None
        gross_return = (exit_price / entry_price - 1.0) * 100.0
        net_return = gross_return - transaction_cost_pct
        entry_date = frame.index[entry_position].strftime("%Y-%m-%d")
        exit_date = frame.index[exit_position].strftime("%Y-%m-%d")
        # Development returns must not include prices from the reserved
        # holdout. Warm-up features may legitimately use earlier bars.
        if split and signal_session < split <= exit_date:
            return "purged", None
        benchmark_symbol = _backtest_benchmark_for(metrics.symbol)
        benchmark = benchmark_frames.get(benchmark_symbol or "")
        benchmark_return = _matched_benchmark_return(
            benchmark, entry_date, exit_date,
            benchmark_positions.get(benchmark_symbol or ""),
        )
        return "ok", {
            "symbol": metrics.symbol,
            "cohort": "holdout" if split and signal_session >= split else "development",
            "market": benchmark_symbol or f"unmapped:{metrics.symbol.rsplit('.', 1)[-1]}",
            "quarter": f"{signal_session[:4]}-Q{(int(signal_session[5:7]) - 1) // 3 + 1}",
            "signal_session": signal_session,
            "entry_session": entry_date,
            "exit_session": exit_date,
            "gross_return_pct": gross_return,
            "net_return_pct": net_return,
            "adverse_excursion_pct": min(
                0.0,
                (float(frame.iloc[entry_position:exit_position + 1]["Low"].min())
                 / entry_price - 1.0) * 100.0,
            ),
            "benchmark_symbol": benchmark_symbol,
            "benchmark_return_pct": benchmark_return,
            "excess_return_pct": (
                net_return - benchmark_return if benchmark_return is not None else None
            ),
        }

    for signal_session in sorted(metrics_by_day):
        qualifiers = [
            metrics for metrics in metrics_by_day[signal_session] if not _failed_gates(metrics)
        ]
        candidates = [
            metrics for metrics in qualifiers if liquidity_by_session[(signal_session, metrics.symbol)]
        ]
        liquidity_rejected += len(qualifiers) - len(candidates)
        candidates.sort(
            key=lambda metrics: _rank_key(
                metrics, streak_by_signal[(signal_session, metrics.symbol)]
            )
        )
        for rank, metrics in enumerate(candidates, 1):
            status, trade = _simulate(metrics, signal_session)
            if status == "pending":
                pending_outcomes += 1
            elif status == "purged":
                purged_outcomes += 1
            if trade is None:
                continue
            trade.update(
                {
                    "rank": rank,
                    "qualifier_count": len(candidates),
                    "signal_metrics": {
                        "week_momentum_pct": metrics.week_momentum_pct,
                        "volume_ratio": metrics.volume_ratio,
                        "rsi_14": metrics.rsi_14,
                        "atr_pct": metrics.atr_pct,
                    },
                    "qualifying_streak": streak_by_signal[(signal_session, metrics.symbol)],
                    "selected": rank <= top_n,
                }
            )
            trades.append(trade)

        # No-gates baseline: every liquid stock on the date, qualifier or not.
        for metrics in metrics_by_day[signal_session]:
            if not liquidity_by_session[(signal_session, metrics.symbol)]:
                continue
            _, trade = _simulate(metrics, signal_session)
            if trade is not None:
                trade["selected"] = False
                baseline_trades.append(trade)

    selected_trades = [trade for trade in trades if trade["selected"]]
    return {
        **_summarize_backtest_trades(selected_trades),
        "usable_symbols": len(frames),
        "usable_stock_symbols": sorted(frames),
        "holdout_start": split,
        "split_mode": "fixed" if holdout_start else "automatic",
        "top_n": top_n,
        "liquidity_rejected": liquidity_rejected,
        "pending_outcomes": pending_outcomes,
        "purged_outcomes": purged_outcomes,
        "cohorts": {
            cohort: _backtest_cohort_summary(
                [trade for trade in trades if trade["cohort"] == cohort]
            )
            for cohort in ("development", "holdout")
        },
        "diagnostics": {
            cohort: _backtest_diagnostics(
                [trade for trade in trades if trade["cohort"] == cohort]
            )
            for cohort in ("development", "holdout")
        },
        "baseline": {
            cohort: _backtest_baseline_summary(
                [trade for trade in baseline_trades if trade["cohort"] == cohort],
                [trade for trade in trades if trade["cohort"] == cohort],
            )
            for cohort in ("development", "holdout")
        },
        "cost_validation": _backtest_cost_validation(trades, cost_bps_per_side),
        "trades": selected_trades,
        "all_trades": trades,
    }
