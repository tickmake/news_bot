import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

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

    @patch("news_bot._compute_trade_metrics")
    def test_get_trade_candidates_formats_ranked_output(self, mock_metrics):
        def side_effect(symbol):
            data = {
                "AAA": news_bot.TradeMetrics(
                    symbol="AAA", session="2026-08-13", last_close=100.0,
                    day_change_pct=1.2, week_momentum_pct=3.5, volume_ratio=1.6,
                    drawdown_pct=2.1, atr_pct=1.4, above_ema20=True,
                ),
                "BBB": news_bot.TradeMetrics(
                    symbol="BBB", session="2026-08-13", last_close=80.0,
                    day_change_pct=0.4, week_momentum_pct=2.1, volume_ratio=1.3,
                    drawdown_pct=3.0, atr_pct=2.0, above_ema20=True,
                ),
                "CCC": None,
            }
            return data.get(symbol)

        mock_metrics.side_effect = side_effect

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
        self.assertIn("Not investment advice. No guarantee of profit.", result)

    @patch("news_bot._compute_trade_metrics")
    def test_get_trade_candidates_no_matches(self, mock_metrics):
        mock_metrics.return_value = None
        result = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=2)
        self.assertIn("No candidates qualified.", result)

    @patch("news_bot.requests.get")
    def test_get_global_news_success(self, mock_get):
        news_bot.STATE.data["sent_headline_keys"] = {}
        mock_response = MagicMock()
        mock_response.text = (
            "<rss><channel>"
            "<item><title>Fed holds interest rates steady</title>"
            "<link>https://example.com/1</link></item>"
            "<item><title>Norway raises fuel duty next year</title>"
            "<link>https://example.com/2</link></item>"
            "</channel></rss>"
        )
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with patch.object(news_bot.SETTINGS, "news_api_key", ""), patch.object(
            news_bot.SETTINGS, "freenews_api_key", ""
        ), patch.object(news_bot.SETTINGS, "freen_ews_api_key", ""), patch.object(
            news_bot.SETTINGS, "news_ranker_enabled", False
        ), patch.object(
            news_bot, "_source_allowed", return_value=True
        ):
            result, _ = news_bot.get_global_news()

        self.assertIn("Top Global News", result)
        self.assertIn("Fed holds interest rates steady", result)
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
            news_bot.SETTINGS, "news_ranker_enabled", False
        ), patch.object(
            news_bot, "_source_allowed", return_value=True
        ):
            result, _ = news_bot.get_global_news()
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
            news_bot.SETTINGS, "news_ranker_enabled", False
        ), patch.object(
            news_bot, "_source_allowed", return_value=True
        ):
            result, _ = news_bot.get_norwegian_morning_news()

        self.assertIn("Early Morning Norway News", result)
        self.assertIn("NRK headline", result)
        self.assertIn("https://example.no/1", result)

    @patch("news_bot.requests.get")
    def test_get_global_news_pools_api_and_rss_providers(self, mock_get):
        news_bot.STATE.data["sent_headline_keys"] = {}
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "articles": [{"title": "API headline", "url": "https://example.com/api"}]
        }
        mock_response.text = (
            "<rss><channel>"
            "<item><title>RSS headline</title><link>https://example.com/rss</link></item>"
            "</channel></rss>"
        )
        mock_get.return_value = mock_response

        with patch.object(news_bot.SETTINGS, "news_api_key", "abc"), patch.object(
            news_bot.SETTINGS, "freenews_api_key", ""
        ), patch.object(news_bot.SETTINGS, "freen_ews_api_key", ""), patch.object(
            news_bot.SETTINGS, "news_fetch_priority", "newsapi,rss"
        ), patch.object(
            news_bot.SETTINGS, "news_ranker_enabled", False
        ), patch.object(news_bot, "_source_allowed", return_value=True):
            result, _selections = news_bot.get_global_news()

        # Both providers now contribute to one pool rather than the first
        # provider short-circuiting the rest.
        self.assertIn("API headline", result)
        self.assertIn("RSS headline", result)

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
            '1. Market headline (<a href="https://example.com/market">more</a>)\n',
            [],
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
            result, _selections = news_bot.get_business_and_stocks()
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
            self.assertTrue(
                reloaded.has_seen_headline(
                    "global_news", "key1", window_days=7, today="2026-06-01"
                )
            )

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

    @patch("news_bot._compute_trade_metrics")
    def test_get_trade_candidates_caps_universe(self, mock_metrics):
        mock_metrics.return_value = None
        big_universe = {f"Name{i}": f"SYM{i}" for i in range(50)}
        with patch.object(news_bot.SETTINGS, "trade_universe_max", 5):
            news_bot.get_trade_candidates(universe=big_universe, top_n=3)
        self.assertEqual(mock_metrics.call_count, 5)

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


class FeedDateParsingTests(unittest.TestCase):
    def test_parses_rfc2822_pubdate(self):
        parsed = news_bot._parse_feed_datetime("Thu, 13 Aug 2026 04:55:58 GMT")
        self.assertEqual(parsed, datetime(2026, 8, 13, 4, 55, 58, tzinfo=timezone.utc))

    def test_parses_iso8601_with_trailing_z(self):
        parsed = news_bot._parse_feed_datetime("2026-08-13T10:00:47Z")
        self.assertEqual(parsed, datetime(2026, 8, 13, 10, 0, 47, tzinfo=timezone.utc))

    def test_parses_iso8601_with_offset_and_normalises_to_utc(self):
        parsed = news_bot._parse_feed_datetime("2026-08-13T12:00:00+02:00")
        self.assertEqual(parsed, datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc))

    def test_naive_timestamp_is_treated_as_utc(self):
        parsed = news_bot._parse_feed_datetime("2026-08-13T10:00:00")
        self.assertEqual(parsed, datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc))

    def test_returns_none_for_malformed_input(self):
        self.assertIsNone(news_bot._parse_feed_datetime("not a date"))

    def test_returns_none_for_empty_and_missing_input(self):
        self.assertIsNone(news_bot._parse_feed_datetime(""))
        self.assertIsNone(news_bot._parse_feed_datetime(None))


class RssDateExtractionTests(unittest.TestCase):
    def test_extracts_pubdate_from_rss_item(self):
        xml_text = (
            "<rss><channel>"
            "<item><title>T1</title><link>https://example.com/1</link>"
            "<pubDate>Thu, 13 Aug 2026 04:55:58 GMT</pubDate></item>"
            "</channel></rss>"
        )
        items = news_bot._parse_rss_items(xml_text)
        self.assertEqual(len(items), 1)
        title, url, published = items[0]
        self.assertEqual(title, "T1")
        self.assertEqual(url, "https://example.com/1")
        self.assertEqual(published, datetime(2026, 8, 13, 4, 55, 58, tzinfo=timezone.utc))

    def test_prefers_pubdate_over_dc_date(self):
        xml_text = (
            '<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
            "<item><title>T1</title><link>https://example.com/1</link>"
            "<pubDate>Thu, 13 Aug 2026 04:00:00 GMT</pubDate>"
            "<dc:date>2026-08-13T09:00:00Z</dc:date></item>"
            "</channel></rss>"
        )
        _, _, published = news_bot._parse_rss_items(xml_text)[0]
        self.assertEqual(published.hour, 4)

    def test_falls_back_to_dc_date_when_pubdate_absent(self):
        xml_text = (
            '<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
            "<item><title>T1</title><link>https://example.com/1</link>"
            "<dc:date>2026-08-13T09:48:44Z</dc:date></item>"
            "</channel></rss>"
        )
        _, _, published = news_bot._parse_rss_items(xml_text)[0]
        self.assertEqual(published, datetime(2026, 8, 13, 9, 48, 44, tzinfo=timezone.utc))

    def test_item_without_any_date_yields_none(self):
        xml_text = (
            "<rss><channel>"
            "<item><title>T1</title><link>https://example.com/1</link></item>"
            "</channel></rss>"
        )
        _, _, published = news_bot._parse_rss_items(xml_text)[0]
        self.assertIsNone(published)

    def test_atom_entry_published_is_parsed(self):
        xml_text = (
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><title>A1</title>"
            '<link href="https://example.com/a1" rel="alternate"/>'
            "<published>2026-08-13T06:30:00Z</published></entry>"
            "</feed>"
        )
        title, url, published = news_bot._parse_rss_items(xml_text)[0]
        self.assertEqual(title, "A1")
        self.assertEqual(url, "https://example.com/a1")
        self.assertEqual(published, datetime(2026, 8, 13, 6, 30, 0, tzinfo=timezone.utc))


