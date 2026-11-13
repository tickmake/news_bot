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
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any, Dict, List, Optional, Tuple

import requests.exceptions
import yfinance as yf

from newsbot import history_store, market_data
from newsbot.archive_quality import (
    CLASSIFIER_VERSION,
    CLEAN,
    INVALID,
    OVERDUE_AFTER_DAYS,
    POTENTIALLY_CONTAMINATED,
    entry_quality,
    parse_recorded_at,
    static_defect,
)
from newsbot.benchmarks import _matched_benchmark_return
from newsbot.config import SETTINGS
from newsbot.exchange_hours import Exchange, exchange_for, open_at
from newsbot.history_store import ENTRY_POLICY_AFTER_RECORDED
from newsbot.indicators import _drop_unfinished_bar
from newsbot.logsetup import APP_LOG, SYS_LOG

# Entry sessions needed before the paired comparison is worth reading at all.
# The backtest's per-variant standard error was ~0.1 pp over ~400 dates; far
# fewer cannot separate a real edge from noise.
MIN_PAIRED_DATES = 60
TOP_N = 5


def _session_dates(row: Dict[str, Any], history: Any) -> List[str]:
    """The history's session dates, once it is known to cover the signal."""
    if history is None or len(history) == 0:
        raise ValueError("missing stock history")
    if int(row["horizon_sessions"]) < 1:
        raise ValueError("invalid holding horizon")
    dates = history.index.strftime("%Y-%m-%d").tolist()
    if dates != sorted(set(dates)):
        raise ValueError("unordered or duplicate stock sessions")
    if dates[0] > row["signal_session"]:
        raise ValueError("history does not cover the signal session")
    return dates


def _require_exchange_zone(stamp: Any, exchange: Exchange) -> None:
    """Raise unless a bar's timestamp is in the exchange's own timezone.

    Offsets are compared, not names: a zone that agrees with the exchange's on
    this date gives the same session date and the same opening instant.
    """
    local = stamp.to_pydatetime()
    if local.utcoffset() != local.replace(tzinfo=exchange.zone).utcoffset():
        raise ValueError(
            f"history timezone {local.tzinfo} does not match the exchange's "
            f"{exchange.zone.key} on {local:%Y-%m-%d}"
        )


def _entry_after_recording(row: Dict[str, Any], history: Any, dates: List[str]) -> Optional[int]:
    """Position of the first session whose open is strictly after `recorded_at`.

    The signal exists from the moment it was recorded. A session that had
    already opened by then could not have been traded, however the signal is
    dated, so its open is never used.
    """
    exchange = exchange_for(row["symbol"])
    if exchange is None:
        raise ValueError("unsupported exchange")
    recorded = parse_recorded_at(row["recorded_at"])
    if getattr(history.index, "tz", None) is None:
        # Never fall back to the host's clock: that is how an entry ends up a day off.
        raise ValueError("history has no timezone")
    for position, day in enumerate(dates):
        if day < row["signal_session"]:
            continue
        _require_exchange_zone(history.index[position], exchange)
        if day > row["signal_session"] and open_at(exchange, date.fromisoformat(day)) > recorded:
            return position
    return None


def entry_position(row: Dict[str, Any], history: Any) -> Optional[int]:
    """Where in `history` this signal enters, or None when that session has not come yet.

    Rows recorded before the entry fix carry no policy and keep the rule they
    were recorded under (first session after the signal date), so their series
    stays what it always was. Raises ValueError for data that cannot be trusted.
    """
    dates = _session_dates(row, history)
    policy = row.get("entry_policy")
    if policy is None:
        eligible = [position for position, day in enumerate(dates) if day > row["signal_session"]]
        return eligible[0] if eligible else None
    if policy != ENTRY_POLICY_AFTER_RECORDED:
        raise ValueError(f"unknown entry policy {policy!r}")
    return _entry_after_recording(row, history, dates)


