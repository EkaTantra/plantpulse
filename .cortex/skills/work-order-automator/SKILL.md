---
name: work-order-automator
description: Turn a PlantPulse predictive-maintenance alert into a prioritised CMMS work order. Applies the priority policy, reserves spare parts with a stock check, raises purchase requisitions for shortages and attaches an AI job plan, then verifies the result in ERP.WORK_ORDERS. Use for "create the work order for <alert/asset>", "raise a PdM job", or "what parts do we need".
---

# Work-order automator

Closes the loop from prediction to action: alert → work order (WO) → parts → purchase requisition (PR).
**Input:** an alert ID (`AL-xxxx`), or an asset ID you resolve to its open alert.
**Output:** WO ID (`WO-PDM-9xxxx`), priority and reason, schedule window, parts and stock status, PRs, and the job plan.

## Instructions
Show each SQL statement you run. Never fabricate a WO or PR ID. Only report what the procedure returns.

1. **Resolve and preview** (read-only):
   ```sql
   SELECT a.ALERT_ID, a.ASSET_ID, a.SEVERITY, a.STATUS, a.WO_ID, r.ASSET_NAME, r.ASSET_TYPE, r.CRITICALITY,
          r.RISK_SCORE, r.ML_PROB, r.SUSPECTED_MODE, r.HOURS_TO_ALARM, r.AS_OF_TS
   FROM PLANTPULSE.ANALYTICS.PDM_ALERTS a
   JOIN PLANTPULSE.ANALYTICS.ASSET_RISK_SCORES r ON r.ASSET_ID = a.ASSET_ID
   WHERE a.ALERT_ID = '<ALERT_ID>';   -- or: WHERE a.ASSET_ID = '<ASSET_ID>' AND a.STATUS IN ('NEW','ACKNOWLEDGED')
   ```
   - If `STATUS = 'WO_CREATED'`, report the existing `WO_ID` and stop.
   - If `STATUS = 'DISMISSED'` or the mode is `SENSOR_FAULT`, warn the user that this is a false alarm and only an instrumentation check (P4) is appropriate.

2. **Preview parts and stock** for the suspected mode:
   ```sql
   SELECT m.PART_NO, s.DESCRIPTION, m.QTY AS NEEDED, s.ON_HAND_QTY, s.LEAD_TIME_DAYS,
          IFF(s.ON_HAND_QTY < m.QTY, 'SHORTAGE', 'OK') AS STOCK
   FROM PLANTPULSE.ANALYTICS.FAILURE_MODE_PARTS m
   JOIN PLANTPULSE.ERP.SPARE_PARTS s ON s.PART_NO = m.PART_NO
   WHERE m.FAILURE_MODE = '<SUSPECTED_MODE>' AND m.ASSET_TYPE IN ('ALL', '<ASSET_TYPE>');
   ```
   The procedure substitutes asset-specific bearings (for example, CNC spindles use BRG-SPN-7014).

3. **Explain the priority policy** (DOC-016) before writing:
   - P2: failure predicted within 72 h on a criticality A/B asset. Schedule within 24 h.
   - P3: degradation without a predicted failure within 72 h. Schedule within the week.
   - P4: instrumentation check.

4. **Ask for confirmation.** State: "This will create 1 predictive WO in ERP.WORK_ORDERS, reserve <parts>, raise PRs for any shortage, and set alert <id> to WO_CREATED. Proceed?" Only continue on an explicit yes.

5. **Create the WO** (about 30 s, because it calls the root-cause brief and Cortex LLM):
   ```sql
   CALL PLANTPULSE.ANALYTICS.CREATE_PDM_WORK_ORDER('<ALERT_ID>', '<user name>');
   ```

6. **Verify in the CMMS tables:**
   ```sql
   SELECT WO_ID, ASSET_ID, WO_TYPE, PRIORITY, STATUS, SCHEDULED_TS, FAILURE_MODE, PARTS_USED, RISK_SCORE, ALERT_ID, CREATED_BY
   FROM PLANTPULSE.ERP.WORK_ORDERS WHERE WO_ID = '<WO_ID>';
   SELECT * FROM PLANTPULSE.ERP.PURCHASE_REQUISITIONS WHERE WO_ID = '<WO_ID>';
   SELECT ALERT_ID, STATUS, WO_ID FROM PLANTPULSE.ANALYTICS.PDM_ALERTS WHERE ALERT_ID = '<ALERT_ID>';
   ```

7. **Present the result** as a WO card:
   - WO ID · asset · priority (with `priority_reason`) · schedule-by time (`SCHEDULED_TS`)
   - Parts table with stock status. Highlight any PR and whether it is **EXPEDITE**, which means the supplier lead time exceeds the time to failure.
   - The numbered job plan (`job_plan`)
   - "Verified in ERP.WORK_ORDERS ✓"

## Best practices
- One WO per alert. The procedure is idempotent for alerts already in `WO_CREATED`.
- Times are on the data clock (`AS_OF_TS`), not the wall clock.
- After the WO, suggest `/oee-analyst` to quantify the availability protected.

## Example
User: `/work-order-automator Create the predictive work order for the gearbox alert`
→ Resolves AL-1001 (PUN-L1-GBX-01, bearing wear, CRITICAL) and previews BRG-6312-C3 ×2 (2 on hand) and GRS-LGHP2 ×1. After confirmation it creates **WO-PDM-90001**, P2 (failure predicted within 72 h on a criticality-A asset), scheduled within 24 h, with no PR needed, a 7-step job plan starting with LOTO, and verification in ERP.WORK_ORDERS.
