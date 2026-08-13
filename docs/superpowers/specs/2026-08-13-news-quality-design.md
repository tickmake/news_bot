# News Quality: Deduplication and Relevance Ranking

**Date:** 2026-08-13
**Status:** Approved design
**Scope:** Content quality of the news sections only. New sections and new data sources are a separate project with their own spec.

## Problem

The briefing repeats stories and ranks them by accident.

Two defects drive this, both in `news_bot.py`:

1. **Repetition.** `_is_duplicate_headline` compares `sha256(title|url)`. Two outlets covering one story produce two different hashes, so both appear. Separately, `has_seen_headline` queries only today's bucket even though `mark_headline_seen` retains seven days, so yesterday's story reappears today.

2. **No ranking.** `_build_news_section` takes the first N items in raw feed order. Feed order is not chronological: a probe of `feeds.bbci.co.uk/news/world/rss.xml` on 2026-08-13 returned items timestamped 04:55, 09:52, 08:12, 01:31 GMT in that order. `_parse_rss_items` also discards the publish date entirely, so recency is not available as a signal even in principle.

Note that stale items are *not* caused by caching. The news path has no cache — `_fetch_rss_items` hits the network on every send. Only market data is cached.

## Goals

- Collapse the same story from multiple outlets into one line.
- Suppress a story already shown in the last several days.
- Order by importance, weighted toward topics the reader cares about, with fresher items favoured.
- Never fail closed: a section renders even when every enhancement is unavailable.

## Non-goals

- New briefing sections or data sources.
- Article summarization. Output stays a numbered headline with a `(more)` link.
- Hard topic suppression. Weighting only — a sports headline may still appear if nothing better exists.

## Architecture

Four stages. Stages 1 and 4 exist today; 2 and 3 are new.

```
FETCH  →  NORMALIZE  →  SELECT  →  RENDER
```

### 1. Fetch

Providers still run in `NEWS_FETCH_PRIORITY` order, but every provider runs and results pool together rather than stopping at the first provider yielding enough items. Target pool is `NEWS_CANDIDATE_POOL_SIZE` (default 60) per section. A provider failure shrinks the pool; it does not fail the section.

This changes the meaning of `NEWS_FETCH_PRIORITY` from "which provider to use" to "tie-break order among equals." Cross-outlet deduplication is impossible without it: you cannot collapse duplicates you never fetched.

### 2. Normalize

Each raw item becomes a `Candidate`:

```python
@dataclass(frozen=True)
class Candidate:
    title: str
    url: Optional[str]
    domain: str
    published_at: Optional[datetime]   # tz-aware UTC, None if unparseable
    provider: str                      # newsapi | freenews | rss
    trusted: bool                      # domain in TRUSTED_NEWS_DOMAINS
```

Date parsing uses the standard library only:

- RSS `<pubDate>` — RFC 2822, via `email.utils.parsedate_to_datetime`.
- `<dc:date>` and Atom `<published>`/`<updated>` — ISO 8601, via `datetime.fromisoformat` after rewriting a trailing `Z` to `+00:00`.
- NewsAPI/FreeNews JSON — ISO 8601 in `publishedAt` / `published_at` / `date`, same path.

All timestamps normalize to UTC. Unparseable or absent dates yield `published_at = None`.

**Undated items stay eligible.** They are exempt from the `NEWS_MAX_AGE_HOURS` ceiling and receive a neutral recency score. Some feeds expose no date at all (`feeds.apnews.com/apf-topnews` returned none on probe), and dropping them would silently remove a whole source. This is also a test constraint: existing fixtures in `test_news_bot.py` carry no `pubDate`, and dropping undated items would break them.

Domain allow/blocklist filtering and the cross-day seen-check both apply here, so the selector only ever sees eligible candidates.

The seen-check at this stage is the *mechanical* one — exact key and cluster key, described under State and deduplication below. It catches identical and lightly reworded repeats. It does not catch a story republished the next day in genuinely different words, which is why `LlmSelector` additionally receives recent titles in its prompt. The two checks are complementary, not redundant.

### 3. Select

The core. Takes a candidate pool, returns an ordered list of at most `limit` selections:

```python
@dataclass(frozen=True)
class Selection:
    candidate: Candidate
    duplicates: List[Candidate]   # collapsed into this one; marked seen alongside it
    reason: str                   # logging and /health only, never shown to the reader

class Selector(Protocol):
    def select(self, pool: List[Candidate], section: str, limit: int) -> List[Selection]: ...
```

Two implementations behind one interface.

#### LlmSelector

One Claude call per section per send.

