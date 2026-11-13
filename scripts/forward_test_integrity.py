#!/usr/bin/env python3
"""Read-only fingerprint of the signal archive, to prove a deploy left it intact.

The archive is the one thing in this project that cannot be regenerated. Take
a snapshot before a deploy and another after, then compare:

    python scripts/forward_test_integrity.py snapshot DB > before.json
    python scripts/forward_test_integrity.py snapshot DB > after.json
    python scripts/forward_test_integrity.py compare before.json after.json

`compare` exits 1 if a row disappeared, if what a signal *was* changed
(anything but its outcome), or if an outcome that already existed changed. A
pending row gaining its outcome, and new rows, are normal.

The database is opened with mode=ro and only read. Needs nothing but Python,
so it also runs inside either container.
"""

import hashlib
import json
import sqlite3
import sys
from typing import Any, Dict, List

# The columns a signal was recorded with before the entry fix. `outcome_json`
# is tracked separately; columns added later are not part of what a signal was.
IDENTITY = (
    "symbol, signal_session, qualified, rank, failed_gate, last_close, day_change_pct, "
    "week_momentum_pct, volume_ratio, atr_pct, drawdown_pct, rsi_14, rule_id, horizon_sessions, "
    "cost_bps_per_side, benchmark_symbol, recorded_at"
)


def _digest(values: Any) -> str:
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


def snapshot(path: str) -> Dict[str, Any]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        names = dict(conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"))
        tables = {
            name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608 - names from sqlite_master
            for name, kind in sorted(names.items()) if kind == "table"
        }
        columns = [row[1] for row in conn.execute("PRAGMA table_info(signal_archive)")]
        rows = conn.execute(
            f"SELECT {IDENTITY}, outcome_json FROM signal_archive ORDER BY symbol, signal_session"  # noqa: S608
        ).fetchall()
        result: Dict[str, Any] = {
            "integrity_check": [row[0] for row in conn.execute("PRAGMA integrity_check")],
            "tables": tables,
            "views": sorted(name for name, kind in names.items() if kind == "view"),
            "signal_archive_columns": columns,
            "rows": len(rows),
            "settled": sum(1 for row in rows if row[-1] is not None),
            # One checksum for a quick eyeball, then per-row digests so a difference can be named.
            "identity_sha256": _digest([row[:-1] for row in rows]),
            "settled_outcomes_sha256": _digest(
                [(row[0], row[1], row[16], row[-1]) for row in rows if row[-1] is not None]),
            "identity": {f"{row[0]}|{row[1]}": _digest(row[:-1]) for row in rows},
            "outcome": {f"{row[0]}|{row[1]}": _digest(row[-1]) for row in rows if row[-1] is not None},
        }
        if "entry_policy" in columns:
            result["entry_policy"] = dict(conn.execute(
                "SELECT COALESCE(entry_policy, 'NULL (recorded before the fix)'), COUNT(*) "
                "FROM signal_archive GROUP BY 1"))
        if "signal_archive_quality_active" in names:
            result["active_classifier_version"] = [
                row[0] for row in conn.execute("SELECT version FROM signal_archive_quality_active")]
        if "signal_archive_evaluated" in names:
            result["classification"] = [
                {"series": series, "hold": hold, "quality": quality, "reason": reason,
                 "rows": count, "settled": settled, "qualified": qualified}
                for series, hold, quality, reason, count, settled, qualified in conn.execute(
                    "SELECT series, horizon_sessions, quality, quality_reason, COUNT(*), "
                    "SUM(settled), SUM(qualified) FROM signal_archive_evaluated "
                    "GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4")
            ]
            # The rule itself, checked on stored data: no new-policy entry at or before its recording.
            result["entries_not_after_recording"] = conn.execute(
                "SELECT COUNT(*) FROM signal_archive "
                "WHERE entry_policy IS NOT NULL AND outcome_json IS NOT NULL "
                "AND julianday(json_extract(outcome_json, '$.entry_open_at')) <= julianday(recorded_at)"
            ).fetchone()[0]
        return result
    finally:
        conn.close()


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """Everything that must not have happened between two snapshots. Empty means intact."""
    problems = []
    for label, snap in (("before", before), ("after", after)):
        if snap["integrity_check"] != ["ok"]:
            problems.append(f"{label}: integrity_check says {snap['integrity_check']}")
    missing = sorted(set(before["identity"]) - set(after["identity"]))
    if missing:
        problems.append(f"{len(missing)} archive rows disappeared, e.g. {missing[:5]}")
    changed = sorted(key for key, digest in before["identity"].items()
                     if key in after["identity"] and after["identity"][key] != digest)
    if changed:
        problems.append(f"{len(changed)} rows changed what was recorded, e.g. {changed[:5]}")
    rewritten = sorted(key for key, digest in before["outcome"].items()
                       if after["outcome"].get(key) != digest)
    if rewritten:
        problems.append(f"{len(rewritten)} existing outcomes changed or vanished, e.g. {rewritten[:5]}")
    for table, count in before["tables"].items():
        if table not in after["tables"]:
            problems.append(f"table {table} is gone")
        elif after["tables"][table] < count and table not in ("trade_metrics_history",):
            # trade_metrics_history is pruned by design; every other table only grows.
            problems.append(f"table {table} lost rows: {count} -> {after['tables'][table]}")
    if after.get("entries_not_after_recording"):
        problems.append(
            f"{after['entries_not_after_recording']} new-policy outcomes use an open at or before recorded_at")
    return problems


def _summary(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rows": [before["rows"], after["rows"]],
        "rows_added": len(set(after["identity"]) - set(before["identity"])),
        "settled": [before["settled"], after["settled"]],
        "newly_settled": len(set(after["outcome"]) - set(before["outcome"])),
        "identity_unchanged_for_rows_present_before": all(
            after["identity"].get(key) == digest for key, digest in before["identity"].items()),
        "outcomes_unchanged_for_rows_settled_before": all(
            after["outcome"].get(key) == digest for key, digest in before["outcome"].items()),
        "columns_added": [c for c in after["signal_archive_columns"] if c not in before["signal_archive_columns"]],
        "tables_added": sorted(set(after["tables"]) - set(before["tables"])),
        "views_added": sorted(set(after["views"]) - set(before["views"])),
    }


def main(argv: List[str]) -> int:
    if len(argv) == 3 and argv[1] == "snapshot":
        json.dump(snapshot(argv[2]), sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    if len(argv) == 4 and argv[1] == "compare":
        with open(argv[2], encoding="utf-8") as first, open(argv[3], encoding="utf-8") as second:
            before, after = json.load(first), json.load(second)
        problems = compare(before, after)
        json.dump({"summary": _summary(before, after), "problems": problems}, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 1 if problems else 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
