-- =====================================================================
-- PlantPulse | 09 - Deploy the Command Center as Streamlit in Snowflake
--   scripts/deploy.py uploads app/streamlit_app.py, app/local_engine.py (demo fallback)
--   and app/environment.yml (pins streamlit + plotly)
--   to @ANALYTICS.APP_STAGE before running this file.
-- =====================================================================
USE WAREHOUSE PLANTPULSE_WH;
USE DATABASE PLANTPULSE;
USE SCHEMA ANALYTICS;

CREATE STAGE IF NOT EXISTS ANALYTICS.APP_STAGE DIRECTORY = (ENABLE = TRUE) COMMENT = 'Streamlit app source';

CREATE OR REPLACE STREAMLIT ANALYTICS.PLANTPULSE_COMMAND_CENTER
  FROM '@PLANTPULSE.ANALYTICS.APP_STAGE'
  MAIN_FILE = 'streamlit_app.py'
  RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'  -- warehouse runtime: packages come from environment.yml (Snowflake Anaconda channel)
  QUERY_WAREHOUSE = PLANTPULSE_WH
  TITLE = 'PlantPulse - Predictive Maintenance & OEE Command Center'
  COMMENT = 'Alert triage, root-cause copilot, work-order automation, OEE';

SHOW STREAMLITS LIKE 'PLANTPULSE_COMMAND_CENTER' IN SCHEMA ANALYTICS;
