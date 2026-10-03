"""news_feeds: parsing, providers, selection plumbing and candidate collection."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests

from newsbot import config as nb_config
from newsbot import news_feeds as nf
from newsbot import state as nb_state


def _resp(text="", payload=None, status=200):
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _cand(title, url="https://www.bbc.com/a", published=None, provider="rss"):
    return nf._make_candidate(title, url, published, provider)


class DomainTests(unittest.TestCase):
    def test_domain_extraction(self):
        self.assertEqual(nf._safe_domain(None), "")
        self.assertEqual(nf._safe_domain(""), "")
        self.assertEqual(nf._safe_domain("https://www.BBC.co.uk/news/1"), "bbc.co.uk")
        self.assertEqual(nf._safe_domain("not a url"), "")

    def test_unparseable_url_gives_empty_domain(self):
        self.assertEqual(nf._safe_domain("http://[::1"), "")

    def test_source_policy(self):
        with patch.object(nb_config, "BLOCKED_DOMAINS", ["spam.com"]), \
             patch.object(nb_config, "TRUSTED_DOMAINS", ["bbc.com"]):
            self.assertTrue(nf._source_allowed(None))
            self.assertTrue(nf._source_allowed("https://www.bbc.com/x"))
            self.assertFalse(nf._source_allowed("https://spam.com/x"))
            self.assertFalse(nf._source_allowed("https://unknown.org/x"))
        with patch.object(nb_config, "BLOCKED_DOMAINS", []), patch.object(nb_config, "TRUSTED_DOMAINS", []):
            self.assertTrue(nf._source_allowed("https://unknown.org/x"))

    def test_candidate_trust_follows_the_allowlist(self):
        with patch.object(nb_config, "TRUSTED_DOMAINS", ["bbc.com"]):
            self.assertTrue(_cand("t", "https://www.bbc.com/a").trusted)
            self.assertFalse(_cand("t", "https://other.org/a").trusted)
            self.assertFalse(_cand("t", None).trusted)


class FormattingAndDatesTests(unittest.TestCase):
    def test_headline_line_with_and_without_a_link(self):
        self.assertEqual(nf._format_headline_line(2, "A & B", None), "2. A &amp; B")
        line = nf._format_headline_line(1, "<x>", 'https://e.com/?a="1"&b=2')
        self.assertIn("&lt;x&gt;", line)
        self.assertIn("&quot;1&quot;&amp;b=2", line)

    def test_datetime_formats(self):
        rfc = nf._parse_feed_datetime("Mon, 05 Oct 2026 07:00:00 +0200")
        self.assertEqual(rfc, datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(nf._parse_feed_datetime("2026-10-05T07:00:00Z"), datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc))
        self.assertEqual(nf._parse_feed_datetime("2026-10-05T07:00:00"), datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc))
        for junk in (None, "", "   ", "yesterday-ish"):
            self.assertIsNone(nf._parse_feed_datetime(junk))

    def test_age_hours(self):
        now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(nf._age_hours(_cand("t")))
        self.assertEqual(nf._age_hours(_cand("t", published=now - timedelta(hours=3)), now), 3.0)
        # A timestamp slightly in the future clamps to zero rather than going negative.
        self.assertEqual(nf._age_hours(_cand("t", published=now + timedelta(hours=1)), now), 0.0)
        # A naive reference time is treated as UTC.
        self.assertEqual(nf._age_hours(_cand("t", published=now - timedelta(hours=2)), now.replace(tzinfo=None)), 2.0)

    def test_title_shingles_and_jaccard(self):
        self.assertEqual(nf._title_shingles(""), frozenset())
        self.assertEqual(nf._title_shingles("ab"), frozenset({"ab"}))
        self.assertEqual(nf._jaccard(frozenset(), frozenset({"a"})), 0.0)
        self.assertEqual(nf._jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})), 1.0)
        self.assertAlmostEqual(nf._jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})), 1 / 3)


class RssParsingTests(unittest.TestCase):
    RSS = """<rss><channel>
      <item><title>One</title><link>https://a.com/1</link><pubDate>Mon, 05 Oct 2026 07:00:00 GMT</pubDate></item>
      <item><title>Two</title><guid>https://a.com/2</guid></item>
      <item><title>Three</title><guid>not-a-url</guid></item>
      <item><title>Dated</title><link>https://a.com/4</link><dc:date xmlns:dc="http://purl.org/dc/elements/1.1/">2026-10-05T08:00:00Z</dc:date></item>
      <item><title>   </title><link>https://a.com/5</link></item>
    </channel></rss>"""

    def test_rss_items_links_guid_fallback_and_dates(self):
        items = nf._parse_rss_items(self.RSS)
        titles = [item[0] for item in items]
        self.assertEqual(titles, ["One", "Two", "Three", "Dated"])
        self.assertEqual(items[0][1], "https://a.com/1")
        self.assertEqual(items[1][1], "https://a.com/2")       # http guid stands in for a link
        self.assertIsNone(items[2][1])                          # non-http guid does not
        self.assertEqual(items[3][2], datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))

    def test_rss_stops_at_max_items(self):
        self.assertEqual(len(nf._parse_rss_items(self.RSS, max_items=2)), 2)

    ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Alt</title><link rel="self" href="https://a.com/self"/><link rel="alternate" href="https://a.com/alt"/>
        <published>2026-10-05T07:00:00Z</published></entry>
      <entry><title>OnlyRelated</title><link rel="related" href="https://a.com/rel"/><updated>2026-10-05T09:00:00Z</updated></entry>
      <entry><title>NoLink</title></entry>
      <entry><link href="https://a.com/untitled"/></entry>
    </feed>"""

    def test_atom_entries(self):
        items = nf._parse_rss_items(self.ATOM)
        self.assertEqual([i[0] for i in items], ["Alt", "OnlyRelated", "NoLink"])
        self.assertEqual(items[0][1], "https://a.com/alt")      # alternate beats self
        self.assertEqual(items[1][1], "https://a.com/rel")      # falls back to any link
        self.assertIsNone(items[2][1])
        self.assertEqual(items[1][2], datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc))

    def test_atom_stops_at_max_items(self):
        self.assertEqual(len(nf._parse_rss_items(self.ATOM, max_items=1)), 1)

    def test_fetch_success_and_failure(self):
        with patch("requests.get", return_value=_resp(text=self.RSS)):
            self.assertEqual(len(nf._fetch_rss_items("https://a.com/feed")), 4)
        with patch("time.sleep"), patch("requests.get", side_effect=requests.exceptions.ConnectionError("x")), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            self.assertEqual(nf._fetch_rss_items("https://a.com/feed"), [])
        self.assertIn("rss_fetch_failed", "\n".join(logs.output))

    def test_malformed_xml_is_a_failure_not_a_crash(self):
        with patch("requests.get", return_value=_resp(text="<rss><oops>")):
            self.assertEqual(nf._fetch_rss_items("https://a.com/feed"), [])

    def test_feeds_are_interleaved_round_robin(self):
        batches = {
            "f1": [("a1", None, None), ("a2", None, None), ("a3", None, None)],
            "f2": [("b1", None, None)],
            "f3": [("c1", None, None), ("c2", None, None)],
        }
        with patch.object(nf, "_fetch_rss_items", side_effect=lambda url, max_items: batches[url]):
            items = nf._fetch_rss_from_feeds(["f1", "f2", "f3"], max_items=5)
        self.assertEqual([i[0] for i in items], ["a1", "b1", "c1", "a2", "c2"])
        with patch.object(nf, "_fetch_rss_items", side_effect=lambda url, max_items: batches[url]):
            self.assertEqual(len(nf._fetch_rss_from_feeds(["f1", "f2", "f3"], max_items=2)), 2)
        self.assertEqual(nf._fetch_rss_from_feeds([], max_items=5), [])


