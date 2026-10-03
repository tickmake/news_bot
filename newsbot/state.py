"""Shared mutable state: persisted dedup state, caches, locks."""

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from newsbot.config import SETTINGS
from newsbot.logsetup import SYS_LOG


LIVE_QUOTES_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


FINNHUB_QUOTE_CACHE: Dict[str, Tuple[float, Dict[str, float]]] = {}


HISTORY_CACHE: Dict[str, Tuple[float, Any]] = {}
TICKER_INFO_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
EVENT_PROFILE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}

# Last-known ranker state, surfaced by /health.
RANKER_STATUS: Dict[str, Any] = {
    "path": "unknown",
    "latency_ms": None,
    "error": None,
    "error_at": None,
}

# Guards shared mutable state against overlapping scheduler/poller threads.
STATE_LOCK = threading.RLock()
CACHE_LOCK = threading.RLock()
TRADE_HISTORY_LOCK = threading.RLock()


class AppState:
    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {
            "sent_headline_keys": {},
            "recent_titles": {},
            "telegram_update_offset": 0,
            "last_health_ping_date": "",
            "last_run_status": "",
        }
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as file_obj:
                loaded = json.load(file_obj)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except Exception as exc:
            SYS_LOG.warning("state_load_failed detail=%s", exc)

    def save(self) -> None:
        # Serialise under the lock and write atomically so overlapping
        # scheduler/poller threads cannot interleave or corrupt the file.
        with STATE_LOCK:
            try:
                payload = json.dumps(self.data, indent=2, sort_keys=True)
            except Exception as exc:
                SYS_LOG.warning("state_serialize_failed detail=%s", exc)
                return
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            try:
                fd, tmp_path = tempfile.mkstemp(prefix=".news_bot_state.", dir=directory)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
                        file_obj.write(payload)
                    os.replace(tmp_path, self.path)
                except Exception:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    raise
            except Exception as exc:
                SYS_LOG.warning("state_save_failed detail=%s", exc)

    @staticmethod
    def _window_cutoff(window_days: int, today: Optional[str]) -> str:
        """Earliest date key still inside the window, as YYYY-MM-DD.

        The window is calendar days back from `today`, not "the last N buckets
        that happen to have data" — otherwise a gap in sends would drag a stale
        bucket back into the window, and a single stored bucket would always
        count as recent.
        """
        anchor_text = today or datetime.today().strftime("%Y-%m-%d")
        try:
            anchor = datetime.strptime(anchor_text, "%Y-%m-%d")
        except ValueError:
            anchor = datetime.today()
        return (anchor - timedelta(days=max(0, window_days - 1))).strftime("%Y-%m-%d")

    @staticmethod
    def _prune_buckets(bucket: Dict[str, Any], keep: int = 7) -> None:
        if len(bucket) > keep:
            for old_date in sorted(bucket.keys())[:-keep]:
                bucket.pop(old_date, None)

    def mark_headline_seen(self, section: str, keys: Iterable[str], date_key: str) -> None:
        # A bare string is iterable, so passing one would silently store its
        # individual characters. Accept it as a single key instead.
        if isinstance(keys, str):
            keys = [keys]
        with STATE_LOCK:
            sent = self.data.setdefault("sent_headline_keys", {})
            section_bucket = sent.setdefault(section, {})
            day_keys = section_bucket.setdefault(date_key, [])
            for key in keys:
                if key not in day_keys:
                    day_keys.append(key)
            self._prune_buckets(section_bucket)

    def has_seen_headline(
        self,
        section: str,
        key: str,
        window_days: int = 7,
        today: Optional[str] = None,
    ) -> bool:
        with STATE_LOCK:
            section_bucket = self.data.get("sent_headline_keys", {}).get(section, {})
            cutoff = self._window_cutoff(window_days, today)
            for date_key, keys in section_bucket.items():
                if date_key >= cutoff and key in keys:
                    return True
            return False

    def record_recent_title(self, title: str, date_key: str) -> None:
        with STATE_LOCK:
            recent = self.data.setdefault("recent_titles", {})
            day_titles = recent.setdefault(date_key, [])
            if title not in day_titles:
                day_titles.append(title)
            self._prune_buckets(recent)

    def recent_titles(
        self, window_days: int = 3, limit: int = 30, today: Optional[str] = None
    ) -> List[str]:
        with STATE_LOCK:
            recent = self.data.get("recent_titles", {})
            cutoff = self._window_cutoff(window_days, today)
            collected: List[str] = []
            for date_key in sorted(recent.keys()):
                if date_key < cutoff:
                    continue
                for title in recent.get(date_key, []):
                    if title not in collected:
                        collected.append(title)
            return collected[-limit:]

    @property
    def telegram_update_offset(self) -> int:
        with STATE_LOCK:
            return int(self.data.get("telegram_update_offset", 0))

    @telegram_update_offset.setter
    def telegram_update_offset(self, value: int) -> None:
        with STATE_LOCK:
            self.data["telegram_update_offset"] = value


STATE = AppState(SETTINGS.state_file)
