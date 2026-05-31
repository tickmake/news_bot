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

    def test_top_weekly_performers_data_selects_best_three(self):
        with patch("news_bot._compute_weekly_change") as mock_change:
            mock_change.side_effect = [(101.0, 1.0), (110.0, 10.0), (106.0, 6.0), (95.0, -5.0)]
            lines = news_bot._top_weekly_performers_data({"A": "A", "B": "B", "C": "C", "D": "D"}, top_n=3)

        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0][0], "B")
        self.assertAlmostEqual(lines[0][3], 10.0)

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

        with patch.object(news_bot, "_source_allowed", return_value=True):
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
        with patch.object(news_bot, "_source_allowed", return_value=True):
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

        with patch.object(news_bot, "_source_allowed", return_value=True):
            result = news_bot.get_norwegian_morning_news()

        self.assertIn("Early Morning Norway News", result)
        self.assertIn("NRK headline", result)
        self.assertIn("https://example.no/1", result)

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


if __name__ == "__main__":
    unittest.main()