class JsonProviderTests(unittest.TestCase):
    def test_payload_shapes(self):
        payload = {
            "articles": [
                {"title": " T1 ", "url": " https://a.com/1 ", "publishedAt": "2026-10-05T07:00:00Z"},
                "not a dict",
                {"headline": "T2", "link": "https://a.com/2", "date": "2026-10-05T08:00:00Z"},
                {"name": "T3", "source_url": ""},
                {"title": "   "},
                {"title": 5},
            ],
            "data": [{"title": "T4", "published_at": "2026-10-05T09:00:00Z"}],
            "ignored": [{"title": "nope"}],
        }
        items = nf._extract_articles_from_payload(payload)
        self.assertEqual([i[0] for i in items], ["T1", "T2", "T3", "T4"])
        self.assertEqual(items[0][1], "https://a.com/1")
        self.assertIsNone(items[2][1])
        self.assertEqual(nf._extract_articles_from_payload([{"title": "L"}])[0][0], "L")
        self.assertEqual(nf._extract_articles_from_payload("junk"), [])
        self.assertEqual(nf._extract_articles_from_payload({"articles": "nope"}), [])

    def test_newsapi_requires_a_key(self):
        with patch.object(nb_config.SETTINGS, "news_api_key", ""), patch("requests.get") as get:
            self.assertEqual(nf._fetch_newsapi_items("global"), [])
        get.assert_not_called()

    def test_newsapi_scope_parameters_and_header_auth(self):
        expected = {
            "norway": {"country": "no"},
            "business": {"language": "en", "category": "business"},
            "global": {"language": "en"},
        }
        for scope, extra in expected.items():
            with patch.object(nb_config.SETTINGS, "news_api_key", "SECRET"), \
                 patch("requests.get", return_value=_resp(payload={"articles": [{"title": "x"}]})) as get:
                items = nf._fetch_newsapi_items(scope, max_items=7)
            params = get.call_args.kwargs["params"]
            self.assertEqual(params, {"pageSize": 7, **extra}, scope)
            self.assertEqual(get.call_args.kwargs["headers"], {"X-Api-Key": "SECRET"})
            self.assertEqual(len(items), 1)

    def test_newsapi_failure_returns_empty(self):
        with patch.object(nb_config.SETTINGS, "news_api_key", "k"), patch("time.sleep"), \
             patch("requests.get", side_effect=requests.exceptions.ConnectionError("x")), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            self.assertEqual(nf._fetch_newsapi_items("global"), [])
        self.assertIn("newsapi_fetch_failed", "\n".join(logs.output))

    def test_freenews_key_resolution(self):
        with patch.object(nb_config.SETTINGS, "freenews_api_key", " "), \
             patch.object(nb_config.SETTINGS, "freen_ews_api_key", " old "):
            self.assertEqual(nf._active_freenews_key(), "old")
        with patch.object(nb_config.SETTINGS, "freenews_api_key", "new"):
            self.assertEqual(nf._active_freenews_key(), "new")
        with patch.object(nb_config.SETTINGS, "freenews_api_key", ""), \
             patch.object(nb_config.SETTINGS, "freen_ews_api_key", ""):
            self.assertEqual(nf._active_freenews_key(), "")

    def test_freenews_requests_and_failure(self):
        with patch.object(nb_config.SETTINGS, "freenews_api_key", ""), \
             patch.object(nb_config.SETTINGS, "freen_ews_api_key", ""), patch("requests.get") as get:
            self.assertEqual(nf._fetch_freenews_items("global"), [])
        get.assert_not_called()
        expected = {
            "norway": {"country": "no"},
            "business": {"category": "business", "language": "en"},
            "global": {"language": "en"},
        }
        for scope, extra in expected.items():
            with patch.object(nb_config.SETTINGS, "freenews_api_key", "k"), \
                 patch("requests.get", return_value=_resp(payload={"data": [{"title": "x"}]})) as get:
                self.assertEqual(len(nf._fetch_freenews_items(scope, max_items=4)), 1)
            self.assertEqual(get.call_args.kwargs["params"], {"apikey": "k", "limit": 4, **extra}, scope)
        with patch.object(nb_config.SETTINGS, "freenews_api_key", "k"), patch("time.sleep"), \
             patch("requests.get", side_effect=requests.exceptions.ConnectionError("x")):
            self.assertEqual(nf._fetch_freenews_items("global"), [])

    def test_provider_priority_sanitised(self):
        with patch.object(nb_config.SETTINGS, "news_fetch_priority", "FreeNews, bogus,rss ,newsapi"):
            self.assertEqual(nf._news_provider_priority(), ["freenews", "rss", "newsapi"])
        for raw in ("", "bogus", None):
            with patch.object(nb_config.SETTINGS, "news_fetch_priority", raw):
                self.assertEqual(nf._news_provider_priority(), ["rss"])


class TopicWeightTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        with patch.object(nb_config.SETTINGS, "news_topic_weights", ""):
            defaults = nf._resolve_topic_weights()
        self.assertEqual(defaults["markets"], 2.0)
        with patch.object(nb_config.SETTINGS, "news_topic_weights", "sports:1.5, MARKETS:0.1"):
            weights = nf._resolve_topic_weights()
        self.assertEqual((weights["sports"], weights["markets"]), (1.5, 0.1))

    def test_bad_entries_are_skipped_with_warnings(self):
        raw = "nocolon, nonsense:2, sports:abc, tech:3"
        with patch.object(nb_config.SETTINGS, "news_topic_weights", raw), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            weights = nf._resolve_topic_weights()
        text = "\n".join(logs.output)
        self.assertIn("topic_weight_unknown category=nonsense", text)
        self.assertIn("topic_weight_invalid category=sports", text)
        self.assertEqual(weights["tech"], 3.0)
        self.assertEqual(weights["sports"], 0.3)


class LlmSelectionValidationTests(unittest.TestCase):
    def setUp(self):
        self.pool = [_cand(f"Story {i}", f"https://www.bbc.com/{i}") for i in range(6)]
        self.selector = nf.LlmSelector(transport=None)

    def _select(self, payload, limit=5):
        return self.selector._to_selections(payload, self.pool, limit)

    def test_malformed_payloads_give_nothing(self):
        for payload in (None, [], "x", {}, {"selections": "nope"}, {"selections": [1, "a", None]}):
            self.assertEqual(self._select(payload), [], payload)

    def test_invalid_ids_are_dropped(self):
        payload = {"selections": [
            {"id": True}, {"id": "1"}, {"id": -1}, {"id": 99}, {"id": 2}, {"id": 2},
        ]}
        selections = self._select(payload)
        self.assertEqual([s.candidate.title for s in selections], ["Story 2"])

    def test_duplicates_are_validated_and_not_reused(self):
        payload = {"selections": [
            {"id": 0, "duplicate_ids": [1, 1, True, 99, "2", 3], "reason": "x" * 500},
            {"id": 1},
            {"id": 4},
        ]}
        selections = self._select(payload)
        self.assertEqual([s.candidate.title for s in selections], ["Story 0", "Story 4"])
        self.assertEqual([c.title for c in selections[0].duplicates], ["Story 1", "Story 3"])
        self.assertEqual(len(selections[0].reason), 200)

    def test_limit_is_respected(self):
        payload = {"selections": [{"id": i} for i in range(6)]}
        self.assertEqual(len(self._select(payload, limit=2)), 2)

    def test_select_without_a_transport_or_pool_is_empty(self):
        self.assertEqual(self.selector.select(self.pool, "s", 3), [])
        self.assertEqual(nf.LlmSelector(transport=MagicMock()).select([], "s", 3), [])

    def test_select_failure_modes_set_the_ranker_status(self):
        saved = dict(nb_state.RANKER_STATUS)
        self.addCleanup(lambda: nb_state.RANKER_STATUS.update(saved))
        bad_responses = [
            _resp(payload=["not", "a", "dict"]),
            _resp(payload={"no": "message"}),
            _resp(payload={"message": {"content": "not json"}}),
            _resp(payload={"message": {"content": json.dumps({"selections": []})}}),
        ]
        for response in bad_responses:
            selector = nf.LlmSelector(transport=MagicMock(return_value=response))
            self.assertEqual(selector.select(self.pool, "s", 3), [])
            self.assertIsNotNone(nb_state.RANKER_STATUS["error"])

    def test_select_success_records_latency_and_clears_the_error(self):
        saved = dict(nb_state.RANKER_STATUS)
        self.addCleanup(lambda: nb_state.RANKER_STATUS.update(saved))
        nb_state.RANKER_STATUS["error"] = "old"
        body = json.dumps({"selections": [{"id": 0}, {"id": 1}]})
        transport = MagicMock(return_value=_resp(payload={"message": {"content": body}}))
        selections = nf.LlmSelector(transport=transport).select(self.pool, "s", 2)
        self.assertEqual(len(selections), 2)
        self.assertIsNone(nb_state.RANKER_STATUS["error"])
        self.assertIsNotNone(nb_state.RANKER_STATUS["latency_ms"])


