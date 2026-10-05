"""
PlantPulse - synthetic IT/OT data generator.

Creates a realistic, fully synthetic dataset for two plants / four production
lines / 24 rotating assets covering 60 days:

  assets.csv              asset master (ERP / CMMS)
  sensor_readings.csv     10-minute OT telemetry (vibration, temperature, rpm, current)
  failure_events.csv      historical functional failures (ground truth labels)
  work_orders.csv         CMMS work orders incl. free-text technician notes
  spare_parts.csv         spare-part inventory (ERP / MM)
  production_log.csv      shift-level production counts for OEE
  maintenance_docs.csv    unstructured manual / SOP chunks for Cortex Search

Three assets are deliberately *degrading* at the end of the window and have NOT
failed yet - these are the assets the model should flag.

Usage:  python generate_data.py [--out ../data] [--seed 42]
"""
import argparse
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from docs_corpus import DOCS

START = datetime(2026, 8, 6, 0, 0)
END = datetime(2026, 10, 5, 0, 0)          # "now" for the demo
STEP_MIN = 10

PLANTS = {
    "PUN": "Pune Assembly Plant",
    "CHN": "Chennai Machining Plant",
}
LINES = ["L1", "L2"]

# type code -> (name, rpm, vib mm/s, temp C, current A, vib_alarm, temp_alarm, manufacturer)
TYPES = {
    "MTR": ("Main Drive Motor", 1480, 1.8, 62, 42, 7.1, 95, "Siemens"),
    "PMP": ("Coolant Pump", 2950, 2.2, 55, 18, 7.1, 85, "Grundfos"),
    "CMP": ("Air Compressor", 3000, 2.5, 78, 55, 7.1, 110, "Atlas Copco"),
    "GBX": ("Gearbox", 420, 2.0, 68, 30, 7.1, 100, "SEW-Eurodrive"),
    "SPN": ("CNC Spindle", 12000, 1.2, 45, 12, 4.5, 75, "Fanuc"),
    "CNV": ("Conveyor Drive", 90, 1.5, 48, 9, 7.1, 80, "Bonfiglioli"),
}
CRITICALITY = {"MTR": "A", "GBX": "A", "SPN": "A", "CMP": "B", "PMP": "B", "CNV": "C"}
IDEAL_CT = {"PUN-L1": 42, "PUN-L2": 38, "CHN-L1": 45, "CHN-L2": 40}

# Historical failures: (asset, failure_mode, failure_ts, degradation_days, downtime_h)
HIST_FAILURES = [
    ("PUN-L1-MTR-01", "BEARING_WEAR", datetime(2026, 8, 19, 14, 20), 6, 9.5),
    ("PUN-L1-PMP-01", "CAVITATION", datetime(2026, 8, 24, 3, 40), 4, 5.0),
    ("PUN-L2-GBX-01", "LUBRICATION_FAILURE", datetime(2026, 8, 28, 22, 10), 5, 14.0),
    ("CHN-L1-SPN-01", "IMBALANCE", datetime(2026, 8, 31, 9, 0), 4, 6.5),
    ("CHN-L2-MTR-01", "ELECTRICAL_WINDING", datetime(2026, 9, 3, 17, 30), 5, 18.0),
    ("PUN-L2-CNV-01", "MISALIGNMENT", datetime(2026, 9, 7, 11, 50), 5, 4.0),
    ("CHN-L1-CMP-01", "LUBRICATION_FAILURE", datetime(2026, 9, 11, 6, 30), 6, 11.0),
    ("PUN-L1-GBX-01", "BEARING_WEAR", datetime(2026, 9, 14, 15, 0), 7, 16.0),
    ("CHN-L2-PMP-01", "CAVITATION", datetime(2026, 9, 17, 2, 20), 4, 4.5),
    ("PUN-L2-SPN-01", "BEARING_WEAR", datetime(2026, 9, 21, 19, 40), 6, 12.0),
    ("CHN-L1-GBX-01", "MISALIGNMENT", datetime(2026, 9, 25, 8, 10), 5, 7.0),
    ("PUN-L1-CMP-01", "IMBALANCE", datetime(2026, 9, 28, 13, 30), 4, 5.5),
]