def forward_outcome(
    row: Dict[str, Any], history: Any, benchmark: Any
) -> Optional[Dict[str, Any]]:
    """Enter at the open `entry_position` picks, exit at the close `horizon` sessions on.

    None means "not finished yet". Raises ValueError for data that cannot be
    trusted, so the row stays pending instead of recording a wrong number.
    """
    entry = entry_position(row, history)
    if entry is None:
        return None
    horizon = int(row["horizon_sessions"])
    dates = history.index.strftime("%Y-%m-%d").tolist()
    exit_position = entry + horizon - 1
    if exit_position >= len(history):
        return None
    prices = history.iloc[entry:exit_position + 1][["Open", "Close"]]
    bad = prices[
        prices.isna().any(axis=1)
        | prices.isin([float("inf"), float("-inf")]).any(axis=1)
        | (prices <= 0).any(axis=1)
    ]
    if len(bad):
        # Name the bad bars: the nightly runs hit these and the afternoon
        # reruns do not, so the log line is the only evidence of why (#89).
        detail = "; ".join(
            f"{stamp:%Y-%m-%d} open={row['Open']} close={row['Close']}" for stamp, row in bad.iterrows()
        )
        raise ValueError(f"invalid prices during holding period: {detail}")
    entry_price = float(history.iloc[entry]["Open"])
    exit_price = float(history.iloc[exit_position]["Close"])
    gross = (exit_price / entry_price - 1.0) * 100.0
    net = gross - 2.0 * float(row["cost_bps_per_side"]) / 100.0
    benchmark_return = _matched_benchmark_return(benchmark, dates[entry], dates[exit_position])
    outcome = {
        "entry_session": dates[entry],
        "exit_session": dates[exit_position],
        "gross_return_pct": gross,
        "net_return_pct": net,
        "benchmark_return_pct": benchmark_return,
        "excess_return_pct": net - benchmark_return if benchmark_return is not None else None,
    }
    policy = row.get("entry_policy")
    if policy is not None:
        exchange = exchange_for(row["symbol"])
        _require_exchange_zone(history.index[exit_position], exchange)
        # Stored so "no price earlier than recorded_at" can be checked from the
        # row alone, and so an outcome written by an older build (which has
        # neither key) is recognisable.
        outcome["entry_policy"] = policy
        outcome["entry_open_at"] = open_at(exchange, date.fromisoformat(dates[entry])).isoformat()
    return outcome


def _might_be_finished(row: Dict[str, Any], today: datetime) -> bool:
    """Cheap pre-check so history is only fetched for rows that could complete."""
    try:
        signal = datetime.strptime(row["signal_session"], "%Y-%m-%d")
    except ValueError:
        return False
    # `horizon` trading sessions after the signal needs at least that many
    # calendar days, and the last bar must be a completed one.
    return today - signal >= timedelta(days=int(row["horizon_sessions"]) + 1)


def _classify_without_prices(store: Any, rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], str]:
    """Class every row that can be judged from what is already stored.

    A permanent defect is written as invalid at once, so the row never sits
    in "awaiting entry" waiting for a bar that cannot come. A finished row's
    entry date is in its outcome. Neither needs a fetch. Returns the class of
    every row known so far under this classifier version.
    """
    known = {key: entry["quality"] for key, entry in store.quality_rows(CLASSIFIER_VERSION).items()}
    fresh = []
    for row in rows:
        key = (row["symbol"], row["signal_session"])
        if key in known:
            continue
        defect = static_defect(row)
        if defect is not None:
            verdict: Tuple[str, str, Optional[str]] = (INVALID, defect, None)
        elif row["outcome_json"]:
            entry_day = date.fromisoformat(json.loads(row["outcome_json"])["entry_session"])
            verdict = entry_quality(row, entry_day)
        else:
            continue
        known[key] = verdict[0]
        fresh.append((*key, *verdict))
    store.classify(fresh, CLASSIFIER_VERSION)
    return known


