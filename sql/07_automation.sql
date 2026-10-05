-- =====================================================================
-- PlantPulse | 07 - Alert triage, AI root-cause copilot, work-order automation
-- =====================================================================
USE WAREHOUSE PLANTPULSE_WH;
USE DATABASE PLANTPULSE;
USE SCHEMA ANALYTICS;

-- ---------- configuration ----------
CREATE OR REPLACE TABLE ANALYTICS.APP_CONFIG (KEY VARCHAR PRIMARY KEY, VALUE VARCHAR);
INSERT INTO ANALYTICS.APP_CONFIG VALUES
  ('LLM_MODEL', 'claude-sonnet-4-5'),           -- any Cortex COMPLETE model available in your region
  ('LLM_FALLBACK_MODEL', 'llama3.3-70b'),       -- used if the primary model errors
  ('SEARCH_SERVICE', 'PLANTPULSE.ANALYTICS.MAINTENANCE_KB_SEARCH');

-- Recommended spares per failure mode (asset-type specific rows override 'ALL')
CREATE OR REPLACE TABLE ANALYTICS.FAILURE_MODE_PARTS (FAILURE_MODE VARCHAR, ASSET_TYPE VARCHAR, PART_NO VARCHAR, QTY NUMBER);
INSERT INTO ANALYTICS.FAILURE_MODE_PARTS VALUES
  ('BEARING_WEAR','ALL','BRG-6312-C3',2), ('BEARING_WEAR','ALL','GRS-LGHP2',1),
  ('BEARING_WEAR','CNC Spindle','BRG-SPN-7014',1),
  ('CAVITATION','ALL','IMP-CP-150',1), ('CAVITATION','ALL','SEAL-MS-35',1), ('CAVITATION','ALL','STR-SUC-80',1),
  ('LUBRICATION_FAILURE','ALL','OIL-ISO-VG220',2), ('LUBRICATION_FAILURE','ALL','FLT-OIL-90',1), ('LUBRICATION_FAILURE','ALL','SNS-PT100',1),
  ('IMBALANCE','ALL','BAL-WT-KIT',1),
  ('MISALIGNMENT','ALL','CPL-INS-L100',1), ('MISALIGNMENT','ALL','SHM-KIT-SS',1),
  ('ELECTRICAL_WINDING','ALL','MTR-STBY-45KW',1);

CREATE SEQUENCE IF NOT EXISTS ANALYTICS.WO_SEQ START = 90001;
CREATE SEQUENCE IF NOT EXISTS ANALYTICS.PR_SEQ START = 5001;

CREATE TABLE IF NOT EXISTS ANALYTICS.PDM_ALERTS (
  ALERT_ID VARCHAR PRIMARY KEY, ASSET_ID VARCHAR, CREATED_TS TIMESTAMP_NTZ, AS_OF_TS TIMESTAMP_NTZ,
  SEVERITY VARCHAR, RISK_SCORE FLOAT, ML_PROB FLOAT, RULE_SCORE FLOAT, SUSPECTED_MODE VARCHAR, HOURS_TO_ALARM FLOAT,
  EVIDENCE VARIANT, STATUS VARCHAR COMMENT 'NEW | ACKNOWLEDGED | DISMISSED | WO_CREATED',
  TRIAGE_NOTE VARCHAR, TRIAGED_BY VARCHAR, TRIAGED_TS TIMESTAMP_NTZ, WO_ID VARCHAR
);

CREATE TABLE IF NOT EXISTS ERP.PURCHASE_REQUISITIONS (
  PR_ID VARCHAR PRIMARY KEY, PART_NO VARCHAR, QTY NUMBER, REASON VARCHAR, WO_ID VARCHAR,
  CREATED_TS TIMESTAMP_NTZ, STATUS VARCHAR, EXPEDITE BOOLEAN
);