# Assets degrading right now (failure projected AFTER END) -> what the copilot must catch
ACTIVE_DEGRADATION = [
    # asset, mode, projected failure ts, degradation_days
    ("PUN-L1-GBX-01", "BEARING_WEAR", END + timedelta(hours=30), 7),        # repeat offender
    ("CHN-L2-PMP-01", "CAVITATION", END + timedelta(hours=26), 5),           # repeat offender
    ("PUN-L2-CMP-01", "LUBRICATION_FAILURE", END + timedelta(hours=40), 6),
]
# Transient sensor glitch (should be triaged as false alarm)
GLITCHES = [("CHN-L1-MTR-01", END - timedelta(hours=5), 30)]  # asset, start, minutes

FAILURE_DESC = {
    "BEARING_WEAR": "Rolling-element bearing wear / outer race spalling",
    "CAVITATION": "Pump cavitation due to low suction head",
    "LUBRICATION_FAILURE": "Lubrication breakdown leading to overheating",
    "IMBALANCE": "Rotor imbalance",
    "MISALIGNMENT": "Shaft / coupling misalignment",
    "ELECTRICAL_WINDING": "Stator winding insulation degradation",
}

TECH_NOTES = {
    "BEARING_WEAR": [
        "Found outer race spalling on drive-end bearing {brg}. Grease was dark and contaminated. Replaced bearing, flushed housing, regreased with {grease}. Last lubrication was {late} days overdue.",
        "High-frequency vibration confirmed bearing defect frequency (BPFO). Replaced DE and NDE bearings {brg}. Recommend shortening lubrication interval to 21 days.",
        "Bearing {brg} noisy, cage damage visible. Root cause: lubrication interval missed during shutdown week. Replaced and added to PdM watch list.",
    ],
    "CAVITATION": [
        "Crackling noise at pump inlet, suction strainer 70% blocked. Cleaned strainer, impeller showed pitting. Replaced impeller and mechanical seal.",
        "Cavitation caused by low sump level in coolant tank. Topped up, recalibrated level switch, replaced worn impeller.",
    ],
    "LUBRICATION_FAILURE": [
        "Oil level low and oil oxidised (dark, burnt smell). Drained and refilled with {oil}. Replaced oil filter. Breather was clogged.",
        "Overheating trip. Oil cooler fins choked with dust; oil degraded. Cleaned cooler, changed oil to {oil}, replaced temperature sensor.",
    ],
    "IMBALANCE": [
        "1x running-speed vibration dominant. Found material build-up on rotor/fan. Cleaned and dynamically balanced to G2.5.",
        "Balance weight missing from rotor. Re-balanced on site, vibration dropped from 6.8 to 1.9 mm/s.",
    ],
    "MISALIGNMENT": [
        "2x running speed with high axial vibration. Coupling misalignment 0.35 mm. Laser-aligned to <0.05 mm, replaced coupling insert.",
        "Soft foot on motor base caused misalignment after base bolts loosened. Shimmed, torqued and laser-aligned.",
    ],
    "ELECTRICAL_WINDING": [
        "Motor tripped on overcurrent. Insulation resistance 0.8 MOhm (low). Sent motor for rewinding, installed standby motor.",
        "Phase current imbalance 12%. Winding hot-spot found by thermography. Motor replaced from stores.",
    ],
}

PARTS_BY_MODE = {
    "BEARING_WEAR": ["BRG-6312-C3", "GRS-LGHP2"],
    "CAVITATION": ["IMP-CP-150", "SEAL-MS-35", "STR-SUC-80"],
    "LUBRICATION_FAILURE": ["OIL-ISO-VG220", "FLT-OIL-90", "SNS-PT100"],
    "IMBALANCE": ["BAL-WT-KIT"],
    "MISALIGNMENT": ["CPL-INS-L100", "SHM-KIT-SS"],
    "ELECTRICAL_WINDING": ["MTR-STBY-45KW"],
}


def build_assets():
    rows = []
    rng = np.random.default_rng(7)
    for p, pname in PLANTS.items():
        for line in LINES:
            for t, (tname, rpm, vib, temp, cur, vlim, tlim, mfr) in TYPES.items():
                aid = f"{p}-{line}-{t}-01"
                rows.append(dict(
                    ASSET_ID=aid, ASSET_NAME=f"{tname} {line}", ASSET_TYPE=tname, TYPE_CODE=t,
                    PLANT_CODE=p, PLANT_NAME=pname, LINE_ID=f"{p}-{line}", MANUFACTURER=mfr,
                    INSTALL_DATE=(datetime(2016, 1, 1) + timedelta(days=int(rng.integers(0, 3000)))).date(),
                    CRITICALITY=CRITICALITY[t], RATED_RPM=rpm, BASE_VIBRATION_MM_S=vib,
                    VIBRATION_ALARM_MM_S=vlim, TEMP_ALARM_C=tlim, RATED_CURRENT_A=cur,
                    PM_INTERVAL_DAYS=30, COST_CENTER=f"CC-{p}-{line}-MNT",
                ))
    return pd.DataFrame(rows)


