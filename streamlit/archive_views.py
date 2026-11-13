"""Which forward-test rows a page may show, and the sums built on them.

No Streamlit import, so every aggregation here is unit-tested
(tests/test_dashboard_views.py). The pages in app.py only draw.

The bot defines the rows in one database view, `signal_archive_evaluated`,
which this dashboard and the Telegram report both read:

  series    clean_test   recorded since the entry fix: entry is the first open
                         after the signal was recorded
            historical   recorded before it, when a signal could be scored
                         from an open that had already happened
  quality   clean / potentially_contaminated / invalid / awaiting_entry

Only clean, finished rows of one series may be averaged. Classes and series
are never pooled: `daily` refuses a frame that mixes them.
"""

import json
import re
from typing import Dict, Optional

import pandas as pd

# The only query against the signal archive in the dashboard.
SIGNALS_SQL = """
    SELECT symbol, signal_session, recorded_at, rule_id, horizon_sessions, qualified,
           failed_gate, last_close, day_change_pct, week_momentum_pct, volume_ratio,
           atr_pct, drawdown_pct, entry_policy, series, classifier_version, quality,
           quality_reason, settled, entry_session, net_return_pct, excess_return_pct
    FROM signal_archive_evaluated
"""
# Which classifier version is in force. No row means none is, and nothing is shown.
POINTER_SQL = "SELECT version FROM signal_archive_quality_active"

CLEAN_TEST = "clean_test"
HISTORICAL = "historical"
SERIES_LABELS = {
    CLEAN_TEST: "Clean test (since the entry fix)",
    HISTORICAL: "Historical (before the entry fix)",
}

CLEAN = "clean"
POTENTIALLY_CONTAMINATED = "potentially_contaminated"
INVALID = "invalid"
AWAITING_ENTRY = "awaiting_entry"
CLASSES = [CLEAN, POTENTIALLY_CONTAMINATED, INVALID]
QUALITY_LABELS = {
    CLEAN: "Clean",
    POTENTIALLY_CONTAMINATED: "Potentially contaminated",
    INVALID: "Invalid",
    AWAITING_ENTRY: "Awaiting entry",
}

# An unclassified row older than this is reported as overdue (same as the bot).
OVERDUE_AFTER_DAYS = 7


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Add the two dates a page plots by.

    `day` is the session a row entered on, which is what performance is
    grouped by: two signals with the same date can enter on different days.
    `signal_day` is the date the signal carries, for the collection log.
    """
    df = df.copy()
    df["signal_day"] = pd.to_datetime(df["signal_session"])
    df["day"] = pd.to_datetime(df["entry_session"])
    return df


# Since P1-2 a new rule id ends in its hold ("...-vol1.2-h20"); a row archived
# before then carries the gate thresholds alone and its hold only in
# `horizon_sessions`. Pages choose by the gates and split by `horizon_sessions`,
# so an old row stays in the group it always had and the clean test is not cut
# in two at the change.
_HOLD_SUFFIX = re.compile(r"-h\d+$")


def filter_settings(rule_ids: pd.Series) -> pd.Series:
    """The gate-threshold part of each rule id: what a page's rule choice is made by."""
    return rule_ids.map(lambda rule_id: _HOLD_SUFFIX.sub("", rule_id))


def selection(df: pd.DataFrame, series: str, quality: str = CLEAN) -> pd.DataFrame:
    """Finished rows of one series and one class: the only input `daily` accepts."""
    return df[(df["series"] == series) & (df["quality"] == quality) & (df["settled"] == 1)].copy()


def _require_one_selection(df: pd.DataFrame) -> None:
    if df.empty:
        return
    if df["series"].nunique() > 1 or df["quality"].nunique() > 1:
        raise ValueError("rows of different series or data-quality classes must not be averaged together")
    if df["quality"].iloc[0] == AWAITING_ENTRY or (df["settled"] != 1).any() or df["day"].isna().any():
        raise ValueError("only finished rows with an entry session can be averaged")


