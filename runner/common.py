"""Shared helpers for the runner and summarizer."""

from __future__ import annotations

import csv
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

JST = timezone(timedelta(hours=9))

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "registry" / "bans.csv"
DATA_DIR = REPO_ROOT / "data"
STATUS_DIR = REPO_ROOT / "status"
SOURCES_DIR = REPO_ROOT / "sources"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def now_jst() -> datetime:
    return datetime.now(JST)


def iso_jst(dt: datetime | None = None) -> str:
    if dt is None:
        dt = now_jst()
    return dt.isoformat(timespec="seconds")


def today_jst_str(dt: datetime | None = None) -> str:
    if dt is None:
        dt = now_jst()
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_WEEKDAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


def _parse_bool(s: str) -> bool:
    return str(s).strip().lower() in ("true", "1", "yes", "y", "t")


def load_registry(path: Path = REGISTRY_PATH) -> list[dict[str, Any]]:
    bans: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("id"):
                continue
            params_str = (row.get("params") or "").strip()
            try:
                params = json.loads(params_str) if params_str else {}
            except json.JSONDecodeError as e:
                raise ValueError(f"bad params JSON for ban {row['id']}: {e}") from e
            bans.append({
                "id": row["id"].strip(),
                "name_ja": (row.get("name_ja") or "").strip(),
                "name_en": (row.get("name_en") or "").strip(),
                "source": (row.get("source") or "").strip(),
                "params": params,
                "freq": (row.get("freq") or "daily").strip().lower(),
                "weekday": (row.get("weekday") or "").strip().lower(),
                "enabled": _parse_bool(row.get("enabled") or ""),
                "owner": (row.get("owner") or "").strip(),
                "page_url": (row.get("page_url") or "").strip(),
            })
    bans.sort(key=lambda b: b["id"])
    return bans


def should_run_today(ban: dict[str, Any], today: datetime | None = None) -> bool:
    """Is this ban scheduled to run on `today` (JST)?"""
    if not ban["enabled"]:
        return False
    if today is None:
        today = now_jst()
    freq = ban["freq"]
    if freq == "daily":
        return True
    wd = _WEEKDAY_MAP.get(ban["weekday"], -1)
    if wd < 0:
        return False
    if today.weekday() != wd:
        return False
    if freq == "weekly":
        return True
    if freq == "monthly":
        return today.day <= 7  # first <weekday> of the month
    return False


def shard_of(index: int, total: int) -> int:
    return index % max(total, 1)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _load_source_module(name: str):
    path = SOURCES_DIR / name / "fetch.py"
    if not path.exists():
        raise FileNotFoundError(f"unknown source: {name} (no {path})")
    spec = importlib.util.spec_from_file_location(f"sources.{name}.fetch", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load source: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_source_cache: dict[str, Any] = {}


def fetch_source(name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Call one source's fetch() with the given params dict."""
    mod = _source_cache.get(name)
    if mod is None:
        mod = _load_source_module(name)
        _source_cache[name] = mod
    return mod.fetch(params or {})


def params_for_component(source: str, name: str, params: dict[str, Any]) -> dict[str, Any]:
    """For a composite source (e.g. "arxiv+zenodo"), return the sub-params
    for one component; for a single source, return the whole params."""
    components = source_components(source)
    if len(components) == 1:
        return params
    sub = params.get(name)
    return sub if isinstance(sub, dict) else {}


def source_components(source: str) -> list[str]:
    return [s for s in source.split("+") if s]


# ---------------------------------------------------------------------------
# Data and status files
# ---------------------------------------------------------------------------

def data_path(ban_id: str, date_str: str) -> Path:
    return DATA_DIR / ban_id / f"{date_str}.json"


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def previous_record_count(ban_id: str, today_str: str) -> int:
    """How many records did the most recent prior data file have? 0 if none."""
    folder = DATA_DIR / ban_id
    if not folder.exists():
        return 0
    prior = sorted(
        (p for p in folder.glob("*.json") if p.stem != today_str),
        reverse=True,
    )
    if not prior:
        return 0
    try:
        with prior[0].open("r", encoding="utf-8") as f:
            obj = json.load(f)
        recs = obj.get("records")
        return len(recs) if isinstance(recs, list) else 0
    except Exception:
        return 0


def last_checked_at(ban_id: str) -> str:
    """Most recent `checked_at` for this ban from past summary.json, or ""."""
    summary = read_json(STATUS_DIR / "summary.json")
    if not isinstance(summary, dict):
        return ""
    last_seen = (summary.get("last_seen") or {}).get(ban_id) or ""
    return last_seen if isinstance(last_seen, str) else ""


def freq_hours(freq: str) -> float:
    return {"daily": 24.0, "weekly": 24.0 * 7, "monthly": 24.0 * 30}.get(freq, 24.0)


# ---------------------------------------------------------------------------
# Status record
# ---------------------------------------------------------------------------

def make_status_record(
    ban_id: str,
    checked_at: str,
    records: int,
    records_delta: int,
    matched: int = 0,
    mismatch: int = 0,
    source_ok: bool = True,
    broken_links: int = 0,
    license_changed: bool = False,
    note: str = "",
) -> dict[str, Any]:
    return {
        "ban": ban_id,
        "checked_at": checked_at,
        "records": int(records),
        "records_delta": int(records_delta),
        "matched": int(matched),
        "mismatch": int(mismatch),
        "source_ok": bool(source_ok),
        "broken_links": int(broken_links),
        "license_changed": bool(license_changed),
        "note": note or "",
    }


def write_status_shard(shard: int, rows: Iterable[dict[str, Any]]) -> Path:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    path = STATUS_DIR / f"{shard}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    return path


def read_status_shard(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out
