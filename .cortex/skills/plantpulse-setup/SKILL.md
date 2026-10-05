---
name: plantpulse-setup
description: Check, build or reset the PlantPulse environment in Snowflake (database PLANTPULSE). Verifies row counts, the ML model, the Cortex Search service, the semantic view, procedures, tasks and the Streamlit app, and runs the SQL build in order when anything is missing. Idempotent. Use for "set up PlantPulse", "is PlantPulse healthy", "reset the demo", or before running the other PlantPulse skills.
---

# PlantPulse setup and health check

Builds or verifies everything the other skills depend on. It is safe to run repeatedly.
**Output:** a status table (component · expected · actual · OK/FIX) and a one-line verdict.

## Instructions
Show each SQL statement you run. Ask before any rebuild that drops data (`sql/00_setup.sql` recreates the tables) or before resetting the demo.

1. **Health check** (read-only):
   ```sql
   SELECT 'ERP.ASSETS' AS OBJ, 24 AS EXPECTED, COUNT(*) AS ACTUAL FROM PLANTPULSE.ERP.ASSETS
   UNION ALL SELECT 'OT.SENSOR_READINGS (>=)', 207360, COUNT(*) FROM PLANTPULSE.OT.SENSOR_READINGS
   UNION ALL SELECT 'ERP.FAILURE_EVENTS', 12, COUNT(*) FROM PLANTPULSE.ERP.FAILURE_EVENTS
   UNION ALL SELECT 'ERP.WORK_ORDERS (PM+CM)', 60, COUNT_IF(WO_TYPE IN ('PM','CM')) FROM PLANTPULSE.ERP.WORK_ORDERS
   UNION ALL SELECT 'ERP.SPARE_PARTS', 13, COUNT(*) FROM PLANTPULSE.ERP.SPARE_PARTS
   UNION ALL SELECT 'ERP.PRODUCTION_LOG', 716, COUNT(*) FROM PLANTPULSE.ERP.PRODUCTION_LOG
   UNION ALL SELECT 'KNOWLEDGE.MAINTENANCE_DOCS', 20, COUNT(*) FROM PLANTPULSE.KNOWLEDGE.MAINTENANCE_DOCS
   UNION ALL SELECT 'ANALYTICS.ASSET_RISK_SCORES', 24, COUNT(*) FROM PLANTPULSE.ANALYTICS.ASSET_RISK_SCORES
   UNION ALL SELECT 'ANALYTICS.V_BACKTEST detected', 12, COUNT_IF(LEAD_TIME_HOURS > 0) FROM PLANTPULSE.ANALYTICS.V_BACKTEST
   UNION ALL SELECT 'ANALYTICS.V_BACKTEST mode correct', 12, COUNT_IF(MODE_CORRECT) FROM PLANTPULSE.ANALYTICS.V_BACKTEST
   UNION ALL SELECT 'ANALYTICS.V_ML_OOT_EVENTS detected (out-of-time)', 4, COUNT_IF(DETECTED) FROM PLANTPULSE.ANALYTICS.V_ML_OOT_EVENTS
   UNION ALL SELECT 'ANALYTICS.ML_VALIDATION metrics (>=)', 30, COUNT(*) FROM PLANTPULSE.ANALYTICS.ML_VALIDATION;
   ```
   Then check the objects:
   ```sql
   SHOW SNOWFLAKE.ML.CLASSIFICATION IN SCHEMA PLANTPULSE.ANALYTICS;
   SHOW CORTEX SEARCH SERVICES IN SCHEMA PLANTPULSE.ANALYTICS;
   SHOW SEMANTIC VIEWS IN SCHEMA PLANTPULSE.ANALYTICS;
   SHOW PROCEDURES IN SCHEMA PLANTPULSE.ANALYTICS;
   SHOW TASKS IN SCHEMA PLANTPULSE.ANALYTICS;
   SHOW STREAMLITS IN SCHEMA PLANTPULSE.ANALYTICS;
   SHOW AGENTS IN SCHEMA PLANTPULSE.ANALYTICS;
   ```
   Expected:
   - models `FAILURE_RISK_MODEL` (production) and `FAILURE_RISK_MODEL_OOT` (out-of-time validation, results in table `ML_VALIDATION`)
   - search service `MAINTENANCE_KB_SEARCH`, indexed and active
   - semantic view `PLANT_OPS_SV`
   - procedures `SCORE_ASSETS`, `GENERATE_ALERTS`, `TRIAGE_ALERT`, `ROOT_CAUSE_BRIEF`, `CREATE_PDM_WORK_ORDER`, `SIMULATE_SENSOR_STREAM`, `ASK_OEE`
   - tasks `T_STREAM_SENSORS` → `T_SCORE_ASSETS` → `T_GENERATE_ALERTS`, all **suspended** unless a live demo is running
   - Streamlit app `PLANTPULSE_COMMAND_CENTER`
   - Cortex Agent `PLANTPULSE_RELIABILITY_AGENT`, which Snowflake Intelligence uses

2. **Smoke-test the AI services:**
   ```sql
   SELECT KEY, VALUE FROM PLANTPULSE.ANALYTICS.APP_CONFIG;
   SELECT SNOWFLAKE.CORTEX.COMPLETE('<LLM_MODEL value from above>', 'Reply with OK');  -- the model name must be a literal
   CALL PLANTPULSE.ANALYTICS.SCORE_ASSETS(TRUE);
   CALL PLANTPULSE.ANALYTICS.ASK_OEE('Which line has the worst OEE?');
   ```
   `SCORE_ASSETS` should return `ml+rules`. `ASK_OEE` should return JSON with rows and an `answer` naming PUN-L1 (76.9%). If the LLM errors, update `APP_CONFIG.LLM_MODEL` to an available model, such as the value of `LLM_FALLBACK_MODEL`.

3. **Build whatever is missing.** If the database or tables are missing, run the one-shot deployer from the repo root:
   ```bash
   python scripts/deploy.py --connection <connection_name>
   ```
   It runs `sql/00_setup.sql`, uploads `data/*.csv`, then runs `sql/01_load.sql` to `sql/08_streaming_and_tasks.sql` (with `03b_ml_validation.sql` after 03), `sql/10_ask_oee.sql` and `sql/11_cortex_agent.sql`, and deploys the Streamlit app (`sql/09_streamlit_app.sql`). To rebuild a single layer, run that file alone, for example `python scripts/deploy.py -c <conn> --only 05_cortex_search.sql`. The layers are, in order: 02 features, 03 ML model, 03b out-of-time ML validation, 04 OEE, 05 Cortex Search, 06 semantic view, 07 automation procedures, 08 stream and tasks, 10 ASK_OEE, 11 Cortex Agent.

4. **Reset the demo** on request. This removes PlantPulse-created WOs, alerts and PRs, and any simulated sensor rows, then rescores:
   ```bash
   snow sql -c <connection_name> -f scripts/demo_reset.sql
   ```
   Expected afterwards: 4 alerts AL-1001 to AL-1004 (1 CRITICAL, 2 WARNING, 1 INFO), with no predictive WOs.

5. **Report** a status table plus the verdict "PlantPulse ready", or the list of fixes applied. Suggest `/asset-health-triage` as the next step.

## Best practices
- Never resume the tasks unless the user asks. They consume credits every 15 minutes. If you resume them, remind the user to suspend them.
- Do not touch objects outside the `PLANTPULSE` database or the `PLANTPULSE_WH` warehouse.
