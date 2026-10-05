---
name: asset-health-triage
description: Rescore every PlantPulse asset, raise predictive-maintenance alerts and present a ranked, evidence-backed triage queue (including likely false alarms). Use when the user asks which machines are at risk, what needs attention, or wants to acknowledge or dismiss an alert.
---

# Asset health triage

Answers "Which assets are at risk right now?" for the reliability engineer, using the live Snowflake objects in database `PLANTPULSE`.
**Output to hand on:** alert IDs (`AL-xxxx`) and asset IDs for `/root-cause-investigator` and `/work-order-automator`.

## When to use
- "Which assets are at risk?", "What should maintenance look at today?", "Show me the alert queue"
- "Acknowledge / dismiss alert AL-1004", "Is the CHN-L1-MTR-01 alarm real?"

## Instructions
Always query live tables. Never invent numbers. Show each SQL statement you run (in a short code block) before its result.

1. **Rescore, then raise alerts**, in this order, as two separate calls one after the other (warehouse `PLANTPULSE_WH`). Alerts are generated from the fresh scores, so never run them in parallel:
   ```sql
   CALL PLANTPULSE.ANALYTICS.SCORE_ASSETS(TRUE);
   CALL PLANTPULSE.ANALYTICS.GENERATE_ALERTS();
   ```
   Report the two return messages verbatim. `ml+rules` means the Snowflake ML classifier and the rule engine both scored. `rules-only` means the model was unavailable; say so.

2. **Fetch the open queue with its evidence:**
   ```sql
   SELECT a.ALERT_ID, a.ASSET_ID, r.ASSET_TYPE, r.LINE_ID, r.CRITICALITY, a.SEVERITY, a.STATUS,
          r.RISK_SCORE, r.RISK_BAND, r.ML_PROB, r.RULE_SCORE, r.SUSPECTED_MODE, r.HOURS_TO_ALARM,
          r.VIB_RATIO, r.VIB_AVG_24H, r.VIBRATION_ALARM_MM_S, r.TEMP_DELTA, r.TEMP_AVG_24H, r.TEMP_ALARM_C,
          r.VIB_SLOPE_PER_DAY, r.CUR_RATIO, r.SATURATED_24H, r.DAYS_SINCE_PM, r.AS_OF_TS
   FROM PLANTPULSE.ANALYTICS.PDM_ALERTS a
   JOIN PLANTPULSE.ANALYTICS.ASSET_RISK_SCORES r ON r.ASSET_ID = a.ASSET_ID
   WHERE a.STATUS IN ('NEW', 'ACKNOWLEDGED', 'WO_CREATED')
   ORDER BY a.RISK_SCORE DESC;
   ```

3. **Present a ranked table** with these columns: Rank · Alert · Asset (type, line, criticality) · Severity · Risk /100 · ML P(fail ≤72 h) · Suspected mode · Key evidence · Hours to alarm · Status.
   - Key evidence: `vibration 2.48× baseline (4.96 of 7.1 mm/s)`, `temp +7.3 °C`, plus slope or current if relevant.
   - `HOURS_TO_ALARM` ≥ 9999 means no rising trend; show it as "no trend".

4. **Flag likely false alarms.** An alert with `SUSPECTED_MODE = 'SENSOR_FAULT'` (`SATURATED_24H > 0`, vibration ratio ≈ 1, temperature normal) is a transmitter saturating at 24.9 mm/s, not a machine fault (knowledge doc DOC-019). Recommend **DISMISS** and an instrumentation check.

5. **Recommend next actions** per alert, using the policy in DOC-016:
   - CRITICAL, or ML P(fail) ≥ 0.5 on a criticality A/B asset: create a predictive WO now (P2, within 24 h). Point to `/work-order-automator <alert id>`.
   - WARNING: investigate first with `/root-cause-investigator <asset id>`.
   - SENSOR_FAULT: dismiss.

6. **Triage on request only.** Before any write, state exactly what will change and ask the user to confirm. Then:
   ```sql
   CALL PLANTPULSE.ANALYTICS.TRIAGE_ALERT('<ALERT_ID>', 'DISMISS' | 'ACK', '<short reason with evidence>', '<user>');
   ```
   Re-query the alert row to show the new `STATUS`.

## Best practices
- Lead with one sentence of headline, for example "3 assets need action; 1 alert is a sensor fault."
- Keep units: mm/s, °C, hours. Round to 1–2 decimals.
- "Now" is `AS_OF_TS`, the latest telemetry, not the wall clock.
- If the queue is empty, show the top 5 rows of `ASSET_RISK_SCORES` by `RISK_SCORE` so the user still sees fleet health.

## Example
User: `/asset-health-triage Which assets are at risk right now?`
→ Rescore (ml+rules), 4 alerts. AL-1001 PUN-L1-GBX-01 CRITICAL, bearing wear, vibration 2.5× baseline, ~44 h to alarm. AL-1002 / AL-1003 WARNING. AL-1004 CHN-L1-MTR-01 INFO is a sensor fault: recommend dismiss. Next: `/root-cause-investigator PUN-L1-GBX-01`.
