"""Live quotes, screeners and the market snapshot."""

import time
from typing import Any, Dict, List, Optional, Tuple

import requests.exceptions
import yfinance as yf

from newsbot import news_feeds
from newsbot.config import BUSINESS_NEWS_FEEDS, FUND_SCREENERS, MARKET_DATA_USER_AGENT, SETTINGS, STOCK_SCREENERS, YAHOO_SCREENER_ENDPOINTS
from newsbot.logsetup import SYS_LOG
from newsbot.news_feeds import Selection
from newsbot.state import CACHE_LOCK, FINNHUB_QUOTE_CACHE, HISTORY_CACHE, LIVE_QUOTES_CACHE, TICKER_INFO_CACHE
from newsbot.util import _render_pre_table, _truncate, _with_retry


YAHOO_SCREENER_BACKOFF_UNTIL = 0.0


FINNHUB_BACKOFF_UNTIL = 0.0


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
    with CACHE_LOCK:
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
                params={"symbol": symbol},
                # A header, not `?token=`: query strings end up in exception
                # text and therefore in the logs.
                headers={"X-Finnhub-Token": api_key},
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
        with CACHE_LOCK:
            FINNHUB_QUOTE_CACHE[symbol] = (now_ts, normalized)
        return dict(normalized)
    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in (401, 403, 429):
            with CACHE_LOCK:
                FINNHUB_BACKOFF_UNTIL = time.time() + max(30, SETTINGS.finnhub_failure_cooldown_seconds)
        SYS_LOG.warning("finnhub_quote_failed symbol=%s status=%s detail=%s", symbol, status_code, exc)
    except Exception as exc:
        error_text = str(exc)
        if isinstance(exc, requests.exceptions.SSLError) or "UNEXPECTED_EOF_WHILE_READING" in error_text:
            with CACHE_LOCK:
                FINNHUB_BACKOFF_UNTIL = time.time() + max(30, SETTINGS.finnhub_failure_cooldown_seconds)
        SYS_LOG.warning("finnhub_quote_failed symbol=%s detail=%s", symbol, error_text)
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
    with CACHE_LOCK:
        backoff_until = YAHOO_SCREENER_BACKOFF_UNTIL
    if now_ts < backoff_until:
        SYS_LOG.info("screener_temporarily_skipped scr_id=%s backoff_until=%s", scr_id, int(backoff_until))
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
            with CACHE_LOCK:
                YAHOO_SCREENER_BACKOFF_UNTIL = time.time() + max(30, SETTINGS.screener_failure_cooldown_seconds)
                backoff_until = YAHOO_SCREENER_BACKOFF_UNTIL
            SYS_LOG.warning(
                "screener_ssl_backoff_active until=%s detail=%s",
                int(backoff_until),
                error_text,
            )
        SYS_LOG.warning("screener_fetch_failed scr_id=%s detail=%s", scr_id, error_text)
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
    with CACHE_LOCK:
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
    with CACHE_LOCK:
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


