"""MES／AOI 匯出檔 → 清洗 → 寫入 mes_* → 重算彙總表（mart_*）。

每個檔案記錄雜湊：同一個檔案不會重複匯入；同檔名內容變了（例如當天重新匯出）則以新檔取代。
清洗規則：
  可自動修正（rows_fixed）：線別寫法、日期格式、時區、批號格式
  直接丟棄（rows_duplicate）：完全重複的列
  擋下（etl_reject）：分數缺漏、線別無法辨識、時間無法解析、工單或料號不存在
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import case, delete, func, insert, select
from sqlalchemy.engine import Connection, Engine

from .db import (erp_incoming_inspection, erp_part, etl_file_log, etl_reject, mart_wo_yield, mart_yield_daily,
                 mes_aoi_result, mes_material_issue, mes_work_order)
from .normalize import normalize_lot, parse_date, to_halfwidth

TW = timezone(timedelta(hours=8))
CHUNK = 150   # MS SQL 一個語句最多 2100 個參數


@dataclass
class FileResult:
    file: str
    kind: str
    rows_in: int = 0
    loaded: int = 0
    duplicate: int = 0
    rejected: int = 0
    fixed: int = 0
    skipped: bool = False                 # 同一個檔案已經匯入過
    rejects_by_rule: dict[str, int] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)


@dataclass
class EtlResult:
    files: list[FileResult] = field(default_factory=list)

    def total(self, attr: str, kind: str | None = None) -> int:
        return sum(getattr(f, attr) for f in self.files if kind is None or f.kind == kind)

    def rejects(self, kind: str | None = None) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.files:
            if kind is None or f.kind == kind:
                for k, v in f.rejects_by_rule.items():
                    out[k] = out.get(k, 0) + v
        return out

    def warnings(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.files:
            for k, v in f.warnings.items():
                out[k] = out.get(k, 0) + v
        return out


# ---------------------------------------------------------------- 清洗函式
def normalize_line(raw: str) -> str | None:
    """A線、LINE-A、Line A、a線 → A。"""
    s = to_halfwidth(raw).upper()
    s = re.sub(r"LINE|線|[\s\-_]", "", s)
    return s if re.fullmatch(r"[A-Z]", s) else None


def parse_timestamp(raw: str) -> datetime | None:
    """ISO（含 +08:00）或 2026/10/28 08:12:33 → 台灣時間（不帶時區）。"""
    s = to_halfwidth(raw)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            t = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if t.tzinfo:
            t = t.astimezone(TW).replace(tzinfo=None)
        return t
    return None


def _decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(to_halfwidth(raw)) if raw.strip() else None
    except InvalidOperation:
        return None


# ---------------------------------------------------------------- 主流程
def run_etl(engine: Engine, folder: Path, run_id: str) -> EtlResult:
    """依相依順序匯入：工單 → 領料 → AOI，最後重算彙總表。"""
    result = EtlResult()
    order = [("work_order", sorted(folder.glob("work_orders*.csv"))),
             ("material_issue", sorted(folder.glob("material_issues*.csv"))),
             ("aoi", sorted(folder.glob("aoi_results_*.csv")))]
    for kind, files in order:
        for path in files:
            result.files.append(_load_file(engine, path, kind, run_id))
    if any(not f.skipped for f in result.files):
        rebuild_marts(engine)
    return result


def _load_file(engine: Engine, path: Path, kind: str, run_id: str) -> FileResult:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    res = FileResult(path.name, kind)
    with engine.connect() as c:
        if c.execute(select(etl_file_log.c.id).where(etl_file_log.c.file_sha256 == sha)).first():
            res.skipped = True
            return res
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    res.rows_in = len(rows)
    rejects: list[dict] = []

    def reject(i: int, rule: str, detail: str, row: dict):
        res.rejected += 1
        res.rejects_by_rule[rule] = res.rejects_by_rule.get(rule, 0) + 1
        rejects.append(dict(run_id=run_id, file_name=path.name, row_no=i, rule=rule, detail=detail[:200],
                            raw=json.dumps(row, ensure_ascii=False)))

    with engine.begin() as conn:
        clean = {"work_order": _clean_work_orders, "material_issue": _clean_issues, "aoi": _clean_aoi}[kind](
            conn, rows, res, reject, path.name)
        _replace(conn, kind, clean, path.name)
        res.loaded = len(clean)
        _bulk(conn, etl_reject, rejects)
        conn.execute(insert(etl_file_log).values(
            run_id=run_id, file_name=path.name, file_sha256=sha, kind=kind, rows_in=res.rows_in,
            rows_loaded=res.loaded, rows_duplicate=res.duplicate, rows_rejected=res.rejected,
            rows_fixed=res.fixed, loaded_at=datetime.now()))
    return res


def _dedupe(rows: list[dict], res: FileResult) -> list[tuple[int, dict]]:
    seen, out = set(), []
    for i, r in enumerate(rows, 2):           # 第 1 列是表頭
        key = tuple(r.values())
        if key in seen:
            res.duplicate += 1
            continue
        seen.add(key)
        out.append((i, r))
    return out


def _clean_work_orders(conn, rows, res, reject, name):
    out = {}
    for i, r in _dedupe(rows, res):
        line = normalize_line(r["line"])
        d = parse_date(r["prod_date"])
        if line is None:
            reject(i, "BAD_LINE", f"線別無法辨識：{r['line']}", r)
            continue
        if d is None:
            reject(i, "BAD_DATE", f"日期無法解析：{r['prod_date']}", r)
            continue
        res.fixed += (r["line"] != f"{line}線") or ("/" in r["prod_date"])
        wo = to_halfwidth(r["wo_no"]).upper()
        if wo in out:
            reject(i, "WO_CONFLICT", f"工單 {wo} 在同一檔案出現兩次且內容不同", r)
            continue
        out[wo] = dict(wo_no=wo, product_no=r["product_no"].strip(), line=line, prod_date=d,
                       planned_qty=int(r["planned_qty"]), source_file=name, loaded_at=datetime.now())
    return list(out.values())


def _clean_issues(conn, rows, res, reject, name):
    wos = set(conn.execute(select(mes_work_order.c.wo_no)).scalars())
    parts = set(conn.execute(select(erp_part.c.part_no)).scalars())
    iqc = set(conn.execute(select(erp_incoming_inspection.c.part_no, erp_incoming_inspection.c.lot_no)).all())
    out = {}
    for i, r in _dedupe(rows, res):
        wo, part = to_halfwidth(r["wo_no"]).upper(), to_halfwidth(r["part_no"]).upper()
        lot = normalize_lot(r["lot_no"])
        if wo not in wos:
            reject(i, "UNKNOWN_WO", f"工單 {wo} 不存在", r)
            continue
        if part not in parts:
            reject(i, "UNKNOWN_PART", f"料號 {part} 不在料號主檔", r)
            continue
        res.fixed += lot != r["lot_no"]
        if (part, lot) not in iqc:
            res.warnings["LOT_WITHOUT_IQC"] = res.warnings.get("LOT_WITHOUT_IQC", 0) + 1
        key = (wo, part, lot)
        if key in out:
            out[key]["issued_qty"] += int(r["issued_qty"])   # 同一批分兩次領，合併
        else:
            out[key] = dict(wo_no=wo, part_no=part, lot_no=lot, issued_qty=int(r["issued_qty"]), source_file=name)
    return list(out.values())


def _clean_aoi(conn, rows, res, reject, name):
    wos = set(conn.execute(select(mes_work_order.c.wo_no)).scalars())
    out = []
    for i, r in _dedupe(rows, res):
        score, thr = _decimal(r["anomaly_score"]), _decimal(r["threshold"])
        line, ts = normalize_line(r["line"]), parse_timestamp(r["inspected_at"])
        wo = to_halfwidth(r["work_order"]).upper()
        if score is None:
            reject(i, "MISSING_SCORE", f"{r['unit_sn']} 沒有分數（影像讀取失敗？），需重拍", r)
            continue
        if line is None:
            reject(i, "BAD_LINE", f"線別無法辨識：{r['line']}", r)
            continue
        if ts is None:
            reject(i, "BAD_TIME", f"時間無法解析：{r['inspected_at']}", r)
            continue
        if wo not in wos:
            reject(i, "UNKNOWN_WO", f"工單 {wo} 不存在", r)
            continue
        decision = r["decision"].strip().upper()
        if decision not in ("OK", "NG"):
            reject(i, "BAD_DECISION", f"判定值無法辨識：{r['decision']}", r)
            continue
        res.fixed += (r["line"] != f"{line}線") or ("+" not in r["inspected_at"])
        out.append(dict(unit_sn=r["unit_sn"].strip(), inspected_at=ts, line=line, station=r["station"].strip(),
                        wo_no=wo, score=score, threshold=thr if thr is not None else Decimal("0"),
                        decision=decision, model_version=r.get("model_version"), source_file=name))
    return out


def _replace(conn: Connection, kind: str, rows: list[dict], name: str) -> None:
    """同檔名重新匯入時，先刪掉舊資料再寫入；工單與領料以工單號為單位覆蓋。"""
    if kind == "aoi":
        conn.execute(delete(mes_aoi_result).where(mes_aoi_result.c.source_file == name))
        _bulk(conn, mes_aoi_result, rows)
    elif kind == "material_issue":
        for chunk in _chunks(sorted({r["wo_no"] for r in rows}), 500):
            conn.execute(delete(mes_material_issue).where(mes_material_issue.c.wo_no.in_(chunk)))
        _bulk(conn, mes_material_issue, rows)
    else:
        existing = set(conn.execute(select(mes_work_order.c.wo_no)).scalars())
        new = [r for r in rows if r["wo_no"] not in existing]
        for r in rows:
            if r["wo_no"] in existing:
                conn.execute(mes_work_order.update().where(mes_work_order.c.wo_no == r["wo_no"]).values(**r))
        _bulk(conn, mes_work_order, new)


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _bulk(conn: Connection, table, rows: list[dict]) -> None:
    if not rows:
        return
    n = max(1, min(CHUNK, 2000 // len(rows[0])))
    for chunk in _chunks(rows, n):
        conn.execute(insert(table).values(chunk))


# ---------------------------------------------------------------- 彙總表
def rebuild_marts(engine: Engine) -> None:
    """每片只算最後一次判定（AOI NG 送複判後改判 OK 的，以複判為準）。用視窗函數在資料庫裡算。"""
    a = mes_aoi_result
    ranked = select(a.c.unit_sn, a.c.wo_no, a.c.decision,
                    func.count().over(partition_by=a.c.unit_sn).label("times"),
                    func.row_number().over(partition_by=a.c.unit_sn,
                                           order_by=a.c.inspected_at.desc()).label("rn")).subquery()
    final = select(ranked.c.wo_no,
                   func.count().label("units"),
                   func.sum(case((ranked.c.decision == "NG", 1), else_=0)).label("ng"),
                   func.sum(case((ranked.c.times > 1, 1), else_=0)).label("rechecked")
                   ).where(ranked.c.rn == 1).group_by(ranked.c.wo_no).subquery()
    w = mes_work_order
    with engine.begin() as conn:
        rows = conn.execute(select(w.c.wo_no, w.c.line, w.c.product_no, w.c.prod_date,
                                   final.c.units, final.c.ng, final.c.rechecked)
                            .join(final, final.c.wo_no == w.c.wo_no)).all()
        conn.execute(delete(mart_wo_yield))
        conn.execute(delete(mart_yield_daily))
        _bulk(conn, mart_wo_yield, [dict(wo_no=r.wo_no, line=r.line, product_no=r.product_no, prod_date=r.prod_date,
                                         units=int(r.units), ng_units=int(r.ng), rechecked_units=int(r.rechecked))
                                    for r in rows])
        daily: dict[tuple[date, str], list[int]] = {}
        for r in rows:
            d = daily.setdefault((r.prod_date, r.line), [0, 0])
            d[0] += int(r.units)
            d[1] += int(r.ng)
        _bulk(conn, mart_yield_daily, [
            dict(prod_date=d, line=l, units=n, ng_units=ng, yield_rate=Decimal(1 - ng / n).quantize(Decimal("0.00001")))
            for (d, l), (n, ng) in sorted(daily.items())])
