"""Settings, constants and configured universes."""

from typing import Dict, Iterable, List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict



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
    slack_webhook_url: str = ""
    # File upload is a separate capability from the webhook. An incoming
    # webhook can only post text, but the Web API's upload endpoints are
    # ordinary outbound HTTPS calls -- no inbound port, no Socket Mode -- so
    # charts can reach Slack without the exposure trade-off that inbound
    # Slack commands would need. Costs a bot token instead.
    slack_bot_token: str = ""
    slack_channel_id: str = ""
    slack_message_max_chars: int = 3900
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
    log_level: str = "INFO"
    log_level_libraries: str = "WARNING"
    log_color: bool = True
    request_timeout_seconds: int = 15
    telegram_message_max_chars: int = 3900
    state_file: str = ".news_bot_state.json"
    command_poll_enabled: bool = True
    command_poll_interval_minutes: int = 2
    health_ping_enabled: bool = True
    health_ping_chat_id: Optional[str] = None
    send_startup_briefing: bool = False
    trusted_news_domains: str = (
        # bbc.co.uk is listed alongside bbc.com because the BBC RSS feeds link
        # to bbc.co.uk; with only bbc.com the allowlist silently dropped every
        # BBC item.
        "reuters.com,bloomberg.com,cnbc.com,finance.yahoo.com,yahoo.com,ft.com,"
        "bbc.com,bbc.co.uk,theguardian.com,nrk.no,aftenposten.no,e24.no,"
        "apnews.com,marketwatch.com"
    )
    blocked_news_domains: str = "news.google.com,pinterest.com,tiktok.com"
    news_max_age_hours: int = 30
    news_candidate_pool_size: int = 60
    news_dedup_window_days: int = 7
    news_recent_title_days: int = 3
    news_topic_weights: str = ""
    news_ranker_enabled: bool = True
    # Ollama-compatible endpoint. In docker-compose this is the bundled service;
    # point it at any other host to reuse an existing Ollama instance.
    news_ranker_url: str = "http://localhost:11434"
    # qwen2.5:3b over llama3.2:3b on measured dedup behaviour: benchmarked on
    # the Pi 5 target across 10, 14 and 20 candidates, llama3.2:3b failed to
    # collapse an obvious two-outlet duplicate ("Fed holds rates steady" and
    # "Federal Reserve keeps benchmark rate unchanged") in every run, ranking
    # both. qwen2.5:3b collapsed it in every run, and was faster. Cross-outlet
    # paraphrase dedup is the reason this path exists at all — HeuristicSelector
    # cannot do it — so that difference outweighs qwen's slightly weaker topic
    # down-weighting.
    news_ranker_model: str = "qwen2.5:3b"
    news_ranker_timeout_seconds: int = 240
    # The LLM re-ranks the heuristic's top N rather than the whole pool. The
    # pool stays large so cross-outlet dedup and the heuristic keep their
    # breadth, while the slow path sees a manageable, already-good shortlist.
    #
    # Measured on the Pi 5 target with a warm model and a schema that actually
    # forces work (see _ranker_response_schema): 20 candidates took 236-269s,
    # at or over the 240s timeout; 14 took 107-193s. The previous "20 ~74s"
    # note was taken while the model was short-circuiting to an empty array,
    # so it timed a no-op. 14 leaves headroom on both models.
    news_ranker_max_candidates: int = 14
    trade_min_week_momentum_pct: float = 1.0
    trade_min_day_change_pct: float = 0.0
    trade_min_volume_ratio: float = 1.2
    trade_max_drawdown_pct: float = 8.0
    trade_max_atr_pct: float = 4.5
    trade_universe_max: int = 30
    trade_fetch_workers: int = 6
    # Market-data health. The container healthcheck only proves threads are
    # alive; if Yahoo returns nothing the bot stays "healthy" while every
    # screen is empty. A screen is "bad" when too many symbols come back with
    # no usable data, or the newest price session is stale.
    market_data_alerts_enabled: bool = True
    market_data_alert_ratio: float = 0.5
    market_data_min_sample: int = 5
    # Calendar days; 6 covers a long weekend plus a holiday without false alarms.
    market_data_max_stale_days: int = 6
    # Consecutive bad screens before alerting (a total outage alerts at once).
    market_data_alert_after: int = 2
    market_data_alert_reminder_hours: int = 24
    trade_history_cache_ttl_seconds: int = 600
    trade_total_deadline_seconds: int = 45
    trade_history_db_file: str = ".news_bot_trade_history.db"
    trade_history_retention_days: int = 14
    # Legacy variable name; this is the fixed backtest holding period in sessions.
    trade_outcome_lookback_days: int = 5
    trade_backtest_period: str = "2y"
    trade_backtest_universe: str = "configured"
    trade_backtest_universe_max: int = 100
    trade_backtest_cost_bps: float = 10.0
    trade_backtest_holdout_start: str = ""
    trade_backtest_min_avg_volume: float = 100000.0
    trade_backtest_benchmarks: str = (
        "DEFAULT:SPY,.NS:^NSEI,.BO:^BSESN,.OL:OSEBX.OL,.DE:^GDAXI,"
        ".PA:^FCHI,.AS:^AEX,.MI:FTSEMIB.MI,.MC:^IBEX,.BR:^BFX,"
        ".L:^FTSE,.SW:^SSMI,.ST:^OMX,.CO:^OMXC25,.HE:^OMXH25"
    )
    trade_weekly_top_picks_lookback_days: int = 7
    trade_weekly_top_picks_min_qualifying_days: int = 2
    trade_weekly_top_picks_count: int = 5
    # Universe continuity. /stocks reads recorded history, so it only has
    # anything to say about symbols re-screened across more than one session.
    # Discovery churns almost completely day to day; keeping recent symbols
    # around gives history-based weekly picks enough observations. Discovery
    # retains reserved slots so it can never crowd out fresh names entirely.
    trade_sticky_universe_days: int = 10
    trade_discovery_min_slots: int = 10
    # Technical-analysis charts attached to /recommendations. Telegram
    # only: an incoming webhook cannot upload a file, so Slack keeps
    # receiving the text rendering (see send_candidate_charts).
    trade_chart_enabled: bool = True
    trade_chart_count: int = 3
    trade_chart_lookback_days: int = 90
    ticker_info_cache_ttl_seconds: int = 21600
    ticker_analysis_summary_max_chars: int = 500
    command_long_poll_timeout_seconds: int = 25
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
    eu_stock_universe: Optional[str] = None
    india_mutual_funds: Optional[str] = None
    norway_mutual_funds: Optional[str] = None

    def missing_required(self) -> List[str]:
        required = {
            "TELEGRAM_TOKEN": self.telegram_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
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


# Unlike the other configured universes below, EU stocks ship with a
# non-empty default: Yahoo's predefined screener endpoint (see
# _fetch_predefined_screener_quotes) is hardcoded to US-market data regardless
# of region/lang params, so EU names can only ever reach the trade screen via
# this configured list -- an empty default would silently mean "no EU
# coverage" exactly like the bug this was added to fix.
DEFAULT_EU_STOCK_UNIVERSE: Dict[str, str] = {
    "SAP": "SAP.DE",
    "Siemens": "SIE.DE",
    "Allianz": "ALV.DE",
    "ASML Holding": "ASML.AS",
    "LVMH": "MC.PA",
    "TotalEnergies": "TTE.PA",
    "L'Oreal": "OR.PA",
    "Sanofi": "SAN.PA",
    "Airbus": "AIR.PA",
    "Enel": "ENEL.MI",
    "Intesa Sanpaolo": "ISP.MI",
    "Iberdrola": "IBE.MC",
    "Inditex": "ITX.MC",
    "Anheuser-Busch InBev": "ABI.BR",
}


USA_STOCK_UNIVERSE = _parse_instrument_env(SETTINGS.usa_stock_universe, {})
INDIA_STOCK_UNIVERSE = _parse_instrument_env(SETTINGS.india_stock_universe, {})
NORWAY_STOCK_UNIVERSE = _parse_instrument_env(SETTINGS.norway_stock_universe, {})
EU_STOCK_UNIVERSE = _parse_instrument_env(SETTINGS.eu_stock_universe, DEFAULT_EU_STOCK_UNIVERSE)
INDIA_MUTUAL_FUNDS = _parse_instrument_env(SETTINGS.india_mutual_funds, {})
NORWAY_MUTUAL_FUNDS = _parse_instrument_env(SETTINGS.norway_mutual_funds, {})


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


def get_missing_required_config() -> Iterable[str]:
    return SETTINGS.missing_required()
