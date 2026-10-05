"""Renders docs/architecture.svg (1920x1080). Run: python docs/diagram_src/architecture.py"""
import os
from html import escape

W, H = 1920, 1080
NAVY, BLUE, INK, MUTED, PURPLE = "#11567F", "#29B5E8", "#1B2A3A", "#5B6B7C", "#8A5CC8"
KIND = {  # fill, stroke
    "ot": ("#E7F6FC", "#29B5E8"), "it": ("#EAF0FB", "#3D6FD1"), "un": ("#F3ECFB", PURPLE),
    "sf": ("#FFFFFF", "#9CC9DD"), "ml": ("#E9F8F1", "#2E9E6B"), "ai": ("#FFF4E5", "#E08A1E"),
    "act": ("#FDECEC", "#D0453F"), "ux": ("#EAF6FB", NAVY),
}
COLS = [  # title, x, width, boxes: (kind, title, lines, height)
    ("1 · Sources", 30, 300, [
        ("ot", "OT telemetry", ["PLC / historian, 10-min", "vibration · temperature", "RPM · motor current"], 130),
        ("it", "ERP / CMMS", ["asset master · work orders", "spare parts · failures"], 110),
        ("it", "MES production", ["shift counts · stops", "ideal cycle times"], 110),
        ("un", "Maintenance knowledge", ["OEM manuals · SOPs", "reliability reviews", "technician free-text notes"], 130),
    ]),
    ("2 · Snowflake ingest", 365, 300, [
        ("sf", "Stage + COPY INTO", ["ERP.LANDING (CSV batch)"], 90),
        ("sf", "OT stream", ["prod: Snowpipe Streaming / Kafka", "demo: SIMULATE_SENSOR_STREAM"], 110),
        ("sf", "Governed tables", ["OT.SENSOR_READINGS (207k rows)", "ERP.* · KNOWLEDGE.*"], 110),
        ("sf", "Tasks (every 15 min)", ["T_STREAM_SENSORS", "→ T_SCORE_ASSETS", "→ T_GENERATE_ALERTS"], 120),
    ]),
    ("3 · Intelligence", 700, 420, [
        ("sf", "IT/OT features", ["hourly → rolling 24 h / 72 h, trend slopes", "ASOF JOIN to last PM · V_ASSET_FEATURES"], 100),
        ("ml", "Explainable rules + Snowflake ML", ["V_RULE_HEALTH: score · failure mode · hours to alarm",
                                                    "ML CLASSIFICATION (fail ≤ 72 h) → SCORE_ASSETS"], 100),
        ("ai", "Cortex Search", ["MAINTENANCE_KB_SEARCH: manuals + tech notes"], 80),
        ("ai", "Cortex AI COMPLETE (LLM)", ["ROOT_CAUSE_BRIEF: cited RAG answer", "AI job plans for work orders"], 100),
        ("sf", "Semantic view + Cortex Analyst", ["PLANT_OPS_SV: OEE · MTTR · PM compliance · risk",
                                       "ASK_OEE: plain English → governed query"], 100),
    ]),
    ("4 · Actions", 1155, 340, [
        ("act", "GENERATE_ALERTS", ["PDM_ALERTS · risk × criticality", "CRITICAL / WARNING / INFO"], 110),
        ("act", "TRIAGE_ALERT", ["acknowledge · dismiss false alarm"], 90),
        ("act", "CREATE_PDM_WORK_ORDER", ["priority policy (DOC-016)", "parts + stock check · AI job plan", "→ ERP.WORK_ORDERS"], 130),
        ("act", "Purchase requisitions", ["stock-out → PR, EXPEDITE if", "lead time > time to failure"], 110),
    ]),
    ("5 · Experiences", 1530, 360, [
        ("ux", "CoCo CLI · 5 project skills", ["/plantpulse-setup", "/asset-health-triage", "/root-cause-investigator",
                                               "/work-order-automator", "/oee-analyst", "+ reliability-engineer subagent"], 210),
        ("ux", "Streamlit in Snowflake", ["PlantPulse Command Center:", "triage · copilot · asset health", "OEE · work orders"], 130),
        ("ux", "Snowflake Intelligence agent", ["tools: Cortex Analyst · Cortex Search", "· root-cause brief"], 90),
    ]),
]
CHIPS = [
    ("/asset-health-triage", ["SCORE_ASSETS · GENERATE_ALERTS", "TRIAGE_ALERT"], "→ alert id"),
    ("/root-cause-investigator", ["V_SENSOR_HOURLY · WORK_ORDERS", "Cortex Search · ROOT_CAUSE_BRIEF"], "→ diagnosis"),
    ("/work-order-automator", ["CREATE_PDM_WORK_ORDER · SPARE_PARTS", "PURCHASE_REQUISITIONS"], "→ WO id"),
    ("/oee-analyst", ["PLANT_OPS_SV · OEE_LOSS_TREE", "ASSET_DOWNTIME_IMPACT"], "→ OEE gain"),
]
TOP, BOTTOM = 150, 840
MONO = 'font-family="Consolas, monospace" font-weight="600"'