-- ---------- 1) Alert generation ----------
CREATE OR REPLACE PROCEDURE ANALYTICS.GENERATE_ALERTS()
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
DECLARE n INTEGER;
BEGIN
  INSERT INTO PLANTPULSE.ANALYTICS.PDM_ALERTS
    (ALERT_ID, ASSET_ID, CREATED_TS, AS_OF_TS, SEVERITY, RISK_SCORE, ML_PROB, RULE_SCORE, SUSPECTED_MODE,
     HOURS_TO_ALARM, EVIDENCE, STATUS)
  -- alert numbers continue from the highest existing id, highest risk first
  SELECT 'AL-' || (COALESCE((SELECT MAX(TRY_TO_NUMBER(SUBSTR(ALERT_ID, 4))) FROM PLANTPULSE.ANALYTICS.PDM_ALERTS), 1000)
                   + ROW_NUMBER() OVER (ORDER BY r.RISK_SCORE DESC, r.ASSET_ID)),
         r.ASSET_ID, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ, r.AS_OF_TS,
         -- risk x consequence matrix: likelihood (risk band) x asset criticality (A = stops the line)
         CASE WHEN r.SUSPECTED_MODE = 'SENSOR_FAULT' AND r.RISK_BAND = 'LOW' THEN 'INFO'
              WHEN r.RISK_BAND = 'HIGH' AND r.CRITICALITY = 'A' THEN 'CRITICAL' ELSE 'WARNING' END,
         r.RISK_SCORE, r.ML_PROB, r.RULE_SCORE, r.SUSPECTED_MODE, r.HOURS_TO_ALARM,
         OBJECT_CONSTRUCT('vib_avg_24h', r.VIB_AVG_24H, 'baseline_vib', r.BASE_VIB, 'vib_ratio', r.VIB_RATIO,
                          'vib_alarm', r.VIBRATION_ALARM_MM_S, 'temp_avg_24h', r.TEMP_AVG_24H, 'temp_delta', r.TEMP_DELTA,
                          'temp_alarm', r.TEMP_ALARM_C, 'current_ratio', r.CUR_RATIO,
                          'vib_slope_per_day', r.VIB_SLOPE_PER_DAY, 'temp_slope_per_day', r.TEMP_SLOPE_PER_DAY,
                          'saturated_readings_24h', r.SATURATED_24H, 'days_since_pm', r.DAYS_SINCE_PM),
         'NEW'
  FROM PLANTPULSE.ANALYTICS.ASSET_RISK_SCORES r
  WHERE (r.RISK_BAND IN ('HIGH', 'MEDIUM') OR r.SUSPECTED_MODE = 'SENSOR_FAULT')
    AND NOT EXISTS (SELECT 1 FROM PLANTPULSE.ANALYTICS.PDM_ALERTS a
                    WHERE a.ASSET_ID = r.ASSET_ID AND a.STATUS IN ('NEW', 'ACKNOWLEDGED', 'WO_CREATED'));
  n := SQLROWCOUNT;
  RETURN n || ' new alert(s) raised';
END;
$$;

-- ---------- 2) Human-in-the-loop triage ----------
CREATE OR REPLACE PROCEDURE ANALYTICS.TRIAGE_ALERT(ALERT_ID VARCHAR, DECISION VARCHAR, NOTE VARCHAR, TRIAGED_BY VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
BEGIN
  UPDATE PLANTPULSE.ANALYTICS.PDM_ALERTS
     SET STATUS = CASE UPPER(:DECISION) WHEN 'DISMISS' THEN 'DISMISSED' WHEN 'ACK' THEN 'ACKNOWLEDGED' ELSE UPPER(:DECISION) END,
         TRIAGE_NOTE = :NOTE, TRIAGED_BY = :TRIAGED_BY, TRIAGED_TS = CURRENT_TIMESTAMP()::TIMESTAMP_NTZ
   WHERE ALERT_ID = :ALERT_ID;
  RETURN 'Alert ' || ALERT_ID || ' -> ' || UPPER(DECISION);
END;
$$;

-- ---------- 3) Root-cause copilot (RAG: telemetry evidence + CMMS history + manuals) ----------
CREATE OR REPLACE PROCEDURE ANALYTICS.ROOT_CAUSE_BRIEF(ASSET_ID VARCHAR, QUESTION VARCHAR)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
AS
$$
import json

