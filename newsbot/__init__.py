"""Telegram news and market-briefing bot.

Importing the package builds the settings and configures logging once, before
any other module logs. Run it with `python news_bot.py`.
"""

from newsbot import config as _config  # noqa: F401  (builds SETTINGS)
from newsbot import logsetup as _logsetup

_logsetup._configure_logging()
