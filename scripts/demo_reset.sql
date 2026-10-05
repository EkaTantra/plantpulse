-- =====================================================================
-- PlantPulse | demo reset: return the account to the "now = 2026-10-05 00:00" demo state
--   * removes PlantPulse-created predictive WOs, alerts and purchase requisitions
--   * removes simulated sensor rows streamed after the end of the dataset
--   * suspends the live loop, rescores and re-raises the alert queue
-- Run:  snow sql -c <conn> -f scripts/demo_reset.sql
-- =====================================================================
USE WAREHOUSE PLANTPULSE_WH;
USE DATABASE PLANTPULSE;
USE SCHEMA ANALYTICS;

ALTER TASK IF EXISTS ANALYTICS.T_STREAM_SENSORS SUSPEND;
ALTER TASK IF EXISTS ANALYTICS.T_SCORE_ASSETS SUSPEND;
ALTER TASK IF EXISTS ANALYTICS.T_GENERATE_ALERTS SUSPEND;

DELETE FROM ERP.WORK_ORDERS WHERE WO_TYPE = 'PDM';
TRUNCATE TABLE ERP.PURCHASE_REQUISITIONS;
TRUNCATE TABLE ANALYTICS.PDM_ALERTS;
TRUNCATE TABLE ANALYTICS.ASSET_RISK_HISTORY;
DELETE FROM OT.SENSOR_READINGS WHERE READING_TS >= '2026-10-05 00:00:00'::TIMESTAMP_NTZ;

-- restart the business-key sequences so the demo shows WO-PDM-90001 / PR-5001
-- (alert ids restart at AL-1001 because PDM_ALERTS is emptied)
CREATE OR REPLACE SEQUENCE ANALYTICS.WO_SEQ START = 90001;
CREATE OR REPLACE SEQUENCE ANALYTICS.PR_SEQ START = 5001;

CALL ANALYTICS.SCORE_ASSETS(TRUE);
CALL ANALYTICS.GENERATE_ALERTS();

SELECT ALERT_ID, ASSET_ID, SEVERITY, RISK_SCORE, SUSPECTED_MODE, HOURS_TO_ALARM, STATUS
FROM ANALYTICS.PDM_ALERTS ORDER BY RISK_SCORE DESC;
