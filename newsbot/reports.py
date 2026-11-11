"""Weekly top picks, single-symbol analysis and the health report."""

from datetime import datetime
from html import escape
from typing import Any, List, Optional

from newsbot import data_health, history_store, indicators, market_data, risk_forecast, risk as risk_analytics
from newsbot.config import SETTINGS
from newsbot.market_data import _as_float
from newsbot.screener import _entry_exit_sketch, _rsi_label
from newsbot.state import RANKER_STATUS, STATE
from newsbot.util import _truncate


def build_weekly_top_picks(lookback_days: Optional[int] = None, top_n: Optional[int] = None) -> str:
    """This week's most consistent qualifiers, for swing trading over the
    next several sessions -- distinct from `/recommendations`, which is a
    live single-day re-screen.

    Computed on demand from `TRADE_HISTORY` -- a local SQLite read, no
    fresh yfinance calls -- ranking by how many sessions in the window a
    symbol qualified, then by average weekly momentum across the window.
    """
    lookback = lookback_days if lookback_days is not None else SETTINGS.trade_weekly_top_picks_lookback_days
    limit = top_n if top_n is not None else SETTINGS.trade_weekly_top_picks_count
    picks = history_store.TRADE_HISTORY.weekly_top_picks(
        lookback_days=lookback,
        top_n=limit,
        min_qualifying_days=SETTINGS.trade_weekly_top_picks_min_qualifying_days,
    )
    lines = [
        "<b>📆 Weekly Top Picks</b>",
        f"<i>Last {lookback} sessions, ranked by qualifying-day consistency</i>",
        "",
    ]
    if not picks:
        lines.append(
            "No symbol has qualified enough sessions yet in this window "
            f"(needs at least {SETTINGS.trade_weekly_top_picks_min_qualifying_days} of the "
            f"last {lookback}). Configured watchlist symbols are screened every session; "
            f"discovered movers are carried forward for "
            f"{SETTINGS.trade_sticky_universe_days} days after they are first seen. "
            "Try /recommendations for today's live picks, or /watchlist to see how your "
            "configured names are doing right now."
        )
        lines.append("")
        return "\n".join(lines) + "\n\n"

    for index, pick in enumerate(picks, start=1):
        lines.append(
            f"<b>{index}. {escape(pick['symbol'])}</b> - qualified {pick['qualifying_days']}/"
            f"{pick['sessions_tracked']} tracked sessions"
        )
        lines.append(
            f"    Avg weekly momentum: {pick['avg_week_momentum_pct']:+.1f}%"
            f" · Last close: {pick['latest_close']:.2f} ({pick['latest_day_change_pct']:+.1f}% that day)"
            f" · ATR: {pick['latest_atr_pct']:.1f}% · Drawdown: {pick['latest_drawdown_pct']:.1f}%"
        )
        lines.append(f"    Last tracked session: {pick['latest_session']}")
        lines.append("")
    lines.append(
        "<i>Informational only. Not investment advice. Ranked from this bot's own stored "
        "screening history, not a fresh live re-scan -- use /analyze TICKER for a deeper "
        "read on any name before acting.</i>"
    )
    return "\n".join(lines) + "\n\n"


def _fmt_num(value: Any, digits: int = 2) -> str:
    parsed = _as_float(value)
    return "n/a" if parsed is None else f"{parsed:,.{digits}f}"


def _fmt_pct(value: Any, digits: int = 1) -> str:
    """`value` is yfinance's usual fractional form (0.23 for 23%)."""
    parsed = _as_float(value)
    return "n/a" if parsed is None else f"{parsed * 100:.{digits}f}%"


def _fmt_large(value: Any) -> str:
    """A large dollar figure with a magnitude suffix, e.g. 12.30B."""
    parsed = _as_float(value)
    if parsed is None:
        return "n/a"
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(parsed) >= threshold:
            return f"{parsed / threshold:,.2f}{suffix}"
    return f"{parsed:,.0f}"


def _beta_read(beta: Optional[float]) -> str:
    if beta is None:
        return ""
    if beta > 1.2:
        return " (more volatile than the market)"
    if beta < 0.8:
        return " (less volatile than the market)"
    return " (roughly market-like volatility)"