def _rank_quotes_by_change(quotes: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for quote in quotes:
        pct_change = _as_float(quote.get("regularMarketChangePercent"))
        if pct_change is None:
            continue
        ranked.append((pct_change, quote))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [quote for _, quote in ranked[:top_n]]


def _collect_stock_quotes_by_screener(
    scr_ids: List[str], count_per_screener: int
) -> Dict[str, List[Dict[str, Any]]]:
    """Per-screener stock quotes, each list kept in the screener's own order.

    _collect_live_quotes merges every screener into one flat, deduped pool,
    which loses which screener a symbol came from. Candidate discovery needs
    that origin preserved so slots can be allocated fairly across screeners
    (see _build_live_universe) instead of by a global rank that a high-%-swing
    screener like day_gainers would always win.
    """
    by_screener: Dict[str, List[Dict[str, Any]]] = {}
    seen: set = set()
    for scr_id in scr_ids:
        rows = []
        for quote in _fetch_predefined_screener_quotes(scr_id, count=count_per_screener):
            if not _is_stock_quote(quote):
                continue
            symbol = (quote.get("symbol") or "").strip()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            rows.append(quote)
        by_screener[scr_id] = rows
    return by_screener


def _round_robin_merge(lists: List[List[Dict[str, Any]]], limit: int) -> List[Dict[str, Any]]:
    """Interleave several ranked lists instead of concatenating them.

    One slot from each list per round, so no single source can fill the
    entire cap before the others contribute anything.
    """
    merged: List[Dict[str, Any]] = []
    index = 0
    while len(merged) < limit and any(index < len(lst) for lst in lists):
        for lst in lists:
            if index < len(lst):
                merged.append(lst[index])
                if len(merged) >= limit:
                    break
        index += 1
    return merged


def _build_live_universe(limit: int = 40) -> Dict[str, str]:
    """Candidate discovery for the trade screen.

    Stock slots are allocated round-robin across STOCK_SCREENERS rather than
    by a global %-change rank: a momentum screener like day_gainers reports
    far larger swings than a calmer source like undervalued_large_caps, so
    ranking the merged pool by change let day_gainers/most_actives fill every
    slot before calmer screeners contributed a single symbol -- leaving the
    trade screen's volatility gate with nothing that could ever pass it.
    """
    stock_by_screener = _collect_stock_quotes_by_screener(
        STOCK_SCREENERS, count_per_screener=SETTINGS.screener_quote_limit
    )
    selected_stocks = _round_robin_merge(list(stock_by_screener.values()), limit)

    fund_quotes = _collect_live_quotes(FUND_SCREENERS, count_per_screener=SETTINGS.screener_quote_limit)
    funds = [quote for quote in fund_quotes if _is_fund_quote(quote)]
    selected_funds = _rank_quotes_by_change(funds, max(10, limit // 2))

    universe: Dict[str, str] = {}
    for quote in selected_stocks + selected_funds:
        symbol = str(quote.get("symbol") or "").strip()
        name = _truncate(_quote_name(quote), 28)
        if symbol and symbol not in universe.values():
            universe[name] = symbol
    return universe


def _business_news_section() -> Tuple[str, List[Selection]]:
    """Business headlines only -- no live quotes. Split out from
    `get_business_and_stocks` so `/news` can send pure news without the
    live market tables that aren't news at all."""
    return news_feeds._build_news_section(
        section_key="business_news",
        scope="business",
        title="💼 Top Business Stories:",
        feed_urls=BUSINESS_NEWS_FEEDS,
        empty_message="No fresh business headlines available right now.",
        max_headlines=8,
    )


def _live_market_snapshot() -> str:
    """Live stock/fund quote tables only -- no headlines. The counterpart to
    `_business_news_section`, split out for the same reason."""
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

    return "\n".join(stock_section) + "\n\n" + "\n".join(fund_section)


def get_business_and_stocks() -> Tuple[str, List[Selection]]:
    biz_str, biz_selections = _business_news_section()
    return f"{biz_str.strip()}\n\n{_live_market_snapshot()}", biz_selections


def _fetch_ticker_history(symbol: str, period: str = "3mo", interval: str = "1d") -> Any:
    """Fetch and cache yfinance OHLCV history to avoid repeated slow calls."""
    cache_key = f"{symbol}|{period}|{interval}"
    now_ts = time.time()
    ttl = max(60, SETTINGS.trade_history_cache_ttl_seconds)
    with CACHE_LOCK:
        cached = HISTORY_CACHE.get(cache_key)
        if cached:
            cached_at, history = cached
            if now_ts - cached_at <= ttl:
                return history
    history = _with_retry(
        lambda: yf.Ticker(symbol).history(
            period=period, interval=interval, auto_adjust=True
        ),
        f"yf_history_{symbol}",
        retries=2,
    )
    with CACHE_LOCK:
        HISTORY_CACHE[cache_key] = (time.time(), history)
    return history


def _fetch_ticker_fundamentals(symbol: str) -> Dict[str, Any]:
    """Fetch and cache yfinance's `.info` snapshot (company profile,
    valuation, margins, balance-sheet ratios).

    Fundamentals move far slower than price, so this gets its own longer TTL
    (`TICKER_INFO_CACHE_TTL_SECONDS`, default 6h) separate from the price
    history cache above. A failed or partial fetch degrades to an empty
    dict rather than raising -- `build_ticker_analysis` renders every field
    as "not available" via `.get(...)` on a missing key either way.
    """
    cache_key = f"info|{symbol}"
    now_ts = time.time()
    ttl = max(300, SETTINGS.ticker_info_cache_ttl_seconds)
    with CACHE_LOCK:
        cached = TICKER_INFO_CACHE.get(cache_key)
        if cached:
            cached_at, info = cached
            if now_ts - cached_at <= ttl:
                return info
    try:
        info = _with_retry(
            lambda: yf.Ticker(symbol).info, f"yf_info_{symbol}", retries=2
        )
        if not isinstance(info, dict):
            info = {}
    except Exception as exc:
        SYS_LOG.debug("ticker_info_fetch_failed symbol=%s detail=%s", symbol, exc)
        info = {}
    with CACHE_LOCK:
        TICKER_INFO_CACHE[cache_key] = (time.time(), info)
    return info
