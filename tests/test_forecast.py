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
              af_hit: int = 1, nd_hit: int = 1) -> dict:
    return {
        "date": date,
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
