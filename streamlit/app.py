"""Fjord Signals, Streamlit edition.

The same pages as the Evidence dashboard, plus a what-if tool for the screen's
five filters. Reads the bot's SQLite DB straight from the volume, read-only,
with short queries cached for a minute, so pages are at most a minute behind.
"""

import os
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

DB_FILE = os.environ.get("DASHBOARD_DB_FILE", "/bot-data/.news_bot_trade_history.db")

GATE_LABELS = {
    "trend": "Below its 20-day average",
    "momentum_5d": "Not up 1% over 5 days",
    "momentum_1d": "Down today",
    "volatility": "Swings too much",
    "drawdown": "Too far below its high",
    "stale": "Price data is old",
}

st.set_page_config(page_title="Fjord Signals", page_icon="📈", layout="wide")


@st.cache_data(ttl=60, show_spinner=False)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    # mode=ro: this can never write. The bot does not use WAL, so a read holds a
    # shared lock that delays the bot's commits; keep every query short.
    with sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=5) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def reason(df: pd.DataFrame, gate_col: str = "failed_gate") -> pd.Series:
    return df[gate_col].map(GATE_LABELS).fillna(df[gate_col]).where(df["qualified"] == 0, "Picked")


def signals() -> pd.DataFrame:
    df = query(
        """
        SELECT symbol, signal_session, horizon_sessions, qualified, failed_gate,
               last_close, day_change_pct, week_momentum_pct, volume_ratio,
               atr_pct, drawdown_pct,
               json_extract(outcome_json, '$.net_return_pct') AS net_return_pct,
               json_extract(outcome_json, '$.excess_return_pct') AS excess_return_pct
        FROM signal_archive
        """
    )
    df["day"] = pd.to_datetime(df["signal_session"])
    df["reason"] = reason(df)
    return df