class XmlHardeningTests(unittest.TestCase):
    def test_entity_expansion_bomb_is_rejected(self):
        bomb = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE lolz [<!ENTITY lol "lol">'
            '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
            "<rss><channel><item><title>&lol3;</title></item></channel></rss>"
        )
        with self.assertRaises(Exception):
            news_bot._parse_rss_items(bomb)

    def test_ordinary_feeds_still_parse(self):
        xml_text = (
            "<rss><channel>"
            "<item><title>Normal</title><link>https://example.com/1</link></item>"
            "</channel></rss>"
        )
        self.assertEqual(len(news_bot._parse_rss_items(xml_text)), 1)


class CandidateModelTests(unittest.TestCase):
    def test_trusted_domain_is_flagged(self):
        candidate = news_bot._make_candidate(
            "Headline", "https://www.reuters.com/x", None, "rss"
        )
        self.assertEqual(candidate.domain, "reuters.com")
        self.assertTrue(candidate.trusted)

    def test_unknown_domain_is_not_trusted(self):
        candidate = news_bot._make_candidate(
            "Headline", "https://random-blog.example/x", None, "rss"
        )
        self.assertFalse(candidate.trusted)

    def test_age_hours_is_none_without_a_date(self):
        candidate = news_bot._make_candidate("Headline", None, None, "rss")
        self.assertIsNone(news_bot._age_hours(candidate))

    def test_age_hours_computed_from_published_at(self):
        now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
        candidate = news_bot._make_candidate(
            "Headline", None, now - timedelta(hours=5), "rss"
        )
        self.assertAlmostEqual(news_bot._age_hours(candidate, now), 5.0, places=3)

    def test_future_timestamps_clamp_to_zero(self):
        now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
        candidate = news_bot._make_candidate(
            "Headline", None, now + timedelta(hours=2), "rss"
        )
        self.assertEqual(news_bot._age_hours(candidate, now), 0.0)


class ClusterKeyTests(unittest.TestCase):
    def test_identical_titles_share_a_cluster_key(self):
        self.assertEqual(
            news_bot._cluster_key("Fed holds rates steady"),
            news_bot._cluster_key("Fed holds rates steady"),
        )

    def test_cluster_key_ignores_case_and_punctuation(self):
        self.assertEqual(
            news_bot._cluster_key("Fed holds rates steady!"),
            news_bot._cluster_key("fed holds rates, steady"),
        )

    def test_cluster_key_ignores_word_order(self):
        self.assertEqual(
            news_bot._cluster_key("Fed holds rates steady"),
            news_bot._cluster_key("Rates steady, Fed holds"),
        )

    def test_cluster_key_ignores_stopwords(self):
        self.assertEqual(
            news_bot._cluster_key("The Fed holds the rates steady"),
            news_bot._cluster_key("Fed holds rates steady"),
        )

    def test_different_stories_have_different_cluster_keys(self):
        self.assertNotEqual(
            news_bot._cluster_key("Fed holds rates steady"),
            news_bot._cluster_key("Norway raises fuel duty"),
        )


class DedupWindowTests(unittest.TestCase):
    def setUp(self):
        self.state = news_bot.AppState(path=os.path.join(tempfile.mkdtemp(), "state.json"))

    def test_headline_seen_yesterday_is_suppressed_today(self):
        self.state.mark_headline_seen("global", ["k1"], "2026-08-12")
        self.assertTrue(
            self.state.has_seen_headline("global", "k1", window_days=7, today="2026-08-13")
        )

    def test_headline_outside_the_window_is_not_suppressed(self):
        self.state.mark_headline_seen("global", ["k1"], "2026-08-01")
        self.assertFalse(
            self.state.has_seen_headline("global", "k1", window_days=3, today="2026-08-13")
        )

    def test_window_is_measured_in_calendar_days_not_buckets_present(self):
        """A gap in sends must not drag a stale bucket back into the window."""
        self.state.mark_headline_seen("global", ["old"], "2026-08-01")
        self.state.mark_headline_seen("global", ["new"], "2026-08-13")
        self.assertFalse(
            self.state.has_seen_headline("global", "old", window_days=7, today="2026-08-13")
        )
        self.assertTrue(
            self.state.has_seen_headline("global", "new", window_days=7, today="2026-08-13")
        )

    def test_window_boundary_is_inclusive_of_today_and_six_days_back(self):
        """window_days=7 means today plus the six days before it."""
        self.state.mark_headline_seen("global", ["inside"], "2026-08-07")
        self.state.mark_headline_seen("global", ["outside"], "2026-08-06")
        self.assertTrue(
            self.state.has_seen_headline("global", "inside", window_days=7, today="2026-08-13")
        )
        self.assertFalse(
            self.state.has_seen_headline("global", "outside", window_days=7, today="2026-08-13")
        )

    def test_bare_string_key_is_treated_as_one_key_not_characters(self):
        self.state.mark_headline_seen("global", "key1", "2026-08-13")
        self.assertTrue(
            self.state.has_seen_headline("global", "key1", window_days=7, today="2026-08-13")
        )
        self.assertFalse(
            self.state.has_seen_headline("global", "k", window_days=7, today="2026-08-13")
        )

    def test_sections_do_not_leak_into_each_other(self):
        self.state.mark_headline_seen("global", ["k1"], "2026-08-12")
        self.assertFalse(
            self.state.has_seen_headline("norway", "k1", window_days=7, today="2026-08-13")
        )

    def test_multiple_keys_are_marked_together(self):
        self.state.mark_headline_seen("global", ["exact", "cluster"], "2026-08-13")
        self.assertTrue(
            self.state.has_seen_headline("global", "exact", window_days=7, today="2026-08-13")
        )
        self.assertTrue(
            self.state.has_seen_headline("global", "cluster", window_days=7, today="2026-08-13")
        )

    def test_bucket_retention_is_bounded(self):
        for day in range(1, 15):
            self.state.mark_headline_seen("global", [f"k{day}"], f"2026-08-{day:02d}")
        buckets = self.state.data["sent_headline_keys"]["global"]
        self.assertLessEqual(len(buckets), 7)


class RecentTitleTests(unittest.TestCase):
    def setUp(self):
        self.state = news_bot.AppState(path=os.path.join(tempfile.mkdtemp(), "state.json"))

    def test_recent_titles_are_returned_within_the_window(self):
        self.state.record_recent_title("Fed holds rates steady", "2026-08-12")
        self.assertIn(
            "Fed holds rates steady",
            self.state.recent_titles(window_days=7, today="2026-08-13"),
        )

    def test_recent_titles_outside_the_window_are_excluded(self):
        self.state.record_recent_title("Old story", "2026-08-01")
        self.assertNotIn(
            "Old story", self.state.recent_titles(window_days=3, today="2026-08-13")
        )

    def test_recent_titles_are_capped(self):
        for index in range(50):
            self.state.record_recent_title(f"Story {index}", "2026-08-13")
        self.assertLessEqual(
            len(self.state.recent_titles(window_days=7, limit=30, today="2026-08-13")), 30
        )

    def test_duplicate_titles_are_not_stored_twice(self):
        self.state.record_recent_title("Same story", "2026-08-13")
        self.state.record_recent_title("Same story", "2026-08-13")
        titles = self.state.recent_titles(window_days=7, today="2026-08-13")
        self.assertEqual(titles.count("Same story"), 1)


