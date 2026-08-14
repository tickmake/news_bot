# Banded Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render infrastructure, system, and application logs as three visually distinct bands in Portainer's container log view.

**Architecture:** The band is carried by the logger name — `news_bot.app`, `news_bot.sys`, everything else — and a custom `logging.Formatter` reads `record.name` to pick a band tag and ANSI colour. A single handler on the root logger writes to stdout. Two independent level settings keep library noise out when the app is switched to DEBUG.

**Tech Stack:** Python standard `logging`, `unittest`, no new dependencies.

## Global Constraints

- All code in `news_bot.py`; all tests in `test_news_bot.py`. This project is a single module with a single test file — do not create new modules.
- Tests use `unittest`, class-per-concern, module-level helper functions. Do not introduce pytest.
- Run tests with `.venv/bin/python -m unittest`. CI uses Python 3.11.
- Use `typing` generics (`List`, `Dict`, `Optional`, `Tuple`), not PEP 585 builtins.
- Existing log call sites keep their `event=key=value` message style: `LOGGER.warning("thing_happened symbol=%s detail=%s", ...)`. Only the *logger object* changes, never the message text.
- All 174 existing tests must still pass at the end of every task.
- Band tags, verbatim: `APP`, `SYS`, `INFRA`. Level tags, verbatim: `DEBUG`, `INFO`, `WARN`, `ERROR`.
- Line layout, verbatim: `{timestamp}  {band:<5}  {level:<5}  {message}` — two spaces between every field, timestamp `%Y-%m-%d %H:%M:%S` with no milliseconds.
- Spec: `docs/superpowers/specs/2026-08-14-banded-logging-design.md`

### Ordering constraint (applies to Task 3)

`SETTINGS = AppSettings()` is defined at `news_bot.py:184`, but the current
`logging.basicConfig` call sits at line 26 — logging is configured before
settings exist. Anything reading `SETTINGS.log_level` must therefore run
*after* line 184. Verified: no `LOGGER.*` call fires between line 26 and line
184, so moving configuration later loses no records.

---

### Task 1: Band resolution and plain formatter

**Files:**
- Modify: `news_bot.py:25-29` — replace the `basicConfig` block
- Test: `test_news_bot.py` — new class `LogBandTests`

**Interfaces:**
- Produces:
  - `LOGGER = logging.getLogger("news_bot")` (unchanged, retained as parent)
  - `APP_LOG = logging.getLogger("news_bot.app")`
  - `SYS_LOG = logging.getLogger("news_bot.sys")`
  - `BAND_APP = "APP"`, `BAND_SYS = "SYS"`, `BAND_INFRA = "INFRA"`
  - `_band_for(logger_name: str) -> str`
  - `BandedFormatter(logging.Formatter)` with `format(record) -> str`

- [ ] **Step 1: Write the failing tests**

Add `import logging` and `import re` to the imports at the top of
`test_news_bot.py` if not already present, then append at the end of the file:

