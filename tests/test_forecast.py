"""Tests for runner/forecast.py (機器 40 予測器)."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner import common, forecast  # noqa: E402

JST = timezone(timedelta(hours=9))


# --- numerical helpers ---------------------------------------------------

def test_quantile_matches_order_stats():
    xs = list(range(10))  # 0..9
    # 10th percentile ~= 0.9, 90th ~= 8.1
    assert forecast._quantile(xs, 0.1) == pytest.approx(0.9)
    assert forecast._quantile(xs, 0.9) == pytest.approx(8.1)
    assert forecast._quantile(xs, 0.5) == pytest.approx(4.5)


def test_empirical_interval_80pct():
    lo, hi = forecast._empirical_interval(list(range(100)), 0.80)
    # 10th percentile ~= 9.9, 90th ~= 89.1
    assert lo == pytest.approx(9.9, rel=0.02)
    assert hi == pytest.approx(89.1, rel=0.02)


def test_laplace_probs_sum_to_one_and_smooth_zeros():
    p = forecast._laplace_probs(["ok"] * 5, ("ok", "warn", "ng", "stale"))
    assert sum(p.values()) == pytest.approx(1.0)
    # Every class has +1 smoothing, so no class has probability 0
    assert all(v > 0 for v in p.values())
    # "ok" still dominates
    assert p["ok"] > p["warn"] > 0


def test_laplace_probs_skips_none_labels():
    """status=None rows (backfilled from data files) must not bias the
    distribution toward "ok" or any other class."""
    only_none = forecast._laplace_probs([None, None, None], forecast.STATUS_CLASSES)
    # With no known labels, all classes end up with the same +1 count.
    assert sum(only_none.values()) == pytest.approx(1.0)
    first = next(iter(only_none.values()))
    assert all(v == pytest.approx(first) for v in only_none.values())
    # Mix: one "ok" among nulls — "ok" rises above the others.
    mixed = forecast._laplace_probs([None, "ok", None], forecast.STATUS_CLASSES)
    assert mixed["ok"] > mixed["warn"]
    assert mixed["warn"] == pytest.approx(mixed["ng"]) == pytest.approx(mixed["stale"])


def test_predict_one_with_only_null_status_rows():
    """A ban whose history is entirely backfilled (status=null) still
    produces a valid prediction; status_probs becomes uniform."""
    rows = [{"date": f"2026-11-{d:02d}", "ban": "a",
             "af": v, "new": 0, "status": None}
            for d, v in zip(range(17, 26), [1, 2, 3, 4, 5, 6, 7, 8, 9])]
    pred = forecast._predict_one("a", rows)
    assert pred["history_days"] == 9
    assert pred["status_known_days"] == 0
    probs = pred["status_probs"]
    first = next(iter(probs.values()))
    assert all(v == pytest.approx(first) for v in probs.values())
    # Baseline-last falls back to "ok" when the most recent status is null.
    assert pred["baselines"]["last"]["status"] == "ok"


def test_brier_perfect_vs_worst():
    # Perfect: p(ok)=1, actual=ok  →  (1-1)² + (0-0)² + ... = 0
    assert forecast.brier_score({"ok": 1.0, "warn": 0, "ng": 0, "stale": 0}, "ok") == 0.0
    # Worst: p(ok)=1, actual=ng  →  (1-0)² + 0 + (0-1)² + 0 = 2
    assert forecast.brier_score({"ok": 1.0, "warn": 0, "ng": 0, "stale": 0}, "ng") == 2.0


# --- prediction shape ----------------------------------------------------

def test_predict_one_shape_and_interval_contains_middle():
    rows = [{"date": f"2026-11-{d:02d}", "ban": "a",
             "af": v, "new": v, "status": "ok"}
            for d, v in zip(range(1, 11), [10, 12, 15, 20, 25, 30, 35, 40, 45, 50])]
    pred = forecast._predict_one("a", rows)
    assert pred["ban"] == "a"
    assert pred["history_days"] == 10
    af = pred["after_filter"]
    assert af["low"] < af["high"]
    # 80% interval of 10..50 should bracket the middle ~30
    assert af["low"] <= 30 <= af["high"]
    probs = pred["status_probs"]
    assert set(probs.keys()) == set(forecast.STATUS_CLASSES)
    assert sum(probs.values()) == pytest.approx(1.0)


def test_build_predictions_skips_disabled(tmp_path):
    bans = [
        {"id": "a", "enabled": True, "freq": "daily"},
        {"id": "z", "enabled": False, "freq": "daily"},
    ]
    history = [{"date": "2026-11-20", "ban": "a", "af": 5, "new": 1, "status": "ok"}]
    doc = forecast.build_predictions("2026-11-26", bans, history,
                                     datetime(2026, 11, 25, 13, 59, tzinfo=JST))
    assert "a" in doc["bans"]
    assert "z" not in doc["bans"]
    assert doc["target_date"] == "2026-11-26"
    assert doc["quantile_level"] == forecast.QUANTILE_LEVEL


# --- scoring -------------------------------------------------------------

def test_score_one_hits_and_misses():
    pred = {
        "after_filter": {"low": 10, "high": 20, "median": 15, "method": "x"},
        "records_delta": {"low": 0, "high": 5, "median": 2, "method": "x"},
        "status_probs": {"ok": 0.9, "warn": 0.05, "ng": 0.03, "stale": 0.02},
        "baselines": {
            "last": {"after_filter": 15, "records_delta": 3, "status": "ok"},
            "med7": {"after_filter_range": [8, 22], "records_delta_range": [0, 6],
                     "status_mode": "ok"},
        },
    }
    # Actual in range for af, out of range for nd
    s = forecast.score_one(pred, af_actual=15, nd_actual=8, status_actual="ok")
    assert s["forecaster"]["af_hit"] == 1
    assert s["forecaster"]["af_miss"] == 0
    assert s["forecaster"]["nd_hit"] == 0
    assert s["forecaster"]["nd_miss"] == pytest.approx(3)  # min(|8-0|, |8-5|) = 3
    assert s["forecaster"]["brier"] < 0.1
    # baseline_last also hit af (15==15), missed nd (|3-8|=5)
    assert s["baseline_last"]["af_hit"] == 1
    assert s["baseline_last"]["nd_miss"] == pytest.approx(5)


# --- history idempotency -------------------------------------------------

def test_append_today_history_is_idempotent(tmp_path):
    rows_by_id = {"a": {"after_filter": 5, "records_delta": 1}}
    statuses = {"a": "ok"}
    # Write twice: second call must replace, not duplicate.
    forecast.append_today_history(tmp_path, "2026-11-25", rows_by_id, statuses)
    forecast.append_today_history(tmp_path, "2026-11-25", rows_by_id, statuses)
    hist = forecast.load_history(tmp_path)
    assert len(hist) == 1
    assert hist[0]["af"] == 5
    assert hist[0]["new"] == 1


# --- fingerprint check (uses a throwaway git repo) -----------------------

def _init_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "commit.gpgsign", "false"], check=True)


def _commit_file(root: Path, rel: str, content: str, msg: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", rel], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", msg], check=True)


def test_fingerprint_check_passes_for_committed_untouched_file(tmp_path):
    _init_git(tmp_path)
    _commit_file(tmp_path, "predictions/2026-11-25.json", "{}", "add pred")
    fp = forecast.fingerprint_check(tmp_path, "predictions/2026-11-25.json")
    assert fp["ok"] is True, fp


def test_fingerprint_check_detects_working_tree_edit(tmp_path):
    _init_git(tmp_path)
    _commit_file(tmp_path, "predictions/2026-11-25.json", "{}", "add pred")
    # Tamper after commit.
    (tmp_path / "predictions/2026-11-25.json").write_text("{\"evil\":1}", encoding="utf-8")
    fp = forecast.fingerprint_check(tmp_path, "predictions/2026-11-25.json")
    assert fp["ok"] is False
    assert "working tree" in fp["detail"] or "rewritten" in fp["detail"]


def test_fingerprint_check_detects_rewrite_commit(tmp_path):
    _init_git(tmp_path)
    _commit_file(tmp_path, "predictions/2026-11-25.json", "{}", "add pred")
    _commit_file(tmp_path, "predictions/2026-11-25.json", "{\"v\":2}", "overwrite pred")
    fp = forecast.fingerprint_check(tmp_path, "predictions/2026-11-25.json")
    assert fp["ok"] is False
    assert "rewritten" in fp["detail"]


# --- calibration windowing -----------------------------------------------

def _mk_score(date: str, forecast_brier: float, last_brier: float,
              af_hit: int = 1, nd_hit: int = 1,
              history_days: int = 10) -> dict:
    return {
        "date": date,
        "history_days": history_days,
        "forecaster": {"brier": forecast_brier,
                       "af_hit": af_hit, "af_miss": 0,
                       "nd_hit": nd_hit, "nd_miss": 0},
        "baseline_last": {"brier": last_brier,
                          "af_hit": 0, "af_miss": 1,
                          "nd_hit": 0, "nd_miss": 1},
        "baseline_med7": None,
    }


def test_calibration_uncalibrated_until_14():
    prior = [_mk_score(f"2026-11-{d:02d}", 0.1, 0.5) for d in range(1, 14)]
    cal = forecast.calibration_for_ban(prior, window=14)
    assert cal["days_scored"] == 13
    assert cal["status"] == "uncalibrated"
    # Forecaster's avg brier (0.1) beats baseline-last (0.5).
    assert cal["vs_last"] == "win"


def test_calibration_calibrated_at_14():
    prior = [_mk_score(f"2026-11-{d:02d}", 0.1, 0.5) for d in range(1, 15)]
    cal = forecast.calibration_for_ban(prior, window=14)
    assert cal["days_scored"] == 14
    assert cal["status"] == "calibrated"
    # 100% hits → proximity to 80% = 1 - |1 - 0.8| = 0.8
    assert cal["hit_rate_ranges"] == pytest.approx(1.0)
    assert cal["proximity_to_80pct"] == pytest.approx(0.8)
    assert cal["excluded_lowdata_days"] == 0


# --- low-data exclusion from hit_rate / proximity -----------------------

def test_calibration_excludes_lowdata_days_from_hit_rate():
    """A score row whose prediction had history_days<7 is counted in
    days_scored and in the Brier averages, but excluded from the hit_rate
    and proximity calculation (the 80% interval would be degenerate)."""
    lowdata = [_mk_score("2026-11-26", 0.1, 0.5, af_hit=0, nd_hit=0,
                         history_days=1)]
    full = [_mk_score(f"2026-11-{d:02d}", 0.1, 0.5, af_hit=1, nd_hit=1,
                      history_days=10)
            for d in range(27, 31)]  # 4 full-data days
    cal = forecast.calibration_for_ban(lowdata + full, window=14)
    assert cal["days_scored"] == 5
    assert cal["excluded_lowdata_days"] == 1
    # hit_rate is computed from the 4 full-data days only (all hit) → 1.0
    assert cal["hit_rate_ranges"] == pytest.approx(1.0)
    assert cal["proximity_to_80pct"] == pytest.approx(0.8)
    # The excluded low-data row still contributes to the Brier average.
    assert cal["forecaster_avg_brier"] == pytest.approx(0.1)


def test_calibration_all_lowdata_yields_none_hit_rate():
    """If every row in the window is low-data, hit_rate and proximity
    are None — we don't guess from a degenerate interval."""
    prior = [_mk_score(f"2026-11-{d:02d}", 0.1, 0.5, history_days=1)
             for d in range(26, 29)]
    cal = forecast.calibration_for_ban(prior, window=14)
    assert cal["days_scored"] == 3
    assert cal["excluded_lowdata_days"] == 3
    assert cal["hit_rate_ranges"] is None
    assert cal["proximity_to_80pct"] is None


