"""Headlines that mention a pick: context for why it moved.

Uses the business and global feeds the briefing already reads, so it adds no data
source and no credentials. Matching is deliberately conservative: a company name as
a whole word, or the ticker written the way news does it, `$NVDA` or `(NVDA)`.
Bare tickers are not matched ("NOW", "ALL" and "ARE" are also words), and names too
short or too generic to be safe are skipped. The point is a headline the reader can
judge, not a sentiment score; the screen has not been shown to have an edge, and
nothing here changes what qualifies.
"""

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Dict, List, Optional, Sequence, Tuple, Union

from newsbot import news_feeds
from newsbot.config import BUSINESS_NEWS_FEEDS, GLOBAL_NEWS_FEEDS, SETTINGS
from newsbot.logsetup import SYS_LOG

_CORPORATE_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|holdings?|group|"
    r"ag|se|nv|sa|asa|ab|oyj|spa|n\.v|s\.a|class [abc])\b\.?",
    re.IGNORECASE,
)
# Names that are ordinary words, or short enough to match unrelated headlines.
_UNSAFE_NAMES = frozenset({
    "target", "block", "visa", "gap", "ford", "general", "first", "national", "united", "american",
    "global", "capital", "energy", "group", "services", "systems", "technologies", "international",
    "the", "one", "best", "home", "live", "face", "snap", "peloton", "square", "unity", "match",
    "bank", "north", "south", "east", "west", "new", "metro", "trust", "financial", "industries",
})
MIN_NAME_LENGTH = 4

_POOL_LOCK = threading.Lock()
_POOL: Optional[Tuple[float, List[news_feeds.Candidate]]] = None


@dataclass(frozen=True)
class Headline:
    title: str
    domain: str
    age_hours: Optional[float]

    @property
    def text(self) -> str:
        age = "" if self.age_hours is None else f", {self.age_hours:.0f}h ago"
        source = self.domain or "news"
        return f"📰 “{escape(self.title)}” ({escape(source)}{age})"


def company_key(label: str) -> Optional[str]:
    """A safe matching name from a label such as "Apple Inc. [USA Stock]", else None."""
    name = re.sub(r"\[[^\]]*\]", " ", label or "")
    name = _CORPORATE_SUFFIXES.sub(" ", name)
    name = re.sub(r"\.com\b", " ", name, flags=re.IGNORECASE)   # "Amazon.com" is "Amazon" in headlines
    name = re.sub(r"[^A-Za-z0-9&'\-\. ]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .-&")
    if len(name) < MIN_NAME_LENGTH or name.lower() in _UNSAFE_NAMES:
        return None
    # An all-uppercase "name" is almost always the ticker echoed back.
    if name.isupper() and len(name) <= 5:
        return None
    return name


def name_variants(label: str) -> List[str]:
    """The cleaned name, plus its first word when that alone is a safe short form
    ("Meta Platforms" is "Meta" in headlines, "JPMorgan Chase" is "JPMorgan")."""
    full = company_key(label)
    if full is None:
        return []
    variants = [full]
    first = full.split(" ")[0]
    if first != full and company_key(first) is not None and first.lower() not in _UNSAFE_NAMES:
        variants.append(first)
    return variants


def mentions(title: str, symbol: str, keys: Union[None, str, Sequence[str]]) -> bool:
    """Whether a headline is about this company, conservatively."""
    ticker = symbol.split(".")[0].upper()
    if len(ticker) >= 2 and re.search(rf"(\$|\()\s*{re.escape(ticker)}\s*\)?(?![A-Za-z0-9])", title):
        return True
    names = [keys] if isinstance(keys, str) else list(keys or [])
    for name in names:
        if name and re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", title, re.IGNORECASE):
            return True
    return False


def _pool() -> List[news_feeds.Candidate]:
    """Recent headlines from the business and global feeds, cached briefly."""
    global _POOL
    now = time.time()
    with _POOL_LOCK:
        if _POOL and now - _POOL[0] <= SETTINGS.catalysts_pool_ttl_seconds:
            return _POOL[1]
    feeds = list(dict.fromkeys(list(BUSINESS_NEWS_FEEDS) + list(GLOBAL_NEWS_FEEDS)))
    candidates = news_feeds._collect_candidates("business", feeds, max(40, SETTINGS.news_candidate_pool_size))
    with _POOL_LOCK:
        _POOL = (time.time(), candidates)
    return candidates


def reset_cache() -> None:
    global _POOL
    with _POOL_LOCK:
        _POOL = None


def find(symbols: Sequence[str], labels: Optional[Dict[str, str]] = None,
         now: Optional[datetime] = None) -> Dict[str, List[Headline]]:
    """Up to `catalysts_max_per_pick` fresh headlines per symbol. Never raises."""
    if not SETTINGS.catalysts_enabled or not symbols or SETTINGS.catalysts_max_per_pick < 1:
        return {}
    try:
        pool = _pool()
    except Exception as exc:
        SYS_LOG.warning("catalysts_pool_failed detail=%s", exc)
        return {}
    now = now or datetime.now(timezone.utc)
    result: Dict[str, List[Headline]] = {}
    limit = SETTINGS.catalysts_max_per_pick
    for symbol in symbols:
        keys = name_variants((labels or {}).get(symbol, ""))
        found: List[Headline] = []
        seen_titles = set()
        # Newest first; undated items last.
        ordered = sorted(
            pool, key=lambda c: c.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True
        )
        for candidate in ordered:
            age = news_feeds._age_hours(candidate, now)
            if age is not None and age > SETTINGS.catalysts_max_age_hours:
                continue
            if candidate.title in seen_titles or not mentions(candidate.title, symbol, keys):
                continue
            seen_titles.add(candidate.title)
            found.append(Headline(candidate.title, candidate.domain, age))
            if len(found) >= limit:
                break
        if found:
            result[symbol] = found
    return result
