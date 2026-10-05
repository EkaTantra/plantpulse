# PlantPulse model metrics

Captured from Snowflake after training. Rebuild with `sql/03_ml_model.sql` (production model) and `sql/03b_ml_validation.sql` (out-of-time validation).

## Failure-risk classifier (`ANALYTICS.FAILURE_RISK_MODEL`, SNOWFLAKE.ML.CLASSIFICATION)

- **Label:** functional failure within the next 72 hours (`FAIL` / `OK`).
- **Features:** 13 hourly condition features (vibration and temperature ratios versus the learned baseline, slopes, current, RPM stability, PM overdue days, asset type and criticality).
- **Training rows:** 32,350 asset-hours, of which 857 are labelled `FAIL`. Hours just after a breakdown and the last 72 h, where the label is not yet known, are excluded.
- **Final risk score:** 60% ML probability + 40% explainable rule score.

### Global evaluation metrics
| DATASET_TYPE   | AVERAGE_TYPE   | ERROR_METRIC   |   METRIC_VALUE |
|:---------------|:---------------|:---------------|---------------:|
| EVAL           | macro          | precision      |         0.9908 |
| EVAL           | macro          | recall         |         0.9967 |
| EVAL           | macro          | f1             |         0.9937 |
| EVAL           | macro          | auc            |         1.0000 |
| EVAL           | weighted       | precision      |         0.9994 |
| EVAL           | weighted       | recall         |         0.9994 |
| EVAL           | weighted       | f1             |         0.9994 |
| EVAL           | weighted       | auc            |         1.0000 |
| EVAL           |                | log_loss       |         0.0020 |

### Per-class evaluation metrics
| DATASET_TYPE   | CLASS   | ERROR_METRIC   |   METRIC_VALUE |
|:---------------|:--------|:---------------|---------------:|
| EVAL           | FAIL    | precision      |         0.9818 |
| EVAL           | FAIL    | recall         |         0.9939 |
| EVAL           | FAIL    | f1             |         0.9878 |
| EVAL           | FAIL    | support        |       163.0000 |
| EVAL           | OK      | precision      |         0.9998 |
| EVAL           | OK      | recall         |         0.9995 |
| EVAL           | OK      | f1             |         0.9997 |
| EVAL           | OK      | support        |      6307.0000 |

### Feature importance
|   RANK | FEATURE            |   SCORE |
|-------:|:-------------------|--------:|
|      1 | RPM_CV             |  0.1429 |
|      2 | CUR_VOLATILITY     |  0.1272 |
|      3 | VIB_ALARM_RATIO    |  0.1111 |
|      4 | CUR_RATIO          |  0.0881 |
|      5 | VIB_CV             |  0.0858 |
|      6 | VIB_RATIO          |  0.0834 |
|      7 | TEMP_MARGIN        |  0.0804 |
|      8 | TEMP_SLOPE_PER_DAY |  0.0794 |
|      9 | TEMP_DELTA         |  0.0688 |
|     10 | PM_OVERDUE_DAYS    |  0.0688 |
|     11 | VIB_SLOPE_PER_DAY  |  0.0521 |
|     12 | CRITICALITY        |  0.0120 |
|     13 | ASSET_TYPE         |  0.0000 |

> **Caveat:** Snowflake ML evaluates on a random hold-out split. Adjacent hours of the same degradation episode can therefore appear in both the training and evaluation sets, which makes these figures optimistic. The event-level back-test below is the more honest measure. The data is synthetic, so production results will vary.

## Out-of-time validation (`ANALYTICS.FAILURE_RISK_MODEL_OOT`, `sql/03b_ml_validation.sql`)

The random hold-out above lets the model see neighbouring hours of the same degradation episode during training. To measure how it does on failures it has never seen, a second classifier with the same 13 features and the same label is trained only on the past and scored on the future. The production `FAILURE_RISK_MODEL` and `SCORE_ASSETS` are unchanged.

- **Cutoff:** 2026-09-15 00:00. That is midnight after the 8th of 12 failures (FL-0008, 2026-09-14 15:00), so 2/3 of the failures are in training. The cutoff is computed from `ERP.FAILURE_EVENTS` and stored in `ANALYTICS.ML_OOT_CUTOFF`.
- **No look-ahead in training:** a pre-cutoff hour is kept only if its 72 h label window closed before the cutoff, or if it is a `FAIL` hour for a failure that had already happened. Hours whose label depends on a later failure are dropped. That leaves 21,116 training rows, 572 of them `FAIL`.
- **Test:** every asset-hour from the cutoff onward, covering 4 held-out failures (FL-0009 to FL-0012). Hourly metrics use the 9,602 labelled hours (264 `FAIL`), with the same exclusions as training. Results are stored in `ANALYTICS.ML_VALIDATION`, with per-event results in `ANALYTICS.V_ML_OOT_EVENTS`.

### Hourly metrics (held-out period, class FAIL)
| Threshold | TP | FP | FN | TN | Precision | Recall | F1 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.3 | 253 | 36 | 11 | 9,302 | 0.8754 | 0.9583 | 0.9150 |
| **0.5** | **252** | **34** | **12** | **9,304** | **0.8811** | **0.9545** | **0.9164** |
| 0.7 | 240 | 30 | 24 | 9,308 | 0.8889 | 0.9091 | 0.8989 |

ROC AUC is **0.9993** (rank-based) and PR-AUC (average precision) is **0.976**, against a base rate of 2.7% `FAIL` hours.

### Event-level metrics (P(FAIL) ≥ 0.5)
A held-out failure counts as detected when P(FAIL) first reaches 0.5 or more in the 8 days before it. Only hours from the cutoff onward are searched.

