"""/check TICKER AMOUNT: everything worth knowing before placing an order (#121).

It joins pieces the bot already has -- the DNB fee, the ASK rule, the next
report and ex-dividend dates, overlap with what I hold, the volatility stop,
how much the stock can fall (#123) and the position's share of my money --
into one Telegram message. It warns in plain words and never says buy or
don't buy: the screen has no shown edge, so the decision stays mine.
"""

import math
from datetime import date, datetime
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from newsbot import fills, indicators, market_data, risk, risk_forecast, stock_list
from newsbot.config import SETTINGS
from newsbot.logsetup import SYS_LOG

USAGE = (
    "Usage: /check TICKER AMOUNT_NOK\n"
    "Example: /check EQNR.OL 12000\n"
    "Shows fees, ASK, upcoming dates, overlap with what you hold, risk and size before you buy."
)

# A 2-4 week hold (#122), so anything in the next 4 weeks is inside it.
EVENT_DAYS = 28
# From this round-trip fee on, mark it: a 20-session hold earned ~1.5% before fees (#122).
FEE_WARN_PCT = 1.0
# Above this, even the size the risk rule allows is too small for the 59 NOK minimum.
SMALL_SIZE_FEE_PCT = 1.5


def _parse(argument: str) -> Tuple[str, float]:
    words = argument.split()
    if len(words) != 2:
        raise ValueError(USAGE)
    symbol = words[0].upper()
    try:
        amount = float(words[1].replace(",", "").replace("_", ""))
    except ValueError:
        raise ValueError(f"'{words[1]}' is not an amount in NOK.\n\n{USAGE}") from None
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError(f"The amount must be more than 0 NOK.\n\n{USAGE}")
    return symbol, amount


def _sector(symbol: str, profile: Optional[Dict[str, Any]] = None) -> Optional[str]:
    row = stock_list._by_ticker().get(symbol)
    if row is not None:
        return row["sector"]
    return (profile or {}).get("sector")


def _profile(symbol: str) -> Dict[str, Any]:
    try:
        return market_data._fetch_event_profile(symbol) or {}
    except Exception as exc:
        SYS_LOG.debug("check_profile_failed symbol=%s detail=%s", symbol, exc)
        return {}


def _days_away(day: Optional[date], today: date) -> Optional[int]:
    return (day - today).days if isinstance(day, date) else None


def _event_lines(symbol: str, profile: Dict[str, Any], forecast: Optional[risk_forecast.Forecast],
                 today: date) -> List[str]:
    lines = []
    earnings = profile.get("next_earnings")
    days = _days_away(earnings, today)
    if days is not None and 0 <= days <= EVENT_DAYS:
        moved = (f" Its report days moved ±{forecast.earnings_move_pct:.1f}% on average."
                 if forecast is not None and forecast.earnings_move_pct is not None else "")
        # Information, not a warning: skipping stocks with a report in the
        # first week of the hold did not cut the bad months (#125).
        lines.append(f"    Earnings report in {days} days ({earnings:%b %d}), inside a 2-4 week hold.{moved}")
    elif days is not None:
        lines.append(f"    Next earnings report: {earnings:%b %d, %Y}, after a 4-week hold.")
    else:
        lines.append("    Next earnings report: not known.")
    try:
        ex_dividend = market_data._next_ex_dividend_date(symbol)
    except Exception as exc:
        SYS_LOG.debug("check_dividend_failed symbol=%s detail=%s", symbol, exc)
        ex_dividend = None
    days = _days_away(ex_dividend, today)
    if days is not None and 0 <= days <= EVENT_DAYS:
        lines.append(
            f"    Ex-dividend in {days} days ({ex_dividend:%b %d}): the price drops by the dividend "
            "that morning. You get the dividend only if you own it the day before.")
    return lines


def _overlap_lines(symbol: str, sector: Optional[str], holdings: Dict[str, Dict[str, Any]]) -> List[str]:
    if not holdings:
        return ["    You hold nothing yet."]
    lines = []
    if symbol in holdings:
        lines.append(f"    You already hold {holdings[symbol]['shares']} shares of {escape(symbol)}.")
    others = [held for held in sorted(holdings) if held != symbol]
    same = [held for held in others if sector and _sector(held, _profile(held)) == sector]
    if same:
        # The one portfolio rule that held up in testing (#125).
        lines.append(f"    ⚠️ Same sector ({escape(sector)}) as {escape(', '.join(same))}. One stock per "
                     "sector made the worst months a little less bad in ten years of tests.")
    try:
        pairs = risk.correlated_pairs([symbol, *others]) if others else []
    except Exception as exc:
        SYS_LOG.debug("check_correlation_failed symbol=%s detail=%s", symbol, exc)
        pairs = []
    for first, second, value, _sessions in pairs:
        if symbol in (first, second):
            other = second if first == symbol else first
            # Information, not a warning: avoiding such pairs did not cut the
            # bad months (#125); fewer than 1 in 100 pairs move this closely.
            lines.append(f"    Moves together with {escape(other)} (correlation {value:.2f}).")
    if not lines:
        lines.append(f"    No overlap with what you hold ({escape(', '.join(others))}).")
    return lines