def degradation(mode, p, rng, n):
    """Return additive deltas (vib, temp, rpm_frac, cur_frac) for progress array p in [0,1]."""
    z = np.zeros(n)
    if mode == "BEARING_WEAR":
        spikes = (rng.random(n) < 0.05 * p) * rng.uniform(0.5, 2.5, n)
        return 5.8 * p ** 2.2 + spikes, 14 * p ** 2, z, 0.04 * p
    if mode == "CAVITATION":
        return 3.0 * p ** 1.5 + rng.normal(0, 1.2 * p, n).clip(-1, None), 4 * p, -0.01 * p, -0.10 * p + rng.normal(0, 0.04 * p, n)
    if mode == "LUBRICATION_FAILURE":
        return 1.6 * p, 34 * p ** 2, z, 0.06 * p
    if mode == "IMBALANCE":
        return 4.8 * p, 3 * p, z, 0.02 * p
    if mode == "MISALIGNMENT":
        return 3.8 * p ** 1.5, 7 * p, rng.normal(0, 0.015 * p, n), 0.05 * p
    if mode == "ELECTRICAL_WINDING":
        return 0.8 * p, 20 * p ** 2, z, 0.26 * p ** 2
    raise ValueError(mode)


def build_sensors(assets, rng):
    ts = pd.date_range(START, END - timedelta(minutes=STEP_MIN), freq=f"{STEP_MIN}min")
    n = len(ts)
    hours = ts.hour.values + ts.minute.values / 60
    # production load: 3 shifts, lighter night shift, Sunday reduced
    load = 0.92 + 0.06 * np.sin((hours - 9) / 24 * 2 * np.pi)
    ambient = 30 + 4 * np.sin((hours - 15) / 24 * 2 * np.pi)
    frames = []
    for _, a in assets.iterrows():
        t = a.TYPE_CODE
        _, rpm, vib, temp, cur, *_ = TYPES[t]
        v = vib * (0.9 + 0.15 * load) + rng.normal(0, 0.12 * vib, n)
        tc = temp + (ambient - 32) * 0.5 + 6 * (load - 0.92) + rng.normal(0, 0.8, n)
        r = rpm * (0.985 + 0.015 * load) + rng.normal(0, 0.002 * rpm, n)
        c = cur * load + rng.normal(0, 0.02 * cur, n)
        running = np.ones(n, dtype=bool)

        events = [(m, f, d, None) for (aid, m, f, d, _) in HIST_FAILURES if aid == a.ASSET_ID]
        events += [(m, f, d, "active") for (aid, m, f, d) in ACTIVE_DEGRADATION if aid == a.ASSET_ID]
        for mode, fts, ddays, kind in events:
            start = fts - timedelta(days=ddays)
            mask = (ts >= start) & (ts < fts)
            prog = ((ts[mask] - start) / (fts - start)).values.astype(float)
            dv, dt_, drpm, dcur = degradation(mode, prog, rng, mask.sum())
            v[mask] += dv
            tc[mask] += dt_
            r[mask] *= (1 + drpm)
            c[mask] *= (1 + dcur)
            if kind is None:
                dh = [h for (aid, m, f, d, h) in HIST_FAILURES if aid == a.ASSET_ID and f == fts][0]
                off = (ts >= fts) & (ts < fts + timedelta(hours=dh))
                running[off] = False
        for aid, gstart, gmin in GLITCHES:
            if aid == a.ASSET_ID:
                g = (ts >= gstart) & (ts < gstart + timedelta(minutes=gmin))
                v[g] = 24.9   # sensor saturation value - physically implausible
        v = np.where(running, v, rng.uniform(0.05, 0.2, n))
        r = np.where(running, r, 0)
        c = np.where(running, c, 0)
        tc = np.where(running, tc, ambient + rng.normal(0, 0.5, n))
        frames.append(pd.DataFrame({
            "ASSET_ID": a.ASSET_ID, "READING_TS": ts,
            "VIBRATION_MM_S": np.round(np.clip(v, 0.02, None), 3),
            "TEMPERATURE_C": np.round(tc, 2),
            "RPM": np.round(np.clip(r, 0, None), 1),
            "CURRENT_A": np.round(np.clip(c, 0, None), 2),
        }))
    return pd.concat(frames, ignore_index=True)


