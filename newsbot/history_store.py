"""SQLite trade history and the published-pick ledger."""

import json
import math
import sqlite3
import statistics
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Dict, List, Optional, Tuple

import requests.exceptions
import yfinance as yf

from newsbot import market_data
from newsbot.benchmarks import _backtest_benchmark_for, _matched_benchmark_return
from newsbot.config import SETTINGS
from newsbot.indicators import TradeMetrics, _drop_in_progress_bar
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.state import TRADE_HISTORY_LOCK


class TradeHistoryStore:
    """Remembers what the screener measured each session, for every symbol it
    looked at -- not just the ones that qualified.

    `_compute_trade_metrics` recomputes from a fresh yfinance pull every time,
    so history supplies streaks and weekly consistency. One row per
    (symbol, session_date); a same-day rerun
    (e.g. /now after the scheduled run) overwrites its row rather than
    duplicating it, so re-running never inflates a streak.
    """

    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None

    def _connection(self) -> sqlite3.Connection:
        # Every access already goes through TRADE_HISTORY_LOCK (record/prune/
        # history all take it), so one shared connection with
        # check_same_thread=False is safe and avoids opening -- and leaking,
        # since a ThreadPoolExecutor's workers are never explicitly torn
        # down -- a new connection per screener worker thread.
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            table_definition = """
                    symbol TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    last_close REAL NOT NULL,
                    day_change_pct REAL NOT NULL,
                    week_momentum_pct REAL NOT NULL,
                    volume_ratio REAL,
                    drawdown_pct REAL NOT NULL,
                    atr_pct REAL NOT NULL,
                    above_ema20 INTEGER NOT NULL,
                    qualified INTEGER NOT NULL,
                    failed_gate TEXT,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, session_date)
                """
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS trade_metrics_history ({table_definition})"
            )
            columns = conn.execute(
                "PRAGMA table_info(trade_metrics_history)"
            ).fetchall()
            volume_column = next((column for column in columns if column[1] == "volume_ratio"), None)
            if volume_column is not None and volume_column[3]:
                # Older databases required a volume ratio, which made the new
                # explicit "unknown" value fail to persist. Rebuild the table
                # once while preserving the existing measurements.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "ALTER TABLE trade_metrics_history RENAME TO trade_metrics_history_legacy"
                )
                conn.execute(
                    f"CREATE TABLE trade_metrics_history ({table_definition})"
                )
                conn.execute(
                    """
                    INSERT INTO trade_metrics_history
                    SELECT symbol, session_date, last_close, day_change_pct,
                           week_momentum_pct, volume_ratio, drawdown_pct, atr_pct,
                           above_ema20, qualified, failed_gate, recorded_at
                    FROM trade_metrics_history_legacy
                    """
                )
                conn.execute("DROP TABLE trade_metrics_history_legacy")
            conn.commit()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS published_picks (
                    symbol TEXT NOT NULL,
                    signal_session TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    horizon_sessions INTEGER NOT NULL,
                    cost_bps_per_side REAL NOT NULL,
                    benchmark_symbol TEXT,
                    snapshot_json TEXT NOT NULL,
                    outcome_json TEXT,
                    PRIMARY KEY (symbol, signal_session)
                )
                """
            )
            conn.commit()
            # Permanent, unlike trade_metrics_history (pruned after a couple of
            # weeks). One row per screened symbol per session, qualified or not,
            # so a forward test has both the signals and the same-date baseline.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_archive (
                    symbol TEXT NOT NULL,
                    signal_session TEXT NOT NULL,
                    qualified INTEGER NOT NULL,
                    rank INTEGER,
                    failed_gate TEXT,
                    last_close REAL NOT NULL,
                    day_change_pct REAL NOT NULL,
                    week_momentum_pct REAL NOT NULL,
                    volume_ratio REAL,
                    atr_pct REAL NOT NULL,
                    drawdown_pct REAL NOT NULL,
                    rsi_14 REAL,
                    rule_id TEXT NOT NULL,
                    horizon_sessions INTEGER NOT NULL,
                    cost_bps_per_side REAL NOT NULL,
                    benchmark_symbol TEXT,
                    recorded_at TEXT NOT NULL,
                    outcome_json TEXT,
                    PRIMARY KEY (symbol, signal_session)
                )
                """
            )
            conn.commit()
            self._conn = conn
        return self._conn

    @staticmethod
    def current_rule_id() -> str:
        """Identifies the gate thresholds a signal was produced under, so a later
        change to them is never averaged together with earlier signals."""
        return (
            f"mom{SETTINGS.trade_min_week_momentum_pct:g}"
            f"-day{SETTINGS.trade_min_day_change_pct:g}"
            f"-atr{SETTINGS.trade_max_atr_pct:g}"
            f"-dd{SETTINGS.trade_max_drawdown_pct:g}"
            f"-vol{SETTINGS.trade_min_volume_ratio:g}"
        )

    def archive_signal(
        self, metrics: "TradeMetrics", qualified: bool, failed_gate: Optional[str]
    ) -> None:
        """First observation of a (symbol, session) wins, like published picks:
        a rerun or a later settings change cannot rewrite what was signalled."""
        with TRADE_HISTORY_LOCK:
            try:
                conn = self._connection()
                with conn:
                    conn.execute(
                        """
                        INSERT INTO signal_archive (
                            symbol, signal_session, qualified, failed_gate, last_close,
                            day_change_pct, week_momentum_pct, volume_ratio, atr_pct,
                            drawdown_pct, rsi_14, rule_id, horizon_sessions,
                            cost_bps_per_side, benchmark_symbol, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(symbol, signal_session) DO NOTHING
                        """,
                        (
                            metrics.symbol, metrics.session, int(qualified), failed_gate,
                            metrics.last_close, metrics.day_change_pct,
                            metrics.week_momentum_pct, metrics.volume_ratio, metrics.atr_pct,
                            metrics.drawdown_pct, metrics.rsi_14, self.current_rule_id(),
                            SETTINGS.trade_outcome_lookback_days,
                            SETTINGS.trade_backtest_cost_bps,
                            _backtest_benchmark_for(metrics.symbol),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
            except Exception as exc:
                SYS_LOG.warning("signal_archive_failed symbol=%s detail=%s", metrics.symbol, exc)

    def archive_ranks(self, ranked: List["TradeMetrics"]) -> None:
        """Record the delivered ordering of qualifiers (1 = top). Set once."""
        with TRADE_HISTORY_LOCK:
            try:
                conn = self._connection()
                with conn:
                    for rank, metrics in enumerate(ranked, 1):
                        conn.execute(
                            """
                            UPDATE signal_archive SET rank = ?
                            WHERE symbol = ? AND signal_session = ? AND rank IS NULL
                            """,
                            (rank, metrics.symbol, metrics.session),
                        )
            except Exception as exc:
                SYS_LOG.warning("signal_archive_rank_failed detail=%s", exc)

    def archive_rows(self) -> List[Dict[str, Any]]:
        with TRADE_HISTORY_LOCK:
            cursor = self._connection().execute(
                "SELECT * FROM signal_archive ORDER BY signal_session, symbol"
            )
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def complete_archive_row(self, symbol: str, signal_session: str, outcome: Dict[str, Any]) -> None:
        with TRADE_HISTORY_LOCK:
            conn = self._connection()
            with conn:
                conn.execute(
                    """
                    UPDATE signal_archive SET outcome_json = ?
                    WHERE symbol = ? AND signal_session = ? AND outcome_json IS NULL
                    """,
                    (json.dumps(outcome, allow_nan=False), symbol, signal_session),
                )

    def publish(self, picks: List["TradeMetrics"], published_at: datetime) -> None:
        """First successful publication wins; reruns cannot rewrite the signal."""
        if published_at.tzinfo is None:
            raise ValueError("publication timestamp must include a timezone")
        if (
            SETTINGS.trade_outcome_lookback_days < 1
            or not math.isfinite(SETTINGS.trade_backtest_cost_bps)
            or SETTINGS.trade_backtest_cost_bps < 0
        ):
            raise ValueError("publication requires a positive horizon and finite nonnegative costs")
        with TRADE_HISTORY_LOCK:
            conn = self._connection()
            with conn:
                for rank, metrics in enumerate(picks, 1):
                    snapshot = {
                        "rank": rank,
                        "metrics": asdict(metrics),
                        "gates": {
                            key: getattr(SETTINGS, key)
                            for key in (
                                "trade_min_week_momentum_pct", "trade_min_day_change_pct",
                                "trade_max_atr_pct", "trade_max_drawdown_pct",
                                "trade_min_volume_ratio",
                            )
                        },
                    }
                    conn.execute(
                        """
                        INSERT INTO published_picks (
                            symbol, signal_session, published_at, horizon_sessions,
                            cost_bps_per_side, benchmark_symbol, snapshot_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(symbol, signal_session) DO NOTHING
                        """,
                        (
                            metrics.symbol, metrics.session,
                            published_at.astimezone(timezone.utc).isoformat(),
                            SETTINGS.trade_outcome_lookback_days,
                            SETTINGS.trade_backtest_cost_bps,
                            _backtest_benchmark_for(metrics.symbol),
                            json.dumps(snapshot, allow_nan=False),
                        ),
                    )

    def published_picks(self, limit: int = 100) -> List[Dict[str, Any]]:
        with TRADE_HISTORY_LOCK:
            cursor = self._connection().execute(
                """
                SELECT * FROM published_picks
                ORDER BY published_at DESC, symbol ASC LIMIT ?
                """,
                (limit,),
            )
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def complete_pick(self, pick: Dict[str, Any], outcome: Dict[str, Any]) -> None:
        with TRADE_HISTORY_LOCK:
            conn = self._connection()
            with conn:
                conn.execute(
                    """
                    UPDATE published_picks SET outcome_json = ?
                    WHERE symbol = ? AND signal_session = ? AND outcome_json IS NULL
                    """,
                    (json.dumps(outcome, allow_nan=False), pick["symbol"], pick["signal_session"]),
                )

    def record(self, metrics: "TradeMetrics", qualified: bool, failed_gate: Optional[str]) -> None:
        with TRADE_HISTORY_LOCK:
            try:
                conn = self._connection()
                conn.execute(
                    """
                    INSERT INTO trade_metrics_history (
                        symbol, session_date, last_close, day_change_pct,
                        week_momentum_pct, volume_ratio, drawdown_pct, atr_pct,
                        above_ema20, qualified, failed_gate, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, session_date) DO UPDATE SET
                        last_close=excluded.last_close,
                        day_change_pct=excluded.day_change_pct,
                        week_momentum_pct=excluded.week_momentum_pct,
                        volume_ratio=excluded.volume_ratio,
                        drawdown_pct=excluded.drawdown_pct,
                        atr_pct=excluded.atr_pct,
                        above_ema20=excluded.above_ema20,
                        qualified=excluded.qualified,
                        failed_gate=excluded.failed_gate,
                        recorded_at=excluded.recorded_at
                    """,
                    (
                        metrics.symbol,
                        metrics.session,
                        metrics.last_close,
                        metrics.day_change_pct,
                        metrics.week_momentum_pct,
                        metrics.volume_ratio,
                        metrics.drawdown_pct,
                        metrics.atr_pct,
                        int(metrics.above_ema20),
                        int(qualified),
                        failed_gate,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
            except Exception as exc:
                SYS_LOG.warning("trade_history_record_failed symbol=%s detail=%s", metrics.symbol, exc)

    def prune(self, retention_days: int, today: Optional[str] = None) -> None:
        anchor_text = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            anchor = datetime.strptime(anchor_text, "%Y-%m-%d")
        except ValueError:
            anchor = datetime.now(timezone.utc)
        cutoff = (anchor - timedelta(days=max(0, retention_days - 1))).strftime("%Y-%m-%d")
        with TRADE_HISTORY_LOCK:
            try:
                conn = self._connection()
                conn.execute(
                    "DELETE FROM trade_metrics_history WHERE session_date < ?", (cutoff,)
                )
                conn.commit()
            except Exception as exc:
                SYS_LOG.warning("trade_history_prune_failed detail=%s", exc)

    def history(self, symbol: str, days: int = 14, today: Optional[str] = None) -> List[sqlite3.Row]:
        anchor_text = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            anchor = datetime.strptime(anchor_text, "%Y-%m-%d")
        except ValueError:
            anchor = datetime.now(timezone.utc)
        cutoff = (anchor - timedelta(days=days)).strftime("%Y-%m-%d")
        with TRADE_HISTORY_LOCK:
            try:
                conn = self._connection()
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT * FROM trade_metrics_history
                    WHERE symbol = ? AND session_date >= ?
                    ORDER BY session_date ASC
                    """,
                    (symbol, cutoff),
                ).fetchall()
                conn.row_factory = None
                return list(rows)
            except Exception as exc:
                SYS_LOG.warning("trade_history_read_failed symbol=%s detail=%s", symbol, exc)
                return []

    def recent_qualifying_streak(
        self, symbol: str, days: int = 14, today: Optional[str] = None
    ) -> int:
        """Consecutive most-recent sessions (working backward) that qualified."""
        rows = self.history(symbol, days=days, today=today)
        streak = 0
        for row in reversed(rows):
            if not row["qualified"]:
                break
            streak += 1
        return streak

    def close(self) -> None:
        with TRADE_HISTORY_LOCK:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def symbols_with_history(self) -> List[str]:
        with TRADE_HISTORY_LOCK:
            try:
                conn = self._connection()
                rows = conn.execute("SELECT DISTINCT symbol FROM trade_metrics_history").fetchall()
                return [row[0] for row in rows]
            except Exception as exc:
                SYS_LOG.warning("trade_history_symbols_failed detail=%s", exc)
                return []

    def recently_tracked_symbols(
        self, days: int, today: Optional[str] = None
    ) -> List[str]:
        """Symbols screened at least once in the last `days` calendar days,
        the ones that qualified most often first.

        This is what gives the screened universe continuity. Discovery hands
        back a fresh set of movers each session, so a symbol picked up on
        Monday was simply never looked at again -- and `/stocks`, which reads
        recorded history rather than live prices, needs a symbol measured on
        several sessions before it can say anything about it at all.

        Ordering by qualifying count and then recency means a limited slot
        budget is spent re-measuring the names that have actually been
        earning their place, not whatever happened to be screened last.
        """
        anchor_text = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            anchor = datetime.strptime(anchor_text, "%Y-%m-%d")
        except ValueError:
            anchor = datetime.now(timezone.utc)
        cutoff = (anchor - timedelta(days=max(0, days))).strftime("%Y-%m-%d")
        with TRADE_HISTORY_LOCK:
            try:
                conn = self._connection()
                rows = conn.execute(
                    """
                    SELECT symbol
                    FROM trade_metrics_history
                    WHERE session_date >= ?
                    GROUP BY symbol
                    ORDER BY SUM(qualified) DESC, MAX(session_date) DESC, symbol ASC
                    """,
                    (cutoff,),
                ).fetchall()
                return [row[0] for row in rows]
            except Exception as exc:
                SYS_LOG.warning("trade_history_tracked_failed detail=%s", exc)
                return []

    def weekly_top_picks(
        self,
        lookback_days: int = 7,
        top_n: int = 5,
        min_qualifying_days: int = 2,
        today: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Rank symbols by how consistently they qualified over the last
        `lookback_days` sessions, not just today's single screener pass --
        the "held up over the week" read `/stocks` needs for swing trading,
        as distinct from `/recommendations`'s live single-day re-screen.

        Pure read over already-recorded history (no fresh yfinance calls),
        symbol only shows up here if it was actually screened on the days
        being counted.
        """
        picks: List[Dict[str, Any]] = []
        for symbol in self.symbols_with_history():
            # `history` windows by calendar day, but this ranks by *sessions*
            # -- a 7-calendar-day window is only about 5 trading sessions, so
            # asking for 7 days and calling the result "7 sessions" quietly
            # under-counted every symbol. Read wide, then keep the last
            # `lookback_days` rows, which are one per session by construction.
            rows = self.history(symbol, days=lookback_days * 2 + 7, today=today)[-lookback_days:]
            if not rows:
                continue
            qualifying_days = sum(1 for row in rows if row["qualified"])
            if qualifying_days < min_qualifying_days:
                continue
            avg_week_momentum = sum(row["week_momentum_pct"] for row in rows) / len(rows)
            latest = rows[-1]
            picks.append(
                {
                    "symbol": symbol,
                    "qualifying_days": qualifying_days,
                    "sessions_tracked": len(rows),
                    "avg_week_momentum_pct": avg_week_momentum,
                    "latest_session": latest["session_date"],
                    "latest_close": latest["last_close"],
                    "latest_day_change_pct": latest["day_change_pct"],
                    "latest_atr_pct": latest["atr_pct"],
                    "latest_drawdown_pct": latest["drawdown_pct"],
                }
            )
        picks.sort(key=lambda p: (-p["qualifying_days"], -p["avg_week_momentum_pct"], p["symbol"]))
        return picks[:top_n]


TRADE_HISTORY = TradeHistoryStore(SETTINGS.trade_history_db_file)


def _record_published_picks(picks: List[TradeMetrics]) -> None:
    if not picks:
        return
    try:
        TRADE_HISTORY.publish(picks, datetime.now(timezone.utc))
    except (sqlite3.Error, ValueError, OSError) as exc:
        APP_LOG.error("published_picks_record_failed detail=%s", exc)


def _published_pick_outcome(
    pick: Dict[str, Any], history: Any, benchmark: Any,
) -> Optional[Dict[str, Any]]:
    """Daily-bar proxy: enter the first session strictly after publication's local date."""
    if history is None or len(history) == 0 or getattr(history.index, "tz", None) is None:
        raise ValueError("missing stock history or exchange timezone")
    if pick["horizon_sessions"] < 1:
        raise ValueError("invalid holding horizon")
    dates = history.index.strftime("%Y-%m-%d").tolist()
    if dates != sorted(set(dates)):
        raise ValueError("unordered or duplicate stock sessions")
    published_at = datetime.fromisoformat(pick["published_at"])
    if published_at.tzinfo is None:
        raise ValueError("missing publication timezone")
    publication_date = published_at.astimezone(history.index.tz).strftime("%Y-%m-%d")
    if dates[0] > publication_date:
        raise ValueError("history does not cover publication")
    eligible = [
        index for index, date in enumerate(dates)
        if date > publication_date and date > pick["signal_session"]
    ]
    if not eligible:
        return None
    entry = eligible[0]
    exit_position = entry + pick["horizon_sessions"] - 1
    if exit_position >= len(history):
        return None
    prices = history.iloc[entry:exit_position + 1][["Open", "Close"]]
    if (
        prices.isna().any().any()
        or prices.isin([float("inf"), float("-inf")]).any().any()
        or (prices <= 0).any().any()
    ):
        raise ValueError("invalid prices during holding period")
    entry_price = float(history.iloc[entry]["Open"])
    exit_price = float(history.iloc[exit_position]["Close"])
    gross = (exit_price / entry_price - 1.0) * 100.0
    benchmark_return = _matched_benchmark_return(
        benchmark, dates[entry], dates[exit_position],
    )
    net = gross - 2.0 * pick["cost_bps_per_side"] / 100.0
    return {
        "entry_session": dates[entry],
        "exit_session": dates[exit_position],
        "gross_return_pct": gross,
        "net_return_pct": net,
        "benchmark_return_pct": benchmark_return,
        "excess_return_pct": net - benchmark_return if benchmark_return is not None else None,
    }


def build_published_pick_report() -> str:
    """Evaluate the latest 100 delivered signals, retaining completed outcomes."""
    lines = ["<b>Published Pick Performance</b>", ""]
    try:
        picks = TRADE_HISTORY.published_picks()
    except (sqlite3.Error, OSError) as exc:
        SYS_LOG.error("published_picks_read_failed detail=%s", exc)
        return "\n".join(lines + ["Pick ledger unavailable. Check bot logs."])
    if not picks:
        return "\n".join(lines + [
            "No published picks recorded yet. Tracking starts with successful Telegram "
            "delivery of /recommendations, /now, or scheduled briefings.",
        ])
    history_cache: Dict[str, Any] = {}

    def history_for(symbol: Optional[str]) -> Any:
        if not symbol:
            return None
        if symbol not in history_cache:
            try:
                history_cache[symbol] = _drop_in_progress_bar(
                    market_data._fetch_ticker_history(symbol, period="max")
                )
            except (requests.RequestException, yf.exceptions.YFException, ValueError, RuntimeError, OSError) as exc:
                SYS_LOG.warning("published_pick_history_failed symbol=%s detail=%s", symbol, exc)
                history_cache[symbol] = None
        return history_cache[symbol]

    completed: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    pending = unavailable = 0
    for pick in picks:
        try:
            if pick["outcome_json"]:
                outcome = json.loads(pick["outcome_json"])
            else:
                outcome = _published_pick_outcome(
                    pick, history_for(pick["symbol"]), history_for(pick["benchmark_symbol"]),
                )
                if outcome is None:
                    pending += 1
                    continue
                TRADE_HISTORY.complete_pick(pick, outcome)
            completed.append((pick, outcome))
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
            SYS_LOG.warning("published_pick_evaluation_failed symbol=%s detail=%s", pick["symbol"], exc)
            unavailable += 1
    lines.append(
        f"Latest {len(picks)} recorded signals (window capped at 100): "
        f"{len(completed)} completed · "
        f"{pending} pending · {unavailable} unavailable"
    )
    if completed:
        returns = [outcome["net_return_pct"] for _, outcome in completed]
        excess = [
            outcome["excess_return_pct"] for _, outcome in completed
            if outcome["excess_return_pct"] is not None
        ]
        lines.append(
            f"Avg net {statistics.mean(returns):+.2f}% · median {statistics.median(returns):+.2f}% · "
            f"hit rate {sum(value > 0 for value in returns) / len(returns) * 100:.0f}%"
        )
        lines.append(
            f"Benchmark coverage: {len(excess)}/{len(completed)}"
            + (f" · avg excess {statistics.mean(excess):+.2f}%" if excess else "")
        )
        for pick, outcome in completed[:10]:
            comparison = (
                f" · excess {outcome['excess_return_pct']:+.2f}% vs "
                f"{escape(pick['benchmark_symbol'])}"
                if outcome["excess_return_pct"] is not None else ""
            )
            lines.append(
                f"    {escape(pick['symbol'])} {outcome['net_return_pct']:+.2f}% net · "
                f"{outcome['gross_return_pct']:+.2f}% gross · "
                f"{outcome['entry_session']} to {outcome['exit_session']} · "
                f"{pick['horizon_sessions']} sessions, {pick['cost_bps_per_side']:g} bps/side"
                + comparison
            )
        if len(completed) > 10:
            lines.append("Showing the 10 most recently published completed signals.")
    lines.extend([
        "",
        "<i>Daily-bar proxy, not actual fills: entry is the first session strictly after "
        "the publication date in the stock exchange timezone. Same-day opens are never "
        "used. Horizon, cost, and benchmark are frozen at first delivery; completed "
        "outcomes are retained. Overlapping signals are not portfolio returns. "
        "Unmatched benchmarks are not assumed to return zero.</i>",
    ])
    return "\n".join(lines)
