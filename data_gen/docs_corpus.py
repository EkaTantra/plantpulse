"""Synthetic maintenance knowledge base (manual excerpts, SOPs, reliability notes).

All text is original and fictional - written for this prototype. Each entry:
(doc_id, title, asset_type, failure_mode, content)
"""

DOCS = [
    ("DOC-001", "Vibration severity guide for rotating machinery", "ALL", "ALL",
     "Overall vibration velocity (RMS, mm/s) is the primary health indicator for motors, pumps, fans and gearboxes. "
     "For medium machines on rigid foundations use these plant limits: below 2.8 mm/s = good, 2.8-4.5 = acceptable, "
     "4.5-7.1 = alert (plan intervention within 7 days), above 7.1 = danger (plan intervention within 24-48 hours). "
     "CNC spindles use tighter limits: alert at 2.8 mm/s and danger at 4.5 mm/s. A steady upward trend matters more than a single "
     "reading - a 50% rise over a week warrants investigation even if the absolute value is still in the acceptable band."),
    ("DOC-002", "Reading a vibration signature - what the pattern tells you", "ALL", "ALL",
     "Dominant 1x running speed vibration in the radial direction usually indicates imbalance. 2x running speed with high axial "
     "vibration points to misalignment. Broadband high-frequency energy and random spikes that grow over days point to rolling "
     "element bearing defects. Erratic, noisy vibration together with fluctuating motor current on pumps is a classic sign of cavitation. "
     "Temperature rising faster than vibration typically means a lubrication or cooling problem rather than a mechanical defect."),
    ("DOC-003", "Bearing wear - diagnosis and corrective action", "ALL", "BEARING_WEAR",
     "Symptoms: progressive rise in overall vibration (often 2-3x baseline over 5-8 days), intermittent spikes, bearing housing "
     "temperature rising 8-15 C above normal, audible grinding. Most common root causes in this plant: missed or late lubrication, "
     "grease contamination, over-greasing, and misalignment after maintenance. Corrective action: confirm with spectrum (BPFO/BPFI), "
     "plan bearing replacement at the next window, check spare BRG-6312-C3 availability, flush housing and regrease with LGHP2. "
     "Expected repair time 6-10 hours for motors and 12-16 hours for gearboxes."),
    ("DOC-004", "Lubrication failure and overheating", "ALL", "LUBRICATION_FAILURE",
     "Symptoms: temperature climbs steadily (often 20-35 C above baseline) while vibration rises only moderately. Oil appears dark, "
     "oxidised or foamy; breathers may be blocked; oil coolers may be fouled with dust. Root causes: oil change interval exceeded, wrong "
     "viscosity grade, blocked breather, cooler fouling. Action: check oil level and condition, sample oil for analysis, change oil "
     "(ISO VG 220 for gearboxes and compressors), replace filter FLT-OIL-90, clean cooler fins, verify PT100 sensor. If temperature "
     "exceeds the alarm limit, reduce load or stop - running hot destroys bearings and gears within hours."),
    ("DOC-005", "Pump cavitation troubleshooting", "Coolant Pump", "CAVITATION",
     "Symptoms: crackling or gravel-like noise at the inlet, erratic vibration, motor current dropping and fluctuating, reduced flow. "
     "Root causes: blocked suction strainer, low tank / sump level, high coolant temperature, suction valve partly closed, air ingress. "
     "Action: check and clean strainer STR-SUC-80, verify tank level switch, inspect impeller for pitting, inspect mechanical seal. "
     "Prolonged cavitation erodes the impeller (IMP-CP-150, lead time 12 days) - order early if pitting is suspected."),
    ("DOC-006", "Rotor imbalance correction", "ALL", "IMBALANCE",
     "Symptoms: vibration at 1x running speed rising roughly linearly, little temperature change, phase stable. Root causes: material "
     "build-up on fans or rotors, lost balance weights, uneven wear, thermal bow. Action: clean rotor, inspect for missing weights, "
     "perform single- or two-plane field balancing to grade G2.5 (G1.0 for spindles). Typical repair time 4-6 hours."),
    ("DOC-007", "Shaft and coupling misalignment", "ALL", "MISALIGNMENT",
     "Symptoms: 2x running speed component, high axial vibration, coupling insert wear, slight speed fluctuation and bearing temperature "
     "rise. Root causes: soft foot, loosened base bolts, thermal growth not compensated, poor alignment after maintenance. Action: check "
     "soft foot, torque base bolts, laser-align to within 0.05 mm offset, replace coupling insert CPL-INS-L100."),
    ("DOC-008", "Motor winding insulation degradation", "Main Drive Motor", "ELECTRICAL_WINDING",
     "Symptoms: rising phase current and current imbalance (>5%), winding temperature rising, hot spots in thermography, nuisance trips. "
     "Root causes: overloading, contamination, moisture, voltage imbalance, age. Action: measure insulation resistance (megger) - below "
     "1 MOhm is unacceptable; check supply voltage balance; plan motor swap with standby MTR-STBY-45KW; send failed motor for rewinding."),
    ("DOC-009", "Main drive motor - maintenance manual excerpt", "Main Drive Motor", "ALL",
     "Rated 45 kW, 1480 rpm, IE3. Normal running: vibration 1.5-2.5 mm/s, frame temperature 55-70 C, current 38-46 A at full load. "
     "Regrease DE/NDE bearings every 2000 running hours (approx. 30 days on three-shift operation) with 30 g LGHP2. Do not over-grease. "
     "Alarm limits: vibration 7.1 mm/s, temperature 95 C."),
    ("DOC-010", "Gearbox - maintenance manual excerpt", "Gearbox", "ALL",
     "Helical-bevel gearbox, output 420 rpm. Normal oil sump temperature 60-75 C. Oil: ISO VG 220 mineral gear oil, change every 4000 "
     "hours or immediately if oxidised. Check breather monthly. Input bearing 6312-C3. Alarm limits: vibration 7.1 mm/s, temperature 100 C. "
     "Gearbox bearing failures have historically caused the longest downtime on Line PUN-L1 (16 h) - treat early warnings as priority."),
    ("DOC-011", "Air compressor - maintenance manual excerpt", "Air Compressor", "ALL",
     "Oil-injected screw compressor, 3000 rpm. Normal discharge temperature 75-85 C. Change compressor oil and oil filter every 4000 hours; "
     "clean aftercooler monthly in dusty seasons. High temperature trip at 110 C. Rising temperature with stable vibration almost always "
     "indicates oil or cooling issues."),
    ("DOC-012", "Coolant pump - maintenance manual excerpt", "Coolant Pump", "ALL",
     "Centrifugal pump 2950 rpm, impeller 150 mm. Normal vibration 1.8-2.8 mm/s, current 16-20 A. Clean suction strainer weekly when "
     "machining cast iron. Maintain coolant tank above 60% level. Two cavitation events in the last quarter were traced to blocked strainers."),
    ("DOC-013", "CNC spindle - maintenance manual excerpt", "CNC Spindle", "ALL",
     "Motor spindle 12000 rpm, angular contact bearing set 7014. Normal vibration 0.8-1.6 mm/s; alert 2.8 mm/s; danger 4.5 mm/s. "
     "Spindle bearing set BRG-SPN-7014 has 14-day lead time and is NOT held in stock - raise purchase requisition as soon as degradation "
     "is detected. Run warm-up cycle after every stop longer than 2 hours."),
    ("DOC-014", "Conveyor drive - maintenance manual excerpt", "Conveyor Drive", "ALL",
     "Geared motor 90 rpm output. Normal vibration 1-2 mm/s. Check chain/belt tension weekly. Coupling insert CPL-INS-L100 is a wear "
     "item - inspect at every PM. Misalignment after base bolt loosening is the most frequent fault."),
    ("DOC-015", "SOP - Lockout/Tagout before maintenance", "ALL", "ALL",
     "Before any intervention: inform line supervisor, stop the machine via normal stop, isolate all energy sources (electrical, pneumatic, "
     "hydraulic, stored mechanical), apply personal lock and tag, verify zero energy by attempting restart, then start work. Remove locks "
     "only after all personnel are clear and guards are refitted."),
    ("DOC-016", "Work order priority policy", "ALL", "ALL",
     "P1 Emergency: safety risk or line stopped - respond within 1 hour. P2 Urgent: predicted failure within 72 hours on a criticality A/B "
     "asset - schedule within 24 hours, prefer the next planned stop. P3 Planned: degradation detected, failure not expected within 7 days "
     "- schedule within the week. P4 Opportunistic. Predictive (PdM) work orders must include the evidence (sensor trend, model risk score) "
     "and the recommended parts."),
    ("DOC-017", "OEE definitions used in this plant", "ALL", "ALL",
     "OEE = Availability x Performance x Quality. Availability = run time / planned production time. Performance = (ideal cycle time x "
     "total count) / run time. Quality = good count / total count. World-class OEE is about 85%; plant target is 80%. Breakdown minutes "
     "reduce availability; degraded equipment running slower reduces performance; vibration-induced defects reduce quality."),
    ("DOC-018", "Reliability review - Q3 lessons learned", "ALL", "ALL",
     "Twelve functional failures occurred in the review window, costing 113 breakdown hours. Every one showed measurable sensor degradation "
     "4-7 days before failure, but no alarm was raised because fixed thresholds were set at the danger level. Bearing wear and lubrication "
     "failures together account for more than half of all breakdown hours. Seven PMs were skipped for production priority; three of those "
     "assets (CHN-L2-MTR-01, PUN-L2-SPN-01, CHN-L2-PMP-01) failed within three weeks of the skipped PM. Recommendation: trend-based "
     "predictive alerts, automatic PdM work orders, and spare-part checks at the time of alert."),
    ("DOC-019", "Sensor data quality - recognising false alarms", "ALL", "ALL",
     "A reading of 24.9 mm/s is the saturation value of the vibration transmitters and is not physically plausible for these assets. "
     "Short bursts at saturation with normal temperature, current and speed indicate a loose connector or EMI, not a machine fault. "
     "Action: raise an instrumentation check (P4), do not stop the line."),
    ("DOC-020", "Spare parts and lead time policy", "ALL", "ALL",
     "Critical spares are reviewed weekly. When a predictive alert is raised, the planner must check on-hand quantity of the recommended "
     "parts; if stock is zero or below the reorder point, raise a purchase requisition immediately and expedite where lead time exceeds "
     "the predicted time-to-failure."),
]
