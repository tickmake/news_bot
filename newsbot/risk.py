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
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

from newsbot import catalysts, market_data, relative_strength, stock_list
from newsbot.config import SETTINGS
from newsbot.indicators import TradeMetrics, _drop_in_progress_bar
from newsbot.benchmarks import _backtest_benchmark_for
from newsbot.logsetup import SYS_LOG


# Where the stop is placed: below the close the signal was formed on, not below
# the buy price (#172). That close is the reference price for every number here.
STOP_ANCHOR = "signal_close"
# What the loss figures assume. Nobody has bought when they are worked out, so
# they are modelled as if bought at the reference price; the actual loss on a
# position depends on its buy price, which the bot does not have for a signal.
LOSS_BASIS = "if_bought_at_reference_price"
# Version of the per-pick risk record saved with a published pick. Raise it when
# a field changes meaning, so an old record is never read as a new one.
# 2: the gap is a fill below the stop price (#161 review); 1 never shipped.
RISK_MODEL_VERSION = 2


@dataclass(frozen=True)
class GapStudy:
    """What `research/gap_risk.py` measured for one stop: N x ATR below `anchor`.

    When a stop gapped, the sale came at the open, below the stop price. The
    shortfall is in percent of the **stop price**, which does not depend on
    what anyone paid, so it can be applied before a buy.
    """

    multiple: float
    anchor: str
    entries: int
    stopped_pct: float            # of entries, over a 20-session hold
    gapped_of_stops_pct: float    # stops that opened below the stop price
    gap_below_stop_mean_pct: float
    gap_below_stop_p90_pct: float
    gap_below_stop_worst_pct: float


# Measured, not asserted: `research/gap_risk.py` over the 44-stock evaluation
# basket, ten years to 2026-11-13, on every session that passed the live gates
# (docs/research, section 11). Copied from the script's last line; the full
# outputs are in docs/research/data/gap_risk_*x_signal-close.txt and a test
# checks these rows against them. A stop that is not here gets "not measured",
# never a borrowed row.
GAP_STUDIES: Tuple[GapStudy, ...] = (
    GapStudy(multiple=2.0, anchor="signal_close", entries=26407, stopped_pct=46.0,
             gapped_of_stops_pct=14.3, gap_below_stop_mean_pct=0.99,
             gap_below_stop_p90_pct=2.36, gap_below_stop_worst_pct=12.04),
    GapStudy(multiple=3.0, anchor="signal_close", entries=26438, stopped_pct=29.0,
             gapped_of_stops_pct=13.7, gap_below_stop_mean_pct=1.09,
             gap_below_stop_p90_pct=2.75, gap_below_stop_worst_pct=12.75),
)


def gap_study(multiple: Optional[float] = None) -> Optional[GapStudy]:
    """The measurement for the stop in use, or None when that stop was never measured."""
    wanted = SETTINGS.risk_stop_atr_multiple if multiple is None else multiple
    for study in GAP_STUDIES:
        if study.anchor == STOP_ANCHOR and math.isclose(study.multiple, wanted, abs_tol=1e-9):
            return study
    return None


def gap_note() -> str:
    """One sentence on gap risk for the stop in use. Never quotes another stop's numbers."""
    study = gap_study()
    if study is None:
        return (f"Gap risk has not been measured for a {SETTINGS.risk_stop_atr_multiple:g}×ATR stop, "
                "so no gap estimate is shown.")
    return (
        f"In ten years of tests on 44 large stocks about 1 stop in "
        f"{round(100 / study.gapped_of_stops_pct)} opened below its price and sold "
        f"{study.gap_below_stop_mean_pct:.1f}% below it on average, less than "
        f"{study.gap_below_stop_p90_pct:.1f}% below in nine of ten and {study.gap_below_stop_worst_pct:.1f}% "
        "at worst; the gap estimate uses the nine-in-ten case and is not a maximum."
    )


@dataclass(frozen=True)
class PositionSizing:
    stop_pct: float        # distance to the volatility stop, percent of price
    stop_price: float
    weight_pct: float      # suggested share of the portfolio
    risk_pct: float        # portfolio loss if stopped out at that weight
    capped: bool           # the per-position cap, not the risk budget, set the weight
    amount: Optional[float] = None  # only when RISK_ACCOUNT_SIZE is set


