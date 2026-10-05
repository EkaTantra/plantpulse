"""
PlantPulse Command Center - predictive maintenance & OEE on Snowflake.

Backends (picked automatically):
  * Streamlit in Snowflake        -> live, via the active Snowpark session
  * PLANTPULSE_CONNECTION env var -> live from a laptop, via snowflake-connector-python
  * otherwise                     -> demo mode on the bundled CSVs (app/local_engine.py)
"""
import decimal
import json
import os
import re
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(layout="wide", page_title="PlantPulse Command Center", page_icon="❄️",
                   initial_sidebar_state="collapsed")

DB = "PLANTPULSE"
DEMO_NOW = pd.Timestamp("2026-10-05 00:00:00")
OEE_TARGET = 0.80
OPEN_ALERT = ("NEW", "ACKNOWLEDGED")
ST_VER = tuple(int(x) for x in st.__version__.split(".")[:2])


# ----------------------------------------------------------------------------- helpers
def stretch(kind="chart"):
    """Full-width kwargs that work on both old (SiS) and new Streamlit versions."""
    if ST_VER >= (1, 50):
        return {"width": "stretch"} if kind == "button" else {}
    return {"use_container_width": True}


def rerun():
    (getattr(st, "rerun", None) or st.experimental_rerun)()


def lit(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def as_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def fnum(v, fmt="{:.1f}", na="–"):
    try:
        if v is None or pd.isna(v):
            return na
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return na


def fmt_hours(h):
    if h is None or pd.isna(h) or h >= 9000:
        return "no rising trend"
    return f"{h:.0f} h" if h <= 72 else f"{h / 24:.1f} d"


def pretty(mode):
    return str(mode or "NONE").replace("_", " ").title()


def compact_md(text):
    """LLM answers may use #/## headings; render them as small bold lines inside the panel."""
    return re.sub(r"^\s{0,3}#{1,6}\s*(.+)$", r"**\1**", str(text or ""), flags=re.M)


def coerce(df):
    for c in df.columns:
        if df[c].dtype == object:
            nn = df[c].dropna()
            if len(nn) and isinstance(nn.iloc[0], decimal.Decimal):
                df[c] = df[c].astype(float)
    return df


# ----------------------------------------------------------------------------- backends
def detect_backend():
    try:
        from snowflake.snowpark.context import get_active_session
        get_active_session()
        return "sis"
    except Exception:
        pass
    if os.environ.get("PLANTPULSE_CONNECTION"):
        return "connector"
    return "demo"


@st.cache_resource(show_spinner=False)
def _connector():
    import snowflake.connector
    conn = snowflake.connector.connect(connection_name=os.environ["PLANTPULSE_CONNECTION"])
    conn.cursor().execute(f"USE WAREHOUSE {DB}_WH")
    return conn


def _run_sql(sql):
    if BACKEND == "sis":
        from snowflake.snowpark.context import get_active_session
        return coerce(get_active_session().sql(sql).to_pandas())
    cur = _connector().cursor()
    cur.execute(sql)
    try:
        df = cur.fetch_pandas_all()
    except Exception:
        df = pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
    return coerce(df)


@st.cache_data(ttl=300, show_spinner=False)
def query(sql):
    return _run_sql(sql)


def call(sql):
    """Run a CALL statement and return the (JSON-decoded) scalar result."""
    df = _run_sql(sql)
    return as_json(df.iloc[0, 0]) if len(df) else None


class LiveRepo:
    mode = "live"

    def risk(self):
        return query(f"""SELECT r.*, a.PM_INTERVAL_DAYS FROM {DB}.ANALYTICS.ASSET_RISK_SCORES r
                         JOIN {DB}.ERP.ASSETS a ON a.ASSET_ID = r.ASSET_ID ORDER BY r.RISK_SCORE DESC""")

    def lines(self):
        return query(f"SELECT LINE_ID, LINE_NAME, PLANT_NAME, OEE_TARGET FROM {DB}.ERP.PRODUCTION_LINES ORDER BY 1")

    def alerts(self):
        return query(f"""SELECT ALERT_ID, ASSET_ID, CREATED_TS, AS_OF_TS, SEVERITY, RISK_SCORE, ML_PROB, RULE_SCORE,
                                SUSPECTED_MODE, HOURS_TO_ALARM, TO_JSON(EVIDENCE) AS EVIDENCE, STATUS, TRIAGE_NOTE,
                                TRIAGED_BY, TRIAGED_TS, WO_ID
                         FROM {DB}.ANALYTICS.PDM_ALERTS ORDER BY RISK_SCORE DESC""")

    def baselines(self):
        return query(f"SELECT ASSET_ID, BASE_VIB, BASE_TEMP, BASE_CUR FROM {DB}.ANALYTICS.V_ASSET_BASELINE")

    def hourly(self, asset_id, days=7):
        a = lit(asset_id)
        return query(f"""SELECT HOUR_TS, VIB_AVG, VIB_MAX, TEMP_AVG, TEMP_MAX, CUR_AVG, RPM_AVG
                         FROM {DB}.ANALYTICS.V_SENSOR_HOURLY
                         WHERE ASSET_ID = {a} AND HOUR_TS >= DATEADD('day', -{int(days)},
                               (SELECT MAX(READING_TS) FROM {DB}.OT.SENSOR_READINGS WHERE ASSET_ID = {a}))
                         ORDER BY HOUR_TS""")

    def work_orders(self):
        return query(f"""SELECT WO_ID, ASSET_ID, WO_TYPE, PRIORITY, STATUS, CREATED_TS, SCHEDULED_TS, CLOSED_TS,
                                FAILURE_MODE, DESCRIPTION, TECHNICIAN_NOTES, PARTS_USED, DOWNTIME_HOURS, COST_INR,
                                ALERT_ID, RISK_SCORE, AI_JOB_PLAN, CREATED_BY
                         FROM {DB}.ERP.WORK_ORDERS ORDER BY CREATED_TS DESC""")

    def purchase_reqs(self):
        return query(f"""SELECT p.PR_ID, p.PART_NO, s.DESCRIPTION, p.QTY, p.WO_ID, p.STATUS, p.EXPEDITE,
                                s.LEAD_TIME_DAYS, p.CREATED_TS, p.REASON
                         FROM {DB}.ERP.PURCHASE_REQUISITIONS p LEFT JOIN {DB}.ERP.SPARE_PARTS s ON s.PART_NO = p.PART_NO
                         ORDER BY p.PR_ID""")

    def failures(self):
        return query(f"SELECT * FROM {DB}.ERP.FAILURE_EVENTS ORDER BY FAILURE_TS")

    def oee_daily(self):
        return query(f"SELECT * FROM {DB}.ANALYTICS.OEE_DAILY ORDER BY PROD_DATE, LINE_ID")

    def loss_tree(self):
        return query(f"SELECT * FROM {DB}.ANALYTICS.OEE_LOSS_TREE ORDER BY LINE_ID")

    def downtime_impact(self):
        return query(f"SELECT * FROM {DB}.ANALYTICS.ASSET_DOWNTIME_IMPACT ORDER BY BREAKDOWN_HOURS DESC")

    def backtest(self):
        return query(f"SELECT * FROM {DB}.ANALYTICS.V_BACKTEST ORDER BY FAILURE_TS")

    def user(self):
        try:
            return str(query("SELECT CURRENT_USER() AS U").iloc[0, 0])
        except Exception:
            return "planner"

    def triage(self, alert_id, decision, note, user):
        out = call(f"CALL {DB}.ANALYTICS.TRIAGE_ALERT({lit(alert_id)}, {lit(decision)}, {lit(note)}, {lit(user)})")
        st.cache_data.clear()
        return out

    def create_wo(self, alert_id, user):
        out = call(f"CALL {DB}.ANALYTICS.CREATE_PDM_WORK_ORDER({lit(alert_id)}, {lit(user)})")
        st.cache_data.clear()
        return out

    def root_cause(self, asset_id, question):
        return call(f"CALL {DB}.ANALYTICS.ROOT_CAUSE_BRIEF({lit(asset_id)}, {lit(question)})")

    def simulate(self):
        msgs = [call(f"CALL {DB}.ANALYTICS.SIMULATE_SENSOR_STREAM(6)"),
                call(f"CALL {DB}.ANALYTICS.SCORE_ASSETS(TRUE)"),
                call(f"CALL {DB}.ANALYTICS.GENERATE_ALERTS()")]
        st.cache_data.clear()
        return " · ".join(str(m) for m in msgs)


# ----------------------------------------------------------------------------- demo backend
DEMO_LINES = pd.DataFrame({
    "LINE_ID": ["CHN-L1", "CHN-L2", "PUN-L1", "PUN-L2"],
    "LINE_NAME": ["Chennai Line 1 - Crankshaft line", "Chennai Line 2 - Gear housing line",
                  "Pune Line 1 - Engine block machining", "Pune Line 2 - Cylinder head machining"],
    "PLANT_NAME": ["Chennai Machining Plant", "Chennai Machining Plant", "Pune Assembly Plant", "Pune Assembly Plant"],
    "OEE_TARGET": [0.8] * 4,
})
DEMO_PART_QTY = {"BRG-6312-C3": 2, "OIL-ISO-VG220": 2}
MODE_ACTIONS = {
    "BEARING_WEAR": "inspect bearing condition (spectrum / shock pulse), re-grease, and plan bearing replacement",
    "CAVITATION": "check suction head, strainer and NPSH margin; inspect impeller and mechanical seal",
    "LUBRICATION_FAILURE": "check oil level and condition, replace oil and filter, verify the temperature sensor",
    "IMBALANCE": "clean the rotor, check for build-up or loose parts, and field-balance",
    "MISALIGNMENT": "laser-align the coupling, check soft foot and coupling insert wear",
    "ELECTRICAL_WINDING": "run insulation resistance and current-balance tests; prepare the standby motor",
    "SENSOR_FAULT": "check the vibration transmitter, cabling and connector; no mechanical work expected",
}


def _le():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import local_engine
    return local_engine


@st.cache_data(show_spinner=False)
def _demo_risk():
    le = _le()
    r = le.latest_risk().rename(columns={"HOUR_TS": "AS_OF_TS"})
    r["ML_PROB"] = np.nan
    r["SCORED_AT"] = DEMO_NOW
    for c, n in {"VIB_AVG_24H": 2, "BASE_VIB": 2, "VIB_RATIO": 2, "TEMP_AVG_24H": 1, "TEMP_DELTA": 1,
                 "CUR_RATIO": 3, "VIB_SLOPE_PER_DAY": 3, "TEMP_SLOPE_PER_DAY": 2, "HOURS_TO_ALARM": 1}.items():
        r[c] = r[c].round(n)
    return r


@st.cache_data(show_spinner=False)
def _demo_hourly(asset_id):
    le = _le()
    s = le.load()["sensors"]
    return le.hourly(s[s.ASSET_ID == asset_id])


@st.cache_data(show_spinner=False)
def _demo_oee_daily():
    p = _le().oee_shift()
    p["IDEAL_RUN_MIN"] = p.IDEAL_CYCLE_TIME_S * p.TOTAL_COUNT / 60
    g = p.groupby(["LINE_ID", "PROD_DATE"]).sum(numeric_only=True).reset_index()
    g["AVAILABILITY"] = g.RUN_TIME_MIN / g.PLANNED_TIME_MIN
    g["PERFORMANCE"] = g.IDEAL_RUN_MIN / g.RUN_TIME_MIN.replace(0, np.nan)
    g["QUALITY"] = g.GOOD_COUNT / g.TOTAL_COUNT.replace(0, np.nan)
    g["OEE"] = g.AVAILABILITY * g.PERFORMANCE * g.QUALITY
    g = g.merge(DEMO_LINES, on="LINE_ID")
    g["PROD_DATE"] = pd.to_datetime(g.PROD_DATE)
    return g[["LINE_ID", "LINE_NAME", "PLANT_NAME", "PROD_DATE", "AVAILABILITY", "PERFORMANCE", "QUALITY", "OEE",
              "BREAKDOWN_MIN", "MINOR_STOP_MIN", "CHANGEOVER_MIN", "OEE_TARGET"]]


@st.cache_data(show_spinner=False)
def _demo_loss_tree():
    p = _le().oee_shift()
    p["IDEAL_RUN_MIN"] = p.IDEAL_CYCLE_TIME_S * p.TOTAL_COUNT / 60
    p["SPEED_LOSS_MIN"] = p.RUN_TIME_MIN - p.IDEAL_RUN_MIN
    p["QUALITY_LOSS_MIN"] = (p.TOTAL_COUNT - p.GOOD_COUNT) * p.IDEAL_CYCLE_TIME_S / 60
    p["FULLY_PRODUCTIVE_MIN"] = p.GOOD_COUNT * p.IDEAL_CYCLE_TIME_S / 60
    g = p.groupby("LINE_ID").agg(
        PLANNED_MIN=("PLANNED_TIME_MIN", "sum"), BREAKDOWN_LOSS_MIN=("BREAKDOWN_MIN", "sum"),
        MINOR_STOP_LOSS_MIN=("MINOR_STOP_MIN", "sum"), CHANGEOVER_LOSS_MIN=("CHANGEOVER_MIN", "sum"),
        SPEED_LOSS_MIN=("SPEED_LOSS_MIN", "sum"), QUALITY_LOSS_MIN=("QUALITY_LOSS_MIN", "sum"),
        FULLY_PRODUCTIVE_MIN=("FULLY_PRODUCTIVE_MIN", "sum"))
    return g.reset_index()


@st.cache_data(show_spinner=False)
def _demo_backtest():
    le = _le()
    f = le.features()
    rows = []
    for fe in le.load()["failures"].itertuples():
        r = f[(f.ASSET_ID == fe.ASSET_ID) & (f.HOUR_TS < fe.FAILURE_TS)
              & (f.HOUR_TS >= fe.FAILURE_TS - pd.Timedelta(days=8))]
        if r.empty:
            continue
        warn = r.loc[r.RULE_SCORE >= 30, "HOUR_TS"].min()
        pred = r.sort_values("HOUR_TS").SUSPECTED_MODE.iloc[-1]
        rows.append(dict(FAILURE_ID=fe.FAILURE_ID, ASSET_ID=fe.ASSET_ID, FAILURE_MODE=fe.FAILURE_MODE,
                         FAILURE_TS=fe.FAILURE_TS, DOWNTIME_HOURS=fe.DOWNTIME_HOURS, FIRST_WARNING_TS=warn,
                         LEAD_TIME_HOURS=None if pd.isna(warn) else int((fe.FAILURE_TS - warn).total_seconds() // 3600),
                         PREDICTED_MODE=pred, MODE_CORRECT=pred == fe.FAILURE_MODE))
    return pd.DataFrame(rows)


class DemoRepo:
    mode = "demo"

    def _state(self):
        if "demo" not in st.session_state:
            r = _demo_risk()
            a = r[r.RISK_BAND.isin(["HIGH", "MEDIUM"]) | (r.SUSPECTED_MODE == "SENSOR_FAULT")]
            a = a.sort_values(["RISK_SCORE", "ASSET_ID"], ascending=[False, True]).reset_index(drop=True)
            sev = np.where((a.SUSPECTED_MODE == "SENSOR_FAULT") & (a.RISK_BAND == "LOW"), "INFO",
                           np.where((a.RISK_BAND == "HIGH") & (a.CRITICALITY == "A"), "CRITICAL", "WARNING"))
            ev = [json.dumps({"vib_avg_24h": x.VIB_AVG_24H, "baseline_vib": x.BASE_VIB, "vib_ratio": x.VIB_RATIO,
                              "vib_alarm": x.VIBRATION_ALARM_MM_S, "temp_avg_24h": x.TEMP_AVG_24H,
                              "temp_delta": x.TEMP_DELTA, "temp_alarm": x.TEMP_ALARM_C, "current_ratio": x.CUR_RATIO,
                              "vib_slope_per_day": x.VIB_SLOPE_PER_DAY, "temp_slope_per_day": x.TEMP_SLOPE_PER_DAY,
                              "saturated_readings_24h": x.SATURATED_24H, "days_since_pm": x.DAYS_SINCE_PM},
                             default=float) for x in a.itertuples()]
            alerts = pd.DataFrame({
                "ALERT_ID": [f"AL-{1001 + i}" for i in range(len(a))], "ASSET_ID": a.ASSET_ID,
                "CREATED_TS": DEMO_NOW, "AS_OF_TS": a.AS_OF_TS, "SEVERITY": sev, "RISK_SCORE": a.RISK_SCORE,
                "ML_PROB": np.nan, "RULE_SCORE": a.RULE_SCORE, "SUSPECTED_MODE": a.SUSPECTED_MODE,
                "HOURS_TO_ALARM": a.HOURS_TO_ALARM, "EVIDENCE": ev, "STATUS": "NEW", "TRIAGE_NOTE": None,
                "TRIAGED_BY": None, "TRIAGED_TS": pd.NaT, "WO_ID": None})
            st.session_state["demo"] = {"alerts": alerts, "wos": [], "prs": [], "wo_seq": 90001, "pr_seq": 5001}
        return st.session_state["demo"]

    def risk(self):
        return _demo_risk()

    def lines(self):
        return DEMO_LINES

    def alerts(self):
        return self._state()["alerts"].copy()

    def baselines(self):
        return _demo_risk()[["ASSET_ID", "BASE_VIB", "BASE_TEMP", "BASE_CUR"]]

    def hourly(self, asset_id, days=7):
        h = _demo_hourly(asset_id)
        return h[h.HOUR_TS >= h.HOUR_TS.max() - pd.Timedelta(days=days)]

    def work_orders(self):
        wo = _le().load()["work_orders"].copy()
        for c in ["ALERT_ID", "RISK_SCORE", "AI_JOB_PLAN", "CREATED_BY"]:
            wo[c] = None
        new = pd.DataFrame(self._state()["wos"])
        return pd.concat([new, wo], ignore_index=True).sort_values("CREATED_TS", ascending=False)

    def purchase_reqs(self):
        prs = pd.DataFrame(self._state()["prs"], columns=["PR_ID", "PART_NO", "QTY", "WO_ID", "STATUS", "EXPEDITE",
                                                          "CREATED_TS", "REASON"])
        parts = _le().load()["parts"][["PART_NO", "DESCRIPTION", "LEAD_TIME_DAYS"]]
        return prs.merge(parts, on="PART_NO", how="left")

    def failures(self):
        return _le().load()["failures"]

    def oee_daily(self):
        return _demo_oee_daily()

    def loss_tree(self):
        return _demo_loss_tree()

    def downtime_impact(self):
        d = _le().load()
        wo = d["work_orders"]
        cm = wo[wo.WO_TYPE == "CM"].groupby("ASSET_ID").agg(
            BREAKDOWNS=("WO_ID", "count"), BREAKDOWN_HOURS=("DOWNTIME_HOURS", "sum"),
            BREAKDOWN_COST_INR=("COST_INR", "sum"))
        sk = wo[(wo.WO_TYPE == "PM") & (wo.STATUS == "CANCELLED")].groupby("ASSET_ID").size().rename("SKIPPED_PMS")
        out = d["assets"][["ASSET_ID", "ASSET_NAME", "ASSET_TYPE", "LINE_ID", "CRITICALITY"]].set_index("ASSET_ID")
        out = out.join(cm).join(sk).fillna({"BREAKDOWNS": 0, "BREAKDOWN_HOURS": 0, "BREAKDOWN_COST_INR": 0,
                                            "SKIPPED_PMS": 0})
        return out.reset_index().sort_values("BREAKDOWN_HOURS", ascending=False)

    def backtest(self):
        return _demo_backtest()

    def user(self):
        return "demo.planner"

    def triage(self, alert_id, decision, note, user):
        a = self._state()["alerts"]
        i = a.index[a.ALERT_ID == alert_id]
        a.loc[i, "STATUS"] = "DISMISSED" if decision == "DISMISS" else "ACKNOWLEDGED"
        a.loc[i, ["TRIAGE_NOTE", "TRIAGED_BY"]] = [note, user]
        a.loc[i, "TRIAGED_TS"] = DEMO_NOW
        return f"Alert {alert_id} -> {decision}"

    def root_cause(self, asset_id, question):
        r = _demo_risk().set_index("ASSET_ID").loc[asset_id]
        mode = r.SUSPECTED_MODE
        hits = _le().search_docs(question or "abnormal vibration temperature", r.ASSET_TYPE,
                                 mode if mode not in ("NONE", "SENSOR_FAULT") else None, k=5)
        cites = " ".join(f"[{d}]" for d in hits.DOC_ID.head(3))
        f = self.failures()
        same = f[f.FAILURE_MODE == mode]
        dt = same.DOWNTIME_HOURS.mean() if len(same) else f.DOWNTIME_HOURS.mean()
        hrs = r.HOURS_TO_ALARM
        when = "within 24 h" if hrs < 72 else "within 7 days"
        if mode == "NONE":
            diagnosis = f"No failure signature detected on {asset_id}; condition is within normal limits."
        else:
            diagnosis = f"**{pretty(mode)}** suspected on {r.ASSET_NAME} ({asset_id}), risk {r.RISK_SCORE:.0f}/100 ({r.RISK_BAND})."
        answer = (
            f"**1) Diagnosis** - {diagnosis}\n\n"
            f"**2) Evidence** - 24 h vibration {r.VIB_AVG_24H:.2f} mm/s = {r.VIB_RATIO:.2f}x baseline "
            f"{r.BASE_VIB:.2f} (alarm {r.VIBRATION_ALARM_MM_S:.1f}); temperature {r.TEMP_AVG_24H:.1f} °C "
            f"({r.TEMP_DELTA:+.1f} °C vs baseline, alarm {r.TEMP_ALARM_C:.0f}); current ratio {r.CUR_RATIO:.3f}; "
            f"projected alarm: {fmt_hours(hrs)}; {int(r.DAYS_SINCE_PM)} days since last PM. {cites}\n\n"
            f"**3) Recommended action** - {MODE_ACTIONS.get(mode, 'continue routine monitoring')}; "
            f"schedule {when}.\n\n"
            f"**4) Risk if ignored** - comparable breakdowns in the history cost on average {dt:.1f} h of downtime."
        )
        sources = [{"doc_id": h.DOC_ID, "title": h.DOC_TITLE, "type": h.SOURCE} for h in hits.itertuples()]
        evidence = {k: (None if pd.isna(r[k]) else r[k]) if not isinstance(r[k], str) else r[k]
                    for k in ["ASSET_TYPE", "LINE_ID", "CRITICALITY", "RISK_SCORE", "RISK_BAND", "SUSPECTED_MODE",
                              "HOURS_TO_ALARM", "VIB_AVG_24H", "BASE_VIB", "VIB_RATIO", "VIBRATION_ALARM_MM_S",
                              "TEMP_AVG_24H", "TEMP_DELTA", "TEMP_ALARM_C", "CUR_RATIO", "DAYS_SINCE_PM"]}
        evidence = {"ASSET_ID": asset_id, "AS_OF_TS": str(r.AS_OF_TS), **evidence}
        return {"asset_id": asset_id, "answer": answer, "evidence": json.loads(json.dumps(evidence, default=float)),
                "sources": sources}

    def create_wo(self, alert_id, user):
        s = self._state()
        a = s["alerts"]
        row = a[a.ALERT_ID == alert_id].iloc[0]
        if row.STATUS == "WO_CREATED":
            return {"status": "exists", "wo_id": row.WO_ID}
        r = _demo_risk().set_index("ASSET_ID").loc[row.ASSET_ID]
        mode, hours = row.SUSPECTED_MODE, float(row.HOURS_TO_ALARM)
        if mode == "SENSOR_FAULT":
            priority, window_h, why = "P4", 168, "instrumentation check only (DOC-019)"
        elif hours < 72 and r.CRITICALITY in ("A", "B"):
            priority, window_h, why = "P2", 24, f"failure predicted within 72 h on criticality {r.CRITICALITY} asset (DOC-016)"
        else:
            priority, window_h, why = "P3", 168, "degradation detected, failure not expected within 72 h (DOC-016)"
        stock = _le().load()["parts"].set_index("PART_NO")
        wo_id = f"WO-PDM-{s['wo_seq']}"
        s["wo_seq"] += 1
        part_lines, prs = [], []
        for p in _le().MODE_PARTS.get(mode, []):
            if p not in stock.index:
                continue
            qty, sp = DEMO_PART_QTY.get(p, 1), stock.loc[p]
            short = sp.ON_HAND_QTY < qty
            part_lines.append(f"{p} x{qty} ({sp.DESCRIPTION}) - on hand {sp.ON_HAND_QTY}" + (" - SHORTAGE" if short else ""))
            if short:
                pr = {"PR_ID": f"PR-{s['pr_seq']}", "PART_NO": p, "QTY": int(qty - sp.ON_HAND_QTY), "WO_ID": wo_id,
                      "STATUS": "OPEN", "EXPEDITE": bool(sp.LEAD_TIME_DAYS * 24 > hours), "CREATED_TS": DEMO_NOW,
                      "REASON": "Stock-out for predictive WO"}
                s["pr_seq"] += 1
                s["prs"].append(pr)
                prs.append({"pr_id": pr["PR_ID"], "part_no": p, "qty": pr["QTY"], "expedite": pr["EXPEDITE"]})
        parts_used = [p.split(" ")[0] for p in part_lines]
        plan = ("1. Apply lockout/tagout per SOP DOC-015 and verify zero energy.\n"
                "2. Confirm the diagnosis: vibration spectrum, bearing/housing temperature, oil or grease condition.\n"
                f"3. Corrective task: {MODE_ACTIONS.get(mode, 'inspect and correct')}.\n"
                f"4. Fit reserved parts: {', '.join(parts_used) or 'none required'}.\n"
                "5. Restart and confirm vibration < 1.2x baseline and temperature within 5 °C of baseline.\n"
                "6. Record as-found condition, parts used and readings in the closing notes.")
        rca = self.root_cause(row.ASSET_ID, f"Root cause and job plan for suspected {mode}")
        s["wos"].append({
            "WO_ID": wo_id, "ASSET_ID": row.ASSET_ID, "WO_TYPE": "PDM", "PRIORITY": priority, "STATUS": "OPEN",
            "CREATED_TS": DEMO_NOW, "SCHEDULED_TS": row.AS_OF_TS + pd.Timedelta(hours=window_h),
            "FAILURE_MODE": mode, "PARTS_USED": ",".join(parts_used), "SOURCE": "PLANTPULSE",
            "DESCRIPTION": (f"PREDICTIVE - {pretty(mode)} suspected on {r.ASSET_NAME}. Risk {row.RISK_SCORE}/100, "
                            f"projected alarm in {round(min(hours, 9999))} h. Raised from alert {alert_id}."),
            "ALERT_ID": alert_id, "RISK_SCORE": row.RISK_SCORE, "AI_JOB_PLAN": plan, "CREATED_BY": user})
        i = a.index[a.ALERT_ID == alert_id]
        a.loc[i, ["STATUS", "WO_ID", "TRIAGED_BY"]] = ["WO_CREATED", wo_id, user]
        a.loc[i, "TRIAGED_TS"] = DEMO_NOW
        return {"status": "created", "wo_id": wo_id, "asset_id": row.ASSET_ID, "priority": priority,
                "priority_reason": why, "schedule_within_hours": window_h, "failure_mode": mode,
                "parts": part_lines, "purchase_requisitions": prs, "job_plan": plan,
                "diagnosis": rca["answer"], "sources": rca["sources"]}

    def simulate(self):
        return None


BACKEND = detect_backend()
repo = DemoRepo() if BACKEND == "demo" else LiveRepo()
LIVE = repo.mode == "live"

# ----------------------------------------------------------------------------- theme
INK, INK2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
NAVY, BRAND, SKY = "#11567F", "#11567F", "#29B5E8"   # Snowflake Mid-Blue / Snowflake Blue
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b", "info": "#6b7785"}
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]       # validated categorical order (CVD-safe)
CRIT_RAMP = {"A": "#1c5cab", "B": "#5598e7", "C": "#9ec5f4"}  # ordinal blue ramp for criticality
SEV_STYLE = {"CRITICAL": ("critical", "●"), "WARNING": ("warning", "▲"), "INFO": ("info", "ℹ")}
BAND_STYLE = {"HIGH": ("critical", "●"), "MEDIUM": ("warning", "▲"), "LOW": ("good", "✓")}
PRIO_STYLE = {"P1": "critical", "P2": "serious", "P3": "warning", "P4": "info"}
TYPE_ICON = {}
TYPE_ORDER = ["Main Drive Motor", "Gearbox", "CNC Spindle", "Air Compressor", "Coolant Pump", "Conveyor Drive"]
FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
PAGES = ["Control Room", "Alerts", "Asset 360", "Copilot", "OEE", "Work Orders", "How It Works"]
APP_DIR = os.path.dirname(os.path.abspath(__file__))

st.markdown("""
<style>
:root { --pp-navy:#0D3B5C; --pp-brand:#11567F; --pp-sky:#29B5E8; --pp-ink:#0b0b0b; --pp-ink2:#52514e;
  --pp-muted:#898781; --pp-line:rgba(11,31,58,.12); --pp-soft:#f4f7fa;
  --pp-crit:#d03b3b; --pp-crit-t:#fbeaea; --pp-warn:#fab219; --pp-warn-t:#fff5dc; --pp-ser:#ec835a; --pp-ser-t:#fdeee7;
  --pp-good:#0ca30c; --pp-good-t:#e9f6e9; --pp-info:#6b7785; --pp-info-t:#eef1f4; }
/* full-screen app: no sidebar, no Streamlit chrome */
section[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"],
.stDeployButton, [data-testid="stAppDeployButton"], [data-testid="stToolbar"], [data-testid="stMainMenu"], #MainMenu,
[data-testid="stDecoration"] { display: none !important; }
header[data-testid="stHeader"] { background: transparent; height: 0; }
.block-container { padding: 1.1rem 2.2rem 2.5rem; max-width: 100%; }
.stApp { background: #ffffff; }
[data-testid="stMain"], section.main, .block-container, [data-testid="stMainBlockContainer"],
[data-testid="stAppViewBlockContainer"] { background: transparent !important; }

/* app bar */
.pp-appbar { background: linear-gradient(110deg, #0D3B5C 0%, #11567F 45%, #1a86bd 100%); color: #fff; border-radius: 16px;
  padding: 14px 22px; display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap;
  box-shadow: 0 8px 24px rgba(11,31,58,.18); margin-bottom: 12px; }
.pp-brand { display: flex; align-items: center; gap: 12px; }
.pp-logo { width: 42px; height: 42px; border-radius: 12px; background: #29B5E8; border: 1px solid rgba(255,255,255,.35);
  display: flex; align-items: center; justify-content: center; font-size: 1.45rem; color: #fff; }
.pp-brand-t { font-size: 1.35rem; font-weight: 800; letter-spacing: -.01em; line-height: 1.1; }
.pp-brand-s { font-size: .78rem; color: #cfe3ee; }
.pp-appbar-r { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
.pp-fresh { color: #cfe3ee; font-size: .8rem; margin-right: 6px; text-align: right; line-height: 1.3; }
.pp-fresh b { color: #fff; }
.pp-pill { background: rgba(255,255,255,.10); border: 1px solid rgba(255,255,255,.22); color: #fff; border-radius: 999px;
  padding: 5px 12px; font-size: .82rem; font-weight: 600; white-space: nowrap; }
.pp-pill b { font-size: 1rem; margin-right: 2px; }
.pp-badge { border-radius: 999px; padding: 5px 12px; font-size: .74rem; font-weight: 800; letter-spacing: .04em; white-space: nowrap; }
.pp-badge.live { background: rgba(12,163,12,.22); color: #c9f7c9; border: 1px solid rgba(80,220,80,.45); }
.pp-badge.demo { background: rgba(250,178,25,.18); color: #ffe3a6; border: 1px solid rgba(250,178,25,.55); }

/* segmented controls (top navigation, copilot mode, time window): horizontal radios styled like Snowsight */
.st-key-nav div[role="radiogroup"], .st-key-cop_mode div[role="radiogroup"], .st-key-a360_win div[role="radiogroup"] {
  flex-direction: row; flex-wrap: nowrap; gap: 2px; width: fit-content; max-width: 100%; overflow-x: auto;
  background: #fff; border: 1px solid #dbe3ea; border-radius: 10px; padding: 3px; box-shadow: 0 1px 2px rgba(13,59,92,.06); }
.st-key-nav label[data-testid="stRadioOption"], .st-key-nav label[data-baseweb="radio"], .st-key-cop_mode label[data-testid="stRadioOption"], .st-key-cop_mode label[data-baseweb="radio"], .st-key-a360_win label[data-testid="stRadioOption"], .st-key-a360_win label[data-baseweb="radio"] { background: transparent; border: 0; border-radius: 8px; padding: 6px 14px; margin: 0; cursor: pointer;
  transition: background .12s; }
.st-key-nav label[data-testid="stRadioOption"]:hover, .st-key-nav label[data-baseweb="radio"]:hover, .st-key-cop_mode label[data-testid="stRadioOption"]:hover, .st-key-cop_mode label[data-baseweb="radio"]:hover, .st-key-a360_win label[data-testid="stRadioOption"]:hover, .st-key-a360_win label[data-baseweb="radio"]:hover { background: #eaf6fc; }
.st-key-nav label[data-testid="stRadioOption"][data-selected="true"], .st-key-cop_mode label[data-testid="stRadioOption"][data-selected="true"], .st-key-a360_win label[data-testid="stRadioOption"][data-selected="true"], .st-key-nav label[data-baseweb="radio"]:has(input:checked), .st-key-cop_mode label[data-baseweb="radio"]:has(input:checked), .st-key-a360_win label[data-baseweb="radio"]:has(input:checked) {
  background: var(--pp-brand); box-shadow: 0 2px 6px rgba(17,86,127,.28); }
.st-key-nav label[data-testid="stRadioOption"][data-selected="true"] p, .st-key-cop_mode label[data-testid="stRadioOption"][data-selected="true"] p, .st-key-a360_win label[data-testid="stRadioOption"][data-selected="true"] p, .st-key-nav label[data-baseweb="radio"]:has(input:checked) p, .st-key-cop_mode label[data-baseweb="radio"]:has(input:checked) p, .st-key-a360_win label[data-baseweb="radio"]:has(input:checked) p { color: #fff; }
.st-key-nav label[data-testid="stRadioOption"] > div > div:first-child, .st-key-cop_mode label[data-testid="stRadioOption"] > div > div:first-child, .st-key-a360_win label[data-testid="stRadioOption"] > div > div:first-child, .st-key-nav label[data-baseweb="radio"] > div:first-child, .st-key-cop_mode label[data-baseweb="radio"] > div:first-child, .st-key-a360_win label[data-baseweb="radio"] > div:first-child { display: none; }
.st-key-nav label p, .st-key-cop_mode label p, .st-key-a360_win label p { font-size: .88rem; font-weight: 650; color: #3b4a59; white-space: nowrap;
  margin: 0; line-height: 1.6; }
.st-key-nav [data-testid="stWidgetLabel"], .st-key-cop_mode [data-testid="stWidgetLabel"], .st-key-a360_win [data-testid="stWidgetLabel"] { display: none; }
/* filter-row controls share the segmented bar's height and border */
.st-key-f_plant div[data-baseweb="select"] > div, .st-key-f_line div[data-baseweb="select"] > div { min-height: 40px;
  border: 1px solid #dbe3ea; border-radius: 10px; background: #fff; }
.st-key-f_sim button { min-height: 40px; border-radius: 10px; border: 1px solid var(--pp-brand); color: var(--pp-brand);
  font-weight: 650; background: #fff; }
.st-key-f_sim button p { color: var(--pp-brand); font-weight: 650; }
.st-key-f_sim button:hover { background: #eaf6fc; }

/* page header */
.pp-ph { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; margin: 14px 0 12px; flex-wrap: wrap; }
.pp-ph-t { font-size: 1.55rem; font-weight: 800; color: var(--pp-ink); letter-spacing: -.01em; }
.pp-ph-s { font-size: .9rem; color: var(--pp-ink2); margin-top: 2px; }

/* KPI cards */
.pp-kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; margin-bottom: 14px; }
@media (max-width: 1150px) { .pp-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.pp-kpi { background: #fff; border: 1px solid var(--pp-line); border-radius: 14px; padding: 14px 16px; box-shadow: 0 1px 3px rgba(11,31,58,.06); }
.pp-kpi-top { display: flex; align-items: center; gap: 8px; color: var(--pp-ink2); font-size: .8rem; font-weight: 650; }
.pp-ic { width: 30px; height: 30px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
  font-size: .9rem; font-weight: 800; background: #e3f5fc; color: var(--pp-brand); flex: none; }
.pp-kpi-val { font-size: 1.95rem; font-weight: 760; color: var(--pp-ink); margin-top: 8px; line-height: 1.05; }
.pp-kpi-sub { font-size: .78rem; color: var(--pp-muted); margin-top: 5px; }
.pp-neg { color: #b42f2f; font-weight: 650; } .pp-pos { color: #006300; font-weight: 650; }

/* next best action */
.pp-nba { border-radius: 14px; padding: 14px 18px; display: flex; align-items: center; gap: 14px;
  border: 1px solid rgba(208,59,59,.35); background: linear-gradient(90deg, var(--pp-crit-t), #fff 75%); }
.pp-nba.good { border-color: rgba(12,163,12,.3); background: linear-gradient(90deg, var(--pp-good-t), #fff 75%); }
.pp-nba .pp-ic { width: 42px; height: 42px; font-size: 1.2rem; background: var(--pp-crit); color: #fff; }
.pp-nba.good .pp-ic { background: var(--pp-good); }
.pp-nba-k { font-size: .7rem; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; color: var(--pp-ink2); }
.pp-nba-t { font-weight: 750; color: var(--pp-ink); font-size: 1.02rem; }
.pp-nba-s { color: var(--pp-ink2); font-size: .86rem; }

/* chips, cards, sections */
.pp-chip { display: inline-flex; align-items: center; gap: 5px; padding: 2px 10px; border-radius: 999px; font-size: .74rem;
  font-weight: 700; letter-spacing: .02em; border: 1px solid transparent; white-space: nowrap; vertical-align: middle; }
.pp-chip.critical { background: var(--pp-crit-t); color: #a32626; border-color: rgba(208,59,59,.4); }
.pp-chip.serious { background: var(--pp-ser-t); color: #9a3f17; border-color: rgba(236,131,90,.5); }
.pp-chip.warning { background: var(--pp-warn-t); color: #7d5200; border-color: rgba(250,178,25,.6); }
.pp-chip.good { background: var(--pp-good-t); color: #006300; border-color: rgba(12,163,12,.35); }
.pp-chip.info { background: var(--pp-info-t); color: #45505c; border-color: rgba(107,119,133,.35); }
.pp-chip.brand { background: #e6f1f8; color: var(--pp-brand); border-color: rgba(17,86,127,.3); }
.pp-card { background: #fff; border: 1px solid var(--pp-line); border-radius: 14px; padding: 14px 18px; margin-bottom: 12px;
  box-shadow: 0 1px 3px rgba(11,31,58,.05); }
.pp-sec { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin: 8px 0 8px; }
.pp-sec-t { font-size: 1.02rem; font-weight: 750; color: var(--pp-ink); }
.pp-sec-s { font-size: .8rem; color: var(--pp-muted); }

/* alert queue cards */
.st-key-alert_pick, .st-key-alert_pick [data-testid="stRadio"], .st-key-alert_pick [data-testid="stRadioGroup"] { width: 100%; }
.st-key-alert_pick div[role="radiogroup"] { gap: 8px; width: 100%; display: flex; flex-direction: column; align-items: stretch; }
.st-key-alert_pick label[data-testid="stRadioOption"], .st-key-alert_pick label[data-baseweb="radio"] { width: 100%;
  box-sizing: border-box; background: #fff; border: 1px solid var(--pp-line); border-radius: 12px; padding: 10px 14px; margin: 0;
  transition: box-shadow .12s, border-color .12s; cursor: pointer; }
.st-key-alert_pick label[data-testid="stRadioOption"]:hover, .st-key-alert_pick label[data-baseweb="radio"]:hover {
  border-color: rgba(17,86,127,.45); box-shadow: 0 2px 10px rgba(11,31,58,.08); }
.st-key-alert_pick label[data-testid="stRadioOption"][data-selected="true"],
.st-key-alert_pick label[data-baseweb="radio"]:has(input:checked) { border-color: var(--pp-brand); background: #f3f9fc;
  box-shadow: 0 0 0 2px rgba(17,86,127,.18); }
.st-key-alert_pick label[data-testid="stRadioOption"] > div > div:first-child,
.st-key-alert_pick label[data-baseweb="radio"] > div:first-child { display: none; }
.st-key-alert_pick label p { font-size: .9rem; line-height: 1.5; }

/* evidence */
.pp-asset-h { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.pp-asset-k { font-size: .72rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; color: var(--pp-muted); }
.pp-asset-t { font-size: 1.3rem; font-weight: 780; color: var(--pp-ink); margin: 2px 0; }
.pp-asset-t span { font-size: .95rem; font-weight: 600; color: var(--pp-ink2); margin-left: 6px; }
.pp-asset-s { font-size: .84rem; color: var(--pp-ink2); }
.pp-bar { margin: 10px 0 16px; }
.pp-bar-h { display: flex; justify-content: space-between; font-size: .86rem; color: var(--pp-ink2); font-weight: 600; }
.pp-bar-h b { color: var(--pp-ink); font-size: 1.05rem; }
.pp-track { position: relative; height: 12px; background: #eef1f4; border-radius: 999px; margin-top: 6px; }
.pp-fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px; }
.pp-tick { position: absolute; top: -4px; width: 2px; height: 20px; border-radius: 2px; }
.pp-tick.base { background: #52514e; } .pp-tick.alarm { background: var(--pp-crit); }
.pp-bar-l { display: flex; justify-content: space-between; font-size: .74rem; color: var(--pp-muted); margin-top: 5px; }
.pp-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 4px 0 10px; }
.pp-stat { background: var(--pp-soft); border-radius: 12px; padding: 10px 12px; }
.pp-stat-l { font-size: .72rem; color: var(--pp-ink2); font-weight: 650; text-transform: uppercase; letter-spacing: .05em; }
.pp-stat-v { font-size: 1.25rem; font-weight: 760; color: var(--pp-ink); margin-top: 2px; }

/* floor map */
.pp-floor, .pp-lines { display: grid; gap: 12px; }
.pp-c1 { grid-template-columns: minmax(0, 1fr); } .pp-c2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.pp-c3 { grid-template-columns: repeat(3, minmax(0, 1fr)); } .pp-c4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
@media (max-width: 1100px) { .pp-floor.pp-c3, .pp-floor.pp-c4 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.pp-lane { background: var(--pp-soft); border: 1px solid var(--pp-line); border-radius: 14px; padding: 12px; }
.pp-lane-h { font-weight: 750; color: var(--pp-ink); font-size: .95rem; }
.pp-lane-s { font-size: .76rem; color: var(--pp-muted); margin-bottom: 10px; }
.pp-tiles { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.pp-tile { background: #fff; border: 1px solid var(--pp-line); border-radius: 12px; padding: 9px 10px; }
.pp-tile.critical { background: var(--pp-crit-t); border-color: rgba(208,59,59,.45); }
.pp-tile.warning { background: var(--pp-warn-t); border-color: rgba(250,178,25,.6); }
.pp-tile.info { background: var(--pp-info-t); border-color: rgba(107,119,133,.4); }
.pp-tile-top { display: flex; justify-content: space-between; align-items: center; gap: 4px; }
.pp-tile-id { font-weight: 750; font-size: .82rem; color: var(--pp-ink); white-space: nowrap; }
.pp-tile-risk { font-size: 1.15rem; font-weight: 780; color: var(--pp-ink); line-height: 1.1; white-space: nowrap; }
.pp-tile-type { font-size: .74rem; color: var(--pp-ink2); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pp-tile-foot { margin-top: 6px; }
.pp-tile-mode { margin-top: 4px; font-size: .72rem; color: var(--pp-ink2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* line OEE cards */
.pp-lines { margin-bottom: 10px; }
.pp-line-v { font-size: 1.7rem; font-weight: 780; color: var(--pp-ink); }
.pp-apq { display: flex; gap: 12px; font-size: .78rem; color: var(--pp-ink2); margin-top: 4px; }
.pp-apq b { color: var(--pp-ink); }

/* copilot */
.pp-q { background: var(--pp-brand); color: #fff; border-radius: 16px 16px 4px 16px; padding: 10px 14px; margin: 6px 0 8px auto;
  max-width: 80%; width: fit-content; font-weight: 600; box-shadow: 0 4px 12px rgba(17,86,127,.2); }
.pp-src { display: flex; gap: 10px; align-items: center; padding: 6px 0; border-bottom: 1px dashed var(--pp-line); font-size: .86rem; }
.pp-src:last-child { border-bottom: none; }
.pp-sql { background: #0f1b2b; color: #cfe3ee; border-radius: 10px; padding: 10px 12px; font-family: Consolas, monospace;
  font-size: .78rem; white-space: pre-wrap; word-break: break-word; }

/* tickets */
.pp-ticket { background: #fff; border: 1px solid var(--pp-line); border-radius: 14px; padding: 12px 16px; margin-bottom: 6px;
  display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; box-shadow: 0 1px 3px rgba(11,31,58,.05); }
.pp-ticket-id { font-weight: 800; color: var(--pp-brand); font-size: 1rem; }
.pp-ticket-s { font-size: .84rem; color: var(--pp-ink2); margin-top: 3px; }

.pp-empty { text-align: center; padding: 52px 24px; }
.pp-empty-ic { width: 56px; height: 56px; border-radius: 50%; margin: 0 auto 10px; background: #e3f5fc; color: #29B5E8;
  display: flex; align-items: center; justify-content: center; font-size: 1.7rem; }
.pp-legend { font-size: .8rem; color: var(--pp-ink2); line-height: 1.9; }

/* how it works */
.pp-steps { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 1200px) { .pp-steps { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
.pp-step { background: #fff; border: 1px solid var(--pp-line); border-radius: 14px; padding: 14px; }
.pp-step-n { width: 28px; height: 28px; border-radius: 50%; background: linear-gradient(135deg, #11567F, #29B5E8); color: #fff; font-weight: 800;
  display: flex; align-items: center; justify-content: center; font-size: .85rem; }
.pp-step-t { font-weight: 750; margin: 8px 0 4px; color: var(--pp-ink); }
.pp-step-s { font-size: .8rem; color: var(--pp-ink2); line-height: 1.45; }
.pp-step-x { font-size: .72rem; color: var(--pp-brand); font-weight: 700; margin-top: 8px; font-family: Consolas, monospace; }
</style>
""", unsafe_allow_html=True)


def chip(text, kind, icon=""):
    return f'<span class="pp-chip {kind}">{icon + " " if icon else ""}{text}</span>'


def sev_chip(sev):
    kind, icon = SEV_STYLE.get(sev, ("info", "ℹ"))
    return chip(sev, kind, icon)


def band_chip(band, mode=None):
    if band == "LOW" and mode == "SENSOR_FAULT":
        return chip("SENSOR", "info", "ℹ")
    kind, icon = BAND_STYLE.get(band, ("info", "ℹ"))
    return chip(band, kind, icon)


def html(s):
    st.markdown(s, unsafe_allow_html=True)


def section(title, sub=""):
    html(f'<div class="pp-sec"><span class="pp-sec-t">{title}</span><span class="pp-sec-s">{sub}</span></div>')


def page_header(title, sub):
    html(f'<div class="pp-ph"><div><div class="pp-ph-t">{title}</div><div class="pp-ph-s">{sub}</div></div></div>')


def style_fig(fig, height=300, **kw):
    fig.update_layout(height=height, margin=dict(l=6, r=10, t=10, b=6), paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", font=dict(family=FONT, size=12, color=INK2),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=12, color=INK2)),
                      hoverlabel=dict(bgcolor="#ffffff", bordercolor=GRID, font=dict(family=FONT, color=INK)))
    fig.update_xaxes(showgrid=False, linecolor=AXIS, tickfont=dict(color=MUTED), zeroline=False)
    fig.update_yaxes(gridcolor=GRID, gridwidth=1, zeroline=False, showline=False, tickfont=dict(color=MUTED))
    fig.update_layout(**kw)
    return fig


def goto(page, asset=None, alert=None):
    """Navigation callback: switch page and optionally focus an asset / alert."""
    st.session_state["nav"] = page
    if asset:
        st.session_state["focus_asset"] = asset
        for k in ("a360_pick", "cop_asset"):
            st.session_state.pop(k, None)
    if alert:
        st.session_state["alert_pick"] = alert


# ----------------------------------------------------------------------------- load data
try:
    risk = repo.risk().copy()
    alerts = repo.alerts()
    lines = repo.lines()
except Exception as e:
    st.error(f"Could not load PlantPulse data: {e}")
    st.stop()

asset_cols = ["ASSET_ID", "ASSET_NAME", "ASSET_TYPE", "LINE_ID", "PLANT_NAME", "CRITICALITY"]
alerts = alerts.merge(risk[asset_cols], on="ASSET_ID", how="left")
as_of = pd.to_datetime(risk.AS_OF_TS).max()
scored_at = pd.to_datetime(risk.SCORED_AT).max()
line_name = lines.set_index("LINE_ID").LINE_NAME.to_dict()
ALL_LINES = sorted(lines.LINE_ID.tolist())


def short_line(lid):
    return line_name.get(lid, lid).split(" - ")[-1]


def line_color(line_id):
    """Colour follows the entity (fixed order of all lines), never its rank in a filtered view."""
    return SERIES[ALL_LINES.index(line_id) % len(SERIES)] if line_id in ALL_LINES else SERIES[0]


# ----------------------------------------------------------------------------- app bar + navigation + filters
open_all = alerts[alerts.STATUS.isin(OPEN_ALERT)]
sev_counts = open_all.SEVERITY.value_counts()
pills = "".join(f'<span class="pp-pill"><b>{int(sev_counts.get(s, 0))}</b> {SEV_STYLE[s][1]} {s.lower()}</span>'
                for s in ["CRITICAL", "WARNING", "INFO"])
badge = ('<span class="pp-badge live">● LIVE · SNOWFLAKE</span>' if LIVE
         else '<span class="pp-badge demo">● DEMO · SYNTHETIC DATA</span>')
html(f"""<div class="pp-appbar"><div class="pp-brand"><div class="pp-logo">❄︎</div><div>
<div class="pp-brand-t">PlantPulse</div><div class="pp-brand-s">Predictive maintenance &amp; OEE command center · Pune &amp; Chennai</div>
</div></div><div class="pp-appbar-r"><div class="pp-fresh">Telemetry as of <b>{as_of:%d %b %Y, %H:%M}</b><br>
scored {scored_at:%d %b %H:%M}{" (account time)" if LIVE else " · demo snapshot"}</div>{pills}{badge}</div></div>""")

if st.session_state.get("nav") not in PAGES:
    st.session_state["nav"] = PAGES[0]
n1, n2, n3, n4 = st.columns([6.6, 1.45, 1.45, 1.5], gap="small", vertical_alignment="center") if ST_VER >= (1, 36) \
    else st.columns([6.6, 1.45, 1.45, 1.5], gap="small")
with n1:
    page = st.radio("Navigation", PAGES, key="nav", horizontal=True, label_visibility="collapsed")
with n2:
    plants = ["All plants"] + sorted(lines.PLANT_NAME.unique().tolist())
    plant = st.selectbox("Plant", plants, key="f_plant", label_visibility="collapsed")
with n3:
    line_opts = lines if plant == "All plants" else lines[lines.PLANT_NAME == plant]
    line = st.selectbox("Line", ["All lines"] + line_opts.LINE_ID.tolist(), key="f_line", label_visibility="collapsed",
                        format_func=lambda x: x if x == "All lines" else f"{x} · {short_line(x)}")
with n4:
    sim = st.button("▶  Simulate next hour", key="f_sim",
                    help="Stream the next hour of sensor data, rescore every asset and raise alerts "
                         "(runs in Snowflake; live mode only)", **stretch("button"))
sel_lines = line_opts.LINE_ID.tolist() if line == "All lines" else [line]
if sim:
    if LIVE:
        with st.spinner("Streaming 6 × 10-min readings, rescoring and raising alerts..."):
            try:
                st.session_state["flash"] = ("success", repo.simulate())
            except Exception as e:
                st.session_state["flash"] = ("error", f"Simulation failed: {e}")
        rerun()
    else:
        st.session_state["flash"] = ("info", "The live sensor stream runs inside Snowflake; the public demo uses a "
                                             "fixed synthetic snapshot.")
if "flash" in st.session_state:
    kind, msg = st.session_state.pop("flash")
    getattr(st, kind)(msg)

scope = ("both plants" if plant == "All plants" else plant) + ("" if line == "All lines" else f" · {line}")
f_risk = risk[risk.LINE_ID.isin(sel_lines)]
f_alerts = alerts[alerts.LINE_ID.isin(sel_lines)]
open_alerts = f_alerts[f_alerts.STATUS.isin(OPEN_ALERT)]


@st.cache_data(ttl=300, show_spinner=False)
def _oee_daily():
    o = repo.oee_daily().copy()
    o["PROD_DATE"] = pd.to_datetime(o.PROD_DATE)
    return o


oee = _oee_daily()
f_oee = oee[oee.LINE_ID.isin(sel_lines)]
last_day = f_oee.PROD_DATE.max()
oee_7 = f_oee[f_oee.PROD_DATE > last_day - pd.Timedelta(days=7)].OEE.mean()
oee_prev = f_oee[(f_oee.PROD_DATE <= last_day - pd.Timedelta(days=7))
                 & (f_oee.PROD_DATE > last_day - pd.Timedelta(days=14))].OEE.mean()
wos = repo.work_orders().copy()
wos = wos.merge(risk[["ASSET_ID", "LINE_ID", "ASSET_NAME"]], on="ASSET_ID", how="left")
f_wos = wos[wos.LINE_ID.isin(sel_lines)]
open_pdm = f_wos[(f_wos.WO_TYPE == "PDM") & ~f_wos.STATUS.isin(["CLOSED", "CANCELLED"])]
failures = repo.failures()
mode_dt = failures.groupby("FAILURE_MODE").DOWNTIME_HOURS.mean()
at_risk = f_risk[f_risk.RISK_BAND.isin(["HIGH", "MEDIUM"]) & ~f_risk.SUSPECTED_MODE.isin(["NONE", "SENSOR_FAULT"])]
avoidable = float(sum(mode_dt.get(m, failures.DOWNTIME_HOURS.mean()) for m in at_risk.SUSPECTED_MODE))
rk_all = risk.set_index("ASSET_ID")


# ----------------------------------------------------------------------------- shared pieces
def kpi(icon, label, value, sub, tip=""):
    return (f'<div class="pp-kpi" title="{tip}"><div class="pp-kpi-top"><span class="pp-ic">{icon}</span>{label}</div>'
            f'<div class="pp-kpi-val">{value}</div><div class="pp-kpi-sub">{sub}</div></div>')


def kpi_row():
    gap = (oee_7 - OEE_TARGET) * 100 if pd.notna(oee_7) else np.nan
    gap_txt = ("–" if pd.isna(gap) else
               f'<span class="{"pp-pos" if gap >= 0 else "pp-neg"}">{"▲" if gap >= 0 else "▼"} {abs(gap):.1f} pp</span> vs 80% target')
    n_high, n_med = int((f_risk.RISK_BAND == "HIGH").sum()), int((f_risk.RISK_BAND == "MEDIUM").sum())
    html('<div class="pp-kpis">' + "".join([
        kpi("%", "Fleet OEE · 7 days", fnum(oee_7 * 100, "{:.1f}%"), gap_txt,
            f"Mean daily OEE over the 7 production days to {last_day:%Y-%m-%d}. Previous 7 days: {fnum(oee_prev * 100, '{:.1f}%')}."),
        kpi("!", "Assets at risk", f"{n_high + n_med}", f'<span class="pp-neg">{n_high} high</span> · {n_med} medium of {len(f_risk)}',
            "Risk bands: HIGH >= 50, MEDIUM >= 30." + ("" if LIVE else " Demo mode uses the rule score only.")),
        kpi("◉", "Open alerts", f"{len(open_alerts)}",
            f'{int((f_alerts.STATUS == "DISMISSED").sum())} dismissed · {int((f_alerts.STATUS == "WO_CREATED").sum())} actioned',
            "Alerts with status NEW or ACKNOWLEDGED."),
        kpi("✓", "Open predictive WOs", f"{len(open_pdm)}",
            f'{int((open_pdm.PRIORITY == "P2").sum())} urgent (P2)' if len(open_pdm) else "raise one from Alerts"),
        kpi("◷", "Avoidable downtime", f"~{avoidable:.0f} h", f"if the {len(at_risk)} predicted failures are prevented",
            "For every HIGH/MEDIUM asset with a predicted failure mode, the average downtime of past breakdowns with the "
            "same failure mode (ERP failure history), summed."),
    ]) + "</div>")


def trend_chart(asset_id, col, base, alarm, unit, days=7, color=SERIES[0], height=240, base_label=True):
    h = repo.hourly(asset_id, days)
    fig = go.Figure()
    if len(h):
        fig.add_trace(go.Scatter(x=h.HOUR_TS, y=h[col], mode="lines", line=dict(color=color, width=2),
                                 hovertemplate="%{x|%d %b %H:%M}<br><b>%{y:.2f} " + unit + "</b><extra></extra>"))
    vals = h[col].dropna() if len(h) else pd.Series(dtype=float)
    lo = np.nanmin([vals.min() if len(vals) else np.nan, base if base else np.nan])
    hi = np.nanmax([vals.max() if len(vals) else np.nan, alarm if alarm else np.nan])
    if alarm is not None and not pd.isna(alarm):
        fig.add_hrect(y0=alarm, y1=hi + (hi - lo) * 0.2 + 0.2, fillcolor=STATUS["critical"], opacity=0.06, line_width=0)
        fig.add_hline(y=alarm, line=dict(color=STATUS["critical"], dash="dash", width=1.5),
                      annotation_text=f"alarm {alarm:.1f}", annotation_position="top left",
                      annotation_font=dict(color="#a32626", size=11))
    if base is not None and not pd.isna(base):
        if base_label:
            fig.add_hline(y=base, line=dict(color=INK2, dash="dot", width=1.5), annotation_text=f"baseline {base:.1f}",
                          annotation_position="bottom right", annotation_font=dict(color=INK2, size=11))
        else:
            fig.add_hline(y=base, line=dict(color=INK2, dash="dot", width=1.5))
    if not (pd.isna(lo) or pd.isna(hi)):
        pad = (hi - lo) * 0.12 + 0.1
        fig.update_yaxes(range=[lo - pad, hi + pad])
    st.plotly_chart(style_fig(fig, height, showlegend=False, hovermode="x"), **stretch())


def baseline_for(asset_id):
    b = repo.baselines()
    b = b[b.ASSET_ID == asset_id]
    if b.empty:
        return None, None, None
    return float(b.BASE_VIB.iloc[0]), float(b.BASE_TEMP.iloc[0]), float(b.BASE_CUR.iloc[0])


def threshold_bar(label, unit, val, base, alarm, lo=0.0, digits=2):
    if val is None or pd.isna(val) or alarm is None or pd.isna(alarm):
        return ""
    base = float(base) if base is not None and not pd.isna(base) else lo
    hi = float(alarm) * 1.12
    pct = lambda v: max(0.0, min(100.0, (float(v) - lo) / (hi - lo) * 100))
    frac = (float(val) - base) / max(float(alarm) - base, 1e-9)
    color = STATUS["critical"] if frac >= 0.6 else STATUS["warning"] if frac >= 0.25 else STATUS["good"]
    return (f'<div class="pp-bar"><div class="pp-bar-h"><span>{label}</span><span><b>{val:.{digits}f}</b> {unit}</span></div>'
            f'<div class="pp-track"><div class="pp-fill" style="width:{pct(val):.1f}%;background:{color}"></div>'
            f'<div class="pp-tick base" style="left:{pct(base):.1f}%"></div>'
            f'<div class="pp-tick alarm" style="left:{pct(alarm):.1f}%"></div></div>'
            f'<div class="pp-bar-l"><span>baseline {base:.{digits}f}</span><span>{max(frac, 0) * 100:.0f}% of the way to alarm</span>'
            f'<span style="color:#a32626">alarm {float(alarm):.1f}</span></div></div>')


def stats(items, cols=4):
    return f'<div class="pp-stats" style="grid-template-columns:repeat({cols},minmax(0,1fr))">' + "".join(
        f'<div class="pp-stat"><div class="pp-stat-l">{l}</div><div class="pp-stat-v">{v}</div></div>' for l, v in items) + "</div>"


def risk_gauge(score, height=225):
    g = go.Figure(go.Indicator(
        mode="gauge+number", value=float(score), number=dict(font=dict(size=40, color=INK, family=FONT)),
        title=dict(text="Failure risk (0–100)", font=dict(size=13, color=INK2)),
        gauge=dict(axis=dict(range=[0, 100], tickvals=[0, 30, 50, 100], tickfont=dict(color=MUTED, size=10)),
                   bar=dict(color=NAVY, thickness=0.28), bgcolor="#eef1f4", borderwidth=0,
                   steps=[dict(range=[0, 30], color="#dff1df"), dict(range=[30, 50], color="#fdebc2"),
                          dict(range=[50, 100], color="#f6cfcf")])))
    st.plotly_chart(style_fig(g, height, margin=dict(l=34, r=34, t=42, b=18)), **stretch())


def asset_card(asset_id, kicker=""):
    a = rk_all.loc[asset_id]
    html(f"""<div class="pp-card"><div class="pp-asset-h"><div>
<div class="pp-asset-k">{kicker}</div>
<div class="pp-asset-t">{a.ASSET_NAME}<span>{asset_id}</span></div>
<div class="pp-asset-s">{a.ASSET_TYPE} · line {a.LINE_ID} ({short_line(a.LINE_ID)}) · {a.PLANT_NAME} · criticality {a.CRITICALITY}
· telemetry {pd.to_datetime(a.AS_OF_TS):%d %b %H:%M}</div></div>
<div>{band_chip(a.RISK_BAND, a.SUSPECTED_MODE)} {chip(pretty(a.SUSPECTED_MODE) if a.SUSPECTED_MODE != 'NONE' else 'Healthy', 'brand')}</div>
</div></div>""")


def evidence_block(asset_id):
    r = rk_all.loc[asset_id]
    bv, bt, _ = baseline_for(asset_id)
    g1, g2 = st.columns([5, 7], gap="medium")
    with g1:
        risk_gauge(r.RISK_SCORE)
    with g2:
        html('<div style="height:8px"></div>'
             + threshold_bar("Vibration · 24 h avg", "mm/s", r.VIB_AVG_24H, bv, r.VIBRATION_ALARM_MM_S)
             + threshold_bar("Temperature · 24 h avg", "°C", r.TEMP_AVG_24H, bt, r.TEMP_ALARM_C, lo=20, digits=1))
    html(stats([("Hours to alarm", fmt_hours(r.HOURS_TO_ALARM)),
                ("ML P(fail ≤ 72 h)", fnum(r.ML_PROB, "{:.0%}", "n/a (demo)" if not LIVE else "–")),
                ("Suspected mode", pretty(r.SUSPECTED_MODE)),
                ("Vibration × baseline", f"{fnum(r.VIB_RATIO, '{:.2f}')}×  ·  {fnum(r.TEMP_DELTA, '{:+.1f}')} °C")]))
    return bv, bt


def floor_map(line_ids):
    lanes = []
    for lid in line_ids:
        g = risk[risk.LINE_ID == lid].copy()
        g["_o"] = g.ASSET_TYPE.map({t: i for i, t in enumerate(TYPE_ORDER)})
        tiles = []
        for a in g.sort_values("_o").itertuples():
            kind = ("info" if a.SUSPECTED_MODE == "SENSOR_FAULT" and a.RISK_BAND == "LOW"
                    else BAND_STYLE.get(a.RISK_BAND, ("good", ""))[0])
            mode = "Healthy" if a.SUSPECTED_MODE == "NONE" else pretty(a.SUSPECTED_MODE)
            eta = "" if a.SUSPECTED_MODE in ("NONE", "SENSOR_FAULT") else f" · {fmt_hours(a.HOURS_TO_ALARM)}"
            tiles.append(f"""<div class="pp-tile {kind if kind != 'good' else ''}" title="{a.ASSET_ID}: {mode}">
<div class="pp-tile-top"><span class="pp-tile-id">{a.ASSET_ID.split('-', 2)[-1]}</span>
<span class="pp-tile-risk" title="risk score 0-100">{a.RISK_SCORE:.0f}</span></div>
<div class="pp-tile-type">{a.ASSET_TYPE}</div>
<div class="pp-tile-foot">{band_chip(a.RISK_BAND, a.SUSPECTED_MODE)}</div>
<div class="pp-tile-mode">{mode}{eta}</div></div>""")
        n_risk = int(g.RISK_BAND.isin(["HIGH", "MEDIUM"]).sum())
        lanes.append(f"""<div class="pp-lane"><div class="pp-lane-h">{lid}</div>
<div class="pp-lane-s">{short_line(lid)} · {n_risk} at risk</div><div class="pp-tiles">{''.join(tiles)}</div></div>""")
    html(f'<div class="pp-floor pp-c{max(1, min(len(lanes), 4))}">{"".join(lanes)}</div>')


def line_cards():
    per_line = f_oee.groupby("LINE_ID")[["AVAILABILITY", "PERFORMANCE", "QUALITY", "OEE"]].mean()
    bd = f_oee.groupby("LINE_ID").BREAKDOWN_MIN.sum() / 60
    cards = []
    for lid, v in per_line.iterrows():
        g = (v.OEE - OEE_TARGET) * 100
        cards.append(f"""<div class="pp-kpi"><div class="pp-kpi-top">
<span class="pp-ic" style="background:{line_color(lid)}22;color:{line_color(lid)}">■</span>{lid} · {short_line(lid)}</div>
<div class="pp-line-v">{v.OEE * 100:.1f}%</div>
<div class="pp-kpi-sub"><span class="{'pp-pos' if g >= 0 else 'pp-neg'}">{'▲' if g >= 0 else '▼'} {abs(g):.1f} pp</span> vs target
· {bd.get(lid, 0):.0f} h breakdowns</div>
<div class="pp-apq"><span>A <b>{v.AVAILABILITY:.1%}</b></span><span>P <b>{v.PERFORMANCE:.1%}</b></span>
<span>Q <b>{v.QUALITY:.1%}</b></span></div></div>""")
    html(f'<div class="pp-lines pp-c{max(1, min(len(cards), 4))}">{"".join(cards)}</div>')
    return per_line


@st.cache_data(ttl=300, show_spinner=False)
def _backtest():
    try:
        return repo.backtest()
    except Exception:
        return pd.DataFrame()


def backtest_chart(height=330):
    bt = _backtest()
    if bt.empty:
        st.caption("Back-test unavailable.")
        return
    bt = bt.copy()
    bt["LEAD_TIME_HOURS"] = pd.to_numeric(bt.LEAD_TIME_HOURS, errors="coerce")
    bt["LABEL"] = bt.ASSET_ID.str.split("-", n=2).str[-1] + " · " + bt.FAILURE_MODE.map(pretty) + " · " + \
        pd.to_datetime(bt.FAILURE_TS).dt.strftime("%d %b")
    bt = bt.sort_values("FAILURE_TS")
    fig = go.Figure(go.Bar(y=bt.LABEL, x=bt.LEAD_TIME_HOURS, orientation="h",
                           marker=dict(color=SERIES[2], line=dict(color="#fff", width=2)),
                           text=[f"{v:.0f} h" for v in bt.LEAD_TIME_HOURS], textposition="outside",
                           textfont=dict(color=INK2, size=11),
                           customdata=np.stack([bt.ASSET_ID, bt.DOWNTIME_HOURS], axis=-1),
                           hovertemplate="%{customdata[0]}: warned <b>%{x:.0f} h</b> before failure"
                                         " (downtime %{customdata[1]} h)<extra></extra>"))
    fig.update_yaxes(autorange="reversed", showgrid=False, tickfont=dict(color=INK2, size=11))
    fig.update_xaxes(showgrid=True, gridcolor=GRID, range=[0, bt.LEAD_TIME_HOURS.max() * 1.18], title="hours of warning")
    st.plotly_chart(style_fig(fig, height, showlegend=False), **stretch())
    lead = bt.LEAD_TIME_HOURS
    st.caption(f"{lead.notna().sum()} of {len(bt)} historical failures flagged in advance (rule score ≥ 30); median "
               f"lead {lead.median():.0f} h; failure mode correct in {bt.MODE_CORRECT.astype(bool).mean():.0%} of cases.")


def render_wo_result(res):
    if not isinstance(res, dict):
        st.write(res)
        return
    if res.get("error"):
        st.error(res["error"])
        return
    if res.get("status") == "exists":
        st.info(f"Work order {res.get('wo_id')} already exists for this alert.")
        return
    prio = res.get("priority")
    html(f"""<div class="pp-ticket"><div><div class="pp-ticket-id">✓ {res.get('wo_id')} created
{chip(prio, PRIO_STYLE.get(prio, 'info'))} {chip('OPEN', 'brand')}</div>
<div class="pp-ticket-s">{res.get('priority_reason')} · schedule within {res.get('schedule_within_hours')} h</div></div></div>""")
    c1, c2 = st.columns([1, 1], gap="medium")
    with c1:
        section("Parts reserved", "stock checked in ERP")
        for p in res.get("parts") or ["No parts required"]:
            html(f'<div class="pp-src">{chip("SHORTAGE", "critical", "▲") if "SHORTAGE" in p else chip("IN STOCK", "good", "✓")}'
                 f'<span>{p.replace(" - SHORTAGE", "")}</span></div>')
        prs = res.get("purchase_requisitions") or []
        if prs:
            section("Purchase requisitions raised")
            for p in prs:
                html(f'<div class="pp-src">{chip(p.get("pr_id"), "brand")}<span>{p.get("part_no")} × {p.get("qty")}</span>'
                     f'{chip("EXPEDITE", "critical", "»") if p.get("expedite") else ""}</div>')
    with c2:
        section("Job plan", "drafted from diagnosis, history and manuals")
        with st.container(border=True):
            st.markdown(compact_md(res.get("job_plan") or ""))
    if res.get("diagnosis"):
        with st.expander("Diagnosis used for the job plan"):
            st.markdown(compact_md(res["diagnosis"]))


def source_rows(sources):
    out = []
    for s in sources or []:
        kind = "brand" if str(s.get("type")) == "MANUAL" else "warning"
        out.append(f'<div class="pp-src">{chip(s.get("doc_id"), kind)}<span>{s.get("title")}</span>'
                   f'<span style="margin-left:auto;color:#898781;font-size:.75rem">'
                   f'{"manual / SOP" if s.get("type") == "MANUAL" else "technician notes"}</span></div>')
    return "".join(out)


def asset_picker(key, options):
    cur = st.session_state.get("focus_asset")
    idx = options.index(cur) if cur in options else 0
    v = st.selectbox("Asset", options, index=idx, key=key, label_visibility="collapsed",
                     format_func=lambda a: f"{a} · {rk_all.ASSET_NAME[a]}"
                                           f" · risk {rk_all.RISK_SCORE[a]:.0f}")
    st.session_state["focus_asset"] = v
    return v


# ----------------------------------------------------------------------------- page: control room
def page_control_room():
    page_header("Control room", f"Live condition of {len(f_risk)} rotating assets across {scope}, joined with ERP, "
                                "CMMS and MES context in Snowflake")
    kpi_row()
    top = open_alerts[open_alerts.SEVERITY != "INFO"].sort_values("RISK_SCORE", ascending=False)
    a1, a2 = st.columns([8.6, 1.4], gap="small")
    with a1:
        if len(top):
            t = top.iloc[0]
            act = ("create a P2 predictive work order within 24 h" if t.SEVERITY == "CRITICAL"
                   else "investigate the root cause and plan the job")
            html(f"""<div class="pp-nba"><span class="pp-ic">!</span><div><div class="pp-nba-k">Next best action</div>
<div class="pp-nba-t">{t.ALERT_ID} · {t.ASSET_NAME} ({t.ASSET_ID}): {pretty(t.SUSPECTED_MODE).lower()} predicted,
alarm limit in {fmt_hours(t.HOURS_TO_ALARM)}</div><div class="pp-nba-s">Recommended: {act}.</div></div></div>""")
        else:
            html('<div class="pp-nba good"><span class="pp-ic">✓</span><div><div class="pp-nba-k">Next best action</div>'
                 '<div class="pp-nba-t">No open predictive alerts for this selection</div>'
                 '<div class="pp-nba-s">Every alert has been actioned or dismissed.</div></div></div>')
    with a2:
        if len(top):
            t = top.iloc[0]
            st.button("Open alert →", type="primary", on_click=goto, args=(PAGES[1], t.ASSET_ID, t.ALERT_ID),
                      key="nba_open", **stretch("button"))
            st.button("Ask copilot", on_click=goto, args=(PAGES[3], t.ASSET_ID), key="nba_ask", **stretch("button"))

    section("Live risk board", "every rotating asset by line · colour = risk band, always with icon and label")
    floor_map(sel_lines)
    st.markdown("")
    c1, c2 = st.columns([5, 7], gap="large")
    with c1:
        section("Top alerts", f"{len(open_alerts)} open · ranked by risk")
        q = open_alerts.sort_values("RISK_SCORE", ascending=False).head(4)
        if q.empty:
            st.caption("No open alerts.")
        for a in q.itertuples():
            with st.container(border=True):
                x1, x2 = st.columns([4, 1.3], vertical_alignment="center") if ST_VER >= (1, 36) else st.columns([4, 1.3])
                with x1:
                    html(f"{sev_chip(a.SEVERITY)} &nbsp;<b>{a.ALERT_ID}</b> · {a.ASSET_ID}<br>"
                         f'<span style="font-size:.84rem;color:#52514e">{pretty(a.SUSPECTED_MODE)} · risk <b>{a.RISK_SCORE:.0f}</b>'
                         f" · alarm {fmt_hours(a.HOURS_TO_ALARM)}</span>")
                with x2:
                    st.button("Open", key=f"cr_{a.ALERT_ID}", on_click=goto, args=(PAGES[1], a.ASSET_ID, a.ALERT_ID),
                              **stretch("button"))
    with c2:
        section("Proven on history", "hours of warning PlantPulse would have given before each past failure")
        backtest_chart(360)


# ----------------------------------------------------------------------------- page: alerts
def page_alerts():
    page_header("Alert triage", "Rank, investigate and act: acknowledge, dismiss a false alarm, or raise a predictive "
                                "work order with parts and a job plan")
    if f_alerts.empty:
        st.info("No alerts for the selected plant / line.")
        return
    q = f_alerts.sort_values(["RISK_SCORE", "ALERT_ID"], ascending=[False, True])
    lab = q.set_index("ALERT_ID")
    if st.session_state.get("alert_pick") not in lab.index:
        st.session_state.pop("alert_pick", None)
    left, right = st.columns([4, 8], gap="large")
    with left:
        section("Alert queue", f"{len(open_alerts)} open · ranked by risk")
        colour = {"CRITICAL": "red", "WARNING": "orange", "INFO": "gray"}

        def _label(i):
            a = lab.loc[i]
            sev = "gray" if a.STATUS in ("DISMISSED", "WO_CREATED") else colour.get(a.SEVERITY, "gray")
            status = {"NEW": "new", "ACKNOWLEDGED": "acknowledged", "DISMISSED": "dismissed",
                      "WO_CREATED": f"{a.WO_ID} raised"}.get(a.STATUS, a.STATUS)
            return (f":{sev}[**{SEV_STYLE.get(a.SEVERITY, ('', 'ℹ'))[1]} {a.SEVERITY}**] · **{i}** · {a.ASSET_ID}  \n"
                    f"{pretty(a.SUSPECTED_MODE)} · risk **{a.RISK_SCORE:.0f}** · alarm {fmt_hours(a.HOURS_TO_ALARM)}"
                    f" · _{status}_")

        alert_id = st.radio("Alert queue", q.ALERT_ID.tolist(), key="alert_pick", format_func=_label,
                            label_visibility="collapsed")
        html(f"""<div class="pp-card" style="margin-top:14px"><div class="pp-asset-k">How alerts are ranked</div>
<div class="pp-legend">{sev_chip("CRITICAL")} high risk on a criticality-A asset (stops the line)<br>
{sev_chip("WARNING")} high or medium risk on a B/C asset<br>
{sev_chip("INFO")} sensor fault: instrument check, machine healthy<br>
Risk = 60% Snowflake ML probability + 40% explainable rules.</div></div>""")
    al = lab.loc[alert_id]
    with right:
        wo_txt = f" · {al.WO_ID}" if isinstance(al.WO_ID, str) and al.WO_ID else ""
        asset_card(al.ASSET_ID, f"{alert_id} · {sev_chip(al.SEVERITY)} · {al.STATUS.replace('_', ' ').lower()}{wo_txt}")
        bv, bt = evidence_block(al.ASSET_ID)
        r = rk_all.loc[al.ASSET_ID]
        t1, t2 = st.columns(2, gap="medium")
        with t1:
            section("Vibration · last 7 days", "mm/s, hourly")
            trend_chart(al.ASSET_ID, "VIB_AVG", bv, r.VIBRATION_ALARM_MM_S, "mm/s", color=SERIES[0])
        with t2:
            section("Temperature · last 7 days", "°C, hourly")
            trend_chart(al.ASSET_ID, "TEMP_AVG", bt, r.TEMP_ALARM_C, "°C", color=SERIES[1])

        section("Decision", "human in the loop: every action is logged against the alert")
        with st.container(border=True):
            note = st.text_input("Triage note", key=f"note_{alert_id}", placeholder="Optional note for the record",
                                 label_visibility="collapsed")
            b = st.columns(4)
            status = al.STATUS
            user = repo.user()
            if b[0].button("✔ Acknowledge", key=f"ack_{alert_id}", disabled=status != "NEW", **stretch("button")):
                st.session_state["flash"] = ("success", repo.triage(alert_id, "ACK", note or "Acknowledged", user))
                rerun()
            if b[1].button("✖ Dismiss false alarm", key=f"dis_{alert_id}", disabled=status not in OPEN_ALERT,
                           **stretch("button")):
                st.session_state["flash"] = ("info", repo.triage(alert_id, "DISMISS", note or "False alarm", user))
                rerun()
            b[2].button("Ask copilot", key=f"ask_{alert_id}", on_click=goto, args=(PAGES[3], al.ASSET_ID),
                        **stretch("button"))
            if b[3].button("Create work order", key=f"wo_{alert_id}", type="primary",
                           disabled=status not in OPEN_ALERT, **stretch("button")):
                with st.spinner("Diagnosing, checking spares and drafting the job plan (about 30–40 s in live mode)..."):
                    try:
                        res = repo.create_wo(alert_id, user)
                    except Exception as e:
                        res = {"error": f"Work-order creation failed: {e}"}
                st.session_state.setdefault("wo_results", {})[alert_id] = res
                rerun()
        res = st.session_state.get("wo_results", {}).get(alert_id)
        if res:
            render_wo_result(res)
        elif status == "WO_CREATED":
            w = wos[wos.WO_ID == al.WO_ID]
            if len(w):
                w = w.iloc[0]
                html(f"""<div class="pp-ticket"><div><div class="pp-ticket-id">{w.WO_ID}
{chip(w.PRIORITY, PRIO_STYLE.get(w.PRIORITY, 'info'))} {chip(w.STATUS, 'brand')}</div>
<div class="pp-ticket-s">Scheduled by {pd.to_datetime(w.SCHEDULED_TS):%d %b %Y %H:%M} · parts {w.PARTS_USED or '–'}</div>
</div></div>""")
                with st.expander("Job plan", expanded=True):
                    st.markdown(compact_md(w.AI_JOB_PLAN or ""))
        elif status == "DISMISSED":
            st.caption(f"Dismissed by {al.TRIAGED_BY}: {al.TRIAGE_NOTE}")
        with st.expander("Raw alert evidence (JSON)"):
            st.json(as_json(al.EVIDENCE) or {})


# ----------------------------------------------------------------------------- page: asset 360
def page_asset_360():
    page_header("Asset 360", "One asset, every signal: condition vs baseline, four sensor trends, maintenance history")
    opts = f_risk.sort_values("RISK_SCORE", ascending=False).ASSET_ID.tolist()
    if not opts:
        st.info("No assets for the selected filter.")
        return
    p1, p2, p3 = st.columns([4, 3.2, 1.8], vertical_alignment="center") if ST_VER >= (1, 36) \
        else st.columns([4, 3.2, 1.8])
    with p1:
        asset = asset_picker("a360_pick", opts)
    with p2:
        win = st.radio("Window", ["3 days", "7 days", "14 days", "30 days", "60 days"], index=2, key="a360_win",
                       horizontal=True, label_visibility="collapsed")
        days = int(win.split()[0])
    with p3:
        st.button("Ask copilot about this asset", on_click=goto, args=(PAGES[3], asset), key="a360_ask", **stretch("button"))
    a = rk_all.loc[asset]
    asset_card(asset, f"{a.LINE_ID} · {a.PLANT_NAME}")
    bv, bt = evidence_block(asset)
    bc = baseline_for(asset)[2]
    t1, t2 = st.columns(2, gap="medium")
    with t1:
        section(f"Vibration · last {days} days", "mm/s, hourly")
        trend_chart(asset, "VIB_AVG", bv, a.VIBRATION_ALARM_MM_S, "mm/s", days, SERIES[0])
    with t2:
        section(f"Temperature · last {days} days", "°C, hourly")
        trend_chart(asset, "TEMP_AVG", bt, a.TEMP_ALARM_C, "°C", days, SERIES[1])
    t3, t4 = st.columns(2, gap="medium")
    with t3:
        section(f"Motor current · last {days} days", "A, hourly")
        trend_chart(asset, "CUR_AVG", bc, None, "A", days, SERIES[2], height=200, base_label=False)
    with t4:
        section(f"Shaft speed · last {days} days", "rpm, hourly")
        trend_chart(asset, "RPM_AVG", None, None, "rpm", days, SERIES[3], height=200)
    hist = wos[wos.ASSET_ID == asset][["WO_ID", "WO_TYPE", "PRIORITY", "STATUS", "CREATED_TS", "FAILURE_MODE",
                                       "DOWNTIME_HOURS", "TECHNICIAN_NOTES"]].copy()
    hist["FAILURE_MODE"] = hist.FAILURE_MODE.map(lambda m: pretty(m) if isinstance(m, str) and m else "–")
    section(f"Maintenance history · {asset}", f"{len(hist)} work orders (PM preventive · CM breakdown · PDM predictive)")
    st.dataframe(hist, hide_index=True, **stretch(), column_config={
        "WO_ID": "Work order", "WO_TYPE": "Type", "PRIORITY": "Priority", "STATUS": "Status",
        "CREATED_TS": st.column_config.DatetimeColumn("Created", format="DD MMM YYYY HH:mm"),
        "FAILURE_MODE": "Failure mode", "DOWNTIME_HOURS": st.column_config.NumberColumn("Downtime h", format="%.1f"),
        "TECHNICIAN_NOTES": st.column_config.TextColumn("Technician notes", width="large")})


# ----------------------------------------------------------------------------- page: copilot
def demo_ask_oee(question):
    """Offline stand-in for ANALYTICS.ASK_OEE: a few governed metrics computed from the bundled data."""
    q = question.lower()
    w = repo.work_orders()
    w = w.merge(risk[["ASSET_ID", "LINE_ID", "ASSET_TYPE", "PLANT_NAME"]], on="ASSET_ID", how="left")
    if "mttr" in q or "repair" in q:
        df = w[w.WO_TYPE == "CM"].groupby("LINE_ID").DOWNTIME_HOURS.mean().round(2).reset_index(name="MTTR_HOURS")
        ans = f"Mean time to repair is highest on {df.sort_values('MTTR_HOURS').iloc[-1].LINE_ID}."
    elif "compliance" in q or " pm" in f" {q}":
        pm = w[w.WO_TYPE == "PM"]
        df = (pm.assign(DONE=pm.STATUS == "CLOSED").groupby("LINE_ID").DONE.mean() * 100).round(1).reset_index(name="PM_COMPLIANCE_PCT")
        ans = f"PM compliance is lowest on {df.sort_values('PM_COMPLIANCE_PCT').iloc[0].LINE_ID}."
    elif "type" in q or "downtime" in q:
        df = w[w.WO_TYPE == "CM"].groupby("ASSET_TYPE").DOWNTIME_HOURS.sum().sort_values(ascending=False).reset_index(name="BREAKDOWN_HOURS")
        ans = f"{df.iloc[0].ASSET_TYPE} assets account for the most breakdown downtime ({df.iloc[0].BREAKDOWN_HOURS:.1f} h)."
    else:
        g = oee.groupby("LINE_ID")[["AVAILABILITY", "PERFORMANCE", "QUALITY", "OEE"]].mean()
        df = (g * 100).round(1).add_suffix("_PCT").reset_index().sort_values("OEE_PCT")
        ans = f"{df.iloc[0].LINE_ID} has the lowest OEE at {df.iloc[0].OEE_PCT:.1f}% against the 80% target."
    return {"question": question, "sql": "-- demo mode: computed locally from the bundled CSVs\n-- live mode: "
            "SELECT * FROM SEMANTIC_VIEW(PLANTPULSE.ANALYTICS.PLANT_OPS_SV ...)", "columns": df.columns.tolist(),
            "rows": df.values.tolist(), "answer": ans, "engine": "demo"}


def page_copilot():
    page_header("Copilot", "Ask in plain English: root cause from manuals and technician notes, or plant analytics "
                           "over the governed semantic view")
    left, right = st.columns([4, 8], gap="large")
    with left:
        mode = st.radio("Mode", ["Root cause", "Plant analytics"],
                        key="cop_mode", horizontal=True, label_visibility="collapsed")
        rca = mode == "Root cause"
        asked, asset = None, None
        if rca:
            opts = f_risk.sort_values("RISK_SCORE", ascending=False).ASSET_ID.tolist() or risk.ASSET_ID.tolist()
            asset = asset_picker("cop_asset", opts)
            ra = rk_all.loc[asset]
            html(f"""<div class="pp-card"><div class="pp-asset-k">Current condition</div>
<div style="margin:6px 0">{band_chip(ra.RISK_BAND, ra.SUSPECTED_MODE)} <b>{pretty(ra.SUSPECTED_MODE)}</b></div>
<div class="pp-asset-s">risk {ra.RISK_SCORE:.0f}/100 · alarm in {fmt_hours(ra.HOURS_TO_ALARM)} · vibration
{fnum(ra.VIB_RATIO, '{:.2f}')}× baseline · {fnum(ra.TEMP_DELTA, '{:+.1f}')} °C</div></div>""")
            suggestions = [
                f"What is the most likely root cause of the {pretty(ra.SUSPECTED_MODE).lower()} signature and how confident are we?",
                "How many hours can we keep running safely, and what happens if we wait until the next planned stop?",
                "Has this happened before on this or similar assets, and what fixed it?"]
        else:
            suggestions = ["Which line has the worst OEE?", "What is MTTR by line?",
                           "Which asset types have the most breakdown downtime?", "What is PM compliance by line?"]
        section("Suggested questions")
        for i, sq in enumerate(suggestions):
            if st.button(sq, key=f"sq_{'r' if rca else 'a'}_{i}", **stretch("button")):
                asked = sq
        with st.form("cop_form", clear_on_submit=True):
            free = st.text_input("Your question", placeholder="e.g. Which spare parts should I reserve?" if rca
                                 else "e.g. Show availability, performance and quality by plant")
            if st.form_submit_button("Ask", type="primary") and free.strip():
                asked = free.strip()
        st.caption(("Cortex Search over manuals, SOPs and technician notes + a Cortex LLM, grounded in this asset's "
                    "telemetry and CMMS history." if rca else
                    "Natural language → governed SEMANTIC_VIEW query over PLANT_OPS_SV, so every number uses the "
                    "plant's single definition of OEE, MTTR and downtime.") if LIVE else
                   "Demo mode: answers come from local stand-ins over the same synthetic data.")
    chat = st.session_state.setdefault("chat", [])
    if asked:
        with st.spinner("Thinking with your plant data" + (" (about 20 s)..." if LIVE else "...")):
            try:
                if rca:
                    res = repo.root_cause(asset, asked)
                elif LIVE:
                    res = call(f"CALL {DB}.ANALYTICS.ASK_OEE({lit(asked)})")
                else:
                    res = demo_ask_oee(asked)
            except Exception as e:
                res = {"error": str(e)}
        chat.append({"kind": "rca" if rca else "oee", "asset": asset, "q": asked,
                     "res": res if isinstance(res, dict) else {"answer": str(res)}})
    with right:
        if not chat:
            html("""<div class="pp-card pp-empty"><div class="pp-empty-ic">❄︎</div><div class="pp-sec-t">Ask PlantPulse</div>
<div class="pp-asset-s" style="margin-top:6px">Root-cause answers cite the manuals (DOC-…) and past work orders (WO-…)
they rely on.<br>Analytics answers show the governed query that produced every number.</div></div>""")
        for i, m in enumerate(reversed(chat[-6:])):
            res = m["res"]
            html(f'<div class="pp-q">{m["q"]}' + (f' <span style="opacity:.75">· {m["asset"]}</span>' if m["asset"] else "")
                 + "</div>")
            if res.get("error"):
                st.error(res["error"])
                continue
            with st.container(border=True):
                if m["kind"] == "rca":
                    html(f'<div class="pp-asset-k">Root-cause brief · {m["asset"]}</div>')
                    st.markdown(compact_md(res.get("answer", "")))
                    if res.get("sources"):
                        with st.expander(f"Cited sources ({len(res['sources'])})", expanded=i == 0):
                            html(source_rows(res["sources"]))
                else:
                    html(f'<div class="pp-asset-k">Plant analytics · {res.get("engine", "")}</div>')
                    st.markdown(f"**{res.get('answer', '')}**")
                    rows, cols = res.get("rows") or [], res.get("columns") or []
                    if rows:
                        st.dataframe(pd.DataFrame(rows, columns=cols or None), hide_index=True, **stretch())
                    if res.get("sql"):
                        with st.expander("Governed query", expanded=False):
                            html(f'<div class="pp-sql">{res["sql"]}</div>')
        if chat and st.button("Clear conversation", key="cop_clear"):
            st.session_state["chat"] = []
            rerun()


# ----------------------------------------------------------------------------- page: OEE
def page_oee():
    page_header("OEE & losses", f"Availability × Performance × Quality for {scope}, the loss tree, and which assets "
                                "cost the most availability")
    if f_oee.empty:
        st.info("No production data for the selected filter.")
        return
    per_line = line_cards()
    o1, o2 = st.columns([3, 2], gap="large")
    with o1:
        section("Daily OEE per line", "dashed line = 80% target · dips are breakdown days")
        fig = go.Figure()
        for lid, g in f_oee.groupby("LINE_ID"):
            fig.add_trace(go.Scatter(x=g.PROD_DATE, y=g.OEE, mode="lines", name=lid, line=dict(width=1.75, color=line_color(lid)),
                                     hovertemplate=lid + " · %{x|%d %b}: <b>%{y:.1%}</b><extra></extra>"))
        fig.add_hline(y=OEE_TARGET, line=dict(color=INK2, dash="dash", width=1.5))
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(style_fig(fig, 340, hovermode="x unified"), **stretch())
    with o2:
        section("Availability · Performance · Quality", "period average per line")
        apq = per_line.reset_index()
        fig = go.Figure()
        for i, (c, name) in enumerate([("AVAILABILITY", "Availability"), ("PERFORMANCE", "Performance"),
                                       ("QUALITY", "Quality"), ("OEE", "OEE")]):
            fig.add_trace(go.Bar(x=apq.LINE_ID, y=apq[c], name=name, marker=dict(color=SERIES[i], line=dict(color="#fff", width=2)),
                                 hovertemplate="%{x} · " + name + ": <b>%{y:.1%}</b><extra></extra>"))
        fig.update_yaxes(tickformat=".0%", range=[0, 1.05])
        st.plotly_chart(style_fig(fig, 340, barmode="group", bargap=0.25, bargroupgap=0.05), **stretch())
    o3, o4 = st.columns([1, 1], gap="large")
    with o3:
        section("Where planned time goes", "hours over the whole period")
        lt = repo.loss_tree()
        lt = lt[lt.LINE_ID.isin(sel_lines)].sum(numeric_only=True) / 60
        labels = ["Planned", "Breakdowns", "Minor stops", "Changeovers", "Speed loss", "Quality loss", "Fully productive"]
        vals = [lt.PLANNED_MIN, -lt.BREAKDOWN_LOSS_MIN, -lt.MINOR_STOP_LOSS_MIN, -lt.CHANGEOVER_LOSS_MIN,
                -lt.SPEED_LOSS_MIN, -lt.QUALITY_LOSS_MIN, 0]
        fig = go.Figure(go.Waterfall(
            x=labels, y=vals, measure=["absolute"] + ["relative"] * 5 + ["total"],
            text=[f"{abs(v):,.0f} h" for v in vals[:-1]] + [f"{lt.FULLY_PRODUCTIVE_MIN:,.0f} h"],
            textposition="outside", textfont=dict(color=INK2, size=11), connector=dict(line=dict(color=AXIS, width=1)),
            decreasing=dict(marker=dict(color=SERIES[1])), increasing=dict(marker=dict(color=SERIES[0])),
            totals=dict(marker=dict(color=SERIES[2])), hovertemplate="%{x}: <b>%{text}</b><extra></extra>"))
        fig.update_yaxes(range=[0, lt.PLANNED_MIN * 1.14])
        st.plotly_chart(style_fig(fig, 340, showlegend=False), **stretch())
    with o4:
        section("Breakdown hours by asset", "darker = more critical asset")
        imp = repo.downtime_impact()
        imp = imp[imp.LINE_ID.isin(sel_lines) & (imp.BREAKDOWN_HOURS > 0)].sort_values("BREAKDOWN_HOURS")
        fig = go.Figure()
        for crit, lbl in [("A", "A · stops the line"), ("B", "B · degrades output"), ("C", "C · minor")]:
            g = imp[imp.CRITICALITY == crit]
            if len(g):
                fig.add_trace(go.Bar(y=g.ASSET_ID, x=g.BREAKDOWN_HOURS, orientation="h", name=lbl,
                                     marker=dict(color=CRIT_RAMP[crit], line=dict(color="#fff", width=2)),
                                     text=[f"{h:.0f} h" for h in g.BREAKDOWN_HOURS], textposition="outside",
                                     textfont=dict(color=INK2, size=11),
                                     customdata=np.stack([g.BREAKDOWNS, g.BREAKDOWN_COST_INR], axis=-1),
                                     hovertemplate="%{y}: <b>%{x:.1f} h</b> · %{customdata[0]:.0f} breakdown(s)"
                                                   " · ₹%{customdata[1]:,.0f}<extra></extra>"))
        fig.update_yaxes(categoryorder="array", categoryarray=imp.ASSET_ID.tolist(), showgrid=False)
        fig.update_xaxes(range=[0, (imp.BREAKDOWN_HOURS.max() if len(imp) else 1) * 1.25], showgrid=True, gridcolor=GRID)
        st.plotly_chart(style_fig(fig, 340), **stretch())
    st.caption("Breakdown losses are the part of OEE that predictive maintenance attacks directly; ask the Copilot "
               "(plant analytics) for any other cut of these governed metrics.")


# ----------------------------------------------------------------------------- page: work orders
def page_work_orders():
    page_header("Work orders", "Predictive jobs raised by PlantPulse, purchase requisitions for stock-outs, and the "
                               "breakdown history the copilot learns from")
    section("Open predictive work orders", f"{len(open_pdm)} open")
    if open_pdm.empty:
        html("""<div class="pp-card pp-empty"><div class="pp-empty-ic">❄︎</div><div class="pp-sec-t">No predictive work orders yet</div>
<div class="pp-asset-s" style="margin-top:6px">Create one from an alert in <b>Alerts</b>: priority, parts,
purchase requisitions and a job plan are generated in one step.</div></div>""")
    for w in open_pdm.itertuples():
        html(f"""<div class="pp-ticket"><div><div class="pp-ticket-id">{w.WO_ID}
{chip(w.PRIORITY, PRIO_STYLE.get(w.PRIORITY, 'info'))} {chip(w.STATUS, 'brand')} {chip(pretty(w.FAILURE_MODE), 'info')}</div>
<div class="pp-ticket-s">{w.ASSET_ID} · {w.ASSET_NAME} · alert {w.ALERT_ID} · risk {fnum(w.RISK_SCORE, '{:.0f}')} ·
parts {w.PARTS_USED or '–'} · requested by {w.CREATED_BY}</div></div>
<div style="text-align:right"><div class="pp-asset-k">Due</div>
<div class="pp-sec-t">{pd.to_datetime(w.SCHEDULED_TS):%d %b %H:%M}</div></div></div>""")
        with st.expander(f"Job plan · {w.WO_ID}"):
            st.markdown(compact_md(w.AI_JOB_PLAN or ""))
    st.markdown("")
    section("Purchase requisitions", "raised automatically when a reserved part is out of stock")
    prs = repo.purchase_reqs()
    if prs.empty:
        st.caption("No purchase requisitions raised.")
    else:
        st.dataframe(prs, hide_index=True, **stretch(), column_config={
            "EXPEDITE": st.column_config.CheckboxColumn("Expedite"),
            "LEAD_TIME_DAYS": st.column_config.NumberColumn("Lead time (d)", format="%d")})
    section("Recent corrective (breakdown) work orders", "the history the copilot learns from")
    cm = f_wos[f_wos.WO_TYPE == "CM"].sort_values("CREATED_TS", ascending=False).head(15).copy()
    cm["FAILURE_MODE"] = cm.FAILURE_MODE.map(pretty)
    cm["COST_INR"] = cm.COST_INR.map(lambda v: f"₹ {float(v):,.0f}" if pd.notna(v) else "–")
    st.dataframe(cm[["WO_ID", "ASSET_ID", "PRIORITY", "CREATED_TS", "FAILURE_MODE", "DOWNTIME_HOURS", "COST_INR",
                     "TECHNICIAN_NOTES"]], hide_index=True, **stretch(), column_config={
        "WO_ID": "Work order", "ASSET_ID": "Asset", "PRIORITY": "Priority",
        "CREATED_TS": st.column_config.DatetimeColumn("Created", format="DD MMM YYYY HH:mm"),
        "FAILURE_MODE": "Failure mode", "DOWNTIME_HOURS": st.column_config.NumberColumn("Downtime h", format="%.1f"),
        "COST_INR": "Cost",
        "TECHNICIAN_NOTES": st.column_config.TextColumn("Technician notes", width="large")})


# ----------------------------------------------------------------------------- page: how it works
STEPS = [
    ("Converge", "OT telemetry (vibration, temperature, RPM, current) lands beside ERP/CMMS work orders, spare parts, "
                 "MES production counts and maintenance manuals.", "COPY INTO · Snowpipe Streaming · Tasks"),
    ("Engineer", "Hourly roll-ups, rolling 24 h / 72 h features, trend slopes and learned healthy baselines, joined to "
                 "the last PM with an ASOF JOIN.", "V_ASSET_FEATURES · ASOF JOIN"),
    ("Predict", "Snowflake ML classifier (failure within 72 h) blended with an explainable rule engine that names the "
                "failure mode and projects hours to alarm.", "SNOWFLAKE.ML.CLASSIFICATION"),
    ("Explain", "Cortex Search retrieves manuals, SOPs and technician notes; a Cortex LLM writes a cited root-cause "
                "brief grounded in live evidence.", "Cortex Search · Cortex AI"),
    ("Act", "One call turns an alert into a prioritised predictive WO with parts reserved, stock checked, an "
            "expedited PR on stock-out and a job plan.", "CREATE_PDM_WORK_ORDER"),
    ("Govern", "A semantic view holds the single definition of OEE, MTTR, downtime and PM compliance for every tool, "
               "agent and dashboard.", "SEMANTIC VIEW PLANT_OPS_SV"),
]
SKILLS = [
    ("$plantpulse-setup", "Build, verify or reset the environment"),
    ("$asset-health-triage", "Rescore, raise alerts, rank, flag false alarms → alert id"),
    ("$root-cause-investigator", "Trend vs baseline + history + search + cited brief → diagnosis"),
    ("$work-order-automator", "Policy, parts, PRs, job plan, verified in ERP → WO id"),
    ("$oee-analyst", "Governed OEE, loss tree, asset impact, improvement case → OEE gain"),
]


def page_how():
    page_header("How it works", "One governed platform: IT + OT + unstructured knowledge in Snowflake, operated "
                                "through CoCo CLI skills and this command center")
    html('<div class="pp-steps">' + "".join(
        f'<div class="pp-step"><div class="pp-step-n">{i}</div><div class="pp-step-t">{t}</div>'
        f'<div class="pp-step-s">{s}</div><div class="pp-step-x">{x}</div></div>'
        for i, (t, s, x) in enumerate(STEPS, 1)) + "</div>")
    st.markdown("")
    arch = os.path.join(APP_DIR, "architecture.png")
    c1, c2 = st.columns([8, 4], gap="large")
    with c1:
        section("Architecture")
        if os.path.exists(arch):
            st.image(arch, **stretch("button"))
        else:
            st.caption("Architecture diagram: see docs/architecture.png in the repository.")
    with c2:
        section("CoCo CLI skills", "modular; each hands an ID to the next")
        html('<div class="pp-card">' + "".join(
            f'<div class="pp-src"><span class="pp-chip brand" style="font-family:Consolas,monospace">{n}</span>'
            f'<span style="font-size:.82rem">{d}</span></div>' for n, d in SKILLS) + "</div>")
        section("Proven on history")
        bt = _backtest()
        if len(bt):
            lead = pd.to_numeric(bt.LEAD_TIME_HOURS, errors="coerce")
            html(stats([("Caught", f"{lead.notna().sum()}/{len(bt)}"), ("Median lead", f"{lead.median():.0f} h"),
                        ("Mode correct", f"{bt.MODE_CORRECT.astype(bool).mean():.0%}"), ("False alarms", "0")], cols=2))
        if LIVE:
            try:
                mv = query(f"SELECT METRIC, VALUE FROM {DB}.ANALYTICS.ML_VALIDATION").set_index("METRIC").VALUE
                if len(mv):
                    section("Out-of-time ML test", "train before 15 Sep · test after")
                    g = lambda k, f="{:.0f}": f.format(float(mv[k])) if k in mv.index else "–"
                    html(stats([("Held-out failures", f"{g('events_detected')}/{g('events_total')}"),
                                ("Median lead", f"{g('event_lead_time_median_h', '{:.1f}')} h"),
                                ("Hourly F1", g("hourly_f1_at_0.5", "{:.2f}")),
                                ("ROC AUC", g("hourly_roc_auc", "{:.3f}"))], cols=2))
            except Exception:
                pass
        st.caption("All data is synthetic. Snowflake features: ML Classification · Cortex Search · Cortex AI · "
                   "Cortex Analyst · Cortex Agents · semantic views · ASOF JOIN · Snowpark Python · Tasks · "
                   "Streamlit in Snowflake.")


# ----------------------------------------------------------------------------- router
{PAGES[0]: page_control_room, PAGES[1]: page_alerts, PAGES[2]: page_asset_360, PAGES[3]: page_copilot,
 PAGES[4]: page_oee, PAGES[5]: page_work_orders, PAGES[6]: page_how}[page]()
