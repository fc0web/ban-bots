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


SEEN_TTL_DAYS = 730  # 2 years — bound the seen set size
LATEST_JSON_CAP = 50  # max records in data/<ban>/latest.json


def dedup_key(s: str) -> str:
    """Normalize a record id for cross-day/same-day dedup.

    Two URL ids that differ only by scheme (http vs https) or by a
    trailing slash should hash to the same key — otherwise the same
    news item republished via a feed URL update ends up as two
    "different" candidates.
    """
    if not s:
        return ""
    s = str(s)
    low = s.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            s = s[len(prefix):]
            break
    while s.endswith("/"):
        s = s[:-1]
    return s


def _seen_path(ban_id: str) -> Path:
    # `_seen.json` starts with "_" so retention pruning (which looks for
    # YYYY-MM-DD.json) never touches it.
    return DATA_DIR / ban_id / "_seen.json"


def load_seen(ban_id: str, today: datetime | None = None) -> dict[str, str]:
    """Load the per-ban seen-id map: {dedup_key(id): "YYYY-MM-DD" when first seen}.

    Keys are stored in `dedup_key` normalized form (scheme-stripped,
    trailing-slash-stripped) so http<->https drift on the same URL does
    not create duplicate candidates. Legacy entries in the older
    non-normalized form are migrated on load; if two legacy keys collide
    on their normalized form, the earlier first-seen date wins.

    Entries older than SEEN_TTL_DAYS are dropped on load. Returns a
    mutable dict; callers add new ids and then call save_seen.
    """
    obj = read_json(_seen_path(ban_id)) or {}
    raw = obj.get("ids") if isinstance(obj, dict) else None
    ids: dict[str, str] = {}
    if isinstance(raw, dict):
        if today is None:
            today = now_jst()
        cutoff = today.date() - timedelta(days=SEEN_TTL_DAYS)
        for k, v in raw.items():
            if not isinstance(v, str):
                continue
            try:
                d = datetime.strptime(v, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff:
                continue
            nk = dedup_key(str(k))
            if not nk:
                continue
            prev = ids.get(nk)
            if prev is None or v < prev:
                ids[nk] = v
    return ids


def save_seen(ban_id: str, ids: dict[str, str]) -> None:
    write_json(_seen_path(ban_id), {"ids": ids})


# ---------------------------------------------------------------------------
# latest.json — a page-facing feed of the most recent candidates
# ---------------------------------------------------------------------------

def _latest_path(ban_id: str) -> Path:
    # `latest.json` doesn't match the YYYY-MM-DD.json retention pattern, so
    # prune_old_data in summarize.py never touches it.
    return DATA_DIR / ban_id / "latest.json"


def write_latest(ban_id: str, cap: int = LATEST_JSON_CAP) -> Path | None:
    """Write data/<ban>/latest.json: the most recent `cap` records across
    the retained daily data files, newest day first.

    Intended for page-side consumption (client-side fetch over
    raw.githubusercontent.com). Deterministic from the current state of
    data/<ban>/*.json, so same-day repeat runs produce the same file.
    """
    folder = DATA_DIR / ban_id
    if not folder.exists():
        return None
    all_records: list[dict[str, Any]] = []
    for f in sorted(folder.glob("*.json"), reverse=True):
        if f.name in ("latest.json", "_seen.json"):
            continue
        try:
            datetime.strptime(f.stem, "%Y-%m-%d")
        except ValueError:
            continue
        obj = read_json(f) or {}
        day = f.stem
        for rec in (obj.get("records") or []):
            rec = dict(rec)
            rec["first_seen_date"] = day
            all_records.append(rec)
    capped = all_records[:cap]
    doc: dict[str, Any] = {
        "ban": ban_id,
        "generated_at": iso_jst(),
        "cap": cap,
        "count": len(capped),
        "total_before_cap": len(all_records),
        "records": capped,
    }
    path = _latest_path(ban_id)
    write_json(path, doc)
    return path


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
    raw_fetched: int | None = None,
    after_filter: int | None = None,
    fetch_errors: int = 0,
) -> dict[str, Any]:
    row: dict[str, Any] = {
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
        "fetch_errors": int(fetch_errors),
    }
    if raw_fetched is not None:
        row["raw_fetched"] = int(raw_fetched)
    if after_filter is not None:
        row["after_filter"] = int(after_filter)
    return row


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


FETCH_ERRORS_PATH = STATUS_DIR / "fetch-errors.jsonl"


def append_fetch_errors(rows: Iterable[dict[str, Any]]) -> None:
    """Append one JSON line per fetch error to status/fetch-errors.jsonl.

    Each row should carry at least `time` (JST ISO-8601 with offset,
    measured at the moment the error occurred), `source` (e.g. "openalex"),
    `phrase` (the specific search phrase; "" for non-phrase sources), and
    `error` ("<ClassName>: <message>" truncated).

    This file lets the daily digest tell a genuine "0 hits" apart from a
    swallowed exception. It is append-only; one line per failure.
    """
    rows = list(rows)
    if not rows:
        return
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    with FETCH_ERRORS_PATH.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True))
            f.write("\n")
