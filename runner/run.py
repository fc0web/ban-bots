"""Common runner: process one shard of the registry.

Reads `registry/bans.csv`, picks the bans assigned to this shard, dispatches
each to its source bot, and writes:
  - data/<ban_id>/YYYY-MM-DD.json  (per-ban fetch results)
  - status/<shard>.jsonl           (one line per ban this shard processed)

Each ban is isolated: one failing ban does not stop the others. Failures
still get a `source_ok: false` status line so nothing silently disappears.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import common  # noqa: E402


def process_ban(ban: dict[str, Any], today_str: str) -> dict[str, Any]:
    ban_id = ban["id"]
    source = ban["source"]
    params = ban["params"]

    all_records: list[dict[str, Any]] = []
    per_source_stats: dict[str, dict[str, int]] = {}
    notes: list[str] = []
    ok = True

    for component in common.source_components(source):
        sub = common.params_for_component(source, component, params)
        try:
            raw = common.fetch_source(component, sub)
            if isinstance(raw, dict):
                recs = raw.get("records") or []
                raw_total = int(raw.get("raw_total", len(recs)))
                after_filter = int(raw.get("after_filter", len(recs)))
            else:
                recs = list(raw)
                raw_total = len(recs)
                after_filter = len(recs)
            per_source_stats[component] = {
                "raw": raw_total,
                "after_filter": after_filter,
            }
            all_records.extend(recs)
        except Exception as e:
            ok = False
            per_source_stats[component] = {"raw": 0, "after_filter": 0}
            notes.append(f"{component}:{type(e).__name__}:{str(e)[:120]}")

    # Cross-day de-dup by id. Rule: only drop items whose seen-set entry
    # points to a PRIOR day. Items first seen today (= whose seen entry is
    # today, from an earlier same-day run, or newly added this run) stay in
    # today's candidate set. This fixes the same-day re-run bug where the
    # data file was being overwritten to zero.
    seen = common.load_seen(ban_id)

    # Preserve any already-saved records for today (union across same-day
    # runs) so a feed that scrolled between runs doesn't lose items.
    existing_today = common.read_json(common.data_path(ban_id, today_str)) or {}
    kept_today: list[dict[str, Any]] = list(existing_today.get("records") or [])
    kept_ids: set[str] = {str(r.get("id")) for r in kept_today if r.get("id")}

    newly_added_this_run = 0
    seen_previously = 0
    for rec in all_records:
        rid = str(rec.get("id") or "")
        if not rid:
            continue
        first_seen = seen.get(rid, "")
        if first_seen and first_seen < today_str:
            # Seen on a prior day — drop.
            seen_previously += 1
            continue
        if rid in kept_ids:
            # Already stored by an earlier same-day run.
            continue
        kept_today.append(rec)
        kept_ids.add(rid)
        seen.setdefault(rid, today_str)
        newly_added_this_run += 1
    common.save_seen(ban_id, seen)

    raw_fetched = sum(s["raw"] for s in per_source_stats.values())
    after_filter_sum = sum(s["after_filter"] for s in per_source_stats.values())

    # The data file stores today's candidates (= items first seen today),
    # built by union across the day's runs. Previously-seen items are
    # counted (seen_previously) but not stored — they live in the earlier
    # day's file.
    data_obj = {
        "ban": ban_id,
        "fetched_at": common.iso_jst(),
        "source": source,
        "params": params,
        "fetch_stats": {
            "by_source": per_source_stats,
            "raw_fetched": raw_fetched,
            "after_filter": after_filter_sum,
            "seen_previously": seen_previously,
            "new_today": len(kept_today),
            "newly_added_this_run": newly_added_this_run,
        },
        "records": kept_today,
    }
    common.write_json(common.data_path(ban_id, today_str), data_obj)
    # Page-facing feed: aggregates recent days' candidates, capped.
    common.write_latest(ban_id)

    checked_at = common.iso_jst()
    return common.make_status_record(
        ban_id=ban_id,
        checked_at=checked_at,
        records=len(kept_today),
        records_delta=len(kept_today),
        source_ok=ok,
        note="; ".join(notes),
        raw_fetched=raw_fetched,
        after_filter=after_filter_sum,
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=int, required=True, help="shard index, 0-based")
    p.add_argument("--total", type=int, required=True, help="total shard count")
    args = p.parse_args()

    if args.total < 1 or args.shard < 0 or args.shard >= args.total:
        print(f"bad shard args: shard={args.shard} total={args.total}", file=sys.stderr)
        return 2

    today = common.now_jst()
    today_str = common.today_jst_str(today)

    bans = common.load_registry()
    assigned = [
        b for i, b in enumerate(bans)
        if common.shard_of(i, args.total) == args.shard
    ]
    scheduled = [b for b in assigned if common.should_run_today(b, today)]

    print(
        f"shard {args.shard}/{args.total}: "
        f"{len(assigned)} assigned, {len(scheduled)} scheduled today"
    )

    rows: list[dict[str, Any]] = []
    for ban in scheduled:
        print(f"[{ban['id']}] source={ban['source']} ...", flush=True)
        try:
            row = process_ban(ban, today_str)
        except Exception as e:
            # Last-resort guard: process_ban should never raise, but if it
            # does, we still record a failure row.
            traceback.print_exc()
            row = common.make_status_record(
                ban_id=ban["id"],
                checked_at=common.iso_jst(),
                records=0,
                records_delta=0,
                source_ok=False,
                note=f"runner:{type(e).__name__}:{str(e)[:120]}",
            )
        rows.append(row)
        print(
            f"[{ban['id']}] ok={row['source_ok']} "
            f"raw={row.get('raw_fetched', '-')} "
            f"filtered={row.get('after_filter', '-')} "
            f"new_today={row['records']} "
            f"note={row['note']!r}",
            flush=True,
        )

    path = common.write_status_shard(args.shard, rows)
    print(f"wrote {path} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