def build_ticker_analysis(raw_symbol: str) -> str:
    """An on-demand, single-ticker report following as much of the
    requester's "elite analyst" framework as this bot can honestly support
    from data it actually has: yfinance price history (reusing the same
    `_compute_trade_metrics`/`_entry_exit_sketch` the automated screener
    uses) and yfinance's `.info` snapshot for fundamentals and valuation.

    Sections the framework asks for that this bot has no real data source
    for -- macro series, options positioning, institutional 13F flow,
    insider transaction feeds -- are named as unavailable, never invented.
    yfinance's fundamentals are frequently incomplete for smaller or
    non-US tickers, so every field degrades to "n/a" rather than raising.
    """
    symbol = raw_symbol.strip().upper()
    if not symbol:
        return "Usage: /analyze TICKER (e.g. /analyze AAPL)\n"

    metrics = indicators._compute_trade_metrics(symbol)
    info = market_data._fetch_ticker_fundamentals(symbol)
    name = info.get("longName") or info.get("shortName") or symbol

    lines: List[str] = [f"<b>📊 {escape(name)} ({escape(symbol)})</b>", ""]

    # 1. Company
    lines.append("<b>Company</b>")
    summary = info.get("longBusinessSummary")
    lines.append(_truncate(summary, SETTINGS.ticker_analysis_summary_max_chars) if summary
                 else "Business summary not available.")
    profile_bits = [info.get(key) for key in ("sector", "industry", "country") if info.get(key)]
    if profile_bits:
        lines.append(" · ".join(profile_bits))
    employees = _as_float(info.get("fullTimeEmployees"))
    if employees:
        lines.append(f"Employees: {employees:,.0f}")
    lines.append("")

    # 2. Price & technicals (reuses the automated screener's own math)
    lines.append("<b>Price &amp; Technicals</b>")
    if metrics:
        trend_note = "above" if metrics.above_ema20 else "below"
        lines.append(
            f"Last close {metrics.last_close:,.2f} ({metrics.day_change_pct:+.2f}% today, "
            f"{metrics.week_momentum_pct:+.2f}% over 5 days), {trend_note} its 20-day EMA "
            f"({metrics.ema20:,.2f})."
        )
        lines.append(
            f"RSI {_rsi_label(metrics.rsi_14)} · volume {metrics.volume_trend} "
            f"({metrics.volume_ratio:.1f}× normal)"
            if metrics.volume_ratio is not None
            else f"RSI {_rsi_label(metrics.rsi_14)} · volume {metrics.volume_trend} (n/a)"
        )
        lines[-1] += (
            f" · daily swing (ATR) {metrics.atr_pct:.1f}%"
            f" · {metrics.drawdown_pct:.1f}% below its 20-day high."
        )
        lines.append(_entry_exit_sketch(metrics).strip())
    else:
        lines.append("Not enough price history available from yfinance for this symbol.")
    lines.append("")

    # 2b. Risk and event risk, from the same helpers /recommendations uses.
    if metrics:
        lines.append("<b>Risk &amp; Events</b>")
        context = risk_analytics.build_context([metrics], {symbol: name}) if SETTINGS.risk_analytics_enabled else None
        sized_lines = risk_analytics.candidate_lines(symbol, context)
        lines.extend(sized_lines or ["Position sizing unavailable (no usable volatility reading)."])
        profile = (context.profiles.get(symbol) if context else None) or {}
        upcoming = profile.get("next_earnings")
        if upcoming and not (context and symbol in context.events):
            lines.append(f"Next earnings: {upcoming:%b %d, %Y}.")
        elif not upcoming and not (context and symbol in context.events):
            lines.append("Next earnings date: not available.")
        forecast_lines = risk_forecast.lines(risk_forecast.build(symbol))
        if forecast_lines:
            lines.append("How much it can fall over a 2-4 week hold:")
            lines.extend(forecast_lines)
        lines.append("")

    # 3. Valuation
    lines.append("<b>Valuation</b>")
    lines.append(
        f"P/E {_fmt_num(info.get('trailingPE'))} (fwd {_fmt_num(info.get('forwardPE'))})"
        f" · PEG {_fmt_num(info.get('pegRatio') or info.get('trailingPegRatio'))}"
        f" · P/S {_fmt_num(info.get('priceToSalesTrailing12Months'))}"
        f" · P/B {_fmt_num(info.get('priceToBook'))}"
        f" · EV/EBITDA {_fmt_num(info.get('enterpriseToEbitda'))}"
    )
    # Unlike the margins above, yfinance gives dividendYield in percent already
    # (5.55 for DNB at 5.55%), so it is not multiplied by 100 (#127).
    dividend_yield = _as_float(info.get("dividendYield"))
    lines.append(f"Dividend yield {'n/a' if dividend_yield is None else f'{dividend_yield:.1f}%'}")
    low_52 = _as_float(info.get("fiftyTwoWeekLow"))
    high_52 = _as_float(info.get("fiftyTwoWeekHigh"))
    if metrics and low_52 is not None and high_52 is not None and high_52 > low_52:
        position_pct = (metrics.last_close - low_52) / (high_52 - low_52) * 100
        lines.append(
            f"Trading at {position_pct:.0f}% of its 52-week range ({low_52:,.2f}–{high_52:,.2f})."
        )
    lines.append(
        "<i>No historical multiple series or peer set is available in this bot — the "
        "52-week range above is the only own-history comparison possible.</i>"
    )
    lines.append("")

    # 4. Profitability
    lines.append("<b>Profitability</b>")
    lines.append(
        f"Gross margin {_fmt_pct(info.get('grossMargins'))}"
        f" · Operating margin {_fmt_pct(info.get('operatingMargins'))}"
        f" · Net margin {_fmt_pct(info.get('profitMargins'))}"
    )
    lines.append(
        f"ROE {_fmt_pct(info.get('returnOnEquity'))} · ROA {_fmt_pct(info.get('returnOnAssets'))}"
    )
    lines.append("")

    # 5. Balance sheet
    lines.append("<b>Balance Sheet</b>")
    lines.append(
        f"Cash {_fmt_large(info.get('totalCash'))} · Debt {_fmt_large(info.get('totalDebt'))}"
        f" · Debt/Equity {_fmt_num(info.get('debtToEquity'))}"
        " (as reported by the data provider; conventions vary by ticker)"
    )
    lines.append(
        f"Current ratio {_fmt_num(info.get('currentRatio'))}"
        f" · Quick ratio {_fmt_num(info.get('quickRatio'))}"
    )
    lines.append("")

    # 6. Cash flow
    lines.append("<b>Cash Flow</b>")
    fcf = _as_float(info.get("freeCashflow"))
    net_income = _as_float(info.get("netIncomeToCommon"))
    lines.append(
        f"Free cash flow {_fmt_large(fcf)} · Operating cash flow {_fmt_large(info.get('operatingCashflow'))}"
        f" · Net income {_fmt_large(net_income)}"
    )
    if fcf is not None and net_income and net_income > 0 and fcf < net_income * 0.5:
        lines.append(
            "<i>Free cash flow is materially below reported net income — worth checking why "
            "(working capital, capex, one-time items) before trusting earnings alone.</i>"
        )
    lines.append("")

    # 7. Risk read (beta + what the price/fundamentals data already flags)
    lines.append("<b>Risk</b>")
    beta = _as_float(info.get("beta"))
    lines.append(f"Beta {_fmt_num(beta)}{_beta_read(beta)}")
    debt_to_equity = _as_float(info.get("debtToEquity"))
    risk_flags = []
    if debt_to_equity is not None and debt_to_equity > 150:
        risk_flags.append("elevated debt/equity")
    if metrics and metrics.rsi_14 is not None and metrics.rsi_14 >= 70:
        risk_flags.append("RSI overbought")
    if metrics and metrics.atr_pct > 4.0:
        risk_flags.append("high daily volatility")
    if fcf is not None and fcf < 0:
        risk_flags.append("negative free cash flow")
    lines.append("Flags: " + (", ".join(risk_flags) if risk_flags else "none from the data available"))
    lines.append("")

    # 8. Bull / base / bear -- technical read only; no fundamentals-driven
    # scenario is attempted without a peer set or historical multiple series.
    lines.append("<b>Bull / Base / Bear (technical read only)</b>")
    if metrics:
        lines.append(
            f"<b>Bull:</b> holds above its 20-day EMA ({metrics.ema20:,.2f}) and clears "
            f"{metrics.resistance_level:,.2f} on rising volume."
        )
        lines.append(
            f"<b>Base:</b> continues to range between {metrics.support_level:,.2f} and "
            f"{metrics.resistance_level:,.2f}."
        )
        lines.append(
            f"<b>Bear:</b> a daily close below {metrics.support_level:,.2f} invalidates this "
            "setup and opens further downside."
        )
    else:
        lines.append("Not enough price history to sketch scenarios.")
    lines.append(
        "<i>Ranges implied by the last 20 sessions' price action, not predictions — they say "
        "nothing about fundamentals, macro, or news risk.</i>"
    )
    lines.append("")

    # 9. Explicit gaps, per the framework's own rule against inventing numbers.
    lines.append("<b>Not available in this bot</b>")
    lines.append(
        "Macro series (rates, inflation, GDP), options positioning, institutional 13F flow, "
        "and insider transaction feeds have no data source configured here — not fabricated, "
        "just absent."
    )
    lines.append("")
    lines.append(
        "<i>Informational only. Not investment advice. Data via yfinance; fundamentals fields "
        "are frequently incomplete for smaller or non-US tickers.</i>"
    )
    return "\n".join(lines) + "\n\n"


def build_health_report() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    latency = RANKER_STATUS.get("latency_ms")
    lines = [
        "✅ Bot Health Check",
        f"Time: {now}",
        f"Timezone: {SETTINGS.tz}",
        f"Last run status: {STATE.data.get('last_run_status', 'unknown')}",
        f"Ranker path: {RANKER_STATUS.get('path', 'unknown')}",
        f"Ranker model: {SETTINGS.news_ranker_model}",
        f"Ranker latency: {latency if latency is not None else 'n/a'} ms",
        f"Slack mirror: {'configured' if SETTINGS.slack_webhook_url else 'not configured'}",
        data_health.describe(),
    ]
    if RANKER_STATUS.get("error"):
        lines.append(
            f"Last ranker error: {RANKER_STATUS['error']} at {RANKER_STATUS.get('error_at')}"
        )
    return "\n".join(lines) + "\n"