def daily(df: pd.DataFrame, picked_col: str = "qualified") -> pd.DataFrame:
    """One row per entry session and hold: average result of picked vs skipped stocks."""
    _require_one_selection(df)
    columns = ["day", "horizon_sessions", "picks", "skipped", "n_picks"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    out = df.groupby(["day", "horizon_sessions"]).apply(
        lambda g: pd.Series(
            {
                "picks": g.loc[g[picked_col] == 1, "net_return_pct"].mean(),
                "skipped": g.loc[g[picked_col] == 0, "net_return_pct"].mean(),
                "n_picks": int((g[picked_col] == 1).sum()),
            }
        ),
        include_groups=False,
    )
    return out.reset_index()


def scorecard(days: pd.DataFrame) -> dict:
    both = days.dropna(subset=["picks", "skipped"])
    return {
        "days": len(days),
        "picks": days["picks"].mean(),
        "skipped": days["skipped"].mean(),
        "better": int((both["picks"] > both["skipped"]).sum()),
        "compared": len(both),
    }


def running_difference(days: pd.DataFrame) -> pd.DataFrame:
    """Picks minus skipped, added up entry session by entry session, per hold."""
    both = days.dropna(subset=["picks", "skipped"]).sort_values("day").copy()
    if both.empty:
        return both.assign(running=pd.Series(dtype=float))
    both["running"] = (both["picks"] - both["skipped"]).groupby(both["horizon_sessions"]).cumsum()
    return both


def stock_averages(rows: pd.DataFrame) -> Dict[str, float]:
    """Average result of one stock's selected rows, when picked and when skipped."""
    _require_one_selection(rows)
    return {
        "picked": rows.loc[rows["qualified"] == 1, "net_return_pct"].mean(),
        "skipped": rows.loc[rows["qualified"] == 0, "net_return_pct"].mean(),
    }


def what_if_picks(df: pd.DataFrame, trend: bool, m5: float, m1: float, atr: float,
                  dd: float, vol: float) -> pd.Series:
    """1 where a row would pass the given filters, else 0."""
    # The screen checks the trend filter first, so failed_gate == 'trend' is the
    # only way to tell a stock was below its 20-day average.
    above = df["failed_gate"].fillna("") != "trend"
    picked = (
        (above | (not trend))
        & (df["week_momentum_pct"] >= m5)
        & (df["day_change_pct"] >= m1)
        & (df["atr_pct"] <= atr)
        & (df["drawdown_pct"] <= dd)
        & ((vol == 0) | (df["volume_ratio"].fillna(0) >= vol))
    )
    return picked.astype(int)


def overdue(df: pd.DataFrame, now: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Rows still without an entry session long after any exchange would have opened."""
    now = now or pd.Timestamp.now(tz="UTC")
    waiting = df[df["quality"] == AWAITING_ENTRY]
    recorded = pd.to_datetime(waiting["recorded_at"], utc=True, errors="coerce")
    return waiting[(now - recorded) > pd.Timedelta(days=OVERDUE_AFTER_DAYS)]


def state_counts(df: pd.DataFrame, now: Optional[pd.Timestamp] = None) -> Dict[str, int]:
    """Where every row stands, so a row left out of the averages is never left out of view."""
    clean = df[df["quality"] == CLEAN]
    return {
        "clean_finished": int((clean["settled"] == 1).sum()),
        "clean_in_hold": int((clean["settled"] != 1).sum()),
        "potentially_contaminated": int((df["quality"] == POTENTIALLY_CONTAMINATED).sum()),
        "invalid": int((df["quality"] == INVALID).sum()),
        "awaiting_entry": int((df["quality"] == AWAITING_ENTRY).sum()),
        "overdue": len(overdue(df, now)),
    }


def invalid_reasons(df: pd.DataFrame) -> pd.DataFrame:
    """Invalid rows counted by reason code."""
    bad = df[df["quality"] == INVALID]
    return (bad.groupby("quality_reason").size().rename("rows").reset_index()
            .sort_values("rows", ascending=False))


def collected(df: pd.DataFrame, now: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """The collection log: one row per signal date, with every row placed in one class."""
    late = set(overdue(df, now).index)
    work = df.assign(
        clean=(df["quality"] == CLEAN).astype(int),
        potentially_contaminated=(df["quality"] == POTENTIALLY_CONTAMINATED).astype(int),
        invalid=(df["quality"] == INVALID).astype(int),
        awaiting_entry=(df["quality"] == AWAITING_ENTRY).astype(int),
        overdue=[int(index in late) for index in df.index],
    )
    out = work.groupby("signal_session").agg(
        checked=("symbol", "count"), picked=("qualified", "sum"), with_result=("settled", "sum"),
        clean=("clean", "sum"), potentially_contaminated=("potentially_contaminated", "sum"),
        invalid=("invalid", "sum"), awaiting_entry=("awaiting_entry", "sum"),
        overdue=("overdue", "sum"),
    )
    return out.reset_index().rename(columns={"signal_session": "day"}).sort_values("day", ascending=False)


def by_entry_session(scope: pd.DataFrame, chosen: pd.DataFrame) -> pd.DataFrame:
    """The day-by-day table for one hold: counts of every class per entry session, averages from `chosen` only."""
    entered = scope[scope["day"].notna()]
    counts = entered.assign(
        clean=(entered["quality"] == CLEAN).astype(int),
        potentially_contaminated=(entered["quality"] == POTENTIALLY_CONTAMINATED).astype(int),
        invalid=(entered["quality"] == INVALID).astype(int),
    ).groupby("day").agg(
        checked=("symbol", "count"), picks=("qualified", "sum"), clean=("clean", "sum"),
        potentially_contaminated=("potentially_contaminated", "sum"), invalid=("invalid", "sum"),
    )
    if scope["horizon_sessions"].nunique() > 1 or chosen["horizon_sessions"].nunique() > 1:
        raise ValueError("different hold lengths must not be averaged together")
    averages = daily(chosen).set_index("day")[["picks", "skipped"]].rename(
        columns={"picks": "picks_avg", "skipped": "skipped_avg"})
    return counts.join(averages).sort_index(ascending=False)


# --- risk of the picks that were sent ----------------------------------------

# The bot saves the risk lines of a sent pick in `snapshot_json` under "risk"
# (newsbot/risk.py, `snapshot`). This page shows that record and never works
# one out: a pick sent before the record existed has none, and stays that way.
PICK_RISK_SQL = """
    SELECT substr(published_at, 1, 10) AS sent_on, symbol, signal_session, snapshot_json
    FROM published_picks ORDER BY published_at DESC, symbol
"""
# The record layout this page knows (risk.RISK_MODEL_VERSION; a test keeps the
# two equal). A record of any other version is listed but its numbers are not
# shown, since its fields may mean something else.
RISK_MODEL_VERSION = 2
RISK_RECORDED = "recorded when sent"
RISK_NOT_RECORDED = "not recorded"
# Nobody has bought when a pick is sent, so no record carries a buy price.
ACTUAL_RISK_UNKNOWN = "not known: no buy price is linked to a sent pick"
RISK_FIELDS = [
    "reference_price", "stop_price", "stop_distance_price", "stop_distance_pct", "atr_pct",
    "atr_multiple", "exposure_nok", "exposure_pct", "stop_loss_nok", "stop_loss_pct",
    "gap_fill_price", "gap_loss_nok", "gap_loss_pct", "fee_nok", "fee_pct",
]
# The record column comes early: a row of blanks must say why before anyone scrolls.
RISK_COLUMNS = ["sent_on", "symbol", "record", "signal_session", *RISK_FIELDS, "gap", "actual",
                "costs", "warnings"]


def _gap_label(risk: dict) -> str:
    if not risk.get("gap_measured"):
        return f"not measured for {risk.get('atr_multiple'):g}×ATR"
    return f"estimate: sold {risk.get('gap_below_stop_pct'):.2f}% below the stop (nine in ten did better)"


def _costs_label(risk: dict) -> str:
    if risk.get("fee_nok") is None:
        return "no amount, so no fee estimate"
    fx = "" if risk.get("fx_cost_included") is None else "; currency exchange not included"
    return f"estimated broker fee{fx}"


def pick_risk(df: pd.DataFrame) -> pd.DataFrame:
    """One row per sent pick with the risk record it was sent with, or why there is none."""
    rows = []
    for row in df.itertuples(index=False):
        try:
            risk = json.loads(row.snapshot_json).get("risk")
        except (TypeError, ValueError, AttributeError):
            risk = None
        out = {"sent_on": row.sent_on, "symbol": row.symbol, "signal_session": row.signal_session}
        if not isinstance(risk, dict):
            out.update(record=RISK_NOT_RECORDED)
        elif risk.get("model_version") != RISK_MODEL_VERSION:
            out.update(record=f"risk model v{risk.get('model_version')}: not shown")
        else:
            out.update({name: risk.get(name) for name in RISK_FIELDS})
            out.update(gap=_gap_label(risk), costs=_costs_label(risk),
                       actual=ACTUAL_RISK_UNKNOWN if risk.get("entry_price") is None else None,
                       warnings=" ".join(risk.get("warnings") or []), record=RISK_RECORDED)
        rows.append(out)
    return pd.DataFrame(rows, columns=RISK_COLUMNS)
