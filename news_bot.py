import hashlib
import json
import logging
import os
import time
from datetime import datetime
from html import escape
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from apscheduler.schedulers.blocking import BlockingScheduler
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

DEFAULT_GLOBAL_NEWS_FEEDS = (
    "https://feeds.bbci.co.uk/news/world/rss.xml,"
    "https://www.cnbc.com/id/100727362/device/rss/rss.html,"
    "https://feeds.apnews.com/apf-topnews"
)
DEFAULT_BUSINESS_NEWS_FEEDS = (
    "https://www.cnbc.com/id/10001147/device/rss/rss.html,"
    "https://finance.yahoo.com/news/rssindex,"
    "https://feeds.marketwatch.com/marketwatch/topstories/"
)
DEFAULT_NORWAY_NEWS_FEEDS = (
    "https://www.nrk.no/toppsaker.rss,"
    "https://e24.no/rss,"
    "https://www.aftenposten.no/rss"
)
DEFAULT_STOCK_SCREENERS = "day_gainers,most_actives"
DEFAULT_FUND_SCREENERS = "conservative_foreign_funds,solid_large_growth_funds,high_yield_bond"
MARKET_DATA_USER_AGENT = "Mozilla/5.0 (compatible; news-bot/1.0)"
YAHOO_SCREENER_ENDPOINTS = (
    "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
    "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved",
)
DEFAULT_FINNHUB_API_URL = "https://finnhub.io/api/v1"

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
    freenews_api_key: str = ""
    # Backward-compatible env support for older typo'd variable name.
    freen_ews_api_key: str = ""
    # Support both FINNHUB_API_KEY and FINHUB_API_KEY env variable names.
    finnhub_api_key: str = ""
    finhub_api_key: str = ""
    finnhub_api_url: str = DEFAULT_FINNHUB_API_URL
    freenews_api_url: str = "https://freenewsapi.com/api/v1/news"
    news_fetch_priority: str = "newsapi,freenews,rss"
    recipient_name: str = "Sunil"
    tz: str = "Europe/Oslo"
    request_timeout_seconds: int = 15
    telegram_message_max_chars: int = 3900
    state_file: str = ".news_bot_state.json"
    command_poll_enabled: bool = True
    command_poll_interval_minutes: int = 2
    health_ping_enabled: bool = True
    health_ping_chat_id: Optional[str] = None
    send_startup_briefing: bool = False
    trusted_news_domains: str = (
        "reuters.com,bloomberg.com,cnbc.com,finance.yahoo.com,yahoo.com,ft.com,"
        "bbc.com,theguardian.com,nrk.no,aftenposten.no,e24.no,apnews.com,marketwatch.com"
    )
    blocked_news_domains: str = "news.google.com,pinterest.com,tiktok.com"
    trade_min_score: int = 3
    trade_min_week_momentum_pct: float = 1.0
    trade_min_day_change_pct: float = 0.0
    trade_min_volume_ratio: float = 1.2
    trade_max_drawdown_pct: float = 8.0
    trade_max_atr_pct: float = 4.5
    global_news_feeds: str = DEFAULT_GLOBAL_NEWS_FEEDS
    business_news_feeds: str = DEFAULT_BUSINESS_NEWS_FEEDS
    norway_news_feeds: str = DEFAULT_NORWAY_NEWS_FEEDS
    stock_screeners: str = DEFAULT_STOCK_SCREENERS
    fund_screeners: str = DEFAULT_FUND_SCREENERS
    screener_quote_limit: int = 50
    screener_request_timeout_seconds: int = 6
    screener_cache_ttl_seconds: int = 90
    screener_failure_cooldown_seconds: int = 300
    finnhub_request_timeout_seconds: int = 4
    finnhub_cache_ttl_seconds: int = 120
    finnhub_failure_cooldown_seconds: int = 180
    finnhub_max_symbols_per_refresh: int = 16
    usa_stock_universe: Optional[str] = None
    india_stock_universe: Optional[str] = None
    norway_stock_universe: Optional[str] = None
    india_mutual_funds: Optional[str] = None
    norway_mutual_funds: Optional[str] = None

    def missing_required(self) -> List[str]:
        required = {
            "TELEGRAM_TOKEN": self.telegram_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
        }
        return [name for name, value in required.items() if not value]


