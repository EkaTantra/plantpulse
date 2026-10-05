"""
Local (offline) engine - mirrors the Snowflake SQL logic in pandas so the
command center can run as a demo without a Snowflake connection, and so the
rule thresholds can be unit-tested. Snowflake remains the system of record.
"""
import os
from functools import lru_cache

import numpy as np
import pandas as pd

DATA_DIR = os.environ.get("PLANTPULSE_DATA", os.path.join(os.path.dirname(__file__), "..", "data"))

MODE_PARTS = {
    "BEARING_WEAR": ["BRG-6312-C3", "GRS-LGHP2"],
    "CAVITATION": ["IMP-CP-150", "SEAL-MS-35", "STR-SUC-80"],
    "LUBRICATION_FAILURE": ["OIL-ISO-VG220", "FLT-OIL-90", "SNS-PT100"],
    "IMBALANCE": ["BAL-WT-KIT"],
    "MISALIGNMENT": ["CPL-INS-L100", "SHM-KIT-SS"],
    "ELECTRICAL_WINDING": ["MTR-STBY-45KW"],
    "SENSOR_FAULT": [],
    "NONE": [],
}


@lru_cache(maxsize=1)
def load():
    rd = lambda n, **k: pd.read_csv(os.path.join(DATA_DIR, f"{n}.csv"), **k)
    d = dict(
        assets=rd("assets"),
        sensors=rd("sensor_readings", parse_dates=["READING_TS"]),
        failures=rd("failure_events", parse_dates=["FAILURE_TS"]),
        work_orders=rd("work_orders", parse_dates=["CREATED_TS", "SCHEDULED_TS", "CLOSED_TS"]),
        parts=rd("spare_parts"),
        production=rd("production_log", parse_dates=["SHIFT_START_TS"]),
        docs=rd("maintenance_docs"),
    )
    return d


def hourly(sensors):
    s = sensors.copy()
    s["HOUR_TS"] = s.READING_TS.dt.floor("h")
    run = s.RPM > 0
    ok = run & (s.VIBRATION_MM_S < 24.5)
    s["v"] = s.VIBRATION_MM_S.where(ok)
    s["t"] = s.TEMPERATURE_C.where(run)
    s["r"] = s.RPM.where(run)
    s["c"] = s.CURRENT_A.where(run)
    s["run"] = run.astype(float)
    s["sat"] = (s.VIBRATION_MM_S >= 24.5).astype(int)
    g = s.groupby(["ASSET_ID", "HOUR_TS"])
    h = pd.DataFrame({
        "VIB_AVG": g.v.mean(), "VIB_MAX": g.v.max(), "VIB_STD": g.v.std(),
        "TEMP_AVG": g.t.mean(), "TEMP_MAX": g.t.max(), "RPM_AVG": g.r.mean(),
        "CUR_AVG": g.c.mean(), "CUR_STD": g.c.std(), "RUNNING_FRAC": g.run.mean(),
        "SATURATED_READINGS": g.sat.sum(),
    }).reset_index()
    return h


