import hashlib
import json
import logging
import os
import time
from datetime import datetime
from html import escape
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from apscheduler.schedulers.blocking import BlockingScheduler
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
import requests
import requests.exceptions
import yfinance as yf

os.environ["YF_NO_CURL_CFFI"] = "1"

if not hasattr(requests.exceptions, "DNSError"):
    requests.exceptions.DNSError = requests.exceptions.ConnectionError

LOGGER = logging.getLogger("news_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s level=%(levelname)s event=%(message)s",
)

USA_STOCK_UNIVERSE_DEFAULT = {
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

INDIA_STOCK_UNIVERSE_DEFAULT = {
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

NORWAY_STOCK_UNIVERSE_DEFAULT = {
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

INDIA_MUTUAL_FUNDS_DEFAULT = {
    "Nippon Nifty BeES": "NIFTYBEES.NS",
    "Nippon Junior BeES": "JUNIORBEES.NS",
    "Nippon Bank BeES": "BANKBEES.NS",
    "Nippon Gold BeES": "GOLDBEES.NS",
    "SBI Nifty 50 ETF": "SETFNIF50.NS",
}

NORWAY_MUTUAL_FUNDS_DEFAULT = {
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


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_token: str = ""
    telegram_chat_id: str = ""
    news_api_key: str = ""
    recipient_name: str = "Sunil"
    norway_news_query: str = "Norway OR Norge"
    tz: str = "Europe/Oslo"
    request_timeout_seconds: int = 15
    telegram_message_max_chars: int = 3900
    state_file: str = ".news_bot_state.json"
    command_poll_enabled: bool = True
    command_poll_interval_minutes: int = 2
    health_ping_enabled: bool = True
    health_ping_chat_id: Optional[str] = None
    trusted_news_domains: str = (
        "reuters.com,bloomberg.com,cnbc.com,finance.yahoo.com,yahoo.com,ft.com,"
        "bbc.com,theguardian.com,nrk.no,aftenposten.no,apnews.com"
    )
    blocked_news_domains: str = "news.google.com,pinterest.com,tiktok.com"
    trade_min_score: int = 3
    trade_min_week_momentum_pct: float = 1.0
    trade_min_day_change_pct: float = 0.0
    trade_min_volume_ratio: float = 1.2
    trade_max_drawdown_pct: float = 8.0
    trade_max_atr_pct: float = 4.5
    usa_stock_universe: Optional[str] = None
    india_stock_universe: Optional[str] = None
    norway_stock_universe: Optional[str] = None
    india_mutual_funds: Optional[str] = None
    norway_mutual_funds: Optional[str] = None

    def missing_required(self) -> List[str]:
        required = {
            "TELEGRAM_TOKEN": self.telegram_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
            "NEWS_API_KEY": self.news_api_key,
        }
        return [name for name, value in required.items() if not value]


SETTINGS = AppSettings()


def _parse_instrument_env(raw_value: Optional[str], fallback: Dict[str, str]) -> Dict[str, str]:
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
            parsed[pair] = pair
    return parsed or dict(fallback)


USA_STOCK_UNIVERSE = _parse_instrument_env(SETTINGS.usa_stock_universe, USA_STOCK_UNIVERSE_DEFAULT)
INDIA_STOCK_UNIVERSE = _parse_instrument_env(SETTINGS.india_stock_universe, INDIA_STOCK_UNIVERSE_DEFAULT)
NORWAY_STOCK_UNIVERSE = _parse_instrument_env(SETTINGS.norway_stock_universe, NORWAY_STOCK_UNIVERSE_DEFAULT)
INDIA_MUTUAL_FUNDS = _parse_instrument_env(SETTINGS.india_mutual_funds, INDIA_MUTUAL_FUNDS_DEFAULT)
NORWAY_MUTUAL_FUNDS = _parse_instrument_env(SETTINGS.norway_mutual_funds, NORWAY_MUTUAL_FUNDS_DEFAULT)


class AppState:
    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {
            "sent_headline_keys": {},
            "telegram_update_offset": 0,
            "last_health_ping_date": "",
            "last_run_status": "",
        }
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as file_obj:
                loaded = json.load(file_obj)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except Exception as exc:
            LOGGER.warning("state_load_failed detail=%s", exc)

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as file_obj:
                json.dump(self.data, file_obj, indent=2, sort_keys=True)
        except Exception as exc:
            LOGGER.warning("state_save_failed detail=%s", exc)

    def mark_headline_seen(self, section: str, key: str, date_key: str) -> None:
        sent = self.data.setdefault("sent_headline_keys", {})
        section_bucket = sent.setdefault(section, {})
        keys = section_bucket.setdefault(date_key, [])
        if key not in keys:
            keys.append(key)
        # keep state bounded
        if len(section_bucket.keys()) > 7:
            old_dates = sorted(section_bucket.keys())[:-7]
            for old_date in old_dates:
                section_bucket.pop(old_date, None)

    def has_seen_headline(self, section: str, key: str, date_key: str) -> bool:
        sent = self.data.get("sent_headline_keys", {})
        return key in sent.get(section, {}).get(date_key, [])

    @property
    def telegram_update_offset(self) -> int:
        return int(self.data.get("telegram_update_offset", 0))

    @telegram_update_offset.setter
    def telegram_update_offset(self, value: int) -> None:
        self.data["telegram_update_offset"] = value


STATE = AppState(SETTINGS.state_file)


def _split_csv(value: str) -> List[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


TRUSTED_DOMAINS = _split_csv(SETTINGS.trusted_news_domains)
BLOCKED_DOMAINS = _split_csv(SETTINGS.blocked_news_domains)


def _with_retry(action: Callable[[], Any], label: str, retries: int = 3, backoff_seconds: float = 1.0) -> Any:
    attempt = 0
    while True:
        try:
            return action()
        except Exception as exc:
            attempt += 1
            if attempt >= retries:
                LOGGER.error("retry_exhausted op=%s attempts=%s detail=%s", label, attempt, exc)
                raise
            sleep_for = backoff_seconds * (2 ** (attempt - 1))
            LOGGER.warning("retry op=%s attempt=%s sleep=%s detail=%s", label, attempt, sleep_for, exc)
            time.sleep(sleep_for)


def _daily_pick(options: List[str], context: str, date_key: Optional[str] = None) -> str:
    if not options:
        return ""
    if not date_key:
        date_key = datetime.today().strftime("%Y-%m-%d")
    digest = hashlib.sha256(f"{date_key}:{context}".encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(options)
    return options[index]


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
    body_rows = [" | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)) for row in rows]
    table_text = "\n".join([header_row, divider, *body_rows])
    return f"{escape(title)}\n<pre>{escape(table_text)}</pre>"


def _safe_domain(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        domain = (urlparse(url).netloc or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def _source_allowed(url: Optional[str]) -> bool:
    domain = _safe_domain(url)
    if not domain:
        return True
    if any(domain.endswith(blocked) for blocked in BLOCKED_DOMAINS):
        return False
    if TRUSTED_DOMAINS and not any(domain.endswith(trusted) for trusted in TRUSTED_DOMAINS):
        return False
    return True


def _headline_key(title: str, url: Optional[str]) -> str:
    return hashlib.sha256(f"{title}|{url or ''}".encode("utf-8")).hexdigest()


def _is_duplicate_headline(section: str, title: str, url: Optional[str], date_key: str) -> bool:
    key = _headline_key(title, url)
    if STATE.has_seen_headline(section, key, date_key):
        return True
    STATE.mark_headline_seen(section, key, date_key)
    return False


def _format_headline_line(index: int, title: str, url: Optional[str]) -> str:
    safe_title = escape(title)
    if not url:
        return f"{index}. {safe_title}"
    safe_url = escape(url, quote=True)
    return f'{index}. {safe_title} (<a href="{safe_url}">more</a>)'


def _split_message_html(message: str, max_chars: int) -> List[str]:
    if len(message) <= max_chars:
        return [message]
    sections = message.split("\n\n")
    chunks: List[str] = []
    current = ""
    for section in sections:
        candidate = section if not current else f"{current}\n\n{section}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(section) <= max_chars:
            current = section
            continue
        lines = section.splitlines()
        current = ""
        for line in lines:
            candidate_line = line if not current else f"{current}\n{line}"
            if len(candidate_line) <= max_chars:
                current = candidate_line
            else:
                if current:
                    chunks.append(current)
                current = line
        if current:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _telegram_post(chat_id: str, message: str) -> None:
    def _send() -> requests.Response:
        return requests.post(
            f"https://api.telegram.org/bot{SETTINGS.telegram_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=SETTINGS.request_timeout_seconds,
        )

    response = _with_retry(_send, "telegram_send")
    response.raise_for_status()


def send_telegram_message(message: str, chat_id: Optional[str] = None) -> bool:
    target_chat_id = chat_id or SETTINGS.telegram_chat_id
    if not SETTINGS.telegram_token or not target_chat_id:
        LOGGER.warning("telegram_skipped missing_token_or_chat")
        return False
    try:
        chunks = _split_message_html(message, SETTINGS.telegram_message_max_chars)
        total = len(chunks)
        for index, chunk in enumerate(chunks, 1):
            if total > 1:
                prefix = f"<b>Part {index}/{total}</b>\n"
                chunk = prefix + chunk
            _telegram_post(target_chat_id, chunk)
        LOGGER.info("telegram_sent chunks=%s", total)
        return True
    except Exception as exc:
        LOGGER.error("telegram_send_failed detail=%s", exc)
        return False


def build_daily_intro(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    date_key = now.strftime("%Y-%m-%d")
    recipient = escape(SETTINGS.recipient_name.strip() or "Sunil")
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


def _story_title_and_link(story: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
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
    if not SETTINGS.news_api_key:
        return "🌍 Top Global News:\nNews API key is not configured.\n\n"
    date_key = datetime.today().strftime("%Y-%m-%d")
    try:
        def _fetch() -> requests.Response:
            return requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={"language": "en", "pageSize": 10, "apiKey": SETTINGS.news_api_key},
                timeout=SETTINGS.request_timeout_seconds,
            )

        response = _with_retry(_fetch, "newsapi_global")
        response.raise_for_status()
        payload = response.json()
        articles = payload.get("articles", [])
        lines = ["🌍 Top Global News:"]
        count = 0
        for article in articles:
            title = article.get("title", "Untitled")
            url = article.get("url")
            if not _source_allowed(url):
                continue
            if _is_duplicate_headline("global_news", title, url, date_key):
                continue
            count += 1
            lines.append(_format_headline_line(count, title, url))
            if count >= 5:
                break
        if count == 0:
            lines.append("No fresh headlines available right now.")
        return "\n".join(lines) + "\n\n"
    except Exception as exc:
        return f"🌍 Top Global News:\nFailed to fetch ({exc}).\n\n"


def get_norwegian_morning_news() -> str:
    if not SETTINGS.news_api_key:
        return "🇳🇴 Early Morning Norway News:\nNews API key is not configured.\n\n"
    date_key = datetime.today().strftime("%Y-%m-%d")
    try:
        def _fetch() -> requests.Response:
            return requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={
                    "country": "no",
                    "q": SETTINGS.norway_news_query,
                    "pageSize": 10,
                    "apiKey": SETTINGS.news_api_key,
                },
                timeout=SETTINGS.request_timeout_seconds,
            )

        response = _with_retry(_fetch, "newsapi_norway")
        response.raise_for_status()
        payload = response.json()
        articles = payload.get("articles", [])
        lines = ["🇳🇴 Early Morning Norway News:"]
        count = 0
        for article in articles:
            title = article.get("title", "Untitled")
            url = article.get("url")
            if not _source_allowed(url):
                continue
            if _is_duplicate_headline("norway_news", title, url, date_key):
                continue
            count += 1
            lines.append(_format_headline_line(count, title, url))
            if count >= 5:
                break
        if count == 0:
            lines.append("No fresh Norwegian headlines available right now.")
        return "\n".join(lines) + "\n\n"
    except Exception as exc:
        return f"🇳🇴 Early Morning Norway News:\nFailed to fetch ({exc}).\n\n"


def _compute_weekly_change(symbol: str) -> Optional[Tuple[float, float]]:
    try:
        history = _with_retry(
            lambda: yf.Ticker(symbol).history(period="7d", interval="1d"),
            f"yf_weekly_{symbol}",
            retries=2,
        )
        close_series = history["Close"].dropna()
        if len(close_series) < 2:
            return None
        first_close = float(close_series.iloc[0])
        last_close = float(close_series.iloc[-1])
        if first_close == 0:
            return None
        return last_close, ((last_close - first_close) / first_close) * 100
    except Exception:
        return None


def _top_weekly_performers_data(instruments: Dict[str, str], top_n: int = 3) -> List[Tuple[str, str, float, float]]:
    scored: List[Tuple[float, str, str, float]] = []
    for name, symbol in instruments.items():
        result = _compute_weekly_change(symbol)
        if not result:
            continue
        close_price, pct_change = result
        scored.append((pct_change, name, symbol, close_price))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [(name, symbol, close_price, pct_change) for pct_change, name, symbol, close_price in scored[:top_n]]


def get_business_and_stocks() -> str:
    date_key = datetime.today().strftime("%Y-%m-%d")
    try:
        market = _with_retry(lambda: yf.Ticker("SPY"), "yf_spy_ticker", retries=2)
        raw_news = (market.news or [])[:15]
        biz_lines = ["💼 Top 10 Business Stories:"]
        count = 0
        for story in raw_news:
            title, link = _story_title_and_link(story)
            if not title:
                continue
            if not _source_allowed(link):
                continue
            if _is_duplicate_headline("business_news", title, link, date_key):
                continue
            count += 1
            biz_lines.append(_format_headline_line(count, title, link))
            if count >= 10:
                break
        if count == 0:
            biz_lines.append("No fresh business headlines available right now.")
        biz_str = "\n".join(biz_lines)
    except Exception as exc:
        biz_str = f"💼 Top 10 Business Stories:\nFailed to fetch ({exc})."

    stock_rows: List[List[str]] = []
    stock_markets = {"India": INDIA_STOCK_UNIVERSE, "USA": USA_STOCK_UNIVERSE, "Norway": NORWAY_STOCK_UNIVERSE}
    for market_name, universe in stock_markets.items():
        for name, symbol, close_price, pct_change in _top_weekly_performers_data(universe, top_n=3):
            stock_rows.append([market_name, _truncate(name, 20), symbol, f"{close_price:.2f}", f"{pct_change:+.2f}%"])
    stock_section = ["📈 Market Watch - Top Weekly Gainers:"]
    if stock_rows:
        stock_section.append(
            _render_pre_table("Top 3 gainers per market (7D)", ["Market", "Name", "Symbol", "Close", "7D"], stock_rows)
        )
    else:
        stock_section.append("Data temporarily unavailable")

    fund_rows: List[List[str]] = []
    fund_markets = {"India": INDIA_MUTUAL_FUNDS, "Norway": NORWAY_MUTUAL_FUNDS}
    for market_name, universe in fund_markets.items():
        for name, symbol, close_price, pct_change in _top_weekly_performers_data(universe, top_n=3):
            fund_rows.append([market_name, _truncate(name, 24), symbol, f"{close_price:.2f}", f"{pct_change:+.2f}%"])
    fund_section = ["🏦 Most Attractive Mutual Funds - Weekly:"]
    if fund_rows:
        fund_section.append(
            _render_pre_table("Top weekly performers", ["Market", "Fund", "Symbol", "Close", "7D"], fund_rows)
        )
    else:
        fund_section.append("Data temporarily unavailable")

    stock_str = "\n".join(stock_section)
    fund_str = "\n".join(fund_section)
    return f"{biz_str}\n\n{stock_str}\n\n{fund_str}"


def _compute_atr_percent(history: Any) -> Optional[float]:
    try:
        high = history["High"].dropna()
        low = history["Low"].dropna()
        close = history["Close"].dropna()
        if len(close) < 15 or len(high) < 15 or len(low) < 15:
            return None
        trs: List[float] = []
        for idx in range(1, len(close)):
            tr = max(
                float(high.iloc[idx] - low.iloc[idx]),
                abs(float(high.iloc[idx] - close.iloc[idx - 1])),
                abs(float(low.iloc[idx] - close.iloc[idx - 1])),
            )
            trs.append(tr)
        if len(trs) < 14:
            return None
        atr = sum(trs[-14:]) / 14.0
        last_close = float(close.iloc[-1])
        if last_close == 0:
            return None
        return (atr / last_close) * 100
    except Exception:
        return None


def _analyze_short_term_candidate(symbol: str) -> Optional[Dict[str, float]]:
    try:
        history = _with_retry(
            lambda: yf.Ticker(symbol).history(period="3mo", interval="1d"),
            f"yf_candidate_{symbol}",
            retries=2,
        )
        close_series = history["Close"].dropna()
        if len(close_series) < 30:
            return None
        last_close = float(close_series.iloc[-1])
        prev_close = float(close_series.iloc[-2])
        if prev_close == 0:
            return None
        day_change_pct = ((last_close - prev_close) / prev_close) * 100
        reference_close = float(close_series.iloc[-6]) if len(close_series) >= 6 else float(close_series.iloc[0])
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

        rolling_high_20 = float(close_series.tail(20).max())
        drawdown_pct = ((rolling_high_20 - last_close) / rolling_high_20) * 100 if rolling_high_20 else 0.0
        atr_pct = _compute_atr_percent(history)
        if atr_pct is None:
            return None

        score = 0
        if trend_ok:
            score += 1
        if week_momentum_pct >= SETTINGS.trade_min_week_momentum_pct:
            score += 1
        if day_change_pct > SETTINGS.trade_min_day_change_pct:
            score += 1
        if volume_ratio >= SETTINGS.trade_min_volume_ratio:
            score += 1
        if drawdown_pct <= SETTINGS.trade_max_drawdown_pct:
            score += 1
        if atr_pct <= SETTINGS.trade_max_atr_pct:
            score += 1

        if score < SETTINGS.trade_min_score:
            return None

        return {
            "score": float(score),
            "last_close": last_close,
            "day_change_pct": day_change_pct,
            "week_momentum_pct": week_momentum_pct,
            "volume_ratio": volume_ratio,
            "drawdown_pct": drawdown_pct,
            "atr_pct": atr_pct,
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
        if metrics:
            scored.append((metrics["score"], metrics["week_momentum_pct"], metrics["day_change_pct"], name, symbol, metrics))

    lines = [
        "⚠️ Short-Term Trade Candidates (Informational Only):",
        "No guarantee of 1-2% daily profit. Use strict risk management.",
        (
            f"Filters: score>={SETTINGS.trade_min_score}, "
            f"5D>={SETTINGS.trade_min_week_momentum_pct:.1f}%, "
            f"1D>{SETTINGS.trade_min_day_change_pct:.1f}%, "
            f"Volx>={SETTINGS.trade_min_volume_ratio:.2f}, "
            f"DD<={SETTINGS.trade_max_drawdown_pct:.1f}%, ATR<={SETTINGS.trade_max_atr_pct:.1f}%"
        ),
    ]
    lines[2] = lines[2].replace("<=", "≤").replace(">=", "≥").replace(">", "›")
    if not scored:
        lines.append("No candidates met the momentum criteria today.")
        return "\n".join(lines) + "\n\n"

    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    rows: List[List[str]] = []
    for idx, (_, _, _, name, symbol, metrics) in enumerate(scored[:top_n], 1):
        rows.append(
            [
                str(idx),
                _truncate(name, 24),
                symbol,
                f"{metrics['last_close']:.2f}",
                f"{metrics['day_change_pct']:+.2f}%",
                f"{metrics['week_momentum_pct']:+.2f}%",
                f"{metrics['volume_ratio']:.2f}",
                f"{metrics['drawdown_pct']:.2f}%",
                f"{metrics['atr_pct']:.2f}%",
                str(int(metrics["score"])),
            ]
        )
    lines.append(
        _render_pre_table(
            "Candidates",
            ["#", "Name", "Symbol", "Close", "1D", "5D", "Volx", "DD", "ATR", "Score"],
            rows,
        )
    )
    return "\n".join(lines) + "\n\n"


def build_health_report() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "✅ Bot Health Check\n"
        f"Time: {now}\n"
        f"Timezone: {SETTINGS.tz}\n"
        f"Last run status: {STATE.data.get('last_run_status', 'unknown')}\n"
    )


def compose_briefing(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return (
        build_daily_intro(now)
        + "\n"
        + get_norwegian_morning_news()
        + get_global_news()
        + get_business_and_stocks()
        + "\n\n"
        + get_trade_candidates()
    )


def job_daily_briefing(force_hour: Optional[int] = None) -> bool:
    run_time = datetime.now()
    if force_hour is not None:
        run_time = run_time.replace(hour=force_hour, minute=0, second=0, microsecond=0)
    LOGGER.info("briefing_start at=%s", run_time.isoformat())
    message = compose_briefing(run_time)
    success = send_telegram_message(message)
    STATE.data["last_run_status"] = "success" if success else "failed"
    STATE.save()
    return success


def job_health_ping() -> bool:
    if not SETTINGS.health_ping_enabled:
        return True
    date_key = datetime.today().strftime("%Y-%m-%d")
    if STATE.data.get("last_health_ping_date") == date_key:
        return True
    success = send_telegram_message(build_health_report(), chat_id=SETTINGS.health_ping_chat_id or SETTINGS.telegram_chat_id)
    if success:
        STATE.data["last_health_ping_date"] = date_key
        STATE.save()
    return success


def _command_help_text() -> str:
    return (
        "Supported commands:\n"
        "/now - Send full briefing now\n"
        "/morning - Send morning-style briefing now\n"
        "/evening - Send evening-style briefing now\n"
        "/watchlist - Send market + trade candidate sections\n"
        "/health - Send bot health report\n"
    )


def _handle_command(command: str, chat_id: str) -> None:
    normalized = command.strip().split()[0].lower()
    if normalized == "/now":
        send_telegram_message(compose_briefing(datetime.now()), chat_id=chat_id)
    elif normalized == "/morning":
        now = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0)
        send_telegram_message(compose_briefing(now), chat_id=chat_id)
    elif normalized == "/evening":
        now = datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)
        send_telegram_message(compose_briefing(now), chat_id=chat_id)
    elif normalized == "/watchlist":
        payload = get_business_and_stocks() + "\n\n" + get_trade_candidates()
        send_telegram_message(payload, chat_id=chat_id)
    elif normalized == "/health":
        send_telegram_message(build_health_report(), chat_id=chat_id)
    else:
        send_telegram_message(_command_help_text(), chat_id=chat_id)


def poll_telegram_commands() -> bool:
    if not SETTINGS.command_poll_enabled or not SETTINGS.telegram_token:
        return True
    try:
        params = {"timeout": 0, "limit": 20}
        if STATE.telegram_update_offset > 0:
            params["offset"] = STATE.telegram_update_offset + 1

        def _fetch() -> requests.Response:
            return requests.get(
                f"https://api.telegram.org/bot{SETTINGS.telegram_token}/getUpdates",
                params=params,
                timeout=SETTINGS.request_timeout_seconds,
            )

        response = _with_retry(_fetch, "telegram_get_updates")
        response.raise_for_status()
        payload = response.json()
        updates = payload.get("result", [])
        for update in updates:
            update_id = int(update.get("update_id", 0))
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat", {})
            chat_id = str(chat.get("id", SETTINGS.telegram_chat_id))
            text = message.get("text", "")
            if text.startswith("/"):
                LOGGER.info("command_received cmd=%s chat_id=%s", text.split()[0], chat_id)
                _handle_command(text, chat_id)
            if update_id > STATE.telegram_update_offset:
                STATE.telegram_update_offset = update_id
        if updates:
            STATE.save()
        return True
    except Exception as exc:
        LOGGER.error("command_poll_failed detail=%s", exc)
        return False


def get_missing_required_config() -> Iterable[str]:
    return SETTINGS.missing_required()


if __name__ == "__main__":
    missing_values = list(get_missing_required_config())
    if missing_values:
        print("Missing required environment variables: " + ", ".join(missing_values) + ".")
        raise SystemExit(1)

    scheduler = BlockingScheduler(timezone=SETTINGS.tz)
    scheduler.add_job(job_daily_briefing, "cron", hour="7,19", minute=0)
    scheduler.add_job(job_health_ping, "cron", hour=12, minute=0)
    if SETTINGS.command_poll_enabled:
        scheduler.add_job(poll_telegram_commands, "interval", minutes=SETTINGS.command_poll_interval_minutes)

    print("--------------------------------------------------")
    print("RUNNING AN INSTANT TEST BRIEFING NOW...")
    print("--------------------------------------------------")
    job_daily_briefing()

    print("\nInitialization finished. Bot is waiting for schedule and commands.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