FIXED_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _cand(title, url=None, hours_old=1.0, trusted=True, provider="rss"):
    """Test helper: build a Candidate with an age relative to FIXED_NOW."""
    published = None if hours_old is None else FIXED_NOW - timedelta(hours=hours_old)
    base = news_bot._make_candidate(title, url, published, provider)
    return news_bot.Candidate(
        title=base.title,
        url=base.url,
        domain=base.domain,
        published_at=base.published_at,
        provider=base.provider,
        trusted=trusted,
    )


def _history(closes, volumes=None, last_date=None, tz="America/New_York"):
    """Build a yfinance-shaped OHLCV frame ending on `last_date`.

    The default `last_date` is derived from the SAME timezone (`tz`) the
    index gets stamped with below. Deriving it from the host's local
    timezone instead (e.g. `datetime.now().date()`) would let the two
    disagree: when the host clock is a calendar day ahead of `tz`, "host
    yesterday" is actually `tz` TODAY, so `_drop_in_progress_bar` correctly
    drops it and the fixture silently loses its last row.
    """
    n = len(closes)
    end = last_date or (datetime.now(ZoneInfo(tz)).date() - timedelta(days=1))
    idx = pd.date_range(end=pd.Timestamp(end), periods=n, freq="D", tz=tz)
    close = pd.Series([float(c) for c in closes], index=idx, dtype="float64")
    vols = volumes if volumes is not None else [1_000_000] * n
    return pd.DataFrame(
        {
            "Close": close,
            "High": close * 1.005,
            "Low": close * 0.995,
            "Volume": pd.Series([float(v) for v in vols], index=idx, dtype="float64"),
        }
    )


class TopicWeightTests(unittest.TestCase):
    def test_defaults_are_used_when_override_is_empty(self):
        with patch.object(news_bot.SETTINGS, "news_topic_weights", ""):
            weights = news_bot._resolve_topic_weights()
        self.assertEqual(weights["markets"], news_bot.TOPIC_CATEGORIES["markets"]["weight"])

    def test_override_replaces_a_single_category(self):
        with patch.object(news_bot.SETTINGS, "news_topic_weights", "markets:5.0"):
            weights = news_bot._resolve_topic_weights()
        self.assertEqual(weights["markets"], 5.0)
        self.assertEqual(weights["sports"], news_bot.TOPIC_CATEGORIES["sports"]["weight"])

    def test_unknown_categories_in_the_override_are_ignored(self):
        with patch.object(news_bot.SETTINGS, "news_topic_weights", "nonsense:9.0"):
            weights = news_bot._resolve_topic_weights()
        self.assertNotIn("nonsense", weights)

    def test_malformed_override_entries_are_skipped(self):
        with patch.object(news_bot.SETTINGS, "news_topic_weights", "markets:abc,tech:1.0"):
            weights = news_bot._resolve_topic_weights()
        self.assertEqual(weights["markets"], news_bot.TOPIC_CATEGORIES["markets"]["weight"])
        self.assertEqual(weights["tech"], 1.0)


DISTINCT_HEADLINES = [
    "Fed holds interest rates steady after inflation data",
    "Norway raises fuel duty in autumn budget proposal",
    "India central bank signals caution on rupee volatility",
    "Semiconductor makers warn of oversupply next quarter",
    "Storm warning issued across the western coastline",
    "Housing starts fall for a third consecutive month",
    "Oil majors report weaker refining margins",
    "Regulators open inquiry into cloud outage",
    "Wheat prices climb on export restrictions",
    "Transit strike disrupts commuter services",
]


class HeuristicSelectorTests(unittest.TestCase):
    def setUp(self):
        self.selector = news_bot.HeuristicSelector(now=FIXED_NOW)

    def test_returns_at_most_the_limit(self):
        pool = [
            _cand(title, f"https://a.example/{i}")
            for i, title in enumerate(DISTINCT_HEADLINES)
        ]
        self.assertEqual(len(self.selector.select(pool, "global", 5)), 5)

    def test_headlines_differing_by_one_token_are_treated_as_duplicates(self):
        """Documents a real limitation: short titles are noisy under shingle
        similarity, so near-identical phrasings collapse. Acceptable, because
        real headlines that differ by one token usually are the same story."""
        pool = [_cand(f"Story number {i}", f"https://a.example/{i}") for i in range(5)]
        self.assertEqual(len(self.selector.select(pool, "global", 5)), 1)

    def test_empty_pool_returns_empty_list(self):
        self.assertEqual(self.selector.select([], "global", 5), [])

    def test_fresher_story_outranks_older_one_all_else_equal(self):
        pool = [
            _cand("Fed holds rates steady in August", "https://a.example/old", hours_old=20),
            _cand("Fed lifts rates sharply in August", "https://a.example/new", hours_old=1),
        ]
        result = self.selector.select(pool, "global", 1)
        self.assertEqual(result[0].candidate.url, "https://a.example/new")

    def test_upweighted_topic_outranks_downweighted_topic(self):
        pool = [
            _cand("Celebrity wedding photos revealed", "https://a.example/celeb"),
            _cand("Fed signals inflation rate decision", "https://a.example/fed"),
        ]
        result = self.selector.select(pool, "global", 1)
        self.assertEqual(result[0].candidate.url, "https://a.example/fed")

    def test_downweight_survives_a_colliding_upweight_keyword(self):
        """A celebrity story mentioning Oslo must not inherit Norway's boost.

        Taking max() of matched weights made down-weights inert whenever any
        up-weight keyword also matched.
        """
        pool = [
            _cand("Actor spotted at film premiere in Oslo", "https://a.example/celeb"),
            _cand("Bond yields rise ahead of Treasury auction", "https://a.example/bonds"),
        ]
        result = self.selector.select(pool, "global", 1)
        self.assertEqual(result[0].candidate.url, "https://a.example/bonds")

    def test_pure_upweight_still_outranks_a_collision(self):
        pool = [
            _cand("Actor spotted at film premiere in Oslo", "https://a.example/celeb"),
            _cand("Norges Bank signals rate decision for Norway", "https://a.example/nb"),
        ]
        result = self.selector.select(pool, "global", 1)
        self.assertEqual(result[0].candidate.url, "https://a.example/nb")

    def test_downweighted_story_still_appears_when_nothing_better_exists(self):
        pool = [_cand("Football match ends in a draw", "https://a.example/sport")]
        self.assertEqual(len(self.selector.select(pool, "global", 5)), 1)

    def test_trusted_source_breaks_a_tie(self):
        pool = [
            _cand("Fed rate decision today", "https://untrusted.example/x", trusted=False),
            _cand("Fed rate verdict today", "https://www.reuters.com/x", trusted=True),
        ]
        result = self.selector.select(pool, "global", 1)
        self.assertEqual(result[0].candidate.domain, "reuters.com")

    def test_reworded_duplicates_collapse_into_one_selection(self):
        pool = [
            _cand("Fed holds interest rates steady", "https://a.example/1"),
            _cand("Fed holds steady interest rates", "https://b.example/2"),
        ]
        result = self.selector.select(pool, "global", 5)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].duplicates), 1)

    def test_identical_urls_collapse_even_with_different_titles(self):
        pool = [
            _cand("Version one of the headline", "https://a.example/same"),
            _cand("A completely different phrasing entirely", "https://a.example/same"),
        ]
        self.assertEqual(len(self.selector.select(pool, "global", 5)), 1)

    def test_distinct_stories_are_not_collapsed(self):
        pool = [
            _cand("Fed holds interest rates steady", "https://a.example/1"),
            _cand("Norway raises fuel duty next year", "https://b.example/2"),
        ]
        self.assertEqual(len(self.selector.select(pool, "global", 5)), 2)

    def test_undated_candidate_is_eligible_and_scores_neutrally(self):
        pool = [_cand("Some undated headline", "https://a.example/1", hours_old=None)]
        self.assertEqual(len(self.selector.select(pool, "global", 5)), 1)

    def test_selection_is_deterministic(self):
        pool = [
            _cand(title, f"https://a.example/{i}")
            for i, title in enumerate(DISTINCT_HEADLINES)
        ]
        first = [s.candidate.url for s in self.selector.select(pool, "global", 5)]
        second = [s.candidate.url for s in self.selector.select(pool, "global", 5)]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 5)

    def test_duplicates_beyond_the_limit_are_still_collected(self):
        """Duplicates must be marked seen even when the limit is already full."""
        pool = [
            _cand("Fed holds interest rates steady", "https://a.example/1"),
            _cand("Norway raises fuel duty next year", "https://b.example/2"),
            _cand("Fed holds steady interest rates", "https://c.example/3"),
        ]
        result = self.selector.select(pool, "global", 1)
        self.assertEqual(len(result), 1)
        total = sum(1 + len(s.duplicates) for s in result)
        self.assertGreaterEqual(total, 2)


