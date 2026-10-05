-- =====================================================================
-- PlantPulse | 04 - OEE (Availability x Performance x Quality) and loss tree
-- =====================================================================
USE WAREHOUSE PLANTPULSE_WH;
USE DATABASE PLANTPULSE;
USE SCHEMA ANALYTICS;

CREATE OR REPLACE VIEW ANALYTICS.OEE_SHIFT COMMENT = 'OEE per line per shift' AS
SELECT p.LINE_ID, l.LINE_NAME, p.PLANT_CODE, l.PLANT_NAME, p.PROD_DATE, p.SHIFT, p.SHIFT_START_TS,
       p.PLANNED_TIME_MIN, p.RUN_TIME_MIN, p.BREAKDOWN_MIN, p.MINOR_STOP_MIN, p.CHANGEOVER_MIN,
       p.IDEAL_CYCLE_TIME_S, p.TOTAL_COUNT, p.GOOD_COUNT,
       p.IDEAL_CYCLE_TIME_S * p.TOTAL_COUNT / 60                                   AS IDEAL_RUN_MIN,
       p.RUN_TIME_MIN / NULLIF(p.PLANNED_TIME_MIN, 0)                              AS AVAILABILITY,
       (p.IDEAL_CYCLE_TIME_S * p.TOTAL_COUNT / 60) / NULLIF(p.RUN_TIME_MIN, 0)     AS PERFORMANCE,
       p.GOOD_COUNT / NULLIF(p.TOTAL_COUNT, 0)                                     AS QUALITY,
       ZEROIFNULL(AVAILABILITY) * ZEROIFNULL(PERFORMANCE) * ZEROIFNULL(QUALITY)    AS OEE,
       l.OEE_TARGET
FROM ERP.PRODUCTION_LOG p
JOIN ERP.PRODUCTION_LINES l ON l.LINE_ID = p.LINE_ID;

-- Volume-weighted daily OEE per line
CREATE OR REPLACE VIEW ANALYTICS.OEE_DAILY COMMENT = 'Daily OEE per line (time-weighted)' AS
SELECT LINE_ID, LINE_NAME, PLANT_NAME, PROD_DATE,
       SUM(RUN_TIME_MIN) / SUM(PLANNED_TIME_MIN)                              AS AVAILABILITY,
       SUM(IDEAL_RUN_MIN) / NULLIF(SUM(RUN_TIME_MIN), 0)                      AS PERFORMANCE,
       SUM(GOOD_COUNT) / NULLIF(SUM(TOTAL_COUNT), 0)                          AS QUALITY,
       (SUM(RUN_TIME_MIN) / SUM(PLANNED_TIME_MIN))
         * (SUM(IDEAL_RUN_MIN) / NULLIF(SUM(RUN_TIME_MIN), 0))
         * (SUM(GOOD_COUNT) / NULLIF(SUM(TOTAL_COUNT), 0))                    AS OEE,
       SUM(BREAKDOWN_MIN) AS BREAKDOWN_MIN, SUM(MINOR_STOP_MIN) AS MINOR_STOP_MIN, SUM(CHANGEOVER_MIN) AS CHANGEOVER_MIN,
       MAX(OEE_TARGET) AS OEE_TARGET
FROM ANALYTICS.OEE_SHIFT
GROUP BY 1, 2, 3, 4;

-- Six-big-losses style loss tree in minutes per line (whole window)
CREATE OR REPLACE VIEW ANALYTICS.OEE_LOSS_TREE COMMENT = 'Where planned time is lost, in minutes' AS
SELECT LINE_ID,
       SUM(PLANNED_TIME_MIN)                                                   AS PLANNED_MIN,
       SUM(BREAKDOWN_MIN)                                                      AS BREAKDOWN_LOSS_MIN,
       SUM(MINOR_STOP_MIN)                                                     AS MINOR_STOP_LOSS_MIN,
       SUM(CHANGEOVER_MIN)                                                     AS CHANGEOVER_LOSS_MIN,
       SUM(RUN_TIME_MIN - IDEAL_RUN_MIN)                                       AS SPEED_LOSS_MIN,
       SUM((TOTAL_COUNT - GOOD_COUNT) * IDEAL_CYCLE_TIME_S / 60)               AS QUALITY_LOSS_MIN,
       SUM(GOOD_COUNT * IDEAL_CYCLE_TIME_S / 60)                               AS FULLY_PRODUCTIVE_MIN
FROM ANALYTICS.OEE_SHIFT
GROUP BY 1;

-- Breakdown impact attributed to assets (from CMMS) - links maintenance to OEE
CREATE OR REPLACE VIEW ANALYTICS.ASSET_DOWNTIME_IMPACT COMMENT = 'Breakdown hours and cost per asset' AS
SELECT a.ASSET_ID, a.ASSET_NAME, a.ASSET_TYPE, a.LINE_ID, a.CRITICALITY,
       COUNT_IF(w.WO_TYPE = 'CM')                         AS BREAKDOWNS,
       SUM(IFF(w.WO_TYPE = 'CM', w.DOWNTIME_HOURS, 0))    AS BREAKDOWN_HOURS,
       SUM(IFF(w.WO_TYPE = 'CM', w.COST_INR, 0))          AS BREAKDOWN_COST_INR,
       COUNT_IF(w.WO_TYPE = 'PM' AND w.STATUS = 'CANCELLED') AS SKIPPED_PMS
FROM ERP.ASSETS a
LEFT JOIN ERP.WORK_ORDERS w ON w.ASSET_ID = a.ASSET_ID
GROUP BY 1, 2, 3, 4, 5;

SELECT LINE_ID, ROUND(AVG(OEE) * 100, 1) AS AVG_DAILY_OEE_PCT FROM ANALYTICS.OEE_DAILY GROUP BY 1 ORDER BY 1;