- **Model:** `NEWS_RANKER_MODEL`, default `claude-opus-5`.
- **Effort:** `output_config={"effort": "low"}`. Ranking sixty headlines is a short, scoped task; low effort keeps thinking tokens (and therefore cost) down.
- **Structured output:** `output_config={"format": {"type": "json_schema", "schema": ...}}`. This guarantees parseable JSON, so there is no regex extraction and no retry-on-parse loop. Assistant prefill is not used — it returns 400 on Opus 5.
- **`max_tokens`:** 8000, non-streaming. On Opus 5 thinking is on by default and `max_tokens` caps thinking plus response text together, so the ceiling needs headroom above the ~600-token JSON payload. Output is billed on actual tokens, so unused headroom is free.
- **Timeout:** `NEWS_RANKER_TIMEOUT_SECONDS`, default 20, via `client.with_options(timeout=...)`.

Response schema:

```json
{
  "type": "object",
  "properties": {
    "selections": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "id": {"type": "integer"},
          "duplicate_ids": {"type": "array", "items": {"type": "integer"}},
          "reason": {"type": "string"}
        },
        "required": ["id", "duplicate_ids", "reason"],
        "additionalProperties": false
      }
    }
  },
  "required": ["selections"],
  "additionalProperties": false
}
```

Structured outputs do not support array length constraints, so the selection count is stated in the prompt and enforced client-side by truncation.

**The model returns indices, never text.** Every returned `id` is validated against the pool's index range; out-of-range or duplicate ids are dropped with a warning. Displayed headlines always come from the local `Candidate`, never from model output. This bounds the damage from a malformed or manipulated response to "a worse pick."

**Feed content is untrusted input.** Headlines come from third-party feeds and are data, not instructions. The system prompt frames the candidate list explicitly as untrusted data to be ranked, and the id-only schema means a headline containing injected instructions cannot alter the output format, exfiltrate anything, or inject text into the briefing. Worst case is a bad ranking.

**Cross-day awareness.** The prompt includes the titles shown in the last `NEWS_RECENT_TITLE_DAYS` days (default 3, capped at 30 titles) with an instruction to skip anything that is the same story. This is what catches a story that reappears the next day under reworded copy — the hash and shingle checks cannot.

#### HeuristicSelector

Pure-Python fallback. No network, no key, fully deterministic.

```
score = 0.45 * recency + 0.40 * topic + 0.15 * source_tier
```

- `recency` — exponential decay, `exp(-age_hours / 10)`; undated items score a neutral 0.4.
- `topic` — normalized keyword hits against the configured topic categories.
- `source_tier` — 1.0 if the domain is in `TRUSTED_NEWS_DOMAINS`, else 0.5.

Near-duplicate collapsing: normalize the title (casefold, strip punctuation and stopwords), take 4-character shingles, cluster on Jaccard similarity above 0.6. Exact URL matches always cluster. This catches reworded and re-punctuated variants of the same headline; it does **not** catch full cross-outlet paraphrase, which is the acknowledged capability gap between the two selectors and the main reason the LLM path exists.

Weights and the half-life are module constants, not configuration. They are the fallback path's tuning, not a user-facing knob.

#### Selector choice and failure handling

`LlmSelector` is used when `NEWS_RANKER_ENABLED` is true **and** an Anthropic credential is available. Every failure falls through to `HeuristicSelector`: missing key, timeout, connection error, rate limit, API error, schema validation failure, empty selection, all ids invalid. Each fallthrough logs at warning with the cause and records it for `/health`. The section always renders.

Selection is a pure function of the pool. It performs no state writes, which is what makes the mark-on-send fix below possible and lets both selectors be tested against identical fixtures with no state file.

### 4. Render

Unchanged. Same `_format_headline_line`, same numbered list with `(more)` links, same section headers. Nothing the reader sees changes structurally — only which five headlines appear and in what order.

## Topic weighting

Topics are code-defined categories carrying both a weight and a keyword list, so one definition serves both selectors — the LLM receives category names and weights, the heuristic receives keywords and weights.

Defaults, up-weighted. The keyword column below is illustrative; the authoritative list is a module constant in `news_bot.py`, and the implementer should expand each category to roughly 10–20 terms covering both languages.

| Category | Weight | Example keywords (English and Norwegian) |
|---|---|---|
| `markets` | 2.0 | fed, rate, inflation, earnings, bourse, rente, børs |
| `norway` | 2.0 | norway, oslo, norge, regjeringen, storting |
| `india` | 2.0 | india, rbi, sensex, nifty, rupee |
| `tech` | 1.5 | ai, chip, semiconductor, teknologi, kunstig intelligens |

Down-weighted: `sports`, `celebrity`, `crime`, `lifestyle`, `shopping`, all at 0.3.

`NEWS_TOPIC_WEIGHTS` overrides weights only, as `markets:2.0,sports:0.2`. Keyword lists stay in code. Down-weighting never removes a candidate from the pool, so a low-weight headline still appears when nothing better is available.

## State and deduplication

Three changes to `AppState`:

1. **`has_seen_headline` scans a window.** It currently reads only today's bucket while `mark_headline_seen` retains seven days. It gains a `window_days` parameter (`NEWS_DEDUP_WINDOW_DAYS`, default 7) and scans every bucket in range. This is the smallest change with the largest effect on the repetition complaint.