def settle_pending(today: Optional[datetime] = None) -> Tuple[int, int]:
    """Classify rows and fill in outcomes that have become computable. Returns (settled, failed).

    Classes are written under this build's CLASSIFIER_VERSION. Which version
    the reports read is a separate, deliberate step
    (`TradeHistoryStore.activate_classifier`); this job never moves it.
    """
    today = today or datetime.now()
    store = history_store.TRADE_HISTORY
    rows = store.archive_rows()
    known = _classify_without_prices(store, rows)
    cache: Dict[str, Any] = {}

    def history_for(symbol: Optional[str]) -> Any:
        if not symbol:
            return None
        if symbol not in cache:
            try:
                cache[symbol] = _drop_unfinished_bar(
                    market_data._fetch_ticker_history(symbol, period="2y")
                )
            except (requests.exceptions.RequestException, yf.exceptions.YFException,
                    ValueError, RuntimeError, OSError) as exc:
                SYS_LOG.warning("forward_history_failed symbol=%s detail=%s", symbol, exc)
                cache[symbol] = None
        return cache[symbol]

    settled = failed = 0
    for row in rows:
        if row["outcome_json"]:
            continue
        key = (row["symbol"], row["signal_session"])
        quality = known.get(key)
        if row.get("entry_policy") and quality == INVALID:
            # A permanent defect: fetching again cannot fix it, and counting it
            # as a failure would fail this job on every run for ever.
            continue
        finished = _might_be_finished(row, today)
        if quality is not None and not finished:
            continue
        history = history_for(row["symbol"])
        if quality is None:
            # Awaiting entry: the class is known as soon as the entry bar exists.
            try:
                entry = entry_position(row, history)
            except (ValueError, KeyError, TypeError) as exc:
                # A fault of this fetch, not a fact about the row: write nothing,
                # so the row is tried again instead of excluded for good.
                if finished:
                    SYS_LOG.warning("forward_outcome_failed symbol=%s detail=%s", row["symbol"], exc)
                    failed += 1
                else:
                    SYS_LOG.info("forward_entry_unknown symbol=%s detail=%s", row["symbol"], exc)
                continue
            if entry is None:
                continue
            verdict = entry_quality(row, history.index[entry].date())
            store.classify([(*key, *verdict)], CLASSIFIER_VERSION)
            known[key] = quality = verdict[0]
            if row.get("entry_policy") and quality == INVALID:
                SYS_LOG.error(
                    "forward_entry_not_after_recording symbol=%s signal=%s", row["symbol"], row["signal_session"]
                )
                continue
        if not finished:
            continue
        try:
            outcome = forward_outcome(row, history, history_for(row["benchmark_symbol"]))
        except (ValueError, KeyError, TypeError) as exc:
            SYS_LOG.warning("forward_outcome_failed symbol=%s detail=%s", row["symbol"], exc)
            failed += 1
            continue
        if outcome is None:
            continue
        store.complete_archive_row(row["symbol"], row["signal_session"], outcome)
        settled += 1
    return settled, failed


def prepare_archive() -> bool:
    """Open the archive at startup so its schema and view exist. Never raises.

    The dashboard reads the `signal_archive_evaluated` view. Without this it
    would find nothing after a deploy until the first scheduled job happened
    to open the database, which can be hours.
    """
    try:
        history_store.TRADE_HISTORY.active_classifier_version()
    except Exception as exc:
        SYS_LOG.error("forward_archive_prepare_failed detail=%s", exc)
        return False
    return True


def job_settle_forward_test() -> bool:
    """Settle every outcome whose holding period has passed. Never raises.

    `build_forward_test_report` settles lazily, so without this job the archive
    only advances when someone asks for the report. Late is the smaller problem:
    settling needs price history covering the signal session, and a symbol that
    stops returning it -- renamed, delisted, dropped by the provider -- can
    never be settled afterwards. Every row left pending is a permanent hole in
    the only out-of-sample record this project keeps, and the holes land on
    exactly the names whose outcomes were worst, so the sample does not just
    shrink, it flatters itself.
    """
    try:
        settled, failed = settle_pending()
    except Exception as exc:
        SYS_LOG.error("forward_settle_failed detail=%s", exc)
        return False
    if failed:
        # Failed rows stay pending and are retried the next night.
        SYS_LOG.warning("forward_settle settled=%d retry_tomorrow=%d", settled, failed)
    elif settled:
        APP_LOG.info("forward_settle settled=%d", settled)
    return not failed