```python
def _record(name, level=logging.INFO, msg="hello world", args=None, exc_info=None):
    """A LogRecord as the logging module would construct it."""
    return logging.LogRecord(
        name=name, level=level, pathname="news_bot.py", lineno=1,
        msg=msg, args=args, exc_info=exc_info,
    )


class LogBandTests(unittest.TestCase):
    def test_app_logger_resolves_to_app_band(self):
        self.assertEqual(news_bot._band_for("news_bot.app"), "APP")

    def test_sys_logger_resolves_to_sys_band(self):
        self.assertEqual(news_bot._band_for("news_bot.sys"), "SYS")

    def test_bare_parent_logger_resolves_to_sys_band(self):
        """A missed call site should land somewhere sane, not be mistaken
        for a third-party library."""
        self.assertEqual(news_bot._band_for("news_bot"), "SYS")

    def test_third_party_logger_resolves_to_infra_band(self):
        self.assertEqual(news_bot._band_for("apscheduler.scheduler"), "INFRA")
        self.assertEqual(news_bot._band_for("urllib3.connectionpool"), "INFRA")

    def test_line_layout_is_two_spaces_between_padded_fields(self):
        line = news_bot.BandedFormatter(colour=False).format(
            _record("news_bot.app", msg="briefing_start at=x")
        )
        # timestamp(19) + 2 + band(5) + 2 + level(5) + 2 + message
        self.assertRegex(
            line,
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}  APP    INFO   briefing_start at=x$",
        )

    def test_warning_renders_as_warn_and_critical_as_error(self):
        fmt = news_bot.BandedFormatter(colour=False)
        self.assertIn("WARN ", fmt.format(_record("news_bot.sys", logging.WARNING)))
        self.assertIn("ERROR", fmt.format(_record("news_bot.sys", logging.CRITICAL)))

    def test_infra_lines_carry_the_originating_logger_name(self):
        line = news_bot.BandedFormatter(colour=False).format(
            _record("apscheduler.scheduler", msg="Scheduler started")
        )
        self.assertIn("apscheduler.scheduler | Scheduler started", line)

    def test_app_and_sys_lines_omit_the_logger_name(self):
        fmt = news_bot.BandedFormatter(colour=False)
        for name in ("news_bot.app", "news_bot.sys"):
            self.assertNotIn(name, fmt.format(_record(name, msg="telegram_sent chunks=1")))

    def test_event_prefix_is_gone(self):
        """`event=` was only ever true for app records; it mislabelled every
        library message."""
        line = news_bot.BandedFormatter(colour=False).format(_record("news_bot.app"))
        self.assertNotIn("event=", line)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest test_news_bot.LogBandTests -v`
Expected: FAIL — `AttributeError: module 'news_bot' has no attribute '_band_for'`

- [ ] **Step 3: Implement**

Replace `news_bot.py:25-29` (the `LOGGER = ...` line through the closing paren
of `logging.basicConfig(...)`) with:

```python
LOGGER = logging.getLogger("news_bot")
# Band is carried by logger name: BandedFormatter reads record.name. The
# hierarchy is load-bearing -- existing tests assertLogs against `news_bot`
# and keep working because child records propagate to it.
APP_LOG = logging.getLogger("news_bot.app")
SYS_LOG = logging.getLogger("news_bot.sys")

BAND_APP = "APP"
BAND_SYS = "SYS"
BAND_INFRA = "INFRA"

_LEVEL_TAGS: Dict[int, str] = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "ERROR",
}


def _band_for(logger_name: str) -> str:
    """Which band a record belongs to, from its logger name."""
    if logger_name == "news_bot.app" or logger_name.startswith("news_bot.app."):
        return BAND_APP
    if logger_name == "news_bot" or logger_name.startswith("news_bot."):
        return BAND_SYS
    return BAND_INFRA


class BandedFormatter(logging.Formatter):
    """One line as: timestamp  BAND   LEVEL  message.

    Replaces the old "%(asctime)s level=%(levelname)s event=%(message)s".
    That format ran through basicConfig on the *root* logger, so APScheduler
    and yfinance messages were rendered as `event=...` too -- asserting they
    were app events when they were not.
    """

    def __init__(self, colour: bool = True) -> None:
        super().__init__()
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        band = _band_for(record.name)
        level = _LEVEL_TAGS.get(record.levelno, "INFO")
        # Milliseconds cost four columns on every line and nothing here is
        # measured at that resolution; durations are logged as latency_ms=.
        stamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")

        message = record.getMessage()
        if band == BAND_INFRA:
            # "Scheduler started" says nothing about who said it.
            message = f"{record.name} | {message}"
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"

        return f"{stamp}  {band:<5}  {level:<5}  {message}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m unittest test_news_bot.LogBandTests -v`
Expected: PASS, 9 tests

