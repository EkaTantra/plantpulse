---
name: oee-analyst
description: Explain PlantPulse OEE (availability x performance x quality) by line, plant or week using the governed semantic view PLANT_OPS_SV, break losses down with the loss tree, tie breakdown losses to specific assets, and estimate the OEE gain from preventing the currently predicted failures. Use for "why is OEE down", "which line is worst", "what is downtime costing us", MTTR, or PM compliance questions.
---

# OEE analyst

Gives the plant manager a governed, single definition of OEE and connects production losses to maintenance actions.
**Input:** a question about a line, plant or period, or none (then analyse the fleet).
**Output:** an OEE breakdown, the biggest losses, the assets driving them, and a quantified improvement case.

## Instructions
Use the semantic view `PLANTPULSE.ANALYTICS.PLANT_OPS_SV` for KPIs, so the definitions are the governed ones. Show each SQL statement you run. Never invent numbers. State every assumption.

**Fast path for a free-form KPI question** that the queries below don't cover, for example "Which asset types have the most breakdown downtime?":
```sql
CALL PLANTPULSE.ANALYTICS.ASK_OEE('<question>');
```
It returns JSON with `sql`, `columns`, `rows` (up to 50), a grounded `answer` and the `engine` (`cortex-analyst`, or `semantic-view text-to-sql` as the fallback). The generated SQL can only read `SEMANTIC_VIEW(PLANT_OPS_SV …)`. Show the returned `sql` to the user and quote numbers from `rows`. If it returns `error`, use the governed queries below instead.

1. **OEE by line**, or by week with `oee.prod_week`, or by plant with `lines.plant_name`:
   ```sql
   SELECT * FROM SEMANTIC_VIEW(PLANTPULSE.ANALYTICS.PLANT_OPS_SV
     DIMENSIONS lines.line_id
     METRICS oee.oee_pct, oee.availability_pct, oee.performance_pct, oee.quality_pct, oee.breakdown_loss_min)
   ORDER BY oee_pct;
   ```
   Compare against the 80% target in `ERP.PRODUCTION_LINES.OEE_TARGET`. For "this week", add `oee.prod_week` to DIMENSIONS and keep the latest week.

2. **Maintenance KPIs per line** (work orders roll up to lines through assets):
   ```sql
   SELECT * FROM SEMANTIC_VIEW(PLANTPULSE.ANALYTICS.PLANT_OPS_SV
     DIMENSIONS lines.line_id
     METRICS work_orders.breakdown_count, work_orders.mttr_hours, work_orders.pm_compliance_pct, work_orders.maintenance_cost_inr);
   ```

3. **Loss tree** (where planned minutes go):
   ```sql
   SELECT LINE_ID, PLANNED_MIN, BREAKDOWN_LOSS_MIN, MINOR_STOP_LOSS_MIN, CHANGEOVER_LOSS_MIN,
          ROUND(SPEED_LOSS_MIN) AS SPEED_LOSS_MIN, ROUND(QUALITY_LOSS_MIN) AS QUALITY_LOSS_MIN, ROUND(FULLY_PRODUCTIVE_MIN) AS FULLY_PRODUCTIVE_MIN
   FROM PLANTPULSE.ANALYTICS.OEE_LOSS_TREE WHERE LINE_ID = '<LINE_ID>';
   ```

4. **Assets behind the breakdown losses**, plus their current predicted risk:
   ```sql
   SELECT d.ASSET_ID, d.ASSET_TYPE, d.CRITICALITY, d.BREAKDOWNS, d.BREAKDOWN_HOURS, d.BREAKDOWN_COST_INR, d.SKIPPED_PMS,
          r.RISK_BAND, r.SUSPECTED_MODE, r.HOURS_TO_ALARM
   FROM PLANTPULSE.ANALYTICS.ASSET_DOWNTIME_IMPACT d
   JOIN PLANTPULSE.ANALYTICS.ASSET_RISK_SCORES r ON r.ASSET_ID = d.ASSET_ID
   WHERE d.LINE_ID = '<LINE_ID>' ORDER BY d.BREAKDOWN_HOURS DESC;
   ```

5. **Improvement case.** A failure caught about 48 h early becomes a planned stop. Assume the planned repair takes 35% of the unplanned breakdown duration (no waiting for parts or diagnosis, done in a scheduled window), so 65% of the breakdown minutes are recovered. Say that this is an assumption.
   ```sql
   SELECT LINE_ID,
          ROUND(100 * BREAKDOWN_LOSS_MIN / PLANNED_MIN, 2)                                   AS AVAILABILITY_LOST_TO_BREAKDOWNS_PTS,
          ROUND(100 * 0.65 * BREAKDOWN_LOSS_MIN / PLANNED_MIN, 2)                            AS AVAILABILITY_GAIN_PTS,
          ROUND(100 * 0.65 * BREAKDOWN_LOSS_MIN / PLANNED_MIN
                * (FULLY_PRODUCTIVE_MIN + QUALITY_LOSS_MIN) / NULLIF(PLANNED_MIN - BREAKDOWN_LOSS_MIN - MINOR_STOP_LOSS_MIN - CHANGEOVER_LOSS_MIN, 0)
                * FULLY_PRODUCTIVE_MIN / NULLIF(FULLY_PRODUCTIVE_MIN + QUALITY_LOSS_MIN, 0), 2) AS OEE_GAIN_PTS
   FROM PLANTPULSE.ANALYTICS.OEE_LOSS_TREE ORDER BY OEE_GAIN_PTS DESC;
   ```
   OEE gain = availability gain × current performance × current quality. Also translate the result into hours and corrective cost avoided, using `ASSET_DOWNTIME_IMPACT`. Mention any currently open alerts or predictive WOs on that line, from `ANALYTICS.PDM_ALERTS` and `ERP.WORK_ORDERS WHERE WO_TYPE = 'PDM'`.

6. **Answer** in this structure:
   - Headline: the worst line and its gap to target
   - A/P/Q table
   - Top 3 losses in minutes
   - The assets responsible, with repeat offenders and skipped PMs called out
   - The improvement case with its assumption
   - One recommended action

## Best practices
- Express OEE components as percentages with 1 decimal place, and losses in minutes and hours.
- Performance is usually the largest loss bucket, because of speed losses. Say so, but keep the maintenance link to availability and to performance during degradation.
- Note that the data is synthetic whenever you quote money.

## Example
User: `/oee-analyst Which line has the worst OEE and what is it costing us?`
→ PUN-L1: 76.9% vs the 80% target (A 93.0%, P 84.6%, Q 97.7%). 2,120 breakdown minutes, with gearbox PUN-L1-GBX-01 the top offender (bearing wear twice). Preventing breakdowns with predictive WOs would recover about 1.7 points of availability, worth about 1.4 OEE points (assuming planned repair takes 35% of breakdown time). The gearbox is degrading again now, so act on AL-1001.
