-- =====================================================================
-- PlantPulse | 10 - ASK_OEE: plain-English questions over the governed semantic view
--   1) Cortex Analyst (REST, from inside the procedure) on PLANT_OPS_SV
--   2) Fallback: Cortex COMPLETE text-to-SQL restricted to SEMANTIC_VIEW(PLANT_OPS_SV ...),
--      strictly validated, with one self-correction retry on a SQL error
--   Either way the SQL only reads the governed definitions of OEE, MTTR, PM compliance, risk.
--   Returns {question, sql, columns, rows (<= 50), answer, engine} or {error, sql}.
-- =====================================================================
USE WAREHOUSE PLANTPULSE_WH;
USE DATABASE PLANTPULSE;
USE SCHEMA ANALYTICS;

CREATE OR REPLACE PROCEDURE ANALYTICS.ASK_OEE(QUESTION VARCHAR)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
COMMENT = 'Plain-English OEE / maintenance KPI questions answered from the semantic view PLANT_OPS_SV'
AS
$$
import datetime
import decimal
import itertools
import json
import re

SV = "PLANTPULSE.ANALYTICS.PLANT_OPS_SV"
MAX_ROWS = 50

CATALOG = """Semantic view PLANTPULSE.ANALYTICS.PLANT_OPS_SV (logical tables and relationships):
  assets -> lines (many assets per line); work_orders -> assets; risk -> assets; oee -> lines.
DIMENSIONS
  lines.line_id, lines.line_name, lines.plant_name (plant: 'Pune' or 'Chennai')
  assets.asset_id, assets.asset_name, assets.asset_type (e.g. 'Gearbox', 'Coolant Pump', 'CNC Spindle', 'Motor'),
    assets.criticality ('A' stops the line, 'B', 'C'), assets.manufacturer
  work_orders.wo_id, work_orders.wo_type ('PM' preventive, 'CM' corrective/breakdown, 'PDM' predictive),
    work_orders.wo_priority, work_orders.wo_status, work_orders.failure_mode, work_orders.wo_created_date, work_orders.wo_month
  risk.risk_band ('HIGH','MEDIUM','LOW'), risk.suspected_mode
  oee.prod_date, oee.shift, oee.prod_week
METRICS
  oee.oee_pct, oee.availability_pct, oee.performance_pct, oee.quality_pct, oee.breakdown_loss_min, oee.good_output
  work_orders.total_downtime_hours, work_orders.breakdown_count, work_orders.maintenance_cost_inr,
    work_orders.mttr_hours, work_orders.pm_compliance_pct
  risk.avg_risk_score, risk.high_risk_assets
FACTS (row-level, usable in WHERE)
  work_orders.wo_downtime_hours, work_orders.wo_cost_inr, risk.risk_score_value, risk.hours_to_alarm_value
GROUPING RULES
  - oee.* metrics can be grouped only by lines.* and oee.* dimensions (NOT by assets.*).
  - work_orders.* metrics can be grouped by work_orders.*, assets.* and lines.* dimensions.
  - risk.* metrics can be grouped by risk.*, assets.* and lines.* dimensions.
  - There is no assets.line_id dimension: use lines.line_id.
  - Output column names are the bare metric/dimension names (e.g. oee_pct, line_id)."""

EXAMPLES = """Q: Which line has the worst OEE?
SELECT * FROM SEMANTIC_VIEW(PLANTPULSE.ANALYTICS.PLANT_OPS_SV DIMENSIONS lines.line_id, lines.line_name METRICS oee.oee_pct, oee.availability_pct, oee.performance_pct, oee.quality_pct) ORDER BY oee_pct ASC LIMIT 4
Q: What is MTTR by line?
SELECT * FROM SEMANTIC_VIEW(PLANTPULSE.ANALYTICS.PLANT_OPS_SV DIMENSIONS lines.line_id METRICS work_orders.mttr_hours, work_orders.breakdown_count) ORDER BY mttr_hours DESC
Q: Breakdown downtime by asset type
SELECT * FROM SEMANTIC_VIEW(PLANTPULSE.ANALYTICS.PLANT_OPS_SV DIMENSIONS assets.asset_type METRICS work_orders.total_downtime_hours, work_orders.breakdown_count WHERE work_orders.wo_type = 'CM') ORDER BY total_downtime_hours DESC
Q: Weekly OEE for Pune
SELECT * FROM SEMANTIC_VIEW(PLANTPULSE.ANALYTICS.PLANT_OPS_SV DIMENSIONS oee.prod_week, lines.plant_name METRICS oee.oee_pct WHERE lines.plant_name = 'Pune') ORDER BY prod_week"""

FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|CALL|EXECUTE|EXEC|COPY|PUT|GET|"
    r"REMOVE|LIST|USE|SET|UNSET|UNDROP|COMMIT|ROLLBACK|BEGIN|DESCRIBE|SHOW|SYSTEM\$\w*)\b", re.I)


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


def _clean(sql):
    """Strip code fences, comments and one trailing semicolon."""
    s = (sql or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    s = "\n".join(line for line in s.splitlines() if not line.strip().startswith("--"))
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S).strip()
    s = re.sub(r";\s*$", "", s).strip()
    return s


