"""Risk analytics for trade candidates: sizing, clustering and event risk.

Why this exists: backtests of the screen (issue #12, and a ten-year check of
price factors and earnings drift on the evaluation basket) found no edge over
holding the same stocks. A ranked pick list therefore says less than it seems to.
What does hold up whatever the signal is how much to risk, whether the picks are
really one bet, and whether a known event sits inside the holding period. These
helpers answer those three questions from data the bot already has or can fetch
cheaply, and every one of them degrades to "unknown" rather than raising.
"""

import math
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

from newsbot import catalysts, market_data, relative_strength
from newsbot.config import SETTINGS
from newsbot.indicators import TradeMetrics, _drop_in_progress_bar
from newsbot.benchmarks import _backtest_benchmark_for
from newsbot.logsetup import SYS_LOG


# Measured, not asserted: `research/gap_risk.py` over the 44-stock evaluation
# basket, 2016-2026, placing this same stop on all 26,645 sessions where a
# symbol passed the shipped gates. 46% of those entries were stopped out, and
# 13.9% of the stops gapped through rather than filling at the stop -- 6.4% of
# all entries. The overshoot beyond the stop averaged 0.93 pp on a ~4% stop
# (1.23x the intended loss), with a 90th percentile of 2.3 pp and a worst case
# of 10.8 pp. Restate the numbers if the study is rerun.
GAP_RISK_NOTE = (
    "About one stop in seven gaps through instead of filling at the stop, "
    "costing roughly a quarter more than planned on average and several times "
    "that at worst."
)


@dataclass(frozen=True)
class PositionSizing:
    stop_pct: float        # distance to the volatility stop, percent of price
    stop_price: float
    weight_pct: float      # suggested share of the portfolio
    risk_pct: float        # portfolio loss if stopped out at that weight
    capped: bool           # the per-position cap, not the risk budget, set the weight
    amount: Optional[float] = None  # only when RISK_ACCOUNT_SIZE is set


def size_position(metrics: TradeMetrics) -> Optional[PositionSizing]:
    """Fixed-fractional sizing from a volatility stop.

    Weight = risk budget / stop distance, so a calmer stock gets a larger
    position and a wilder one a smaller one, each losing the same fraction of
    the portfolio if the stop is hit. Gaps can still lose more than the stop.
    """
    atr = metrics.atr_pct
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return None
    if SETTINGS.risk_per_trade_pct <= 0 or SETTINGS.risk_stop_atr_multiple <= 0:
        return None
    stop_pct = SETTINGS.risk_stop_atr_multiple * atr
    raw_weight = SETTINGS.risk_per_trade_pct / stop_pct * 100.0
    cap = SETTINGS.risk_max_position_pct
    weight = min(raw_weight, cap) if cap > 0 else raw_weight
    amount = SETTINGS.risk_account_size * weight / 100.0 if SETTINGS.risk_account_size > 0 else None
    return PositionSizing(
        stop_pct=stop_pct,
        stop_price=metrics.last_close * (1.0 - stop_pct / 100.0),
        weight_pct=weight,
        risk_pct=weight * stop_pct / 100.0,
        capped=weight < raw_weight,
        amount=amount,
    )


@dataclass(frozen=True)
class EventFlag:
    kind: str
    on: date
    days: int

    @property
    def text(self) -> str:
        when = "today" if self.days == 0 else "tomorrow" if self.days == 1 else f"in {self.days} days"
        return f"earnings {when} ({self.on:%b %d})"


def earnings_flag(profile: Optional[Dict[str, Any]], today: Optional[date] = None) -> Optional[EventFlag]:
    """Earnings inside the warning window, or None (including "unknown")."""
    if not profile or not isinstance(profile.get("next_earnings"), date):
        return None
    today = today or datetime.now().date()
    on = profile["next_earnings"]
    days = (on - today).days
    if 0 <= days <= SETTINGS.risk_earnings_warn_days:
        return EventFlag("earnings", on, days)
    return None


def _date_closes(symbol: str, period: str = "3mo") -> Optional[Any]:
    """Completed-session closes keyed by calendar date, so markets align by day."""
    history = _drop_in_progress_bar(market_data._fetch_ticker_history(symbol, period=period))
    if history is None or len(history) < 20 or "Close" not in history:
        return None
    close = history["Close"].dropna()
    close.index = [stamp.strftime("%Y-%m-%d") for stamp in close.index]
    return close[~close.index.duplicated()]