| FAILURE_ID | ASSET_ID | FAILURE_MODE | FAILURE_TS | FIRST P ≥ 0.5 | LEAD_TIME_HOURS | Note |
|:--|:--|:--|:--|:--|--:|:--|
| FL-0009 | CHN-L2-PMP-01 | CAVITATION | 2026-09-17 02:20 | 2026-09-15 00:00 | 50 | Flagged in the first scored hour. The lead time is capped by the cutoff (the search window is only 50 h). |
| FL-0010 | PUN-L2-SPN-01 | BEARING_WEAR | 2026-09-21 19:40 | 2026-09-17 12:00 | 103 | Flagged before the 72 h label window opened |
| FL-0011 | CHN-L1-GBX-01 | MISALIGNMENT | 2026-09-25 08:10 | 2026-09-22 07:00 | 73 | |
| FL-0012 | PUN-L1-CMP-01 | IMBALANCE | 2026-09-28 13:30 | 2026-09-26 01:00 | 60 | |

- Held-out failures detected in advance: **4 / 4**. Median lead time **66.5 h** (range 50–103 h).
- **False-alarm hours:** 34 hours had P ≥ 0.5 outside a 72 h pre-failure window and outside the post-failure recovery window (failure + 30 h + downtime), on 2 assets over 3 asset-days. All 34 fall 72 h to 8 days before a real failure on the same asset: 32 h on PUN-L2-SPN-01 before FL-0010 and 2 h on CHN-L1-GBX-01 before FL-0011. They are early warnings rather than spurious alarms, but the strict hourly label counts them as false positives. No healthy asset was flagged during the labelled held-out period. This includes the sensor glitch on CHN-L1-MTR-01.
- In the last 72 h of telemetry, where outcomes are not yet known, the OOT model flags 3 assets: CHN-L2-PMP-01, PUN-L1-GBX-01 and PUN-L2-CMP-01. These are the same 3 assets the production model and rule engine flag. They are not counted as false alarms.

**How to read this.** Hourly F1 drops from 0.99 on the random hold-out to 0.92 out-of-time. Precision falls from 0.98 to 0.88, mostly because of warnings raised earlier than the 72 h label window. Event detection holds at 4/4. These figures are still high, and that is a property of the synthetic data: each failure has a clean multi-day degradation signature. Also keep in mind:

- The test set has only 4 events, so the uncertainty is wide.
- All 4 held-out failure modes also appear in training.
- The per-asset healthy baselines behind the ratio features (`V_ASSET_BASELINE`, a median over all running hours) are computed over the full period. That is a small, known look-ahead.

Expect lower numbers on real plant data.

## Event-level back-test (`ANALYTICS.V_BACKTEST`, explainable rule engine)

A failure counts as flagged when the rule score first reaches 30 or more (MEDIUM) in the 8 days before it.

| FAILURE_ID   | ASSET_ID      | FAILURE_MODE        | FAILURE_TS       |   DOWNTIME_HOURS |   LEAD_TIME_HOURS | PREDICTED_MODE      | MODE_CORRECT   |
|:-------------|:--------------|:--------------------|:-----------------|-----------------:|------------------:|:--------------------|:---------------|
| FL-0001      | PUN-L1-MTR-01 | BEARING_WEAR        | 2026-08-19 14:20 |           9.5000 |                59 | BEARING_WEAR        | True           |
| FL-0002      | PUN-L1-PMP-01 | CAVITATION          | 2026-08-24 03:40 |           5.0000 |                23 | CAVITATION          | True           |
| FL-0003      | PUN-L2-GBX-01 | LUBRICATION_FAILURE | 2026-08-28 22:10 |          14.0000 |                49 | LUBRICATION_FAILURE | True           |
| FL-0004      | CHN-L1-SPN-01 | IMBALANCE           | 2026-08-31 09:00 |           6.5000 |                61 | IMBALANCE           | True           |
| FL-0005      | CHN-L2-MTR-01 | ELECTRICAL_WINDING  | 2026-09-03 17:30 |          18.0000 |                36 | ELECTRICAL_WINDING  | True           |
| FL-0006      | PUN-L2-CNV-01 | MISALIGNMENT        | 2026-09-07 11:50 |           4.0000 |                47 | MISALIGNMENT        | True           |
| FL-0007      | CHN-L1-CMP-01 | LUBRICATION_FAILURE | 2026-09-11 06:30 |          11.0000 |                56 | LUBRICATION_FAILURE | True           |
| FL-0008      | PUN-L1-GBX-01 | BEARING_WEAR        | 2026-09-14 15:00 |          16.0000 |                65 | BEARING_WEAR        | True           |
| FL-0009      | CHN-L2-PMP-01 | CAVITATION          | 2026-09-17 02:20 |           4.5000 |                23 | CAVITATION          | True           |
| FL-0010      | PUN-L2-SPN-01 | BEARING_WEAR        | 2026-09-21 19:40 |          12.0000 |                69 | BEARING_WEAR        | True           |
| FL-0011      | CHN-L1-GBX-01 | MISALIGNMENT        | 2026-09-25 08:10 |           7.0000 |                38 | MISALIGNMENT        | True           |
| FL-0012      | PUN-L1-CMP-01 | IMBALANCE           | 2026-09-28 13:30 |           5.5000 |                39 | IMBALANCE           | True           |

- Detected in advance: **12 / 12**
- Median lead time: **48 h** (range 23–69 h)
- Failure mode correctly identified: **12 / 12**
- False alarms (score ≥ 30 outside degradation windows): **0**, checked by `tests/test_rules.py`
- Sensor glitch (CHN-L1-MTR-01, transmitter saturated at 24.9 mm/s) is classified as `SENSOR_FAULT` with LOW risk