def _validate(sql, require_sv_call):
    """Raise ValueError unless sql is a single read-only SELECT over the semantic view."""
    if not sql:
        raise ValueError("empty SQL")
    no_str = re.sub(r"'(?:[^']|'')*'", "''", sql)          # ignore string literals for keyword checks
    if ";" in no_str:
        raise ValueError("multiple statements are not allowed")
    head = no_str.lstrip().upper()
    if not (head.startswith("SELECT") or (not require_sv_call and head.startswith("WITH"))):
        raise ValueError("query must start with SELECT")
    m = FORBIDDEN.search(no_str)
    if m:
        raise ValueError(f"keyword {m.group(1).upper()} is not allowed")
    compact = re.sub(r"\s+", "", no_str.upper())
    if require_sv_call and "SEMANTIC_VIEW(PLANTPULSE.ANALYTICS.PLANT_OPS_SV" not in compact:
        raise ValueError("query must read FROM SEMANTIC_VIEW(PLANTPULSE.ANALYTICS.PLANT_OPS_SV ...)")
    if "PLANT_OPS_SV" not in compact:
        raise ValueError("query must use the semantic view PLANT_OPS_SV")
    refs = set(re.findall(r"\bPLANTPULSE\.(\w+)\.(\w+)", no_str.upper()))
    if refs - {("ANALYTICS", "PLANT_OPS_SV")}:
        raise ValueError("query may only reference the semantic view")


def _jsonable(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


def _execute(session, sql):
    df = session.sql(sql)
    cols = [c.strip('"') for c in df.columns]
    rows = [[_jsonable(v) for v in r] for r in itertools.islice(df.to_local_iterator(), MAX_ROWS)]
    return cols, rows


def _analyst_sql(question):
    """Ask Cortex Analyst for SQL over the semantic view. Returns (sql, interpretation) or raises."""
    import _snowflake
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
            "semantic_view": SV, "stream": False}
    resp = _snowflake.send_snow_api_request("POST", "/api/v2/cortex/analyst/message", {}, {}, body, None, 60000)
    if int(resp.get("status", 0)) != 200:
        raise RuntimeError(f"Cortex Analyst HTTP {resp.get('status')}: {str(resp.get('content'))[:300]}")
    content = json.loads(resp["content"]) if isinstance(resp.get("content"), str) else resp.get("content")
    sql, text = None, ""
    for part in content.get("message", {}).get("content", []):
        if part.get("type") == "sql" and part.get("statement"):
            sql = part["statement"]
        elif part.get("type") == "text":
            text = part.get("text", "")
    if not sql:
        raise RuntimeError("Cortex Analyst returned no SQL: " + text[:300])
    return _clean(sql), text


def _t2s_prompt(question, previous_sql=None, error=None):
    p = f"""You translate plant-operations questions into ONE Snowflake query over a governed semantic view.
{CATALOG}

Return ONLY the SQL, no explanation, no markdown, no semicolon. The query MUST have exactly this form:
SELECT * FROM SEMANTIC_VIEW(PLANTPULSE.ANALYTICS.PLANT_OPS_SV DIMENSIONS <dims> METRICS <metrics> [WHERE <condition on dimensions/facts>]) [ORDER BY <output column>] [LIMIT n]
Use fully qualified names (table.name) inside SEMANTIC_VIEW(...) and bare output column names in ORDER BY.
Use only names listed above and obey the grouping rules. Prefer a few useful related metrics.

Examples:
{EXAMPLES}

Q: {question}"""
    if previous_sql:
        p += f"\n\nYour previous query failed.\nQuery: {previous_sql}\nError: {error}\nReturn a corrected query."
    return p


def _answer(session, question, cols, rows):
    prompt = f"""Answer the plant manager's question in 1-3 sentences using ONLY the query result below.
Quote the key numbers (round to 1 decimal place, % where the column is a percentage) and name the line/plant/asset.
If the result is empty, say no matching data was found. Do not invent numbers. Plain text, no markdown.
Question: {question}
Columns: {json.dumps(cols)}
Rows: {json.dumps(rows[:MAX_ROWS], default=str)}"""
    try:
        return _complete(session, prompt).strip()
    except Exception as e:
        return f"(summary unavailable: {str(e)[:100]}) First row: " + ", ".join(f"{c}={v}" for c, v in zip(cols, rows[0] if rows else []))


def run(session, question):
    question = (question or "").strip()
    if not question:
        return {"error": "Please ask a question.", "sql": None}
    attempted = None
    errors = []

    # 1) Cortex Analyst
    try:
        sql, _ = _analyst_sql(question)
        attempted = sql
        _validate(sql, require_sv_call=False)
        cols, rows = _execute(session, sql)
        return {"question": question, "sql": sql, "columns": cols, "rows": rows,
                "answer": _answer(session, question, cols, rows), "engine": "cortex-analyst"}
    except Exception as e:
        errors.append(f"cortex-analyst: {str(e)[:300]}")

    # 2) Text-to-SQL restricted to SEMANTIC_VIEW(...), one retry with the error fed back
    prev_sql, prev_err = None, None
    for _ in range(2):
        try:
            sql = _clean(_complete(session, _t2s_prompt(question, prev_sql, prev_err)))
            attempted = sql
            _validate(sql, require_sv_call=True)
            cols, rows = _execute(session, sql)
            return {"question": question, "sql": sql, "columns": cols, "rows": rows,
                    "answer": _answer(session, question, cols, rows), "engine": "semantic-view text-to-sql"}
        except Exception as e:
            prev_sql, prev_err = attempted, str(e)[:500]
            errors.append(f"text-to-sql: {prev_err}")
    return {"error": "Could not answer from the semantic view. " + " | ".join(errors), "sql": attempted}
$$;

-- Smoke test
CALL ANALYTICS.ASK_OEE('Which line has the worst OEE?');
