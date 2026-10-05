"""
Back-test of the explainable rule engine (app/local_engine.py mirrors sql/02_features.sql).

Run:  python -m pytest tests/ -q
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import local_engine as le  # noqa: E402

NOW = pd.Timestamp("2026-10-05 00:00")
WARN = 30            # MEDIUM band threshold used for "flagged in advance"
ACTIVE = {
    "PUN-L1-GBX-01": ("BEARING_WEAR", "HIGH"),
    "PUN-L2-CMP-01": ("LUBRICATION_FAILURE", "MEDIUM"),
    "CHN-L2-PMP-01": ("CAVITATION", "MEDIUM"),
}


@pytest.fixture(scope="module")
def feats():
    return le.features()


@pytest.fixture(scope="module")
def failures():
    return le.load()["failures"]


def _window(f, asset, end, days=8):
    return f[(f.ASSET_ID == asset) & (f.HOUR_TS < end) & (f.HOUR_TS >= end - pd.Timedelta(days=days))]


def test_dataset_shape():
    d = le.load()
    assert len(d["assets"]) == 24
    assert len(d["sensors"]) == 207_360
    assert len(d["failures"]) == 12
    assert len(d["docs"]) == 20


def test_every_failure_flagged_in_advance(feats, failures):
    leads = []
    for _, r in failures.iterrows():
        w = _window(feats, r.ASSET_ID, r.FAILURE_TS)
        first = w.loc[w.RULE_SCORE >= WARN, "HOUR_TS"].min()
        assert pd.notna(first), f"{r.FAILURE_ID} on {r.ASSET_ID} was never flagged"
        leads.append((r.FAILURE_TS - first) / pd.Timedelta(hours=1))
    assert min(leads) >= 20, f"lead times too short: {sorted(leads)}"
    assert 40 <= pd.Series(leads).median() <= 60


def test_failure_modes_identified(feats, failures):
    for _, r in failures.iterrows():
        last = _window(feats, r.ASSET_ID, r.FAILURE_TS).sort_values("HOUR_TS").iloc[-1]
        assert last.SUSPECTED_MODE == r.FAILURE_MODE, f"{r.FAILURE_ID}: {last.SUSPECTED_MODE} != {r.FAILURE_MODE}"


def test_no_false_alarms_outside_degradation(feats, failures):
    mask = feats.RULE_SCORE >= WARN
    for _, r in failures.iterrows():
        start = r.FAILURE_TS - pd.Timedelta(days=8)
        end = r.FAILURE_TS + pd.Timedelta(hours=r.DOWNTIME_HOURS + 72)   # rolling windows still hold pre-failure data
        mask &= ~((feats.ASSET_ID == r.ASSET_ID) & feats.HOUR_TS.between(start, end))
    for asset in ACTIVE:
        mask &= ~((feats.ASSET_ID == asset) & (feats.HOUR_TS >= NOW - pd.Timedelta(days=8)))
    assert mask.sum() == 0, feats.loc[mask, ["ASSET_ID", "HOUR_TS", "RULE_SCORE"]].head().to_string()


def test_current_degradations_in_expected_bands():
    latest = le.latest_risk().set_index("ASSET_ID")
    for asset, (mode, band) in ACTIVE.items():
        assert latest.loc[asset, "SUSPECTED_MODE"] == mode
        assert latest.loc[asset, "RISK_BAND"] == band
    healthy = latest.drop(index=list(ACTIVE))
    assert (healthy.RISK_BAND == "LOW").all()


def test_sensor_glitch_classified_as_sensor_fault():
    latest = le.latest_risk().set_index("ASSET_ID")
    glitch = latest.loc["CHN-L1-MTR-01"]
    assert glitch.SUSPECTED_MODE == "SENSOR_FAULT"
    assert glitch.RISK_BAND == "LOW"
    assert glitch.SATURATED_24H > 0