def _daily_returns(symbol: str) -> Optional[Any]:
    close = _date_closes(symbol)
    if close is None:
        return None
    return close.pct_change().dropna().tail(max(2, SETTINGS.risk_corr_window))


def correlated_pairs(symbols: Sequence[str]) -> List[Tuple[str, str, float, int]]:
    """Pairs whose recent daily returns moved together: (a, b, correlation, sessions).

    Uses only dates both symbols traded, so a US and an Oslo stock are compared
    on shared sessions. Pairs with too few shared sessions are left out rather
    than guessed at.
    """
    returns: Dict[str, Any] = {}
    for symbol in symbols:
        try:
            series = _daily_returns(symbol)
        except Exception as exc:
            SYS_LOG.debug("risk_returns_failed symbol=%s detail=%s", symbol, exc)
            series = None
        if series is not None:
            returns[symbol] = series
    pairs: List[Tuple[str, str, float, int]] = []
    names = list(returns)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            joined = returns[first].to_frame("a").join(returns[second].rename("b"), how="inner")
            if len(joined) < SETTINGS.risk_corr_min_obs:
                continue
            value = joined["a"].corr(joined["b"])
            if value is not None and math.isfinite(value) and value >= SETTINGS.risk_corr_threshold:
                pairs.append((first, second, float(value), len(joined)))
    return sorted(pairs, key=lambda item: -item[2])


def correlated_clusters(
    symbols: Sequence[str], pairs: Sequence[Tuple[str, str, float, int]]
) -> List[List[str]]:
    """Groups of symbols joined, directly or transitively, by a correlated pair.

    Sizing treats every pick as its own bet, so the "if every stop is hit"
    total implicitly assumes they fall independently. They often do not --
    `correlated_pairs` already proves it pair by pair. A cluster is the
    transitive closure of those pairs: if A moves with B and B with C, one
    shock plausibly stops out all three, so their risk adds up at once.
    Correlation is not causation and a cluster is not a guarantee; this only
    puts a number on a concentration the reader can already see pair by pair.
    """
    parent: Dict[str, str] = {symbol: symbol for symbol in symbols}

    def find(symbol: str) -> str:
        while parent[symbol] != symbol:
            parent[symbol] = parent[parent[symbol]]
            symbol = parent[symbol]
        return symbol

    for first, second, _value, _sessions in pairs:
        if first in parent and second in parent:
            a, b = find(first), find(second)
            if a != b:
                parent[b] = a
    groups: Dict[str, List[str]] = {}
    for symbol in symbols:
        groups.setdefault(find(symbol), []).append(symbol)
    # Only multi-symbol groups say anything; a lone symbol is not a cluster.
    return sorted((sorted(group) for group in groups.values() if len(group) > 1),
                  key=lambda group: (-len(group), group))


@dataclass
class PortfolioRisk:
    sizing: Dict[str, PositionSizing] = field(default_factory=dict)
    profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    events: Dict[str, EventFlag] = field(default_factory=dict)
    pairs: List[Tuple[str, str, float, int]] = field(default_factory=list)
    sector_groups: Dict[str, List[str]] = field(default_factory=dict)
    total_weight_pct: float = 0.0
    max_loss_pct: float = 0.0
    clusters: List[List[str]] = field(default_factory=list)
    cluster_weight_pct: float = 0.0   # the largest cluster's combined weight
    cluster_loss_pct: float = 0.0     # loss if that whole cluster stops out
    warnings: List[str] = field(default_factory=list)
    unknown_profiles: int = 0
    strength: Dict[str, relative_strength.RelativeStrength] = field(default_factory=dict)
    headlines: Dict[str, List[catalysts.Headline]] = field(default_factory=dict)


