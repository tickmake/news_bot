"""Backtest runner and the /performance and /diagnostics reports."""

import csv
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from newsbot import market_data, screener
from newsbot.backtest import _summarize_backtest_trades, _walk_forward_backtest
from newsbot.benchmarks import _backtest_benchmark_for
from newsbot.config import SETTINGS
from newsbot.indicators import _drop_in_progress_bar
from newsbot.logsetup import APP_LOG, SYS_LOG


def _evaluation_stock_universe() -> Tuple[str, Dict[str, Dict[str, str]]]:
    preset = SETTINGS.trade_backtest_universe.strip()
    if preset == "configured":
        return preset, {
            symbol: {"symbol": symbol, "name": label, "market": "configured", "sector": "unclassified"}
            for label, symbol in screener._configured_stock_watchlist()
        }
    if preset != "broad-v1":
        raise ValueError("TRADE_BACKTEST_UNIVERSE must be configured or broad-v1")
    # The basket ships beside the entry point, one directory above this package.
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evaluation_universe.csv"
    )
    with open(path, newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ["symbol", "name", "market", "sector"]:
            raise ValueError("invalid evaluation universe columns")
        stocks = {}
        for row in reader:
            if set(row) != set(reader.fieldnames) or any(
                not isinstance(value, str) or not value.strip() for value in row.values()
            ):
                raise ValueError("empty evaluation universe field")
            row = {key: value.strip() for key, value in row.items()}
            symbol = row["symbol"]
            if symbol in stocks:
                raise ValueError(f"duplicate evaluation stock: {symbol}")
            stocks[symbol] = row
    if not stocks:
        raise ValueError("empty evaluation universe")
    return preset, stocks


def _run_walk_forward_backtest(
    horizon_sessions: Optional[int] = None,
) -> Dict[str, Any]:
    """Walk-forward test the current gates/ranking on the configured universe."""
    horizon = (
        horizon_sessions
        if horizon_sessions is not None
        else SETTINGS.trade_outcome_lookback_days
    )
    if horizon < 1:
        APP_LOG.error("backtest_invalid_horizon value=%s", horizon)
        return {"horizon_sessions": horizon, "trade_count": 0, "error": "invalid_horizon"}
    if (
        not math.isfinite(SETTINGS.trade_backtest_cost_bps)
        or SETTINGS.trade_backtest_cost_bps < 0
    ):
        APP_LOG.error("backtest_invalid_cost value=%s", SETTINGS.trade_backtest_cost_bps)
        return {
            "horizon_sessions": horizon,
            "trade_count": 0,
            "error": "invalid_cost",
        }
    if (
        not math.isfinite(SETTINGS.trade_backtest_min_avg_volume)
        or SETTINGS.trade_backtest_min_avg_volume < 0
    ):
        APP_LOG.error("backtest_invalid_liquidity value=%s", SETTINGS.trade_backtest_min_avg_volume)
        return {"horizon_sessions": horizon, "trade_count": 0, "error": "invalid_liquidity"}
    holdout_start = SETTINGS.trade_backtest_holdout_start.strip()
    if holdout_start:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", holdout_start):
                raise ValueError("expected YYYY-MM-DD")
            datetime.strptime(holdout_start, "%Y-%m-%d")
        except ValueError as exc:
            APP_LOG.error("backtest_invalid_holdout value=%s detail=%s", holdout_start, exc)
            return {"horizon_sessions": horizon, "trade_count": 0, "error": "invalid_holdout"}
    try:
        universe_name, universe = _evaluation_stock_universe()
    except (OSError, ValueError, csv.Error) as exc:
        APP_LOG.error("backtest_universe_invalid detail=%s", exc)
        return {"horizon_sessions": horizon, "trade_count": 0, "error": "invalid_universe"}
    if SETTINGS.trade_backtest_universe_max < 1:
        APP_LOG.error("backtest_universe_limit_invalid value=%s", SETTINGS.trade_backtest_universe_max)
        return {"horizon_sessions": horizon, "trade_count": 0, "error": "invalid_universe"}
    symbols = list(universe)
    if not symbols:
        APP_LOG.warning("backtest_empty_stock_universe")
        return {"horizon_sessions": horizon, "trade_count": 0, "error": "empty_universe"}
    configured_symbols = len(symbols)
    if len(symbols) > SETTINGS.trade_backtest_universe_max:
        APP_LOG.warning(
            "backtest_universe_truncated configured=%s tested=%s",
            len(symbols),
            SETTINGS.trade_backtest_universe_max,
        )
        symbols = symbols[:SETTINGS.trade_backtest_universe_max]

    benchmark_map = {symbol: _backtest_benchmark_for(symbol) for symbol in symbols}
    requested = set(symbols)
    requested.update(benchmark for benchmark in benchmark_map.values() if benchmark)
    histories: Dict[str, Any] = {}
    fetch_failures = 0

    def fetch(symbol: str) -> Tuple[str, Any]:
        try:
            history = market_data._fetch_ticker_history(
                symbol, period=SETTINGS.trade_backtest_period, interval="1d"
            )
            return symbol, _drop_in_progress_bar(history)
        except Exception as exc:
            SYS_LOG.warning("backtest_history_failed symbol=%s detail=%s", symbol, exc)
            return symbol, None

    workers = max(1, min(SETTINGS.trade_fetch_workers, len(requested) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, symbol) for symbol in sorted(requested)]
        for future in as_completed(futures):
            symbol, history = future.result()
            if history is None or len(history) == 0:
                fetch_failures += 1
            else:
                histories[symbol] = history

    result = _walk_forward_backtest(
        {symbol: histories[symbol] for symbol in symbols if symbol in histories},
        {
            benchmark: histories[benchmark]
            for benchmark in set(benchmark_map.values())
            if benchmark and benchmark in histories
        },
        horizon_sessions=horizon,
        top_n=5,
        cost_bps_per_side=SETTINGS.trade_backtest_cost_bps,
        holdout_start=holdout_start or None,
        min_avg_volume=SETTINGS.trade_backtest_min_avg_volume,
    )
    result.update(
        {
            "horizon_sessions": horizon,
            "period": SETTINGS.trade_backtest_period,
            "configured_symbols": configured_symbols,
            "tested_symbols": len(symbols),
            "omitted_symbols": configured_symbols - len(symbols),
            "fetch_failures": fetch_failures,
            "min_avg_volume": SETTINGS.trade_backtest_min_avg_volume,
            "cost_bps_per_side": SETTINGS.trade_backtest_cost_bps,
            "universe_name": universe_name,
            "stock_fetch_failures": sorted(symbol for symbol in symbols if symbol not in histories),
            "benchmark_fetch_failures": sorted(
                benchmark for benchmark in set(benchmark_map.values())
                if benchmark and benchmark not in histories
            ),
            "unusable_stocks": sorted(
                set(symbols).intersection(histories).difference(result["usable_stock_symbols"])
            ),
            "coverage": {
                dimension: {
                    group: {
                        "configured": sum(stock[dimension] == group for stock in universe.values()),
                        "tested": sum(universe[symbol][dimension] == group for symbol in symbols),
                        "usable": sum(
                            universe[symbol][dimension] == group
                            for symbol in result["usable_stock_symbols"]
                        ),
                    }
                    for group in sorted({stock[dimension] for stock in universe.values()})
                }
                for dimension in ("market", "sector")
            },
        }
    )
    for cohort in ("development", "holdout"):
        rows = [trade for trade in result["all_trades"] if trade["cohort"] == cohort]
        for dimension in ("market", "sector"):
            result["cohorts"][cohort]["by_universe_" + dimension] = {
                group: {
                    key: _summarize_backtest_trades([
                        trade for trade in rows
                        if universe[trade["symbol"]][dimension] == group
                        and (key == "all" or trade["selected"])
                    ])
                    for key in ("all", "top")
                }
                for group in result["coverage"][dimension]
            }
    return result


def _render_evaluation_coverage(summary: Dict[str, Any]) -> List[str]:
    lines = [
        f"Evaluation universe: {escape(summary.get('universe_name', 'configured'))}. "
        "Live discovery/watchlists unchanged."
    ]
    for dimension, groups in summary.get("coverage", {}).items():
        lines.append(f"<b>Universe {dimension} coverage (usable/tested/configured)</b>")
        lines.extend(
            f"    {escape(group)}: {counts['usable']}/{counts['tested']}/{counts['configured']}"
            for group, counts in groups.items()
        )
    for key, label in (
        ("stock_fetch_failures", "Missing stock histories"),
        ("unusable_stocks", "Unusable stock histories"),
        ("benchmark_fetch_failures", "Missing benchmarks"),
    ):
        if summary.get(key):
            lines.append(label + ": " + escape(", ".join(summary[key])))
    return lines


def _render_backtest_comparison(comparison: Dict[str, Any], top_n: int) -> List[str]:
    def percentage(value: Optional[float]) -> str:
        return f"{value:+.2f}%" if value is not None else "n/a"

    lines: List[str] = []
    for key, label in (("all", "All qualifiers"), ("top", f"Top {top_n}")):
        stats = comparison[key]
        count = stats["trade_count"]
        if not count:
            lines.append(f"    {label}: no completed trades.")
            continue
        lines.extend([
            f"    <b>{label}</b>: {count} trades / {stats['signal_days']} signal dates / "
            f"{stats['symbol_count']} stocks",
            f"    signals {stats['signal_start']} to {stats['signal_end']}",
            f"    avg net {percentage(stats['avg_net_return_pct'])} · "
            f"median {percentage(stats['median_net_return_pct'])} · "
            f"hit rate {stats['hit_rate_pct']:.0f}%",
            f"    avg win {percentage(stats['avg_win_pct'])} · "
            f"avg loss {percentage(stats['avg_loss_pct'])} · "
            f"worst return {percentage(stats['worst_return_pct'])}",
            f"    avg adverse move {percentage(stats['avg_adverse_excursion_pct'])} · "
            f"worst adverse move {percentage(stats['worst_adverse_excursion_pct'])}",
            f"    benchmark coverage: {stats['benchmark_count']}/{count} · "
            f"matched avg net {percentage(stats['avg_matched_net_return_pct'])} · "
            f"benchmark {percentage(stats['avg_benchmark_return_pct'])} · "
            f"excess {percentage(stats['avg_excess_return_pct'])}",
        ])
    return lines


def build_trade_diagnostics_report() -> str:
    summary = _run_walk_forward_backtest()
    lines = ["<b>Signal diagnostics - historical rules, not delivered trades</b>"]
    if summary.get("error"):
        lines.append(
            f"Diagnostics unavailable: {escape(summary['error'])}. "
            "Check stock universe, backtest settings, and bot logs."
        )
        return "\n".join(lines)

    def percentage(value: Optional[float], unit: str = "%") -> str:
        return f"{value:+.2f}{unit}" if value is not None else "n/a"

    lines.extend([
        f"{summary['horizon_sessions']}-session hold, "
        f"{summary['cost_bps_per_side']:g} bps/side; holdout starts {summary['holdout_start']}.",
        f"Stocks usable/tested: {summary['usable_symbols']}/{summary['tested_symbols']}; "
        f"fetch failures {summary['fetch_failures']}; omitted {summary['omitted_symbols']}.",
        f"Pending {summary['pending_outcomes']}; purged {summary['purged_outcomes']}; "
        f"liquidity rejected {summary['liquidity_rejected']}.",
        f"Volume confirmed means ratio >= {SETTINGS.trade_min_volume_ratio:g}.",
        "Slices cover ALL completed liquid qualifiers, not only selected picks.",
        "Positive paired delta favors selected picks; dates have equal weight. "
        "Benchmark pairs require coverage for every qualifier on that date.",
    ])
    lines.extend(_render_evaluation_coverage(summary))
    for cohort, label in (("development", "Development"), ("holdout", "Holdout (exploratory if inspected)")):
        diagnostics = summary["diagnostics"][cohort]
        paired = diagnostics["paired_ranking"]
        lines.extend([
            "",
            f"<b>{label}</b>",
            f"Top {summary['top_n']} vs non-selected on {paired['paired_dates']} paired dates: "
            f"mean net delta {percentage(paired['avg_net_delta_pct'], ' pp')}, "
            f"median {percentage(paired['median_net_delta_pct'], ' pp')}; "
            f"selected better {percentage(paired['selected_better_pct'])}.",
            f"Benchmark paired dates {paired['benchmark_paired_dates']}: "
            f"mean excess delta {percentage(paired['avg_excess_delta_pct'], ' pp')}; "
            f"excluded observed dates: {paired['incomplete_dates']} incomplete, "
            f"{paired['no_comparison_dates']} without both groups.",
        ])
        if not diagnostics["groups"]:
            lines.append("No completed qualifiers.")
        for dimension, buckets in diagnostics["groups"].items():
            lines.append(f"<b>{escape(dimension)}</b>")
            for bucket, stats in buckets.items():
                lines.extend([
                    f"  {escape(bucket)}: {stats['trade_count']} signals / "
                    f"{stats['signal_days']} dates / {stats['symbol_count']} stocks; "
                    f"net {percentage(stats['avg_net_return_pct'])}, "
                    f"median {percentage(stats['median_net_return_pct'])}, "
                    f"hit {percentage(stats['hit_rate_pct'])}.",
                    f"  win/loss {percentage(stats['avg_win_pct'])}/"
                    f"{percentage(stats['avg_loss_pct'])}; "
                    f"adverse avg/worst {percentage(stats['avg_adverse_excursion_pct'])}/"
                    f"{percentage(stats['worst_adverse_excursion_pct'])}; "
                    f"excess {percentage(stats['avg_excess_return_pct'])} "
                    f"(coverage {stats['benchmark_count']}/{stats['trade_count']}).",
                ])
    lines.extend([
        "",
        "<i>Fixed broad buckets, no threshold optimization. Slices overlap; "
        "signals and neighboring dates are not independent. Differences are descriptive, "
        "not proof an indicator causes profit. Current universe has selection/survivorship "
        "bias; missing data affects coverage. An inspected holdout is not untouched "
        "validation. Live rules are unchanged.</i>",
    ])
    return "\n".join(lines)


def _render_baseline(summary: Dict[str, Any]) -> List[str]:
    """Compare the screener with holding any liquid stock, ungated."""
    def percentage(value: Optional[float]) -> str:
        return f"{value:+.2f}%" if value is not None else "n/a"

    def delta(pairing: Dict[str, Any]) -> str:
        if not pairing["paired_dates"]:
            return "no paired dates"
        return (
            f"{pairing['mean_delta_pp']:+.2f} pp across {pairing['paired_dates']} dates, "
            f"better on {pairing['better_pct']:.0f}%"
        )

    lines = [
        "<b>Baseline: every liquid stock, no gates</b>",
        "Same entry, exit, costs and holdout purge. Dates weighted equally; "
        "positive favours the screener.",
    ]
    for key, label in (("development", "Development"), ("holdout", "Reserved holdout")):
        block = summary["baseline"][key]
        stats = block["stats"]
        if not stats["trade_count"]:
            lines.append(f"    {label}: no completed baseline trades.")
            continue
        lines.extend([
            f"    <b>{label}</b>: {stats['trade_count']} stock-days / "
            f"{stats['signal_days']} dates / {stats['symbol_count']} stocks",
            f"    avg net {percentage(stats['avg_net_return_pct'])} · "
            f"median {percentage(stats['median_net_return_pct'])} · "
            f"hit rate {stats['hit_rate_pct']:.0f}% · "
            f"excess {percentage(stats['avg_excess_return_pct'])}",
            f"    qualifiers vs baseline: {delta(block['qualifiers_vs_baseline'])}",
            f"    top {summary['top_n']} vs baseline: {delta(block['top_vs_baseline'])}",
        ])
    return lines


def build_trade_performance_report(
    horizon_sessions: Optional[int] = None,
) -> str:
    """Report fixed-horizon, next-open walk-forward results for current rules."""
    summary = _run_walk_forward_backtest(horizon_sessions)
    horizon = summary.get("horizon_sessions", SETTINGS.trade_outcome_lookback_days)
    lines = [
        "<b>📊 Walk-Forward Trade Screener Performance</b>",
        f"<i>Current rules · next-session open entry · {horizon}-session hold</i>",
        "",
    ]
    if summary.get("error") == "empty_universe":
        lines.append(
            "No configured symbols are available to test. Add stocks to the "
            "USA_STOCK_UNIVERSE, INDIA_STOCK_UNIVERSE, NORWAY_STOCK_UNIVERSE, "
            "or EU_STOCK_UNIVERSE settings."
        )
        lines.append("")
        return "\n".join(lines) + "\n\n"

    if summary.get("error") in (
        "invalid_horizon", "invalid_cost", "invalid_liquidity", "invalid_holdout", "invalid_universe",
    ):
        lines.append(
            "Backtest configuration is invalid. The holding horizon must be at "
            "least one session, cost and minimum volume must be finite and nonnegative, "
            "holdout start must be blank or a valid YYYY-MM-DD date, and universe must "
            "be configured or broad-v1 with a positive evaluation limit. Check bot logs."
        )
        lines.append("")
        return "\n".join(lines) + "\n\n"

    lines.append(
        f"Universe: {summary['usable_symbols']} usable of {summary['tested_symbols']} "
        f"tested stocks; {summary['fetch_failures']} data fetch failure(s). Fund lists excluded."
    )
    lines.extend(_render_evaluation_coverage(summary))
    if summary.get("omitted_symbols", 0):
        lines.append(
            f"Only {summary['tested_symbols']} of {summary['configured_symbols']} "
            "configured symbols fit the backtest universe limit."
        )
    lines.extend([
        f"Holdout starts: {summary['holdout_start'] or 'unavailable'} "
        f"({summary['split_mode']}) · history {escape(summary['period'])}",
        f"Liquidity: prior 20-session average ≥ {summary['min_avg_volume']:,.0f} shares/day · "
        f"cost {summary['cost_bps_per_side']:.1f} bps per side",
        f"Excluded outcomes: {summary['purged_outcomes']} cross the split; "
        f"{summary['pending_outcomes']} lack a complete hold. "
        f"{summary['liquidity_rejected']} qualifying signals failed liquidity.",
        "",
    ])
    for key, label in (("development", "Development"), ("holdout", "Reserved holdout")):
        comparison = summary["cohorts"][key]
        lines.append(f"<b>{label}</b>")
        lines.extend(_render_backtest_comparison(comparison, summary["top_n"]))
        lines.append("")

    lines.extend(_render_baseline(summary))
    lines.append("")

    holdout = summary["cohorts"]["holdout"]
    for dimension in ("market", "sector"):
        groups = holdout.get("by_universe_" + dimension, {})
        if summary.get("universe_name") == "broad-v1":
            lines.append(f"<b>Holdout by universe {dimension}</b>")
            for group, comparison in groups.items():
                lines.append(escape(group))
                lines.extend(_render_backtest_comparison(comparison, summary["top_n"]))
    for key, heading in (("by_market", "Holdout by benchmark market"), ("by_quarter", "Holdout by quarter")):
        lines.append(f"<b>{heading}</b>")
        groups = holdout[key]
        if not groups:
            lines.append("    No completed holdout trades.")
        for name, comparison in groups.items():
            parts = []
            for cohort_key, label in (("all", "all"), ("top", "top")):
                stats = comparison[cohort_key]
                excess = stats["avg_excess_return_pct"]
                net = stats["avg_net_return_pct"]
                net_text = f"{net:+.2f}%" if net is not None else "n/a"
                excess_text = f"{excess:+.2f}%" if excess is not None else "n/a"
                parts.append(
                    f"{label} {stats['trade_count']} trades, net {net_text}, "
                    f"excess {excess_text} ({stats['benchmark_count']} matched)"
                )
            lines.append(f"    {escape(name)}: " + " · ".join(parts))
        lines.append("")
    validation = summary["cost_validation"]
    lines.extend([
        "<b>Unchanged-rule validation: chronological blocks and cost stress</b>",
        f"Excludes {validation['quarter_crossing_excluded']} additional holds crossing "
        "calendar-quarter boundaries. Partial quarters remain labeled by date range.",
        "Same picks in every scenario; costs do not alter selection. "
        "These are descriptive blocks, not independent folds or fresh validation.",
    ])
    for scenario in validation["scenarios"]:
        lines.append(f"<b>Cost {scenario['cost_bps_per_side']:g} bps per side</b>")
        for cohort, label in (("development", "Development"), ("holdout", "Holdout (inspected = exploratory)")):
            comparison = scenario["cohorts"][cohort]
            lines.append(label)
            for quarter, block in comparison["by_quarter"].items():
                parts = []
                for key in ("all", "top"):
                    stats = block[key]
                    net, excess = stats["avg_net_return_pct"], stats["avg_excess_return_pct"]
                    net_text = f"{net:+.2f}%" if net is not None else "n/a"
                    excess_text = f"{excess:+.2f} pp" if excess is not None else "n/a"
                    parts.append(
                        f"{key}: {stats['trade_count']} signals / {stats['signal_days']} dates / "
                        f"{stats['symbol_count']} stocks, net {net_text}, excess {excess_text} "
                        f"({stats['benchmark_count']} matched)"
                    )
                dates = block["all"]
                lines.append(
                    f"    {quarter} ({dates['signal_start']} to {dates['signal_end']}): "
                    + " · ".join(parts)
                )
            if not comparison["by_quarter"]:
                lines.append("    No completed within-quarter trades.")
        lines.append("")
    if summary["split_mode"] == "automatic":
        lines.append(
            "<i>Automatic split reserves the final 25% of signal dates. Set "
            "TRADE_BACKTEST_HOLDOUT_START to the displayed date to freeze it before tuning.</i>"
        )
    lines.append("")
    lines.append(
        "<i>Do not tune on holdout results. A reserved date range is not evidence it was "
        "never inspected. Small samples and overlapping trades do not support independent "
        "confidence intervals; these averages are not portfolio returns. Today's stocks "
        "introduce survivorship/universe bias. Share volume is only a liquidity proxy, "
        "not a spread or execution guarantee. Costs are estimates; results do not predict "
        "future performance.</i>"
    )
    return "\n".join(lines) + "\n\n"