def _ollama_response(payload_text, status=200):
    """Stub of an Ollama /api/chat response."""
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {"message": {"role": "assistant", "content": payload_text}}
    response.raise_for_status.return_value = None
    return response


class _StubTransport:
    """Records calls and returns a canned Ollama response."""

    def __init__(self, payload=None, exception=None, raw_text=None, response=None):
        self.payload = payload
        self.exception = exception
        self.raw_text = raw_text
        self.response = response
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.exception is not None:
            raise self.exception
        if self.response is not None:
            return self.response
        text = self.raw_text if self.raw_text is not None else json.dumps(self.payload)
        return _ollama_response(text)

    @property
    def body(self):
        return self.calls[0]["json"]


class LlmSelectorTests(unittest.TestCase):
    def setUp(self):
        self.pool = [
            _cand("Fed holds interest rates steady", "https://a.example/1"),
            _cand("Federal Reserve keeps rates unchanged", "https://b.example/2"),
            _cand("Norway raises fuel duty", "https://c.example/3"),
        ]

    def _select(self, transport, limit=2):
        selector = news_bot.LlmSelector(transport=transport, now=FIXED_NOW, recent_titles=[])
        return selector.select(self.pool, "global", limit)

    def test_selects_by_returned_index(self):
        t = _StubTransport({"selections": [{"id": 2, "duplicate_ids": [], "reason": "big"}]})
        self.assertEqual(self._select(t)[0].candidate.url, "https://c.example/3")

    def test_duplicate_ids_are_attached_to_the_selection(self):
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [1], "reason": "dupe"}]})
        result = self._select(t)
        self.assertEqual(len(result[0].duplicates), 1)
        self.assertEqual(result[0].duplicates[0].url, "https://b.example/2")

    def test_out_of_range_ids_are_dropped(self):
        t = _StubTransport(
            {
                "selections": [
                    {"id": 99, "duplicate_ids": [], "reason": "bad"},
                    {"id": 0, "duplicate_ids": [], "reason": "ok"},
                ]
            }
        )
        result = self._select(t)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].candidate.url, "https://a.example/1")

    def test_out_of_range_duplicate_ids_are_dropped(self):
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [99], "reason": "x"}]})
        self.assertEqual(self._select(t)[0].duplicates, [])

    def test_non_integer_ids_are_dropped(self):
        t = _StubTransport(
            {
                "selections": [
                    {"id": "zero", "duplicate_ids": [], "reason": "x"},
                    {"id": 1, "duplicate_ids": [], "reason": "y"},
                ]
            }
        )
        result = self._select(t)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].candidate.url, "https://b.example/2")

    def test_repeated_ids_are_deduplicated(self):
        t = _StubTransport(
            {
                "selections": [
                    {"id": 0, "duplicate_ids": [], "reason": "a"},
                    {"id": 0, "duplicate_ids": [], "reason": "b"},
                ]
            }
        )
        self.assertEqual(len(self._select(t)), 1)

    def test_result_is_truncated_to_the_limit(self):
        t = _StubTransport(
            {"selections": [{"id": i, "duplicate_ids": [], "reason": "x"} for i in range(3)]}
        )
        self.assertEqual(len(self._select(t, limit=2)), 2)

    def test_empty_pool_does_not_call_the_model(self):
        t = _StubTransport({"selections": []})
        selector = news_bot.LlmSelector(transport=t, now=FIXED_NOW, recent_titles=[])
        self.assertEqual(selector.select([], "global", 5), [])
        self.assertEqual(t.calls, [])

    def test_recent_titles_are_included_in_the_prompt(self):
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [], "reason": "x"}]})
        selector = news_bot.LlmSelector(
            transport=t, now=FIXED_NOW, recent_titles=["Previously shown story"]
        )
        selector.select(self.pool, "global", 2)
        self.assertIn("Previously shown story", json.dumps(t.body))

    def test_candidate_titles_are_included_in_the_prompt(self):
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [], "reason": "x"}]})
        self._select(t)
        self.assertIn("Norway raises fuel duty", json.dumps(t.body))

    def test_prompt_frames_candidates_as_untrusted_data(self):
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [], "reason": "x"}]})
        self._select(t)
        self.assertIn("UNTRUSTED", json.dumps(t.body))

    def test_request_targets_the_configured_ollama_endpoint(self):
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [], "reason": "x"}]})
        with patch.object(news_bot.SETTINGS, "news_ranker_url", "http://ollama:11434"):
            self._select(t)
        self.assertEqual(t.calls[0]["url"], "http://ollama:11434/api/chat")

    def test_request_pins_the_json_schema_and_model(self):
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [], "reason": "x"}]})
        self._select(t)
        self.assertEqual(t.body["format"], news_bot.RANKER_RESPONSE_SCHEMA)
        self.assertEqual(t.body["model"], news_bot.SETTINGS.news_ranker_model)
        self.assertFalse(t.body["stream"])

    def test_pool_is_shortlisted_before_the_call(self):
        """Local inference times out on a full pool, so only the heuristic's
        best N are sent."""
        pool = [
            _cand(title, f"https://a.example/{i}")
            for i, title in enumerate(DISTINCT_HEADLINES)
        ]
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [], "reason": "x"}]})
        selector = news_bot.LlmSelector(transport=t, now=FIXED_NOW, recent_titles=[])
        with patch.object(news_bot.SETTINGS, "news_ranker_max_candidates", 3):
            selector.select(pool, "global", 2)
        sent = t.body["messages"][1]["content"]
        self.assertEqual(sent.count("\n["), 3)

    def test_shortlist_keeps_ids_aligned_with_what_was_sent(self):
        """Returned ids index the shortlist, not the original pool."""
        pool = [
            _cand("Celebrity wedding photos revealed", "https://a.example/celeb"),
            _cand("Fed signals inflation rate decision", "https://a.example/fed"),
        ]
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [], "reason": "x"}]})
        selector = news_bot.LlmSelector(transport=t, now=FIXED_NOW, recent_titles=[])
        with patch.object(news_bot.SETTINGS, "news_ranker_max_candidates", 1):
            result = selector.select(pool, "global", 1)
        # The heuristic ranks the markets story first, so id 0 is that one.
        self.assertEqual(result[0].candidate.url, "https://a.example/fed")

    def test_small_pool_is_sent_untouched(self):
        pool = [_cand("Only story", "https://a.example/1")]
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [], "reason": "x"}]})
        selector = news_bot.LlmSelector(transport=t, now=FIXED_NOW, recent_titles=[])
        with patch.object(news_bot.SETTINGS, "news_ranker_max_candidates", 20):
            self.assertEqual(len(selector.select(pool, "global", 5)), 1)

    def test_request_is_deterministic(self):
        t = _StubTransport({"selections": [{"id": 0, "duplicate_ids": [], "reason": "x"}]})
        self._select(t)
        self.assertEqual(t.body["options"]["temperature"], 0)