def assess(picks: Sequence[TradeMetrics], profiles: Dict[str, Dict[str, Any]],
           pairs: Sequence[Tuple[str, str, float, int]], today: Optional[date] = None) -> PortfolioRisk:
    """Combine per-pick sizing, events, clustering and totals into warnings. Pure."""
    risk = PortfolioRisk(profiles=dict(profiles), pairs=list(pairs))
    for metrics in picks:
        sized = size_position(metrics)
        if sized is not None:
            risk.sizing[metrics.symbol] = sized
        flag = earnings_flag(profiles.get(metrics.symbol), today)
        if flag is not None:
            risk.events[metrics.symbol] = flag
        if not profiles.get(metrics.symbol):
            risk.unknown_profiles += 1

    by_sector: Dict[str, List[str]] = {}
    for metrics in picks:
        sector = (profiles.get(metrics.symbol) or {}).get("sector")
        if sector:
            by_sector.setdefault(sector, []).append(metrics.symbol)
    risk.sector_groups = {sector: names for sector, names in by_sector.items() if len(names) >= 2}

    risk.total_weight_pct = sum(item.weight_pct for item in risk.sizing.values())
    risk.max_loss_pct = sum(item.risk_pct for item in risk.sizing.values())

    risk.clusters = correlated_clusters([m.symbol for m in picks], risk.pairs)
    if risk.clusters:
        largest = risk.clusters[0]
        risk.cluster_weight_pct = sum(
            risk.sizing[symbol].weight_pct for symbol in largest if symbol in risk.sizing)
        risk.cluster_loss_pct = sum(
            risk.sizing[symbol].risk_pct for symbol in largest if symbol in risk.sizing)

    for first, second, value, sessions in risk.pairs:
        combined = sum(risk.sizing[s].weight_pct for s in (first, second) if s in risk.sizing)
        risk.warnings.append(
            f"{first} and {second} moved together (correlation {value:.2f} over {sessions} sessions): "
            f"sized separately they put {combined:.0f}% of the portfolio into what is close to one bet."
        )
    for sector, names in risk.sector_groups.items():
        risk.warnings.append(f"{', '.join(names)} are all {sector}.")
    # Only worth saying for a cluster bigger than the pair warnings above
    # already cover, otherwise it restates them with extra arithmetic.
    if len(risk.clusters) and len(risk.clusters[0]) >= 3 and risk.cluster_loss_pct > 0:
        largest = risk.clusters[0]
        risk.warnings.append(
            f"{', '.join(largest)} all move together: one shock stopping out the whole group "
            f"costs {risk.cluster_loss_pct:.1f}%, not the "
            f"{risk.cluster_loss_pct / len(largest):.1f}% a single pick risks."
        )
    if (len(risk.sizing) >= 2 and SETTINGS.risk_max_total_pct > 0
            and risk.total_weight_pct > SETTINGS.risk_max_total_pct):
        risk.warnings.append(
            f"Taking all {len(risk.sizing)} at these sizes puts {risk.total_weight_pct:.0f}% of the "
            f"portfolio to work; if every stop is hit that is a {risk.max_loss_pct:.1f}% loss, "
            "and more if they move together or gap."
        )
    return risk


def build_context(picks: Sequence[TradeMetrics], labels: Optional[Dict[str, str]] = None) -> PortfolioRisk:
    """Fetch what `assess` needs, within a deadline. Never raises, never blocks long."""
    if not SETTINGS.risk_analytics_enabled or not picks:
        return PortfolioRisk()
    symbols = [metrics.symbol for metrics in picks]
    profiles: Dict[str, Dict[str, Any]] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, min(5, len(symbols))), thread_name_prefix="risk")
    try:
        futures = {symbol: pool.submit(market_data._fetch_event_profile, symbol) for symbol in symbols}
        deadline_ok = True
        end = time.monotonic() + max(2, SETTINGS.risk_context_deadline_seconds)
        for symbol, future in futures.items():
            try:
                profiles[symbol] = future.result(timeout=max(0.0, end - time.monotonic()))
            except FuturesTimeoutError:
                deadline_ok = False
            except Exception as exc:
                SYS_LOG.debug("risk_profile_failed symbol=%s detail=%s", symbol, exc)
        if not deadline_ok:
            SYS_LOG.warning("risk_context_deadline_reached got=%s of=%s", len(profiles), len(symbols))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    try:
        pairs = correlated_pairs(symbols) if len(symbols) >= 2 else []
    except Exception as exc:
        SYS_LOG.warning("risk_correlation_failed detail=%s", exc)
        pairs = []
    context = assess(picks, profiles, pairs)
    if SETTINGS.relative_strength_enabled:
        context.strength = _relative_strength(picks, profiles)
    context.headlines = catalysts.find(symbols, labels)
    return context


