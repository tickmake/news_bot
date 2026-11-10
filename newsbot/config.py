"""Settings, constants and configured universes."""

from typing import Dict, Iterable, List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict



DEFAULT_GLOBAL_NEWS_FEEDS = (
    "https://feeds.bbci.co.uk/news/world/rss.xml,"
    "https://www.cnbc.com/id/100727362/device/rss/rss.html,"
    "https://feeds.npr.org/1001/rss.xml"
)
DEFAULT_BUSINESS_NEWS_FEEDS = (
    "https://www.cnbc.com/id/10001147/device/rss/rss.html,"
    # finance.yahoo.com/news/rssindex was retired and 404s on every briefing.
    # Yahoo's surviving headline feed is a poor replacement here: most of its
    # items link to fool.com, benzinga.com and stocktwits.com, which the
    # trusted-domain allowlist drops. BBC Business links to bbc.co.uk, which
    # is already trusted.
    "https://feeds.bbci.co.uk/news/business/rss.xml,"
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
        "apnews.com,marketwatch.com,npr.org"
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
    # Wall-clock allowance for composing one briefing. Three LLM-ranked news
    # sections can take ~240s each on the Pi, so sections that have not started
    # when this runs out are skipped rather than letting a briefing run for ever.
    briefing_budget_seconds: int = 900
    trade_min_week_momentum_pct: float = 1.0
    trade_min_day_change_pct: float = 0.0
    trade_min_volume_ratio: float = 1.2
    trade_max_drawdown_pct: float = 8.0
    trade_max_atr_pct: float = 4.5
    trade_universe_max: int = 30
    # Stocks in the nightly analyst-estimate snapshot (configured ones first).
    snapshot_max_symbols: int = 120
    trade_fetch_workers: int = 6
    # Risk analytics shown with trade candidates. These are decision aids that
    # hold whether or not the screen has an edge (it has not been shown to):
    # volatility-based sizing, correlation and sector clustering across the
    # picks, and earnings-date flags. The stop is a volatility stop, a multiple
    # of ATR, and is separate from the "invalidation" level in the sketch.
    risk_analytics_enabled: bool = True
    # Portfolio loss, in percent, if a position is stopped out.
    risk_per_trade_pct: float = 0.5
    risk_stop_atr_multiple: float = 2.0
    # No single position above this share of the portfolio, whatever the stop.
    risk_max_position_pct: float = 20.0
    # Warn when taking every shown pick would put more than this to work.
    risk_max_total_pct: float = 60.0
    # Optional. When set (in your account currency), sizes also show an amount.
    risk_account_size: float = 0.0
    # Broker fee per order (DNB: 0.12%, at least 59 NOK). Shown next to each
    # pick, because at small sizes the minimum is a big share of the position.
    broker_fee_min_nok: float = 59.0
    broker_fee_pct: float = 0.12
    # Evening alert when a stock I own is this far below my average buy price.
    holding_alert_drop_pct: float = 15.0
    # The money's alternative (#69): the monthly scorecard compares with it.
    benchmark_fund_symbol: str = "0P00018V9L.IR"
    benchmark_fund_name: str = "KLP AksjeGlobal Indeks P"
    risk_corr_threshold: float = 0.7
    risk_corr_window: int = 60
    risk_corr_min_obs: int = 30
    # Calendar days; 7 covers the default 5-session hold.
    risk_earnings_warn_days: int = 7
    # Wall-clock allowance for the extra lookups; the screen never waits longer.
    risk_context_deadline_seconds: int = 12
    # Relative strength shown beside the risk lines: the stock's return over the
    # last N completed sessions minus the market's (and its US sector ETF's), plus
    # beta to the market. Context for reading a move, not a ranking signal.
    relative_strength_enabled: bool = True
    relative_strength_windows: str = "20,60"
    relative_strength_beta_min_obs: int = 30
    # Headlines that mention a pick, from the news feeds the bot already reads,
    # shown as "why is it moving" context. Matching is by company name or an
    # explicit ($TICKER) / (TICKER) mention, so it errs on the side of silence.
    catalysts_enabled: bool = True
    catalysts_max_per_pick: int = 2
    catalysts_max_age_hours: int = 36
    catalysts_pool_ttl_seconds: int = 1200
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
    # Nightly snapshots of the trade-history DB. It holds signal_archive and
    # published_picks -- the only out-of-sample record this project keeps, and
    # the only thing here that git cannot reproduce. Unset means no backup
    # runs, so the compose file points this at a bind mount outside the data
    # volume; a path inside it would survive DB corruption but not the volume.
    trade_backup_dir: str = ""
    trade_backup_keep: int = 14
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
    # Technical-analysis charts attached to /recommendations. Sent to Telegram,
    # and to Slack too when a bot token and channel id are set; a webhook alone
    # cannot upload a file (see send_candidate_charts).
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
