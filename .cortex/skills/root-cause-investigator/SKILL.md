---
name: root-cause-investigator
description: Investigate why a PlantPulse asset is degrading. Combines the live sensor trend against the learned baseline, the CMMS history of this asset and same-type assets, Cortex Search over manuals, SOPs and technician notes, and a Cortex LLM brief with citations. Use for "why is <asset> vibrating / overheating", "what is the root cause", or "what should we do about <asset>".
---

# Root-cause investigator

Natural-language root-cause analysis for one asset, grounded in structured telemetry and unstructured maintenance knowledge.
**Input:** an asset ID (for example `PUN-L1-GBX-01`), or an alert ID you resolve to its asset.
**Output to hand on:** diagnosis, evidence with citations (`DOC-xxx`, `WO-xxxxx`), confidence, and the recommended action. If action is needed, pass the alert ID to `/work-order-automator`.

## Instructions
Always query live data. Never invent readings, dates or document IDs. Cite only IDs returned by the queries. Show each SQL statement you run.

1. **Resolve the asset.** If you were given an alert ID:
   `SELECT ASSET_ID FROM PLANTPULSE.ANALYTICS.PDM_ALERTS WHERE ALERT_ID = '<id>';`
   Then load the current condition:
   ```sql
   SELECT ASSET_ID, ASSET_TYPE, LINE_ID, CRITICALITY, AS_OF_TS, RISK_SCORE, RISK_BAND, ML_PROB, SUSPECTED_MODE,
          HOURS_TO_ALARM, VIB_AVG_24H, BASE_VIB, VIB_RATIO, VIBRATION_ALARM_MM_S, TEMP_AVG_24H, TEMP_DELTA,
          TEMP_ALARM_C, CUR_RATIO, VIB_SLOPE_PER_DAY, TEMP_SLOPE_PER_DAY, DAYS_SINCE_PM
   FROM PLANTPULSE.ANALYTICS.ASSET_RISK_SCORES WHERE ASSET_ID = '<ASSET_ID>';
   ```

2. **Trend versus baseline, last 7 days** (daily summary of the hourly view):
   ```sql
   SELECT TO_DATE(h.HOUR_TS) AS DAY, ROUND(AVG(h.VIB_AVG), 2) AS VIB_MM_S, ROUND(MAX(h.VIB_MAX), 2) AS VIB_PEAK,
          ROUND(AVG(h.TEMP_AVG), 1) AS TEMP_C, ROUND(AVG(h.CUR_AVG), 1) AS CURRENT_A,
          ROUND(ANY_VALUE(b.BASE_VIB), 2) AS BASE_VIB, ROUND(ANY_VALUE(b.BASE_TEMP), 1) AS BASE_TEMP
   FROM PLANTPULSE.ANALYTICS.V_SENSOR_HOURLY h
   JOIN PLANTPULSE.ANALYTICS.V_ASSET_BASELINE b ON b.ASSET_ID = h.ASSET_ID
   WHERE h.ASSET_ID = '<ASSET_ID>'
     AND h.HOUR_TS > DATEADD('day', -7, (SELECT MAX(HOUR_TS) FROM PLANTPULSE.ANALYTICS.V_SENSOR_HOURLY WHERE ASSET_ID = '<ASSET_ID>'))
   GROUP BY 1 ORDER BY 1;
   ```
   Describe the shape: steady, accelerating rise, spiky, or temperature-led.

3. **Maintenance history: this asset, and breakdowns on same-type assets:**
   ```sql
   SELECT w.WO_ID, w.ASSET_ID, w.WO_TYPE, w.STATUS, TO_DATE(w.CREATED_TS) AS CREATED, w.FAILURE_MODE,
          w.DOWNTIME_HOURS, w.TECHNICIAN_NOTES
   FROM PLANTPULSE.ERP.WORK_ORDERS w JOIN PLANTPULSE.ERP.ASSETS a ON a.ASSET_ID = w.ASSET_ID
   WHERE w.ASSET_ID = '<ASSET_ID>'
      OR (w.WO_TYPE = 'CM' AND a.ASSET_TYPE = (SELECT ASSET_TYPE FROM PLANTPULSE.ERP.ASSETS WHERE ASSET_ID = '<ASSET_ID>'))
   ORDER BY (w.ASSET_ID = '<ASSET_ID>') DESC, w.CREATED_TS DESC LIMIT 12;
   ```
   Call out repeat failures of the same mode, and skipped PMs (`STATUS = 'CANCELLED'`) before a failure.

4. **Search the knowledge base** with Cortex Search (manuals, SOPs and technician notes):
   ```sql
   SELECT r.value:DOC_ID::STRING AS DOC_ID, r.value:DOC_TITLE::STRING AS TITLE, r.value:SOURCE_TYPE::STRING AS SOURCE,
          LEFT(r.value:CONTENT::STRING, 300) AS EXCERPT
   FROM TABLE(FLATTEN(PARSE_JSON(SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
     'PLANTPULSE.ANALYTICS.MAINTENANCE_KB_SEARCH',
     '{"query": "<asset type> <symptom words> <suspected mode in words>",
       "columns": ["DOC_ID","DOC_TITLE","SOURCE_TYPE","CONTENT"],
       "filter": {"@or": [{"@eq": {"ASSET_TYPE": "<ASSET_TYPE>"}}, {"@eq": {"ASSET_TYPE": "ALL"}}]},
       "limit": 5}'
   )):results)) r;
   ```
   `SEARCH_PREVIEW` needs literal string arguments, so substitute the values directly in the JSON text.

5. **Generate the cited brief** (Cortex LLM over the evidence above):
   ```sql
   CALL PLANTPULSE.ANALYTICS.ROOT_CAUSE_BRIEF('<ASSET_ID>', '<the user''s question>');
   ```
   It returns JSON with `answer`, `evidence` and `sources`. Escape single quotes in the question by doubling them.

6. **Write the final answer** in this structure:
   - **Diagnosis:** failure mode plus mechanism, with confidence (High/Medium/Low) and why
   - **Evidence:** 3–5 bullets mixing telemetry numbers, history (`WO-xxxxx`) and manuals (`DOC-xxx`)
   - **Recommended action and timing:** tie the timing to `HOURS_TO_ALARM` and the ML probability
   - **Risk if ignored:** expected downtime, using past CM downtime for the same mode
   - **Next step:** `/work-order-automator <ALERT_ID>` if a WO is warranted

## Best practices
- "Now" is `AS_OF_TS`. Compute "days ago" from it and the WO dates.
- If the suspected mode is `SENSOR_FAULT`, say plainly that the machine is healthy, and cite DOC-019.
- If the LLM call fails, the procedure returns a rule-based fallback. Use it and say so.
- `ML_PROB` is the probability of a functional failure within 72 hours. It is not the probability of a particular failure mode; the mode comes from the rule signature (`SUSPECTED_MODE`). Word it that way.

## Example
User: `/root-cause-investigator Why is PUN-L1-GBX-01 vibration rising and what should we do?`
→ Bearing wear recurrence (High confidence). Vibration 2.5× baseline and accelerating, temperature +7 °C. The same gearbox failed with outer-race spalling on 2026-09-14 [WO-xxxxx]. The bearing-wear guide [DOC-003] matches the signature. Replace bearing 6312-C3 and check alignment within 24 h. Next: `/work-order-automator AL-1001`.