def _lit(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"

def _cfg(session, key, default):
    r = session.sql(f"SELECT VALUE FROM PLANTPULSE.ANALYTICS.APP_CONFIG WHERE KEY = {_lit(key)}").collect()
    return r[0][0] if r else default

def _complete(session, prompt):
    """Cortex COMPLETE with the configured model, then the fallback model."""
    last = None
    for key, default in (("LLM_MODEL", "claude-sonnet-4-5"), ("LLM_FALLBACK_MODEL", "llama3.3-70b")):
        try:
            return session.sql("SELECT SNOWFLAKE.CORTEX.COMPLETE(?, ?)", params=[_cfg(session, key, default), prompt]).collect()[0][0]
        except Exception as e:
            last = e
    raise last

def _search(session, service, query, asset_type, mode, source_type, limit):
    flt = {"@and": [{"@eq": {"SOURCE_TYPE": source_type}},
                    {"@or": [{"@eq": {"ASSET_TYPE": asset_type}}, {"@eq": {"ASSET_TYPE": "ALL"}}]}]}
    if mode and mode not in ("NONE", "SENSOR_FAULT"):
        query = f"{query} {mode.replace('_', ' ').lower()}"
    payload = json.dumps({"query": query, "columns": ["DOC_ID", "DOC_TITLE", "SOURCE_TYPE", "CONTENT"],
                          "filter": flt, "limit": limit})
    try:
        raw = session.sql(f"SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW({_lit(service)}, {_lit(payload)})").collect()[0][0]
        return json.loads(raw).get("results", [])
    except Exception as e:
        return [{"DOC_ID": "N/A", "DOC_TITLE": "search unavailable", "SOURCE_TYPE": "ERROR", "CONTENT": str(e)[:300]}]

def run(session, asset_id, question):
    risk = session.sql(f"SELECT OBJECT_CONSTRUCT(*) FROM PLANTPULSE.ANALYTICS.ASSET_RISK_SCORES WHERE ASSET_ID = {_lit(asset_id)}").collect()
    if not risk:
        return {"error": f"Unknown asset {asset_id}"}
    r = json.loads(risk[0][0])
    hist = session.sql(f"""SELECT WO_ID, WO_TYPE, TO_VARCHAR(CREATED_TS,'YYYY-MM-DD') D, STATUS, FAILURE_MODE, TECHNICIAN_NOTES, DOWNTIME_HOURS
                           FROM PLANTPULSE.ERP.WORK_ORDERS WHERE ASSET_ID = {_lit(asset_id)}
                           ORDER BY CREATED_TS DESC LIMIT 6""").collect()
    hist_txt = "\n".join(f"- {h['WO_ID']} {h['D']} {h['WO_TYPE']} {h['STATUS']} {h['FAILURE_MODE'] or ''}: {h['TECHNICIAN_NOTES']}" for h in hist)
    service = _cfg(session, "SEARCH_SERVICE", "PLANTPULSE.ANALYTICS.MAINTENANCE_KB_SEARCH")
    q = question or "root cause of abnormal vibration and temperature"
    # manuals/SOPs and past technician notes on same-type assets are retrieved separately so both kinds of evidence appear
    hits = (_search(session, service, q, r["ASSET_TYPE"], r.get("SUSPECTED_MODE"), "MANUAL", 4)
            + _search(session, service, q, r["ASSET_TYPE"], r.get("SUSPECTED_MODE"), "WORK_ORDER", 3))
    kb_txt = "\n".join(f"[{h.get('DOC_ID')}] {h.get('DOC_TITLE')}: {h.get('CONTENT')}" for h in hits)
    evidence = {k: r.get(k) for k in ["ASSET_ID", "ASSET_TYPE", "LINE_ID", "CRITICALITY", "AS_OF_TS", "RISK_SCORE", "RISK_BAND", "ML_PROB",
                                      "SUSPECTED_MODE", "HOURS_TO_ALARM", "VIB_AVG_24H", "BASE_VIB", "VIB_RATIO", "VIBRATION_ALARM_MM_S",
                                      "TEMP_AVG_24H", "TEMP_DELTA", "TEMP_ALARM_C", "CUR_RATIO", "VIB_SLOPE_PER_DAY",
                                      "TEMP_SLOPE_PER_DAY", "SATURATED_24H", "DAYS_SINCE_PM", "PM_OVERDUE_DAYS"]}
    prompt = f"""You are a senior reliability engineer. Answer the maintenance planner's question using ONLY the evidence below.
Cite sources in square brackets like [DOC-003] or [WO-70061]. Be concise and practical (max 180 words).
Structure: 1) Diagnosis, 2) Evidence, 3) Recommended action and timing, 4) Risk if ignored.
In the Diagnosis, first name the failure mode (use SUSPECTED_MODE unless the evidence clearly contradicts it), then the most
likely underlying cause, then confidence (High/Medium/Low) with a one-line reason.
Format: markdown with bold section labels such as **1) Diagnosis** and short bullets. Do not use # headings.
The current time is AS_OF_TS (latest telemetry); compute any "days ago" from it and the work-order dates. Do not invent numbers.
Field meanings: RISK_SCORE = blended 0-100 risk (60% ML, 40% rules); ML_PROB = model probability of a functional failure
within 72 h; VIB_RATIO = 24 h vibration / learned healthy baseline; TEMP_DELTA = deg C above baseline;
HOURS_TO_ALARM = linear projection of the 72 h trend to the alarm limit; SATURATED_24H = transmitter readings stuck at 24.9 mm/s.

QUESTION: {question}

LIVE CONDITION EVIDENCE (24h rolling, vs learned baseline):
{json.dumps(evidence, default=str)}

MAINTENANCE HISTORY FOR THIS ASSET (CMMS):
{hist_txt}

KNOWLEDGE BASE EXCERPTS (Cortex Search: manuals, SOPs and technician notes from same-type assets):
{kb_txt}
"""
    try:
        answer = _complete(session, prompt)
    except Exception as e:
        answer = (f"(LLM unavailable: {str(e)[:120]}) Rule-based diagnosis: {r.get('SUSPECTED_MODE')} on {asset_id}; "
                  f"vibration {r.get('VIB_RATIO')}x baseline, temperature +{r.get('TEMP_DELTA')} C, "
                  f"projected alarm in {r.get('HOURS_TO_ALARM')} h. See {', '.join(h.get('DOC_ID','') for h in hits[:3])}.")
    return {"asset_id": asset_id, "answer": answer, "evidence": evidence,
            "sources": [{"doc_id": h.get("DOC_ID"), "title": h.get("DOC_TITLE"), "type": h.get("SOURCE_TYPE")} for h in hits]}
$$;

-- ---------- 4) Predictive work order automation ----------
CREATE OR REPLACE PROCEDURE ANALYTICS.CREATE_PDM_WORK_ORDER(ALERT_ID VARCHAR, REQUESTED_BY VARCHAR)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
AS
$$
import json

