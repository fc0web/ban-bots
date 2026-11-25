"""機器 40: 予測器 (forecaster).

Called once per daily summarize run from runner/summarize.main().

Inputs come from the current summarize step:
- rows_by_id: today's per-ban status rows (after_filter, records_delta, ...)
- statuses:   today's per-ban classification (from classify())

Side effects:
- Appends/replaces today's lines in status/history.jsonl
- Scores predictions/<today>.json if present, writing predictions/<today>-score.json
- Writes predictions/<tomorrow>.json (immutable once committed)

Returns a dict embedded in status/summary.json under the "forecast" key.

Not novel: empirical quantiles for ranges, Brier for class probabilities,
persistence and median baselines. See docs/40-41-design.md.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from runner import common

HISTORY_WINDOW_DAYS = 30
BASELINE_MED_DAYS = 7
CALIBRATION_WORKDAYS = 14
QUANTILE_LEVEL = 0.80
STATUS_CLASSES: tuple[str, ...] = ("ok", "warn", "ng", "stale")

PREDICTIONS_DIR_NAME = "predictions"
HISTORY_FILE_NAME = "history.jsonl"


# --- paths --------------------------------------------------------------

def predictions_dir(repo_root: Path) -> Path:
    return repo_root / PREDICTIONS_DIR_NAME


def history_path(repo_root: Path) -> Path:
    return repo_root / "status" / HISTORY_FILE_NAME


# --- history ------------------------------------------------------------

def load_history(repo_root: Path) -> list[dict[str, Any]]:
    """Read history.jsonl. Each line carries {date, ban, af, new, status}.
    Returns the rows in the order they appear in the file."""
    p = history_path(repo_root)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def append_today_history(
    repo_root: Path,
    today_jst: str,
    rows_by_id: dict[str, dict[str, Any]],
    statuses: dict[str, str],
) -> None:
    """Replace today's lines in history.jsonl (idempotent on re-run)."""
    path = history_path(repo_root)
    kept = [r for r in load_history(repo_root) if r.get("date") != today_jst]
    for bid, status in statuses.items():
        row = rows_by_id.get(bid)
        if row is None:
            continue
        kept.append({
            "date": today_jst,
            "ban": bid,
            "af": int(row.get("after_filter", row.get("records", 0))),
            "new": int(row.get("records_delta", 0)),
            "status": status,
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _history_for_ban(
    history: list[dict[str, Any]],
    ban_id: str,
    before_date: str,
    max_days: int,
) -> list[dict[str, Any]]:
    """Most-recent `max_days` rows for ban strictly before `before_date`,
    sorted by date ascending (newest last)."""
    rows = [r for r in history
            if r.get("ban") == ban_id and r.get("date", "") < before_date]
    rows.sort(key=lambda r: r["date"])
    return rows[-max_days:]


# --- numerical helpers --------------------------------------------------

def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    pos = q * (len(vs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    frac = pos - lo
    return vs[lo] * (1 - frac) + vs[hi] * frac


def _empirical_interval(values: list[float], level: float) -> tuple[float, float]:
    tail = (1 - level) / 2
    return _quantile(values, tail), _quantile(values, 1 - tail)


def _laplace_probs(labels: list[str], classes: tuple[str, ...]) -> dict[str, float]:
    counts = {c: 1 for c in classes}  # +1 smoothing
    for lab in labels:
        if lab in counts:
            counts[lab] += 1
    total = sum(counts.values())
    return {c: counts[c] / total for c in classes}


def _mode(labels: list[str], default: str = "ok") -> str:
    if not labels:
        return default
    # Pick the most frequent; ties broken by insertion order via Python Counter.
    from collections import Counter
    return Counter(labels).most_common(1)[0][0]


# --- prediction ---------------------------------------------------------

def _predict_one(ban_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    afs = [float(r["af"]) for r in rows]
    nds = [float(r["new"]) for r in rows]
    statuses = [r.get("status", "ok") for r in rows]

    af_low, af_high = _empirical_interval(afs, QUANTILE_LEVEL)
    nd_low, nd_high = _empirical_interval(nds, QUANTILE_LEVEL)

    if rows:
        last = rows[-1]
        baseline_last = {
            "after_filter": int(last["af"]),
            "records_delta": int(last["new"]),
            "status": last.get("status", "ok"),
        }
    else:
        baseline_last = {"after_filter": 0, "records_delta": 0, "status": "ok"}

    recent7 = rows[-BASELINE_MED_DAYS:]
    if recent7:
        af7 = [float(r["af"]) for r in recent7]
        nd7 = [float(r["new"]) for r in recent7]
        baseline_med7 = {
            "after_filter_median": _quantile(af7, 0.5),
            "records_delta_median": _quantile(nd7, 0.5),
            "after_filter_range": [min(af7), max(af7)],
            "records_delta_range": [min(nd7), max(nd7)],
            "status_mode": _mode([r.get("status", "ok") for r in recent7]),
        }
    else:
        baseline_med7 = {}

    return {
        "ban": ban_id,
        "history_days": len(rows),
        "after_filter": {
            "low": af_low, "high": af_high,
            "median": _quantile(afs, 0.5),
            "method": f"empirical_{int(QUANTILE_LEVEL * 100)}pct",
        },
        "records_delta": {
            "low": nd_low, "high": nd_high,
            "median": _quantile(nds, 0.5),
            "method": f"empirical_{int(QUANTILE_LEVEL * 100)}pct",
        },
        "status_probs": _laplace_probs(statuses, STATUS_CLASSES),
        "baselines": {"last": baseline_last, "med7": baseline_med7},
    }


def build_predictions(
    target_date: str,
    bans: list[dict[str, Any]],
    history: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "target_date": target_date,
        "generated_at": common.iso_jst(now),
        "history_window_days": HISTORY_WINDOW_DAYS,
        "quantile_level": QUANTILE_LEVEL,
        "status_classes": list(STATUS_CLASSES),
        "bans": {},
    }
    for ban in bans:
        if not ban["enabled"]:
            continue
        bid = ban["id"]
        rows = _history_for_ban(history, bid, target_date, HISTORY_WINDOW_DAYS)
        out["bans"][bid] = _predict_one(bid, rows)
    return out


# --- fingerprint check --------------------------------------------------

def fingerprint_check(repo_root: Path, rel_path: str) -> dict[str, Any]:
    """Verify `rel_path` was committed once and matches HEAD.

    Returns {ok, detail}. If git calls fail (e.g. in a non-git tmp dir used
    by tests), returns ok=False with a clear detail — the caller logs it and
    skips scoring.
    """
    try:
        logp = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--format=%H", "--", rel_path],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "detail": "git not available"}
    if logp.returncode != 0:
        return {"ok": False,
                "detail": f"git-log rc={logp.returncode}: {logp.stderr.strip()[:200]}"}
    hashes = [h for h in logp.stdout.splitlines() if h.strip()]
    if len(hashes) == 0:
        return {"ok": False, "detail": "not committed"}
    if len(hashes) > 1:
        return {"ok": False,
                "detail": f"file rewritten in {len(hashes)} commits"}
    diffp = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--quiet", "HEAD", "--", rel_path],
        check=False,
    )
    if diffp.returncode != 0:
        return {"ok": False, "detail": "working tree differs from HEAD"}
    return {"ok": True, "detail": "ok"}


# --- scoring ------------------------------------------------------------

def brier_score(probs: dict[str, float], actual: str,
                classes: tuple[str, ...] = STATUS_CLASSES) -> float:
    total = 0.0
    for c in classes:
        p = float(probs.get(c, 0.0))
        y = 1.0 if c == actual else 0.0
        total += (p - y) ** 2
    return total


def _range_hit_miss(low: float, high: float, actual: float) -> tuple[int, float]:
    if low <= actual <= high:
        return 1, 0.0
    return 0, min(abs(actual - low), abs(actual - high))


def score_one(pred: dict[str, Any], af_actual: float, nd_actual: float,
              status_actual: str) -> dict[str, Any]:
    """Score one ban's prediction against observed values."""
    af_hit, af_miss = _range_hit_miss(
        pred["after_filter"]["low"], pred["after_filter"]["high"], af_actual)
    nd_hit, nd_miss = _range_hit_miss(
        pred["records_delta"]["low"], pred["records_delta"]["high"], nd_actual)
    brier = brier_score(pred.get("status_probs", {}), status_actual)

    # baseline-last (persistence)
    last = pred.get("baselines", {}).get("last", {}) or {}
    last_af = float(last.get("after_filter", 0))
    last_nd = float(last.get("records_delta", 0))
    last_status = last.get("status", "ok")
    base_last = {
        "af_hit": int(last_af == af_actual),
        "af_miss": abs(last_af - af_actual),
        "nd_hit": int(last_nd == nd_actual),
        "nd_miss": abs(last_nd - nd_actual),
        "brier": brier_score(
            {c: 1.0 if c == last_status else 0.0 for c in STATUS_CLASSES},
            status_actual),
    }

    # baseline-med7 (seven-day range + modal status)
    med7 = pred.get("baselines", {}).get("med7", {}) or {}
    if med7:
        af_r = med7.get("after_filter_range", [0.0, 0.0])
        nd_r = med7.get("records_delta_range", [0.0, 0.0])
        mh, mm = _range_hit_miss(float(af_r[0]), float(af_r[1]), af_actual)
        nh, nm = _range_hit_miss(float(nd_r[0]), float(nd_r[1]), nd_actual)
        mode = med7.get("status_mode", "ok")
        base_med7: dict[str, Any] | None = {
            "af_hit": mh, "af_miss": mm,
            "nd_hit": nh, "nd_miss": nm,
            "brier": brier_score(
                {c: 1.0 if c == mode else 0.0 for c in STATUS_CLASSES},
                status_actual),
        }
    else:
        base_med7 = None

    return {
        "actual": {"after_filter": af_actual,
                   "records_delta": nd_actual,
                   "status": status_actual},
        "forecaster": {"af_hit": af_hit, "af_miss": af_miss,
                       "nd_hit": nd_hit, "nd_miss": nd_miss,
                       "brier": brier},
        "baseline_last": base_last,
        "baseline_med7": base_med7,
    }


# --- calibration --------------------------------------------------------

def _load_prior_scores(
    repo_root: Path,
    bans: list[dict[str, Any]],
    before_date: str,
) -> dict[str, list[dict[str, Any]]]:
    """Read the per-ban stream of previous score files, newest last."""
    d = predictions_dir(repo_root)
    out: dict[str, list[dict[str, Any]]] = {b["id"]: [] for b in bans}
    if not d.exists():
        return out
    files = sorted(d.glob("*-score.json"))
    for f in files:
        stem = f.stem.rsplit("-score", 1)[0]
        if stem >= before_date:
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not doc.get("fingerprint_check", {}).get("ok", False):
            continue
        for bid, per in (doc.get("per_ban") or {}).items():
            if bid in out:
                out[bid].append({"date": stem, **per})
    return out


def _cmp_lower_is_better(a: float | None, b: float | None) -> str:
    if a is None or b is None:
        return "unknown"
    if a < b:
        return "win"
    if a > b:
        return "loss"
    return "even"


def calibration_for_ban(prior: list[dict[str, Any]],
                        window: int = CALIBRATION_WORKDAYS) -> dict[str, Any]:
    """Compute rolling-window calibration stats from the last `window`
    score rows for one ban."""
    recent = prior[-window:]
    n = len(recent)
    if n == 0:
        return {"days_scored": 0, "status": "uncalibrated",
                "vs_last": "unknown", "vs_med7": "unknown",
                "hit_rate_ranges": None, "proximity_to_80pct": None}

    af_hits = [r["forecaster"]["af_hit"] for r in recent]
    nd_hits = [r["forecaster"]["nd_hit"] for r in recent]
    hits = af_hits + nd_hits
    hit_rate = sum(hits) / len(hits)
    proximity = 1.0 - abs(hit_rate - QUANTILE_LEVEL)

    briers = [r["forecaster"]["brier"] for r in recent]
    avg_brier = sum(briers) / n

    last_briers = [r["baseline_last"]["brier"] for r in recent
                   if r.get("baseline_last")]
    avg_last = sum(last_briers) / len(last_briers) if last_briers else None

    med7_briers = [r["baseline_med7"]["brier"] for r in recent
                   if r.get("baseline_med7")]
    avg_med7 = sum(med7_briers) / len(med7_briers) if med7_briers else None

    status = "calibrated" if n >= window else "uncalibrated"
    return {
        "days_scored": n,
        "status": status,
        "forecaster_avg_brier": avg_brier,
        "baseline_last_avg_brier": avg_last,
        "baseline_med7_avg_brier": avg_med7,
        "vs_last": _cmp_lower_is_better(avg_brier, avg_last),
        "vs_med7": _cmp_lower_is_better(avg_brier, avg_med7),
        "hit_rate_ranges": hit_rate,
        "proximity_to_80pct": proximity,
    }


# --- top-level entry ----------------------------------------------------

def run_forecast(
    now: datetime,
    bans: list[dict[str, Any]],
    rows_by_id: dict[str, dict[str, Any]],
    statuses: dict[str, str],
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Called from summarize.main() once counters are built.

    1. Append today's row to status/history.jsonl.
    2. If predictions/<today>.json exists, verify fingerprint and score it;
       write predictions/<today>-score.json.
    3. Build predictions/<tomorrow>.json.
    4. Compute per-ban calibration from prior scored days (14-workday window).
    5. Return the forecast dict for embedding in status/summary.json.
    """
    if repo_root is None:
        repo_root = common.REPO_ROOT
    today_jst = now.strftime("%Y-%m-%d")
    tomorrow_jst = (now.date() + timedelta(days=1)).strftime("%Y-%m-%d")

    # (1) History
    append_today_history(repo_root, today_jst, rows_by_id, statuses)
    history = load_history(repo_root)

    out: dict[str, Any] = {
        "generated_at": common.iso_jst(now),
        "today": today_jst,
        "tomorrow": tomorrow_jst,
        "quantile_level": QUANTILE_LEVEL,
        "calibration_window_workdays": CALIBRATION_WORKDAYS,
        "scoring": None,
        "wrote_prediction_for": tomorrow_jst,
        "per_ban": {},
    }

    # (2) Score today's prediction if present
    pred_today_path = predictions_dir(repo_root) / f"{today_jst}.json"
    if pred_today_path.exists():
        rel = f"{PREDICTIONS_DIR_NAME}/{today_jst}.json"
        fp = fingerprint_check(repo_root, rel)
        score_doc: dict[str, Any] = {
            "scored_date": today_jst,
            "scored_at": common.iso_jst(now),
            "fingerprint_check": fp,
            "quantile_level": QUANTILE_LEVEL,
            "per_ban": {},
        }
        if fp["ok"]:
            pred_doc = json.loads(pred_today_path.read_text(encoding="utf-8"))
            for bid, pred in (pred_doc.get("bans") or {}).items():
                row = rows_by_id.get(bid)
                if row is None:
                    continue
                af_actual = float(row.get("after_filter", row.get("records", 0)))
                nd_actual = float(row.get("records_delta", 0))
                status_actual = statuses.get(bid, "ok")
                score_doc["per_ban"][bid] = score_one(
                    pred, af_actual, nd_actual, status_actual)
        score_path = predictions_dir(repo_root) / f"{today_jst}-score.json"
        score_path.parent.mkdir(parents=True, exist_ok=True)
        common.write_json(score_path, score_doc)
        out["scoring"] = {
            "date": today_jst,
            "fingerprint": fp,
            "scored_bans": len(score_doc["per_ban"]),
        }

    # (3) Write tomorrow's prediction
    pred_doc = build_predictions(tomorrow_jst, bans, history, now)
    pred_path = predictions_dir(repo_root) / f"{tomorrow_jst}.json"
    pred_path.parent.mkdir(parents=True, exist_ok=True)
    common.write_json(pred_path, pred_doc)

    # (4) Calibration per ban
    prior = _load_prior_scores(repo_root, bans, tomorrow_jst)
    for ban in bans:
        if not ban["enabled"]:
            continue
        bid = ban["id"]
        out["per_ban"][bid] = calibration_for_ban(
            prior.get(bid, []), CALIBRATION_WORKDAYS)

    return out