Then the full suite: `.venv/bin/python -m unittest -q`
Expected: OK, 183 tests

- [ ] **Step 5: Commit**

```bash
git add news_bot.py test_news_bot.py
git commit -m "feat: carry the log band in the logger name

basicConfig configured the root logger, so every library shared the app's
format string and APScheduler and yfinance messages were rendered with an
event= prefix that asserted they were app events. BandedFormatter reads
record.name instead: news_bot.app is APP, anything else under news_bot is
SYS, everything else is INFRA.

The hierarchy naming is load-bearing rather than cosmetic. Two existing
tests assertLogs against news_bot and keep passing only because news_bot.sys
records propagate to it."
```

---

### Task 2: Colour

**Files:**
- Modify: `news_bot.py` — `BandedFormatter.format`, add the ANSI table above the class
- Test: `test_news_bot.py` — new class `LogColourTests`

**Interfaces:**
- Consumes: `BandedFormatter`, `_band_for`, `BAND_*` (Task 1)
- Produces: `_ANSI: Dict[str, str]` — keys `reset`, `dim`, `app`, `sys`, `warn`, `error`

- [ ] **Step 1: Write the failing tests**

Append to `test_news_bot.py`:

```python
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text):
    return _ANSI_RE.sub("", text)


class LogColourTests(unittest.TestCase):
    def test_colour_off_emits_no_escapes(self):
        line = news_bot.BandedFormatter(colour=False).format(_record("news_bot.app"))
        self.assertNotIn("\033[", line)

    def test_colour_on_emits_escapes(self):
        line = news_bot.BandedFormatter(colour=True).format(_record("news_bot.app"))
        self.assertIn("\033[", line)

    def test_stripping_colour_reproduces_the_plain_line_exactly(self):
        """The load-bearing invariant: grep, log shipping and any future
        parsing must not depend on a display setting."""
        cases = [
            _record("news_bot.app", logging.INFO),
            _record("news_bot.sys", logging.WARNING),
            _record("news_bot.sys", logging.ERROR),
            _record("apscheduler.scheduler", logging.INFO, msg="Scheduler started"),
            _record("urllib3.connectionpool", logging.DEBUG),
        ]
        plain = news_bot.BandedFormatter(colour=False)
        fancy = news_bot.BandedFormatter(colour=True)
        for record in cases:
            with self.subTest(name=record.name, level=record.levelno):
                self.assertEqual(_strip_ansi(fancy.format(record)), plain.format(record))

    def test_infra_lines_are_dimmed_whole(self):
        line = news_bot.BandedFormatter(colour=True).format(
            _record("apscheduler.scheduler", msg="Scheduler started")
        )
        self.assertTrue(line.startswith(news_bot._ANSI["dim"]))
        self.assertTrue(line.endswith(news_bot._ANSI["reset"]))

    def test_error_level_is_coloured_independently_of_band(self):
        """An error must stay obvious without losing which band it came from."""
        line = news_bot.BandedFormatter(colour=True).format(
            _record("news_bot.app", logging.ERROR)
        )
        self.assertIn(news_bot._ANSI["error"], line)
        self.assertIn(news_bot._ANSI["app"], line)

    def test_message_body_is_never_coloured(self):
        line = news_bot.BandedFormatter(colour=True).format(
            _record("news_bot.app", logging.WARNING, msg="telegram_sent chunks=3")
        )
        self.assertIn(f"{news_bot._ANSI['reset']}  telegram_sent chunks=3", line)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest test_news_bot.LogColourTests -v`
Expected: FAIL — `AttributeError: module 'news_bot' has no attribute '_ANSI'`

- [ ] **Step 3: Implement**

Insert above `class BandedFormatter` in `news_bot.py`:

```python
_ANSI: Dict[str, str] = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "app": "\033[1;36m",    # bold cyan -- the lines you came to read
    "sys": "\033[33m",      # yellow
    "warn": "\033[33m",
    "error": "\033[1;31m",  # bold red
}
```