def _stop_pct(metrics: TradeMetrics) -> Optional[float]:
    """Distance from the signal close to the stop, percent. None when there is no usable ATR."""
    atr = metrics.atr_pct
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return None
    if not math.isfinite(metrics.last_close) or metrics.last_close <= 0:
        return None
    if SETTINGS.risk_stop_atr_multiple <= 0:
        return None
    return SETTINGS.risk_stop_atr_multiple * atr


def size_position(metrics: TradeMetrics) -> Optional[PositionSizing]:
    """Fixed-fractional sizing from a volatility stop.

    Weight = risk budget / stop distance, so a calmer stock gets a larger
    position and a wilder one a smaller one, each losing the same fraction of
    the portfolio if the stop is hit. Gaps can still lose more than the stop.
    """
    stop_pct = _stop_pct(metrics)
    if stop_pct is None or SETTINGS.risk_per_trade_pct <= 0:
        return None
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
class TradeRisk:
    """Everything the risk lines say about one position, worked out once.

    Telegram renders it, a published pick saves it (`snapshot`) and the
    dashboard shows the saved copy, so the three cannot disagree. Prices are
    in the stock's currency; money is in NOK. A field is None when it cannot
    be worked out, never a guess.

    The loss figures are modelled as if the position were bought at
    `reference_price`, the signal close (`LOSS_BASIS`). The real buy price is
    not known when this is worked out, so the actual loss is not: paying more
    than the reference makes it larger, paying less makes it smaller. The stop
    price and the gap sale price do not depend on the buy price.
    """

    symbol: str
    session: str                         # the close every number is from
    reference_price: float               # that close: the stop anchor
    atr_pct: float
    atr_price: float
    atr_multiple: float
    stop_price: float
    stop_distance_pct: float             # of the reference price
    stop_distance_price: float
    exposure_nok: Optional[float]        # money in the position, at the reference price
    exposure_pct: Optional[float]        # ... as a share of the account
    account_nok: Optional[float]
    capped: bool
    # Theoretical stop risk: bought at the reference, sold exactly at the stop, before costs.
    stop_loss_nok: Optional[float]
    stop_loss_pct: Optional[float]       # of the account
    # Gap estimate: bought at the reference, sold at `gap_fill_price`, the stop
    # price less the nine-in-ten shortfall measured for this stop. None when
    # this stop was never measured.
    gap_measured: bool
    gap_below_stop_pct: Optional[float]  # of the stop price
    gap_fill_price: Optional[float]
    gap_loss_nok: Optional[float]
    gap_loss_pct: Optional[float]        # of the account
    # Estimated broker fee to buy and sell. Spread and slippage are not modelled.
    fee_nok: Optional[float]
    fee_pct: Optional[float]             # of the account
    fx_cost_included: Optional[bool]     # None: no currency exchange is needed
    warnings: Tuple[str, ...] = ()


def round_trip_fee(amount: float) -> float:
    """Estimated broker fee to buy and later sell a position of `amount` NOK."""
    return 2 * max(SETTINGS.broker_fee_min_nok, amount * SETTINGS.broker_fee_pct / 100.0)


def liquidity_warnings(metrics: TradeMetrics) -> Tuple[str, ...]:
    """Reasons a stop might fill worse than shown, from the volume the bot already reads.

    No gate reads these and sizing ignores them. The floor is the one the
    backtest already applies (`TRADE_BACKTEST_MIN_AVG_VOLUME`).
    """
    if metrics.volume_ratio is None and metrics.avg_volume_20 is None:
        return ("No volume data: how easily this stock trades was not checked.",)
    floor = SETTINGS.trade_backtest_min_avg_volume
    average = metrics.avg_volume_20
    if average is not None and math.isfinite(average) and floor > 0 and average < floor:
        return (f"Thin trading: about {average:,.0f} shares a day, under the {floor:,.0f} the "
                "backtest requires. A stop can fill well below its price.",)
    return ()


