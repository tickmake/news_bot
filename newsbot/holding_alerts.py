"""Evening Telegram alert for stocks I own (#85).

Sent only when something needs a look: a big fall below the buy price,
earnings coming up, or a headline about the company. Each alert is sent
once, so a stock that stays down does not repeat every evening. The bot
never sells anything.
"""

from datetime import date
from html import escape
from typing import Dict, List, Optional

from newsbot import catalysts, fills, market_data, messaging, risk
from newsbot.config import SETTINGS
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.state import STATE

# Remember this many sent alerts; older ones may repeat, which is harmless.
_MAX_REMEMBERED = 300


def _company_name(symbol: str) -> str:
    info = market_data._fetch_ticker_fundamentals(symbol)
    return info.get("longName") or info.get("shortName") or ""


def find_alerts(today: Optional[date] = None) -> List[tuple]:
    """(key, text) for each thing worth saying today, before de-duplication."""
    today = today or date.today()
    holdings = fills.current_holdings()
    alerts: List[tuple] = []
    if not holdings:
        return alerts
    names: Dict[str, str] = {symbol: _company_name(symbol) for symbol in holdings}
    for symbol, held in sorted(holdings.items()):
        average = held["cost_local"] / held["shares"]
        price = fills._latest_price(symbol)
        if price is not None:
            fall = (price / average - 1.0) * 100.0
            if fall <= -SETTINGS.holding_alert_drop_pct:
                alerts.append((
                    f"drop:{symbol}:{average:.4f}",
                    f"📉 <b>{escape(symbol)}</b> is {fall:.0f}% below your average buy price "
                    f"({price:,.2f} vs {average:,.2f} {held['currency']}).",
                ))
        flag = risk.earnings_flag(market_data._fetch_event_profile(symbol), today)
        if flag is not None:
            alerts.append((
                f"earnings:{symbol}:{flag.on.isoformat()}",
                f"📅 <b>{escape(symbol)}</b>: {escape(flag.text)}. The price can jump either way.",
            ))
    for symbol, headlines in catalysts.find(list(holdings), names).items():
        for headline in headlines:
            alerts.append((f"news:{symbol}:{headline.title}", f"<b>{escape(symbol)}</b>: {headline.text}"))
    return alerts


def send_holding_alerts(today: Optional[date] = None) -> int:
    """Send the new alerts as one Telegram message. Returns how many were sent."""
    sent_keys: List[str] = list(STATE.data.get("holding_alerts_sent") or [])
    fresh = [(key, text) for key, text in find_alerts(today) if key not in sent_keys]
    if not fresh:
        return 0
    message = "\n".join(
        ["<b>Stocks you own: worth a look</b>", ""]
        + [text for _, text in fresh]
        + ["", "<i>Information only. The bot never sells anything. See /portfolio.</i>"]
    )
    if not messaging.send_telegram_message(message):
        return 0
    STATE.data["holding_alerts_sent"] = (sent_keys + [key for key, _ in fresh])[-_MAX_REMEMBERED:]
    STATE.save()
    return len(fresh)


def job_holding_alerts() -> bool:
    """Evening wrapper. Never raises."""
    try:
        count = send_holding_alerts()
    except Exception as exc:
        SYS_LOG.error("holding_alerts_failed detail=%s", exc)
        return False
    APP_LOG.info("holding_alerts sent=%d", count)
    return True