def _by_date(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """Average result per entry session.

    Keyed by the session a row entered on, not the date its signal carries:
    two signals with the same date can be recorded hours apart and enter on
    different days, and those are different holding periods.
    """
    grouped: Dict[str, List[float]] = {}
    for row in rows:
        grouped.setdefault(row["entry_session"], []).append(row["outcome"]["net_return_pct"])
    return {session: statistics.mean(values) for session, values in grouped.items()}


def paired_vs_baseline(
    cohort: List[Dict[str, Any]], baseline: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Per-entry-session mean of `cohort` minus that of `baseline`, sessions weighted equally.

    Positive favours the cohort. Sessions are overlapping holds, so the t value
    is a rough guide, not a test statistic to trust at face value.
    """
    mine, base = _by_date(cohort), _by_date(baseline)
    deltas = [mine[session] - base[session] for session in mine if session in base]
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
        return f"    {label}: no entry sessions with both groups yet"
    text = f"    {label}: {paired['mean_delta_pp']:+.2f} pp over {paired['dates']} entry sessions"
    if paired["se_pp"] is not None:
        text += f" (±{paired['se_pp']:.2f} se, better on {paired['better_pct']:.0f}%"
        if paired["t"] is not None:
            text += f", t {paired['t']:+.1f}"
        text += ")"
    return text


def _is_overdue(row: Dict[str, Any], now: datetime) -> bool:
    """Still without an entry session long after any exchange would have opened."""
    try:
        recorded = parse_recorded_at(row["recorded_at"])
    except ValueError:
        return False
    return now - recorded > timedelta(days=OVERDUE_AFTER_DAYS)


def _counts_line(rows: List[Dict[str, Any]], now: datetime) -> str:
    """Where every row stands, so nothing is left out of view by being left out of the averages."""
    clean = [row for row in rows if row["quality"] == CLEAN]
    finished = sum(1 for row in clean if row["settled"])
    parts = [f"{finished} clean and finished", f"{len(clean) - finished} clean and in hold"]
    contaminated = sum(1 for row in rows if row["quality"] == POTENTIALLY_CONTAMINATED)
    if contaminated:
        parts.append(f"{contaminated} potentially contaminated")
    reasons: Dict[str, int] = {}
    for row in rows:
        if row["quality"] == INVALID:
            reasons[row["quality_reason"]] = reasons.get(row["quality_reason"], 0) + 1
    if reasons:
        detail = ", ".join(f"{escape(str(reason))} {count}" for reason, count in sorted(reasons.items()))
        parts.append(f"{sum(reasons.values())} invalid ({detail})")
    awaiting = [row for row in rows if row["quality"] == "awaiting_entry"]
    parts.append(f"{len(awaiting)} awaiting entry")
    text = "    " + " · ".join(parts)
    overdue = sorted({row["symbol"] for row in awaiting if _is_overdue(row, now)})
    if overdue:
        shown = ", ".join(escape(symbol) for symbol in overdue[:8])
        more = f" and {len(overdue) - 8} more" if len(overdue) > 8 else ""
        text += (
            f"\n    <b>Overdue</b> (no entry session after {OVERDUE_AFTER_DAYS} days; "
            f"check the symbol and its price data): {shown}{more}"
        )
    return text


def _finished_clean(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The only rows any average may use: clean, and with a result."""
    return [
        {**row, "outcome": json.loads(row["outcome_json"])}
        for row in rows if row["quality"] == CLEAN and row["settled"]
    ]


def _performance_lines(done: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Any]]:
    """Cohort averages and the paired comparisons for one set of clean, finished rows."""
    qualified = [row for row in done if row["qualified"]]
    top = [row for row in qualified if row["rank"] is not None and row["rank"] <= TOP_N]
    qualified_paired = paired_vs_baseline(qualified, done)
    lines = [
        _cohort_line("Every screened symbol (baseline)", done),
        _cohort_line("Qualified", qualified),
        _cohort_line(f"Top {TOP_N} as delivered", top),
        "    <i>Paired by entry session vs. baseline</i>",
        _delta_line("Qualified", qualified_paired),
        _delta_line(f"Top {TOP_N}", paired_vs_baseline(top, done)),
    ]
    return lines, qualified_paired


def build_forward_test_report(today: Optional[datetime] = None) -> str:
    lines = ["<b>Forward Test (live signals, out of sample)</b>", ""]
    try:
        settled, failed = settle_pending(today)
        store = history_store.TRADE_HISTORY
        archived = len(store.archive_rows())
        rows = store.evaluated_rows()
    except Exception as exc:
        SYS_LOG.error("forward_test_failed detail=%s", exc)
        return "\n".join(lines + ["Signal archive unavailable. Check bot logs."])
    if not archived:
        return "\n".join(lines + [
            "No signals archived yet. Every screened symbol is now recorded each session "
            "(kept permanently); outcomes appear once the holding period has passed.",
        ])
    if not rows:
        # The view shows nothing without an active classifier version, and so
        # does this report: unfiltered rows are worse than no rows.
        return "\n".join(lines + [
            "<b>Classifier not active.</b> No data-quality classification is in force, "
            "so no result is shown. Check bot logs.",
        ])

    now = datetime.now(timezone.utc) if today is None else (
        today if today.tzinfo is not None else today.replace(tzinfo=timezone.utc)
    )
    rule = history_store.TradeHistoryStore.current_rule_id()
    # A different hold length is a different test, so it is never averaged in either.
    horizon = SETTINGS.trade_outcome_lookback_days
    mine = [row for row in rows if row["rule_id"] == rule]
    test = [
        row for row in mine
        if row["series"] == "clean_test" and int(row["horizon_sessions"]) == horizon
    ]
    historical = [row for row in mine if row["series"] == "historical"]
    excluded = len(rows) - len(test) - len(historical)
    lines.append(
        f"Rule {escape(rule)} · hold {horizon} sessions · entry policy "
        f"{ENTRY_POLICY_AFTER_RECORDED} · classifier v{rows[0]['classifier_version']}"
    )
    notes = (
        ([f"{failed} unavailable"] if failed else [])
        + ([f"{excluded} recorded under earlier rules or hold lengths (excluded)"] if excluded else [])
        + ([f"{settled} settled just now"] if settled else [])
    )
    if notes:
        lines.append(" · ".join(notes))
    lines.append("")

    # --- the test: only signals recorded under the after-recording policy ---
    done = _finished_clean(test)
    sessions = sorted({row["entry_session"] for row in done})
    lines.append("<b>Clean test</b> (signals recorded since the entry fix)")
    lines.append(
        f"    {len(test)} archived signals"
        + (f" · {len(done)} completed over {len(sessions)} entry sessions "
           f"({sessions[0]} to {sessions[-1]})" if sessions else " · 0 completed")
    )
    lines.append(_counts_line(test, now))
    performance, qualified_paired = _performance_lines(done)
    lines.extend(performance)
    lines.append("")
    if qualified_paired["dates"] < MIN_PAIRED_DATES:
        lines.append(
            f"<b>Not enough data to conclude.</b> {qualified_paired['dates']} paired entry sessions "
            f"so far; read nothing into these numbers before about {MIN_PAIRED_DATES}."
        )
    else:
        lines.append(
            "Judge the paired difference against its standard error, not the raw averages."
        )
    lines.append("")

    # --- before the fix: kept for audit, split by class, never part of the test ---
    if historical:
        lines.append("<b>Before the entry fix</b> (historical, not part of the test)")
        for hold in sorted({int(row["horizon_sessions"]) for row in historical}):
            group = [row for row in historical if int(row["horizon_sessions"]) == hold]
            lines.append(f"  <b>Hold {hold} sessions</b>: {len(group)} signals")
            lines.append(_counts_line(group, now))
            clean_done = _finished_clean(group)
            if clean_done:
                lines.append("    <i>Clean rows only:</i>")
                lines.extend(_performance_lines(clean_done)[0])
        lines.append("")

    lines.append(
        "<i>Signals are written when screened and never rewritten. Entry is the first open "
        "after the signal was recorded, never one that had already happened; exit "
        f"{SETTINGS.trade_outcome_lookback_days} sessions on, costs as at recording. Results are "
        "grouped by entry session. Signals recorded before the entry fix could be scored from an "
        "open that had already passed; those are counted above and never averaged. The baseline "
        "is every symbol the bot screened, which comes from its watchlist plus mover discovery, "
        "not the whole market, and is not liquidity-filtered. Overlapping holds are not "
        "independent; averages are not portfolio returns. Markets that share an entry session "
        "can exit a day apart around a local holiday. \"Top "
        f"{TOP_N}\" is by rank within each screen, so one entry session can hold more than {TOP_N}.</i>"
    )
    return "\n".join(lines)
