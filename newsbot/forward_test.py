"""Forward test: how signals recorded live, going forward, actually did.

The walk-forward backtest (`/performance`) replays history, so every rule it
judges was chosen with the data in view. This report judges only signals the
bot recorded *before* their outcome existed (`signal_archive`, never pruned),
which makes it the one out-of-sample read the project has. It is deliberately
blunt about sample size: a handful of dates says nothing either way.
"""

import json
import math
import statistics
from datetime import datetime, timedelta
from html import escape
from typing import Any, Dict, List, Optional, Tuple

import requests.exceptions
import yfinance as yf

from newsbot import history_store, market_data
from newsbot.benchmarks import _matched_benchmark_return
from newsbot.config import SETTINGS
from newsbot.indicators import _drop_in_progress_bar
from newsbot.logsetup import SYS_LOG

# Dates needed before the paired comparison is worth reading at all. The
# backtest's per-variant standard error was ~0.1 pp over ~400 dates; far fewer
# dates cannot separate a real edge from noise.
MIN_PAIRED_DATES = 60
TOP_N = 5


def forward_outcome(
    row: Dict[str, Any], history: Any, benchmark: Any
) -> Optional[Dict[str, Any]]:
    """Enter at the next session's open, exit at the close `horizon` sessions on.

    None means "not finished yet". Raises ValueError for data that cannot be
    trusted, so the row stays pending instead of recording a wrong number.
    """
    if history is None or len(history) == 0:
        raise ValueError("missing stock history")
    horizon = int(row["horizon_sessions"])
    if horizon < 1:
        raise ValueError("invalid holding horizon")
    dates = history.index.strftime("%Y-%m-%d").tolist()
    if dates != sorted(set(dates)):
        raise ValueError("unordered or duplicate stock sessions")
    if dates[0] > row["signal_session"]:
        raise ValueError("history does not cover the signal session")
    eligible = [index for index, date in enumerate(dates) if date > row["signal_session"]]
    if not eligible:
        return None
    entry = eligible[0]
    exit_position = entry + horizon - 1
    if exit_position >= len(history):
        return None
    prices = history.iloc[entry:exit_position + 1][["Open", "Close"]]
    if (
        prices.isna().any().any()
        or prices.isin([float("inf"), float("-inf")]).any().any()
        or (prices <= 0).any().any()
    ):
        raise ValueError("invalid prices during holding period")
    entry_price = float(history.iloc[entry]["Open"])
    exit_price = float(history.iloc[exit_position]["Close"])
    gross = (exit_price / entry_price - 1.0) * 100.0
    net = gross - 2.0 * float(row["cost_bps_per_side"]) / 100.0
    benchmark_return = _matched_benchmark_return(benchmark, dates[entry], dates[exit_position])
    return {
        "entry_session": dates[entry],
        "exit_session": dates[exit_position],
        "gross_return_pct": gross,
        "net_return_pct": net,
        "benchmark_return_pct": benchmark_return,
        "excess_return_pct": net - benchmark_return if benchmark_return is not None else None,
    }


def _might_be_finished(row: Dict[str, Any], today: datetime) -> bool:
    """Cheap pre-check so history is only fetched for rows that could complete."""
    try:
        signal = datetime.strptime(row["signal_session"], "%Y-%m-%d")
    except ValueError:
        return False
    # `horizon` trading sessions after the signal needs at least that many
    # calendar days, and the last bar must be a completed one.
    return today - signal >= timedelta(days=int(row["horizon_sessions"]) + 1)


def settle_pending(today: Optional[datetime] = None) -> Tuple[int, int]:
    """Fill in outcomes that have become computable. Returns (settled, failed)."""
    today = today or datetime.now()
    store = history_store.TRADE_HISTORY
    pending = [
        row for row in store.archive_rows()
        if not row["outcome_json"] and _might_be_finished(row, today)
    ]
    cache: Dict[str, Any] = {}

    def history_for(symbol: Optional[str]) -> Any:
        if not symbol:
            return None
        if symbol not in cache:
            try:
                cache[symbol] = _drop_in_progress_bar(
                    market_data._fetch_ticker_history(symbol, period="2y")
                )
            except (requests.exceptions.RequestException, yf.exceptions.YFException,
                    ValueError, RuntimeError, OSError) as exc:
                SYS_LOG.warning("forward_history_failed symbol=%s detail=%s", symbol, exc)
                cache[symbol] = None
        return cache[symbol]

    settled = failed = 0
    for row in pending:
        try:
            outcome = forward_outcome(
                row, history_for(row["symbol"]), history_for(row["benchmark_symbol"])
            )
        except (ValueError, KeyError, TypeError) as exc:
            SYS_LOG.warning("forward_outcome_failed symbol=%s detail=%s", row["symbol"], exc)
            failed += 1
            continue
        if outcome is None:
            continue
        store.complete_archive_row(row["symbol"], row["signal_session"], outcome)
        settled += 1
    return settled, failed


