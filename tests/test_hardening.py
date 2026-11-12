"""Log redaction, HTTP retry policy, HTML-safe message splitting, briefing catch-up."""

import logging
import re
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import requests

from newsbot import (
    briefing as nb_briefing,
    config as nb_config,
    logsetup as nb_logsetup,
    messaging as nb_messaging,
    state as nb_state,
    util as nb_util,
)


class RedactionTests(unittest.TestCase):
    def test_telegram_token_in_url_is_masked(self):
        text = "401 Client Error for url: https://api.telegram.org/bot123456789:AAE-secret_Token1234567890abcdef/sendMessage"
        out = nb_logsetup.redact_secrets(text)
        self.assertNotIn("AAE-secret", out)
        self.assertIn("/bot***/sendMessage", out)

    def test_query_string_keys_are_masked(self):
        out = nb_logsetup.redact_secrets("GET /quote?symbol=AAPL&token=abc123&apiKey=zzz999 failed")
        self.assertNotIn("abc123", out)
        self.assertNotIn("zzz999", out)
        self.assertIn("symbol=AAPL", out)

    def test_slack_webhook_and_bearer_are_masked(self):
        out = nb_logsetup.redact_secrets(
            "https://hooks.slack.com/services/T000/B000/XXXXXXXX and Bearer xoxb-1234-abcd"
        )
        self.assertNotIn("XXXXXXXX", out)
        self.assertNotIn("xoxb-1234", out)

    def test_ordinary_text_is_untouched(self):
        text = "news_select section=global_news pool=12 selected=5 key_count=3"
        self.assertEqual(nb_logsetup.redact_secrets(text), text)

    def test_formatter_masks_message_and_traceback(self):
        formatter = nb_logsetup.BandedFormatter(colour=False)
        try:
            raise RuntimeError("boom https://x.test/a?token=SEKRET")
        except RuntimeError:
            import sys
            record = logging.LogRecord(
                "news_bot.app", logging.ERROR, __file__, 1,
                "failed url=https://x.test/a?token=SEKRET", None, sys.exc_info(),
            )
        self.assertNotIn("SEKRET", formatter.format(record))


