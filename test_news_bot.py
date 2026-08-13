import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


if __name__ == "__main__":
    unittest.main()
