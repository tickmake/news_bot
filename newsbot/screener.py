"""Candidate universe, live screening and the /recommendations text."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from newsbot import config, data_health, history_store, indicators, market_data, risk as risk_analytics
from newsbot.config import SETTINGS
from newsbot.indicators import TRADE_GATES, TradeMetrics, _failed_gates
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.ranking import _rank_key
from newsbot.util import _truncate


def _configured_stock_watchlist() -> List[Tuple[str, str]]:
    """Stable equity evaluation universe; fund lists are deliberately separate."""
    entries: List[Tuple[str, str]] = []
    for suffix, mapping in (
        ("USA Stock", config.USA_STOCK_UNIVERSE),
        ("India Stock", config.INDIA_STOCK_UNIVERSE),
        ("Norway Stock", config.NORWAY_STOCK_UNIVERSE),
        ("EU Stock", config.EU_STOCK_UNIVERSE),
    ):
        for name, symbol in mapping.items():
            entries.append((f"{name} [{suffix}]", symbol))
    return entries


def _configured_watchlist() -> List[Tuple[str, str]]:
    """(label, symbol) pairs for every symbol configured via environment."""
    entries = _configured_stock_watchlist()
    for suffix, mapping in (
        ("India Fund", config.INDIA_MUTUAL_FUNDS),
        ("Norway Fund", config.NORWAY_MUTUAL_FUNDS),
    ):
        for name, symbol in mapping.items():
            entries.append((f"{name} [{suffix}]", symbol))
    return entries


def _build_candidate_universe(max_symbols: int) -> List[Tuple[str, str]]:
    """Configured watchlist, then recently-tracked symbols, then discovery.

    The previous order appended the watchlist *after* ~67 screener names and
    then truncated to the cap, so with healthy screeners no configured symbol
    was ever analysed. Whichever side loses a slot now, it is reported.

    Screener names are discovery only: they decide which symbols are looked at,
    never whether one qualifies. That decision belongs to _failed_gates, which
    reads completed-session metrics.

    The sticky middle tier exists because discovery alone made the universe
    churn almost completely from one session to the next. `/stocks` reads
    recorded history rather than live prices, so a symbol seen exactly once
    cannot meet its consistency requirement.
    """
    selected: List[Tuple[str, str]] = []
    seen: set = set()

    for label, symbol in _configured_watchlist():
        if symbol in seen:
            continue
        seen.add(symbol)
        selected.append((label, symbol))

    if len(selected) > max_symbols:
        APP_LOG.warning(
            "watchlist_truncated configured=%s analysed=%s detail=raise TRADE_UNIVERSE_MAX to analyse all",
            len(selected),
            max_symbols,
        )
        return selected[:max_symbols]

    # A full budget means no screener call at all.
    if len(selected) >= max_symbols:
        return selected

    # Hold back slots for discovery before the sticky set spends them, so a
    # long-lived tracked set can never freeze the universe and stop surfacing
    # new names.
    reserved = max(0, min(SETTINGS.trade_discovery_min_slots, max_symbols - len(selected)))
    sticky_budget = max_symbols - reserved
    for symbol in history_store.TRADE_HISTORY.recently_tracked_symbols(SETTINGS.trade_sticky_universe_days):
        if len(selected) >= sticky_budget:
            break
        if symbol in seen:
            continue
        seen.add(symbol)
        selected.append((f"{symbol} [Tracked]", symbol))

    remaining = max_symbols - len(selected)
    if remaining <= 0:
        return selected

    for label, symbol in market_data._build_live_universe(limit=remaining).items():
        if symbol in seen:
            continue
        seen.add(symbol)
        selected.append((label, symbol))
        if len(selected) >= max_symbols:
            break
    return selected


@dataclass
class ScreenOutcome:
    """What one screening run saw. Rendered as the diagnostic footer.

    Invariant: checked - no_data == len(qualified) + sum(failed_counts.values())
    when the deadline did not truncate the run.
    """

    checked: int = 0
    analysed: int = 0
    no_data: int = 0
    qualified: List[TradeMetrics] = field(default_factory=list)
    failed_counts: Dict[str, int] = field(default_factory=dict)
    deadline_hit: bool = False
    # Rejections kept in full, not just counted. `/watchlist` has to show a
    # configured symbol whatever its verdict -- the point of a watchlist is
    # seeing how your own names are doing, including the ones that are not
    # trade-worthy today -- and counts alone cannot answer that.
    rejected: List[Tuple[TradeMetrics, List[str]]] = field(default_factory=list)
    no_data_symbols: List[str] = field(default_factory=list)


def _effective_history_retention_days() -> int:
    """Keep enough recorded sessions for the SQLite streak and weekly-pick readers."""
    return max(
        SETTINGS.trade_history_retention_days,
        SETTINGS.trade_weekly_top_picks_lookback_days * 2 + 7,
        SETTINGS.trade_sticky_universe_days + 7,
    )


def _screen_universe(
    candidate_items: List[Tuple[str, str]]
) -> Tuple[ScreenOutcome, Dict[str, str]]:
    outcome = ScreenOutcome(checked=len(candidate_items))
    labels = {symbol: name for name, symbol in candidate_items}
    workers = max(1, min(SETTINGS.trade_fetch_workers, len(candidate_items) or 1))
    deadline = time.time() + max(5, SETTINGS.trade_total_deadline_seconds)

    consumed: set = set()

    def _consume(future: Any, symbol: str) -> None:
        if symbol in consumed:
            return
        consumed.add(symbol)
        try:
            metrics = future.result()
        except Exception as exc:
            SYS_LOG.debug("candidate_future_failed symbol=%s detail=%s", symbol, exc)
            metrics = None

        outcome.analysed += 1
        if metrics is None:
            outcome.no_data += 1
            outcome.no_data_symbols.append(symbol)
            return

        failed = _failed_gates(metrics)
        if failed:
            outcome.failed_counts[failed[0]] = outcome.failed_counts.get(failed[0], 0) + 1
            outcome.rejected.append((metrics, failed))
        else:
            outcome.qualified.append(metrics)
        history_store.TRADE_HISTORY.record(
            metrics, qualified=not failed, failed_gate=(failed[0] if failed else None)
        )
        history_store.TRADE_HISTORY.archive_signal(
            metrics, qualified=not failed, failed_gate=(failed[0] if failed else None)
        )

    def _expire(future_to_symbol: Dict[Any, str]) -> None:
        APP_LOG.warning(
            "trade_candidates_deadline_reached analysed=%s of=%s",
            outcome.analysed,
            outcome.checked,
        )
        outcome.deadline_hit = True
        # Drain everything else that already finished before cancelling.
        # Throwing away a completed measurement loses that symbol's
        # history row for the session, which silently breaks its
        # qualifying streak and its eligibility for /stocks and
        # /performance -- for no saving at all, since the work is done.
        for other, other_symbol in list(future_to_symbol.items()):
            if other.done():
                _consume(other, other_symbol)
        for pending in future_to_symbol:
            pending.cancel()

    # Not a `with` block: its exit is shutdown(wait=True), which waits for
    # every fetch already running, so the deadline bounded nothing but the
    # loop. Shutting down without waiting lets a hung fetch be abandoned.
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_symbol = {
            pool.submit(indicators._compute_trade_metrics, symbol): symbol
            for _, symbol in candidate_items
        }
        try:
            # The timeout also covers the case where no future ever
            # completes, which a post-result clock check cannot see.
            for future in as_completed(
                future_to_symbol, timeout=max(0.0, deadline - time.time())
            ):
                _consume(future, future_to_symbol[future])

                # Checked after the result is consumed: testing first
                # discarded work that had already finished.
                if time.time() >= deadline:
                    _expire(future_to_symbol)
                    break
        except FuturesTimeoutError:
            _expire(future_to_symbol)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    outcome.qualified.sort(key=_rank_key)
    history_store.TRADE_HISTORY.prune(_effective_history_retention_days())
    data_health.record_screen(outcome)
    return outcome, labels


# Plain-English rendering of each gate, for readers who are not going to
# remember what ATR or EMA20 stand for while glancing at a phone.
TRADE_GATE_LABELS: Dict[str, str] = {
    "trend": "below their 20-day average",
    "momentum_5d": "too weak over 5 days",
    "momentum_1d": "fell on the day",
    "volatility": "too volatile",
    "drawdown": "too far below their 20-day high",
}


def _rsi_label(rsi: Optional[float]) -> str:
    if rsi is None:
        return "n/a"
    if rsi >= 70:
        return f"{rsi:.0f} (overbought)"
    if rsi <= 30:
        return f"{rsi:.0f} (oversold)"
    return f"{rsi:.0f}"


def _entry_exit_sketch(metrics: TradeMetrics) -> str:
    """A plain-technical entry/exit sketch from the recent 20-session range.

    Not a standalone signal -- it reads alongside the rest of the block and
    the "informational only" footer in `get_trade_candidates`. Reward/risk
    is measured against the same 20-session support/resistance the drawdown
    gate already uses, so it does not introduce a second notion of range.
    """
    reward = metrics.resistance_level - metrics.last_close
    risk = metrics.last_close - metrics.support_level
    if risk <= 0:
        reward_risk = "n/a"
    elif reward <= 0:
        # At or above its 20-session high there is no measured room to the old
        # resistance; "0.0:1" read as a terrible trade when it is really "the
        # range gives no target".
        reward_risk = "no room to the 20-day high"
    else:
        reward_risk = f"{reward / risk:.1f}:1"
    return (
        f"    zone {metrics.support_level:,.2f}–{metrics.ema20:,.2f}"
        f" · invalidation below {metrics.support_level:,.2f}"
        f" · reward:risk {reward_risk}"
    )


def _render_candidate(
    index: int, metrics: TradeMetrics, name: str,
    risk: Optional[risk_analytics.PortfolioRisk] = None,
) -> List[str]:
    """One candidate as a short block rather than a wide table row.

    A ten-column <pre> table runs about 100 characters, which Telegram either
    wraps into fragments or makes the reader scroll sideways on a phone.
    Stacking the same numbers costs vertical space, which scrolls fine.
    """
    confirmed = (
        " ✓"
        if metrics.volume_ratio is not None
        and metrics.volume_ratio >= SETTINGS.trade_min_volume_ratio
        else ""
    )
    volume_text = (
        f"{metrics.volume_ratio:.1f}× normal"
        if metrics.volume_ratio is not None
        else "n/a"
    )
    # Today's own row was already recorded by `_screen_universe` before
    # ranking runs, so a streak of 1 just means "qualified today" -- only
    # worth a note once it says something about the days before today too.
    streak = history_store.TRADE_HISTORY.recent_qualifying_streak(metrics.symbol)
    streak_note = f" · {streak}-session streak" if streak >= 2 else ""
    return [
        f"<b>{index}. {escape(name)}</b> · {escape(metrics.symbol)}",
        f"    {metrics.last_close:,.2f}   {metrics.day_change_pct:+.2f}% today"
        f"   {metrics.week_momentum_pct:+.2f}% over 5 days{streak_note}",
        f"    volume {volume_text}{confirmed}"
        f" · {metrics.drawdown_pct:.1f}% below 20-day high"
        f" · daily swing {metrics.atr_pct:.1f}%",
        f"    RSI {_rsi_label(metrics.rsi_14)} · volume {metrics.volume_trend}",
        _entry_exit_sketch(metrics),
        *risk_analytics.candidate_lines(metrics.symbol, risk),
        f"    <i>from the {escape(metrics.session)} close</i>",
        "",
    ]


def _render_blockers(outcome: ScreenOutcome) -> List[str]:
    """Why the rest did not qualify, most common reason first.

    Ordering by count rather than by gate order puts the actionable number at
    the top: if 10 of 30 were rejected as too volatile, TRADE_MAX_ATR_PCT is
    the knob worth looking at.
    """
    rows = [
        (TRADE_GATE_LABELS[gate], outcome.failed_counts[gate])
        for gate in TRADE_GATES
        if outcome.failed_counts.get(gate)
    ]
    rows.sort(key=lambda row: -row[1])
    if outcome.no_data:
        rows.append(("no usable price data", outcome.no_data))
    if not rows:
        return []

    rejected = outcome.checked - len(outcome.qualified)
    lines = [f"<b>Why the other {rejected} didn't qualify</b>"]
    lines.extend(f"    {count:>2} · {label}" for label, count in rows)
    lines.append("")
    return lines


def _render_deadline_notice(outcome: ScreenOutcome) -> List[str]:
    """Truncation notice, rendered independently of the blocker list.

    It cannot live inside _render_blockers: that returns early when nothing
    failed, so a run where every analysed symbol qualified would silently drop
    the notice and report a smaller universe than was configured.
    """
    if not outcome.deadline_hit:
        return []
    return [
        f"<i>Ran out of time — analysed {outcome.analysed} of "
        f"{outcome.checked}.</i>",
        "",
    ]


def _render_checks(any_qualified: bool) -> List[str]:
    """The gates as prose. Without these the blocker counts are unactionable.

    The heading is conditional because "every candidate passed all five" is a
    false statement on a day when none did.
    """
    heading = "Every candidate passed all five" if any_qualified else "The five checks"
    return [
        f"<b>{heading}</b>",
        "    price above its 20-day average",
        f"    up at least {SETTINGS.trade_min_week_momentum_pct:.1f}% over 5 days",
        f"    up more than {SETTINGS.trade_min_day_change_pct:.1f}% on the day",
        f"    within {SETTINGS.trade_max_drawdown_pct:.1f}% of its 20-day high",
        f"    daily swing under {SETTINGS.trade_max_atr_pct:.1f}%",
        "",
    ]


def get_trade_candidates(universe: Optional[Dict[str, str]] = None, top_n: int = 5) -> str:
    """The rendered trade-candidate section. Unchanged signature: most callers
    only want the text."""
    return build_trade_candidates_payload(universe=universe, top_n=top_n)[0]


def build_trade_candidates_payload(
    universe: Optional[Dict[str, str]] = None, top_n: int = 5
) -> Tuple[str, "ScreenOutcome", Dict[str, str]]:
    """The same render, plus the screening result that produced it.

    Split out so `/recommendations` can chart the top candidates without
    screening the universe a second time -- the screen is the expensive part
    of the command, and running it twice would also record each symbol's
    session row twice.
    """
    universe_max = max(1, SETTINGS.trade_universe_max)
    if universe is None:
        candidate_items = _build_candidate_universe(universe_max)
    else:
        candidate_items = list(universe.items())[:universe_max]

    outcome, labels = _screen_universe(candidate_items)
    # The ordering delivered to the reader is part of what a forward test must
    # judge. /watchlist screens a different, smaller universe, so only this
    # path records ranks.
    history_store.TRADE_HISTORY.archive_ranks(outcome.qualified)
    qualified_count = len(outcome.qualified)
    shown = outcome.qualified[:top_n]

    if qualified_count:
        lines = [
            f"📈 <b>Short-Term Trade Candidates — {qualified_count} of "
            f"{outcome.checked} passed</b>",
            "",
        ]
        # Looked up once for the shown picks, within a deadline; a failure only
        # means the risk lines are missing, never that the screen is.
        risk = risk_analytics.build_context(shown, labels)
        for index, metrics in enumerate(shown, 1):
            lines.extend(
                _render_candidate(index, metrics, labels.get(metrics.symbol, metrics.symbol), risk)
            )
        if qualified_count > len(shown):
            # Otherwise the header count and the number of blocks disagree.
            lines.append(
                f"<i>{qualified_count - len(shown)} more passed but are not shown.</i>"
            )
            lines.append("")
        lines.extend(risk_analytics.portfolio_lines(risk))
    else:
        lines = [
            "📉 <b>Short-Term Trade Candidates — none qualified today</b>",
            "",
            "No stock passed all five checks.",
            "",
        ]

    lines.extend(_render_deadline_notice(outcome))
    lines.extend(_render_blockers(outcome))
    lines.extend(_render_checks(bool(qualified_count)))
    # Split across two lines: a single 94-character sentence wraps awkwardly on
    # a phone, and this is the one part that should stay legible.
    lines.append("<i>Informational only. Not investment advice.</i>")
    lines.append("<i>No guarantee of profit. Use strict risk management.</i>")
    return "\n".join(lines) + "\n\n", outcome, labels


def _watchlist_line(metrics: TradeMetrics, name: str, note: str = "") -> str:
    """One symbol on one line. A watchlist is read by scanning it, so every
    entry gets the same shape and the same columns in the same order."""
    return (
        f"    <b>{escape(metrics.symbol)}</b> {escape(_truncate(name, 24))}"
        f" · {metrics.last_close:,.2f}"
        f" · {metrics.day_change_pct:+.2f}% today"
        f" · {metrics.week_momentum_pct:+.2f}% 5d"
        f" · RSI {_rsi_label(metrics.rsi_14)}{note}"
    )


def build_watchlist_report() -> str:
    """Every configured watchlist symbol with today's read and its verdict.

    `/watchlist` used to render the live market snapshot followed by a full
    trade screen -- which is what `/recommendations` already shows, filtered
    to the few names that passed. A configured symbol that failed a gate
    appeared nowhere at all, so the command never actually showed the user's
    watchlist. That is backwards: the reason to keep a watchlist is to see
    how *your* names are doing, and the ones that are not trade-worthy today
    are exactly the ones worth knowing about.

    Screens only the configured symbols, so it stays fast and its result does
    not depend on whatever the screeners happened to surface this morning.
    """
    entries = _configured_watchlist()
    # Defensive rather than routine: EU_STOCK_UNIVERSE falls back to
    # DEFAULT_EU_STOCK_UNIVERSE, so an untouched install still has 14 names
    # here. This branch only fires if that default is emptied too.
    if not entries:
        return (
            "<b>👀 Watchlist</b>\n\n"
            "No watchlist symbols are configured, so there is nothing to track.\n"
            "Set USA_STOCK_UNIVERSE, INDIA_STOCK_UNIVERSE, NORWAY_STOCK_UNIVERSE or "
            "EU_STOCK_UNIVERSE (Label:SYMBOL, comma separated) and restart the bot.\n"
            "Until then /recommendations screens discovered movers instead.\n\n"
        )

    universe_max = max(1, SETTINGS.trade_universe_max)
    analysed_entries = entries[:universe_max]
    outcome, labels = _screen_universe(analysed_entries)

    lines = [
        f"<b>👀 Watchlist — {len(outcome.qualified)} of {outcome.checked} trade-ready</b>",
        "",
    ]

    if outcome.qualified:
        lines.append("<b>✅ Trade-ready today</b>")
        for metrics in outcome.qualified:
            streak = history_store.TRADE_HISTORY.recent_qualifying_streak(metrics.symbol)
            note = f" · {streak}-session streak" if streak >= 2 else ""
            lines.append(_watchlist_line(metrics, labels.get(metrics.symbol, metrics.symbol), note))
        lines.append("")

    if outcome.rejected:
        # Strongest first, so the names closest to qualifying sit at the top
        # of the section a reader is most likely to skim.
        rejected = sorted(outcome.rejected, key=lambda item: -item[0].week_momentum_pct)
        lines.append("<b>⏸ Holding off</b>")
        for metrics, failed in rejected:
            reason = TRADE_GATE_LABELS.get(failed[0], failed[0])
            lines.append(
                _watchlist_line(metrics, labels.get(metrics.symbol, metrics.symbol), f" · {reason}")
            )
        lines.append("")

    if outcome.no_data_symbols:
        lines.append("<b>❔ No usable price data</b>")
        lines.append("    " + escape(", ".join(sorted(outcome.no_data_symbols))))
        lines.append("")

    lines.extend(_render_deadline_notice(outcome))

    if len(entries) > len(analysed_entries):
        lines.append(
            f"<i>{len(entries) - len(analysed_entries)} configured symbol(s) not analysed — "
            f"raise TRADE_UNIVERSE_MAX above {universe_max} to cover them all.</i>"
        )
        lines.append("")

    # Always the neutral heading: this report deliberately lists the symbols
    # that failed too, so "every candidate passed all five" would be false
    # here whenever anything is in the holding-off section.
    lines.extend(_render_checks(False))
    lines.append("<i>Informational only. Not investment advice.</i>")
    lines.append("<i>No guarantee of profit. Use strict risk management.</i>")
    return "\n".join(lines) + "\n\n"
