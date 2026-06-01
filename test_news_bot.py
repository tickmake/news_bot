import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import news_bot


class NewsBotTests(unittest.TestCase):
    def test_daily_pick_is_deterministic_for_context_and_day(self):
        picked_1 = news_bot._daily_pick(["a", "b", "c"], "ctx", "2026-06-01")
        picked_2 = news_bot._daily_pick(["a", "b", "c"], "ctx", "2026-06-01")
        self.assertEqual(picked_1, picked_2)

    def test_build_daily_intro_morning_contains_quote(self):
        with patch.object(news_bot.SETTINGS, "recipient_name", "Sunil"):
            intro = news_bot.build_daily_intro(datetime(2026, 6, 1, 7, 0))
        self.assertIn("Quote of the day", intro)
        self.assertIn("2026-06-01", intro)
        self.assertIn("Hello, Sunil!", intro)

    def test_build_daily_intro_evening_contains_evening_message(self):
        with patch.object(news_bot.SETTINGS, "recipient_name", "Sunil"):
            intro = news_bot.build_daily_intro(datetime(2026, 6, 1, 19, 0))
        self.assertNotIn("Quote of the day", intro)
        self.assertIn("2026-06-01", intro)
        self.assertIn("Hello, Sunil!", intro)

    def test_parse_instrument_env_parses_label_symbol_pairs(self):
        parsed = news_bot._parse_instrument_env(
            "Apple:AAPL, Microsoft:MSFT, TSLA",
            {"Fallback": "FALL"},
        )
        self.assertEqual(parsed["Apple"], "AAPL")
        self.assertEqual(parsed["Microsoft"], "MSFT")
        self.assertEqual(parsed["TSLA"], "TSLA")

    def test_parse_instrument_env_uses_fallback_for_invalid_or_empty(self):
        fallback = {"Fallback": "FALL"}
        parsed_empty = news_bot._parse_instrument_env("", fallback)
        parsed_invalid = news_bot._parse_instrument_env(" , : , : ", fallback)
        self.assertEqual(parsed_empty, fallback)
        self.assertEqual(parsed_invalid, fallback)

    @patch("news_bot._analyze_short_term_candidate")
    def test_get_trade_candidates_formats_ranked_output(self, mock_analyze):
        def side_effect(symbol):
            data = {
                "AAA": {
                    "score": 4.0,
                    "last_close": 100.0,
                    "day_change_pct": 1.2,
                    "week_momentum_pct": 3.5,
                    "volume_ratio": 1.6,
                    "drawdown_pct": 2.1,
                    "atr_pct": 1.4,
                },
                "BBB": {
                    "score": 3.0,
                    "last_close": 80.0,
                    "day_change_pct": 0.4,
                    "week_momentum_pct": 2.1,
                    "volume_ratio": 1.3,
                    "drawdown_pct": 3.0,
                    "atr_pct": 2.0,
                },
                "CCC": None,
            }
            return data.get(symbol)

        mock_analyze.side_effect = side_effect

        result = news_bot.get_trade_candidates(
            universe={"Alpha": "AAA", "Beta": "BBB", "Gamma": "CCC"},
            top_n=2,
        )

        self.assertIn("Short-Term Trade Candidates", result)
        self.assertIn("Alpha", result)
        self.assertIn("AAA", result)
        self.assertIn("Beta", result)
        self.assertIn("BBB", result)
        self.assertNotIn("Gamma", result)
        self.assertIn("No guarantee of 1-2% daily profit", result)

    @patch("news_bot._analyze_short_term_candidate")
    def test_get_trade_candidates_no_matches(self, mock_analyze):
        mock_analyze.return_value = None
        result = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=2)
        self.assertIn("No candidates met the momentum criteria today.", result)

    @patch("news_bot.requests.get")
    def test_get_global_news_success(self, mock_get):
        news_bot.STATE.data["sent_headline_keys"] = {}
        mock_response = MagicMock()
        mock_response.text = (
            "<rss><channel>"
            "<item><title>Title 1</title><link>https://example.com/1</link></item>"
            "<item><title>Title 2</title><link>https://example.com/2</link></item>"
            "</channel></rss>"
        )
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.object(news_bot.SETTINGS, "news_api_key", ""), patch.object(
            news_bot.SETTINGS, "freenews_api_key", ""
        ), patch.object(news_bot.SETTINGS, "freen_ews_api_key", ""), patch.object(
            news_bot, "_source_allowed", return_value=True
        ):
            result = news_bot.get_global_news()

        self.assertIn("Top Global News", result)
        self.assertIn("Title 1", result)
        self.assertIn("https://example.com/2", result)

    @patch("news_bot.requests.get")
    def test_get_global_news_without_news_api_key_still_works(self, mock_get):
        mock_response = MagicMock()
        mock_response.text = (
            "<rss><channel>"
            "<item><title>Public headline</title><link>https://example.com/1</link></item>"
            "</channel></rss>"
        )
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        with patch.object(news_bot.SETTINGS, "news_api_key", ""), patch.object(
            news_bot.SETTINGS, "freenews_api_key", ""
        ), patch.object(news_bot.SETTINGS, "freen_ews_api_key", ""), patch.object(
            news_bot, "_source_allowed", return_value=True
        ):
            result = news_bot.get_global_news()
        self.assertIn("Public headline", result)

    @patch("news_bot.requests.get")
    def test_get_norwegian_morning_news_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.text = (
            "<rss><channel>"
            "<item><title>NRK headline</title><link>https://example.no/1</link></item>"
            "</channel></rss>"
        )
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.object(news_bot.SETTINGS, "news_api_key", ""), patch.object(
            news_bot.SETTINGS, "freenews_api_key", ""
        ), patch.object(news_bot.SETTINGS, "freen_ews_api_key", ""), patch.object(
            news_bot, "_source_allowed", return_value=True
        ):
            result = news_bot.get_norwegian_morning_news()

        self.assertIn("Early Morning Norway News", result)
        self.assertIn("NRK headline", result)
        self.assertIn("https://example.no/1", result)

    @patch("news_bot.requests.get")
    def test_get_global_news_prefers_newsapi_when_key_present(self, mock_get):
        news_bot.STATE.data["sent_headline_keys"] = {}
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "articles": [
                {"title": "API headline", "url": "https://example.com/api"},
            ]
        }
        mock_get.return_value = mock_response

        with patch.object(news_bot.SETTINGS, "news_api_key", "abc"), patch.object(
            news_bot.SETTINGS, "freenews_api_key", ""
        ), patch.object(news_bot.SETTINGS, "freen_ews_api_key", ""), patch.object(
            news_bot.SETTINGS, "news_fetch_priority", "newsapi,rss"
        ), patch.object(news_bot, "_source_allowed", return_value=True):
            result = news_bot.get_global_news()

        self.assertIn("API headline", result)

    def test_active_finnhub_key_supports_legacy_env_name(self):
        with patch.object(news_bot.SETTINGS, "finnhub_api_key", ""), patch.object(
            news_bot.SETTINGS, "finhub_api_key", "legacy-key"
        ):
            self.assertEqual(news_bot._active_finnhub_key(), "legacy-key")

    @patch("news_bot._collect_live_quotes")
    @patch("news_bot._build_news_section")
    def test_get_business_and_stocks_uses_live_feeds(self, mock_news_section, mock_collect_live):
        news_bot.STATE.data["sent_headline_keys"] = {}
        mock_news_section.return_value = (
            "💼 Top Business Stories:\n"
            '1. Market headline (<a href="https://example.com/market">more</a>)\n'
        )
        mock_collect_live.return_value = [
            {
                "quoteType": "EQUITY",
                "shortName": "Acme Corp",
                "symbol": "ACME",
                "regularMarketPrice": 120.5,
                "regularMarketChangePercent": 4.2,
            },
            {
                "quoteType": "MUTUALFUND",
                "shortName": "Growth Fund",
                "symbol": "GFNDX",
                "regularMarketPrice": 24.1,
                "regularMarketChangePercent": 1.5,
            },
        ]

        with patch.object(news_bot.SETTINGS, "finnhub_api_key", ""), patch.object(
            news_bot.SETTINGS, "finhub_api_key", ""
        ):
            result = news_bot.get_business_and_stocks()
        self.assertIn("Top Business Stories", result)
        self.assertIn("Market headline", result)
        self.assertIn("Live Stock Movers", result)
        self.assertIn("Live Funds & ETFs", result)

    @patch("news_bot.requests.post")
    def test_send_telegram_message_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        with patch.object(news_bot.SETTINGS, "telegram_token", "token"), patch.object(
            news_bot.SETTINGS, "telegram_chat_id", "chat"
        ):
            ok = news_bot.send_telegram_message("hello")

        self.assertTrue(ok)
        self.assertTrue(mock_post.called)
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["parse_mode"], "HTML")

    def test_send_telegram_message_missing_config(self):
        with patch.object(news_bot.SETTINGS, "telegram_token", ""), patch.object(
            news_bot.SETTINGS, "telegram_chat_id", ""
        ):
            ok = news_bot.send_telegram_message("hello")
        self.assertFalse(ok)

    def test_split_message_html_chunks_long_payload(self):
        text = ("section\n\n" * 2000).strip()
        chunks = news_bot._split_message_html(text, 200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))

    def test_source_allowed_blocks_explicit_blocklist(self):
        with patch.object(news_bot, "BLOCKED_DOMAINS", ["bad.com"]), patch.object(
            news_bot, "TRUSTED_DOMAINS", []
        ):
            self.assertFalse(news_bot._source_allowed("https://bad.com/x"))
            self.assertTrue(news_bot._source_allowed("https://good.com/x"))

    def test_get_missing_required_config_reads_settings(self):
        with patch.object(news_bot.SETTINGS, "telegram_token", ""), patch.object(
            news_bot.SETTINGS, "telegram_chat_id", "1"
        ):
            missing = list(news_bot.get_missing_required_config())
        self.assertIn("TELEGRAM_TOKEN", missing)
        self.assertNotIn("NEWS_API_KEY", missing)

    def test_state_save_writes_valid_json_atomically(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "state.json")
            state = news_bot.AppState(path)
            state.telegram_update_offset = 42
            state.mark_headline_seen("global_news", "key1", "2026-06-01")
            state.save()

            # No leftover temp files and the payload reloads cleanly.
            self.assertEqual(os.listdir(tmp_dir), ["state.json"])
            with open(path, "r", encoding="utf-8") as file_obj:
                loaded = json.load(file_obj)
            self.assertEqual(loaded["telegram_update_offset"], 42)

            reloaded = news_bot.AppState(path)
            self.assertEqual(reloaded.telegram_update_offset, 42)
            self.assertTrue(reloaded.has_seen_headline("global_news", "key1", "2026-06-01"))

    def test_fetch_ticker_history_caches_result(self):
        news_bot.HISTORY_CACHE.clear()
        sentinel = MagicMock(name="history_frame")
        with patch("news_bot.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = sentinel
            first = news_bot._fetch_ticker_history("AAPL")
            second = news_bot._fetch_ticker_history("AAPL")
        self.assertIs(first, sentinel)
        self.assertIs(second, sentinel)
        # Second call served from cache: yfinance touched only once.
        self.assertEqual(mock_ticker.call_count, 1)
        news_bot.HISTORY_CACHE.clear()

    @patch("news_bot._analyze_short_term_candidate")
    def test_get_trade_candidates_caps_universe(self, mock_analyze):
        mock_analyze.return_value = None
        big_universe = {f"Name{i}": f"SYM{i}" for i in range(50)}
        with patch.object(news_bot.SETTINGS, "trade_universe_max", 5):
            news_bot.get_trade_candidates(universe=big_universe, top_n=3)
        self.assertEqual(mock_analyze.call_count, 5)

    @patch("news_bot.requests.get")
    def test_poll_telegram_commands_passes_long_poll_timeout(self, mock_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"result": []}
        mock_get.return_value = mock_response
        with patch.object(news_bot.SETTINGS, "telegram_token", "token"), patch.object(
            news_bot.SETTINGS, "command_poll_enabled", True
        ):
            news_bot.poll_telegram_commands(long_poll_timeout=25)
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["timeout"], 25)
        # Read timeout must outlast the server-side hold.
        self.assertGreater(mock_get.call_args.kwargs["timeout"], 25)


if __name__ == "__main__":
    unittest.main()
