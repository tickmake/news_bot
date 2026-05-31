import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import news_bot


class FakeCloseSeries:
    def __init__(self, values):
        self._values = values
        self.iloc = self

    def __getitem__(self, index):
        return self._values[index]

    def __len__(self):
        return len(self._values)

    def dropna(self):
        return self


class FakeHistory:
    def __init__(self, close_values):
        self._close = FakeCloseSeries(close_values)

    def __len__(self):
        return len(self._close._values)

    def __getitem__(self, key):
        if key == "Close":
            return self._close
        raise KeyError(key)


class NewsBotTests(unittest.TestCase):
    def test_daily_pick_is_deterministic_for_context_and_day(self):
        picked_1 = news_bot._daily_pick(["a", "b", "c"], "ctx", "2026-06-01")
        picked_2 = news_bot._daily_pick(["a", "b", "c"], "ctx", "2026-06-01")
        self.assertEqual(picked_1, picked_2)

    def test_build_daily_intro_morning_contains_quote(self):
        intro = news_bot.build_daily_intro(datetime(2026, 6, 1, 7, 0))
        self.assertIn("Quote of the day", intro)
        self.assertIn("2026-06-01", intro)

    def test_build_daily_intro_evening_contains_evening_message(self):
        intro = news_bot.build_daily_intro(datetime(2026, 6, 1, 19, 0))
        self.assertNotIn("Quote of the day", intro)
        self.assertIn("2026-06-01", intro)

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

    def test_top_weekly_performers_selects_best_three(self):
        with patch("news_bot._compute_weekly_change") as mock_change:
            mock_change.side_effect = [
                (101.0, 1.0),
                (110.0, 10.0),
                (106.0, 6.0),
                (95.0, -5.0),
            ]
            instruments = {
                "A": "A",
                "B": "B",
                "C": "C",
                "D": "D",
            }
            lines = news_bot._top_weekly_performers(instruments, top_n=3)

        self.assertEqual(len(lines), 3)
        self.assertIn("B", lines[0])
        self.assertIn("+10.00%", lines[0])

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
                },
                "BBB": {
                    "score": 3.0,
                    "last_close": 80.0,
                    "day_change_pct": 0.4,
                    "week_momentum_pct": 2.1,
                    "volume_ratio": 1.3,
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
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "articles": [
                {"title": "Title 1", "url": "https://example.com/1"},
                {"title": "Title 2", "url": "https://example.com/2"},
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.object(news_bot, "NEWS_API_KEY", "test-key"):
            result = news_bot.get_global_news()

        self.assertIn("Top Global News", result)
        self.assertIn("Title 1", result)
        self.assertIn("https://example.com/2", result)

    @patch("news_bot.requests.get")
    def test_get_global_news_missing_api_key(self, _mock_get):
        with patch.object(news_bot, "NEWS_API_KEY", None):
            result = news_bot.get_global_news()

        self.assertIn("not configured", result)

    @patch("news_bot.requests.get")
    def test_get_norwegian_morning_news_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "articles": [
                {"title": "NRK headline", "url": "https://example.no/1"},
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.object(news_bot, "NEWS_API_KEY", "test-key"):
            result = news_bot.get_norwegian_morning_news()

        self.assertIn("Early Morning Norway News", result)
        self.assertIn("NRK headline", result)
        self.assertIn("https://example.no/1", result)

    @patch("news_bot.yf.Ticker")
    def test_get_business_and_stocks_handles_yfinance_shapes(self, mock_ticker):
        spy_ticker = MagicMock()
        spy_ticker.news = [
            {
                "content": {
                    "title": "Market headline",
                    "canonicalUrl": {"url": "https://example.com/market"},
                }
            }
        ]

        index_ticker = MagicMock()
        index_ticker.history.return_value = FakeHistory([100.0, 102.0])

        def ticker_side_effect(symbol):
            if symbol == "SPY":
                return spy_ticker
            return index_ticker

        mock_ticker.side_effect = ticker_side_effect

        result = news_bot.get_business_and_stocks()
        self.assertIn("Top 10 Business Stories", result)
        self.assertIn("Market headline", result)
        self.assertIn("Top Weekly Gainers", result)
        self.assertIn("Most Attractive Mutual Funds", result)

    @patch("news_bot.requests.post")
    def test_send_telegram_message_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        with patch.object(news_bot, "TELEGRAM_TOKEN", "token"), patch.object(
            news_bot, "TELEGRAM_CHAT_ID", "chat"
        ):
            ok = news_bot.send_telegram_message("hello")

        self.assertTrue(ok)
        self.assertTrue(mock_post.called)
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["parse_mode"], "HTML")

    def test_send_telegram_message_missing_config(self):
        with patch.object(news_bot, "TELEGRAM_TOKEN", None), patch.object(
            news_bot, "TELEGRAM_CHAT_ID", None
        ):
            ok = news_bot.send_telegram_message("hello")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