Replace the `return` statement at the end of `BandedFormatter.format` with:

```python
        band_text = f"{band:<5}"
        level_text = f"{level:<5}"
        if self.colour:
            if band == BAND_APP:
                band_text = f"{_ANSI['app']}{band_text}{_ANSI['reset']}"
            elif band == BAND_SYS:
                band_text = f"{_ANSI['sys']}{band_text}{_ANSI['reset']}"
            # Severity colours the level tag independently, so an ERROR stays
            # obvious without losing its band.
            if record.levelno >= logging.ERROR:
                level_text = f"{_ANSI['error']}{level_text}{_ANSI['reset']}"
            elif record.levelno == logging.WARNING:
                level_text = f"{_ANSI['warn']}{level_text}{_ANSI['reset']}"

        line = f"{stamp}  {band_text}  {level_text}  {message}"
        if self.colour and band == BAND_INFRA:
            # INFRA recedes rather than competing for attention.
            line = f"{_ANSI['dim']}{line}{_ANSI['reset']}"
        return line
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m unittest test_news_bot.LogColourTests -v`
Expected: PASS, 6 tests

Full suite: `.venv/bin/python -m unittest -q`
Expected: OK, 189 tests

- [ ] **Step 5: Commit**

```bash
git add news_bot.py test_news_bot.py
git commit -m "feat: colour the log band and severity independently

Band colours the band tag, severity colours the level tag, and the message
body is never coloured -- so an ERROR stays obvious without losing which band
it came from, and the text stays legible under any terminal theme.

Stripping ANSI from a coloured line reproduces the uncoloured line exactly.
That invariant is what keeps grep, log shipping and any future parsing
independent of a display setting, and it is asserted across all three bands
and four levels."
```

---

### Task 3: Configuration, level floors, and handler installation

**Files:**
- Modify: `news_bot.py` — add `import sys`; add three settings to `AppSettings`; add `_resolve_level` and `_configure_logging`; call it after `SETTINGS = AppSettings()`
- Test: `test_news_bot.py` — new class `LogConfigTests`

**Interfaces:**
- Consumes: `BandedFormatter` (Tasks 1-2), `SETTINGS`
- Produces:
  - `AppSettings.log_level: str = "INFO"`, `log_level_libraries: str = "WARNING"`, `log_color: bool = True`
  - `_resolve_level(raw: str, fallback: int = logging.INFO) -> Tuple[int, Optional[str]]` — returns `(level_number, rejected_value_or_None)`
  - `_configure_logging() -> None` — idempotent
  - `_LOGGING_CONFIGURED: bool` module flag, which tests reset

- [ ] **Step 1: Write the failing tests**

Append to `test_news_bot.py`:

