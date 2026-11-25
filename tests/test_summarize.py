"""Tests for runner/summarize.py.

Focus: the `disabled` counter. Boards whose registry row has enabled=false
must not be tallied into ok/warn/ng/stale, must not advance streaks, and
must not appear in `problems`.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner import common, summarize  # noqa: E402

JST = timezone(timedelta(hours=9))


# --- helpers --------------------------------------------------------------

def _write_registry(path: Path, rows: list[dict]) -> None:
    """Write a tiny registry CSV (same columns as registry/bans.csv)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "id", "name_ja", "name_en", "source", "params", "freq",
        "weekday", "enabled", "owner", "page_url",
    ]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([str(r.get(c, "")) for c in cols])
    path.write_text(buf.getvalue(), encoding="utf-8")


def _row(ban: str, *, af: int = 0, new: int = 0, source_ok: bool = True,
         note: str = "", broken: int = 0, mismatch: int = 0,
         date_jst: str = "2026-11-25",
         checked_at: str = "2026-11-25T13:00:00+09:00") -> dict:
    return {
        "ban": ban,
        "checked_at": checked_at,
        "date_jst": date_jst,
        "records": new,
        "records_delta": new,
        "raw_fetched": af,
        "after_filter": af,
        "source_ok": source_ok,
        "mismatch": mismatch,
        "broken_links": broken,
        "license_changed": False,
        "note": note,
        "fetch_errors": 0,
    }


@pytest.fixture
def summary_env(tmp_path, monkeypatch):
    """Fresh tmp repo layout, with summarize wired to it."""
    status = tmp_path / "status"
    data = tmp_path / "data"
    registry = tmp_path / "registry" / "bans.csv"
    status.mkdir()
    data.mkdir()
    monkeypatch.setattr(common, "STATUS_DIR", status)
    monkeypatch.setattr(common, "DATA_DIR", data)
    monkeypatch.setattr(common, "REGISTRY_PATH", registry)
    # load_registry()'s default path arg is captured at def-time, so patch
    # the function itself to always read our tmp registry.
    orig_load = common.load_registry
    monkeypatch.setattr(common, "load_registry",
                        lambda path=registry: orig_load(path))
    # Freeze "now" to a known JST workday (Wed 2026-11-25 13:59).
    monkeypatch.setattr(common, "now_jst",
                        lambda: datetime(2026, 11, 25, 13, 59, 0, tzinfo=JST))
    return {"status": status, "data": data, "registry": registry}


def _write_shard(status_dir: Path, rows: list[dict]) -> None:
    (status_dir / "0.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _write_prev_summary(status_dir: Path, obj: dict) -> None:
    (status_dir / "summary.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# --- tests ----------------------------------------------------------------

def test_disabled_counted_separately_not_as_ok(summary_env):
    """A registry with 2 enabled + 1 disabled should produce disabled=1 and
    the disabled board must NOT be in ok/warn/ng/stale or in streaks."""
    _write_registry(summary_env["registry"], [
        {"id": "a", "source": "none", "params": "{}", "freq": "daily", "enabled": "true"},
        {"id": "b", "source": "none", "params": "{}", "freq": "daily", "enabled": "true"},
        {"id": "z", "source": "selfgen", "params": "{}", "freq": "daily", "enabled": "false"},
    ])
    _write_shard(summary_env["status"], [
        _row("a", af=5, new=1),
        _row("b", af=3, new=0),  # sparse=false, not enough for warn (streak 1)
    ])
    summarize.main()
    s = json.loads((summary_env["status"] / "summary.json").read_text(encoding="utf-8"))
    assert s["total"] == 3
    assert s["ran_today"] == 2
    assert s["disabled"] == 1
    assert s["ok"] == 2
    assert s["warn"] == 0 and s["ng"] == 0 and s["stale"] == 0
    assert "z" not in s["streaks"], "disabled board must not have a streak entry"
    assert all(p["ban"] != "z" for p in s["problems"])


def test_disabled_with_last_seen_still_disabled(summary_env):
    """A board that used to run (prev summary carries last_seen for it) but is
    now enabled=false must be counted as disabled, not as ok (via the no-row
    fallback) and not as stale (via the >1.5x freq fallback)."""
    _write_registry(summary_env["registry"], [
        {"id": "b", "source": "none", "params": "{}", "freq": "daily", "enabled": "false"},
    ])
    # Previous summary — b was active yesterday with last_seen and a streak.
    _write_prev_summary(summary_env["status"], {
        "generated_at": "2026-11-24T13:59:00+09:00",
        "total": 1, "ran_today": 1,
        "ok": 1, "warn": 0, "ng": 0, "stale": 0,
        "problems": [], "problems_truncated": 0,
        "counts": {"b": {"raw": 3, "after_filter": 3, "new": 0}},
        "last_seen": {"b": "2026-11-24T13:00:00+09:00"},
        "streaks": {"b": {"zero_filter": 0, "zero_new": 1}},
    })
    _write_shard(summary_env["status"], [])  # no shard rows this run
    summarize.main()
    s = json.loads((summary_env["status"] / "summary.json").read_text(encoding="utf-8"))
    assert s["total"] == 1
    assert s["ran_today"] == 0
    assert s["disabled"] == 1
    assert s["ok"] == 0
    assert s["warn"] == 0 and s["ng"] == 0 and s["stale"] == 0
    # The previously-maintained streak entry for b must be dropped — classify
    # was skipped entirely for the disabled board.
    assert "b" not in s["streaks"]


def test_enabled_but_no_row_is_stale_not_disabled(summary_env):
    """Guardrail: enabled=true with no row today is still stale, not
    disabled — the `disabled` bucket is strictly for the registry flag."""
    _write_registry(summary_env["registry"], [
        {"id": "a", "source": "none", "params": "{}", "freq": "daily", "enabled": "true"},
    ])
    _write_shard(summary_env["status"], [])
    summarize.main()
    s = json.loads((summary_env["status"] / "summary.json").read_text(encoding="utf-8"))
    assert s["total"] == 1
    assert s["disabled"] == 0
    assert s["stale"] == 1
