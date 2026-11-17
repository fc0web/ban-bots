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
DATA_RETENTION_DAYS = 30


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
        # If the row is scheduled to be refreshed today but its own JST
        # date is older than today, it's a stale row from a previous day
        # (most often a crashed run.py where summarize fell back to the
        # previously committed 0.jsonl). Count it as stale so mokuroku
        # turns red, instead of silently "ok".
        today_jst = common.today_jst_str(now)
        row_date = row.get("date_jst") or (row.get("checked_at") or "")[:10]
        if common.should_run_today(ban, now) and row_date and row_date != today_jst:
            return "stale", f"no row for today (row.date_jst={row_date})"
        if (not row.get("source_ok")) or row.get("mismatch", 0) > 0 or row.get("license_changed"):
            reasons = []
            if not row.get("source_ok"):
                reasons.append(f"source_ok=false ({row.get('note','') or 'no detail'})")
            if row.get("mismatch", 0) > 0:
                reasons.append(f"mismatch={row['mismatch']}")
            if row.get("license_changed"):
                reasons.append("license_changed")
            return "ng", "; ".join(reasons)
        # With cross-day dedup, records == records_delta == "new today",
        # so a daily board with records_delta == 0 and after_filter > 0 means
        # "the source is alive and produced candidates, but all of them had
        # already been surfaced before" — noise worth flagging.
        after_filter = row.get("after_filter", row.get("records", 0))
        delta_zero_with_candidates = (
            ban["freq"] == "daily"
            and row.get("records_delta", 0) == 0
            and after_filter > 0
        )
        if row.get("broken_links", 0) > 0 or delta_zero_with_candidates:
            parts = []
            if row.get("broken_links", 0) > 0:
                parts.append(f"broken_links={row['broken_links']}")
            if delta_zero_with_candidates:
                parts.append(f"new_today=0 (after_filter={after_filter})")
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


def prune_old_data(now: datetime) -> int:
    """Delete data/<ban>/YYYY-MM-DD.json files older than DATA_RETENTION_DAYS.

    Returns the number of files removed. Measured in JST days: cutoff is
    (today_jst - (RETENTION - 1)), so today plus the previous 29 days are
    kept, giving a 30-day window including today.
    """
    cutoff = now.date() - timedelta(days=DATA_RETENTION_DAYS - 1)
    removed = 0
    if not common.DATA_DIR.exists():
        return 0
    for ban_dir in sorted(common.DATA_DIR.iterdir()):
        if not ban_dir.is_dir():
            continue
        for f in sorted(ban_dir.glob("*.json")):
            try:
                file_date = datetime.strptime(f.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_date < cutoff:
                f.unlink()
                removed += 1
    return removed


def write_mokuroku(summary: dict[str, Any]) -> Path:
    """Write status/mokuroku.json: the 盤目録 display state.

    `color` and `alert` are driven by ng + stale: any >0 makes it red.
    """
    alert_count = summary["ng"] + summary["stale"]
    doc = {
        "generated_at": summary["generated_at"],
        "color": "red" if alert_count >= 1 else "green",
        "alert": alert_count >= 1,
        "alert_count": alert_count,
        "total": summary["total"],
        "ran_today": summary["ran_today"],
        "ok": summary["ok"],
        "warn": summary["warn"],
        "ng": summary["ng"],
        "stale": summary["stale"],
        "problems": summary["problems"],
        "problems_truncated": summary["problems_truncated"],
        "counts": summary.get("counts", {}),
    }
    path = common.STATUS_DIR / "mokuroku.json"
    common.write_json(path, doc)
    return path


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

    # Per-ban counts for the morning report. Includes every ban that ran
    # today, whether it's a problem or not — important for "sparse" boards
    # (e.g. kokoku-ban) where "0件" is normal and not a warn, but the
    # morning report still wants to show the number.
    counts: dict[str, dict[str, int]] = {}
    for ban in bans:
        row = rows_by_id.get(ban["id"])
        if row is None:
            continue
        counts[ban["id"]] = {
            "raw": int(row.get("raw_fetched", row.get("records", 0))),
            "after_filter": int(row.get("after_filter", row.get("records", 0))),
            "new": int(row.get("records_delta", row.get("records", 0))),
        }

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
        "counts": counts,
        "last_seen": {b["id"]: last_seen[b["id"]] for b in bans if b["id"] in last_seen},
    }
    common.write_json(common.STATUS_DIR / "summary.json", summary)
    write_mokuroku(summary)
    removed = prune_old_data(now)
    print(
        f"summary: total={summary['total']} ran_today={summary['ran_today']} "
        f"ok={summary['ok']} warn={summary['warn']} ng={summary['ng']} "
        f"stale={summary['stale']} problems={len(problems)} "
        f"(truncated={summary['problems_truncated']}) "
        f"pruned={removed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