```python
class LogConfigTests(unittest.TestCase):
    def setUp(self):
        self._root_handlers = list(logging.getLogger().handlers)
        self._root_level = logging.getLogger().level
        self._app_level = news_bot.LOGGER.level

    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in self._root_handlers:
            root.addHandler(handler)
        root.setLevel(self._root_level)
        news_bot.LOGGER.setLevel(self._app_level)
        news_bot._LOGGING_CONFIGURED = True

    def _configure(self, **overrides):
        settings = {"log_level": "INFO", "log_level_libraries": "WARNING", "log_color": False}
        settings.update(overrides)
        news_bot._LOGGING_CONFIGURED = False
        with patch.multiple(news_bot.SETTINGS, **settings):
            news_bot._configure_logging()

    def test_resolve_level_accepts_known_names(self):
        self.assertEqual(news_bot._resolve_level("DEBUG"), (logging.DEBUG, None))
        self.assertEqual(news_bot._resolve_level("warning"), (logging.WARNING, None))

    def test_resolve_level_reports_an_unknown_name(self):
        """logging.getLevelName("BOGUS") returns the string "Level BOGUS"
        rather than raising, so a typo would otherwise configure nonsense."""
        level, rejected = news_bot._resolve_level("BOGUS")
        self.assertEqual(level, logging.INFO)
        self.assertEqual(rejected, "BOGUS")

    def test_invalid_level_warns_and_names_the_ignored_value(self):
        with self.assertLogs(news_bot.LOGGER, level="WARNING") as logs:
            self._configure(log_level="BOGUS")
        self.assertTrue(any("log_level_invalid" in line for line in logs.output))
        self.assertTrue(any("BOGUS" in line for line in logs.output))

    def test_handler_writes_to_stdout_not_stderr(self):
        """basicConfig defaults to stderr, so routine INFO arrived on the
        error stream."""
        self._configure()
        handlers = logging.getLogger().handlers
        self.assertEqual(len(handlers), 1)
        self.assertIs(handlers[0].stream, sys.stdout)

    def test_configuring_twice_installs_one_handler(self):
        self._configure()
        news_bot._configure_logging()  # second call, flag already set
        self.assertEqual(len(logging.getLogger().handlers), 1)

    def test_app_debug_does_not_unleash_library_debug(self):
        """urllib3 emits a line per socket at DEBUG; the app must be able to
        go verbose without that."""
        self._configure(log_level="DEBUG", log_level_libraries="WARNING")
        self.assertTrue(news_bot.SYS_LOG.isEnabledFor(logging.DEBUG))
        self.assertFalse(logging.getLogger("urllib3.connectionpool").isEnabledFor(logging.DEBUG))

    def test_library_level_can_be_lowered_on_its_own(self):
        self._configure(log_level="INFO", log_level_libraries="DEBUG")
        self.assertTrue(logging.getLogger("urllib3.connectionpool").isEnabledFor(logging.DEBUG))
```

Add `import sys` to the test file imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest test_news_bot.LogConfigTests -v`
Expected: FAIL — `AttributeError: module 'news_bot' has no attribute '_resolve_level'`

- [ ] **Step 3: Implement**

Add `import sys` to the imports at the top of `news_bot.py` (alphabetical, after `import os`).

Add to `AppSettings`, immediately after the `tz: str = "Europe/Oslo"` line:

```python
    log_level: str = "INFO"
    log_level_libraries: str = "WARNING"
    log_color: bool = True
```

Add immediately after the `BandedFormatter` class:

```python
_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LOGGING_CONFIGURED = False


def _resolve_level(raw: str, fallback: int = logging.INFO) -> Tuple[int, Optional[str]]:
    """Level number, plus the rejected value when the name was not recognised.

    logging.getLevelName("BOGUS") returns the *string* "Level BOGUS" rather
    than raising, so a typo'd level would sail through and configure nonsense.
    """
    text = (raw or "").strip().upper()
    if text in _VALID_LEVELS:
        return getattr(logging, text), None
    return fallback, raw