def build_failures():
    rows = []
    for i, (aid, mode, fts, ddays, dh) in enumerate(HIST_FAILURES, 1):
        rows.append(dict(FAILURE_ID=f"FL-{i:04d}", ASSET_ID=aid, FAILURE_TS=fts, FAILURE_MODE=mode,
                         FAILURE_DESCRIPTION=FAILURE_DESC[mode], DOWNTIME_HOURS=dh,
                         DETECTED_BY="Operator / breakdown"))
    return pd.DataFrame(rows)


def build_work_orders(assets, rng):
    rows = []
    wo = 70000
    # Preventive maintenance every ~30 days per asset
    for _, a in assets.iterrows():
        first = START + timedelta(days=int(rng.integers(0, 25)), hours=int(rng.integers(6, 18)))
        d = first
        while d < END:
            wo += 1
            skipped = rng.random() < 0.12
            rows.append(dict(
                WO_ID=f"WO-{wo}", ASSET_ID=a.ASSET_ID, WO_TYPE="PM", PRIORITY="P3",
                STATUS="CANCELLED" if skipped else "CLOSED", CREATED_TS=d - timedelta(days=3),
                SCHEDULED_TS=d, CLOSED_TS=None if skipped else d + timedelta(hours=2),
                FAILURE_MODE=None,
                DESCRIPTION=f"Monthly PM - {a.ASSET_TYPE}: lubrication, visual inspection, fastener torque check",
                TECHNICIAN_NOTES=("PM skipped - production priority / shutdown week" if skipped else
                                  rng.choice(["Routine PM completed. No abnormality.",
                                              "Lubricated as per schedule. Minor oil seepage noted at gasket, monitor.",
                                              "PM done. Belt tension adjusted.",
                                              "Completed PM, cleaned cooling fins, readings normal."])),
                PARTS_USED="GRS-LGHP2" if not skipped else None,
                LABOR_HOURS=0 if skipped else 2.0, COST_INR=0 if skipped else 3500,
                DOWNTIME_HOURS=0 if skipped else 1.0, SOURCE="SCHEDULED"))
            d += timedelta(days=30)
    # Corrective WOs for each historical failure
    for (aid, mode, fts, ddays, dh) in HIST_FAILURES:
        wo += 1
        note = rng.choice(TECH_NOTES[mode]).format(
            brg="6312-C3", grease="LGHP2 polyurea grease", late=int(rng.integers(8, 20)), oil="ISO VG 220 gear oil")
        parts = PARTS_BY_MODE[mode]
        rows.append(dict(
            WO_ID=f"WO-{wo}", ASSET_ID=aid, WO_TYPE="CM", PRIORITY="P1", STATUS="CLOSED",
            CREATED_TS=fts + timedelta(minutes=15), SCHEDULED_TS=fts + timedelta(minutes=30),
            CLOSED_TS=fts + timedelta(hours=dh), FAILURE_MODE=mode,
            DESCRIPTION=f"BREAKDOWN - {FAILURE_DESC[mode]}",
            TECHNICIAN_NOTES=note, PARTS_USED=",".join(parts),
            LABOR_HOURS=round(dh * 1.6, 1), COST_INR=int(45000 + dh * 9000 + rng.integers(0, 40000)),
            DOWNTIME_HOURS=dh, SOURCE="BREAKDOWN"))
    df = pd.DataFrame(rows).sort_values("CREATED_TS").reset_index(drop=True)
    return df