class LlmSelectorFallthroughTests(unittest.TestCase):
    """Every failure mode must fall through, never raise."""

    def setUp(self):
        self.pool = [_cand("Fed holds interest rates steady", "https://a.example/1")]

    def _select_with(self, transport):
        selector = news_bot.LlmSelector(transport=transport, now=FIXED_NOW, recent_titles=[])
        return selector.select(self.pool, "global", 5)

    def test_missing_transport_returns_empty(self):
        selector = news_bot.LlmSelector(transport=None, now=FIXED_NOW, recent_titles=[])
        self.assertEqual(selector.select(self.pool, "global", 5), [])

    def test_connection_error_returns_empty(self):
        """The common case: Ollama is not running."""
        exc = news_bot.requests.exceptions.ConnectionError("refused")
        self.assertEqual(self._select_with(_StubTransport(exception=exc)), [])

    def test_timeout_returns_empty(self):
        exc = news_bot.requests.exceptions.Timeout("too slow")
        self.assertEqual(self._select_with(_StubTransport(exception=exc)), [])

    def test_http_error_returns_empty(self):
        response = MagicMock()
        response.status_code = 404
        response.raise_for_status.side_effect = news_bot.requests.exceptions.HTTPError(
            "model not found"
        )
        self.assertEqual(self._select_with(_StubTransport(response=response)), [])

    def test_malformed_json_returns_empty(self):
        self.assertEqual(self._select_with(_StubTransport(raw_text="not json at all")), [])

    def test_missing_selections_key_returns_empty(self):
        self.assertEqual(self._select_with(_StubTransport({"wrong": []})), [])

    def test_unexpected_envelope_returns_empty(self):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {"unexpected": "shape"}
        self.assertEqual(self._select_with(_StubTransport(response=response)), [])

    def test_all_ids_invalid_returns_empty(self):
        t = _StubTransport({"selections": [{"id": 99, "duplicate_ids": [], "reason": "x"}]})
        self.assertEqual(self._select_with(t), [])

    def test_failure_is_recorded_for_health(self):
        news_bot.RANKER_STATUS["error"] = None
        self._select_with(_StubTransport(exception=RuntimeError("boom")))
        self.assertIsNotNone(news_bot.RANKER_STATUS["error"])

    def test_success_clears_a_previous_error(self):
        news_bot.RANKER_STATUS["error"] = "stale"
        self._select_with(
            _StubTransport({"selections": [{"id": 0, "duplicate_ids": [], "reason": "x"}]})
        )
        self.assertIsNone(news_bot.RANKER_STATUS["error"])


class SelectorFactoryTests(unittest.TestCase):
    def test_heuristic_used_when_ranker_disabled(self):
        with patch.object(news_bot.SETTINGS, "news_ranker_enabled", False):
            selector = news_bot._build_selector("global", FIXED_NOW)
        self.assertIsInstance(selector, news_bot.HeuristicSelector)

    def test_heuristic_used_when_no_url_configured(self):
        with patch.object(news_bot.SETTINGS, "news_ranker_enabled", True), patch.object(
            news_bot.SETTINGS, "news_ranker_url", ""
        ):
            selector = news_bot._build_selector("global", FIXED_NOW)
        self.assertIsInstance(selector, news_bot.HeuristicSelector)

    def test_llm_selector_used_when_enabled_and_url_present(self):
        """No credential needed — a reachable local endpoint is the only gate."""
        with patch.object(news_bot.SETTINGS, "news_ranker_enabled", True), patch.object(
            news_bot.SETTINGS, "news_ranker_url", "http://ollama:11434"
        ):
            selector = news_bot._build_selector("global", FIXED_NOW)
        self.assertIsInstance(selector, news_bot.LlmSelector)


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        news_bot.STATE.data["sent_headline_keys"] = {}
        news_bot.STATE.data["recent_titles"] = {}

    @staticmethod
    def _rss(*items):
        """items: (title, pubdate_or_None) pairs."""
        body = "".join(
            f"<item><title>{title}</title><link>https://example.com/{i}</link>"
            + (f"<pubDate>{pub}</pubDate>" if pub else "")
            + "</item>"
            for i, (title, pub) in enumerate(items)
        )
        response = MagicMock()
        response.text = f"<rss><channel>{body}</channel></rss>"
        response.raise_for_status.return_value = None
        return response

    @staticmethod
    def _patches():
        return (
            patch.object(news_bot.SETTINGS, "news_api_key", ""),
            patch.object(news_bot.SETTINGS, "freenews_api_key", ""),
            patch.object(news_bot.SETTINGS, "freen_ews_api_key", ""),
            patch.object(news_bot.SETTINGS, "news_ranker_enabled", False),
            patch.object(news_bot, "_source_allowed", return_value=True),
        )

    def _run(self, mock_get, response):
        mock_get.return_value = response
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            return news_bot.get_global_news()
        finally:
            for item in patches:
                item.stop()

    @patch("news_bot.requests.get")
    def test_section_renders_and_returns_selections(self, mock_get):
        fresh = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        rendered, selections = self._run(
            mock_get, self._rss(("Fed rate decision", fresh), ("Norway budget talks", fresh))
        )
        self.assertIn("Top Global News", rendered)
        self.assertGreaterEqual(len(selections), 1)

    @patch("news_bot.requests.get")
    def test_empty_pool_renders_the_empty_message(self, mock_get):
        response = MagicMock()
        response.text = "<rss><channel></channel></rss>"
        response.raise_for_status.return_value = None
        rendered, selections = self._run(mock_get, response)
        self.assertIn("No fresh global headlines", rendered)
        self.assertEqual(selections, [])

    @patch("news_bot.requests.get")
    def test_stale_dated_items_are_dropped_by_the_ceiling(self, mock_get):
        rendered, selections = self._run(
            mock_get, self._rss(("Ancient story", "Mon, 01 Jun 2020 10:00:00 GMT"))
        )
        self.assertEqual(selections, [])
        self.assertIn("No fresh global headlines", rendered)

    @patch("news_bot.requests.get")
    def test_undated_items_survive_the_ceiling(self, mock_get):
        rendered, selections = self._run(mock_get, self._rss(("Undated story", None)))
        self.assertIn("Undated story", rendered)
        self.assertEqual(len(selections), 1)

    @patch("news_bot.requests.get")
    def test_already_seen_headline_is_suppressed(self, mock_get):
        fresh = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        today = datetime.today().strftime("%Y-%m-%d")
        news_bot.STATE.mark_headline_seen(
            "global_news", news_bot._headline_keys("Seen already", None), today
        )
        rendered, selections = self._run(mock_get, self._rss(("Seen already", fresh)))
        self.assertEqual(selections, [])