# --- score_one carries history_days through to the score doc ------------

def test_score_one_embeds_history_days():
    pred = {
        "history_days": 1,
        "after_filter": {"low": 5, "high": 5, "median": 5, "method": "x"},
        "records_delta": {"low": 0, "high": 0, "median": 0, "method": "x"},
        "status_probs": {"ok": 0.25, "warn": 0.25, "ng": 0.25, "stale": 0.25},
        "baselines": {
            "last": {"after_filter": 5, "records_delta": 0, "status": "ok"},
            "med7": {},
        },
    }
    s = forecast.score_one(pred, af_actual=5, nd_actual=0, status_actual="ok")
    assert s["history_days"] == 1


# --- backfill reconstruction matches the live history line --------------

def test_backfill_reconstructs_today_matching_live(tmp_path):
    """The backfill script's data-file reader must produce the same (af, new)
    values that summarize already wrote to history.jsonl for today — if it
    doesn't, we cannot trust that it's filling prior days consistently."""
    # Simulate a data/<ban>/<today>.json like the real pipeline writes.
    data_dir = tmp_path / "data" / "a"
    data_dir.mkdir(parents=True)
    (data_dir / "2026-11-26.json").write_text(json.dumps({
        "ban": "a",
        "fetch_stats": {"after_filter": 42, "new_today": 7, "raw_fetched": 42},
    }), encoding="utf-8")
    # Import the backfill helper via a sys.path hack (same layout as tests).
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    sys.path.insert(0, str(scripts_dir))
    try:
        import backfill_history as bf
    finally:
        sys.path.remove(str(scripts_dir))
    bans = [{"id": "a", "enabled": True}]
    rows = bf.rows_from_data_dir(tmp_path, bans)
    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2026-11-26"
    assert r["ban"] == "a"
    assert r["af"] == 42
    assert r["new"] == 7
    assert r["status"] is None
    assert r["backfilled"] is True

    # And the match verifier confirms equality against a live row.
    live = [{"date": "2026-11-26", "ban": "a", "af": 42, "new": 7, "status": "ok"}]
    ok, mismatches = bf.verify_today_match("2026-11-26", rows, live)
    assert ok, mismatches

    # A deliberate mismatch is detected.
    live_bad = [{"date": "2026-11-26", "ban": "a", "af": 43, "new": 7, "status": "ok"}]
    ok2, mismatches2 = bf.verify_today_match("2026-11-26", rows, live_bad)
    assert not ok2
    assert any("af=43" in m for m in mismatches2)
