"""Technical-analysis charts for /recommendations."""

import io
from typing import Any, Dict, List, Optional, Tuple

from newsbot import market_data, messaging
from newsbot.config import SETTINGS
from newsbot.indicators import TradeMetrics, _drop_in_progress_bar, _rsi_series
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.screener import ScreenOutcome


# Chart palette. Fixed dark theme rather than anything theme-aware: the image
# is uploaded as a PNG and Telegram renders it identically in both its light
# and dark clients, so one deliberate look beats guessing the reader's.
_CHART_BG = "#11161d"
_CHART_PANEL = "#171e27"
_CHART_TEXT = "#c7d1dd"
_CHART_MUTED = "#6b7a8d"
_CHART_PRICE = "#4da3ff"
_CHART_EMA = "#ffb648"
_CHART_UP = "#3fb950"
_CHART_DOWN = "#f0603f"


def _chart_history(symbol: str) -> Any:
    """Completed sessions for the chart window.

    Reuses `_fetch_ticker_history`, so this is a cache hit off the screening
    run that just produced these candidates rather than a second network
    fetch, and drops the in-progress bar for the same reason the screener
    does -- a half-formed session would draw a misleading final point.
    """
    history = _drop_in_progress_bar(market_data._fetch_ticker_history(symbol))
    if history is None or len(history) == 0:
        return None
    return history.tail(max(30, SETTINGS.trade_chart_lookback_days))


