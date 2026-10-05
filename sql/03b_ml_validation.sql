-- =====================================================================
-- PlantPulse | 03b - Honest ML validation: out-of-time (OOT) split
--   The production model (03) is evaluated by Snowflake ML on a random hold-out,
--   which leaks adjacent hours of the same degradation episode into both sets.
--   Here a second classifier is trained only on what was known before a cutoff
--   and is scored on the later, unseen period: hourly metrics + event-level metrics.
--   The production FAILURE_RISK_MODEL and SCORE_ASSETS are not touched.
-- =====================================================================
USE WAREHOUSE PLANTPULSE_WH;
USE DATABASE PLANTPULSE;
USE SCHEMA ANALYTICS;

-- 1) Cutoff = midnight after the failure that completes ~2/3 of the failure history.
--    With 12 failures: the 8th failure (FL-0008, 2026-09-14 15:00) -> cutoff 2026-09-15 00:00,
--    leaving 4 failures (FL-0009..FL-0012) entirely in the unseen later period.
CREATE OR REPLACE TABLE ANALYTICS.ML_OOT_CUTOFF AS
WITH f AS (
  SELECT FAILURE_ID, FAILURE_TS,
         ROW_NUMBER() OVER (ORDER BY FAILURE_TS) AS RN,
         COUNT(*) OVER () AS N
  FROM ERP.FAILURE_EVENTS
)
SELECT DATEADD('day', 1, DATE_TRUNC('day', FAILURE_TS)) AS CUTOFF_TS,
       RN AS FAILURES_IN_TRAIN, N - RN AS FAILURES_IN_TEST,
       'Midnight after failure #' || RN || ' of ' || N || ' (' || FAILURE_ID || ' at '
         || TO_VARCHAR(FAILURE_TS, 'YYYY-MM-DD HH24:MI') || ') so ~2/3 of failures fall in training' AS RATIONALE
FROM f
WHERE RN = CEIL(2 * N / 3);

-- 2) Labelled asset-hours that keep ASSET_ID/HOUR_TS for splitting (same features, label and
--    exclusions as V_TRAINING_SET).
CREATE OR REPLACE VIEW ANALYTICS.V_TRAINING_SET_TS COMMENT = 'V_TRAINING_SET with ASSET_ID/HOUR_TS kept for time-based splits' AS
WITH lbl AS (
  SELECT f.ASSET_ID, f.HOUR_TS,
         COUNT_IF(fe.FAILURE_TS >  f.HOUR_TS AND fe.FAILURE_TS <= DATEADD('hour', 72, f.HOUR_TS)) AS N_FUTURE,
         MIN(IFF(fe.FAILURE_TS > f.HOUR_TS AND fe.FAILURE_TS <= DATEADD('hour', 72, f.HOUR_TS), fe.FAILURE_TS, NULL)) AS NEXT_FAILURE_TS,
         COUNT_IF(fe.FAILURE_TS <= f.HOUR_TS AND fe.FAILURE_TS >  DATEADD('hour', -(30 + fe.DOWNTIME_HOURS), f.HOUR_TS)) AS N_RECENT
  FROM ANALYTICS.ASSET_FEATURES f
  LEFT JOIN ERP.FAILURE_EVENTS fe ON fe.ASSET_ID = f.ASSET_ID
  GROUP BY 1, 2
)
SELECT f.ASSET_ID, f.HOUR_TS, l.NEXT_FAILURE_TS,
       f.ASSET_TYPE, f.CRITICALITY,
       f.VIB_RATIO, f.VIB_ALARM_RATIO, f.VIB_CV, f.TEMP_DELTA, f.TEMP_MARGIN, f.CUR_RATIO, f.CUR_VOLATILITY,
       f.RPM_CV, f.VIB_SLOPE_PER_DAY, f.TEMP_SLOPE_PER_DAY, f.PM_OVERDUE_DAYS,
       IFF(l.N_FUTURE > 0, 'FAIL', 'OK') AS FAILURE_72H