class SelectorBuildTests(unittest.TestCase):
    def setUp(self):
        saved = dict(nb_state.RANKER_STATUS)
        self.addCleanup(lambda: nb_state.RANKER_STATUS.update(saved))

    def test_disabled_ranker_uses_the_heuristic(self):
        with patch.object(nb_config.SETTINGS, "news_ranker_enabled", False):
            selector = nf._build_selector("s", datetime.now(timezone.utc))
        self.assertIsInstance(selector, nf.HeuristicSelector)
        self.assertEqual(nb_state.RANKER_STATUS["path"], "heuristic")

    def test_blank_url_uses_the_heuristic(self):
        with patch.object(nb_config.SETTINGS, "news_ranker_enabled", True), \
             patch.object(nb_config.SETTINGS, "news_ranker_url", "  "):
            self.assertIsInstance(nf._build_selector("s", datetime.now(timezone.utc)), nf.HeuristicSelector)

    def test_enabled_ranker_uses_the_llm_with_recent_titles(self):
        with patch.object(nb_config.SETTINGS, "news_ranker_enabled", True), \
             patch.object(nb_config.SETTINGS, "news_ranker_url", "http://ollama:11434"), \
             patch.object(nf.STATE, "recent_titles", return_value=["seen before"]):
            selector = nf._build_selector("s", datetime.now(timezone.utc))
        self.assertIsInstance(selector, nf.LlmSelector)
        self.assertEqual(selector.recent_titles, ["seen before"])
        self.assertEqual(nb_state.RANKER_STATUS["path"], "llm")


