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
    prev_count = common.previous_record_count(ban_id, today_str)

    all_records: list[dict[str, Any]] = []
    notes: list[str] = []
    ok = True
    for component in common.source_components(source):
        sub = common.params_for_component(source, component, params)
        try:
            recs = common.fetch_source(component, sub)
            all_records.extend(recs)
        except Exception as e:
            ok = False
            notes.append(f"{component}:{type(e).__name__}:{str(e)[:120]}")

    # Always write the data file (possibly empty on failure) so a reader can
    # see that we tried.
    data_obj = {
        "ban": ban_id,
        "fetched_at": common.iso_jst(),
        "source": source,
        "params": params,
        "records": all_records,
    }
    common.write_json(common.data_path(ban_id, today_str), data_obj)

    checked_at = common.iso_jst()
    return common.make_status_record(
        ban_id=ban_id,
        checked_at=checked_at,
        records=len(all_records),
        records_delta=len(all_records) - prev_count,
        source_ok=ok,
        note="; ".join(notes),
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
            f"records={row['records']} delta={row['records_delta']} "
            f"note={row['note']!r}",
            flush=True,
        )

    path = common.write_status_shard(args.shard, rows)
    print(f"wrote {path} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