FROM ANALYTICS.ASSET_FEATURES f
JOIN lbl l ON l.ASSET_ID = f.ASSET_ID AND l.HOUR_TS = f.HOUR_TS
WHERE l.N_RECENT = 0
  AND f.HOUR_TS <= (SELECT DATEADD('hour', -72, MAX(HOUR_TS)) FROM ANALYTICS.ASSET_FEATURES);

-- 3) OOT training input: only what was knowable at the cutoff. An hour before the cutoff is kept
--    if its 72 h label window closed before the cutoff, or if it is a FAIL hour for a failure that
--    had already happened. Hours whose label depends on a post-cutoff failure are dropped.
--    Only the feature columns + label are passed to training.
CREATE OR REPLACE VIEW ANALYTICS.V_TRAINING_SET_OOT COMMENT = 'Training input for FAILURE_RISK_MODEL_OOT (pre-cutoff only)' AS
SELECT t.ASSET_TYPE, t.CRITICALITY,
       t.VIB_RATIO, t.VIB_ALARM_RATIO, t.VIB_CV, t.TEMP_DELTA, t.TEMP_MARGIN, t.CUR_RATIO, t.CUR_VOLATILITY,
       t.RPM_CV, t.VIB_SLOPE_PER_DAY, t.TEMP_SLOPE_PER_DAY, t.PM_OVERDUE_DAYS,
       t.FAILURE_72H
FROM ANALYTICS.V_TRAINING_SET_TS t
CROSS JOIN ANALYTICS.ML_OOT_CUTOFF c
WHERE t.HOUR_TS < c.CUTOFF_TS
  AND (DATEADD('hour', 72, t.HOUR_TS) < c.CUTOFF_TS
       OR (t.FAILURE_72H = 'FAIL' AND t.NEXT_FAILURE_TS < c.CUTOFF_TS));

-- 4) Train the OOT challenger (separate object; production model untouched)
CREATE OR REPLACE SNOWFLAKE.ML.CLASSIFICATION ANALYTICS.FAILURE_RISK_MODEL_OOT(
  INPUT_DATA     => SYSTEM$REFERENCE('VIEW', 'PLANTPULSE.ANALYTICS.V_TRAINING_SET_OOT'),
  TARGET_COLNAME => 'FAILURE_72H',
  CONFIG_OBJECT  => {'evaluate': FALSE, 'on_error': 'skip'}
);

-- 5) Score every asset-hour from the cutoff onward with the OOT model.
--    IN_EVAL = labelled hour (same exclusions as training) used for hourly metrics.
CREATE OR REPLACE TABLE ANALYTICS.ML_OOT_SCORES AS
WITH test AS (
  SELECT f.*
  FROM ANALYTICS.ASSET_FEATURES f
  CROSS JOIN ANALYTICS.ML_OOT_CUTOFF c
  WHERE f.HOUR_TS >= c.CUTOFF_TS
)
SELECT t.ASSET_ID, t.HOUR_TS,
       ANALYTICS.FAILURE_RISK_MODEL_OOT!PREDICT(INPUT_DATA => OBJECT_CONSTRUCT_KEEP_NULL(
         'ASSET_TYPE', t.ASSET_TYPE, 'CRITICALITY', t.CRITICALITY, 'VIB_RATIO', t.VIB_RATIO,
         'VIB_ALARM_RATIO', t.VIB_ALARM_RATIO, 'VIB_CV', t.VIB_CV, 'TEMP_DELTA', t.TEMP_DELTA,
         'TEMP_MARGIN', t.TEMP_MARGIN, 'CUR_RATIO', t.CUR_RATIO, 'CUR_VOLATILITY', t.CUR_VOLATILITY,
         'RPM_CV', t.RPM_CV, 'VIB_SLOPE_PER_DAY', t.VIB_SLOPE_PER_DAY, 'TEMP_SLOPE_PER_DAY', t.TEMP_SLOPE_PER_DAY,
         'PM_OVERDUE_DAYS', t.PM_OVERDUE_DAYS)):probability:FAIL::FLOAT AS P_FAIL,
       l.FAILURE_72H AS LABEL,
       (l.ASSET_ID IS NOT NULL) AS IN_EVAL