class CollectCandidatesTests(unittest.TestCase):
    NOW = datetime.now(timezone.utc)

    def _collect(self, items_by_provider, pool_size=10, priority="rss,newsapi", **patches):
        stack = [
            patch.object(nb_config.SETTINGS, "news_fetch_priority", priority),
            patch.object(nf, "_fetch_rss_from_feeds", return_value=items_by_provider.get("rss", [])),
            patch.object(nf, "_fetch_newsapi_items", return_value=items_by_provider.get("newsapi", [])),
            patch.object(nf, "_fetch_freenews_items", return_value=items_by_provider.get("freenews", [])),
            patch.object(nb_config, "TRUSTED_DOMAINS", ["bbc.com"]),
            patch.object(nb_config, "BLOCKED_DOMAINS", ["spam.com"]),
        ]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        return nf._collect_candidates("global", ["feed"], pool_size)

    def test_filters_dedups_and_keeps_undated_items(self):
        old = self.NOW - timedelta(hours=100)
        fresh = self.NOW - timedelta(hours=1)
        items = {"rss": [
            ("Good", "https://www.bbc.com/1", fresh),
            ("Good", "https://www.bbc.com/1", fresh),            # exact duplicate
            ("Stale", "https://www.bbc.com/2", old),             # too old
            ("Undated", "https://www.bbc.com/3", None),          # kept: no date to judge
            ("Blocked", "https://spam.com/4", fresh),
            ("Untrusted", "https://random.org/5", fresh),
            ("", "https://www.bbc.com/6", fresh),                # blank title
        ]}
        titles = [c.title for c in self._collect(items)]
        self.assertEqual(titles, ["Good", "Undated"])

    def test_every_listed_provider_is_pooled_and_unknown_ones_are_skipped(self):
        items = {"rss": [("From RSS", "https://www.bbc.com/1", None)],
                 "newsapi": [("From API", "https://www.bbc.com/2", None)]}
        titles = [c.title for c in self._collect(items, priority="rss,newsapi,freenews")]
        self.assertEqual(titles, ["From RSS", "From API"])
        with patch.object(nf, "_news_provider_priority", return_value=["mystery", "rss"]), \
             patch.object(nf, "_fetch_rss_from_feeds", return_value=items["rss"]):
            self.assertEqual(len(nf._collect_candidates("global", [], 5)), 1)

    def test_a_failing_provider_does_not_stop_the_others(self):
        with patch.object(nb_config.SETTINGS, "news_fetch_priority", "newsapi,rss"), \
             patch.object(nf, "_fetch_newsapi_items", side_effect=RuntimeError("down")), \
             patch.object(nf, "_fetch_rss_from_feeds", return_value=[("Ok", "https://www.bbc.com/1", None)]), \
             patch.object(nb_config, "TRUSTED_DOMAINS", ["bbc.com"]), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            candidates = nf._collect_candidates("global", ["f"], 5)
        self.assertEqual([c.title for c in candidates], ["Ok"])
        self.assertIn("provider_failed provider=newsapi", "\n".join(logs.output))

    def test_pool_size_caps_the_result(self):
        items = {"rss": [(f"Story {i}", f"https://www.bbc.com/{i}", None) for i in range(10)]}
        self.assertEqual(len(self._collect(items, pool_size=3)), 3)


