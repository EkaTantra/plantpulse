-- =====================================================================
-- PlantPulse | 08 - Near-real-time loop: simulated sensor stream + scheduled scoring
--   In production the OT stream lands via Snowpipe Streaming / Kafka connector
--   from the plant historian; here a procedure simulates the next readings.
-- =====================================================================
USE WAREHOUSE PLANTPULSE_WH;
USE DATABASE PLANTPULSE;
USE SCHEMA ANALYTICS;

-- Appends the next 10-minute reading for every asset, continuing each asset's recent trend.
CREATE OR REPLACE PROCEDURE ANALYTICS.SIMULATE_SENSOR_STREAM(STEPS INTEGER DEFAULT 6)
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
DECLARE i INTEGER DEFAULT 0;
BEGIN
  WHILE (i < STEPS) DO
    INSERT INTO PLANTPULSE.OT.SENSOR_READINGS (ASSET_ID, READING_TS, VIBRATION_MM_S, TEMPERATURE_C, RPM, CURRENT_A)
    WITH last_ts AS (SELECT ASSET_ID, MAX(READING_TS) TS FROM PLANTPULSE.OT.SENSOR_READINGS GROUP BY 1),
    recent AS (
      SELECT s.ASSET_ID, l.TS,
             AVG(IFF(s.READING_TS > DATEADD('hour', -2, l.TS), s.VIBRATION_MM_S, NULL)) V2,
             AVG(IFF(s.READING_TS <= DATEADD('hour', -22, l.TS), s.VIBRATION_MM_S, NULL)) V24,
             AVG(IFF(s.READING_TS > DATEADD('hour', -2, l.TS), s.TEMPERATURE_C, NULL)) T2,
             AVG(IFF(s.READING_TS <= DATEADD('hour', -22, l.TS), s.TEMPERATURE_C, NULL)) T24,
             AVG(IFF(s.READING_TS > DATEADD('hour', -2, l.TS), s.RPM, NULL)) R2,
             AVG(IFF(s.READING_TS > DATEADD('hour', -2, l.TS), s.CURRENT_A, NULL)) C2
      FROM PLANTPULSE.OT.SENSOR_READINGS s
      JOIN last_ts l ON l.ASSET_ID = s.ASSET_ID AND s.READING_TS > DATEADD('hour', -24, l.TS)
      WHERE s.VIBRATION_MM_S < 24.5
      GROUP BY 1, 2
    )
    SELECT ASSET_ID, DATEADD('minute', 10, TS),
           ROUND(GREATEST(0.05, V2 + GREATEST(0, (V2 - V24) / 132) + 0.08 * V2 * NORMAL(0, 1, RANDOM())), 3),
           ROUND(T2 + GREATEST(0, (T2 - T24) / 132) + 0.6 * NORMAL(0, 1, RANDOM()), 2),
           ROUND(R2 * (1 + 0.002 * NORMAL(0, 1, RANDOM())), 1),
           ROUND(C2 * (1 + 0.02 * NORMAL(0, 1, RANDOM())), 2)
    FROM recent;
    i := i + 1;
  END WHILE;
  RETURN 'Streamed ' || STEPS || ' x 10-minute readings for every asset';
END;
$$;

-- Every 15 minutes: ingest -> rescore -> raise alerts (chained tasks).
CREATE OR REPLACE TASK ANALYTICS.T_STREAM_SENSORS
  WAREHOUSE = PLANTPULSE_WH SCHEDULE = '15 MINUTE'
  COMMENT = 'Simulated OT ingestion (replace with Snowpipe Streaming in production)'
AS CALL PLANTPULSE.ANALYTICS.SIMULATE_SENSOR_STREAM(1);

CREATE OR REPLACE TASK ANALYTICS.T_SCORE_ASSETS
  WAREHOUSE = PLANTPULSE_WH AFTER ANALYTICS.T_STREAM_SENSORS
AS CALL PLANTPULSE.ANALYTICS.SCORE_ASSETS(TRUE);

CREATE OR REPLACE TASK ANALYTICS.T_GENERATE_ALERTS
  WAREHOUSE = PLANTPULSE_WH AFTER ANALYTICS.T_SCORE_ASSETS
AS CALL PLANTPULSE.ANALYTICS.GENERATE_ALERTS();

-- Tasks are created SUSPENDED. To switch the live loop on for a demo:
--   ALTER TASK ANALYTICS.T_GENERATE_ALERTS RESUME;
--   ALTER TASK ANALYTICS.T_SCORE_ASSETS RESUME;
--   ALTER TASK ANALYTICS.T_STREAM_SENSORS RESUME;
-- and remember to SUSPEND them afterwards to save credits.

SHOW TASKS IN SCHEMA ANALYTICS;