FROM test t
LEFT JOIN ANALYTICS.V_TRAINING_SET_TS l ON l.ASSET_ID = t.ASSET_ID AND l.HOUR_TS = t.HOUR_TS;

-- 6) Event-level results for each held-out failure: first hour with P(fail) >= 0.5 in the
--    8 days before it (searched only from the cutoff onward, so nothing is in-sample).
CREATE OR REPLACE VIEW ANALYTICS.V_ML_OOT_EVENTS COMMENT = 'Out-of-time event-level detection per held-out failure' AS
SELECT fe.FAILURE_ID, fe.ASSET_ID, fe.FAILURE_MODE, fe.FAILURE_TS, fe.DOWNTIME_HOURS,
       MIN(IFF(s.P_FAIL >= 0.5, s.HOUR_TS, NULL))                                   AS FIRST_ALERT_TS,
       (MIN(IFF(s.P_FAIL >= 0.5, s.HOUR_TS, NULL)) IS NOT NULL)                      AS DETECTED,
       DATEDIFF('hour', MIN(IFF(s.P_FAIL >= 0.5, s.HOUR_TS, NULL)), fe.FAILURE_TS)  AS LEAD_TIME_HOURS,
       DATEDIFF('hour', GREATEST(c.CUTOFF_TS, DATEADD('day', -8, fe.FAILURE_TS)), fe.FAILURE_TS) AS SEARCH_WINDOW_HOURS,
       ROUND(MAX(s.P_FAIL), 4)                                                      AS MAX_P_FAIL,
       COUNT_IF(s.P_FAIL >= 0.5 AND s.HOUR_TS >= DATEADD('hour', -72, fe.FAILURE_TS)) AS ALERT_HOURS_IN_72H
FROM ERP.FAILURE_EVENTS fe
CROSS JOIN ANALYTICS.ML_OOT_CUTOFF c
LEFT JOIN ANALYTICS.ML_OOT_SCORES s
  ON s.ASSET_ID = fe.ASSET_ID AND s.HOUR_TS < fe.FAILURE_TS AND s.HOUR_TS >= DATEADD('day', -8, fe.FAILURE_TS)
WHERE fe.FAILURE_TS >= c.CUTOFF_TS
GROUP BY fe.FAILURE_ID, fe.ASSET_ID, fe.FAILURE_MODE, fe.FAILURE_TS, fe.DOWNTIME_HOURS, c.CUTOFF_TS;

-- 7) Hours flagged (P >= 0.5) that are neither in a 72 h pre-failure window nor in a post-failure
--    recovery window (failure + 30 h + downtime). Only the labelled period is counted; the last
--    72 h of telemetry are reported separately because their outcome is not known yet.
CREATE OR REPLACE VIEW ANALYTICS.V_ML_OOT_FALSE_ALARMS AS
SELECT s.ASSET_ID, s.HOUR_TS, s.P_FAIL
FROM ANALYTICS.ML_OOT_SCORES s
WHERE s.P_FAIL >= 0.5
  AND s.HOUR_TS <= (SELECT DATEADD('hour', -72, MAX(HOUR_TS)) FROM ANALYTICS.ASSET_FEATURES)
  AND NOT EXISTS (
    SELECT 1 FROM ERP.FAILURE_EVENTS fe
    WHERE fe.ASSET_ID = s.ASSET_ID
      AND s.HOUR_TS >= DATEADD('hour', -72, fe.FAILURE_TS)
      AND s.HOUR_TS <  DATEADD('minute', ROUND((30 + fe.DOWNTIME_HOURS) * 60)::INT, fe.FAILURE_TS));

-- 8) Metrics table
CREATE OR REPLACE TABLE ANALYTICS.ML_VALIDATION (METRIC VARCHAR, VALUE FLOAT, NOTES VARCHAR);

