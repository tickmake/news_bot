"""Headline matching for picks: name cleaning, conservative matching, ageing, caching, wiring."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from newsbot import catalysts as cat
from newsbot import config as nb_config
from newsbot import indicators as nb_indicators
from newsbot import news_feeds as nf
from newsbot import risk

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _headline(title, hours_ago=2.0, url="https://www.cnbc.com/x", published=True):
    when = NOW - timedelta(hours=hours_ago) if published else None
    return nf._make_candidate(title, url, when, "rss")


def _m(symbol="AAPL"):
    return nb_indicators.TradeMetrics(
        symbol=symbol, session="2026-10-02", last_close=100.0, day_change_pct=1.0,
        week_momentum_pct=3.0, volume_ratio=1.5, drawdown_pct=2.0, atr_pct=2.0, above_ema20=True,
    )


class CompanyKeyTests(unittest.TestCase):
    def test_labels_are_cleaned_to_a_matching_name(self):
        cases = {
            "Apple Inc. [USA Stock]": "Apple",
            "NVIDIA Corporation": "NVIDIA",
            "Anheuser-Busch InBev SA/NV": "Anheuser-Busch InBev",
            "Equinor ASA [Norway Stock]": "Equinor",
            "L'Oreal": "L'Oreal",
            "Siemens [EU Stock]": "Siemens",
            "Amazon.com, Inc.": "Amazon",
            "JPMorgan Chase & Co.": "JPMorgan Chase",
        }
        for label, expected in cases.items():
            self.assertEqual(cat.company_key(label), expected, label)

    def test_unsafe_short_or_ticker_like_names_are_skipped(self):
        for label in ("Target Corp", "Block, Inc.", "Visa Inc.", "AAPL", "MSFT", "Ab", "", None,
                      "[Tracked]", "Global Group Holdings"):
            self.assertIsNone(cat.company_key(label), label)


class NameVariantTests(unittest.TestCase):
    def test_first_word_is_added_when_it_is_safe_on_its_own(self):
        self.assertEqual(cat.name_variants("Meta Platforms, Inc."), ["Meta Platforms", "Meta"])
        self.assertEqual(cat.name_variants("JPMorgan Chase & Co."), ["JPMorgan Chase", "JPMorgan"])
        self.assertEqual(cat.name_variants("Apple Inc."), ["Apple"])

    def test_an_unsafe_first_word_is_not_used(self):
        self.assertEqual(cat.name_variants("Bank of America Corporation"), ["Bank of America"])
        self.assertEqual(cat.name_variants("General Electric Company"), ["General Electric"])
        self.assertEqual(cat.name_variants("Target Corp"), [])
        self.assertEqual(cat.name_variants(""), [])


class MentionTests(unittest.TestCase):
    def test_company_name_as_a_whole_word(self):
        self.assertTrue(cat.mentions("Apple unveils new chip", "AAPL", "Apple"))
        self.assertTrue(cat.mentions("Why investors love APPLE today", "AAPL", "Apple"))
        self.assertFalse(cat.mentions("Pineapple prices surge", "AAPL", "Apple"))
        self.assertFalse(cat.mentions("Applebee's closes stores", "AAPL", "Apple"))

    def test_tickers_only_in_news_notation(self):
        self.assertTrue(cat.mentions("Chipmaker (NVDA) beats estimates", "NVDA", None))
        self.assertTrue(cat.mentions("Buy $NVDA before earnings", "NVDA", None))
        self.assertFalse(cat.mentions("NOW is the time to buy", "NOW", None))
        self.assertFalse(cat.mentions("Nvdax index rises", "NVDA", None))

    def test_exchange_suffix_is_dropped_from_the_ticker(self):
        self.assertTrue(cat.mentions("Equinor (EQNR) raises dividend", "EQNR.OL", None))

    def test_no_key_and_no_notation_matches_nothing(self):
        self.assertFalse(cat.mentions("Apple unveils new chip", "AAPL", None))
        self.assertFalse(cat.mentions("Apple unveils new chip", "AAPL", []))

    def test_any_variant_can_match(self):
        self.assertTrue(cat.mentions("Meta shares jump", "META", ["Meta Platforms", "Meta"]))

    def test_regex_metacharacters_in_names_are_literal(self):
        self.assertTrue(cat.mentions("AT&T expands fibre", "T", "AT&T"))
        self.assertFalse(cat.mentions("ATxT expands fibre", "T", "AT&T"))


class HeadlineTextTests(unittest.TestCase):
    def test_text_with_and_without_age_and_escaping(self):
        self.assertEqual(
            cat.Headline("Apple <b>soars</b>", "cnbc.com", 5.4).text,
            "📰 “Apple &lt;b&gt;soars&lt;/b&gt;” (cnbc.com, 5h ago)",
        )
        self.assertEqual(cat.Headline("X", "", None).text, "📰 “X” (news)")


class FindTests(unittest.TestCase):
    def setUp(self):
        cat.reset_cache()
        self.addCleanup(cat.reset_cache)
        for p in (
            patch.object(nb_config.SETTINGS, "catalysts_enabled", True),
            patch.object(nb_config.SETTINGS, "catalysts_max_per_pick", 2),
            patch.object(nb_config.SETTINGS, "catalysts_max_age_hours", 36),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _find(self, pool, symbols=("AAPL",), labels=None):
        with patch.object(nf, "_collect_candidates", return_value=pool):
            return cat.find(list(symbols), labels or {"AAPL": "Apple Inc."}, NOW)

    def test_newest_matching_headlines_first_and_limited(self):
        pool = [
            _headline("Apple old news", 20), _headline("Apple newest", 1), _headline("Apple middle", 5),
            _headline("Unrelated markets wrap", 0.5),
        ]
        found = self._find(pool)
        self.assertEqual([h.title for h in found["AAPL"]], ["Apple newest", "Apple middle"])
        self.assertAlmostEqual(found["AAPL"][0].age_hours, 1.0)

    def test_stale_headlines_are_ignored_and_undated_ones_are_kept_last(self):
        pool = [_headline("Apple stale", 48), _headline("Apple undated", published=False), _headline("Apple fresh", 2)]
        found = self._find(pool)
        self.assertEqual([h.title for h in found["AAPL"]], ["Apple fresh", "Apple undated"])

    def test_duplicates_are_collapsed(self):
        pool = [_headline("Apple beats", 1), _headline("Apple beats", 2, url="https://other.com/a")]
        self.assertEqual(len(self._find(pool)["AAPL"]), 1)

    def test_picks_without_matches_are_absent(self):
        self.assertEqual(self._find([_headline("Oil rises")]), {})

    def test_short_forms_of_multiword_names_match(self):
        found = self._find([_headline("Meta shares jump on ad outlook", 1)], ("META",), {"META": "Meta Platforms, Inc."})
        self.assertEqual([h.title for h in found["META"]], ["Meta shares jump on ad outlook"])

    def test_two_picks_get_their_own_headlines(self):
        pool = [_headline("Apple up", 1), _headline("Equinor (EQNR) raises dividend", 2)]
        found = self._find(pool, ("AAPL", "EQNR.OL"), {"AAPL": "Apple Inc.", "EQNR.OL": "Equinor ASA"})
        self.assertEqual([h.title for h in found["AAPL"]], ["Apple up"])
        self.assertEqual([h.title for h in found["EQNR.OL"]], ["Equinor (EQNR) raises dividend"])

    def test_disabled_empty_and_zero_limit(self):
        with patch.object(nb_config.SETTINGS, "catalysts_enabled", False), \
             patch.object(nf, "_collect_candidates") as collect:
            self.assertEqual(cat.find(["AAPL"], {}), {})
        collect.assert_not_called()
        self.assertEqual(cat.find([], {}), {})
        with patch.object(nb_config.SETTINGS, "catalysts_max_per_pick", 0):
            self.assertEqual(self._find([_headline("Apple up")]), {})

    def test_a_failing_feed_fetch_gives_nothing_and_does_not_raise(self):
        with patch.object(nf, "_collect_candidates", side_effect=RuntimeError("feeds down")), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            self.assertEqual(cat.find(["AAPL"], {"AAPL": "Apple Inc."}, NOW), {})
        self.assertIn("catalysts_pool_failed", "\n".join(logs.output))

    def test_the_feed_pool_is_cached_between_calls(self):
        with patch.object(nf, "_collect_candidates", return_value=[_headline("Apple up")]) as collect:
            cat.find(["AAPL"], {"AAPL": "Apple Inc."}, NOW)
            cat.find(["AAPL"], {"AAPL": "Apple Inc."}, NOW)
            self.assertEqual(collect.call_count, 1)
            with patch.object(nb_config.SETTINGS, "catalysts_pool_ttl_seconds", -1):
                cat.find(["AAPL"], {"AAPL": "Apple Inc."}, NOW)
            self.assertEqual(collect.call_count, 2)


class WiringTests(unittest.TestCase):
    def setUp(self):
        for p in (
            patch.object(nb_config.SETTINGS, "risk_analytics_enabled", True),
            patch.object(nb_config.SETTINGS, "relative_strength_enabled", False),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_build_context_attaches_headlines_and_renders_them(self):
        headline = cat.Headline("Apple beats estimates", "cnbc.com", 3.0)
        with patch("newsbot.market_data._fetch_event_profile", return_value={}), \
             patch.object(risk, "correlated_pairs", return_value=[]), \
             patch.object(cat, "find", return_value={"AAPL": [headline]}) as find:
            context = risk.build_context([_m("AAPL")], {"AAPL": "Apple Inc."})
        find.assert_called_once_with(["AAPL"], {"AAPL": "Apple Inc."})
        lines = risk.candidate_lines("AAPL", context)
        self.assertTrue(any("📰 “Apple beats estimates” (cnbc.com, 3h ago)" in line for line in lines))

    def test_default_labels_are_optional(self):
        with patch("newsbot.market_data._fetch_event_profile", return_value={}), \
             patch.object(risk, "correlated_pairs", return_value=[]), \
             patch.object(cat, "find", return_value={}) as find:
            risk.build_context([_m("AAPL")])
        find.assert_called_once_with(["AAPL"], None)


if __name__ == "__main__":
    unittest.main()