def _configure_logging() -> None:
    """Install the banded handler on the root logger. Idempotent.

    Must run after SETTINGS exists. Called twice -- module re-import, test
    setup -- an unguarded version would duplicate every line.
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    app_level, rejected_app = _resolve_level(SETTINGS.log_level)
    lib_level, rejected_lib = _resolve_level(SETTINGS.log_level_libraries, logging.WARNING)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(BandedFormatter(colour=SETTINGS.log_color))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)

    # Libraries inherit the root level; news_bot carries its own. Ancestor
    # logger levels do not filter propagated records -- only the originating
    # logger's effective level and the handler's level do -- so news_bot at
    # DEBUG still reaches this handler with root at WARNING, while urllib3
    # inherits WARNING and stays quiet.
    root.setLevel(lib_level)
    LOGGER.setLevel(app_level)

    _LOGGING_CONFIGURED = True

    if rejected_app is not None:
        SYS_LOG.warning("log_level_invalid value=%s using=INFO", rejected_app)
    if rejected_lib is not None:
        SYS_LOG.warning("log_level_libraries_invalid value=%s using=WARNING", rejected_lib)
```

Add this line immediately after `SETTINGS = AppSettings()`:

```python
_configure_logging()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m unittest test_news_bot.LogConfigTests -v`
Expected: PASS, 7 tests

Full suite: `.venv/bin/python -m unittest -q`
Expected: OK, 196 tests

- [ ] **Step 5: Commit**

```bash
git add news_bot.py test_news_bot.py
git commit -m "feat: configurable log levels with a separate library floor

LOG_LEVEL was hardcoded to INFO, so eight LOGGER.debug calls could never
emit -- five of them the per-symbol reasons behind the trade screen's
'no data N' count, computed and then discarded.

LOG_LEVEL and LOG_LEVEL_LIBRARIES are separate because turning the app to
DEBUG must not turn urllib3 to DEBUG. This works because ancestor logger
levels do not filter propagated records: news_bot at DEBUG still reaches the
root handler with root at WARNING, while urllib3 inherits WARNING.

LOG_COLOR defaults to true rather than to isatty(). A container has no TTY,
so TTY detection would disable colour in exactly the place it was asked for.

Output moves from stderr to stdout; basicConfig defaulted to stderr, so every
routine INFO line arrived on the error stream."
```

---

### Task 4: Reclassify the 38 call sites

**Files:**
- Modify: `news_bot.py` — every `LOGGER.debug/info/warning/error` call site
- Test: `test_news_bot.py` — new class `LogRoutingTests`

**Interfaces:**
- Consumes: `APP_LOG`, `SYS_LOG` (Task 1)
- Produces: no new symbols; `LOGGER` remains defined but is no longer called directly

- [ ] **Step 1: Write the failing tests**

Append to `test_news_bot.py`:

```python
class LogRoutingTests(unittest.TestCase):
    def test_business_events_go_to_the_app_logger(self):
        # Both credentials blanked so this can never reach the network, even
        # if a real .env is present.
        with patch.object(news_bot.SETTINGS, "telegram_token", ""), patch.object(
            news_bot.SETTINGS, "telegram_chat_id", ""
        ):
            with self.assertLogs(news_bot.APP_LOG, level="WARNING") as logs:
                sent = news_bot.send_telegram_message("x")
        self.assertFalse(sent)
        self.assertTrue(any("telegram_skipped" in line for line in logs.output))

    def test_plumbing_events_go_to_the_sys_logger(self):
        with self.assertLogs(news_bot.SYS_LOG, level="WARNING") as logs:
            with patch.object(news_bot.SETTINGS, "news_topic_weights", "nonsense:1.0"):
                news_bot._resolve_topic_weights()
        self.assertTrue(any("topic_weight_unknown" in line for line in logs.output))

    def test_no_call_site_uses_the_bare_parent_logger(self):
        """Every emitter must pick a band explicitly.

        Resolved via news_bot.__file__ rather than a relative path, so the
        test does not depend on the working directory.
        """
        with open(news_bot.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for level in ("debug", "info", "warning", "error"):
            self.assertNotIn(f"LOGGER.{level}(", source)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m unittest test_news_bot.LogRoutingTests -v`
Expected: FAIL — records still arrive on `news_bot`, and `LOGGER.warning(` is still present in the source

- [ ] **Step 3: Implement**

Change the logger object only. Do not touch message text, arguments, or
levels.

**These ten become `APP_LOG`** — what the bot exists to do:

| Event | Level |
|---|---|
| `briefing_start` | info |
| `news_select` | info |
| `telegram_sent` | info |
| `telegram_skipped` | warning |
| `telegram_send_failed` | error |
| `briefing_send_failed` | warning |
| `command_received` | info |
| `ranker_failed` | warning |
| `trade_candidates_deadline_reached` | warning |
| `watchlist_truncated` | warning |

`ranker_failed` is APP rather than SYS because it changes what the reader
receives: it is the signal that headlines were ranked by the heuristic
instead of the model.

**These twenty-four become `SYS_LOG`** — the bot's own plumbing:

`retry`, `retry_exhausted`, `rss_fetch_failed`, `newsapi_fetch_failed`,
`freenews_fetch_failed`, `provider_failed`, `finnhub_quote_failed` (two call
sites), `screener_fetch_failed`, `screener_temporarily_skipped`,
`screener_ssl_backoff_active`, `state_load_failed`, `state_save_failed`,
`state_serialize_failed`, `command_poll_failed`,
`telegram_webhook_check_failed`, `telegram_webhook_detected`,
`domain_parse_failed`, `atr_compute_failed`, `metrics_skipped` (four call
sites), `metrics_failed`, `candidate_future_failed`, `topic_weight_invalid`,
`topic_weight_unknown`, `deprecated_setting`

After editing, confirm nothing was missed:

```bash
grep -nE 'LOGGER\.(debug|info|warning|error)\(' news_bot.py
```

Expected: no output.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m unittest test_news_bot.LogRoutingTests -v`
Expected: PASS, 3 tests

Full suite: `.venv/bin/python -m unittest -q`
Expected: OK, 199 tests

The two pre-existing `assertLogs(news_bot.LOGGER, ...)` tests must still pass
unchanged — `news_bot.sys` records propagate to `news_bot`. If either fails,
the logger names are wrong, not the tests.

- [ ] **Step 5: Commit**

```bash
git add news_bot.py test_news_bot.py
git commit -m "refactor: route each log call to its band

Ten business events to news_bot.app, twenty-four plumbing events to
news_bot.sys. Message text, arguments and levels are unchanged -- only the
logger object moves.

ranker_failed is APP rather than SYS because it changes what the reader
receives: it is the signal that headlines were ranked by the heuristic
instead of the model.

A test greps the source to assert no call site uses the bare parent logger,
so a new emitter cannot silently default into a band."
```

---

### Task 5: Documentation and log rotation

**Files:**
- Modify: `README.md` — new subsection under Environment Variables
- Modify: `.env.example` — three new settings
- Modify: `docker-compose.yml` — three env vars plus a `logging:` block on `news-notifier`

**Interfaces:**
- Consumes: the three settings from Task 3
- Produces: no code symbols

- [ ] **Step 1: Add the settings to `.env.example`**

Append:

```bash
# Logging.
# LOG_LEVEL controls the bot's own APP and SYS bands.
# LOG_LEVEL_LIBRARIES controls third-party output (APScheduler, yfinance,
# urllib3) separately, so LOG_LEVEL=DEBUG does not make urllib3 emit a line
# per socket.
LOG_LEVEL=INFO
LOG_LEVEL_LIBRARIES=WARNING
# ANSI colour in container logs. Defaults on -- a container has no TTY, so
# TTY detection would disable colour exactly where it is wanted.
LOG_COLOR=true
```

- [ ] **Step 2: Add the env vars and rotation to `docker-compose.yml`**

In the `news-notifier` service's `environment:` list, after the `TZ` line:

```yaml
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - LOG_LEVEL_LIBRARIES=${LOG_LEVEL_LIBRARIES:-WARNING}
      - LOG_COLOR=${LOG_COLOR:-true}
```

At the same indent level as `environment:` on `news-notifier`:

```yaml
    # json-file with no options grows without limit, and DEBUG accelerates it.
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

- [ ] **Step 3: Document in `README.md`**

Add a `### Logging` subsection immediately before the `### News ranking`
heading:

````markdown
### Logging

Every line is tagged with one of three bands, so application output is
distinguishable from infrastructure noise at a glance:

```
2026-08-14 19:12:03  APP    INFO   briefing_start at=2026-08-14T19:12:03
2026-08-14 19:12:05  SYS    WARN   rss_fetch_failed feed=https://... detail=...
2026-08-14 19:12:06  INFRA  INFO   apscheduler.scheduler | Scheduler started
```

- **APP** — what the bot exists to do: briefings composed, headlines
  selected, messages sent, commands received.
- **SYS** — the bot's own plumbing: retries, feed and screener failures,
  state file I/O, cache backoff.
- **INFRA** — third-party libraries (APScheduler, yfinance, urllib3),
  carrying their originating logger name.

Bands are colour-coded — APP bold cyan, SYS yellow, INFRA dimmed — and
severity colours the level tag independently, so an error stays obvious
without losing its band. The band tag is also written as text, so the
distinction survives a viewer that does not render ANSI, and
`docker logs > file`.

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | APP and SYS bands |
| `LOG_LEVEL_LIBRARIES` | `WARNING` | INFRA band |
| `LOG_COLOR` | `true` | ANSI escapes on/off |

The two levels are separate because `LOG_LEVEL=DEBUG` would otherwise make
urllib3 emit a line per socket. Setting `LOG_LEVEL=DEBUG` reveals per-symbol
detail that is otherwise computed and discarded — including the reason behind
each symbol counted in the trade screen's `no data` total:

```
SYS    DEBUG  metrics_skipped symbol=AAPL reason=insufficient_history bars=12
```

`LOG_COLOR` defaults to on rather than to TTY detection: a container has no
TTY, so the usual `isatty()` check would disable colour exactly where it is
wanted. Set `LOG_COLOR=false` when piping logs to a file or a parser.

Container logs are capped at 3 files of 10 MB by the `logging:` block in
`docker-compose.yml`. Applying a change to it needs a redeploy, not just a
restart.
````

- [ ] **Step 4: Verify the whole suite and the compose file**

```bash
.venv/bin/python -m unittest -q
```
Expected: OK, 199 tests

```bash
docker compose config >/dev/null && echo "compose valid"
```
Expected: `compose valid`

- [ ] **Step 5: Commit**

```bash
git add README.md .env.example docker-compose.yml
git commit -m "docs: document the log bands and cap container log size

Records what LOG_LEVEL, LOG_LEVEL_LIBRARIES and LOG_COLOR do, why the two
levels are separate, and why LOG_COLOR does not use TTY detection.

Also caps container logs at 3 files of 10 MB. json-file with no options grows
without limit, and enabling DEBUG accelerates it."
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Logger topology, three bands | 1 |
| Band resolution table, bare `news_bot` → SYSTEM | 1 |
| Output format, widths, no milliseconds | 1 |
| INFRA carries logger name; `event=` removed | 1 |
| Colour per band; severity independent; body uncoloured | 2 |
| Strip-ANSI-equals-plain invariant | 2 |
| `LOG_LEVEL` / `LOG_LEVEL_LIBRARIES` / `LOG_COLOR` | 3 |
| Library floor via root level | 3 |
| Invalid level falls back and warns | 3 |
| Idempotent installation | 3 |
| stdout not stderr | 3 |
| Band assignment for 38 call sites | 4 |
| Log rotation | 5 |
| README / `.env.example` documentation | 5 |

**Deliberately not a task:** whether Portainer EE renders ANSI. No test can
establish it; verify on screen after deploy, which is why the textual band
marker exists.

**Type consistency:** `_band_for` returns the `BAND_*` string constants used
by `BandedFormatter` and asserted in tests. `_resolve_level` returns
`Tuple[int, Optional[str]]` in Task 3's definition, its tests, and both call
sites in `_configure_logging`. `BandedFormatter(colour=...)` uses the same
keyword in Tasks 1, 2, 3 and every test.

**Test counts:** 174 existing + 9 (T1) + 6 (T2) + 7 (T3) + 3 (T4) + 0 (T5) =
199.