class RetryPolicyTests(unittest.TestCase):
    @staticmethod
    def _response(status, retry_after=None):
        response = MagicMock()
        response.status_code = status
        response.headers = {"Retry-After": retry_after} if retry_after else {}
        return response

    @patch("time.sleep")
    def test_retryable_status_is_retried_then_succeeds(self, mock_sleep):
        action = MagicMock(side_effect=[self._response(503), self._response(200)])
        result = nb_util._with_retry(action, "t")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(action.call_count, 2)

    @patch("time.sleep")
    def test_exhausted_retryable_status_is_returned_not_raised(self, mock_sleep):
        action = MagicMock(return_value=self._response(500))
        result = nb_util._with_retry(action, "t", retries=3)
        self.assertEqual(result.status_code, 500)
        self.assertEqual(action.call_count, 3)

    @patch("time.sleep")
    def test_client_error_is_not_retried(self, mock_sleep):
        action = MagicMock(return_value=self._response(400))
        nb_util._with_retry(action, "t")
        self.assertEqual(action.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_retry_after_is_honoured_and_capped(self, mock_sleep):
        action = MagicMock(side_effect=[self._response(429, "7"), self._response(429, "9999"), self._response(200)])
        nb_util._with_retry(action, "t")
        self.assertEqual([call.args[0] for call in mock_sleep.call_args_list], [7.0, 30.0])

    @patch("time.sleep")
    def test_exceptions_still_retry(self, mock_sleep):
        action = MagicMock(side_effect=[requests.exceptions.ConnectionError("x"), self._response(200)])
        self.assertEqual(nb_util._with_retry(action, "t").status_code, 200)


class SplitterTests(unittest.TestCase):
    TAG = re.compile(r"<(/?)(b|i|pre|a)\b[^>]*>")

    def _assert_balanced(self, chunk):
        stack = []
        for match in self.TAG.finditer(chunk):
            if match.group(1):
                self.assertTrue(stack and stack[-1] == match.group(2), chunk[:80])
                stack.pop()
            else:
                stack.append(match.group(2))
        self.assertEqual(stack, [])

    def test_pre_table_split_is_balanced_and_bounded(self):
        table = "<pre>" + "\n".join(f"row {i} " + "x" * 60 for i in range(120)) + "</pre>"
        chunks = nb_messaging._split_message_html("<b>Title</b>\n\n" + table, 3900)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk) + len("<b>Part 10/10</b>\n"), 3900)
            self._assert_balanced(chunk)

    def test_overlong_single_line_is_split(self):
        chunks = nb_messaging._split_message_html("word " * 3000, 3900)
        self.assertTrue(all(len(c) <= 3900 for c in chunks))
        self.assertEqual("".join(chunks).replace(" ", ""), ("word " * 3000).replace(" ", ""))

    def test_split_never_cuts_inside_a_tag(self):
        message = ("<a href=\"https://example.com/" + "p" * 30 + "\">link</a> ") * 400
        for chunk in nb_messaging._split_message_html(message, 1000):
            self._assert_balanced(chunk)

    def test_short_message_unchanged(self):
        self.assertEqual(nb_messaging._split_message_html("<b>hi</b>", 3900), ["<b>hi</b>"])

    @patch("time.sleep")
    @patch("requests.post")
    def test_parse_error_falls_back_to_plain_text(self, mock_post, mock_sleep):
        rejected = MagicMock(status_code=400, text="Bad Request: can't parse entities: ...")
        accepted = MagicMock(status_code=200)
        accepted.raise_for_status.return_value = None
        mock_post.side_effect = [rejected, accepted]
        with patch.object(nb_config.SETTINGS, "telegram_token", "t"):
            nb_messaging._telegram_post("1", "<b>5 &lt; 6</b>")
        second = mock_post.call_args_list[1].kwargs["json"]
        self.assertNotIn("parse_mode", second)
        self.assertEqual(second["text"], "5 < 6")


class CatchupTests(unittest.TestCase):
    def _state(self, recorded):
        data = {} if recorded is None else {"last_scheduled_slot": recorded}
        return patch.dict(nb_state.STATE.data, data, clear=False)

    def setUp(self):
        nb_state.STATE.data.pop("last_scheduled_slot", None)

    def test_missed_morning_slot_is_caught_up(self):
        with self._state("2026-10-02@19"):
            self.assertEqual(
                nb_briefing.missed_briefing_slot(datetime(2026, 10, 3, 7, 40)), "2026-10-03@07"
            )

    def test_completed_slot_is_not_repeated(self):
        with self._state("2026-10-03@07"):
            self.assertIsNone(nb_briefing.missed_briefing_slot(datetime(2026, 10, 3, 8, 0)))

    def test_no_record_means_no_catchup(self):
        self.assertIsNone(nb_briefing.missed_briefing_slot(datetime(2026, 10, 3, 8, 0)))

    def test_stale_slot_outside_window_is_skipped(self):
        with self._state("2026-10-02@19"):
            self.assertIsNone(nb_briefing.missed_briefing_slot(datetime(2026, 10, 3, 12, 0)))

    def test_before_first_slot_nothing_due(self):
        with self._state("2026-10-02@19"):
            self.assertIsNone(nb_briefing.missed_briefing_slot(datetime(2026, 10, 3, 6, 0)))

    def test_successful_scheduled_run_records_slot(self):
        fake = nb_briefing.Briefing(message="m")
        with patch.object(nb_briefing, "compose_briefing", return_value=fake), \
             patch.object(nb_briefing.messaging, "_broadcast_message", return_value=True), \
             patch.object(nb_briefing.STATE, "save"):
            nb_briefing.job_daily_briefing(slot="2026-10-03@07")
        self.assertEqual(nb_state.STATE.data["last_scheduled_slot"], "2026-10-03@07")
        nb_state.STATE.data.pop("last_scheduled_slot", None)


if __name__ == "__main__":
    unittest.main()