class MarkOnSendTests(unittest.TestCase):
    def setUp(self):
        news_bot.STATE.data["sent_headline_keys"] = {}
        news_bot.STATE.data["recent_titles"] = {}

    @staticmethod
    def _briefing():
        candidate = _cand("Fed holds rates steady", "https://a.example/1")
        return news_bot.Briefing(
            message="body",
            pending=[
                ("global_news", key)
                for key in news_bot._headline_keys(candidate.title, candidate.url)
            ],
            titles=[candidate.title],
        )

    def _run_job(self, send_succeeds):
        with patch.object(news_bot, "compose_briefing", return_value=self._briefing()), patch.object(
            news_bot, "send_telegram_message", return_value=send_succeeds
        ), patch.object(news_bot.STATE, "save"):
            return news_bot.job_daily_briefing()

    def _seen(self):
        return news_bot.STATE.has_seen_headline(
            "global_news", news_bot._cluster_key("Fed holds rates steady"), window_days=7
        )

    def test_failed_send_does_not_mark_headlines_seen(self):
        self.assertFalse(self._run_job(send_succeeds=False))
        self.assertFalse(self._seen())

    def test_successful_send_marks_headlines_seen(self):
        self.assertTrue(self._run_job(send_succeeds=True))
        self.assertTrue(self._seen())

    def test_successful_send_records_titles_for_the_recent_window(self):
        self._run_job(send_succeeds=True)
        self.assertIn("Fed holds rates steady", news_bot.STATE.recent_titles(window_days=7))

    def test_failed_send_records_no_titles(self):
        self._run_job(send_succeeds=False)
        self.assertEqual(news_bot.STATE.recent_titles(window_days=7), [])

    def test_collapsed_duplicates_are_marked_alongside_the_shown_headline(self):
        shown = _cand("Fed holds interest rates steady", "https://a.example/1")
        dupe = _cand("Federal Reserve keeps rates unchanged", "https://b.example/2")
        selection = news_bot.Selection(candidate=shown, duplicates=[dupe], reason="")
        pending = news_bot._pending_keys("global_news", [selection])
        self.assertIn(("global_news", news_bot._cluster_key(dupe.title)), pending)

    def test_every_news_section_contributes_pending_keys(self):
        """Regression guard: _is_duplicate_headline is read-only, so any section
        whose selections are not committed loses deduplication entirely."""
        selection = news_bot.Selection(
            candidate=_cand("Business story", "https://a.example/b"), duplicates=[], reason=""
        )
        with patch.object(
            news_bot, "get_norwegian_morning_news", return_value=("n", [])
        ), patch.object(news_bot, "get_global_news", return_value=("g", [])), patch.object(
            news_bot, "get_business_and_stocks", return_value=("b", [selection])
        ), patch.object(
            news_bot, "get_trade_candidates", return_value="t"
        ), patch.object(
            news_bot, "build_daily_intro", return_value="i"
        ):
            briefing = news_bot.compose_briefing(datetime(2026, 8, 13, 7, 0))
        sections = {section for section, _key in briefing.pending}
        self.assertIn("business_news", sections)

    def test_command_briefing_commits_only_on_success(self):
        with patch.object(news_bot, "compose_briefing", return_value=self._briefing()), patch.object(
            news_bot, "send_telegram_message", return_value=False
        ):
            news_bot._send_briefing(datetime(2026, 8, 13, 7, 0), "chat-1")
        self.assertFalse(self._seen())

        with patch.object(news_bot, "compose_briefing", return_value=self._briefing()), patch.object(
            news_bot, "send_telegram_message", return_value=True
        ):
            news_bot._send_briefing(datetime(2026, 8, 13, 7, 0), "chat-1")
        self.assertTrue(self._seen())


class FeedFairShareTests(unittest.TestCase):
    """One prolific feed must not starve the others.

    Cross-outlet deduplication only works if the pool actually contains items
    from more than one outlet.
    """

    @staticmethod
    def _feed_items(prefix, count):
        return [(f"{prefix} story {i}", f"https://{prefix}.example/{i}", None) for i in range(count)]

    def test_prolific_first_feed_does_not_starve_later_feeds(self):
        def fake_fetch(feed_url, max_items=20):
            counts = {"a": 100, "b": 100, "c": 100}
            name = feed_url.split("//")[1].split(".")[0]
            return self._feed_items(name, counts[name])[:max_items]

        with patch.object(news_bot, "_fetch_rss_items", side_effect=fake_fetch):
            items = news_bot._fetch_rss_from_feeds(
                ["https://a.example/f", "https://b.example/f", "https://c.example/f"],
                max_items=30,
            )
        domains = {url.split("//")[1].split(".")[0] for _t, url, _p in items}
        self.assertEqual(domains, {"a", "b", "c"})
        self.assertLessEqual(len(items), 30)

    def test_short_feeds_do_not_waste_the_quota(self):
        def fake_fetch(feed_url, max_items=20):
            name = feed_url.split("//")[1].split(".")[0]
            count = 1 if name == "a" else 100
            return self._feed_items(name, count)[:max_items]

        with patch.object(news_bot, "_fetch_rss_items", side_effect=fake_fetch):
            items = news_bot._fetch_rss_from_feeds(
                ["https://a.example/f", "https://b.example/f"], max_items=30
            )
        self.assertEqual(len(items), 30)

    def test_failing_feed_does_not_break_the_others(self):
        def fake_fetch(feed_url, max_items=20):
            if "broken" in feed_url:
                return []
            return self._feed_items("ok", 10)[:max_items]

        with patch.object(news_bot, "_fetch_rss_items", side_effect=fake_fetch):
            items = news_bot._fetch_rss_from_feeds(
                ["https://broken.example/f", "https://ok.example/f"], max_items=20
            )
        self.assertEqual(len(items), 10)

    def test_empty_feed_list_returns_empty(self):
        self.assertEqual(news_bot._fetch_rss_from_feeds([], max_items=10), [])


class TrustedDomainTests(unittest.TestCase):
    def test_bbc_co_uk_links_are_allowed(self):
        """BBC RSS links point at bbc.co.uk; an allowlist with only bbc.com
        silently dropped every BBC item."""
        self.assertTrue(news_bot._source_allowed("https://www.bbc.co.uk/news/articles/abc"))

    def test_bbc_com_links_are_still_allowed(self):
        self.assertTrue(news_bot._source_allowed("https://www.bbc.com/news/articles/abc"))

    def test_untrusted_domain_is_still_rejected(self):
        self.assertFalse(news_bot._source_allowed("https://not-a-real-outlet.example/x"))


class HealthReportTests(unittest.TestCase):
    def tearDown(self):
        news_bot.RANKER_STATUS.update(
            {"path": "unknown", "latency_ms": None, "error": None, "error_at": None}
        )

    def test_health_report_includes_the_ranker_path(self):
        news_bot.RANKER_STATUS.update({"path": "llm", "latency_ms": 1840, "error": None})
        report = news_bot.build_health_report()
        self.assertIn("Ranker path: llm", report)
        self.assertIn("1840", report)

    def test_health_report_surfaces_the_last_ranker_error(self):
        news_bot.RANKER_STATUS.update(
            {
                "path": "heuristic",
                "error": "APITimeoutError: timed out",
                "error_at": "2026-08-13T07:00:01",
            }
        )
        self.assertIn("APITimeoutError", news_bot.build_health_report())

    def test_health_report_omits_the_error_line_when_clean(self):
        news_bot.RANKER_STATUS.update({"path": "heuristic", "latency_ms": None, "error": None})
        self.assertNotIn("Last ranker error", news_bot.build_health_report())

    def test_health_report_retains_pre_existing_fields(self):
        report = news_bot.build_health_report()
        self.assertIn("Bot Health Check", report)
        self.assertIn("Last run status", report)
        self.assertIn("Timezone", report)


class TradeMetricsTests(unittest.TestCase):
    def tearDown(self):
        news_bot.HISTORY_CACHE.clear()

    def _cache(self, symbol, history):
        news_bot.HISTORY_CACHE[f"{symbol}|3mo|1d"] = (time.time(), history)

    def test_drops_bar_dated_today(self):
        today = datetime.now(timezone.utc).date()
        history = _history([100.0] * 40, last_date=today, tz="UTC")
        trimmed = news_bot._drop_in_progress_bar(history)
        self.assertEqual(len(trimmed), 39)
        self.assertLess(trimmed.index[-1].date(), today)

    def test_keeps_bar_dated_before_today(self):
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        history = _history([100.0] * 40, last_date=yesterday, tz="UTC")
        trimmed = news_bot._drop_in_progress_bar(history)
        self.assertEqual(len(trimmed), 40)

    def test_returns_none_below_thirty_usable_bars(self):
        self._cache("SHORT", _history([100.0] * 29))
        self.assertIsNone(news_bot._compute_trade_metrics("SHORT"))

    def test_metrics_computed_on_known_fixture(self):
        closes = [100.0] * 35 + [100.0, 101.0, 102.0, 103.0, 104.0]
        self._cache("RISE", _history(closes))
        metrics = news_bot._compute_trade_metrics("RISE")
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.symbol, "RISE")
        self.assertAlmostEqual(metrics.last_close, 104.0, places=4)
        # 104 vs prior close 103
        self.assertAlmostEqual(metrics.day_change_pct, 0.970873786, places=6)
        # 104 vs the close five bars back (100.0)
        self.assertAlmostEqual(metrics.week_momentum_pct, 4.0, places=6)
        self.assertTrue(metrics.above_ema20)
        self.assertAlmostEqual(metrics.drawdown_pct, 0.0, places=6)
        # Volume is uniform across the fixture (default 1_000_000/bar), so the
        # last bar's volume equals its own 20-bar average.
        self.assertAlmostEqual(metrics.volume_ratio, 1.0, places=6)
        # True range is constant at 1% of close (High/Low = close * 1.005/0.995)
        # for every bar except the flat->rising transition, which is why ATR%
        # settles just above 1% rather than exactly at it.
        self.assertAlmostEqual(metrics.atr_pct, 1.102335, places=5)

    def test_session_reflects_last_kept_bar(self):
        last = datetime.now().date() - timedelta(days=3)
        self._cache("SESS", _history([100.0] * 40, last_date=last))
        metrics = news_bot._compute_trade_metrics("SESS")
        self.assertEqual(metrics.session, last.strftime("%Y-%m-%d"))