def _relative_strength(
    picks: Sequence[TradeMetrics], profiles: Dict[str, Dict[str, Any]]
) -> Dict[str, relative_strength.RelativeStrength]:
    """Relative strength per pick. Each distinct symbol (stock, market proxy,
    sector ETF) is fetched once, in parallel, then every pick is computed from
    those; a shared benchmark such as SPY is not requested once per pick."""
    wanted: Dict[str, None] = {}
    for metrics in picks:
        wanted[metrics.symbol] = None
        market = _backtest_benchmark_for(metrics.symbol)
        if market:
            wanted[market] = None
        etf = relative_strength.sector_etf(metrics.symbol, (profiles.get(metrics.symbol) or {}).get("sector"))
        if etf:
            wanted[etf] = None

    closes: Dict[str, Any] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, min(6, len(wanted))), thread_name_prefix="rs")
    try:
        futures = {symbol: pool.submit(_date_closes, symbol, "6mo") for symbol in wanted}
        end = time.monotonic() + max(2, SETTINGS.risk_context_deadline_seconds)
        for symbol, future in futures.items():
            try:
                closes[symbol] = future.result(timeout=max(0.0, end - time.monotonic()))
            except FuturesTimeoutError:
                SYS_LOG.warning("relative_strength_deadline_reached got=%s of=%s", len(closes), len(wanted))
                break
            except Exception as exc:
                SYS_LOG.debug("relative_strength_fetch_failed symbol=%s detail=%s", symbol, exc)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    result: Dict[str, relative_strength.RelativeStrength] = {}
    for metrics in picks:
        sector = (profiles.get(metrics.symbol) or {}).get("sector")
        strength = relative_strength.safe_compute(metrics.symbol, sector, closes.get)
        if strength is not None:
            result[metrics.symbol] = strength
    return result


def candidate_lines(symbol: str, risk: Optional[PortfolioRisk]) -> List[str]:
    """Extra lines under one candidate: sizing and any event flag."""
    if risk is None:
        return []
    lines: List[str] = []
    sized = risk.sizing.get(symbol)
    if sized is not None:
        text = (
            f"    risk: volatility stop {sized.stop_price:,.2f} ({sized.stop_pct:.1f}% away)"
            f" · size {sized.weight_pct:.0f}% of portfolio"
        )
        if sized.capped:
            text += f" (capped; risks {sized.risk_pct:.2f}%)"
        else:
            text += f" for {sized.risk_pct:.2f}% risk"
        if sized.amount is not None:
            text += f" ≈ {sized.amount:,.0f}"
        lines.append(text)
    strength_line = relative_strength.render_line(risk.strength.get(symbol))
    if strength_line:
        lines.append(escape(strength_line, quote=False))
    lines.extend(f"    {item.text}" for item in risk.headlines.get(symbol, []))
    flag = risk.events.get(symbol)
    if flag is not None:
        lines.append(f"    ⚠️ {escape(flag.text)} — inside a typical {SETTINGS.trade_outcome_lookback_days}-session hold")
    return lines


def portfolio_lines(risk: Optional[PortfolioRisk]) -> List[str]:
    """The cross-pick section: clustering and totals. Empty when there is nothing to say."""
    if risk is None or not risk.sizing:
        return []
    lines = ["<b>Taken together</b>"]
    lines.extend(f"    ⚠️ {escape(warning)}" for warning in risk.warnings)
    if not risk.warnings:
        lines.append(
            f"    {len(risk.sizing)} picks at these sizes: {risk.total_weight_pct:.0f}% of the "
            f"portfolio at work, {risk.max_loss_pct:.1f}% at risk if every stop is hit."
        )
    elif len(risk.sizing) >= 2:
        lines.append(f"    Totals: {risk.total_weight_pct:.0f}% at work, {risk.max_loss_pct:.1f}% at risk if every stop is hit.")
    if risk.unknown_profiles:
        lines.append(f"    <i>Sector and earnings data unavailable for {risk.unknown_profiles} pick(s).</i>")
    lines.append(
        f"    <i>Sizes risk {SETTINGS.risk_per_trade_pct:g}% of the portfolio per pick on a "
        f"{SETTINGS.risk_stop_atr_multiple:g}×ATR stop. {GAP_RISK_NOTE} The screen itself has "
        "not been shown to beat holding the same stocks.</i>"
    )
    lines.append("")
    return lines