def _account_value() -> Tuple[Optional[float], Optional[float]]:
    """(account total in NOK, cash in NOK), from /fill and /deposit. None when unknown."""
    rows = fills.trade_rows()
    holdings, _realised, cash_change = fills._holdings(rows)
    money_in = fills.deposits(rows)
    if money_in:
        cash = sum(amount for _, amount in money_in) + cash_change
        value, unpriced = fills.holdings_value(holdings)
        return (None if unpriced else cash + value), cash
    if SETTINGS.risk_account_size > 0:
        return float(SETTINGS.risk_account_size), float(SETTINGS.risk_account_size)
    return None, None


def build_check(argument: str, today: Optional[date] = None) -> str:
    try:
        symbol, amount = _parse(argument)
    except ValueError as exc:
        return escape(str(exc), quote=False)
    today = today or datetime.now().date()

    metrics = indicators._compute_trade_metrics(symbol)
    if metrics is None:
        return f"No prices for {escape(symbol)}. Check the ticker: Oslo stocks end in .OL, e.g. EQNR.OL."
    currency = fills.currency_for(symbol)
    if currency is None:
        return f"{escape(symbol)}: this exchange isn't supported yet (no known currency)."
    fx = 1.0 if currency == "NOK" else fills._latest_price(f"{currency}NOK=X")
    if fx is None:
        return f"No {currency}/NOK rate available right now, so the size can't be worked out. Try again later."
    price_nok = metrics.last_close * fx
    shares = math.floor(amount / price_nok)
    if shares < 1:
        return (f"One share of {escape(symbol)} costs about {price_nok:,.0f} NOK, "
                f"more than {amount:,.0f} NOK.")
    cost = shares * price_nok

    lines = [f"<b>Check: {escape(symbol)} for {amount:,.0f} NOK</b>", ""]
    price_text = f"{metrics.last_close:,.2f} {currency}"
    if currency != "NOK":
        price_text += f" ≈ {price_nok:,.2f} NOK"
    lines.append(f"{shares} shares at {price_text} = {cost:,.0f} NOK (close of {escape(metrics.session)})")
    lines.append("")

    total, cash = _account_value()
    # The same calculation /recommendations uses, for the amount asked about.
    trade = risk.trade_risk(metrics, exposure_nok=cost, account_nok=total)

    lines.append("<b>Fees</b>")
    lines.append(risk.fee_line(symbol, cost, total))
    fee_pct = risk.round_trip_fee(cost) / cost * 100
    mark = "⚠️ " if fee_pct >= FEE_WARN_PCT else ""
    lines.append(f"    {mark}The stock must rise {fee_pct:.1f}% before you make anything. "
                 "A 4-week hold made about 1.5% on average before fees in the 10-year test.")
    lines.append("")

    lines.append("<b>ASK</b>")
    note = stock_list.ask_note(symbol)
    lines.append(f"    ⛔ {escape(note)}" if note else "    ✅ Allowed in an Aksjesparekonto.")
    lines.append("")

    profile = _profile(symbol)
    forecast = risk_forecast.build(symbol)
    lines.append("<b>Dates in the next 4 weeks</b>")
    lines.extend(_event_lines(symbol, profile, forecast, today))
    lines.append("")

    lines.append("<b>Overlap with what you hold</b>")
    lines.extend(_overlap_lines(symbol, _sector(symbol, profile), fills.current_holdings()))
    lines.append("")

    lines.append("<b>Risk</b>")
    sized = risk.size_position(metrics)
    if trade is not None:
        lines.append(risk.stop_line(trade, currency))
        lines.extend(risk.loss_lines(trade, "your money"))
        lines.extend(risk.warning_lines(trade))
    else:
        lines.append("    No stop: the stock's daily swing (ATR) isn't available.")
    lines.extend(risk_forecast.lines(forecast, cost))
    lines.append("")

    lines.append("<b>Size</b>")
    if total:
        share = cost / total * 100
        text = f"    {share:.0f}% of your money ({total:,.0f} NOK)."
        suggested = total * sized.weight_pct / 100 if sized is not None else None
        if suggested:
            text += f" The risk rule suggests about {suggested:,.0f} NOK."
        lines.append(text)
        if suggested:
            suggested_fee = risk.round_trip_fee(suggested)
            if suggested_fee / suggested * 100 > SMALL_SIZE_FEE_PCT:
                lines.append(
                    f"    ⚠️ At that size the fee is {suggested_fee / suggested * 100:.1f}% round trip: "
                    "this stock swings too much for a position the fee doesn't eat. A calmer stock fits better.")
        if SETTINGS.risk_max_position_pct > 0 and share > SETTINGS.risk_max_position_pct:
            lines.append(f"    ⚠️ More than the {SETTINGS.risk_max_position_pct:g}% limit for one stock.")
    else:
        lines.append("    Your account size isn't known. Add it with /deposit.")
    if cash is not None and cost > cash:
        lines.append(f"    ⚠️ More than your cash ({cash:,.0f} NOK).")
    lines.append("")
    lines.append(f"<i>The stop and the losses are worked out from the {escape(metrics.session)} close, "
                 "not from your buy price, which isn't known yet. Paying more than that close makes "
                 f"the actual loss larger. {risk.gap_note()} Fees are estimates; spread, slippage and "
                 "currency exchange cost are not included.</i>")
    lines.append("<i>Information only, not a buy or sell signal. Prices may be delayed.</i>")
    return "\n".join(lines)