def render_candidate_charts(candidates: List[Tuple[TradeMetrics, str]]) -> Optional[bytes]:
    """One PNG covering every supplied candidate, or None if it cannot render.

    matplotlib is imported here rather than at module scope on purpose: it is
    a heavy import the briefing path never needs, and keeping it local means
    an install without it (or a broken font cache on a fresh container) loses
    the charts and nothing else -- every caller treats None as "send the text
    only".

    One figure for all candidates rather than one upload each: Telegram would
    otherwise deliver them as separate messages, which on a phone separates
    each chart from the text block describing it.
    """
    if not candidates:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")  # No display on a headless home-lab box.
        import matplotlib.pyplot as plt
        from matplotlib.gridspec import GridSpec
    except Exception as exc:
        APP_LOG.warning("chart_backend_unavailable detail=%s", exc)
        return None

    try:
        panels = []
        for metrics, name in candidates:
            history = _chart_history(metrics.symbol)
            if history is None or len(history) < 20:
                SYS_LOG.debug("chart_skipped symbol=%s reason=insufficient_history", metrics.symbol)
                continue
            panels.append((metrics, name, history))
        if not panels:
            return None

        fig = plt.figure(figsize=(9, 3.9 * len(panels)), dpi=110, facecolor=_CHART_BG)
        # Price gets three times the height of RSI: the RSI panel only has to
        # show which side of 30/70 the line is on.
        grid = GridSpec(
            len(panels) * 2, 1,
            height_ratios=[3, 1] * len(panels),
            hspace=0.38,
            figure=fig,
        )

        for index, (metrics, name, history) in enumerate(panels):
            close = history["Close"].dropna()
            ema20 = close.ewm(span=20, adjust=False).mean()
            price_ax = fig.add_subplot(grid[index * 2])
            rsi_ax = fig.add_subplot(grid[index * 2 + 1], sharex=price_ax)

            for axis in (price_ax, rsi_ax):
                axis.set_facecolor(_CHART_PANEL)
                axis.tick_params(colors=_CHART_MUTED, labelsize=8)
                for spine in axis.spines.values():
                    spine.set_color(_CHART_MUTED)
                    spine.set_linewidth(0.6)
                axis.grid(True, color=_CHART_MUTED, alpha=0.15, linewidth=0.6)

            price_ax.plot(close.index, close.values, color=_CHART_PRICE, linewidth=1.6, label="Close")
            price_ax.plot(ema20.index, ema20.values, color=_CHART_EMA, linewidth=1.2, label="EMA20")

            # The 20-session band the screener's drawdown and entry/exit
            # sketch are both measured against, drawn so the reader can see
            # where in that range the last close actually sits.
            if metrics.resistance_level:
                price_ax.axhline(metrics.resistance_level, color=_CHART_DOWN, linewidth=0.9,
                                 linestyle="--", alpha=0.75)
            if metrics.support_level:
                price_ax.axhline(metrics.support_level, color=_CHART_UP, linewidth=0.9,
                                 linestyle="--", alpha=0.75)
            if metrics.support_level and metrics.resistance_level:
                price_ax.axhspan(metrics.support_level, metrics.resistance_level,
                                 color=_CHART_PRICE, alpha=0.08)

            if "Volume" in history:
                volume = history["Volume"].fillna(0)
                volume_ax = price_ax.twinx()
                changes = close.diff().fillna(0)
                colors = [_CHART_UP if value >= 0 else _CHART_DOWN for value in changes]
                volume_ax.bar(volume.index, volume.values, color=colors, alpha=0.18, width=1.0)
                # Volume compressed into the bottom fifth so it reads as
                # context under the price line instead of competing with it.
                # Scaled off the window maximum rather than a fixed ceiling,
                # so a quiet stock and a heavily traded one look the same.
                volume_ax.set_ylim(0, float(volume.max()) * 5 if float(volume.max()) else 1)
                volume_ax.set_yticks([])
                for spine in volume_ax.spines.values():
                    spine.set_visible(False)

            price_ax.set_title(
                f"{index + 1}. {name} ({metrics.symbol})   "
                f"{metrics.last_close:,.2f}  {metrics.day_change_pct:+.2f}% today  "
                f"{metrics.week_momentum_pct:+.2f}% 5d",
                color=_CHART_TEXT, fontsize=10, loc="left", pad=8,
            )
            # "best" rather than a fixed corner: these are by definition
            # rising series, so a pinned upper-left legend sits on top of the
            # price line exactly when the candidate is strongest.
            legend = price_ax.legend(loc="best", fontsize=7, framealpha=0.0)
            for text in legend.get_texts():
                text.set_color(_CHART_MUTED)
            price_ax.tick_params(labelbottom=False)

            rsi_series = _rsi_series(close)
            if rsi_series is not None:
                rsi_ax.plot(rsi_series.index, rsi_series.values, color=_CHART_EMA, linewidth=1.2)
            rsi_ax.axhline(70, color=_CHART_DOWN, linewidth=0.8, linestyle=":", alpha=0.8)
            rsi_ax.axhline(30, color=_CHART_UP, linewidth=0.8, linestyle=":", alpha=0.8)
            rsi_ax.set_ylim(0, 100)
            rsi_ax.set_yticks([30, 70])
            rsi_ax.set_ylabel("RSI", color=_CHART_MUTED, fontsize=8)

        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", facecolor=_CHART_BG, bbox_inches="tight")
        plt.close(fig)
        return buffer.getvalue()
    except Exception as exc:
        # Never let a drawing failure take down the text the charts decorate.
        APP_LOG.warning("chart_render_failed detail=%s", exc)
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def send_candidate_charts(outcome: ScreenOutcome, labels: Dict[str, str],
                          chat_id: Optional[str] = None) -> bool:
    """Chart the top candidates and upload them to Telegram.

    Goes to both channels, but by different routes. Telegram takes a
    multipart `sendPhoto`. Slack cannot: `_slack_post` is an incoming
    webhook, and a webhook can only post text. The Web API's upload
    endpoints handle it instead -- outbound calls needing a bot token, not an
    exposed port -- so Slack gets the image too wherever one is configured,
    and falls back to the text rendering alone where one is not.

    Rendered once and sent twice: the figure is the expensive part.
    """
    if not SETTINGS.trade_chart_enabled:
        return False
    top = outcome.qualified[: max(0, SETTINGS.trade_chart_count)]
    if not top:
        return False
    image = render_candidate_charts([(m, labels.get(m.symbol, m.symbol)) for m in top])
    if image is None:
        return False
    symbols = ", ".join(m.symbol for m in top)
    caption = f"📊 {symbols} — price vs EMA20, 20-day range, volume, RSI"
    # Both are attempted regardless of the other's outcome: a Slack app that
    # was never invited to the channel must not cost Telegram its chart.
    telegram_ok = messaging.send_telegram_photo(image, caption=caption, chat_id=chat_id)
    slack_ok = messaging.send_slack_photo(image, title=f"Trade candidates — {symbols}", comment=caption)
    return telegram_ok or slack_ok