SETTINGS = AppSettings()
LIVE_QUOTES_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
YAHOO_SCREENER_BACKOFF_UNTIL = 0.0
FINNHUB_QUOTE_CACHE: Dict[str, Tuple[float, Dict[str, float]]] = {}
FINNHUB_BACKOFF_UNTIL = 0.0


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


USA_STOCK_UNIVERSE = _parse_instrument_env(SETTINGS.usa_stock_universe, {})
INDIA_STOCK_UNIVERSE = _parse_instrument_env(SETTINGS.india_stock_universe, {})
NORWAY_STOCK_UNIVERSE = _parse_instrument_env(SETTINGS.norway_stock_universe, {})
INDIA_MUTUAL_FUNDS = _parse_instrument_env(SETTINGS.india_mutual_funds, {})
NORWAY_MUTUAL_FUNDS = _parse_instrument_env(SETTINGS.norway_mutual_funds, {})


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


def _split_csv_values(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


TRUSTED_DOMAINS = _split_csv(SETTINGS.trusted_news_domains)
BLOCKED_DOMAINS = _split_csv(SETTINGS.blocked_news_domains)
GLOBAL_NEWS_FEEDS = _split_csv_values(SETTINGS.global_news_feeds)
BUSINESS_NEWS_FEEDS = _split_csv_values(SETTINGS.business_news_feeds)
NORWAY_NEWS_FEEDS = _split_csv_values(SETTINGS.norway_news_feeds)
STOCK_SCREENERS = _split_csv_values(SETTINGS.stock_screeners)
FUND_SCREENERS = _split_csv_values(SETTINGS.fund_screeners)


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


def _parse_rss_items(xml_text: str, max_items: int = 20) -> List[Tuple[str, Optional[str]]]:
    items: List[Tuple[str, Optional[str]]] = []
    root = ET.fromstring(xml_text)

    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip() or None
        if not link:
            guid = (node.findtext("guid") or "").strip()
            if guid.startswith("http"):
                link = guid
        if title:
            items.append((title, link))
        if len(items) >= max_items:
            return items

    atom_ns = "{http://www.w3.org/2005/Atom}"
    for node in root.findall(f".//{atom_ns}entry"):
        title = (node.findtext(f"{atom_ns}title") or "").strip()
        link: Optional[str] = None
        for link_node in node.findall(f"{atom_ns}link"):
            href = (link_node.attrib.get("href") or "").strip()
            rel = (link_node.attrib.get("rel") or "alternate").strip()
            if href and rel in ("alternate", ""):
                link = href
                break
            if href and not link:
                link = href
        if title:
            items.append((title, link))
        if len(items) >= max_items:
            return items
    return items


def _fetch_rss_items(feed_url: str, max_items: int = 20) -> List[Tuple[str, Optional[str]]]:
    try:
        response = _with_retry(
            lambda: requests.get(
                feed_url,
                headers={"User-Agent": MARKET_DATA_USER_AGENT},
                timeout=SETTINGS.request_timeout_seconds,
            ),
            f"rss_{hashlib.sha1(feed_url.encode('utf-8')).hexdigest()[:8]}",
            retries=2,
        )
        response.raise_for_status()
        return _parse_rss_items(response.text, max_items=max_items)
    except Exception as exc:
        LOGGER.warning("rss_fetch_failed feed=%s detail=%s", feed_url, exc)
        return []


def _extract_articles_from_payload(payload: Any) -> List[Tuple[str, Optional[str]]]:
    containers: List[Any] = []
    if isinstance(payload, dict):
        for key in ("articles", "data", "results", "news"):
            value = payload.get(key)
            if isinstance(value, list):
                containers.append(value)
    elif isinstance(payload, list):
        containers.append(payload)

    items: List[Tuple[str, Optional[str]]] = []
    for container in containers:
        for article in container:
            if not isinstance(article, dict):
                continue
            title = article.get("title") or article.get("headline") or article.get("name")
            url = article.get("url") or article.get("link") or article.get("source_url")
            if isinstance(title, str) and title.strip():
                normalized_url = url.strip() if isinstance(url, str) and url.strip() else None
                items.append((title.strip(), normalized_url))
    return items


def _fetch_newsapi_items(scope: str, max_items: int = 20) -> List[Tuple[str, Optional[str]]]:
    api_key = (SETTINGS.news_api_key or "").strip()
    if not api_key:
        return []
    params: Dict[str, Any] = {"pageSize": max_items, "apiKey": api_key}
    if scope == "norway":
        params["country"] = "no"
    else:
        params["language"] = "en"
    if scope == "business":
        params["category"] = "business"

    try:
        response = _with_retry(
            lambda: requests.get(
                "https://newsapi.org/v2/top-headlines",
                params=params,
                timeout=SETTINGS.request_timeout_seconds,
            ),
            f"newsapi_{scope}",
            retries=2,
        )
        response.raise_for_status()
        return _extract_articles_from_payload(response.json())
    except Exception as exc:
        LOGGER.warning("newsapi_fetch_failed scope=%s detail=%s", scope, exc)
        return []


def _active_freenews_key() -> str:
    for key in (SETTINGS.freenews_api_key, SETTINGS.freen_ews_api_key):
        if key and key.strip():
            return key.strip()
    return ""


def _fetch_freenews_items(scope: str, max_items: int = 20) -> List[Tuple[str, Optional[str]]]:
    api_key = _active_freenews_key()
    if not api_key:
        return []
    params: Dict[str, Any] = {"apikey": api_key, "limit": max_items}
    if scope == "business":
        params["category"] = "business"
    if scope == "norway":
        params["country"] = "no"
    else:
        params["language"] = "en"

    try:
        response = _with_retry(
            lambda: requests.get(
                SETTINGS.freenews_api_url,
                params=params,
                timeout=SETTINGS.request_timeout_seconds,
            ),
            f"freenews_{scope}",
            retries=2,
        )
        response.raise_for_status()
        return _extract_articles_from_payload(response.json())
    except Exception as exc:
        LOGGER.warning("freenews_fetch_failed scope=%s detail=%s", scope, exc)
        return []


def _fetch_rss_from_feeds(feed_urls: List[str], max_items: int = 20) -> List[Tuple[str, Optional[str]]]:
    items: List[Tuple[str, Optional[str]]] = []
    for feed_url in feed_urls:
        for item in _fetch_rss_items(feed_url, max_items=max_items):
            items.append(item)
            if len(items) >= max_items:
                return items
    return items


def _news_provider_priority() -> List[str]:
    providers = _split_csv(SETTINGS.news_fetch_priority or "")
    sanitized = [provider for provider in providers if provider in {"newsapi", "freenews", "rss"}]
    return sanitized or ["rss"]


def _build_news_section(
    section_key: str,
    scope: str,
    title: str,
    feed_urls: List[str],
    empty_message: str,
    max_headlines: int = 5,
) -> str:
    date_key = datetime.today().strftime("%Y-%m-%d")
    lines = [title]
    count = 0

    source_candidates: List[Tuple[str, List[Tuple[str, Optional[str]]]]] = []
    for provider in _news_provider_priority():
        if provider == "newsapi":
            source_candidates.append(("newsapi", _fetch_newsapi_items(scope, max_items=20)))
        elif provider == "freenews":
            source_candidates.append(("freenews", _fetch_freenews_items(scope, max_items=20)))
        elif provider == "rss":
            source_candidates.append(("rss", _fetch_rss_from_feeds(feed_urls, max_items=30)))

    for _provider, headlines in source_candidates:
        for headline, url in headlines:
            if not _source_allowed(url):
                continue
            if _is_duplicate_headline(section_key, headline, url, date_key):
                continue
            count += 1
            lines.append(_format_headline_line(count, headline, url))
            if count >= max_headlines:
                break
        if count >= max_headlines:
            break

    if count == 0:
        lines.append(empty_message)
    return "\n".join(lines) + "\n\n"


def get_global_news() -> str:
    return _build_news_section(
        section_key="global_news",
        scope="global",
        title="🌍 Top Global News:",
        feed_urls=GLOBAL_NEWS_FEEDS,
        empty_message="No fresh global headlines available right now.",
    )


def get_norwegian_morning_news() -> str:
    return _build_news_section(
        section_key="norway_news",
        scope="norway",
        title="🇳🇴 Early Morning Norway News:",
        feed_urls=NORWAY_NEWS_FEEDS,
        empty_message="No fresh Norwegian headlines available right now.",
    )


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


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        raw = value.get("raw")
        if isinstance(raw, (int, float)):
            return float(raw)
    try:
        return float(value)
    except Exception:
        return None


def _active_finnhub_key() -> str:
    for key in (SETTINGS.finnhub_api_key, SETTINGS.finhub_api_key):
        if key and key.strip():
            return key.strip()
    return ""


def _fetch_finnhub_quote(symbol: str) -> Optional[Dict[str, float]]:
    global FINNHUB_BACKOFF_UNTIL
    api_key = _active_finnhub_key()
    if not api_key or not symbol:
        return None

    now_ts = time.time()
    if now_ts < FINNHUB_BACKOFF_UNTIL:
        return None

    cached = FINNHUB_QUOTE_CACHE.get(symbol)
    if cached:
        cached_at, payload = cached
        if now_ts - cached_at <= max(10, SETTINGS.finnhub_cache_ttl_seconds):
            return dict(payload)

    timeout_seconds = max(2, min(SETTINGS.request_timeout_seconds, SETTINGS.finnhub_request_timeout_seconds))
    try:
        response = _with_retry(
            lambda: requests.get(
                f"{SETTINGS.finnhub_api_url.rstrip('/')}/quote",
                params={"symbol": symbol, "token": api_key},
                timeout=timeout_seconds,
            ),
            f"finnhub_quote_{symbol}",
            retries=1,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(str(payload.get("error")))
        price = _as_float(payload.get("c"))
        pct_change = _as_float(payload.get("dp"))
        prev_close = _as_float(payload.get("pc"))
        if price is None or price <= 0:
            return None
        if pct_change is None and prev_close and prev_close != 0:
            pct_change = ((price - prev_close) / prev_close) * 100
        if pct_change is None:
            return None
        normalized = {"price": price, "pct_change": pct_change}
        FINNHUB_QUOTE_CACHE[symbol] = (now_ts, normalized)
        return dict(normalized)
    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in (401, 403, 429):
            FINNHUB_BACKOFF_UNTIL = time.time() + max(30, SETTINGS.finnhub_failure_cooldown_seconds)
        LOGGER.warning("finnhub_quote_failed symbol=%s status=%s detail=%s", symbol, status_code, exc)
    except Exception as exc:
        error_text = str(exc)
        if isinstance(exc, requests.exceptions.SSLError) or "UNEXPECTED_EOF_WHILE_READING" in error_text:
            FINNHUB_BACKOFF_UNTIL = time.time() + max(30, SETTINGS.finnhub_failure_cooldown_seconds)
        LOGGER.warning("finnhub_quote_failed symbol=%s detail=%s", symbol, error_text)
    return None


def _refresh_quotes_with_finnhub(quotes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not _active_finnhub_key():
        return [dict(quote) for quote in quotes]

    max_refresh = max(0, SETTINGS.finnhub_max_symbols_per_refresh)
    refreshed: List[Dict[str, Any]] = []
    for index, quote in enumerate(quotes):
        normalized = dict(quote)
        if index < max_refresh:
            quote_type = (normalized.get("quoteType") or "").upper()
            symbol = str(normalized.get("symbol") or "").strip()
            if quote_type in {"EQUITY", "ETF"} and symbol:
                finnhub_quote = _fetch_finnhub_quote(symbol)
                if finnhub_quote:
                    normalized["regularMarketPrice"] = finnhub_quote["price"]
                    normalized["regularMarketChangePercent"] = finnhub_quote["pct_change"]
                    normalized["marketDataProvider"] = "finnhub"
        refreshed.append(normalized)
    return refreshed


def _fetch_predefined_screener_quotes(scr_id: str, count: int) -> List[Dict[str, Any]]:
    global YAHOO_SCREENER_BACKOFF_UNTIL
    now_ts = time.time()
    if now_ts < YAHOO_SCREENER_BACKOFF_UNTIL:
        LOGGER.info("screener_temporarily_skipped scr_id=%s backoff_until=%s", scr_id, int(YAHOO_SCREENER_BACKOFF_UNTIL))
        return []

    timeout_seconds = max(2, min(SETTINGS.request_timeout_seconds, SETTINGS.screener_request_timeout_seconds))
    last_error: Optional[Exception] = None
    try:
        for endpoint in YAHOO_SCREENER_ENDPOINTS:
            try:
                response = _with_retry(
                    lambda: requests.get(
                        endpoint,
                        params={"scrIds": scr_id, "count": count, "start": 0},
                        headers={"User-Agent": MARKET_DATA_USER_AGENT},
                        timeout=timeout_seconds,
                    ),
                    f"screener_{scr_id}",
                    retries=1,
                )
                response.raise_for_status()
                payload = response.json()
                result = payload.get("finance", {}).get("result") or []
                if not result:
                    return []
                quotes = result[0].get("quotes") or []
                if isinstance(quotes, list):
                    return [quote for quote in quotes if isinstance(quote, dict)]
            except Exception as endpoint_exc:
                last_error = endpoint_exc
                continue
    except Exception as exc:
        last_error = exc

    if last_error:
        error_text = str(last_error)
        if isinstance(last_error, requests.exceptions.SSLError) or "UNEXPECTED_EOF_WHILE_READING" in error_text:
            YAHOO_SCREENER_BACKOFF_UNTIL = time.time() + max(30, SETTINGS.screener_failure_cooldown_seconds)
            LOGGER.warning(
                "screener_ssl_backoff_active until=%s detail=%s",
                int(YAHOO_SCREENER_BACKOFF_UNTIL),
                error_text,
            )
        LOGGER.warning("screener_fetch_failed scr_id=%s detail=%s", scr_id, error_text)
    return []


def _quote_name(quote: Dict[str, Any]) -> str:
    for key in ("shortName", "longName", "displayName", "symbol"):
        value = quote.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Unknown"


def _is_fund_quote(quote: Dict[str, Any]) -> bool:
    quote_type = (quote.get("quoteType") or "").upper()
    return quote_type in {"MUTUALFUND", "ETF"}


def _is_stock_quote(quote: Dict[str, Any]) -> bool:
    return (quote.get("quoteType") or "").upper() == "EQUITY"


def _collect_live_quotes(scr_ids: List[str], count_per_screener: int) -> List[Dict[str, Any]]:
    cache_key = f"{','.join(scr_ids)}|{count_per_screener}"
    now_ts = time.time()
    cached = LIVE_QUOTES_CACHE.get(cache_key)
    if cached:
        cached_at, cached_quotes = cached
        if now_ts - cached_at <= max(5, SETTINGS.screener_cache_ttl_seconds):
            return list(cached_quotes)

    deduped: Dict[str, Dict[str, Any]] = {}
    for scr_id in scr_ids:
        for quote in _fetch_predefined_screener_quotes(scr_id, count=count_per_screener):
            symbol = (quote.get("symbol") or "").strip()
            if not symbol:
                continue
            if symbol not in deduped:
                deduped[symbol] = quote
    quotes = list(deduped.values())
    LIVE_QUOTES_CACHE[cache_key] = (now_ts, quotes)
    return quotes


def _format_live_rows(
    quotes: List[Dict[str, Any]],
    top_n: int,
    max_name_len: int,
) -> List[List[str]]:
    initial_ranked: List[Tuple[float, Dict[str, Any]]] = []
    for quote in quotes:
        pct_change = _as_float(quote.get("regularMarketChangePercent"))
        if pct_change is None:
            continue
        initial_ranked.append((pct_change, quote))
    initial_ranked.sort(key=lambda item: item[0], reverse=True)

    ranked_quotes = [quote for _, quote in initial_ranked]
    enriched_quotes = _refresh_quotes_with_finnhub(ranked_quotes)

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for quote in enriched_quotes:
        pct_change = _as_float(quote.get("regularMarketChangePercent"))
        if pct_change is None:
            continue
        scored.append((pct_change, quote))
    scored.sort(key=lambda item: item[0], reverse=True)

    rows: List[List[str]] = []
    for pct_change, quote in scored[:top_n]:
        symbol = str(quote.get("symbol") or "-")
        close_price = _as_float(quote.get("regularMarketPrice"))
        rows.append(
            [
                _truncate(_quote_name(quote), max_name_len),
                symbol,
                f"{close_price:.2f}" if close_price is not None else "N/A",
                f"{pct_change:+.2f}%",
            ]
        )
    return rows


def _build_live_universe(limit: int = 40) -> Dict[str, str]:
    dynamic_quotes = _collect_live_quotes(STOCK_SCREENERS + FUND_SCREENERS, count_per_screener=SETTINGS.screener_quote_limit)
    stocks = [quote for quote in dynamic_quotes if _is_stock_quote(quote)]
    funds = [quote for quote in dynamic_quotes if _is_fund_quote(quote)]
    rows = _format_live_rows(stocks, top_n=limit, max_name_len=28) + _format_live_rows(
        funds,
        top_n=max(10, limit // 2),
        max_name_len=28,
    )
    universe: Dict[str, str] = {}
    for row in rows:
        name, symbol = row[0], row[1]
        if symbol != "-" and symbol not in universe.values():
            universe[name] = symbol
    return universe


def get_business_and_stocks() -> str:
    biz_str = _build_news_section(
        section_key="business_news",
        scope="business",
        title="💼 Top Business Stories:",
        feed_urls=BUSINESS_NEWS_FEEDS,
        empty_message="No fresh business headlines available right now.",
        max_headlines=8,
    ).strip()

    quotes = _collect_live_quotes(STOCK_SCREENERS + FUND_SCREENERS, count_per_screener=SETTINGS.screener_quote_limit)
    stock_quotes = [quote for quote in quotes if _is_stock_quote(quote)]
    fund_quotes = [quote for quote in quotes if _is_fund_quote(quote)]

    stock_rows = _format_live_rows(stock_quotes, top_n=8, max_name_len=24)
    stock_section = ["📈 Live Stock Movers (Public Market Data):"]
    if stock_rows:
        stock_section.append(_render_pre_table("Top stocks by 1D change", ["Name", "Symbol", "Price", "1D"], stock_rows))
    else:
        stock_section.append("Data temporarily unavailable")

    fund_rows = _format_live_rows(fund_quotes, top_n=8, max_name_len=26)
    fund_section = ["🏦 Live Funds & ETFs (Public Market Data):"]
    if fund_rows:
        fund_section.append(_render_pre_table("Top funds/ETFs by 1D change", ["Fund", "Symbol", "Price", "1D"], fund_rows))
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
        universe = _build_live_universe(limit=45)
        configured_universe = {
            **{f"{name} [USA Stock]": sym for name, sym in USA_STOCK_UNIVERSE.items()},
            **{f"{name} [India Stock]": sym for name, sym in INDIA_STOCK_UNIVERSE.items()},
            **{f"{name} [Norway Stock]": sym for name, sym in NORWAY_STOCK_UNIVERSE.items()},
            **{f"{name} [India Fund]": sym for name, sym in INDIA_MUTUAL_FUNDS.items()},
            **{f"{name} [Norway Fund]": sym for name, sym in NORWAY_MUTUAL_FUNDS.items()},
        }
        for label, symbol in configured_universe.items():
            if symbol not in universe.values():
                universe[label] = symbol

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


def _prepare_telegram_long_polling() -> None:
    if not SETTINGS.telegram_token:
        return
    try:
        webhook_info = _with_retry(
            lambda: requests.get(
                f"https://api.telegram.org/bot{SETTINGS.telegram_token}/getWebhookInfo",
                timeout=SETTINGS.request_timeout_seconds,
            ),
            "telegram_get_webhook_info",
            retries=2,
        )
        webhook_info.raise_for_status()
        payload = webhook_info.json()
        webhook_url = ((payload.get("result") or {}).get("url") or "").strip()
        if webhook_url:
            LOGGER.warning("telegram_webhook_detected removing_for_long_polling")
            _with_retry(
                lambda: requests.post(
                    f"https://api.telegram.org/bot{SETTINGS.telegram_token}/deleteWebhook",
                    json={"drop_pending_updates": False},
                    timeout=SETTINGS.request_timeout_seconds,
                ),
                "telegram_delete_webhook",
                retries=2,
            )
    except Exception as exc:
        LOGGER.warning("telegram_webhook_check_failed detail=%s", exc)


def get_missing_required_config() -> Iterable[str]:
    return SETTINGS.missing_required()


if __name__ == "__main__":
    missing_values = list(get_missing_required_config())
    if missing_values:
        print("Missing required environment variables: " + ", ".join(missing_values) + ".")
        raise SystemExit(1)

    _prepare_telegram_long_polling()

    scheduler = BlockingScheduler(timezone=SETTINGS.tz)
    scheduler.add_job(job_daily_briefing, "cron", hour="7,19", minute=0)
    scheduler.add_job(job_health_ping, "cron", hour=12, minute=0)
    if SETTINGS.command_poll_enabled:
        scheduler.add_job(poll_telegram_commands, "interval", minutes=SETTINGS.command_poll_interval_minutes)

    if SETTINGS.send_startup_briefing:
        print("--------------------------------------------------")
        print("RUNNING AN INSTANT TEST BRIEFING NOW...")
        print("--------------------------------------------------")
        job_daily_briefing()
    else:
        print("Startup briefing skipped (SEND_STARTUP_BRIEFING=false).")

    print("\nInitialization finished. Bot is waiting for schedule and commands.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
