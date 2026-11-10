"""Monthly scorecard: my real trades against the index fund (#86).

The fair comparison: every sum put into the account could have bought the
index fund on the same day instead. So each deposit buys fund units at that
day's price, and the units are valued at today's price. The fund's yearly
fee is already inside its price.
"""

from datetime import date
from typing import Optional

from newsbot import fills, messaging
from newsbot.config import SETTINGS
from newsbot.logsetup import APP_LOG, SYS_LOG


def _fund_value(money_in) -> Optional[float]:
    """What the deposits would be worth today in the fund, or None without prices."""
    today_price = fills._close_on_or_before(SETTINGS.benchmark_fund_symbol, date.today(), period="5y")
    if today_price is None:
        return None
    value = 0.0
    for day, amount in money_in:
        price = fills._close_on_or_before(
            SETTINGS.benchmark_fund_symbol, date.fromisoformat(day), period="5y")
        if price is None:
            return None
        value += amount / price * today_price
    return value


def build_scorecard() -> Optional[str]:
    """The scorecard text, or None when there is nothing real to report yet."""
    rows = fills.trade_rows()
    money_in = fills.deposits(rows)
    put_in = sum(amount for _, amount in money_in)
    if not rows or put_in <= 0:
        return None
    holdings, _realised, cash_change = fills._holdings(rows)
    shares_value, unpriced = fills.holdings_value(holdings)
    fees = sum(row["fee_nok"] for row in rows)
    lines = [f"<b>Scorecard: my trades vs {SETTINGS.benchmark_fund_name}</b>", ""]
    if unpriced:
        lines.append(f"Can't work it out today: no price for {', '.join(unpriced)}.")
        return "\n".join(lines)
    mine = put_in + cash_change + shares_value
    my_change = mine - put_in
    lines.append(f"Money put in: {put_in:,.0f} NOK, first on {money_in[0][0]}")
    lines.append(f"My account now: {mine:,.0f} NOK, {my_change:+,.0f} NOK ({my_change / put_in * 100:+.1f}%) after all fees")
    fund = _fund_value(money_in)
    if fund is None:
        lines.append(f"Index fund: no price found for {SETTINGS.benchmark_fund_symbol}.")
    else:
        fund_change = fund - put_in
        lines.append(
            f"Same money in the fund: {fund:,.0f} NOK, {fund_change:+,.0f} NOK "
            f"({fund_change / put_in * 100:+.1f}%)"
        )
        gap = mine - fund
        verdict = "ahead of" if gap > 0 else "behind"
        lines.append(f"<b>You are {abs(gap):,.0f} NOK {verdict} the index fund.</b>")
    lines.append(f"Fees paid so far: {fees:,.0f} NOK in {len(rows)} trades")
    return "\n".join(lines)


def scorecard_reply() -> str:
    return build_scorecard() or "No scorecard yet: save trades with /fill and money put in with /deposit."


def job_monthly_scorecard() -> bool:
    """First day of the month. Silent until there are real trades. Never raises."""
    try:
        text = build_scorecard()
        if text is None:
            return True
        sent = messaging.send_telegram_message(text)
    except Exception as exc:
        SYS_LOG.error("monthly_scorecard_failed detail=%s", exc)
        return False
    APP_LOG.info("monthly_scorecard sent=%s", sent)
    return bool(sent)