def _metrics(**overrides):
    """A qualifying TradeMetrics, with fields overridden per test."""
    base = dict(
        symbol="AAA",
        session="2026-08-13",
        last_close=100.0,
        day_change_pct=1.0,
        week_momentum_pct=3.0,
        volume_ratio=1.5,
        drawdown_pct=2.0,
        atr_pct=2.0,
        above_ema20=True,
    )
    base.update(overrides)
    return news_bot.TradeMetrics(**base)


class TradeGateTests(unittest.TestCase):
    def test_qualifying_metrics_fail_nothing(self):
        self.assertEqual(news_bot._failed_gates(_metrics()), [])

    def test_declining_stock_is_rejected(self):
        failed = news_bot._failed_gates(
            _metrics(above_ema20=False, week_momentum_pct=-0.26, day_change_pct=-0.05)
        )
        self.assertEqual(failed[0], "trend")
        self.assertIn("momentum_5d", failed)
        self.assertIn("momentum_1d", failed)

    def test_flat_stock_is_rejected(self):
        failed = news_bot._failed_gates(
            _metrics(above_ema20=False, week_momentum_pct=0.0, day_change_pct=0.0)
        )
        self.assertTrue(failed)

    def test_volume_spike_alone_cannot_admit(self):
        """The primary defect: risk criteria plus volume used to reach the
        passing score with negative momentum. Volume no longer admits at all."""
        failed = news_bot._failed_gates(
            _metrics(
                above_ema20=False,
                week_momentum_pct=-0.26,
                day_change_pct=-0.05,
                volume_ratio=2.73,   # capitulation-sized spike
                drawdown_pct=0.99,   # safely near the 20d high
                atr_pct=1.00,        # comfortably low volatility
            )
        )
        self.assertNotEqual(failed, [])

    def test_day_change_of_exactly_zero_fails_momentum_1d(self):
        """The gate uses strict `<=` against the default 0.0 threshold, so an
        exactly-flat day must fail rather than pass on a boundary tie."""
        with patch.object(news_bot.SETTINGS, "trade_min_day_change_pct", 0.0):
            failed = news_bot._failed_gates(_metrics(day_change_pct=0.0))
        self.assertIn("momentum_1d", failed)

    def test_high_volatility_is_rejected(self):
        with patch.object(news_bot.SETTINGS, "trade_max_atr_pct", 4.5):
            self.assertEqual(news_bot._failed_gates(_metrics(atr_pct=9.0)), ["volatility"])

    def test_deep_drawdown_is_rejected(self):
        with patch.object(news_bot.SETTINGS, "trade_max_drawdown_pct", 8.0):
            self.assertEqual(news_bot._failed_gates(_metrics(drawdown_pct=20.0)), ["drawdown"])

    def test_gate_order_is_stable(self):
        failed = news_bot._failed_gates(
            _metrics(above_ema20=False, week_momentum_pct=-1.0, atr_pct=99.0)
        )
        self.assertEqual(failed, ["trend", "momentum_5d", "volatility"])


class TradeRankingTests(unittest.TestCase):
    def test_orders_by_week_momentum_first(self):
        low = _metrics(symbol="LOW", week_momentum_pct=1.0)
        high = _metrics(symbol="HIGH", week_momentum_pct=9.0)
        self.assertEqual(
            [m.symbol for m in sorted([low, high], key=news_bot._rank_key)],
            ["HIGH", "LOW"],
        )

    def test_volume_breaks_momentum_ties(self):
        quiet = _metrics(symbol="QUIET", week_momentum_pct=3.0, volume_ratio=1.0)
        busy = _metrics(symbol="BUSY", week_momentum_pct=3.0, volume_ratio=2.0)
        self.assertEqual(
            [m.symbol for m in sorted([quiet, busy], key=news_bot._rank_key)],
            ["BUSY", "QUIET"],
        )

    def test_lower_atr_breaks_volume_ties(self):
        calm = _metrics(symbol="CALM", week_momentum_pct=3.0, volume_ratio=1.5, atr_pct=1.0)
        wild = _metrics(symbol="WILD", week_momentum_pct=3.0, volume_ratio=1.5, atr_pct=4.0)
        self.assertEqual(
            [m.symbol for m in sorted([calm, wild], key=news_bot._rank_key)],
            ["CALM", "WILD"],
        )

    def test_symbol_makes_full_ties_deterministic(self):
        a = _metrics(symbol="AAA")
        b = _metrics(symbol="BBB")
        self.assertEqual(
            [m.symbol for m in sorted([b, a], key=news_bot._rank_key)],
            ["AAA", "BBB"],
        )


class ScreenUniverseDeadlineTests(unittest.TestCase):
    """Regression: `_screen_universe` checks the deadline AFTER consuming and
    counting `future.result()`. Checking first would discard work that had
    already finished by the time the loop got around to looking at it."""

    def _qualifying(self, symbol):
        return news_bot.TradeMetrics(
            symbol=symbol, session="2026-08-13", last_close=100.0,
            day_change_pct=1.0, week_momentum_pct=5.0, volume_ratio=1.6,
            drawdown_pct=1.0, atr_pct=1.0, above_ema20=True,
        )

    def test_already_finished_result_survives_an_expired_deadline(self):
        # time.time() is called once to set the baseline deadline, then once
        # per loop iteration to check it. Returning a huge value from the
        # second call onward simulates a deadline that has already expired
        # by the time the (single) in-flight future finishes.
        with patch.object(news_bot.SETTINGS, "trade_total_deadline_seconds", 1), patch.object(
            news_bot.time, "time", side_effect=[0.0] + [1e12] * 20
        ), patch("news_bot._compute_trade_metrics", return_value=self._qualifying("AAA")):
            outcome, _labels = news_bot._screen_universe([("Alpha", "AAA")])

        self.assertTrue(outcome.deadline_hit)
        self.assertEqual(outcome.analysed, 1)
        self.assertEqual([m.symbol for m in outcome.qualified], ["AAA"])

    def test_deadline_hit_rendering_shows_analysed_of_checked(self):
        with patch.object(news_bot.SETTINGS, "trade_total_deadline_seconds", 1), patch.object(
            news_bot.time, "time", side_effect=[0.0] + [1e12] * 20
        ), patch("news_bot._compute_trade_metrics", return_value=self._qualifying("AAA")):
            result = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=1)

        self.assertIn("AAA", result)
        self.assertIn("qualified 1", result)
        self.assertIn("(deadline hit, analysed 1 of 1)", result)


