-- =====================================================================
-- PlantPulse | 11 - Cortex Agent for Snowflake Intelligence
--   One conversational reliability engineer over the same governed assets:
--     * Cortex Analyst on the semantic view PLANT_OPS_SV (OEE, MTTR, downtime, PM compliance, risk)
--     * Cortex Search on MAINTENANCE_KB_SEARCH (manuals, SOPs, technician notes)
--     * ROOT_CAUSE_BRIEF procedure (telemetry evidence + CMMS history + cited RAG brief)
--   Use it in Snowsight (AI & ML > Agents / Snowflake Intelligence) or via the Agents REST API.
-- =====================================================================
USE WAREHOUSE PLANTPULSE_WH;
USE DATABASE PLANTPULSE;
USE SCHEMA ANALYTICS;

CREATE OR REPLACE AGENT ANALYTICS.PLANTPULSE_RELIABILITY_AGENT
  COMMENT = 'PlantPulse reliability engineer: OEE and maintenance KPIs, maintenance knowledge base, root-cause briefs'
  PROFILE = '{"display_name": "PlantPulse Reliability Engineer", "color": "blue"}'
  FROM SPECIFICATION
  $$
  models:
    orchestration: claude-sonnet-4-5

  orchestration:
    budget:
      seconds: 120
      tokens: 32000

  instructions:
    orchestration: >
      You are a senior reliability engineer for two automotive component plants, Pune and Chennai
      (lines PUN-L1, PUN-L2, CHN-L1, CHN-L2; 24 rotating assets). "Now" is the latest telemetry
      timestamp, 2026-10-05 00:00, not the wall clock.
      Use PlantOpsAnalyst for any number: OEE and its availability/performance/quality components,
      breakdown losses, downtime, MTTR, breakdown counts, maintenance cost, PM compliance and current
      predicted failure risk per asset (risk band, suspected failure mode, hours to alarm).
      Work-order metrics roll up to lines through assets; OEE metrics are by line, plant, day, shift or week.
      Use MaintenanceKnowledge for what manuals, SOPs, reliability reviews and past technician notes say
      about a failure mode, symptom, procedure or asset type.
      Use RootCauseBrief when the user asks why a specific asset (ID like PUN-L1-GBX-01) is degrading or
      what to do about it. When a "why" question is about a line, first find the line's worst assets
      with PlantOpsAnalyst (breakdown downtime or high risk), then explain using the knowledge base or a brief.
      Never create, change or approve work orders, alerts or purchase requisitions; this agent is read-only.
    response: >
      Be concise and practical, like a reliability engineer briefing a plant manager.
      Lead with the direct answer, then the supporting numbers, then one recommended action.
      Never invent numbers: every figure must come from a tool result. Percentages to 1 decimal place.
      Cite knowledge-base documents and work orders by their ids in square brackets, for example [DOC-003] or [WO-70061].
      If the data cannot answer the question, say so and suggest what to check. All data is synthetic.
    sample_questions:
      - question: "Which line has the worst OEE and why?"
      - question: "What is MTTR and PM compliance by line?"
      - question: "Which assets are at high risk right now and what failure mode is suspected?"
      - question: "What does the manual say about gearbox bearing wear?"
      - question: "Why is PUN-L1-GBX-01 vibration rising and what should we do?"

  tools:
    - tool_spec:
        type: "cortex_analyst_text_to_sql"
        name: "PlantOpsAnalyst"
        description: >
          Governed plant-operations metrics from the semantic view PLANT_OPS_SV: OEE, availability,
          performance, quality, breakdown loss minutes and good output by line, plant, day, shift or week;
          work-order KPIs (total downtime hours, breakdown count, MTTR hours, maintenance cost in INR,
          PM compliance %) by line, asset, asset type, criticality, failure mode, work-order type or month;
          and current predicted failure risk per asset (risk score, risk band, suspected mode, hours to alarm).
    - tool_spec:
        type: "cortex_search"
        name: "MaintenanceKnowledge"
        description: >
          Hybrid search over OEM manuals, SOPs, reliability reviews (DOC- ids) and historical corrective
          work-order technician notes (WO- ids). Filterable by ASSET_TYPE, FAILURE_MODE, SOURCE_TYPE
          (MANUAL or WORK_ORDER) and ASSET_ID. Use for symptoms, causes, inspection steps and repair procedures.
    - tool_spec:
        type: "generic"
        name: "RootCauseBrief"
        description: >
          Runs the PlantPulse root-cause copilot for ONE asset: combines live condition evidence
          (vibration and temperature versus learned baseline, trends, ML probability, hours to alarm),
          the asset's CMMS history and cited knowledge-base excerpts into a diagnosis with recommended action.
          Returns JSON with answer, evidence and sources.
        input_schema:
          type: "object"
          properties:
            asset_id:
              type: "string"
              description: "Asset ID, e.g. PUN-L1-GBX-01 (format <PLANT>-<LINE>-<TYPE>-<NN>)"
            question:
              type: "string"
              description: "The engineer's question about this asset, e.g. why is vibration rising and what should we do"
          required: ["asset_id", "question"]

  tool_resources:
    PlantOpsAnalyst:
      semantic_view: "PLANTPULSE.ANALYTICS.PLANT_OPS_SV"
      execution_environment:
        type: "warehouse"
        warehouse: "PLANTPULSE_WH"
    MaintenanceKnowledge:
      search_service: "PLANTPULSE.ANALYTICS.MAINTENANCE_KB_SEARCH"
      max_results: "5"
      id_column: "DOC_ID"
      title_column: "DOC_TITLE"
    RootCauseBrief:
      type: "procedure"
      identifier: "PLANTPULSE.ANALYTICS.ROOT_CAUSE_BRIEF"
      execution_environment:
        type: "warehouse"
        warehouse: "PLANTPULSE_WH"
        query_timeout: 120
  $$;

-- Snowflake Intelligence visibility. Without a Snowflake Intelligence object, users see every agent
-- they have USAGE on. If the account has the curated object (created the first time someone edits the
-- Snowflake Intelligence settings in Snowsight), the agent must be added to it explicitly.
-- Other roles additionally need USAGE on the agent, the semantic view, the search service, the
-- procedure and PLANTPULSE_WH, e.g. GRANT USAGE ON AGENT ANALYTICS.PLANTPULSE_RELIABILITY_AGENT TO ROLE <role>.
EXECUTE IMMEDIATE $$
BEGIN
  ALTER SNOWFLAKE INTELLIGENCE SNOWFLAKE_INTELLIGENCE_OBJECT_DEFAULT ADD AGENT PLANTPULSE.ANALYTICS.PLANTPULSE_RELIABILITY_AGENT;
  RETURN 'Added to SNOWFLAKE_INTELLIGENCE_OBJECT_DEFAULT';
EXCEPTION
  WHEN OTHER THEN
    RETURN 'No curated Snowflake Intelligence object (or already added): agent is listed for every role with USAGE on it';
END;
$$;

DESCRIBE AGENT ANALYTICS.PLANTPULSE_RELIABILITY_AGENT;
