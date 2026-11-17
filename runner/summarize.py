"""Summarizer: collect all shard status files into `status/summary.json`.

Runs after every shard finishes. Keeps the summary small even with 10k+ bans:
a few counters, a `last_seen` map (ban_id -> JST timestamp of the most
recent check), and at most 50 "problem" entries (ng/warn/stale). Anything
over that is counted but not listed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import common  # noqa: E402

MAX_PROBLEMS = 50


def _parse_jst(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=common.JST)
        return dt
    except Exception:
        return None


def classify(ban: dict[str, Any], row: dict[str, Any] | None, now: datetime,
             last_seen: dict[str, str]) -> tuple[str, str]:
    """Return (status, reason). status ∈ {ok, warn, ng, stale}."""
    if row is not None:
        if (not row.get("source_ok")) or row.get("mismatch", 0) > 0 or row.get("license_changed"):
            reasons = []
            if not row.get("source_ok"):
                reasons.append(f"source_ok=false ({row.get('note','') or 'no detail'})")
            if row.get("mismatch", 0) > 0:
                reasons.append(f"mismatch={row['mismatch']}")
            if row.get("license_changed"):
                reasons.append("license_changed")
            return "ng", "; ".join(reasons)
        if row.get("broken_links", 0) > 0 or (
            ban["freq"] == "daily" and row.get("records_delta", 0) == 0 and row.get("records", 0) > 0
        ):
            parts = []
            if row.get("broken_links", 0) > 0:
                parts.append(f"broken_links={row['broken_links']}")
            if ban["freq"] == "daily" and row.get("records_delta", 0) == 0 and row.get("records", 0) > 0:
                parts.append("delta=0 (daily)")
            return "warn", "; ".join(parts)
        return "ok", ""

    # No row this run. Was it scheduled today?
    if common.should_run_today(ban, now):
        return "stale", "scheduled today but no status row"

    # Not scheduled today — stale only if we haven't seen it in 1.5x its freq.
    seen = _parse_jst(last_seen.get(ban["id"], ""))
    if seen is None:
        return "ok", ""  # new ban, never ran — not stale yet
    budget = timedelta(hours=common.freq_hours(ban["freq"]) * 1.5)
    if now - seen > budget:
        return "stale", f"no update in >1.5x freq ({ban['freq']})"
    return "ok", ""


def main() -> int:
    now = common.now_jst()
    bans = common.load_registry()

    # Gather every row written by any shard this run.
    rows_by_id: dict[str, dict[str, Any]] = {}
    for p in sorted(common.STATUS_DIR.glob("*.jsonl")):
        for row in common.read_status_shard(p):
            if row.get("ban"):
                rows_by_id[row["ban"]] = row

    # Previous summary's last_seen map (so unscheduled bans can age into stale).
    prev = common.read_json(common.STATUS_DIR / "summary.json") or {}
    last_seen: dict[str, str] = dict(prev.get("last_seen") or {})
    for bid, row in rows_by_id.items():
        if row.get("source_ok") and row.get("checked_at"):
            last_seen[bid] = row["checked_at"]

    counters = {"ok": 0, "warn": 0, "ng": 0, "stale": 0}
    problems: list[dict[str, Any]] = []
    ran_today = 0
    for ban in bans:
        row = rows_by_id.get(ban["id"])
        if row is not None:
            ran_today += 1
        status, reason = classify(ban, row, now, last_seen)
        counters[status] += 1
        if status in ("warn", "ng", "stale") and len(problems) < MAX_PROBLEMS:
            problems.append({
                "ban": ban["id"],
                "status": status,
                "freq": ban["freq"],
                "reason": reason,
                "checked_at": (row or {}).get("checked_at", ""),
            })

    truncated = counters["warn"] + counters["ng"] + counters["stale"] - len(problems)
    summary = {
        "generated_at": common.iso_jst(now),
        "total": len(bans),
        "ran_today": ran_today,
        "ok": counters["ok"],
        "warn": counters["warn"],
        "ng": counters["ng"],
        "stale": counters["stale"],
        "problems": problems,
        "problems_truncated": max(truncated, 0),
        "last_seen": {b["id"]: last_seen[b["id"]] for b in bans if b["id"] in last_seen},
    }
    common.write_json(common.STATUS_DIR / "summary.json", summary)
    print(
        f"summary: total={summary['total']} ran_today={summary['ran_today']} "
        f"ok={summary['ok']} warn={summary['warn']} ng={summary['ng']} "
        f"stale={summary['stale']} problems={len(problems)} "
        f"(truncated={summary['problems_truncated']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
