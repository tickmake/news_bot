import os
import hashlib
from datetime import datetime
from html import escape
from typing import Any, Dict, Iterable, List, Optional, Tuple

from apscheduler.schedulers.blocking import BlockingScheduler
import requests
import requests.exceptions
import yfinance as yf

os.environ["YF_NO_CURL_CFFI"] = "1"

# Keep compatibility for older yfinance/requests combinations.
if not hasattr(requests.exceptions, "DNSError"):
    requests.exceptions.DNSError = requests.exceptions.ConnectionError

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Sunil").strip() or "Sunil"
NORWAY_NEWS_QUERY = os.getenv("NORWAY_NEWS_QUERY", "Norway OR Norge")

REQUEST_TIMEOUT_SECONDS = 15

USA_STOCK_UNIVERSE = {
    "Apple": "AAPL",
    "Microsoft": "MSFT",
    "NVIDIA": "NVDA",
    "Amazon": "AMZN",
    "Alphabet": "GOOGL",
    "Meta": "META",
    "Tesla": "TSLA",
    "JPMorgan": "JPM",
    "Exxon Mobil": "XOM",
    "Visa": "V",
}

INDIA_STOCK_UNIVERSE = {
    "Reliance": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "HDFC Bank": "HDFCBANK.NS",
    "ICICI Bank": "ICICIBANK.NS",
    "Infosys": "INFY.NS",
    "SBI": "SBIN.NS",
    "Bharti Airtel": "BHARTIARTL.NS",
    "L&T": "LT.NS",
    "ITC": "ITC.NS",
    "Hindustan Unilever": "HINDUNILVR.NS",
}

NORWAY_STOCK_UNIVERSE = {
    "Equinor": "EQNR.OL",
    "DNB Bank": "DNB.OL",
    "Yara": "YAR.OL",
    "Mowi": "MOWI.OL",
    "Aker BP": "AKRBP.OL",
    "Orkla": "ORK.OL",
    "Norsk Hydro": "NHY.OL",
    "SalMar": "SALM.OL",
    "Gjensidige": "GJF.OL",
    "Telenor": "TEL.OL",
}

# Yahoo Finance coverage for Norway mutual funds is limited.
# We include Norway-focused fund instruments available on Yahoo.
INDIA_MUTUAL_FUNDS = {
    "Nippon Nifty BeES": "NIFTYBEES.NS",
    "Nippon Junior BeES": "JUNIORBEES.NS",
    "Nippon Bank BeES": "BANKBEES.NS",
    "Nippon Gold BeES": "GOLDBEES.NS",
    "SBI Nifty 50 ETF": "SETFNIF50.NS",
}

NORWAY_MUTUAL_FUNDS = {
    "iShares MSCI Norway ETF": "ENOR",
    "Global X Norway ETF": "NORW",
}

MORNING_GREETINGS = [
    "🌞 Good morning! Wishing you a focused and positive day ahead.",
    "☀️ Rise and shine! Here's your fresh market and news briefing.",
    "🌅 New day, new opportunities. Let's begin with clarity.",
    "💪 Good morning! Small consistent steps create big outcomes.",
    "🚀 Morning update is ready. Make today count.",
]

EVENING_GREETINGS = [
    "🌙 Good evening! Here's your market wrap and highlights.",
    "✨ Good evening! Time to reflect and reset for tomorrow.",
    "🕯️ Evening briefing is here. Hope your day went well.",
    "🌆 Good evening! Closing the day with key updates.",
    "📘 Evening check-in ready. End the day with perspective.",
]

INSPIRATIONAL_QUOTES = [
    "Success is the sum of small efforts, repeated day in and day out.",
    "Discipline is choosing between what you want now and what you want most.",
    "The future depends on what you do today.",
    "Do not wait; the time will never be just right.",
    "Great things are done by a series of small things brought together.",
    "Consistency beats intensity when intensity is not sustainable.",
    "Focus on progress, not perfection.",
    "Well begun is half done.",
]


