"""Fjord Signals, Streamlit edition.

The same pages as the Evidence dashboard, plus a what-if tool for the screen's
five filters. Reads the bot's SQLite DB straight from the volume, read-only,
with short queries cached for a minute, so pages are at most a minute behind.
"""

import json
import os
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

import archive_views as views

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


def load_signals() -> tuple[pd.DataFrame, str | None]:
    """Every archived signal with its series and data-quality class, and why not if none.

    The one place the dashboard reads the signal archive, through the bot's
    `signal_archive_evaluated` view, so a page can never show a row the
    Telegram report would leave out.
    """
    try:
        df = query(views.SIGNALS_SQL)
        active = query(views.POINTER_SQL)
    except Exception:  # the view is made by the bot when it starts
        return views.prepare(pd.DataFrame(columns=_SIGNAL_COLUMNS)).assign(reason=None), (
            "The bot has not prepared the forward-test data yet. It does so when it "
            "starts; this page fills in after that.")
    df = views.prepare(df)
    df["reason"] = reason(df)
    if active.empty:
        return df.iloc[0:0], ("Classifier not active: no data-quality classification is in "
                              "force, so no result is shown. Check the bot's logs.")
    return df, None


_SIGNAL_COLUMNS = [
    "symbol", "signal_session", "recorded_at", "rule_id", "horizon_sessions", "qualified",
    "failed_gate", "last_close", "day_change_pct", "week_momentum_pct", "volume_ratio",
    "atr_pct", "drawdown_pct", "entry_policy", "series", "classifier_version", "quality",
    "quality_reason", "settled", "entry_session", "net_return_pct", "excess_return_pct",
]


def signals() -> pd.DataFrame:
    """The signals for a performance page; stops the page when they cannot be shown."""
    df, problem = load_signals()
    if problem:
        st.error(problem)
        st.stop()
    return df