INSERT INTO ANALYTICS.ML_VALIDATION
SELECT 'cutoff_ts_epoch', DATE_PART('epoch_second', CUTOFF_TS), TO_VARCHAR(CUTOFF_TS, 'YYYY-MM-DD HH24:MI') || ' - ' || RATIONALE
FROM ANALYTICS.ML_OOT_CUTOFF
UNION ALL SELECT 'failures_in_train', FAILURES_IN_TRAIN, 'Failures before the cutoff' FROM ANALYTICS.ML_OOT_CUTOFF
UNION ALL SELECT 'failures_in_test', FAILURES_IN_TEST, 'Held-out failures after the cutoff' FROM ANALYTICS.ML_OOT_CUTOFF
UNION ALL SELECT 'train_rows', COUNT(*), 'Asset-hours used to train FAILURE_RISK_MODEL_OOT' FROM ANALYTICS.V_TRAINING_SET_OOT
UNION ALL SELECT 'train_fail_rows', COUNT_IF(FAILURE_72H = 'FAIL'), 'FAIL-labelled training hours' FROM ANALYTICS.V_TRAINING_SET_OOT;

-- hourly confusion matrix and precision / recall / F1 at three thresholds
INSERT INTO ANALYTICS.ML_VALIDATION
WITH e AS (SELECT P_FAIL, LABEL = 'FAIL' AS Y FROM ANALYTICS.ML_OOT_SCORES WHERE IN_EVAL),
th AS (SELECT column1 AS T FROM VALUES (0.3), (0.5), (0.7)),
cm AS (
  SELECT th.T,
         COUNT_IF(e.Y AND e.P_FAIL >= th.T)          AS TP,
         COUNT_IF(NOT e.Y AND e.P_FAIL >= th.T)      AS FP,
         COUNT_IF(e.Y AND e.P_FAIL < th.T)           AS FN,
         COUNT_IF(NOT e.Y AND e.P_FAIL < th.T)       AS TN
  FROM e CROSS JOIN th GROUP BY th.T
),
m AS (
  SELECT T, TP, FP, FN, TN,
         TP / NULLIF(TP + FP, 0) AS PREC, TP / NULLIF(TP + FN, 0) AS REC
  FROM cm
)
SELECT 'hourly_' || k.K || '_at_' || LTRIM(TO_VARCHAR(m.T, '0.0')),
       CASE k.K WHEN 'tp' THEN m.TP WHEN 'fp' THEN m.FP WHEN 'fn' THEN m.FN WHEN 'tn' THEN m.TN
                WHEN 'precision' THEN ROUND(m.PREC, 4) WHEN 'recall' THEN ROUND(m.REC, 4)
                ELSE ROUND(2 * m.PREC * m.REC / NULLIF(m.PREC + m.REC, 0), 4) END::FLOAT,
       'Held-out labelled asset-hours, P(FAIL) >= ' || LTRIM(TO_VARCHAR(m.T, '0.0'))
FROM m
CROSS JOIN (SELECT column1 AS K FROM VALUES ('tp'), ('fp'), ('fn'), ('tn'), ('precision'), ('recall'), ('f1')) k;

-- hourly ROC AUC (Mann-Whitney rank statistic, ties get average rank) and PR-AUC (average precision)
INSERT INTO ANALYTICS.ML_VALIDATION
WITH e AS (SELECT P_FAIL, IFF(LABEL = 'FAIL', 1, 0) AS Y FROM ANALYTICS.ML_OOT_SCORES WHERE IN_EVAL),
r AS (SELECT P_FAIL, Y, AVG(RN) OVER (PARTITION BY P_FAIL) AS RK
      FROM (SELECT P_FAIL, Y, ROW_NUMBER() OVER (ORDER BY P_FAIL) AS RN FROM e)),
pr AS (
  SELECT Y,
         SUM(Y) OVER (ORDER BY P_FAIL DESC ROWS UNBOUNDED PRECEDING)
           / COUNT(*) OVER (ORDER BY P_FAIL DESC ROWS UNBOUNDED PRECEDING) AS PREC_AT_K
  FROM e
)
SELECT 'hourly_eval_rows', COUNT(*), 'Held-out labelled asset-hours' FROM e
UNION ALL SELECT 'hourly_eval_fail_rows', SUM(Y), 'Held-out FAIL-labelled asset-hours' FROM e
UNION ALL SELECT 'hourly_roc_auc',
       ROUND((SUM(IFF(Y = 1, RK, 0)) - SUM(Y) * (SUM(Y) + 1) / 2) / NULLIF(SUM(Y) * (COUNT(*) - SUM(Y)), 0), 4),
       'Rank-based ROC AUC on held-out hours'
