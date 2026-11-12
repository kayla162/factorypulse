"""良率異常回查：這一天用到的每個料號批號，是不是在「其他工單」也一樣不良率偏高？

做法（排除本次再比較）：
  1. 找出異常當天、該線的工單與它們領用的批號。
  2. 對每個批號，只看「排除異常當天工單後」其他用到這個批號的工單，
     跟同料號其他批號的工單比較不良率（單尾雙比例 z 檢定）。
  3. 真的是料的問題 → 這批料在別的工單也會偏高；一天的設備異常 → 同批料在別天是正常的。
這樣可以避免「異常當天剛好用到的批號全部被冤枉」。

結論只是「疑似」：統計相關不等於因果，最後要由品保現場確認。
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.engine import Connection

from .db import (doc_document, erp_incoming_inspection, erp_part, erp_vendor, mart_wo_yield, mes_material_issue,
                 ops_anomaly)

P_THRESHOLD = 1e-3      # 顯著水準（每天會比很多批號，所以設嚴一點）
MIN_RISK_RATIO = 2.0    # 不良率至少是其他批號的 2 倍才算有意義
MIN_OTHER_UNITS = 300   # 排除當天後，這批料至少還要有這麼多片才判斷得出來


@dataclass
class LotEvidence:
    part_no: str
    lot_no: str
    units: int            # 排除本次後，用這批料的片數
    ng: int
    other_units: int      # 同料號其他批號
    other_ng: int
    risk_ratio: float | None
    p_value: float | None
    note: str = ""

    @property
    def rate(self) -> float:
        return self.ng / self.units if self.units else 0.0

    @property
    def other_rate(self) -> float:
        return self.other_ng / self.other_units if self.other_units else 0.0


@dataclass
class RootCause:
    line: str
    prod_date: date
    work_orders: list[str]
    suspect: LotEvidence | None
    conclusion: str
    vendor_name: str | None = None
    iqc: list[dict] = field(default_factory=list)          # 這批料的進料檢驗紀錄
    iqc_alerts: list[str] = field(default_factory=list)    # 進料時就已經發出的異常
    evidence: list[LotEvidence] = field(default_factory=list)

    def evidence_json(self) -> str:
        return json.dumps([dict(asdict(e), rate=e.rate, other_rate=e.other_rate) for e in self.evidence],
                          ensure_ascii=False)


def two_proportion_p(x1: int, n1: int, x2: int, n2: int) -> float | None:
    """單尾檢定 H1：第一組不良率較高。"""
    if not n1 or not n2:
        return None
    p = (x1 + x2) / (n1 + n2)
    if p in (0, 1):
        return 1.0
    z = (x1 / n1 - x2 / n2) / math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return 0.5 * math.erfc(z / math.sqrt(2))


def investigate(conn: Connection, line: str, prod_date: date) -> RootCause:
    wy = {r.wo_no: r for r in conn.execute(select(mart_wo_yield))}
    issues = conn.execute(select(mes_material_issue.c.wo_no, mes_material_issue.c.part_no,
                                 mes_material_issue.c.lot_no)).all()
    signal_wos = sorted(w for w, r in wy.items() if r.line == line and r.prod_date == prod_date)
    used = sorted({(i.part_no, i.lot_no) for i in issues if i.wo_no in signal_wos})
    wos_by_lot: dict[tuple[str, str], set[str]] = {}
    for i in issues:
        wos_by_lot.setdefault((i.part_no, i.lot_no), set()).add(i.wo_no)

    evidence = []
    for part, lot in used:
        mine = wos_by_lot[(part, lot)] - set(signal_wos)
        others = set().union(*(w for (p, l), w in wos_by_lot.items() if p == part and l != lot)) - set(signal_wos)
        others -= mine
        n1, x1 = sum(wy[w].units for w in mine if w in wy), sum(wy[w].ng_units for w in mine if w in wy)
        n2, x2 = sum(wy[w].units for w in others if w in wy), sum(wy[w].ng_units for w in others if w in wy)
        note = ""
        if n1 < MIN_OTHER_UNITS:
            note = "這批料幾乎只用在本次，無法和其他工單比較"
        rr = (x1 / n1) / (x2 / n2) if n1 and n2 and x2 else None
        evidence.append(LotEvidence(part, lot, n1, x1, n2, x2, rr, two_proportion_p(x1, n1, x2, n2), note))
    evidence.sort(key=lambda e: (e.p_value is None, e.p_value if e.p_value is not None else 1.0))

    suspects = [e for e in evidence if not e.note and e.p_value is not None and e.p_value < P_THRESHOLD
                and (e.risk_ratio is None or e.risk_ratio >= MIN_RISK_RATIO)]
    rc = RootCause(line, prod_date, signal_wos, suspects[0] if suspects else None, "", evidence=evidence)
    if rc.suspect:
        s = rc.suspect
        _attach_iqc(conn, rc)
        rc.conclusion = (f"疑似 {rc.vendor_name or ''} {s.part_no} 批號 {s.lot_no}：這批料在其他工單不良率 "
                         f"{s.rate:.1%}，同料號其他批號 {s.other_rate:.1%}（{_rr(s)}，p={s.p_value:.1e}）")
        if len(suspects) > 1:
            rc.conclusion += "；另有 " + "、".join(f"{e.part_no} {e.lot_no}" for e in suspects[1:]) + " 也偏高，可能是一起使用造成"
        if rc.iqc_alerts:
            rc.conclusion += "。進料時已有警訊：" + "、".join(rc.iqc_alerts)
    elif any(e.note for e in evidence) and not any(not e.note for e in evidence):
        rc.conclusion = "當天用到的批號都只用在這幾張工單，資料不足以判斷是否為料的問題，請現場確認"
    else:
        rc.conclusion = "當天用到的批號在其他工單良率都正常，跟批號無明顯關聯，疑似製程或設備，請現場確認"
    return rc


def _rr(e: LotEvidence) -> str:
    return f"{e.risk_ratio:.1f} 倍" if e.risk_ratio else "其他批號無不良"


def _attach_iqc(conn: Connection, rc: RootCause) -> None:
    s = rc.suspect
    v = conn.execute(select(erp_vendor.c.vendor_name).join(erp_part, erp_part.c.vendor_code == erp_vendor.c.vendor_code)
                     .where(erp_part.c.part_no == s.part_no)).scalar()
    rc.vendor_name = v
    rows = conn.execute(select(erp_incoming_inspection).where(erp_incoming_inspection.c.part_no == s.part_no,
                                                              erp_incoming_inspection.c.lot_no == s.lot_no)).all()
    for r in rows:
        rc.iqc.append(dict(document_id=r.document_id, inspected_on=r.inspected_on.isoformat() if r.inspected_on else None,
                           sample=r.sample_qty, defects=r.defect_qty, vendor_result=r.vendor_result))
        alerts = conn.execute(select(ops_anomaly.c.detail, doc_document.c.doc_no)
                              .join(doc_document, doc_document.c.id == ops_anomaly.c.document_id)
                              .where(ops_anomaly.c.document_id == r.document_id,
                                     ops_anomaly.c.part_no == s.part_no)).all()
        rc.iqc_alerts += [f"檢驗報告 {a.doc_no}「{a.detail}」" for a in alerts]