@lru_cache(maxsize=1)
def features():
    d = load()
    h = hourly(d["sensors"])
    base = h[h.RUNNING_FRAC > 0.9].groupby("ASSET_ID").agg(
        BASE_VIB=("VIB_AVG", "median"), BASE_TEMP=("TEMP_AVG", "median"),
        BASE_CUR=("CUR_AVG", "median"), BASE_CUR_STD=("CUR_STD", "median")).reset_index()
    h = h[(h.RUNNING_FRAC > 0.5) & h.VIB_AVG.notna()].sort_values(["ASSET_ID", "HOUR_TS"]).copy()
    h["X"] = (h.HOUR_TS - pd.Timestamp("2026-01-01")).dt.total_seconds() / 3600
    out = []
    for aid, g in h.groupby("ASSET_ID"):
        r24 = lambda c, f="mean": getattr(g[c].rolling(24, min_periods=1), f)()
        r72 = lambda s: s.rolling(72, min_periods=1).sum()
        f = pd.DataFrame({"ASSET_ID": aid, "HOUR_TS": g.HOUR_TS})
        f["VIB_AVG_24H"] = r24("VIB_AVG"); f["VIB_MAX_24H"] = r24("VIB_MAX", "max")
        f["VIB_STD_24H"] = r24("VIB_STD"); f["TEMP_AVG_24H"] = r24("TEMP_AVG")
        f["TEMP_MAX_24H"] = r24("TEMP_MAX", "max"); f["CUR_AVG_24H"] = r24("CUR_AVG")
        f["CUR_STD_24H"] = r24("CUR_STD"); f["RPM_AVG_24H"] = r24("RPM_AVG")
        f["RPM_STD_24H"] = g.RPM_AVG.rolling(24, min_periods=2).std()
        n = g.X.rolling(72, min_periods=1).count()
        sx, sxx = r72(g.X), r72(g.X * g.X)
        den = (n * sxx - sx * sx).replace(0, np.nan)
        f["VIB_SLOPE_PER_DAY"] = 24 * (n * r72(g.X * g.VIB_AVG) - sx * r72(g.VIB_AVG)) / den
        f["TEMP_SLOPE_PER_DAY"] = 24 * (n * r72(g.X * g.TEMP_AVG) - sx * r72(g.TEMP_AVG)) / den
        f["SATURATED_24H"] = g.SATURATED_READINGS.rolling(24, min_periods=1).sum()
        out.append(f)
    f = pd.concat(out).merge(base, on="ASSET_ID").merge(d["assets"], on="ASSET_ID")
    f["VIB_RATIO"] = f.VIB_AVG_24H / f.BASE_VIB
    f["VIB_ALARM_RATIO"] = f.VIB_AVG_24H / f.VIBRATION_ALARM_MM_S
    f["VIB_CV"] = f.VIB_STD_24H / f.VIB_AVG_24H
    f["TEMP_DELTA"] = f.TEMP_AVG_24H - f.BASE_TEMP
    f["TEMP_MARGIN"] = f.TEMP_ALARM_C - f.TEMP_AVG_24H
    f["CUR_RATIO"] = f.CUR_AVG_24H / f.BASE_CUR
    f["CUR_VOLATILITY"] = f.CUR_STD_24H / f.BASE_CUR_STD
    f["RPM_CV"] = f.RPM_STD_24H / f.RPM_AVG_24H
    # days since last closed PM (asof)
    pm = d["work_orders"][(d["work_orders"].WO_TYPE == "PM") & (d["work_orders"].STATUS == "CLOSED")][["ASSET_ID", "CLOSED_TS"]].dropna().sort_values("CLOSED_TS")
    f = f.sort_values("HOUR_TS")
    f = pd.merge_asof(f, pm, left_on="HOUR_TS", right_on="CLOSED_TS", by="ASSET_ID", direction="backward")
    f["DAYS_SINCE_PM"] = ((f.HOUR_TS - f.CLOSED_TS).dt.days).fillna(f.PM_INTERVAL_DAYS)
    f["PM_OVERDUE_DAYS"] = f.DAYS_SINCE_PM - f.PM_INTERVAL_DAYS
    return rule_health(f.sort_values(["ASSET_ID", "HOUR_TS"]).reset_index(drop=True))