def _by_date(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    grouped: Dict[str, List[float]] = {}
    for row in rows:
        grouped.setdefault(row["signal_session"], []).append(row["outcome"]["net_return_pct"])
    return {date: statistics.mean(values) for date, values in grouped.items()}


def paired_vs_baseline(
    cohort: List[Dict[str, Any]], baseline: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Per-date mean of `cohort` minus per-date mean of `baseline`, dates weighted equally.

    Positive favours the cohort. Dates are overlapping holds, so the t value is
    a rough guide, not a test statistic to trust at face value.
    """
    mine, base = _by_date(cohort), _by_date(baseline)
    deltas = [mine[date] - base[date] for date in mine if date in base]
    result: Dict[str, Any] = {
        "dates": len(deltas), "mean_delta_pp": None, "se_pp": None,
        "better_pct": None, "t": None,
    }
    if not deltas:
        return result
    mean = statistics.mean(deltas)
    result["mean_delta_pp"] = mean
    result["better_pct"] = sum(delta > 0 for delta in deltas) / len(deltas) * 100.0
    if len(deltas) >= 2:
        spread = statistics.stdev(deltas)
        result["se_pp"] = spread / math.sqrt(len(deltas))
        if spread > 0:
            result["t"] = mean / result["se_pp"]
    return result


def _cohort_line(label: str, rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return f"    {label}: no completed signals yet"
    returns = [row["outcome"]["net_return_pct"] for row in rows]
    excess = [
        row["outcome"]["excess_return_pct"] for row in rows
        if row["outcome"]["excess_return_pct"] is not None
    ]
    text = (
        f"    {label}: {len(rows)} signals · avg net {statistics.mean(returns):+.2f}%"
        f" · hit rate {sum(value > 0 for value in returns) / len(returns) * 100:.0f}%"
    )
    if excess:
        text += f" · avg excess {statistics.mean(excess):+.2f}% ({len(excess)} matched)"
    return text


def _delta_line(label: str, paired: Dict[str, Any]) -> str:
    if not paired["dates"]:
        return f"    {label}: no dates with both groups yet"
    text = f"    {label}: {paired['mean_delta_pp']:+.2f} pp over {paired['dates']} dates"
    if paired["se_pp"] is not None:
        text += f" (±{paired['se_pp']:.2f} se, better on {paired['better_pct']:.0f}%"
        if paired["t"] is not None:
            text += f", t {paired['t']:+.1f}"
        text += ")"
    return text


def build_forward_test_report(today: Optional[datetime] = None) -> str:
    lines = ["<b>Forward Test (live signals, out of sample)</b>", ""]
    try:
        settled, failed = settle_pending(today)
        rows = history_store.TRADE_HISTORY.archive_rows()
    except Exception as exc:
        SYS_LOG.error("forward_test_failed detail=%s", exc)
        return "\n".join(lines + ["Signal archive unavailable. Check bot logs."])
    if not rows:
        return "\n".join(lines + [
            "No signals archived yet. Every screened symbol is now recorded each session "
            "(kept permanently); outcomes appear once the holding period has passed.",
        ])

    rule = history_store.TradeHistoryStore.current_rule_id()
    current = [row for row in rows if row["rule_id"] == rule]
    other_rules = len(rows) - len(current)
    done = []
    for row in current:
        if row["outcome_json"]:
            done.append({**row, "outcome": json.loads(row["outcome_json"])})
    pending = len(current) - len(done)
    dates = sorted({row["signal_session"] for row in current})
    lines.append(
        f"Rule {escape(rule)} · {len(current)} archived signals over {len(dates)} sessions"
        + (f" ({dates[0]} to {dates[-1]})" if dates else "")
    )
    lines.append(
        f"{len(done)} completed · {pending} pending"
        + (f" · {failed} unavailable" if failed else "")
        + (f" · {other_rules} recorded under earlier rules (excluded)" if other_rules else "")
        + (f" · {settled} settled just now" if settled else "")
    )
    lines.append("")

    qualified = [row for row in done if row["qualified"]]
    top = [row for row in qualified if row["rank"] is not None and row["rank"] <= TOP_N]
    lines.append("<b>Outcomes</b>")
    lines.append(_cohort_line("Every screened symbol (baseline)", done))
    lines.append(_cohort_line("Qualified", qualified))
    lines.append(_cohort_line(f"Top {TOP_N} as delivered", top))
    lines.append("")
    lines.append("<b>Paired by signal date vs. baseline</b>")
    qualified_paired = paired_vs_baseline(qualified, done)
    lines.append(_delta_line("Qualified", qualified_paired))
    lines.append(_delta_line(f"Top {TOP_N}", paired_vs_baseline(top, done)))
    lines.append("")

    if qualified_paired["dates"] < MIN_PAIRED_DATES:
        lines.append(
            f"<b>Not enough data to conclude.</b> {qualified_paired['dates']} paired dates so far; "
            f"read nothing into these numbers before about {MIN_PAIRED_DATES}."
        )
    else:
        lines.append(
            "Judge the paired difference against its standard error, not the raw averages."
        )
    lines.append("")
    lines.append(
        "<i>Signals are written when screened and never rewritten; entry is the next session's "
        f"open, exit {SETTINGS.trade_outcome_lookback_days} sessions on, costs as at "
        "recording. The baseline is every symbol the bot screened that day, which comes from "
        "its watchlist plus mover discovery, not the whole market, and is not liquidity-filtered. "
        "Overlapping holds are not independent; averages are not portfolio returns.</i>"
    )
    return "\n".join(lines)
