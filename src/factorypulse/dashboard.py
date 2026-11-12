"""儀表板資料：自動化成效、良率管制圖、良率異常事件、批號品質（進料檢驗 vs 產線）。"""
from __future__ import annotations

import json
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from . import config
from .db import (erp_incoming_inspection, erp_part, erp_vendor, mart_wo_yield, mes_material_issue,
                 mes_yield_signal, ops_anomaly)
from .mes import chart_points
from .spc import RULE_ZH
from .stats import compute


def dashboard_data(engine: Engine) -> dict:
    st = compute(engine)
    charts = {
        line: [{"date": p.key.isoformat(), "n": p.n, "ng": p.ng, "p": p.p, "center": p.center, "ucl": p.ucl,
                "rules": [RULE_ZH[r] for r in p.rules]} for p in pts]
        for line, pts in chart_points(engine).items()}
    with engine.connect() as c:
        open_anomalies = c.execute(select(func.count()).select_from(ops_anomaly)
                                   .where(ops_anomaly.c.resolved_at.is_(None))).scalar()
        sigs = c.execute(select(mes_yield_signal).order_by(mes_yield_signal.c.prod_date.desc())).all()
        prod = c.execute(
            select(mes_material_issue.c.part_no, mes_material_issue.c.lot_no,
                   func.sum(mart_wo_yield.c.units).label("units"), func.sum(mart_wo_yield.c.ng_units).label("ng"),
                   func.count().label("wos"))
            .join(mart_wo_yield, mart_wo_yield.c.wo_no == mes_material_issue.c.wo_no)
            .group_by(mes_material_issue.c.part_no, mes_material_issue.c.lot_no)).all()
        iqc = {(r.part_no, r.lot_no): r for r in c.execute(
            select(erp_incoming_inspection.c.part_no, erp_incoming_inspection.c.lot_no,
                   func.sum(erp_incoming_inspection.c.sample_qty).label("sample"),
                   func.sum(erp_incoming_inspection.c.defect_qty).label("defects"),
                   func.max(erp_incoming_inspection.c.vendor_result).label("result"))
            .group_by(erp_incoming_inspection.c.part_no, erp_incoming_inspection.c.lot_no))}
        vendor = dict(c.execute(select(erp_part.c.part_no, erp_vendor.c.vendor_name)
                                .join(erp_vendor, erp_vendor.c.vendor_code == erp_part.c.vendor_code)).all())

    incidents: dict[tuple, dict] = {}
    for s in sigs:
        key = (s.line, s.prod_date)
        if key not in incidents:
            incidents[key] = {"line": config.LINE_NAMES.get(s.line, s.line), "date": s.prod_date.isoformat(),
                              "ng_rate": float(s.ng_rate), "ucl": float(s.ucl), "conclusion": s.conclusion,
                              "suspect": f"{s.suspect_part_no} {s.suspect_lot_no}" if s.suspect_lot_no else None}
    lots = []
    for r in prod:
        i = iqc.get((r.part_no, r.lot_no))
        lots.append({"part_no": r.part_no, "lot_no": r.lot_no, "vendor": vendor.get(r.part_no),
                     "wos": r.wos, "units": int(r.units), "ng": int(r.ng), "ng_rate": int(r.ng) / int(r.units),
                     "iqc_rate": (int(i.defects) / int(i.sample)) if i and i.sample else None,
                     "iqc_result": i.result if i else None})
    lots.sort(key=lambda x: -x["ng_rate"])
    return {
        "generated": date.today().isoformat(),
        "kpi": {"mails": st.mails, "docs": st.docs, "auto_rate": st.auto_rate,
                "saved_minutes": round(st.saved_minutes, 1), "baseline_is_assumed": st.baseline_is_assumed,
                "open_anomalies": open_anomalies, "yield_incidents": len(incidents)},
        "charts": charts,
        "line_names": config.LINE_NAMES,
        "incidents": list(incidents.values())[:30],
        "lots": lots,
    }


def dashboard_json(engine: Engine) -> str:
    return json.dumps(dashboard_data(engine), ensure_ascii=False)
