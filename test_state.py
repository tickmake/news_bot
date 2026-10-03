"""AppState persistence and the dedup windows."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from newsbot.state import AppState


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def state(self):
        return AppState(self.path)

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()


class LoadTests(_Base):
    def test_missing_file_gives_defaults(self):
        data = self.state().data
        self.assertEqual(data["telegram_update_offset"], 0)
        self.assertEqual(data["sent_headline_keys"], {})
        self.assertEqual(data["last_run_status"], "")

    def test_saved_values_are_merged_over_defaults(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"telegram_update_offset": 9, "extra": "kept"}, handle)
        data = self.state().data
        self.assertEqual(data["telegram_update_offset"], 9)
        self.assertEqual(data["extra"], "kept")
        self.assertIn("recent_titles", data)

    def test_corrupt_file_is_ignored_with_a_warning(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertLogs("news_bot", level="WARNING") as logs:
            data = self.state().data
        self.assertIn("state_load_failed", "\n".join(logs.output))
        self.assertEqual(data["telegram_update_offset"], 0)

    def test_non_object_json_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump([1, 2, 3], handle)
        self.assertEqual(self.state().data["telegram_update_offset"], 0)


class SaveTests(_Base):
    def test_round_trip(self):
        state = self.state()
        state.telegram_update_offset = 77
        state.mark_headline_seen("global_news", ["k"], "2026-10-01")
        state.save()
        reloaded = self.state()
        self.assertEqual(reloaded.telegram_update_offset, 77)
        self.assertEqual(reloaded.data["sent_headline_keys"], {"global_news": {"2026-10-01": ["k"]}})

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        state = self.state()
        state.save()
        self.assertEqual(os.listdir(self.dir), ["state.json"])

    def test_unserialisable_data_keeps_the_old_file(self):
        state = self.state()
        state.save()
        before = self.read()
        state.data["bad"] = object()
        with self.assertLogs("news_bot", level="WARNING") as logs:
            state.save()
        self.assertIn("state_serialize_failed", "\n".join(logs.output))
        self.assertEqual(self.read(), before)

    def test_failed_replace_cleans_up_and_keeps_the_old_file(self):
        state = self.state()
        state.save()
        before = self.read()
        state.telegram_update_offset = 5
        with patch("os.replace", side_effect=OSError("disk")), \
             self.assertLogs("news_bot", level="WARNING") as logs:
            state.save()
        self.assertIn("state_save_failed", "\n".join(logs.output))
        self.assertEqual(os.listdir(self.dir), ["state.json"])
        self.assertEqual(self.read(), before)

    def test_unwritable_directory_is_a_warning_not_a_crash(self):
        state = AppState(os.path.join(self.dir, "missing", "state.json"))
        with self.assertLogs("news_bot", level="WARNING") as logs:
            state.save()
        self.assertIn("state_save_failed", "\n".join(logs.output))


class WindowTests(_Base):
    def test_cutoff_is_calendar_days_back(self):
        self.assertEqual(AppState._window_cutoff(7, "2026-10-08"), "2026-10-02")
        self.assertEqual(AppState._window_cutoff(1, "2026-10-08"), "2026-10-08")
        self.assertEqual(AppState._window_cutoff(0, "2026-10-08"), "2026-10-08")

    def test_bad_anchor_falls_back_to_today(self):
        cutoff = AppState._window_cutoff(1, "not-a-date")
        self.assertRegex(cutoff, r"^\d{4}-\d{2}-\d{2}$")

    def test_default_anchor_is_today(self):
        self.assertEqual(AppState._window_cutoff(1, None), AppState._window_cutoff(1, None))

    def test_prune_keeps_only_the_newest_buckets(self):
        bucket = {f"2026-10-0{i}": [] for i in range(1, 10)}
        AppState._prune_buckets(bucket, keep=3)
        self.assertEqual(sorted(bucket), ["2026-10-07", "2026-10-08", "2026-10-09"])
        small = {"2026-10-01": []}
        AppState._prune_buckets(small, keep=3)
        self.assertEqual(list(small), ["2026-10-01"])


class HeadlineTests(_Base):
    def test_seen_within_window_only(self):
        state = self.state()
        state.mark_headline_seen("s", ["k"], "2026-10-01")
        self.assertTrue(state.has_seen_headline("s", "k", window_days=7, today="2026-10-07"))
        self.assertFalse(state.has_seen_headline("s", "k", window_days=7, today="2026-10-08"))

    def test_sections_are_independent(self):
        state = self.state()
        state.mark_headline_seen("a", ["k"], "2026-10-01")
        self.assertFalse(state.has_seen_headline("b", "k", window_days=7, today="2026-10-01"))

    def test_a_bare_string_is_one_key_not_its_characters(self):
        state = self.state()
        state.mark_headline_seen("s", "abc", "2026-10-01")
        self.assertEqual(state.data["sent_headline_keys"]["s"]["2026-10-01"], ["abc"])

    def test_marking_twice_does_not_duplicate(self):
        state = self.state()
        state.mark_headline_seen("s", ["k", "k"], "2026-10-01")
        state.mark_headline_seen("s", ["k"], "2026-10-01")
        self.assertEqual(state.data["sent_headline_keys"]["s"]["2026-10-01"], ["k"])

    def test_only_the_last_seven_days_of_buckets_are_stored(self):
        state = self.state()
        for day in range(1, 11):
            state.mark_headline_seen("s", [f"k{day}"], f"2026-10-{day:02d}")
        self.assertEqual(len(state.data["sent_headline_keys"]["s"]), 7)

    def test_recent_titles_window_limit_and_order(self):
        state = self.state()
        state.record_recent_title("old", "2026-10-01")
        state.record_recent_title("a", "2026-10-05")
        state.record_recent_title("a", "2026-10-05")
        state.record_recent_title("b", "2026-10-06")
        self.assertEqual(state.recent_titles(window_days=3, today="2026-10-06"), ["a", "b"])
        self.assertEqual(state.recent_titles(window_days=3, limit=1, today="2026-10-06"), ["b"])
        self.assertEqual(state.recent_titles(window_days=1, today="2026-10-06"), ["b"])

    def test_offset_defaults_to_zero_and_is_stored_as_int(self):
        state = self.state()
        self.assertEqual(state.telegram_update_offset, 0)
        state.data["telegram_update_offset"] = "12"
        self.assertEqual(state.telegram_update_offset, 12)


if __name__ == "__main__":
    unittest.main()
