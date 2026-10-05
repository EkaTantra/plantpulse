-- =====================================================================
-- PlantPulse | 05 - Cortex Search over unstructured maintenance knowledge
--   OEM manuals + SOPs + reliability reviews + historical technician notes
-- =====================================================================
USE WAREHOUSE PLANTPULSE_WH;
USE DATABASE PLANTPULSE;
USE SCHEMA ANALYTICS;

CREATE OR REPLACE VIEW ANALYTICS.V_KNOWLEDGE_CORPUS AS
SELECT DOC_ID, DOC_TITLE, ASSET_TYPE, FAILURE_MODE, 'MANUAL' AS SOURCE_TYPE, NULL AS ASSET_ID, CONTENT
FROM KNOWLEDGE.MAINTENANCE_DOCS
UNION ALL
SELECT w.WO_ID, 'Work order ' || w.WO_ID || ' - ' || w.ASSET_ID || ' (' || TO_VARCHAR(w.CREATED_TS, 'YYYY-MM-DD') || ')',
       a.ASSET_TYPE, w.FAILURE_MODE, 'WORK_ORDER', w.ASSET_ID,
       w.DESCRIPTION || '. Technician notes: ' || w.TECHNICIAN_NOTES || '. Parts used: ' || COALESCE(w.PARTS_USED, 'none')
         || '. Downtime hours: ' || w.DOWNTIME_HOURS
FROM ERP.WORK_ORDERS w JOIN ERP.ASSETS a ON a.ASSET_ID = w.ASSET_ID
WHERE w.WO_TYPE = 'CM' AND w.TECHNICIAN_NOTES IS NOT NULL;

CREATE OR REPLACE CORTEX SEARCH SERVICE ANALYTICS.MAINTENANCE_KB_SEARCH
  ON CONTENT
  ATTRIBUTES ASSET_TYPE, FAILURE_MODE, SOURCE_TYPE, ASSET_ID
  WAREHOUSE = PLANTPULSE_WH
  TARGET_LAG = '1 hour'
  COMMENT = 'Hybrid (vector + keyword) search over manuals, SOPs and technician notes'
AS
  SELECT CONTENT, DOC_ID, DOC_TITLE, ASSET_TYPE, FAILURE_MODE, SOURCE_TYPE, ASSET_ID
  FROM ANALYTICS.V_KNOWLEDGE_CORPUS;

-- Smoke test
SELECT PARSE_JSON(SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
  'PLANTPULSE.ANALYTICS.MAINTENANCE_KB_SEARCH',
  '{"query": "gearbox vibration rising and bearing temperature high, what is the root cause?",
    "columns": ["DOC_TITLE", "SOURCE_TYPE", "CONTENT"],
    "filter": {"@or": [{"@eq": {"ASSET_TYPE": "Gearbox"}}, {"@eq": {"ASSET_TYPE": "ALL"}}]},
    "limit": 4}'
)):results AS RESULTS;