def rule_health(f):
    c = lambda x: np.clip(x, 0, 1)
    f = f.copy()
    f["S_VIB"] = c((f.VIB_RATIO - 1.3) / 1.7)
    f["S_TEMP"] = c((f.TEMP_DELTA - 4) / 10)
    f["S_VIB_TREND"] = c((f.VIB_SLOPE_PER_DAY / f.BASE_VIB) / 0.25)
    f["S_TEMP_TREND"] = c(f.TEMP_SLOPE_PER_DAY / 3)
    f["S_CUR"] = c(((f.CUR_RATIO - 1).abs() - 0.03) / 0.12)
    f["RULE_SCORE"] = (100 * (0.35 * f.S_VIB + 0.25 * f.S_TEMP + 0.15 * f.S_VIB_TREND
                              + 0.10 * f.S_TEMP_TREND + 0.15 * f.S_CUR)).round(1)
    conds = [
        (f.SATURATED_24H > 0) & (f.S_VIB < 0.2) & (f.S_TEMP < 0.2),
        (f.CUR_RATIO > 1.12) & (f.TEMP_DELTA > 5),
        (f.ASSET_TYPE == "Coolant Pump") & (f.VIB_RATIO > 1.25) & ((f.CUR_RATIO < 0.97) | (f.CUR_VOLATILITY > 1.5)),
        (f.TEMP_DELTA > 7) & (f.VIB_RATIO < 1.7),
        (f.VIB_RATIO > 1.4) & (f.RPM_CV > 0.0025),
        (f.VIB_RATIO > 1.5) & (f.TEMP_DELTA > 4),
        (f.VIB_RATIO > 1.4),
    ]
    modes = ["SENSOR_FAULT", "ELECTRICAL_WINDING", "CAVITATION", "LUBRICATION_FAILURE",
             "MISALIGNMENT", "BEARING_WEAR", "IMBALANCE"]
    f["SUSPECTED_MODE"] = np.select(conds, modes, default="NONE")
    hv = np.where(f.VIB_SLOPE_PER_DAY > 0.02, np.maximum(0, (f.VIBRATION_ALARM_MM_S - f.VIB_AVG_24H) / f.VIB_SLOPE_PER_DAY * 24), 9999)
    ht = np.where(f.TEMP_SLOPE_PER_DAY > 0.3, np.maximum(0, (f.TEMP_ALARM_C - f.TEMP_AVG_24H) / f.TEMP_SLOPE_PER_DAY * 24), 9999)
    f["HOURS_TO_ALARM"] = np.minimum(hv, ht)
    return f


def risk_band(score):
    return "HIGH" if score >= 50 else ("MEDIUM" if score >= 30 else "LOW")


def latest_risk():
    f = features()
    last = f.sort_values("HOUR_TS").groupby("ASSET_ID").tail(1).copy()
    last["RISK_SCORE"] = last.RULE_SCORE
    last["RISK_BAND"] = last.RISK_SCORE.apply(risk_band)
    return last.sort_values("RISK_SCORE", ascending=False).reset_index(drop=True)


def oee_shift():
    p = load()["production"].copy()
    p["AVAILABILITY"] = p.RUN_TIME_MIN / p.PLANNED_TIME_MIN
    p["PERFORMANCE"] = (p.IDEAL_CYCLE_TIME_S * p.TOTAL_COUNT / 60) / p.RUN_TIME_MIN.replace(0, np.nan)
    p["QUALITY"] = p.GOOD_COUNT / p.TOTAL_COUNT.replace(0, np.nan)
    p["OEE"] = p.AVAILABILITY * p.PERFORMANCE.fillna(0) * p.QUALITY.fillna(0)
    return p


def search_docs(query, asset_type=None, mode=None, k=4):
    """Tiny keyword retriever standing in for Cortex Search in demo mode."""
    d = load()
    docs = d["docs"].assign(SOURCE="MANUAL")
    wo = d["work_orders"].dropna(subset=["FAILURE_MODE"]).merge(d["assets"][["ASSET_ID", "ASSET_TYPE"]], on="ASSET_ID")
    wo_docs = pd.DataFrame({
        "DOC_ID": wo.WO_ID, "DOC_TITLE": "Work order " + wo.WO_ID + " (" + wo.ASSET_ID + ")",
        "ASSET_TYPE": wo.ASSET_TYPE, "FAILURE_MODE": wo.FAILURE_MODE,
        "CONTENT": wo.DESCRIPTION + ". " + wo.TECHNICIAN_NOTES, "SOURCE": "WORK_ORDER"})
    allx = pd.concat([docs, wo_docs], ignore_index=True)
    terms = [t for t in str(query).lower().replace(",", " ").split() if len(t) > 3]
    sc = allx.CONTENT.str.lower().apply(lambda c: sum(c.count(t) for t in terms)).astype(float)
    if mode:
        sc += (allx.FAILURE_MODE == mode) * 5
    if asset_type:
        sc += (allx.ASSET_TYPE == asset_type) * 3
    allx["SCORE"] = sc
    return allx.sort_values("SCORE", ascending=False).head(k)
