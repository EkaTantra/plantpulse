-- =====================================================================
-- PlantPulse | 06 - Semantic view for governed natural-language analytics
--   (Cortex Analyst / CoCo / Snowflake Intelligence all read the same
--    business definitions of OEE, downtime and risk)
-- =====================================================================
USE WAREHOUSE PLANTPULSE_WH;
USE DATABASE PLANTPULSE;
USE SCHEMA ANALYTICS;

CREATE OR REPLACE SEMANTIC VIEW ANALYTICS.PLANT_OPS_SV
  TABLES (
    lines AS PLANTPULSE.ERP.PRODUCTION_LINES PRIMARY KEY (LINE_ID)
      WITH SYNONYMS = ('production line', 'line') COMMENT = 'Production lines and OEE targets',
    assets AS PLANTPULSE.ERP.ASSETS PRIMARY KEY (ASSET_ID)
      WITH SYNONYMS = ('machine', 'equipment', 'asset') COMMENT = 'Rotating equipment master data',
    work_orders AS PLANTPULSE.ERP.WORK_ORDERS PRIMARY KEY (WO_ID)
      WITH SYNONYMS = ('maintenance job', 'WO', 'ticket') COMMENT = 'CMMS work orders (PM, CM, PDM)',
    risk AS PLANTPULSE.ANALYTICS.ASSET_RISK_SCORES PRIMARY KEY (ASSET_ID)
      WITH SYNONYMS = ('failure risk', 'health score', 'prediction') COMMENT = 'Latest predicted failure risk per asset',
    oee AS PLANTPULSE.ANALYTICS.OEE_SHIFT PRIMARY KEY (LINE_ID, PROD_DATE, SHIFT)
      WITH SYNONYMS = ('production', 'shift performance') COMMENT = 'Shift-level production and OEE inputs'
  )
  RELATIONSHIPS (
    assets_to_lines      AS assets (LINE_ID) REFERENCES lines,
    work_orders_to_asset AS work_orders (ASSET_ID) REFERENCES assets,
    risk_to_asset        AS risk (ASSET_ID) REFERENCES assets,
    oee_to_line          AS oee (LINE_ID) REFERENCES lines
  )
  FACTS (
    work_orders.wo_downtime_hours AS DOWNTIME_HOURS COMMENT = 'Equipment downtime caused by the job (hours)',
    work_orders.wo_cost_inr       AS COST_INR COMMENT = 'Labour + parts cost in INR',
    work_orders.wo_labor_hours    AS LABOR_HOURS,
    risk.risk_score_value         AS RISK_SCORE COMMENT = '0-100 failure risk (60% ML, 40% rules)',
    risk.hours_to_alarm_value     AS HOURS_TO_ALARM COMMENT = 'Projected hours until alarm limit is crossed',
    oee.planned_min               AS PLANNED_TIME_MIN,
    oee.run_min                   AS RUN_TIME_MIN,
    oee.breakdown_min             AS BREAKDOWN_MIN,
    oee.minor_stop_min            AS MINOR_STOP_MIN,
    oee.ideal_run_min             AS IDEAL_RUN_MIN,
    oee.total_units               AS TOTAL_COUNT,
    oee.good_units                AS GOOD_COUNT
  )
  DIMENSIONS (
    lines.line_id          AS LINE_ID,
    lines.line_name        AS LINE_NAME,
    lines.plant_name       AS PLANT_NAME WITH SYNONYMS = ('plant', 'site', 'factory'),
    assets.asset_id        AS ASSET_ID,
    assets.asset_name      AS ASSET_NAME,
    assets.asset_type      AS ASSET_TYPE WITH SYNONYMS = ('equipment type', 'machine type'),
    assets.criticality     AS CRITICALITY COMMENT = 'A = stops the line, B = degrades output, C = minor',
    assets.manufacturer    AS MANUFACTURER WITH SYNONYMS = ('OEM', 'vendor'),
    work_orders.wo_id      AS WO_ID,
    work_orders.wo_type    AS WO_TYPE COMMENT = 'PM = preventive, CM = corrective/breakdown, PDM = predictive',
    work_orders.wo_priority AS PRIORITY,
    work_orders.wo_status  AS STATUS,
    work_orders.failure_mode AS FAILURE_MODE WITH SYNONYMS = ('root cause', 'failure type'),
    work_orders.wo_created_date AS TO_DATE(CREATED_TS) WITH SYNONYMS = ('date raised'),
    work_orders.wo_month   AS DATE_TRUNC('MONTH', CREATED_TS),
    risk.risk_band         AS RISK_BAND COMMENT = 'HIGH >= 50, MEDIUM >= 30, else LOW',
    risk.suspected_mode    AS SUSPECTED_MODE WITH SYNONYMS = ('predicted failure mode'),
    oee.prod_date          AS PROD_DATE WITH SYNONYMS = ('production date', 'day'),
    oee.shift              AS SHIFT,
    oee.prod_week          AS DATE_TRUNC('WEEK', PROD_DATE)
  )
  METRICS (
    work_orders.total_downtime_hours AS SUM(work_orders.wo_downtime_hours)
      WITH SYNONYMS = ('downtime', 'lost hours'),
    work_orders.breakdown_count AS COUNT_IF(work_orders.wo_type = 'CM')
      WITH SYNONYMS = ('number of breakdowns', 'failures'),
    work_orders.maintenance_cost_inr AS SUM(work_orders.wo_cost_inr),
    work_orders.mttr_hours AS AVG(IFF(work_orders.wo_type = 'CM', work_orders.wo_downtime_hours, NULL))
      WITH SYNONYMS = ('mean time to repair', 'MTTR'),
    work_orders.pm_compliance_pct AS 100 * COUNT_IF(work_orders.wo_type = 'PM' AND work_orders.wo_status = 'CLOSED')
                                     / NULLIF(COUNT_IF(work_orders.wo_type = 'PM'), 0)
      WITH SYNONYMS = ('PM compliance', 'preventive maintenance compliance'),
    risk.avg_risk_score AS AVG(risk.risk_score_value),
    risk.high_risk_assets AS COUNT_IF(risk.risk_band = 'HIGH'),
    oee.availability_pct AS 100 * SUM(oee.run_min) / NULLIF(SUM(oee.planned_min), 0),
    oee.performance_pct  AS 100 * SUM(oee.ideal_run_min) / NULLIF(SUM(oee.run_min), 0),
    oee.quality_pct      AS 100 * SUM(oee.good_units) / NULLIF(SUM(oee.total_units), 0),
    oee.oee_pct AS 100 * (SUM(oee.run_min) / NULLIF(SUM(oee.planned_min), 0))
                       * (SUM(oee.ideal_run_min) / NULLIF(SUM(oee.run_min), 0))
                       * (SUM(oee.good_units) / NULLIF(SUM(oee.total_units), 0))
      WITH SYNONYMS = ('OEE', 'overall equipment effectiveness'),
    oee.breakdown_loss_min AS SUM(oee.breakdown_min),
    oee.good_output AS SUM(oee.good_units)
  )
  COMMENT = 'PlantPulse governed semantic layer: OEE, maintenance and predicted risk';

-- Query the semantic view directly (same definitions everywhere)
SELECT * FROM SEMANTIC_VIEW(
  ANALYTICS.PLANT_OPS_SV
  DIMENSIONS lines.line_id
  METRICS oee.oee_pct, oee.availability_pct, oee.breakdown_loss_min
) ORDER BY oee_pct;