class CandidateUniverseTests(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch.object(news_bot, "USA_STOCK_UNIVERSE", {"Apple": "AAPL"}),
            patch.object(news_bot, "INDIA_STOCK_UNIVERSE", {"Infosys": "INFY.NS"}),
            patch.object(news_bot, "NORWAY_STOCK_UNIVERSE", {"Equinor": "EQNR.OL"}),
            patch.object(news_bot, "INDIA_MUTUAL_FUNDS", {}),
            patch.object(news_bot, "NORWAY_MUTUAL_FUNDS", {}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_watchlist_survives_healthy_screeners(self):
        """Regression: the watchlist used to be appended after ~67 screener
        names and then truncated away entirely."""
        screener = {f"Mover {i}": f"SCR{i}" for i in range(67)}
        with patch.object(news_bot, "_build_live_universe", return_value=screener):
            universe = news_bot._build_candidate_universe(30)
        symbols = [symbol for _, symbol in universe]
        self.assertEqual(len(symbols), 30)
        for expected in ("AAPL", "INFY.NS", "EQNR.OL"):
            self.assertIn(expected, symbols)

    def test_watchlist_entries_come_first(self):
        screener = {f"Mover {i}": f"SCR{i}" for i in range(10)}
        with patch.object(news_bot, "_build_live_universe", return_value=screener):
            universe = news_bot._build_candidate_universe(10)
        self.assertEqual(
            [symbol for _, symbol in universe[:3]], ["AAPL", "INFY.NS", "EQNR.OL"]
        )

    def test_screener_not_called_when_watchlist_fills_budget(self):
        with patch.object(news_bot, "_build_live_universe") as mock_live:
            universe = news_bot._build_candidate_universe(3)
        mock_live.assert_not_called()
        self.assertEqual(len(universe), 3)

    def test_oversized_watchlist_truncates_and_warns(self):
        with patch.object(news_bot, "USA_STOCK_UNIVERSE", {f"N{i}": f"S{i}" for i in range(10)}):
            with patch.object(news_bot, "_build_live_universe") as mock_live:
                with self.assertLogs(news_bot.LOGGER, level="WARNING") as logs:
                    universe = news_bot._build_candidate_universe(4)
        mock_live.assert_not_called()
        self.assertEqual(len(universe), 4)
        self.assertTrue(any("watchlist_truncated" in line for line in logs.output))

    def test_symbol_in_both_appears_once_with_watchlist_label(self):
        screener = {"Apple Inc Screener Name": "AAPL", "Other": "OTHR"}
        with patch.object(news_bot, "_build_live_universe", return_value=screener):
            universe = news_bot._build_candidate_universe(10)
        apple_labels = [label for label, symbol in universe if symbol == "AAPL"]
        self.assertEqual(len(apple_labels), 1)
        self.assertIn("[USA Stock]", apple_labels[0])

    def test_labels_carry_market_suffix(self):
        with patch.object(news_bot, "_build_live_universe", return_value={}):
            universe = news_bot._build_candidate_universe(10)
        labels = dict((symbol, label) for label, symbol in universe)
        self.assertEqual(labels["AAPL"], "Apple [USA Stock]")
        self.assertEqual(labels["EQNR.OL"], "Equinor [Norway Stock]")


class TradeRenderingTests(unittest.TestCase):
    def _qualifying(self, symbol, momentum, volume=1.6):
        return news_bot.TradeMetrics(
            symbol=symbol, session="2026-08-13", last_close=100.0,
            day_change_pct=1.0, week_momentum_pct=momentum, volume_ratio=volume,
            drawdown_pct=1.0, atr_pct=1.0, above_ema20=True,
        )

    def test_score_column_is_gone_and_session_present(self):
        with patch("news_bot._compute_trade_metrics", return_value=self._qualifying("AAA", 5.0)):
            result = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=1)
        self.assertIn("Session", result)
        self.assertIn("2026-08-13", result)
        self.assertNotIn("Score", result)

    def test_volume_marker_threshold_is_disclosed(self):
        """The ✓ marker needs a legend, or a reader has no way to know what
        volume ratio it takes to earn one."""
        with patch.object(news_bot.SETTINGS, "trade_min_volume_ratio", 1.2):
            with patch("news_bot._compute_trade_metrics", return_value=self._qualifying("AAA", 5.0)):
                result = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=1)
        self.assertIn("1.2", result)

    def test_qualified_footer_discloses_truncation(self):
        universe = {f"Name{i}": f"SYM{i}" for i in range(7)}
        with patch(
            "news_bot._compute_trade_metrics",
            side_effect=lambda symbol: self._qualifying(symbol, momentum=5.0),
        ):
            result = news_bot.get_trade_candidates(universe=universe, top_n=2)
        self.assertIn("qualified 7 (showing 2)", result)

    def test_qualified_footer_omits_showing_when_not_truncated(self):
        with patch("news_bot._compute_trade_metrics", return_value=self._qualifying("AAA", 5.0)):
            result = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=1)
        self.assertIn("qualified 1", result)
        self.assertNotIn("showing", result)

    def test_volume_confirmation_marker(self):
        with patch.object(news_bot.SETTINGS, "trade_min_volume_ratio", 1.2):
            with patch("news_bot._compute_trade_metrics", return_value=self._qualifying("AAA", 5.0, volume=2.0)):
                confirmed = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=1)
            with patch("news_bot._compute_trade_metrics", return_value=self._qualifying("BBB", 5.0, volume=0.5)):
                unconfirmed = news_bot.get_trade_candidates(universe={"Beta": "BBB"}, top_n=1)
        self.assertIn("2.00✓", confirmed)
        self.assertIn("0.50", unconfirmed)
        self.assertNotIn("0.50✓", unconfirmed)

    def test_diagnostics_attribute_to_first_failing_gate(self):
        rejected = news_bot.TradeMetrics(
            symbol="AAA", session="2026-08-13", last_close=100.0,
            day_change_pct=-1.0, week_momentum_pct=-1.0, volume_ratio=1.0,
            drawdown_pct=1.0, atr_pct=1.0, above_ema20=False,
        )
        with patch("news_bot._compute_trade_metrics", return_value=rejected):
            result = news_bot.get_trade_candidates(universe={"Alpha": "AAA"}, top_n=1)
        self.assertIn("Checked 1", result)
        self.assertIn("qualified 0", result)
        self.assertIn("trend 1", result)
        # Attributed to `trend` only, though momentum_5d also failed.
        self.assertNotIn("momentum_5d", result)

    def test_diagnostic_counts_balance(self):
        def side_effect(symbol):
            if symbol == "AAA":
                return self._qualifying("AAA", 5.0)
            if symbol == "BBB":
                return None
            return news_bot.TradeMetrics(
                symbol=symbol, session="2026-08-13", last_close=100.0,
                day_change_pct=-1.0, week_momentum_pct=-1.0, volume_ratio=1.0,
                drawdown_pct=1.0, atr_pct=1.0, above_ema20=False,
            )

        with patch("news_bot._compute_trade_metrics", side_effect=side_effect):
            result = news_bot.get_trade_candidates(
                universe={"A": "AAA", "B": "BBB", "C": "CCC"}, top_n=3
            )
        self.assertIn("Checked 3", result)
        self.assertIn("no data 1", result)
        self.assertIn("qualified 1", result)
        self.assertIn("trend 1", result)

    def test_ranked_order_in_output(self):
        def side_effect(symbol):
            return self._qualifying(symbol, {"AAA": 1.0, "BBB": 9.0}[symbol])

        with patch("news_bot._compute_trade_metrics", side_effect=side_effect):
            result = news_bot.get_trade_candidates(universe={"A": "AAA", "B": "BBB"}, top_n=2)
        self.assertLess(result.index("BBB"), result.index("AAA"))


class DeprecatedSettingTests(unittest.TestCase):
    def test_warns_when_trade_min_score_is_customised(self):
        with patch.object(news_bot.SETTINGS, "trade_min_score", 5):
            with self.assertLogs(news_bot.LOGGER, level="WARNING") as logs:
                news_bot._warn_deprecated_settings()
        self.assertTrue(any("TRADE_MIN_SCORE" in line for line in logs.output))

    def test_silent_at_default_value(self):
        default = news_bot.AppSettings.model_fields["trade_min_score"].default
        with patch.object(news_bot.SETTINGS, "trade_min_score", default):
            with patch.object(news_bot.LOGGER, "warning") as mock_warn:
                news_bot._warn_deprecated_settings()
        mock_warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