def daily(df: pd.DataFrame, picked_col: str = "qualified") -> pd.DataFrame:
    """One row per day: average result of picked vs skipped stocks."""
    done = df[df["net_return_pct"].notna()]
    out = done.groupby(["day", "horizon_sessions"]).apply(
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


def metric_row(card: dict) -> None:
    cols = st.columns(4)
    cols[0].metric("Picks: average %", f"{card['picks']:.2f}")
    cols[1].metric("Skipped: average %", f"{card['skipped']:.2f}")
    cols[2].metric("Difference %", f"{card['picks'] - card['skipped']:.2f}")
    cols[3].metric("Days picks did better", f"{card['better']} of {card['compared']}")


# --- pages -------------------------------------------------------------------


def home() -> None:
    st.title("Fjord Signals")
    st.write(
        "Every weekday at 07:00 (Oslo time) the bot checks 20-40 stocks. A stock that "
        "passes all 5 filters becomes a **pick**. When its hold is over, the bot works out "
        "what it actually did after fees. **If picks do not beat the skipped stocks, the "
        "filters are not helping.**"
    )
    days = daily(signals())
    for hold, d in days.groupby("horizon_sessions"):
        st.subheader(f"Scorecard, {int(hold)}-day hold")
        metric_row(scorecard(d))
    both = days.dropna(subset=["picks", "skipped"]).sort_values("day").copy()
    both["running"] = (both["picks"] - both["skipped"]).groupby(both["horizon_sessions"]).cumsum()
    both["hold"] = both["horizon_sessions"].astype(int).astype(str) + "-day hold"
    fig = px.line(both, x="day", y="running", color="hold", markers=True,
                  labels={"running": "%", "day": ""},
                  title="Picks minus skipped, added up day by day (%)")
    st.plotly_chart(fig, use_container_width=True)
    st.caption('"After fees" assumes 0.10% per buy and per sell. Real DNB fees are higher '
               "on small trades (59 NOK minimum). Data is at most a minute old.")


def latest_screen() -> None:
    df = query(
        "SELECT * FROM trade_metrics_history "
        "WHERE session_date = (SELECT MAX(session_date) FROM trade_metrics_history)"
    )
    st.title("Latest screen")
    if df.empty:
        st.info("No screen yet.")
        return
    df["reason"] = reason(df)
    st.write(f"Every stock the bot checked on **{df['session_date'].iloc[0]}**.")
    counts = df["reason"].value_counts().reset_index()
    st.plotly_chart(px.bar(counts, x="count", y="reason", orientation="h",
                           labels={"count": "Stocks", "reason": ""},
                           title="Picked or skipped, and why"), use_container_width=True)
    view = df.sort_values(["qualified", "week_momentum_pct"], ascending=False)[
        ["symbol", "reason", "last_close", "day_change_pct", "week_momentum_pct",
         "atr_pct", "drawdown_pct", "volume_ratio"]]
    st.dataframe(view.round(2), hide_index=True, use_container_width=True, column_config={
        "symbol": "Stock", "reason": "Result", "last_close": "Price",
        "day_change_pct": "Today %", "week_momentum_pct": "Last 5 days %",
        "atr_pct": "Normal daily swing %", "drawdown_pct": "Below 20-day high %",
        "volume_ratio": "Volume vs normal (x)"})


def day_by_day() -> None:
    st.title("Day by day")
    st.write("Each row is one day's screen. Returns are after fees; the newest days are "
             "still waiting for their hold to end.")
    df = signals()
    days = daily(df).sort_values("day", ascending=False)
    long = days.melt(id_vars="day", value_vars=["picks", "skipped"], var_name="group",
                     value_name="average")
    st.plotly_chart(px.bar(long, x="day", y="average", color="group", barmode="group",
                           labels={"average": "%", "day": ""},
                           title="Average result after fees, picks vs skipped (%)"),
                    use_container_width=True)
    counts = df.groupby("day").agg(picks=("qualified", "sum"), checked=("symbol", "count"),
                                   with_result=("net_return_pct", "count"))
    table = counts.join(days.set_index("day")[["picks", "skipped"]].rename(
        columns={"picks": "picks_avg", "skipped": "skipped_avg"})).sort_index(ascending=False)
    table.index = table.index.date
    st.dataframe(table.round(2), use_container_width=True, column_config={
        "picks": "Picks", "checked": "Stocks checked", "with_result": "With result",
        "picks_avg": "Picks: average %", "skipped_avg": "Skipped: average %"})


def sent_picks() -> None:
    st.title("Sent picks")
    df = query(
        """
        SELECT substr(published_at, 1, 10) AS sent_on, symbol, horizon_sessions,
               benchmark_symbol,
               json_extract(outcome_json, '$.net_return_pct') AS net_return_pct,
               json_extract(outcome_json, '$.excess_return_pct') AS excess_return_pct
        FROM published_picks ORDER BY published_at DESC, symbol
        """
    )
    done = df.dropna(subset=["net_return_pct"])
    cols = st.columns(5)
    cols[0].metric("Picks sent", len(df))
    cols[1].metric("With result", len(done))
    cols[2].metric("Average after fees %", f"{done['net_return_pct'].mean():.2f}")
    cols[3].metric("Went up", f"{(done['net_return_pct'] > 0).mean():.0%}")
    cols[4].metric("Beat the index", f"{(done['excess_return_pct'] > 0).mean():.0%}")
    st.plotly_chart(px.histogram(done, x="net_return_pct", nbins=40,
                                 labels={"net_return_pct": "Result after fees %"},
                                 title="How the finished picks did"),
                    use_container_width=True)
    df["status"] = df["net_return_pct"].map(lambda v: "done" if pd.notna(v) else "waiting")
    st.dataframe(df.round(2), hide_index=True, use_container_width=True, column_config={
        "sent_on": "Sent on", "symbol": "Stock", "horizon_sessions": "Hold (days)",
        "benchmark_symbol": "Index", "net_return_pct": "Result after fees %",
        "excess_return_pct": "Better than index by %", "status": "Status"})


def one_stock() -> None:
    st.title("One stock")
    df = signals()
    symbols = sorted(df["symbol"].unique())
    symbol = st.selectbox("Stock", symbols,
                          index=symbols.index("EQNR.OL") if "EQNR.OL" in symbols else 0)
    rows = df[df["symbol"] == symbol].sort_values("day")
    st.plotly_chart(px.line(rows, x="day", y="last_close", markers=True, hover_data=["reason"],
                            labels={"last_close": "Price", "day": ""},
                            title="Price on each screen day"), use_container_width=True)
    view = rows.sort_values("day", ascending=False)[
        ["signal_session", "reason", "last_close", "day_change_pct", "week_momentum_pct",
         "net_return_pct"]]
    st.dataframe(view.round(2), hide_index=True, use_container_width=True, column_config={
        "signal_session": "Day", "reason": "Result", "last_close": "Price",
        "day_change_pct": "Today %", "week_momentum_pct": "Last 5 days %",
        "net_return_pct": "At the end of the hold, after fees %"})


def data_collected() -> None:
    st.title("Data collected")
    st.write("A gap in the dates means the bot did not run that day; weekends and holidays "
             "are normal gaps.")
    df = query(
        "SELECT signal_session AS day, COUNT(*) AS checked, SUM(qualified) AS picked, "
        "SUM(outcome_json IS NOT NULL) AS with_result FROM signal_archive "
        "GROUP BY signal_session ORDER BY signal_session DESC"
    )
    st.plotly_chart(px.bar(df, x="day", y=["checked", "picked"], barmode="group",
                           labels={"value": "Stocks", "day": "", "variable": ""},
                           title="Stocks checked and picked per day"),
                    use_container_width=True)
    st.dataframe(df, hide_index=True, use_container_width=True)


def my_money() -> None:
    st.title("My money")
    trades = query("SELECT * FROM real_trades ORDER BY id DESC")
    if trades.empty:
        st.info("No trades saved yet. Send /fill in Telegram after a real trade.")
        return
    t = trades.assign(
        signed=lambda d: d["shares"].where(d["side"] == "BUY", -d["shares"]),
        cost=lambda d: (d["shares"] * d["price"] * d["fx_to_nok"] + d["fee_nok"]).where(d["side"] == "BUY", 0),
        back=lambda d: (d["shares"] * d["price"] * d["fx_to_nok"] - d["fee_nok"]).where(d["side"] == "SELL", 0),
    )
    hold = t.groupby("symbol").agg(shares_held=("signed", "sum"), paid_nok=("cost", "sum"),
                                   got_back_nok=("back", "sum"), fees_nok=("fee_nok", "sum"))
    hold["result"] = (hold["got_back_nok"] - hold["paid_nok"]).map(
        lambda v: f"closed: {v:+.0f} NOK").where(hold["shares_held"] == 0, "still holding")
    st.subheader("What I hold")
    st.dataframe(hold.round(0), use_container_width=True)
    st.subheader("My trades")
    st.dataframe(trades[["trade_date", "side", "symbol", "shares", "price", "currency",
                         "fx_to_nok", "fee_nok"]], hide_index=True, use_container_width=True)


def what_if() -> None:
    st.title("What-if filters")
    st.write(
        "Move the filters and see how the picks would have done against the stocks they "
        "skip, on every screen with a result so far. **This is in-sample**: tuning "
        "thresholds to past days finds noise easily, and the earlier study found no "
        "threshold set that held up out of sample. Use it to understand the filters, "
        "not to pick new ones."
    )
    df = signals()
    df = df[df["net_return_pct"].notna() & (df["failed_gate"].fillna("") != "stale")].copy()
    holds = sorted(df["horizon_sessions"].unique())
    hold = st.radio("Hold (trading days)", holds, horizontal=True,
                    index=holds.index(5) if 5 in holds else 0)
    df = df[df["horizon_sessions"] == hold]

    c1, c2, c3 = st.columns(3)
    trend = c1.checkbox("Must be above its 20-day average", value=True)
    m5 = c1.slider("Min change over 5 days %", -5.0, 10.0, 1.0, 0.5)
    m1 = c2.slider("Min change today %", -5.0, 5.0, 0.0, 0.25)
    atr = c2.slider("Max normal daily swing %", 1.0, 15.0, 4.5, 0.5)
    dd = c3.slider("Max below 20-day high %", 0.0, 30.0, 8.0, 1.0)
    vol = c3.slider("Min volume vs normal (x, 0 = off)", 0.0, 3.0, 0.0, 0.1)

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
    df["what_if"] = picked.astype(int)

    now, new = daily(df, "qualified"), daily(df, "what_if")
    st.subheader("The bot's filters today")
    metric_row(scorecard(now))
    st.subheader("Your filters")
    metric_row(scorecard(new))
    st.caption(f"Your filters pick {int(df['what_if'].sum())} of {len(df)} stock-days; "
               f"the bot's picked {int(df['qualified'].sum())}.")

    lines = []
    for name, d in (("Bot's filters", now), ("Your filters", new)):
        d = d.dropna(subset=["picks", "skipped"]).sort_values("day").copy()
        d["running"] = (d["picks"] - d["skipped"]).cumsum()
        d["set"] = name
        lines.append(d)
    fig = px.line(pd.concat(lines), x="day", y="running", color="set", markers=True,
                  labels={"running": "%", "day": "", "set": ""},
                  title="Picks minus skipped, added up day by day (%)")
    st.plotly_chart(fig, use_container_width=True)


pages = [
    st.Page(home, title="Home", icon="🏠", default=True),
    st.Page(latest_screen, title="Latest screen", icon="🔎", url_path="latest-screen"),
    st.Page(day_by_day, title="Day by day", icon="📅", url_path="day-by-day"),
    st.Page(sent_picks, title="Sent picks", icon="📨", url_path="sent-picks"),
    st.Page(one_stock, title="One stock", icon="📈", url_path="one-stock"),
    st.Page(data_collected, title="Data collected", icon="🗂️", url_path="data-collected"),
    st.Page(my_money, title="My money", icon="💰", url_path="my-money"),
    st.Page(what_if, title="What-if filters", icon="🎛️", url_path="what-if"),
]
st.navigation(pages).run()
