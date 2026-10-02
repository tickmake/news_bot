"""Candidate ordering that combines today's metrics with recorded history."""

from typing import Optional, Tuple

from newsbot import history_store
from newsbot.config import SETTINGS
from newsbot.indicators import TradeMetrics


def _rank_key(
    metrics: TradeMetrics, qualifying_streak: Optional[int] = None
) -> Tuple[bool, bool, float, float, int, float, str]:
    """Ordering for qualifying candidates: momentum, then confirmation,
    then consistency, then calm.

    An ordinal rule rather than a weighted composite keeps the ordering
    explainable; `/performance` now provides a historical validation path
    before changing these priorities. Symbol sorts last so identical metrics
    always produce the same output.

    The qualifying streak (how many recent sessions running, today included,
    this symbol has passed every gate) sits after volume and before ATR: it
    is a read of the bot's own history for this symbol, one tier weaker a
    signal than today's directly measured volume confirmation, but still
    more informative than volatility alone for breaking a tie.
    """
    # Two quality tiers ahead of momentum, both of them booleans rather than
    # gates: they reorder the list, they never shorten it.
    #
    # RSI was measured and printed but read by nothing, so a name at RSI 85 --
    # extended, and the worst moment to open a swing position -- sorted above
    # a healthy pullback purely on momentum, which is exactly the number a
    # blow-off top maximises. Volume had the same problem in reverse: the
    # docstring said it "ranks rather than admits", but as a raw ratio in the
    # second slot it only ever broke exact momentum ties, so an unconfirmed
    # move still outranked a confirmed one whenever momentum differed at all.
    # Both now sort *down* rather than out, which keeps every currently
    # qualifying symbol qualifying.
    overbought = metrics.rsi_14 is not None and metrics.rsi_14 >= 70
    unconfirmed = (
        metrics.volume_ratio is None
        or metrics.volume_ratio < SETTINGS.trade_min_volume_ratio
    )
    streak = (
        history_store.TRADE_HISTORY.recent_qualifying_streak(metrics.symbol)
        if qualifying_streak is None
        else qualifying_streak
    )
    return (
        overbought,
        unconfirmed,
        -metrics.week_momentum_pct,
        -(metrics.volume_ratio if metrics.volume_ratio is not None else 0.0),
        -streak,
        metrics.atr_pct,
        metrics.symbol,
    )