2. **Two keys per headline.** The existing exact key `sha256(title|url)`, plus a normalized cluster key: casefold, strip punctuation and stopwords, sort the remaining tokens, hash. The cluster key catches word-order and light-rewording repeats across days. Both are checked; both are stored.

3. **Recent titles retained.** Plain titles from the last `NEWS_RECENT_TITLE_DAYS` days, capped at 30, for the LLM prompt's cross-day check. Bounded growth, pruned on write like the existing buckets.

All duplicates collapsed into a `Selection` are marked seen along with the shown headline, so tomorrow's run suppresses the whole cluster rather than just the one line that was displayed.

## Mark-on-send bug fix

`_build_news_section` marks headlines seen while building the message, but `job_daily_briefing` sends afterwards. When a send fails, the headlines are already recorded as seen and are suppressed permanently — a silent loss of a day's news.

`compose_briefing` returns a `Briefing` instead of a string:

```python
@dataclass
class Briefing:
    message: str
    pending: List[Tuple[str, str]]   # (section, key) pairs, exact and cluster
```

`job_daily_briefing` marks `pending` seen only after `send_telegram_message` returns success. A failed send leaves state untouched and the stories reappear next run. The `/now`, `/morning`, and `/evening` command handlers use the same path and inherit the fix.

## Configuration

New environment variables, all with working defaults:

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables the LLM selector. Absent means heuristic-only. |
| `NEWS_RANKER_ENABLED` | `true` | Kill switch; no-ops without a credential. |
| `NEWS_RANKER_MODEL` | `claude-opus-5` | Ranking model. |
| `NEWS_RANKER_EFFORT` | `low` | Effort level. |
| `NEWS_RANKER_TIMEOUT_SECONDS` | `20` | Per-call timeout. |
| `NEWS_MAX_AGE_HOURS` | `30` | Freshness ceiling; undated items exempt. |
| `NEWS_CANDIDATE_POOL_SIZE` | `60` | Candidates per section. |
| `NEWS_TOPIC_WEIGHTS` | unset | Weight overrides. |
| `NEWS_DEDUP_WINDOW_DAYS` | `7` | Cross-day suppression window. |
| `NEWS_RECENT_TITLE_DAYS` | `3` | Recent-title retention for the LLM prompt. |

New dependency: `anthropic`, pinned in `requirements.txt` per the existing convention. It is imported lazily inside `LlmSelector` so the bot still starts if the package is missing. `.env.example` and `README.md` are updated.

### Cost

At roughly 2K input and 2K output tokens per call, two sections, two sends per day:

| Model | Approx. monthly |
|---|---|
| `claude-opus-5` | ~$7 |
| `claude-sonnet-5` | ~$4 |
| `claude-haiku-4-5` | ~$1.50 |

Prompt caching is deliberately not used. Sends are twelve hours apart and the default cache TTL is five minutes, so a cached prefix would never be read and would only add write cost.

## Testing

- **Fixture pools** — JSON candidate sets covering known cross-outlet duplicates, stale items, undated items, and off-topic items. Both selectors run against identical fixtures.
- **`HeuristicSelector`** — deterministic assertions on ordering, clustering, decay, and neutral scoring of undated items.
- **`LlmSelector`** — a stubbed client, no network in CI. Covers prompt assembly, schema validation, out-of-range and duplicate id rejection, and that each failure mode falls through to the heuristic rather than raising.
- **Date parsing** — RFC 2822, ISO 8601 with `Z`, `dc:date`, Atom, malformed, and absent.
- **Cross-day dedup** — the same story on consecutive days is suppressed on day two, via both the exact key and the cluster key.
- **Mark-on-send** — a failed send leaves `sent_headline_keys` unchanged; a successful one records every key including collapsed duplicates.
- **Existing suite** — must pass unchanged except `test_get_global_news_prefers_newsapi_when_key_present`, whose name no longer describes pooling behavior and which is rewritten to assert pooling.

## Observability

`/health` gains: selector path in use (`llm` or `heuristic`), last ranker latency, last ranker error with timestamp, and per-section candidate pool sizes.

One structured log line per section:

```
news_select section=global pool=57 selected=5 collapsed=9 path=llm latency_ms=1840
```

This makes the two questions you will actually ask answerable: is the LLM path running, and is the pool big enough for deduplication to have anything to do.

## Risks

- **Pooling multiplies fetch cost.** Every provider runs on every send instead of short-circuiting. Feeds are small and the bot runs twice daily, so this is acceptable; if it becomes a latency problem, providers can be fetched concurrently.
- **Cross-day paraphrase suppression depends on the LLM path.** On the heuristic path it is best-effort, limited to what the cluster key catches. This is an accepted degradation, not a defect.
- **Topic keyword lists rot.** They are a fallback-path heuristic, not the primary ranking mechanism, so drift degrades gracefully.
