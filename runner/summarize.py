"""Summarizer: collect all shard status files into `status/summary.json`.

Runs after every shard finishes. Keeps the summary small even with 10k+ bans:
a few counters, a `last_seen` map (ban_id -> JST timestamp of the most
recent check), and at most 50 "problem" entries (ng/warn/stale). Anything
over that is counted but not listed.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import jpholiday

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import common  # noqa: E402

MAX_PROBLEMS = 50
DATA_RETENTION_DAYS = 30
# Warn only after this many consecutive JST workdays with the symptom —
# so weekends and holidays don't cause false alarms, and a single quiet
# workday doesn't either.
ZERO_FILTER_WORKDAY_THRESHOLD = 3
ZERO_NEW_WORKDAY_THRESHOLD = 3

# 年末年始の休み。jpholiday は 1/1 しか祝日扱いしないので、1/2・1/3 と
# 12/29・12/30・12/31 を同じ扱いにして稼働日から外す。毎年同じ日付。
YEAR_END_OFF_MMDD: set[tuple[int, int]] = {
    (12, 29), (12, 30), (12, 31), (1, 2), (1, 3),
}


def is_jst_workday(d: date) -> bool:
    """A JST weekday (Mon-Fri) that is neither a national holiday nor a
    year-end/new-year off day (12/29-31, 1/2-3)."""
    if d.weekday() >= 5:
        return False
    if jpholiday.is_holiday(d):
        return False
    if (d.month, d.day) in YEAR_END_OFF_MMDD:
        return False
    return True


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
             last_seen: dict[str, str], streak_zf: int = 0,
             streak_zn: int = 0) -> tuple[str, str]:
    """Return (status, reason). status ∈ {ok, warn, ng, stale}.

    streak_zf / streak_zn are the running workday-only streaks AS OF today
    (already including today's increment): consecutive JST workdays with
    after_filter=0, or with after_filter>0 and records_delta=0.
    """
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
        if row.get("broken_links", 0) > 0:
            return "warn", f"broken_links={row['broken_links']}"
        # The remaining signals (after_filter=0, records_delta=0 with
        # candidates) only mean "something is wrong" when they persist
        # across workdays — a single quiet Friday is not a problem, nor
        # is a 3-day holiday weekend. Streaks are advanced on JST workdays
        # only, so weekends/holidays never move them.
        after_filter = int(row.get("after_filter", row.get("records", 0)))
        records_delta = int(row.get("records_delta", 0))
        sparse = bool((ban.get("params") or {}).get("sparse"))
        if ban["freq"] == "daily":
            if after_filter == 0 and streak_zf >= ZERO_FILTER_WORKDAY_THRESHOLD:
                return "warn", f"after_filter=0 for {streak_zf} workdays"
            if after_filter > 0 and records_delta == 0 and not sparse:
                if streak_zn >= ZERO_NEW_WORKDAY_THRESHOLD:
                    return "warn", (
                        f"new=0 for {streak_zn} workdays (after_filter={after_filter})"
                    )
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
    # We only want per-shard status files here. `fetch-errors.jsonl` lives
    # in the same directory and matches `*.jsonl`, but its lines are error
    # records, not status rows — since c0d6e3b they also carry `ban`, so
    # without this skip the last 429 row for a given ban overwrites its
    # real status row and the ban comes out as `source_ok=false (no detail)`.
    rows_by_id: dict[str, dict[str, Any]] = {}
    for p in sorted(common.STATUS_DIR.glob("*.jsonl")):
        if p.name == "fetch-errors.jsonl":
            continue
        for row in common.read_status_shard(p):
            if row.get("ban"):
                rows_by_id[row["ban"]] = row

    # Previous summary's last_seen map (so unscheduled bans can age into stale)
    # and previous streaks (workday-only consecutive-zero counters per ban).
    prev = common.read_json(common.STATUS_DIR / "summary.json") or {}
    last_seen: dict[str, str] = dict(prev.get("last_seen") or {})
    prev_streaks: dict[str, dict[str, int]] = dict(prev.get("streaks") or {})
    for bid, row in rows_by_id.items():
        if row.get("source_ok") and row.get("checked_at"):
            last_seen[bid] = row["checked_at"]

    # Advance streaks only on JST workdays (weekday & non-holiday). On a
    # weekend or a national holiday the previous streak values carry over
    # unchanged, so e.g. a Fri -> Mon run across Sat/Sun/祝 is treated as
    # one workday step and a 6-day 年末年始 休業 never moves the counters.
    today_wd = is_jst_workday(now.date())
    streaks: dict[str, dict[str, int]] = {}

    counters = {"ok": 0, "warn": 0, "ng": 0, "stale": 0, "disabled": 0}
    problems: list[dict[str, Any]] = []
    ran_today = 0
    for ban in bans:
        bid = ban["id"]
        # 停止中（registry の enabled=false）の盤は classify に入れず、
        # 専用の disabled カウンタで数える。streak も進めない。
        if not ban["enabled"]:
            counters["disabled"] += 1
            continue
        row = rows_by_id.get(bid)
        if row is not None:
            ran_today += 1
        prev_zf = int((prev_streaks.get(bid) or {}).get("zero_filter", 0))
        prev_zn = int((prev_streaks.get(bid) or {}).get("zero_new", 0))
        if row is None or not today_wd:
            zf, zn = prev_zf, prev_zn
        else:
            af = int(row.get("after_filter", row.get("records", 0)))
            nd = int(row.get("records_delta", 0))
            if af == 0:
                # No candidates today — zero_new's "candidates existed" chain
                # breaks, so reset it; zero_filter advances.
                zf = prev_zf + 1
                zn = 0
            else:
                zf = 0
                zn = prev_zn + 1 if nd == 0 else 0
        streaks[bid] = {"zero_filter": zf, "zero_new": zn}
        status, reason = classify(ban, row, now, last_seen, zf, zn)
        counters[status] += 1
        if status in ("warn", "ng", "stale") and len(problems) < MAX_PROBLEMS:
            problems.append({
                "ban": bid,
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
        "disabled": counters["disabled"],
        "problems": problems,
        "problems_truncated": max(truncated, 0),
        "counts": counts,
        "last_seen": {b["id"]: last_seen[b["id"]] for b in bans if b["id"] in last_seen},
        "streaks": {b["id"]: streaks[b["id"]] for b in bans if b["id"] in streaks},
    }
    common.write_json(common.STATUS_DIR / "summary.json", summary)
    write_mokuroku(summary)
    removed = prune_old_data(now)
    print(
        f"summary: total={summary['total']} ran_today={summary['ran_today']} "
        f"ok={summary['ok']} warn={summary['warn']} ng={summary['ng']} "
        f"stale={summary['stale']} disabled={summary['disabled']} "
        f"problems={len(problems)} "
        f"(truncated={summary['problems_truncated']}) "
        f"pruned={removed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
