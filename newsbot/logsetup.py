"""Banded INFRA/SYSTEM/APP logging."""

import logging
import sys
from datetime import datetime
from typing import Dict, Optional, Tuple

from newsbot.config import SETTINGS


LOGGER = logging.getLogger("news_bot")
# Band is carried by logger name: BandedFormatter reads record.name. The
# hierarchy is load-bearing -- existing tests assertLogs against `news_bot`
# and keep working because child records propagate to it.
APP_LOG = logging.getLogger("news_bot.app")
SYS_LOG = logging.getLogger("news_bot.sys")

BAND_APP = "APP"
BAND_SYS = "SYS"
BAND_INFRA = "INFRA"

_LEVEL_TAG_THRESHOLDS: Tuple[Tuple[int, str], ...] = (
    (logging.DEBUG, "DEBUG"),
    (logging.INFO, "INFO"),
    (logging.WARNING, "WARN"),
    (logging.ERROR, "ERROR"),
)


def _tag_for_level(levelno: int) -> str:
    """The standard-level tag for a numeric level, via a threshold ladder.

    Mirrors the boundaries the colour logic in BandedFormatter.format uses
    (>= ERROR, == WARNING), so a non-standard level (e.g. a custom level 45
    between ERROR=40 and CRITICAL=50) gets the tag of the nearest standard
    level at or below it -- "ERROR" here -- rather than a fixed fallback that
    could contradict the colour it's rendered in. Levels below DEBUG=10 floor
    at "DEBUG", the lowest tag available.
    """
    tag = "DEBUG"
    for threshold, name in _LEVEL_TAG_THRESHOLDS:
        if levelno >= threshold:
            tag = name
    return tag


def _band_for(logger_name: str) -> str:
    """Which band a record belongs to, from its logger name."""
    if logger_name == "news_bot.app" or logger_name.startswith("news_bot.app."):
        return BAND_APP
    if logger_name == "news_bot" or logger_name.startswith("news_bot."):
        return BAND_SYS
    return BAND_INFRA


_ANSI: Dict[str, str] = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "app": "\033[1;36m",    # bold cyan -- the lines you came to read
    "sys": "\033[33m",      # yellow
    "warn": "\033[33m",
    "error": "\033[1;31m",  # bold red
}


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
        level = _tag_for_level(record.levelno)
        # Milliseconds cost four columns on every line and nothing here is
        # measured at that resolution; durations are logged as latency_ms=.
        stamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")

        message = record.getMessage()
        if band == BAND_INFRA:
            # "Scheduler started" says nothing about who said it.
            message = f"{record.name} | {message}"
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"

        band_text = f"{band:<5}"
        level_text = f"{level:<5}"
        if self.colour:
            if band == BAND_APP:
                band_text = f"{_ANSI['app']}{band_text}{_ANSI['reset']}"
            elif band == BAND_SYS:
                band_text = f"{_ANSI['sys']}{band_text}{_ANSI['reset']}"
            # Severity colours the level tag independently, so an ERROR stays
            # obvious without losing its band. On INFRA lines, close severity
            # spans with reset+re-dim to prevent colour bleeding into the message.
            if record.levelno >= logging.ERROR:
                closer = _ANSI['reset'] + _ANSI['dim'] if band == BAND_INFRA else _ANSI['reset']
                level_text = f"{_ANSI['error']}{level_text}{closer}"
            elif record.levelno == logging.WARNING:
                closer = _ANSI['reset'] + _ANSI['dim'] if band == BAND_INFRA else _ANSI['reset']
                level_text = f"{_ANSI['warn']}{level_text}{closer}"

        line = f"{stamp}  {band_text}  {level_text}  {message}"
        if self.colour and band == BAND_INFRA:
            # INFRA recedes rather than competing for attention.
            line = f"{_ANSI['dim']}{line}{_ANSI['reset']}"
        return line

_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LOGGING_CONFIGURED = False


def _resolve_level(raw: str, fallback: int = logging.INFO) -> Tuple[int, Optional[str]]:
    """Level number, plus the rejected value when the name was not recognised.

    logging.getLevelName("BOGUS") returns the *string* "Level BOGUS" rather
    than raising, so a typo'd level would sail through and configure nonsense.
    """
    text = (raw or "").strip().upper()
    if not text:
        # Unset/blank (e.g. `LOG_LEVEL=` with nothing after the `=`) means
        # "use the default", not "a typo" -- do not warn about it.
        return fallback, None
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