def _parse_instrument_env(raw_value: Optional[str], fallback: Dict[str, str]) -> Dict[str, str]:
    """
    Parses a comma-separated list of `Label:SYMBOL` pairs.
    Example:
    "Apple:AAPL,Microsoft:MSFT"
    """
    if not raw_value:
        return dict(fallback)

    parsed: Dict[str, str] = {}
    for item in raw_value.split(","):
        pair = item.strip()
        if not pair:
            continue
        if ":" in pair:
            label, symbol = pair.split(":", 1)
            label = label.strip()
            symbol = symbol.strip()
            if label and symbol:
                parsed[label] = symbol
        else:
            # Allow providing only symbols; use symbol as label.
            symbol = pair.strip()
            if symbol:
                parsed[symbol] = symbol

    return parsed or dict(fallback)


USA_STOCK_UNIVERSE = _parse_instrument_env(os.getenv("USA_STOCK_UNIVERSE"), USA_STOCK_UNIVERSE)
INDIA_STOCK_UNIVERSE = _parse_instrument_env(os.getenv("INDIA_STOCK_UNIVERSE"), INDIA_STOCK_UNIVERSE)
NORWAY_STOCK_UNIVERSE = _parse_instrument_env(os.getenv("NORWAY_STOCK_UNIVERSE"), NORWAY_STOCK_UNIVERSE)
INDIA_MUTUAL_FUNDS = _parse_instrument_env(os.getenv("INDIA_MUTUAL_FUNDS"), INDIA_MUTUAL_FUNDS)
NORWAY_MUTUAL_FUNDS = _parse_instrument_env(os.getenv("NORWAY_MUTUAL_FUNDS"), NORWAY_MUTUAL_FUNDS)


def _format_headline_line(index: int, title: str, url: Optional[str]) -> str:
    safe_title = escape(title)
    if not url:
        return f"{index}. {safe_title}"
    safe_url = escape(url, quote=True)
    return f'{index}. {safe_title} (<a href="{safe_url}">more</a>)'