class BuildNewsSectionTests(unittest.TestCase):
    def setUp(self):
        saved = dict(nb_state.RANKER_STATUS)
        self.addCleanup(lambda: nb_state.RANKER_STATUS.update(saved))
        for p in (
            patch.dict(nb_state.STATE.data, {"sent_headline_keys": {}, "recent_titles": {}}),
            patch.object(nb_config.SETTINGS, "news_candidate_pool_size", 10),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _section(self, candidates, selector=None):
        with patch.object(nf, "_collect_candidates", return_value=candidates), \
             patch.object(nf, "_build_selector", return_value=selector or nf.HeuristicSelector()):
            return nf._build_news_section("global_news", "global", "TITLE:", ["f"], "NOTHING NEW", 3)

    def test_renders_numbered_headlines_with_links(self):
        text, selections = self._section([_cand("Alpha"), _cand("Beta", "https://www.bbc.com/b")])
        self.assertTrue(text.startswith("TITLE:\n1. "))
        self.assertIn('<a href="https://www.bbc.com/b">more</a>', text)
        self.assertEqual(len(selections), 2)

    def test_empty_pool_shows_the_empty_message(self):
        text, selections = self._section([])
        self.assertEqual(text, "TITLE:\nNOTHING NEW\n\n")
        self.assertEqual(selections, [])

    def test_already_sent_headlines_are_suppressed(self):
        seen = _cand("Seen story")
        nb_state.STATE.mark_headline_seen(
            "global_news", nf._headline_keys(seen.title, seen.url), datetime.today().strftime("%Y-%m-%d")
        )
        text, selections = self._section([seen, _cand("Fresh story", "https://www.bbc.com/f")])
        self.assertNotIn("Seen story", text)
        self.assertIn("Fresh story", text)

    def test_llm_failure_falls_back_to_the_heuristic(self):
        failing = nf.LlmSelector(transport=MagicMock(side_effect=RuntimeError("ollama down")))
        text, selections = self._section([_cand("Alpha"), _cand("Beta", "https://www.bbc.com/b")], failing)
        self.assertEqual(len(selections), 2)
        self.assertEqual(nb_state.RANKER_STATUS["path"], "heuristic")


if __name__ == "__main__":
    unittest.main()