def text(x, y, s, size=15, fill=MUTED, extra=""):
    return f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" fill="{fill}" {extra}>{escape(s)}</text>'


def arrow(x1, y1, x2, y2, color=NAVY, marker="a"):
    return f'<path d="M{x1},{y1} L{x2},{y2}" stroke="{color}" stroke-width="3" fill="none" marker-end="url(#{marker})"/>'


def build():
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
         'font-family="Segoe UI, Helvetica, Arial, sans-serif">',
         '<defs>'
         f'<marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{NAVY}"/></marker>'
         f'<marker id="p" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{PURPLE}"/></marker>'
         '</defs>',
         f'<rect width="{W}" height="{H}" fill="#F7FAFC"/>',
         text(30, 62, "PlantPulse — Predictive Maintenance & OEE Command Center on Snowflake", 38, NAVY, 'font-weight="700"'),
         text(30, 100, "IT + OT + unstructured knowledge converge in one governed platform; "
                       "CoCo CLI skills and a Streamlit app turn predictions into actions", 20)]
    pos = {}
    for title, x, w, boxes in COLS:
        o.append(f'<rect x="{x}" y="{TOP-20}" width="{w}" height="{BOTTOM-TOP+40}" rx="16" fill="#FFFFFF" stroke="#D5E3EC" stroke-width="2"/>')
        o.append(f'<rect x="{x}" y="{TOP-20}" width="{w}" height="46" rx="16" fill="{NAVY}"/>'
                 f'<rect x="{x}" y="{TOP+6}" width="{w}" height="20" fill="{NAVY}"/>')
        o.append(text(x + 18, TOP + 12, title, 22, "#FFFFFF", 'font-weight="700"'))
        gap = (BOTTOM - TOP - 50 - sum(b[3] for b in boxes)) / max(len(boxes) - 1, 1)
        y = TOP + 45
        for kind, bt, lines, h in boxes:
            fill, stroke = KIND[kind]
            o.append(f'<rect x="{x+14}" y="{y:.0f}" width="{w-28}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
            o.append(text(x + 28, y + 28, bt, 19, INK, 'font-weight="700"'))
            for i, ln in enumerate(lines):
                mono = ln.startswith("$")
                o.append(text(x + 28, y + 52 + i * 22, ln, 16 if mono else 15, NAVY if mono else MUTED, MONO if mono else ""))
            pos[bt] = (x, x + w, y, h)
            y += h + gap
    for x1, x2 in [(330, 365), (665, 700), (1120, 1155), (1495, 1530)]:
        for yy in (330, 540, 710):
            o.append(arrow(x1 + 2, yy, x2 - 2, yy))
    # unstructured knowledge -> Cortex Search
    kx0, kx1, ky, kh = pos["Maintenance knowledge"]
    sx0, sx1, sy, sh = pos["Cortex Search"]
    # routed through the gutters: down under the columns, along the bottom gap, up between ingest and intelligence
    gx, gy, ty = 683, 876, sy + 22
    o.append(f'<path d="M{(kx0+kx1)/2},{ky+kh} L{(kx0+kx1)/2},{gy} L{gx},{gy} L{gx},{ty} L{sx0+12},{ty}" stroke="{PURPLE}" '
             f'stroke-width="3" fill="none" stroke-dasharray="9 6" marker-end="url(#p)"/>')
    o.append(text(200, gy - 6, "unstructured text → Cortex Search (vector + keyword index)", 15, PURPLE, 'font-weight="600"'))
    # skill chain band
    by = 895
    o.append(f'<rect x="30" y="{by}" width="1860" height="160" rx="16" fill="#FFFFFF" stroke="#D5E3EC" stroke-width="2"/>')
    o.append(text(50, by + 36, "Modular CoCo skills: each does one job and hands an ID to the next", 21, NAVY, 'font-weight="700"'))
    o.append(text(1870, by + 36, "/plantpulse-setup builds and verifies sql/00–09", 16, NAVY, MONO + ' text-anchor="end"'))
    cw = 430
    for i, (name, lines, hand) in enumerate(CHIPS):
        x = 50 + i * (cw + 30)
        o.append(f'<rect x="{x}" y="{by+54}" width="{cw}" height="88" rx="10" fill="#EAF6FB" stroke="{BLUE}" stroke-width="2"/>')
        o.append(text(x + 16, by + 82, name, 18, NAVY, MONO))
        o.append(text(x + cw - 16, by + 82, hand, 15, "#D0453F", 'font-weight="700" text-anchor="end"'))
        for j, ln in enumerate(lines):
            o.append(text(x + 16, by + 106 + j * 20, ln, 14))
        if i < len(CHIPS) - 1:
            o.append(arrow(x + cw + 2, by + 98, x + cw + 28, by + 98))
    o.append(text(1890, by - 12, "Legend: blue = OT · indigo = IT · purple = unstructured · green = ML · "
                                 "orange = Cortex AI · red = actions", 14, MUTED, 'text-anchor="end"'))
    o.append("</svg>")
    return "\n".join(o)


if __name__ == "__main__":
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "architecture.svg")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(build())
    print("wrote", os.path.normpath(p))