FROM r
UNION ALL SELECT 'hourly_pr_auc', ROUND(SUM(IFF(Y = 1, PREC_AT_K, 0)) / NULLIF(SUM(Y), 0), 4),
       'Average precision on held-out hours (base rate = fail share)'
FROM pr;

-- event-level metrics
INSERT INTO ANALYTICS.ML_VALIDATION
SELECT 'events_detected', COUNT_IF(DETECTED), 'Held-out failures with P(FAIL) >= 0.5 in the 8 days before (searched from cutoff)' FROM ANALYTICS.V_ML_OOT_EVENTS
UNION ALL SELECT 'events_total', COUNT(*), 'Held-out failures' FROM ANALYTICS.V_ML_OOT_EVENTS
UNION ALL SELECT 'event_lead_time_median_h', MEDIAN(LEAD_TIME_HOURS), 'Median hours from first P>=0.5 to failure (detected events)' FROM ANALYTICS.V_ML_OOT_EVENTS
UNION ALL SELECT 'event_lead_time_min_h', MIN(LEAD_TIME_HOURS), 'Shortest lead time (detected events)' FROM ANALYTICS.V_ML_OOT_EVENTS
UNION ALL SELECT 'event_lead_time_max_h', MAX(LEAD_TIME_HOURS), 'Longest lead time (detected events)' FROM ANALYTICS.V_ML_OOT_EVENTS
UNION ALL SELECT 'false_alarm_hours', COUNT(*), 'P>=0.5 hours outside 72h pre-failure and post-failure recovery windows (labelled period)' FROM ANALYTICS.V_ML_OOT_FALSE_ALARMS
UNION ALL SELECT 'false_alarm_hours_early_warning', COUNT_IF(EXISTS_FAILURE_8D),
       'Of the false-alarm hours: those 72h-8d before a real failure on the same asset (early, not spurious)'
FROM (SELECT fa.ASSET_ID, fa.HOUR_TS,
             MAX(IFF(fe.FAILURE_ID IS NOT NULL, 1, 0)) = 1 AS EXISTS_FAILURE_8D
      FROM ANALYTICS.V_ML_OOT_FALSE_ALARMS fa
      LEFT JOIN ERP.FAILURE_EVENTS fe
        ON fe.ASSET_ID = fa.ASSET_ID AND fe.FAILURE_TS > fa.HOUR_TS AND fe.FAILURE_TS <= DATEADD('day', 8, fa.HOUR_TS)
      GROUP BY 1, 2)
UNION ALL SELECT 'false_alarm_assets', COUNT(DISTINCT ASSET_ID), 'Distinct assets with at least one false-alarm hour' FROM ANALYTICS.V_ML_OOT_FALSE_ALARMS
UNION ALL SELECT 'false_alarm_asset_days', COUNT(DISTINCT ASSET_ID || TO_VARCHAR(HOUR_TS::DATE)), 'Distinct asset-days with a false-alarm hour' FROM ANALYTICS.V_ML_OOT_FALSE_ALARMS
UNION ALL SELECT 'unlabelled_tail_alert_assets', COUNT(DISTINCT ASSET_ID),
       'Assets with P>=0.5 in the last 72 h of telemetry (outcome not yet known, not counted as false alarms)'
FROM ANALYTICS.ML_OOT_SCORES
WHERE P_FAIL >= 0.5 AND HOUR_TS > (SELECT DATEADD('hour', -72, MAX(HOUR_TS)) FROM ANALYTICS.ASSET_FEATURES);

SELECT METRIC, VALUE, NOTES FROM ANALYTICS.ML_VALIDATION ORDER BY METRIC;

SELECT * FROM ANALYTICS.V_ML_OOT_EVENTS ORDER BY FAILURE_TS;