def _lit(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"

def _cfg(session, key, default):
    r = session.sql(f"SELECT VALUE FROM PLANTPULSE.ANALYTICS.APP_CONFIG WHERE KEY = {_lit(key)}").collect()
    return r[0][0] if r else default

def _complete(session, prompt):
    """Cortex COMPLETE with the configured model, then the fallback model."""
    last = None
    for key, default in (("LLM_MODEL", "claude-sonnet-4-5"), ("LLM_FALLBACK_MODEL", "llama3.3-70b")):
        try:
            return session.sql("SELECT SNOWFLAKE.CORTEX.COMPLETE(?, ?)", params=[_cfg(session, key, default), prompt]).collect()[0][0]
        except Exception as e:
            last = e
    raise last

def run(session, alert_id, requested_by):
    a = session.sql(f"""SELECT al.*, r.ASSET_TYPE, r.CRITICALITY, r.LINE_ID, r.ASSET_NAME
                        FROM PLANTPULSE.ANALYTICS.PDM_ALERTS al
                        JOIN PLANTPULSE.ANALYTICS.ASSET_RISK_SCORES r ON r.ASSET_ID = al.ASSET_ID
                        WHERE al.ALERT_ID = {_lit(alert_id)}""").collect()
    if not a:
        return {"error": f"Alert {alert_id} not found"}
    a = a[0].as_dict()
    if a["STATUS"] == "WO_CREATED":
        return {"status": "exists", "wo_id": a["WO_ID"]}
    mode = a["SUSPECTED_MODE"]
    hours = float(a["HOURS_TO_ALARM"] or 9999)

    # Priority policy (KNOWLEDGE DOC-016): P2 = failure predicted within 72 h on a criticality A/B asset
    ml_prob = float(a["ML_PROB"]) if a["ML_PROB"] is not None else None
    within_72h = (ml_prob is not None and ml_prob >= 0.5) or hours < 72
    if mode == "SENSOR_FAULT":
        priority, window_h, why = "P4", 168, "instrumentation check only (DOC-019)"
    elif within_72h and a["CRITICALITY"] in ("A", "B"):
        priority, window_h, why = "P2", 24, f"failure predicted within 72 h on criticality {a['CRITICALITY']} asset (DOC-016)"
    else:
        priority, window_h, why = "P3", 168, "degradation detected, failure not expected within 72 h (DOC-016)"

    # Parts + stock check (ERP MM)
    parts = session.sql(f"""
        SELECT m.PART_NO, m.QTY, s.DESCRIPTION, s.ON_HAND_QTY, s.LEAD_TIME_DAYS, s.UNIT_COST_INR
        FROM PLANTPULSE.ANALYTICS.FAILURE_MODE_PARTS m
        JOIN PLANTPULSE.ERP.SPARE_PARTS s ON s.PART_NO = m.PART_NO
        WHERE m.FAILURE_MODE = {_lit(mode)}
          AND (m.ASSET_TYPE = {_lit(a['ASSET_TYPE'])}
               OR (m.ASSET_TYPE = 'ALL' AND NOT EXISTS (
                     SELECT 1 FROM PLANTPULSE.ANALYTICS.FAILURE_MODE_PARTS x
                     WHERE x.FAILURE_MODE = m.FAILURE_MODE AND x.ASSET_TYPE = {_lit(a['ASSET_TYPE'])}
                       AND x.PART_NO LIKE 'BRG%' AND m.PART_NO LIKE 'BRG%')))""").collect()
    part_lines, shortages = [], []
    for p in parts:
        short = p["ON_HAND_QTY"] < p["QTY"]
        late = p["LEAD_TIME_DAYS"] * 24 > hours
        part_lines.append(f"{p['PART_NO']} x{p['QTY']} ({p['DESCRIPTION']}) - on hand {p['ON_HAND_QTY']}"
                          + (" - SHORTAGE" if short else ""))
        if short:
            shortages.append((p["PART_NO"], p["QTY"] - p["ON_HAND_QTY"], late))

    # Context for the job plan
    rca = session.call("PLANTPULSE.ANALYTICS.ROOT_CAUSE_BRIEF", a["ASSET_ID"],
                       f"What is the most likely root cause and what job plan should the technician follow for suspected {mode}?")
    rca = json.loads(rca) if isinstance(rca, str) else rca
    prompt = f"""Write a predictive maintenance work-order job plan for a technician. Max 160 words, numbered steps, plain text.
Include: lockout/tagout first, inspection checks that confirm the diagnosis, corrective steps, parts to use, acceptance criteria
(vibration and temperature back within baseline), and what to record in the closing notes.
Asset: {a['ASSET_ID']} ({a['ASSET_TYPE']}), suspected failure mode: {mode}, priority {priority}.
Diagnosis context: {rca.get('answer', '')}
Parts reserved: {'; '.join(part_lines) or 'none'}"""
    try:
        plan = _complete(session, prompt)
    except Exception:
        plan = ("1. Apply LOTO per SOP DOC-015. 2. Verify diagnosis (vibration spectrum, temperature, oil/grease condition). "
                f"3. Execute corrective task for {mode}. 4. Fit parts: {', '.join(p['PART_NO'] for p in parts) or 'n/a'}. "
                "5. Restart, confirm vibration < baseline x1.2 and temperature within 5 C of baseline. 6. Record findings.")

    wo_id = "WO-PDM-" + str(session.sql("SELECT PLANTPULSE.ANALYTICS.WO_SEQ.NEXTVAL").collect()[0][0])
    desc = (f"PREDICTIVE - {mode.replace('_', ' ').title()} suspected on {a['ASSET_NAME']}. "
            f"Risk {a['RISK_SCORE']}/100, projected alarm in {round(hours)} h. Raised from alert {alert_id}.")
    session.sql(f"""INSERT INTO PLANTPULSE.ERP.WORK_ORDERS
        (WO_ID, ASSET_ID, WO_TYPE, PRIORITY, STATUS, CREATED_TS, SCHEDULED_TS, FAILURE_MODE, DESCRIPTION,
         PARTS_USED, SOURCE, ALERT_ID, RISK_SCORE, AI_JOB_PLAN, CREATED_BY)
        SELECT {_lit(wo_id)}, {_lit(a['ASSET_ID'])}, 'PDM', {_lit(priority)}, 'OPEN', CURRENT_TIMESTAMP()::TIMESTAMP_NTZ,
               DATEADD('hour', {window_h}, {_lit(a['AS_OF_TS'])}::TIMESTAMP_NTZ), {_lit(mode)}, {_lit(desc)},
               {_lit(','.join(p['PART_NO'] for p in parts))}, 'PLANTPULSE', {_lit(alert_id)}, {float(a['RISK_SCORE'] or 0)},
               {_lit(plan)}, {_lit(requested_by)}""").collect()
    prs = []
    for part_no, qty, late in shortages:
        pr_id = "PR-" + str(session.sql("SELECT PLANTPULSE.ANALYTICS.PR_SEQ.NEXTVAL").collect()[0][0])
        session.sql(f"""INSERT INTO PLANTPULSE.ERP.PURCHASE_REQUISITIONS
            SELECT {_lit(pr_id)}, {_lit(part_no)}, {int(qty)}, 'Stock-out for predictive WO', {_lit(wo_id)},
                   CURRENT_TIMESTAMP()::TIMESTAMP_NTZ, 'OPEN', {str(bool(late)).upper()}""").collect()
        prs.append({"pr_id": pr_id, "part_no": part_no, "qty": int(qty), "expedite": bool(late)})
    session.sql(f"""UPDATE PLANTPULSE.ANALYTICS.PDM_ALERTS SET STATUS = 'WO_CREATED', WO_ID = {_lit(wo_id)},
                    TRIAGED_BY = {_lit(requested_by)}, TRIAGED_TS = CURRENT_TIMESTAMP()::TIMESTAMP_NTZ
                    WHERE ALERT_ID = {_lit(alert_id)}""").collect()
    return {"status": "created", "wo_id": wo_id, "asset_id": a["ASSET_ID"], "priority": priority,
            "priority_reason": why, "schedule_within_hours": window_h, "failure_mode": mode, "parts": part_lines,
            "purchase_requisitions": prs, "job_plan": plan, "diagnosis": rca.get("answer"),
            "sources": rca.get("sources")}
$$;

-- ---------- run once ----------
CALL ANALYTICS.GENERATE_ALERTS();
SELECT ALERT_ID, ASSET_ID, SEVERITY, RISK_SCORE, SUSPECTED_MODE, HOURS_TO_ALARM, STATUS
FROM ANALYTICS.PDM_ALERTS ORDER BY RISK_SCORE DESC;