def choose(df: pd.DataFrame, key: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Ask which results to show. Returns (rows that may be averaged, every row in scope).

    The clean test is the default everywhere. Historical rows are one class at
    a time; nothing here can pool classes, series or filter sets.
    """
    series = st.radio("Results", [views.CLEAN_TEST, views.HISTORICAL], horizontal=True,
                      format_func=views.SERIES_LABELS.get, key=f"{key}_series")
    quality = views.CLEAN
    if series == views.HISTORICAL:
        quality = st.radio("Data quality", views.CLASSES, horizontal=True,
                           format_func=views.QUALITY_LABELS.get, key=f"{key}_quality")
    scope = df[df["series"] == series]
    rules = sorted(scope["rule_id"].unique())
    rule = rules[-1] if rules else None
    if len(rules) > 1:
        rule = st.selectbox("Filter settings (never mixed)", rules, index=len(rules) - 1,
                            key=f"{key}_rule")
    scope = scope[scope["rule_id"] == rule]
    chosen = views.selection(scope, series, quality)
    counts = views.state_counts(scope)
    policy = ("entry is the first open after the signal was recorded (after-recorded-v1)"
              if series == views.CLEAN_TEST
              else "entry was the first session after the signal date, whenever it was recorded")
    version = int(df["classifier_version"].iloc[0]) if not df.empty else "–"
    holds = ", ".join(str(int(h)) for h in sorted(scope["horizon_sessions"].unique())) or "–"
    st.caption(
        f"**{views.SERIES_LABELS[series]}** · showing {views.QUALITY_LABELS[quality].lower()}, "
        f"finished rows only · {policy} · classifier v{version} · filter settings `{rule or '–'}` · "
        f"hold {holds} days.  \n"
        f"Rows: {counts['clean_finished']} clean and finished · {counts['clean_in_hold']} clean and "
        f"in hold · {counts['potentially_contaminated']} potentially contaminated · "
        f"{counts['invalid']} invalid · {counts['awaiting_entry']} awaiting entry · "
        f"{counts['overdue']} overdue."
    )
    if chosen.empty:
        if series == views.CLEAN_TEST and not scope.empty:
            st.info(f"The clean test started on {scope['recorded_at'].min()[:10]}. "
                    f"{len(scope)} signals are recorded; results appear when their hold ends.")
        elif series == views.CLEAN_TEST:
            st.info("No signals have been recorded since the entry fix yet.")
        else:
            st.info("No finished rows in this class.")
    return chosen, scope


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
    chosen, _scope = choose(signals(), "home")
    days = views.daily(chosen)
    for hold, d in days.groupby("horizon_sessions"):
        st.subheader(f"Scorecard, {int(hold)}-day hold")
        metric_row(views.scorecard(d))
    both = views.running_difference(days)
    if not both.empty:
        both["hold"] = both["horizon_sessions"].astype(int).astype(str) + "-day hold"
        fig = px.line(both, x="day", y="running", color="hold", markers=True,
                      labels={"running": "%", "day": "Entry session"},
                      title="Picks minus skipped, added up entry session by entry session (%)")
        st.plotly_chart(fig, width="stretch")
    st.caption('"After fees" assumes 0.10% per buy and per sell. Real DNB fees are higher '
               "on small trades (59 NOK minimum). Data is at most a minute old.")
    st.subheader("The data behind it")
    for page in DATA_PAGES:
        st.page_link(page)


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
                           title="Picked or skipped, and why"), width="stretch")
    view = df.sort_values(["qualified", "week_momentum_pct"], ascending=False)[
        ["symbol", "reason", "last_close", "day_change_pct", "week_momentum_pct",
         "atr_pct", "drawdown_pct", "volume_ratio"]]
    st.dataframe(view.round(2), hide_index=True, width="stretch", column_config={
        "symbol": "Stock", "reason": "Result", "last_close": "Price",
        "day_change_pct": "Today %", "week_momentum_pct": "Last 5 days %",
        "atr_pct": "Normal daily swing %", "drawdown_pct": "Below 20-day high %",
        "volume_ratio": "Volume vs normal (x)"})


def day_by_day() -> None:
    st.title("Day by day")
    st.write("Each row is one entry session: the day the stocks would have been bought. "
             "Returns are after fees. A signal counts on the day it enters, not the day it "
             "is dated, because two signals with the same date can enter on different days.")
    chosen, scope = choose(signals(), "days")
    holds = sorted(int(h) for h in scope["horizon_sessions"].unique())
    if not holds:
        return
    hold = st.radio("Hold (trading days)", holds, horizontal=True, index=len(holds) - 1)
    chosen, scope = chosen[chosen["horizon_sessions"] == hold], scope[scope["horizon_sessions"] == hold]
    days = views.daily(chosen).sort_values("day", ascending=False)
    if not days.empty:
        long = days.melt(id_vars="day", value_vars=["picks", "skipped"], var_name="group",
                         value_name="average")
        st.plotly_chart(px.bar(long, x="day", y="average", color="group", barmode="group",
                               labels={"average": "%", "day": "Entry session"},
                               title="Average result after fees, picks vs skipped (%)"),
                        width="stretch")
    table = views.by_entry_session(scope, chosen)
    table.index = table.index.date
    st.dataframe(table.round(2), width="stretch", column_config={
        "checked": "Stocks entered", "picks": "Picks", "clean": "Clean",
        "potentially_contaminated": "Potentially contaminated", "invalid": "Invalid",
        "picks_avg": "Picks: average %", "skipped_avg": "Skipped: average %"})
    st.caption("Counts cover every row that entered that day. The two averages use only the "
               "rows selected above.")


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
                    width="stretch")
    df["status"] = df["net_return_pct"].map(lambda v: "done" if pd.notna(v) else "waiting")
    st.dataframe(df.round(2), hide_index=True, width="stretch", column_config={
        "sent_on": "Sent on", "symbol": "Stock", "horizon_sessions": "Hold (days)",
        "benchmark_symbol": "Index", "net_return_pct": "Result after fees %",
        "excess_return_pct": "Better than index by %", "status": "Status"})

    st.subheader("Risk when sent")
    risk = views.pick_risk(query(views.PICK_RISK_SQL))
    recorded = risk[risk["record"] == views.RISK_RECORDED]
    st.write("The risk lines each pick was sent with: the same numbers as the Telegram message, "
             "saved at that moment. Nothing here is worked out again later. The losses are "
             "**modelled as if bought at the signal close**; nobody had bought yet, so the actual "
             "risk of a position is not known here.")
    st.dataframe(risk.round(2), hide_index=True, width="stretch", column_config={
        "sent_on": "Sent on", "symbol": "Stock", "record": "Risk record", "signal_session": "Close of",
        "reference_price": "Reference: signal close", "stop_price": "Stop",
        "stop_distance_price": "Stop distance", "stop_distance_pct": "Stop distance %",
        "atr_pct": "ATR %", "atr_multiple": "ATR ×",
        "exposure_nok": "Position at the close (NOK)", "exposure_pct": "Position % of account",
        "stop_loss_nok": "Loss at the stop, bought at the close (NOK)",
        "stop_loss_pct": "Loss at the stop, bought at the close (% of account)",
        "gap_fill_price": "Gap sale estimate (price)",
        "gap_loss_nok": "Loss in a gap, bought at the close (NOK)",
        "gap_loss_pct": "Loss in a gap, bought at the close (% of account)",
        "fee_nok": "Fees to buy and sell (NOK)", "fee_pct": "Fees % of account",
        "gap": "Gap figure", "actual": "Actual position risk", "costs": "Costs",
        "warnings": "Trading warnings"})
    st.caption(
        "**Reference** is the close the signal was formed on: the stop is measured from it and every "
        "loss assumes the shares were bought at it. Paying more than that close makes the actual loss "
        "larger; paying less makes it smaller. **Loss at the stop** sells exactly at the stop, before "
        "costs. **Gap sale estimate** is the stop price less the shortfall nine of ten measured gaps "
        "stayed under; it is not a maximum. **Fees** are the broker's table, not what was paid; "
        "spread, slippage and currency exchange are not included. Prices are in the stock's currency.  \n"
        f"{len(risk) - len(recorded)} of {len(risk)} picks have no risk numbers shown: those sent before "
        "the record existed say \"not recorded\", because the settings they were sized with were not "
        "saved and their risk cannot be rebuilt.")


RANGES = {"1M": 21, "3M": 63, "6M": 126, "YTD": None, "1Y": 252, "MAX": 0}
YEARLY_CHARTS = [
    ("Revenue", "revenue", "#5aa9e6"),
    ("Operating profit", "operating_profit", "#f28c28"),
    ("Net profit", "net_profit", "#f2c94c"),
    ("Operating cash flow", "operating_cash_flow", "#eb5757"),
    ("Earnings per share", "eps", "#4fd1c5"),
]


def _big(v: float | None) -> str:
    if v is None or pd.isna(v):
        return "–"
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= div:
            return f"{v / div:.2f}{suffix}"
    return f"{v:,.2f}"


def _num(v, fmt="{:.2f}") -> str:
    return "–" if v is None or pd.isna(v) else fmt.format(v)


def _small_chart(df: pd.DataFrame, y, title: str, color=None):
    fig = px.bar(df, x="year", y=y, title=title, barmode="group",
                 color_discrete_sequence=[color] if color else None,
                 labels={"value": "", "year": "", "variable": ""})
    fig.update_yaxes(title=None)
    fig.for_each_trace(lambda t: t.update(name=t.name.replace("total_debt", "debt")))
    fig.update_layout(height=260, margin=dict(l=10, r=10, t=40, b=10), showlegend=not color,
                      legend=dict(orientation="h", y=-0.2))
    return fig


def one_stock() -> None:
    sig, signals_problem = load_signals()
    symbols = sorted(set(sig["symbol"]) | set(query("SELECT DISTINCT symbol FROM daily_prices")["symbol"]))
    recent = st.session_state.setdefault("recent_stocks", [])
    default = recent[0] if recent else ("EQNR.OL" if "EQNR.OL" in symbols else symbols[0])

    symbol = st.selectbox("Search stock", symbols, index=symbols.index(default),
                          placeholder="Ticker, e.g. EQNR.OL", label_visibility="collapsed")
    if symbol in recent:
        recent.remove(symbol)
    recent.insert(0, symbol)
    del recent[6:]
    if len(recent) > 1:
        cols = st.columns(len(recent) - 1 + 4)
        for col, s in zip(cols, recent[1:]):
            # Clicking puts that stock first in the list; the rerun picks it up.
            if col.button(s, key=f"recent_{s}", width="stretch"):
                recent.remove(s)
                recent.insert(0, s)
                st.rerun()

    prices = query("SELECT session_date, close, currency FROM daily_prices WHERE symbol = ? "
                   "ORDER BY session_date", (symbol,))
    snap = query("SELECT data_json FROM stock_snapshots WHERE symbol = ? "
                 "ORDER BY snapshot_date DESC LIMIT 1", (symbol,))
    info = json.loads(snap["data_json"].iloc[0]).get("info", {}) if not snap.empty else {}
    mine = sig[sig["symbol"] == symbol].sort_values("signal_day")

    left, right = st.columns([3, 1], gap="large")
    with left:
        rng = st.segmented_control("Range", list(RANGES), default="1Y",
                                   label_visibility="collapsed") or "1Y"
        p = prices.assign(day=pd.to_datetime(prices["session_date"]))
        if rng == "YTD":
            p = p[p["day"].dt.year == p["day"].max().year]
        elif RANGES[rng]:
            p = p.tail(RANGES[rng])
        if p.empty:
            st.info("No daily prices saved for this stock yet.")
        else:
            first, last = p["close"].iloc[0], p["close"].iloc[-1]
            change = (last / first - 1) * 100
            currency = p["currency"].dropna().iloc[-1] if p["currency"].notna().any() else ""
            st.markdown(f"### {symbol} &nbsp; {last:,.2f} {currency} &nbsp; "
                        f":{'green' if change >= 0 else 'red'}[{change:+.2f}% {rng}]")
            fig = px.area(p, x="day", y="close", labels={"close": "", "day": ""})
            line = "#3ecf8e" if change >= 0 else "#eb5757"
            fig.update_traces(fill="tozeroy", line_color=line)
            fig.update_yaxes(range=[p["close"].min() * 0.95, p["close"].max() * 1.02])
            # The bot's picks on this stock, as markers on the price line.
            picks = mine[(mine["qualified"] == 1) & (mine["signal_day"] >= p["day"].min())]
            if not picks.empty:
                fig.add_scatter(x=picks["signal_day"], y=picks["last_close"], mode="markers",
                                name="Bot picked", marker=dict(symbol="triangle-up", size=11,
                                                               color="#f2c94c"))
            fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10),
                              legend=dict(orientation="h", y=1.05))
            st.plotly_chart(fig, width="stretch")

    with right:
        with st.container(border=True):
            st.markdown("**Key numbers**")
            fcf_yield = (info["freeCashflow"] / info["marketCap"] * 100
                         if info.get("freeCashflow") and info.get("marketCap") else None)
            rows = [
                ("Sector", info.get("sector") or "–"),
                ("Market cap", _big(info.get("marketCap"))),
                ("P/E trailing / forward",
                 f"{_num(info.get('trailingPE'))} / {_num(info.get('forwardPE'))}"),
                ("Price to book", _num(info.get("priceToBook"))),
                ("Free cash flow yield", _num(fcf_yield, "{:.2f}%")),
                ("Dividend yield", _num(info.get("dividendYield"), "{:.2f}%")),
                ("Analyst target", _num(info.get("targetMeanPrice"))),
                ("Analysts' view", (info.get("recommendationKey") or "–").replace("_", " ")),
            ]
            st.markdown("\n".join(f"| {k} | **{v}** |" for k, v in rows).join(
                ["| | |\n|:--|--:|\n", ""]))
            if snap.empty:
                st.caption("No analyst snapshot for this stock.")
        with st.container(border=True):
            st.markdown("**Scores** (0-100 vs similar stocks)")
            try:
                sc = query("SELECT month_end, quality, value, performance, momentum "
                           "FROM company_scores WHERE symbol = ? ORDER BY month_end DESC LIMIT 1",
                           (symbol,))
            except Exception:  # the table is made by the bot's first Sunday run
                sc = pd.DataFrame()
            if sc.empty:
                st.caption("No scores for this stock yet.")
            else:
                r = sc.iloc[0]
                st.markdown("| | |\n|:--|--:|\n" + "\n".join(
                    f"| {label} | **{_num(r[col], '{:.0f}')}** |" for label, col in (
                        ("Quality", "quality"), ("Value", "value"),
                        ("Company performance", "performance"), ("Momentum", "momentum"))))
                st.caption(f"End of {r['month_end']}. 100 = best of the group. They describe the "
                           "company: tested on 2021-2026, none picked shares that did better "
                           "over the next month.")

    tab_numbers, tab_bot = st.tabs(["Company numbers", "The bot and this stock"])
    with tab_numbers:
        nums = query("SELECT year_end, item, value, currency FROM company_numbers "
                     "WHERE symbol = ? ORDER BY year_end", (symbol,))
        if nums.empty:
            st.info("No company numbers saved for this stock.")
        else:
            wide = nums.pivot_table(index="year_end", columns="item", values="value").reset_index()
            wide["year"] = wide["year_end"].str[:7]
            if {"net_profit", "shares_outstanding"} <= set(wide):
                wide["eps"] = wide["net_profit"] / wide["shares_outstanding"]
            charts = [(t, c, col) for t, c, col in YEARLY_CHARTS if c in wide]
            if {"cash", "total_debt"} <= set(wide):
                charts.append(("Cash and debt", ["cash", "total_debt"], None))
            for row in range(0, len(charts), 3):
                cols = st.columns(3)
                for col, (title, y, color) in zip(cols, charts[row:row + 3]):
                    with col, st.container(border=True):
                        st.plotly_chart(_small_chart(wide, y, title, color),
                                        width="stretch")
            st.caption(f"Yearly figures in {nums['currency'].dropna().iloc[-1]}, by financial "
                       "year end. Earnings per share = net profit / shares outstanding.")
    with tab_bot:
        if signals_problem:
            st.error(signals_problem)
            return
        chosen, scope = choose(mine, "stock")
        averages = views.stock_averages(chosen)
        cols = st.columns(4)
        cols[0].metric("Days screened", len(scope))
        cols[1].metric("Times picked", int(scope["qualified"].sum()))
        cols[2].metric("Average result when picked %", _num(averages["picked"]))
        cols[3].metric("Average result when skipped %", _num(averages["skipped"]))
        view = mine.sort_values("signal_day", ascending=False).assign(
            series=lambda d: d["series"].map(views.SERIES_LABELS),
            quality=lambda d: d["quality"].map(views.QUALITY_LABELS))[
            ["signal_session", "entry_session", "reason", "last_close", "day_change_pct",
             "week_momentum_pct", "net_return_pct", "series", "quality"]]
        st.dataframe(view.round(2), hide_index=True, width="stretch", column_config={
            "signal_session": "Signal date", "entry_session": "Entered on", "reason": "Result",
            "last_close": "Price", "day_change_pct": "Today %",
            "week_momentum_pct": "Last 5 days %",
            "net_return_pct": "At the end of the hold, after fees %",
            "series": "Recorded", "quality": "Data quality"})
        st.caption("Every row is listed with its class. The two averages above use only the "
                   "rows selected.")


def data_collected() -> None:
    st.title("Data collected")
    st.write("A gap in the dates means the bot did not run that day; weekends and holidays "
             "are normal gaps. Each stock is in exactly one of the four data-quality columns.")
    sig = signals()
    df = views.collected(sig)
    st.plotly_chart(px.bar(df, x="day", y=["checked", "picked"], barmode="group",
                           labels={"value": "Stocks", "day": "", "variable": ""},
                           title="Stocks checked and picked per signal date"),
                    width="stretch")
    st.dataframe(df, hide_index=True, width="stretch", column_config={
        "day": "Signal date", "checked": "Checked", "picked": "Picked",
        "with_result": "With result", "clean": "Clean",
        "potentially_contaminated": "Potentially contaminated", "invalid": "Invalid",
        "awaiting_entry": "Awaiting entry", "overdue": "Overdue"})
    st.caption(
        "**Clean**: bought at an open that came after the signal was recorded. "
        "**Potentially contaminated**: recorded before the entry fix, and scored from an open "
        "that had already happened. **Invalid**: cannot be evaluated (reason below). "
        "**Awaiting entry**: the next session has not opened yet. **Overdue**: still no entry "
        f"session after {views.OVERDUE_AFTER_DAYS} days; the symbol or its price data needs a look.")
    reasons = views.invalid_reasons(sig)
    if not reasons.empty:
        st.subheader("Invalid rows, by reason")
        st.dataframe(reasons, hide_index=True, column_config={
            "quality_reason": "Reason", "rows": "Rows"})
    late = views.overdue(sig)
    if not late.empty:
        st.subheader("Overdue")
        st.dataframe(late[["symbol", "signal_session", "recorded_at"]], hide_index=True,
                     column_config={"symbol": "Stock", "signal_session": "Signal date",
                                    "recorded_at": "Recorded at"})


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
    st.dataframe(hold.round(0), width="stretch")
    st.subheader("My trades")
    st.dataframe(trades[["trade_date", "side", "symbol", "shares", "price", "currency",
                         "fx_to_nok", "fee_nok"]], hide_index=True, width="stretch")
    st.caption("Fees here are the ones you typed in with /fill: what was paid, not an estimate.")


def what_if() -> None:
    st.title("What-if filters")
    st.write(
        "Move the filters and see how the picks would have done against the stocks they "
        "skip, on every screen with a result so far. **This is in-sample**: tuning "
        "thresholds to past days finds noise easily, and the earlier study found no "
        "threshold set that held up out of sample. Use it to understand the filters, "
        "not to pick new ones."
    )
    df, _scope = choose(signals(), "whatif")
    df = df[df["failed_gate"].fillna("") != "stale"].copy()
    holds = sorted(df["horizon_sessions"].unique())
    if not holds:
        return
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

    df["what_if"] = views.what_if_picks(df, trend, m5, m1, atr, dd, vol)

    now, new = views.daily(df, "qualified"), views.daily(df, "what_if")
    st.subheader("The bot's filters today")
    metric_row(views.scorecard(now))
    st.subheader("Your filters")
    metric_row(views.scorecard(new))
    st.caption(f"Your filters pick {int(df['what_if'].sum())} of {len(df)} stock-days; "
               f"the bot's picked {int(df['qualified'].sum())}.")

    lines = []
    for name, d in (("Bot's filters", now), ("Your filters", new)):
        d = d.dropna(subset=["picks", "skipped"]).sort_values("day").copy()
        d["running"] = (d["picks"] - d["skipped"]).cumsum()
        d["set"] = name
        lines.append(d)
    fig = px.line(pd.concat(lines), x="day", y="running", color="set", markers=True,
                  labels={"running": "%", "day": "Entry session", "set": ""},
                  title="Picks minus skipped, added up entry session by entry session (%)")
    st.plotly_chart(fig, width="stretch")


NUMBER_LABELS = {
    "revenue": "Revenue", "operating_profit": "Operating profit", "net_profit": "Net profit",
    "equity": "Equity", "total_debt": "Debt", "cash": "Cash", "total_assets": "Total assets",
    "operating_cash_flow": "Operating cash flow", "gross_profit": "Gross profit",
    "current_assets": "Current assets", "current_liabilities": "Current liabilities",
    "shares_outstanding": "Shares outstanding",
}
SOURCE_LABELS = {"esef": "Annual report (ESEF filing)", "yahoo": "Yahoo Finance"}


def prices_kept() -> None:
    st.title("Prices we keep")
    st.write("One line per stock. Every stock should end on the last trading day; one that "
             "stops earlier means the nightly price load is failing for it.")
    df = query("SELECT symbol, MIN(session_date) AS first_day, MAX(session_date) AS last_day, "
               "COUNT(*) AS days FROM daily_prices GROUP BY symbol ORDER BY symbol")
    if df.empty:
        st.info("No daily prices saved yet.")
        return
    # The day most stocks end on is the last trading day loaded. Not the newest
    # day: one stock on an exchange that traded a day later would put all others
    # "behind".
    newest = pd.to_datetime(df["last_day"]).mode().max()
    behind = (newest - pd.to_datetime(df["last_day"])).dt.days.clip(lower=0)
    df["status"] = behind.map(lambda d: "Up to date" if d == 0 else f"{d} days behind")
    cols = st.columns(3)
    cols[0].metric("Stocks", len(df))
    cols[1].metric("Last trading day loaded", newest.strftime("%Y-%m-%d"))
    cols[2].metric("Stocks behind", int((behind > 0).sum()))
    st.dataframe(df.sort_values(["last_day", "symbol"]), hide_index=True, width="stretch",
                 column_config={"symbol": "Stock", "first_day": "First day",
                                "last_day": "Last day", "days": "Trading days",
                                "status": "Status"})


def company_numbers_page() -> None:
    st.title("Company numbers")
    st.write("Yearly numbers from annual reports, in billions. Each number shows where it "
             "came from and the day it became public, which is the first day the bot may use it.")
    symbols = query("SELECT DISTINCT symbol FROM company_numbers ORDER BY symbol")["symbol"].tolist()
    if not symbols:
        st.info("No company numbers saved yet.")
        return
    symbol = st.selectbox("Stock", symbols,
                          index=symbols.index("EQNR.OL") if "EQNR.OL" in symbols else 0)
    df = query("SELECT year_end, item, value, currency, source, known_from FROM company_numbers "
               "WHERE symbol = ? ORDER BY year_end DESC", (symbol,))
    money = df[df["item"] != "shares_outstanding"]
    wide = (money.assign(value=money["value"] / 1e9)
            .pivot_table(index="year_end", columns="item", values="value")
            .sort_index(ascending=False))
    order = [k for k in NUMBER_LABELS if k in wide]
    st.dataframe(wide[order].rename(columns=NUMBER_LABELS).round(2), width="stretch")
    currency = df["currency"].dropna().iloc[0] if df["currency"].notna().any() else ""
    st.caption(f"Billions of {currency}, by financial year end.")
    st.subheader("Where each number came from")
    detail = df.assign(item=df["item"].map(NUMBER_LABELS).fillna(df["item"]),
                       source=df["source"].map(SOURCE_LABELS).fillna(df["source"]),
                       value=df.apply(lambda r: _big(r["value"]), axis=1))
    detail = detail.sort_values(["year_end", "item"], ascending=[False, True])
    st.dataframe(detail[["year_end", "item", "value", "source", "known_from"]], hide_index=True,
                 width="stretch", column_config={"year_end": "Year end", "item": "Number",
                                                 "value": "Value", "source": "Source",
                                                 "known_from": "Public from"})


def money_put_in() -> None:
    st.title("Money put in")
    df = query("SELECT deposit_date, amount_nok FROM deposits ORDER BY deposit_date DESC, id DESC")
    if df.empty:
        st.info("No deposits saved yet. Send /deposit in Telegram when you add money.")
        return
    cols = st.columns(2)
    cols[0].metric("Total put in", f"{df['amount_nok'].sum():,.0f} NOK")
    cols[1].metric("Deposits", len(df))
    st.dataframe(df.round(0), hide_index=True, width="stretch",
                 column_config={"deposit_date": "Day", "amount_nok": "Amount (NOK)"})


DATA_PAGES = [
    st.Page(prices_kept, title="Prices we keep", icon="🗓️", url_path="prices-kept"),
    st.Page(company_numbers_page, title="Company numbers", icon="🧾", url_path="company-numbers"),
    st.Page(money_put_in, title="Money put in", icon="🏦", url_path="money-put-in"),
]
pages = [
    st.Page(home, title="Home", icon="🏠", default=True),
    st.Page(latest_screen, title="Latest screen", icon="🔎", url_path="latest-screen"),
    st.Page(day_by_day, title="Day by day", icon="📅", url_path="day-by-day"),
    st.Page(sent_picks, title="Sent picks", icon="📨", url_path="sent-picks"),
    st.Page(one_stock, title="One stock", icon="📈", url_path="one-stock"),
    st.Page(data_collected, title="Data collected", icon="🗂️", url_path="data-collected"),
    st.Page(my_money, title="My money", icon="💰", url_path="my-money"),
    st.Page(what_if, title="What-if filters", icon="🎛️", url_path="what-if"),
    *DATA_PAGES,
]
# Served by the login in front of the app (auth.py), not by a page.
st.sidebar.link_button("Log out", "/logout")
st.navigation(pages).run()