def trade_risk(metrics: TradeMetrics, exposure_nok: Optional[float] = None,
               account_nok: Optional[float] = None, weight_pct: Optional[float] = None,
               capped: bool = False) -> Optional[TradeRisk]:
    """The risk of holding `exposure_nok` of this stock with the configured stop. Pure.

    `weight_pct` gives the account shares when only the weight is known (no
    account size set). Long positions only: the bot has no short trades.
    """
    stop_pct = _stop_pct(metrics)
    if stop_pct is None:
        return None
    if exposure_nok is not None and (not math.isfinite(exposure_nok) or exposure_nok <= 0):
        exposure_nok = None
    if account_nok is not None and (not math.isfinite(account_nok) or account_nok <= 0):
        account_nok = None
    if exposure_nok is not None and account_nok is not None:
        exposure_pct: Optional[float] = exposure_nok / account_nok * 100.0
    else:
        exposure_pct = weight_pct

    def share(loss_pct_of_position: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
        if loss_pct_of_position is None:
            return None, None
        return (exposure_nok * loss_pct_of_position / 100.0 if exposure_nok is not None else None,
                exposure_pct * loss_pct_of_position / 100.0 if exposure_pct is not None else None)

    close = metrics.last_close
    stop_price = close * (1.0 - stop_pct / 100.0)
    # The gap shortfall was measured in percent of the stop price, so the sale
    # price follows from the stop alone; the loss is then from the reference.
    study = gap_study()
    gap_fill = stop_price * (1.0 - study.gap_below_stop_p90_pct / 100.0) if study else None
    stop_loss_nok, stop_loss_pct = share(stop_pct)
    gap_loss_nok, gap_loss_pct = share((1.0 - gap_fill / close) * 100.0 if gap_fill is not None else None)
    fee_nok = round_trip_fee(exposure_nok) if exposure_nok is not None else None
    return TradeRisk(
        symbol=metrics.symbol,
        session=metrics.session,
        reference_price=close,
        atr_pct=metrics.atr_pct,
        atr_price=close * metrics.atr_pct / 100.0,
        atr_multiple=SETTINGS.risk_stop_atr_multiple,
        stop_price=stop_price,
        stop_distance_pct=stop_pct,
        stop_distance_price=close * stop_pct / 100.0,
        exposure_nok=exposure_nok,
        exposure_pct=exposure_pct,
        account_nok=account_nok,
        capped=capped,
        stop_loss_nok=stop_loss_nok,
        stop_loss_pct=stop_loss_pct,
        gap_measured=study is not None,
        gap_below_stop_pct=study.gap_below_stop_p90_pct if study else None,
        gap_fill_price=gap_fill,
        gap_loss_nok=gap_loss_nok,
        gap_loss_pct=gap_loss_pct,
        fee_nok=fee_nok,
        fee_pct=fee_nok / account_nok * 100.0 if fee_nok is not None and account_nok else None,
        fx_cost_included=None if metrics.symbol.upper().endswith(".OL") else False,
        warnings=liquidity_warnings(metrics),
    )


def pick_risk(metrics: TradeMetrics) -> Optional[TradeRisk]:
    """The risk of a pick at the size the risk rule suggests: what /recommendations shows."""
    sized = size_position(metrics)
    if sized is None:
        return None
    account = SETTINGS.risk_account_size if SETTINGS.risk_account_size > 0 else None
    return trade_risk(metrics, exposure_nok=sized.amount, account_nok=account,
                      weight_pct=sized.weight_pct, capped=sized.capped)


def snapshot(risk: Optional[TradeRisk]) -> Optional[Dict[str, Any]]:
    """The record saved with a published pick, and read back by the dashboard."""
    if risk is None:
        return None
    # No buy price: a pick is published before anyone buys, so the actual
    # position risk is recorded as unknown rather than guessed.
    return {"model_version": RISK_MODEL_VERSION, "stop_anchor": STOP_ANCHOR,
            "loss_basis": LOSS_BASIS, "entry_price": None,
            **asdict(risk), "warnings": list(risk.warnings)}


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
    trades: Dict[str, TradeRisk] = field(default_factory=dict)
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
            trade = pick_risk(metrics)
            if trade is not None:
                risk.trades[metrics.symbol] = trade
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


def fee_line(symbol: str, amount: Optional[float], account: Optional[float] = None) -> str:
    """What buying and later selling would cost at the broker, in NOK. An estimate
    from the fee table; the currency exchange cost is not known, so it is named, not added."""
    minimum = 2 * SETTINGS.broker_fee_min_nok
    fx_note = "" if symbol.upper().endswith(".OL") else " + currency exchange fee"
    if amount is None or amount <= 0:
        return f"    fees: at least {minimum:,.0f} NOK to buy and sell{fx_note}"
    round_trip = round_trip_fee(amount)
    of_account = f", {round_trip / account * 100:.2f}% of {account:,.0f} NOK" if account else ""
    return (
        f"    fees: {round_trip:,.0f} NOK to buy and sell"
        f" ({round_trip / amount * 100:.1f}% of {amount:,.0f} NOK{of_account}){fx_note}"
    )


def _money(pct: Optional[float], nok: Optional[float], whole: str) -> Optional[str]:
    """"1.50% of the portfolio ≈ 750 NOK", with whichever half is known."""
    parts = []
    if pct is not None:
        parts.append(f"{pct:.2f}% of {whole}")
    if nok is not None:
        parts.append(f"{nok:,.0f} NOK")
    return " ≈ ".join(parts) or None


def stop_line(trade: TradeRisk, currency: str = "") -> str:
    """Where the stop is, the reference price it is measured from, and how far away it is."""
    unit = f" {currency}" if currency else ""
    return (
        f"    risk: volatility stop {trade.stop_price:,.2f}{unit}, {trade.atr_multiple:g}×ATR below the "
        f"{trade.reference_price:,.2f} signal close: {trade.stop_distance_price:,.2f} "
        f"({trade.stop_distance_pct:.1f}%) away · ATR {trade.atr_price:,.2f} ({trade.atr_pct:.1f}%)"
    )


def size_line(trade: TradeRisk, whole: str = "the portfolio") -> Optional[str]:
    """Money in the position, kept apart from the money at risk."""
    if trade.exposure_pct is None:
        return None
    text = f"    size: {trade.exposure_pct:.0f}% of {whole}"
    if trade.exposure_nok is not None:
        text += f" ≈ {trade.exposure_nok:,.0f} NOK"
    if trade.capped:
        text += f" (capped at {SETTINGS.risk_max_position_pct:g}%)"
    return text


def loss_lines(trade: TradeRisk, whole: str = "the portfolio") -> List[str]:
    """Loss at the stop and in a gap, both modelled from the signal close and said to be.

    The buy price is not known here, so the line says the actual risk is not
    known yet rather than letting the modelled figure pass for it.
    """
    planned = _money(trade.stop_loss_pct, trade.stop_loss_nok, whole)
    if planned is None:
        return []
    lines = [f"    loss if bought at the {trade.reference_price:,.2f} close: {planned} at the stop, "
             "before costs; your actual risk isn't known until you buy"]
    if trade.gap_measured:
        gap = _money(trade.gap_loss_pct, trade.gap_loss_nok, whole)
        lines.append(f"    if it gaps through: sold near {trade.gap_fill_price:,.2f}, about {gap} "
                     "from that close (estimate, not a maximum)")
    else:
        lines.append(f"    if it gaps through: not measured for a {trade.atr_multiple:g}×ATR stop")
    return lines


def warning_lines(trade: TradeRisk) -> List[str]:
    return [f"    ⚠️ {escape(warning, quote=False)}" for warning in trade.warnings]


def ask_lines(symbol: str) -> List[str]:
    """A warning line when the stock can't, or might not, go in an ASK (#107)."""
    note = stock_list.ask_note(symbol)
    return [f"    ⛔ {escape(note)}"] if note else []


def candidate_lines(symbol: str, risk: Optional[PortfolioRisk]) -> List[str]:
    """Extra lines under one candidate: stop, size, loss, fees, warnings and any event flag."""
    if risk is None:
        # The fee and the ASK rule are known without any lookups, so they show even then.
        return [fee_line(symbol, None), *ask_lines(symbol)]
    lines: List[str] = []
    trade = risk.trades.get(symbol)
    if trade is not None:
        lines.append(stop_line(trade))
        size = size_line(trade)
        if size:
            lines.append(size)
        lines.extend(loss_lines(trade))
    lines.append(fee_line(symbol, trade.exposure_nok if trade else None, trade.account_nok if trade else None))
    if trade is not None:
        lines.extend(warning_lines(trade))
    lines.extend(ask_lines(symbol))
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
    account = (f" Amounts use a portfolio of {SETTINGS.risk_account_size:,.0f} NOK (RISK_ACCOUNT_SIZE)."
               if SETTINGS.risk_account_size > 0 else "")
    lines.append(
        f"    <i>Sizes risk {SETTINGS.risk_per_trade_pct:g}% of the portfolio per pick on a "
        f"{SETTINGS.risk_stop_atr_multiple:g}×ATR stop below the signal close. Losses are modelled as "
        "if bought at that close: the actual risk of a position isn't known until it is bought, and "
        f"paying more than the close makes it larger.{account} {gap_note()} Fees are estimates; spread, slippage and "
        "currency exchange cost are not included. The screen itself has "
        "not been shown to beat holding the same stocks.</i>"
    )
    lines.append("")
    return lines
