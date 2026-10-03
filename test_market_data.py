"""market_data: Finnhub quotes, Yahoo screeners, universe discovery, history caches."""

import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from newsbot import config as nb_config
from newsbot import market_data as md
from newsbot import state as nb_state


def _response(payload=None, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _http_error(status):
    response = MagicMock()
    response.status_code = status
    error = requests.exceptions.HTTPError(f"{status} error", response=response)
    response.raise_for_status.side_effect = error
    return response


def _quote(symbol, change, quote_type="EQUITY", **extra):
    return {
        "symbol": symbol, "regularMarketChangePercent": change,
        "regularMarketPrice": 10.0, "quoteType": quote_type,
        "shortName": f"{symbol} Inc", **extra,
    }


class _Isolated(unittest.TestCase):
    """Process-wide caches and backoff timers must not leak between tests."""

    def setUp(self):
        for cache in (nb_state.LIVE_QUOTES_CACHE, nb_state.FINNHUB_QUOTE_CACHE,
                      nb_state.HISTORY_CACHE, nb_state.TICKER_INFO_CACHE):
            cache.clear()
        md.FINNHUB_BACKOFF_UNTIL = 0.0
        md.YAHOO_SCREENER_BACKOFF_UNTIL = 0.0
        sleeper = patch("time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.addCleanup(setattr, md, "FINNHUB_BACKOFF_UNTIL", 0.0)
        self.addCleanup(setattr, md, "YAHOO_SCREENER_BACKOFF_UNTIL", 0.0)


class HelperTests(unittest.TestCase):
    def test_as_float_accepts_numbers_dicts_and_numeric_strings(self):
        self.assertEqual(md._as_float(3), 3.0)
        self.assertEqual(md._as_float({"raw": 2.5, "fmt": "2.5"}), 2.5)
        self.assertEqual(md._as_float("4.25"), 4.25)

    def test_as_float_rejects_everything_else(self):
        for value in (None, "abc", {"raw": "x"}, {}, [1]):
            self.assertIsNone(md._as_float(value))

    def test_active_finnhub_key_prefers_first_nonblank(self):
        with patch.object(nb_config.SETTINGS, "finnhub_api_key", "  "), \
             patch.object(nb_config.SETTINGS, "finhub_api_key", " alt "):
            self.assertEqual(md._active_finnhub_key(), "alt")
        with patch.object(nb_config.SETTINGS, "finnhub_api_key", ""), \
             patch.object(nb_config.SETTINGS, "finhub_api_key", ""):
            self.assertEqual(md._active_finnhub_key(), "")

    def test_quote_name_falls_back_in_order(self):
        self.assertEqual(md._quote_name({"shortName": " Acme ", "symbol": "A"}), "Acme")
        self.assertEqual(md._quote_name({"longName": "Long", "symbol": "A"}), "Long")
        self.assertEqual(md._quote_name({"symbol": "A"}), "A")
        self.assertEqual(md._quote_name({}), "Unknown")

    def test_quote_type_predicates(self):
        self.assertTrue(md._is_stock_quote({"quoteType": "equity"}))
        self.assertFalse(md._is_stock_quote({"quoteType": "ETF"}))
        self.assertTrue(md._is_fund_quote({"quoteType": "ETF"}))
        self.assertTrue(md._is_fund_quote({"quoteType": "MUTUALFUND"}))
        self.assertFalse(md._is_fund_quote({}))

    def test_round_robin_interleaves_and_respects_limit(self):
        merged = md._round_robin_merge([[{"s": 1}, {"s": 2}, {"s": 3}], [{"s": 10}], [{"s": 20}, {"s": 21}]], 5)
        self.assertEqual([item["s"] for item in merged], [1, 10, 20, 2, 21])
        self.assertEqual(len(md._round_robin_merge([[{"s": 1}, {"s": 2}]], 1)), 1)
        self.assertEqual(md._round_robin_merge([], 5), [])

    def test_rank_quotes_by_change_skips_unscored(self):
        quotes = [_quote("A", 1.0), _quote("B", 5.0), {"symbol": "C"}, _quote("D", 3.0)]
        self.assertEqual([q["symbol"] for q in md._rank_quotes_by_change(quotes, 2)], ["B", "D"])


class FinnhubQuoteTests(_Isolated):
    def setUp(self):
        super().setUp()
        key = patch.object(nb_config.SETTINGS, "finnhub_api_key", "SECRETKEY")
        key.start()
        self.addCleanup(key.stop)

    def test_no_key_means_no_request(self):
        with patch.object(nb_config.SETTINGS, "finnhub_api_key", ""), \
             patch.object(nb_config.SETTINGS, "finhub_api_key", ""), \
             patch("requests.get") as get:
            self.assertIsNone(md._fetch_finnhub_quote("AAPL"))
        get.assert_not_called()

    def test_success_is_normalised_cached_and_key_stays_out_of_the_url(self):
        with patch("requests.get", return_value=_response({"c": 190.5, "dp": 1.25, "pc": 188.0})) as get:
            first = md._fetch_finnhub_quote("AAPL")
            second = md._fetch_finnhub_quote("AAPL")
        self.assertEqual(first, {"price": 190.5, "pct_change": 1.25})
        self.assertEqual(second, first)
        self.assertEqual(get.call_count, 1)
        kwargs = get.call_args.kwargs
        self.assertNotIn("token", kwargs["params"])
        self.assertEqual(kwargs["headers"], {"X-Finnhub-Token": "SECRETKEY"})

    def test_pct_change_derived_from_previous_close(self):
        with patch("requests.get", return_value=_response({"c": 110.0, "dp": None, "pc": 100.0})):
            self.assertAlmostEqual(md._fetch_finnhub_quote("X")["pct_change"], 10.0)

    def test_unusable_payloads_return_none(self):
        for payload in ({"c": 0, "dp": 1}, {"c": 5, "dp": None, "pc": 0}, {"c": None}):
            md.FINNHUB_QUOTE_CACHE.clear()
            with patch("requests.get", return_value=_response(payload)):
                self.assertIsNone(md._fetch_finnhub_quote("X"), payload)

    def test_error_field_is_treated_as_failure(self):
        with patch("requests.get", return_value=_response({"error": "bad symbol"})):
            self.assertIsNone(md._fetch_finnhub_quote("X"))

    def test_auth_and_rate_limit_errors_start_a_backoff_that_skips_later_calls(self):
        for status in (401, 403, 429):
            md.FINNHUB_BACKOFF_UNTIL = 0.0
            md.FINNHUB_QUOTE_CACHE.clear()
            with patch("requests.get", return_value=_http_error(status)) as get:
                self.assertIsNone(md._fetch_finnhub_quote("AAPL"))
                self.assertGreater(md.FINNHUB_BACKOFF_UNTIL, time.time(), status)
                self.assertIsNone(md._fetch_finnhub_quote("MSFT"))
            self.assertEqual(get.call_count, 1, status)

    def test_other_http_errors_do_not_start_a_backoff(self):
        with patch("requests.get", return_value=_http_error(404)):
            self.assertIsNone(md._fetch_finnhub_quote("AAPL"))
        self.assertEqual(md.FINNHUB_BACKOFF_UNTIL, 0.0)

    def test_ssl_failure_starts_a_backoff(self):
        with patch("requests.get", side_effect=requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")):
            self.assertIsNone(md._fetch_finnhub_quote("AAPL"))
        self.assertGreater(md.FINNHUB_BACKOFF_UNTIL, time.time())

    def test_failure_logs_without_the_key(self):
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("boom")), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            md._fetch_finnhub_quote("AAPL")
        self.assertNotIn("SECRETKEY", "\n".join(logs.output))


class RefreshQuotesTests(_Isolated):
    def test_without_a_key_quotes_are_copied_untouched(self):
        quotes = [_quote("A", 1.0)]
        with patch.object(nb_config.SETTINGS, "finnhub_api_key", ""), \
             patch.object(nb_config.SETTINGS, "finhub_api_key", ""):
            result = md._refresh_quotes_with_finnhub(quotes)
        self.assertEqual(result, quotes)
        self.assertIsNot(result[0], quotes[0])

    def test_only_listed_types_within_the_refresh_cap_are_updated(self):
        quotes = [_quote("A", 1.0), _quote("B", 2.0, quote_type="MUTUALFUND"), _quote("C", 3.0)]
        fresh = {"price": 99.0, "pct_change": 9.0}
        with patch.object(nb_config.SETTINGS, "finnhub_api_key", "k"), \
             patch.object(nb_config.SETTINGS, "finnhub_max_symbols_per_refresh", 2), \
             patch.object(md, "_fetch_finnhub_quote", return_value=fresh) as fetch:
            result = md._refresh_quotes_with_finnhub(quotes)
        fetch.assert_called_once_with("A")
        self.assertEqual(result[0]["regularMarketPrice"], 99.0)
        self.assertEqual(result[0]["marketDataProvider"], "finnhub")
        self.assertEqual(result[1]["regularMarketPrice"], 10.0)
        self.assertEqual(result[2]["regularMarketPrice"], 10.0)

    def test_failed_lookup_keeps_the_original_quote(self):
        with patch.object(nb_config.SETTINGS, "finnhub_api_key", "k"), \
             patch.object(md, "_fetch_finnhub_quote", return_value=None):
            result = md._refresh_quotes_with_finnhub([_quote("A", 1.0)])
        self.assertNotIn("marketDataProvider", result[0])


class ScreenerFetchTests(_Isolated):
    PAYLOAD = {"finance": {"result": [{"quotes": [{"symbol": "AAA"}, "junk", {"symbol": "BBB"}]}]}}

    def test_returns_only_dict_quotes(self):
        with patch("requests.get", return_value=_response(self.PAYLOAD)):
            quotes = md._fetch_predefined_screener_quotes("day_gainers", 10)
        self.assertEqual([q["symbol"] for q in quotes], ["AAA", "BBB"])

    def test_empty_result_returns_empty(self):
        with patch("requests.get", return_value=_response({"finance": {"result": []}})):
            self.assertEqual(md._fetch_predefined_screener_quotes("x", 10), [])

    def test_second_endpoint_is_tried_when_the_first_fails(self):
        responses = [requests.exceptions.ConnectionError("down"), _response(self.PAYLOAD)]
        with patch("requests.get", side_effect=responses) as get:
            quotes = md._fetch_predefined_screener_quotes("x", 10)
        self.assertEqual(len(quotes), 2)
        self.assertEqual(get.call_count, 2)
        self.assertNotEqual(get.call_args_list[0].args[0], get.call_args_list[1].args[0])

    def test_all_endpoints_failing_gives_empty_and_logs(self):
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("down")), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            self.assertEqual(md._fetch_predefined_screener_quotes("x", 10), [])
        self.assertIn("screener_fetch_failed", "\n".join(logs.output))
        self.assertEqual(md.YAHOO_SCREENER_BACKOFF_UNTIL, 0.0)

    def test_ssl_failure_starts_a_backoff_that_skips_later_calls(self):
        with patch("requests.get", side_effect=requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")) as get:
            md._fetch_predefined_screener_quotes("x", 10)
            self.assertGreater(md.YAHOO_SCREENER_BACKOFF_UNTIL, time.time())
            calls = get.call_count
            self.assertEqual(md._fetch_predefined_screener_quotes("y", 10), [])
        self.assertEqual(get.call_count, calls)


class LiveQuoteCollectionTests(_Isolated):
    def test_collect_dedupes_by_symbol_and_caches(self):
        batches = {"a": [_quote("X", 1.0), _quote("Y", 2.0)], "b": [_quote("Y", 9.0), {"symbol": ""}]}
        with patch.object(md, "_fetch_predefined_screener_quotes",
                          side_effect=lambda scr, count: batches[scr]) as fetch:
            first = md._collect_live_quotes(["a", "b"], 10)
            second = md._collect_live_quotes(["a", "b"], 10)
        self.assertEqual([q["symbol"] for q in first], ["X", "Y"])
        self.assertEqual(first[1]["regularMarketChangePercent"], 2.0)  # first source wins
        self.assertEqual(second, first)
        self.assertEqual(fetch.call_count, 2)

    def test_stock_quotes_by_screener_keep_origin_order_and_skip_repeats(self):
        batches = {
            "a": [_quote("X", 1.0), _quote("F", 1.0, quote_type="ETF"), _quote("Y", 2.0)],
            "b": [_quote("Y", 3.0), _quote("Z", 4.0), {"quoteType": "EQUITY"}],
        }
        with patch.object(md, "_fetch_predefined_screener_quotes",
                          side_effect=lambda scr, count: batches[scr]):
            result = md._collect_stock_quotes_by_screener(["a", "b"], 10)
        self.assertEqual([q["symbol"] for q in result["a"]], ["X", "Y"])
        self.assertEqual([q["symbol"] for q in result["b"]], ["Z"])

    def test_build_live_universe_mixes_stocks_and_funds_without_duplicates(self):
        stocks = {"a": [_quote("S1", 1.0), _quote("S2", 2.0)], "b": [_quote("S3", 3.0)]}
        funds = [_quote("F1", 5.0, quote_type="ETF"), _quote("S1", 1.0, quote_type="ETF"),
                 _quote("E1", 1.0)]
        with patch.object(md, "STOCK_SCREENERS", ["a", "b"]), \
             patch.object(md, "_collect_stock_quotes_by_screener", return_value=stocks), \
             patch.object(md, "_collect_live_quotes", return_value=funds):
            universe = md._build_live_universe(limit=3)
        self.assertEqual(sorted(universe.values()), ["F1", "S1", "S2", "S3"])
        self.assertEqual(universe["S1 Inc"], "S1")


class FormattingTests(_Isolated):
    def test_rows_sorted_by_change_truncated_and_limited(self):
        quotes = [_quote("LOW", -1.0), _quote("HIGH", 7.5, shortName="A very long company name indeed"),
                  _quote("MID", 2.0), {"symbol": "NOPCT"}]
        with patch.object(md, "_refresh_quotes_with_finnhub", side_effect=lambda q: q):
            rows = md._format_live_rows(quotes, top_n=2, max_name_len=12)
        self.assertEqual([row[1] for row in rows], ["HIGH", "MID"])
        self.assertLessEqual(len(rows[0][0]), 12)
        self.assertEqual(rows[0][2:], ["10.00", "+7.50%"])

    def test_missing_price_renders_na(self):
        quote = _quote("X", 1.0)
        quote["regularMarketPrice"] = None
        with patch.object(md, "_refresh_quotes_with_finnhub", side_effect=lambda q: q):
            self.assertEqual(md._format_live_rows([quote], 5, 20)[0][2], "N/A")

    def test_snapshot_renders_both_tables(self):
        quotes = [_quote("STK", 3.0), _quote("ETFX", 2.0, quote_type="ETF")]
        with patch.object(md, "_collect_live_quotes", return_value=quotes), \
             patch.object(md, "_refresh_quotes_with_finnhub", side_effect=lambda q: q):
            text = md._live_market_snapshot()
        self.assertIn("Top stocks by 1D change", text)
        self.assertIn("Top funds/ETFs by 1D change", text)
        self.assertIn("STK", text)
        self.assertIn("ETFX", text)

    def test_snapshot_says_so_when_data_is_unavailable(self):
        with patch.object(md, "_collect_live_quotes", return_value=[]):
            text = md._live_market_snapshot()
        self.assertEqual(text.count("Data temporarily unavailable"), 2)

    def test_business_and_stocks_joins_news_and_snapshot(self):
        with patch.object(md, "_business_news_section", return_value=("BIZNEWS\n\n", ["sel"])), \
             patch.object(md, "_live_market_snapshot", return_value="SNAPSHOT"):
            text, selections = md.get_business_and_stocks()
        self.assertEqual(text, "BIZNEWS\n\nSNAPSHOT")
        self.assertEqual(selections, ["sel"])

    def test_business_news_section_uses_the_business_scope(self):
        with patch.object(md.news_feeds, "_build_news_section", return_value=("t", [])) as build:
            md._business_news_section()
        self.assertEqual(build.call_args.kwargs["section_key"], "business_news")
        self.assertEqual(build.call_args.kwargs["scope"], "business")


class HistoryAndFundamentalsTests(_Isolated):
    def test_history_is_cached_per_symbol_period_interval(self):
        ticker = MagicMock()
        ticker.history.return_value = "FRAME"
        with patch("yfinance.Ticker", return_value=ticker) as ctor:
            self.assertEqual(md._fetch_ticker_history("AAPL"), "FRAME")
            self.assertEqual(md._fetch_ticker_history("AAPL"), "FRAME")
            md._fetch_ticker_history("AAPL", period="max")
        self.assertEqual(ctor.call_count, 2)
        ticker.history.assert_called_with(period="max", interval="1d", auto_adjust=True)

    def test_expired_history_is_refetched(self):
        ticker = MagicMock()
        ticker.history.return_value = "FRAME"
        with patch("yfinance.Ticker", return_value=ticker) as ctor:
            md._fetch_ticker_history("AAPL")
            key = next(iter(nb_state.HISTORY_CACHE))
            stamp, value = nb_state.HISTORY_CACHE[key]
            nb_state.HISTORY_CACHE[key] = (stamp - 10_000, value)
            md._fetch_ticker_history("AAPL")
        self.assertEqual(ctor.call_count, 2)

    def test_history_retries_then_raises(self):
        with patch("yfinance.Ticker", side_effect=RuntimeError("yahoo down")) as ctor, \
             self.assertRaises(RuntimeError):
            md._fetch_ticker_history("AAPL")
        self.assertEqual(ctor.call_count, 2)

    def test_fundamentals_cached(self):
        ticker = MagicMock()
        ticker.info = {"longName": "Apple"}
        with patch("yfinance.Ticker", return_value=ticker) as ctor:
            self.assertEqual(md._fetch_ticker_fundamentals("AAPL"), {"longName": "Apple"})
            md._fetch_ticker_fundamentals("AAPL")
        self.assertEqual(ctor.call_count, 1)

    def test_fundamentals_degrade_to_empty_on_bad_payload_or_error(self):
        ticker = MagicMock()
        ticker.info = "not a dict"
        with patch("yfinance.Ticker", return_value=ticker):
            self.assertEqual(md._fetch_ticker_fundamentals("A"), {})
        with patch("yfinance.Ticker", side_effect=RuntimeError("x")):
            self.assertEqual(md._fetch_ticker_fundamentals("B"), {})


if __name__ == "__main__":
    unittest.main()
