"""One-off backfill for status/history.jsonl from data/<ban>/<day>.json.

Why: the forecaster started on 2026-11-26, so history.jsonl only had 1 row
per ban on its first live run. That made the 80% empirical interval collapse
to a single point. We fill the past from the per-day data files that already
live in the repo.

Rules (from the 11/26 directive):
- af and new are read the same way summarize does it:
  `fetch_stats.after_filter` and `fetch_stats.new_today`.
- status is set to null for backfilled rows; we don't have a durable per-day
  status record before 11/26 and we don't make one up.
- Each backfilled row carries "backfilled": true.
- Before writing, we recompute today's rows from data files and make sure
  they match the lines history.jsonl already has for today; mismatch -> stop.
- Lines are written sorted by (date, ban) so the file stays stable.

Usage:
  python scripts/backfill_history.py --dry-run   # verify only, write nothing
  python scripts/backfill_history.py             # write after verifying
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner import common  # noqa: E402

JST = timezone(timedelta(hours=9))


def day_file_af_new(path: Path) -> tuple[int, int] | None:
    """Read af, new from a data/<ban>/<day>.json file, or None if unusable."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    fs = obj.get("fetch_stats") or {}
    if "after_filter" not in fs or "new_today" not in fs:
        return None
    try:
        return int(fs["after_filter"]), int(fs["new_today"])
    except (TypeError, ValueError):
        return None


def rows_from_data_dir(repo_root: Path, bans: list[dict]) -> list[dict]:
    """Return all per-day rows reconstructed from data/<ban>/<YYYY-MM-DD>.json."""
    rows: list[dict] = []
    for ban in bans:
        if not ban.get("enabled"):
            continue
        bid = ban["id"]
        bdir = repo_root / "data" / bid
        if not bdir.exists():
            continue
        for p in sorted(bdir.glob("*.json")):
            stem = p.stem
            if stem in ("latest", "_seen"):
                continue
            try:
                date.fromisoformat(stem)
            except ValueError:
                continue
            res = day_file_af_new(p)
            if res is None:
                continue
            af, new = res
            rows.append({
                "date": stem, "ban": bid,
                "af": af, "new": new,
                "status": None, "backfilled": True,
            })
    return rows


def load_history_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def verify_today_match(
    today_jst: str,
    reconstructed: list[dict],
    live_rows: list[dict],
) -> tuple[bool, list[str]]:
    """Compare af/new for today's rows against live history. Returns
    (ok, mismatch_messages)."""
    recon_today = {r["ban"]: (r["af"], r["new"]) for r in reconstructed
                   if r["date"] == today_jst}
    live_today = {r["ban"]: (int(r.get("af", 0)), int(r.get("new", 0)))
                  for r in live_rows if r.get("date") == today_jst}
    mismatches: list[str] = []
    all_bans = sorted(set(recon_today) | set(live_today))
    for bid in all_bans:
        r = recon_today.get(bid)
        l = live_today.get(bid)
        if r is None:
            mismatches.append(f"{bid}: in live but no reconstructed row")
        elif l is None:
            mismatches.append(f"{bid}: in reconstructed but not in live")
        elif r != l:
            mismatches.append(f"{bid}: live=(af={l[0]},new={l[1]}) "
                              f"reconstructed=(af={r[0]},new={r[1]})")
    return (len(mismatches) == 0 and len(all_bans) > 0), mismatches


def merge(live: list[dict], recon: list[dict], today_jst: str) -> list[dict]:
    """Keep live rows as-is (they already carry the real status). For
    reconstructed rows strictly before today, add them only if the live
    file has no row yet for that (date, ban) pair."""
    seen = {(r.get("date"), r.get("ban")) for r in live}
    merged = list(live)
    for r in recon:
        if r["date"] >= today_jst:
            continue  # today and future are live-only
        key = (r["date"], r["ban"])
        if key in seen:
            continue
        merged.append(r)
        seen.add(key)
    merged.sort(key=lambda r: (r.get("date", ""), r.get("ban", "")))
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Verify match only; do not write history.jsonl.")
    args = ap.parse_args()

    repo_root = common.REPO_ROOT
    today_jst = datetime.now(JST).strftime("%Y-%m-%d")

    bans = common.load_registry()
    enabled = [b["id"] for b in bans if b.get("enabled")]
    print(f"repo_root = {repo_root}")
    print(f"today (JST) = {today_jst}")
    print(f"enabled bans ({len(enabled)}): {', '.join(enabled)}")

    hist_path = repo_root / "status" / "history.jsonl"
    live = load_history_rows(hist_path)
    print(f"current history.jsonl rows: {len(live)}")

    recon = rows_from_data_dir(repo_root, bans)
    print(f"reconstructed rows from data/: {len(recon)}")

    ok, mismatches = verify_today_match(today_jst, recon, live)
    if not ok:
        print("MISMATCH for today — stopping.")
        for m in mismatches:
            print(f"  {m}")
        return 2
    print(f"today match: OK for {len(enabled)} bans")

    merged = merge(live, recon, today_jst)
    added = len(merged) - len(live)
    print(f"rows to add: {added}  (new total = {len(merged)})")

    if args.dry_run:
        print("dry-run: not writing.")
        return 0

    hist_path.parent.mkdir(parents=True, exist_ok=True)
    with hist_path.open("w", encoding="utf-8") as f:
        for r in merged:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    print(f"wrote {hist_path}")
    return 0


if __name__ == "__main__":
    # Re-wrap stdout here (not at import time) so pytest-driven imports of
    # this module don't replace the test runner's stdout.
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    raise SystemExit(main())