def _daily_pick(options: List[str], context: str, date_key: Optional[str] = None) -> str:
    if not options:
        return ""
    if not date_key:
        date_key = datetime.today().strftime("%Y-%m-%d")
    digest = hashlib.sha256(f"{date_key}:{context}".encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(options)
    return options[index]


def build_daily_intro(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    date_key = now.strftime("%Y-%m-%d")
    recipient = escape(RECIPIENT_NAME)

    if now.hour < 12:
        greeting = _daily_pick(MORNING_GREETINGS, "morning", date_key)
        quote = _daily_pick(INSPIRATIONAL_QUOTES, "quote", date_key)
        return (
            f"{escape(greeting)}\n"
            f"Hello, {recipient}!\n"
            f"📅 {date_key}\n"
            f"💡 Quote of the day: <i>{escape(quote)}</i>\n"
        )

    greeting = _daily_pick(EVENING_GREETINGS, "evening", date_key)
    return f"{escape(greeting)}\nHello, {recipient}!\n📅 {date_key}\n"


def _format_performance_line(name: str, symbol: str, close_price: float, pct_change: float) -> str:
    sign = "+" if pct_change >= 0 else ""
    return f"{escape(name)} ({escape(symbol)}): {close_price:,.2f} ({sign}{pct_change:.2f}%)"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return text[:max_len]
    return text[: max_len - 1] + "…"


def _render_pre_table(title: str, headers: List[str], rows: List[List[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    divider = "-+-".join("-" * width for width in widths)
    header_row = " | ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers))

    body_rows: List[str] = []
    for row in rows:
        body_rows.append(" | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)))

    table_lines = [header_row, divider, *body_rows]
    table_text = "\n".join(table_lines)
    return f"{escape(title)}\n<pre>{escape(table_text)}</pre>"


def _compute_weekly_change(symbol: str) -> Optional[Tuple[float, float]]:
    try:
        history = yf.Ticker(symbol).history(period="7d", interval="1d")
        close_series = history["Close"]
        if hasattr(close_series, "dropna"):
            close_series = close_series.dropna()
        if len(close_series) < 2:
            return None
        first_close = float(close_series.iloc[0])
        last_close = float(close_series.iloc[-1])
        if first_close == 0:
            return None
        pct_change = ((last_close - first_close) / first_close) * 100
        return last_close, pct_change
    except Exception:
        return None


def _top_weekly_performers(instruments: Dict[str, str], top_n: int = 3) -> List[str]:
    scored: List[Tuple[float, str, str, float]] = []
    for name, symbol in instruments.items():
        result = _compute_weekly_change(symbol)
        if not result:
            continue
        close_price, pct_change = result
        scored.append((pct_change, name, symbol, close_price))

    scored.sort(key=lambda item: item[0], reverse=True)
    lines = []
    for pct_change, name, symbol, close_price in scored[:top_n]:
        lines.append(_format_performance_line(name, symbol, close_price, pct_change))
    return lines


def _top_weekly_performers_data(
    instruments: Dict[str, str], top_n: int = 3
) -> List[Tuple[str, str, float, float]]:
    scored: List[Tuple[float, str, str, float]] = []
    for name, symbol in instruments.items():
        result = _compute_weekly_change(symbol)
        if not result:
            continue
        close_price, pct_change = result
        scored.append((pct_change, name, symbol, close_price))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [(name, symbol, close_price, pct_change) for pct_change, name, symbol, close_price in scored[:top_n]]


def _analyze_short_term_candidate(symbol: str) -> Optional[Dict[str, float]]:
    """
    Compute simple momentum/participation metrics for short-term candidate ranking.
    This is a technical screener only, not a prediction or guarantee.
    """
    try:
        history = yf.Ticker(symbol).history(period="2mo", interval="1d")
        close_series = history["Close"].dropna()
        if len(close_series) < 21:
            return None

        last_close = float(close_series.iloc[-1])
        prev_close = float(close_series.iloc[-2])
        if prev_close == 0:
            return None

        day_change_pct = ((last_close - prev_close) / prev_close) * 100

        reference_index = -6 if len(close_series) >= 6 else 0
        reference_close = float(close_series.iloc[reference_index])
        if reference_close == 0:
            return None
        week_momentum_pct = ((last_close - reference_close) / reference_close) * 100

        ema_20 = float(close_series.ewm(span=20, adjust=False).mean().iloc[-1])
        trend_ok = last_close > ema_20

        volume_ratio = 1.0
        if "Volume" in history:
            volume_series = history["Volume"].dropna()
            if len(volume_series) >= 20:
                avg_volume_20 = float(volume_series.tail(20).mean())
                if avg_volume_20 > 0:
                    volume_ratio = float(volume_series.iloc[-1]) / avg_volume_20

        score = 0
        if trend_ok:
            score += 1
        if week_momentum_pct >= 1.0:
            score += 1
        if day_change_pct > 0:
            score += 1
        if volume_ratio >= 1.2:
            score += 1

        if score < 3:
            return None

        return {
            "score": float(score),
            "last_close": last_close,
            "day_change_pct": day_change_pct,
            "week_momentum_pct": week_momentum_pct,
            "volume_ratio": volume_ratio,
        }
    except Exception:
        return None


def get_trade_candidates(universe: Optional[Dict[str, str]] = None, top_n: int = 5) -> str:
    if universe is None:
        universe = {
            **{f"{name} [India Stock]": sym for name, sym in INDIA_STOCK_UNIVERSE.items()},
            **{f"{name} [USA Stock]": sym for name, sym in USA_STOCK_UNIVERSE.items()},
            **{f"{name} [Norway Stock]": sym for name, sym in NORWAY_STOCK_UNIVERSE.items()},
            **{f"{name} [India Fund]": sym for name, sym in INDIA_MUTUAL_FUNDS.items()},
            **{f"{name} [Norway Fund]": sym for name, sym in NORWAY_MUTUAL_FUNDS.items()},
        }

    scored: List[Tuple[float, float, float, str, str, Dict[str, float]]] = []
    for name, symbol in universe.items():
        metrics = _analyze_short_term_candidate(symbol)
        if not metrics:
            continue
        scored.append(
            (
                metrics["score"],
                metrics["week_momentum_pct"],
                metrics["day_change_pct"],
                name,
                symbol,
                metrics,
            )
        )

    lines = [
        "⚠️ Short-Term Trade Candidates (Informational Only):",
        "No guarantee of 1-2% daily profit. Use strict risk management.",
    ]
    if not scored:
        lines.append("No candidates met the momentum criteria today.")
        return "\n".join(lines) + "\n\n"

    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)

    table_rows: List[List[str]] = []
    for idx, (_, _, _, name, symbol, metrics) in enumerate(scored[:top_n], 1):
        table_rows.append(
            [
                str(idx),
                _truncate(name, 26),
                symbol,
                f"{metrics['last_close']:.2f}",
                f"{metrics['day_change_pct']:+.2f}%",
                f"{metrics['week_momentum_pct']:+.2f}%",
                f"{metrics['volume_ratio']:.2f}",
                f"{int(metrics['score'])}",
            ]
        )
    lines.append(
        _render_pre_table(
            "Candidates",
            ["#", "Name", "Symbol", "Close", "1D", "5D", "Volx", "Score"],
            table_rows,
        )
    )
    return "\n".join(lines) + "\n\n"


def get_missing_required_config() -> Iterable[str]:
    required = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "NEWS_API_KEY": NEWS_API_KEY,
    }
    return [name for name, value in required.items() if not value]


def _story_title_and_link(story: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    # yfinance returns different shapes depending on version/source.
    title = story.get("title")
    link = story.get("link")
    content = story.get("content")
    if isinstance(content, dict):
        title = title or content.get("title")
        canonical_url = content.get("canonicalUrl")
        if isinstance(canonical_url, dict):
            link = link or canonical_url.get("url")
        click_url = content.get("clickThroughUrl")
        if isinstance(click_url, dict):
            link = link or click_url.get("url")
    return title, link


def get_global_news() -> str:
    if not NEWS_API_KEY:
        return "🌍 Top Global News:\nNews API key is not configured.\n\n"

    try:
        response = requests.get(
            "https://newsapi.org/v2/top-headlines",
            params={"language": "en", "pageSize": 5, "apiKey": NEWS_API_KEY},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        articles = payload.get("articles", [])[:5]
        if not articles:
            return "🌍 Top Global News:\nNo headlines available right now.\n\n"

        lines = ["🌍 Top Global News:"]
        for idx, article in enumerate(articles, 1):
            title = article.get("title", "Untitled")
            url = article.get("url")
            lines.append(_format_headline_line(idx, title, url))
        return "\n".join(lines) + "\n\n"
    except Exception as exc:
        return f"🌍 Top Global News:\nFailed to fetch ({exc}).\n\n"


def get_norwegian_morning_news() -> str:
    if not NEWS_API_KEY:
        return "🇳🇴 Early Morning Norway News:\nNews API key is not configured.\n\n"

    try:
        response = requests.get(
            "https://newsapi.org/v2/top-headlines",
            params={
                "country": "no",
                "q": NORWAY_NEWS_QUERY,
                "pageSize": 5,
                "apiKey": NEWS_API_KEY,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        articles = payload.get("articles", [])[:5]
        if not articles:
            return "🇳🇴 Early Morning Norway News:\nNo Norwegian headlines available right now.\n\n"

        lines = ["🇳🇴 Early Morning Norway News:"]
        for idx, article in enumerate(articles, 1):
            title = article.get("title", "Untitled")
            url = article.get("url")
            lines.append(_format_headline_line(idx, title, url))
        return "\n".join(lines) + "\n\n"
    except Exception as exc:
        return f"🇳🇴 Early Morning Norway News:\nFailed to fetch ({exc}).\n\n"


def get_business_and_stocks() -> str:
    try:
        market = yf.Ticker("SPY")
        raw_news = (market.news or [])[:10]
        biz_lines = ["💼 Top 10 Business Stories:"]
        for idx, story in enumerate(raw_news, 1):
            title, link = _story_title_and_link(story)
            if not title:
                continue
            biz_lines.append(_format_headline_line(idx, title, link))
        if len(biz_lines) == 1:
            biz_lines.append("No business headlines available right now.")
        biz_str = "\n".join(biz_lines)
    except Exception as exc:
        biz_str = f"💼 Top 10 Business Stories:\nFailed to fetch ({exc})."

    stock_lines = ["📈 Market Watch - Top Weekly Gainers:"]
    stock_markets = {
        "India": INDIA_STOCK_UNIVERSE,
        "USA": USA_STOCK_UNIVERSE,
        "Norway": NORWAY_STOCK_UNIVERSE,
    }
    stock_rows: List[List[str]] = []
    for market_name, universe in stock_markets.items():
        gainers = _top_weekly_performers_data(universe, top_n=3)
        for name, symbol, close_price, pct_change in gainers:
            stock_rows.append(
                [
                    market_name,
                    _truncate(name, 20),
                    symbol,
                    f"{close_price:.2f}",
                    f"{pct_change:+.2f}%",
                ]
            )
    if stock_rows:
        stock_lines.append(
            _render_pre_table(
                "Top 3 gainers per market (7D)",
                ["Market", "Name", "Symbol", "Close", "7D"],
                stock_rows,
            )
        )
    else:
        stock_lines.append("Data temporarily unavailable")
    stock_str = "\n".join(stock_lines)

    fund_lines = ["🏦 Most Attractive Mutual Funds - Weekly:"]
    fund_markets = {
        "India": INDIA_MUTUAL_FUNDS,
        "Norway": NORWAY_MUTUAL_FUNDS,
    }
    fund_rows: List[List[str]] = []
    for market_name, universe in fund_markets.items():
        performers = _top_weekly_performers_data(universe, top_n=3)
        for name, symbol, close_price, pct_change in performers:
            fund_rows.append(
                [
                    market_name,
                    _truncate(name, 24),
                    symbol,
                    f"{close_price:.2f}",
                    f"{pct_change:+.2f}%",
                ]
            )
    if fund_rows:
        fund_lines.append(
            _render_pre_table(
                "Top weekly performers",
                ["Market", "Fund", "Symbol", "Close", "7D"],
                fund_rows,
            )
        )
    else:
        fund_lines.append("Data temporarily unavailable")
    funds_str = "\n".join(fund_lines)

    return f"{biz_str}\n\n{stock_str}\n\n{funds_str}"


def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Skipping Telegram send: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        print("Telegram message sent successfully.")
        return True
    except Exception as exc:
        print(f"Failed to push message: {exc}")
        return False


def job_daily_briefing() -> bool:
    print(f"Executing briefing at {datetime.now()}")
    intro_message = build_daily_intro()
    norway_morning_news = get_norwegian_morning_news()
    global_news = get_global_news()
    business_news = get_business_and_stocks()
    trade_candidates = get_trade_candidates()

    full_message = intro_message + "\n" + norway_morning_news + global_news + business_news + "\n\n" + trade_candidates
    return send_telegram_message(full_message)


if __name__ == "__main__":
    missing_values = list(get_missing_required_config())
    if missing_values:
        print(
            "Missing required environment variables: "
            + ", ".join(missing_values)
            + "."
        )
        raise SystemExit(1)

    scheduler = BlockingScheduler(timezone=os.getenv("TZ", "Europe/Oslo"))
    scheduler.add_job(job_daily_briefing, "cron", hour="7,19", minute=0)

    print("--------------------------------------------------")
    print("RUNNING AN INSTANT TEST BRIEFING NOW...")
    print("--------------------------------------------------")
    job_daily_briefing()

    print("\nInitialization finished. Bot is sleeping until 07:00 AM and 07:00 PM local time.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