def build_parts():
    parts = [
        ("BRG-6312-C3", "Deep groove ball bearing 6312 C3", "Main Drive Motor|Gearbox|Air Compressor", 2, 5, 6800),
        ("GRS-LGHP2", "High-performance polyurea grease 18kg", "ALL", 6, 3, 14500),
        ("IMP-CP-150", "Coolant pump impeller 150mm", "Coolant Pump", 1, 12, 22000),
        ("SEAL-MS-35", "Mechanical seal 35mm", "Coolant Pump", 3, 6, 9500),
        ("STR-SUC-80", "Suction strainer basket 80mm", "Coolant Pump", 4, 4, 2800),
        ("OIL-ISO-VG220", "Gear oil ISO VG 220 (20L)", "Gearbox|Air Compressor", 8, 2, 7600),
        ("FLT-OIL-90", "Oil filter element 90 micron", "Gearbox|Air Compressor", 5, 3, 1900),
        ("SNS-PT100", "PT100 temperature sensor", "ALL", 6, 4, 3200),
        ("BAL-WT-KIT", "Balancing weight kit", "ALL", 2, 7, 4100),
        ("CPL-INS-L100", "Jaw coupling elastomer insert L100", "Conveyor Drive|Main Drive Motor", 3, 5, 2600),
        ("SHM-KIT-SS", "Stainless shim kit", "ALL", 4, 3, 1800),
        ("MTR-STBY-45KW", "Standby 45kW IE3 motor", "Main Drive Motor", 1, 21, 185000),
        ("BRG-SPN-7014", "Angular contact spindle bearing set 7014", "CNC Spindle", 0, 14, 48000),
    ]
    return pd.DataFrame(parts, columns=["PART_NO", "DESCRIPTION", "COMPATIBLE_ASSET_TYPES",
                                        "ON_HAND_QTY", "LEAD_TIME_DAYS", "UNIT_COST_INR"])


def build_production(assets, rng):
    fails = pd.DataFrame(HIST_FAILURES, columns=["ASSET_ID", "MODE", "TS", "DDAYS", "DH"])
    fails["LINE_ID"] = fails.ASSET_ID.str[:6]
    rows = []
    day = START
    shifts = [("A", 6), ("B", 14), ("C", 22)]
    while day < END:
        for line in IDEAL_CT:
            for sh, h in shifts:
                s0 = day + timedelta(hours=h)
                s1 = s0 + timedelta(hours=8)
                if s1 > END:
                    continue
                planned = 450.0
                minor = float(rng.integers(6, 28))
                breakdown = 0.0
                for _, f in fails[fails.LINE_ID == line].iterrows():
                    f0, f1 = f.TS, f.TS + timedelta(hours=f.DH)
                    ov = (min(s1, f1) - max(s0, f0)).total_seconds() / 60
                    if ov > 0:
                        breakdown += ov
                # degradation slows the line before failure
                perf = rng.uniform(0.82, 0.93)
                qual = rng.uniform(0.975, 0.995)
                for (aid, m, fts, dd, *_) in HIST_FAILURES + [x + (0,) for x in ACTIVE_DEGRADATION]:
                    if aid[:6] == line and fts - timedelta(days=dd) <= s0 < fts:
                        perf -= 0.06
                        qual -= 0.015
                breakdown = min(breakdown, planned)
                changeover = 20.0 if sh == "A" and rng.random() < 0.3 else 0.0
                downtime = min(planned, breakdown + minor + changeover)
                run = planned - downtime
                ict = IDEAL_CT[line]
                total = int(run * 60 / ict * perf)
                good = int(total * qual)
                rows.append(dict(LINE_ID=line, PLANT_CODE=line[:3], PROD_DATE=day.date(), SHIFT=sh,
                                 SHIFT_START_TS=s0, PLANNED_TIME_MIN=planned,
                                 BREAKDOWN_MIN=round(breakdown, 1), MINOR_STOP_MIN=minor,
                                 CHANGEOVER_MIN=changeover, RUN_TIME_MIN=round(run, 1),
                                 IDEAL_CYCLE_TIME_S=ict, TOTAL_COUNT=total, GOOD_COUNT=good))
        day += timedelta(days=1)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    os.makedirs(args.out, exist_ok=True)

    assets = build_assets()
    out = {
        "assets": assets,
        "sensor_readings": build_sensors(assets, rng),
        "failure_events": build_failures(),
        "work_orders": build_work_orders(assets, rng),
        "spare_parts": build_parts(),
        "production_log": build_production(assets, rng),
        "maintenance_docs": pd.DataFrame(DOCS, columns=["DOC_ID", "DOC_TITLE", "ASSET_TYPE", "FAILURE_MODE", "CONTENT"]),
    }
    for name, df in out.items():
        path = os.path.join(args.out, f"{name}.csv")
        df.to_csv(path, index=False, lineterminator="\n")
        print(f"{name:18s} {len(df):>8,d} rows -> {path}")


if __name__ == "__main__":
    main()
