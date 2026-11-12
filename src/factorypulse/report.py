"""Excel 日報（openpyxl）：給主管看的格式，打開就能讀，不用再整理。

工作表：摘要｜異常待處理｜待人工確認｜採購單到貨｜信件分類｜執行紀錄
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from . import config
from .classify import UNCLASSIFIED
from .db import (doc_document, erp_goods_receipt, erp_part, erp_purchase_order, erp_vendor, mail_message,
                 mes_yield_signal, ops_anomaly, ops_run_log)
from .notify import DOC_TYPE_ZH
from .stats import Stats, compute

FONT = "微軟正黑體"
HEAD_FILL = PatternFill("solid", fgColor="1F3864")
RED = PatternFill("solid", fgColor="F8D7DA")
YELLOW = PatternFill("solid", fgColor="FFF3CD")
GREEN = PatternFill("solid", fgColor="D4EDDA")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
STATUS_ZH = {"posted": "已入帳", "posted_with_alerts": "已入帳（有異常）", "needs_review": "待人工確認",
             "needs_ocr": "需 OCR", "failed": "解析失敗"}


def build_daily_report(engine: Engine, day: date | None = None, path: Path | None = None) -> Path:
    day = day or date.today()
    path = path or config.REPORTS_DIR / f"FactoryPulse日報_{day:%Y%m%d}.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    st = compute(engine, day)
    wb = Workbook()
    _summary(wb.active, st, day)
    _anomalies(wb.create_sheet("異常待處理"), engine)
    _manual(wb.create_sheet("待人工確認"), engine, day)
    _po_status(wb.create_sheet("採購單到貨"), engine, day)
    _yield(wb.create_sheet("產線良率"), engine)
    _mails(wb.create_sheet("信件分類"), engine, day)
    _runs(wb.create_sheet("執行紀錄"), engine, day)
    wb.save(path)
    return path


# ---------------------------------------------------------------- 共用格式
def _table(ws, start_row: int, headers: list[str], rows: list[list], widths: list[int]) -> int:
    for c, h in enumerate(headers, 1):
        cell = ws.cell(start_row, c, h)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF")
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
    for r, row in enumerate(rows, start_row + 1):
        for c, v in enumerate(row, 1):
            cell = ws.cell(r, c, v)
            cell.font = Font(name=FONT)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=isinstance(v, str) and len(v) > 20)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    end = start_row + max(len(rows), 1)
    ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(headers))}{end}"
    ws.freeze_panes = ws.cell(start_row + 1, 1)
    if not rows:
        ws.cell(start_row + 1, 1, "（無）").font = Font(name=FONT, color="808080")
    return end


def _title(ws, text: str, sub: str) -> None:
    ws["A1"] = text
    ws["A1"].font = Font(name=FONT, bold=True, size=16, color="1F3864")
    ws["A2"] = sub
    ws["A2"].font = Font(name=FONT, color="808080")


# ---------------------------------------------------------------- 各工作表
def _summary(ws, st: Stats, day: date) -> None:
    ws.title = "摘要"
    _title(ws, f"FactoryPulse 進料自動化日報　{day:%Y/%m/%d}",
           f"產生時間 {datetime.now():%Y-%m-%d %H:%M}　資料範圍：當日處理的信件與文件")
    pct = lambda v: v if v is not None else "—"  # noqa: E731
    kpis = [
        ["收到信件", st.mails, "封"],
        ["　規則分類", st.mails_by_method.get("rule", 0), "封"],
        ["　LLM 分類", st.mails_by_method.get("llm", 0), "封"],
        ["　待人工分類", st.mails_by_label.get(UNCLASSIFIED, 0), "封"],
        ["處理文件", st.docs, "份"],
        ["自動入帳率", pct(st.auto_rate), ""],
        ["文件人工介入率", pct(st.doc_intervention_rate), ""],
        ["解析失敗率", pct(st.failure_rate), ""],
        ["異常提醒", sum(st.anomalies_by_code.values()), "筆"],
        ["全人工處理時間", round(st.manual_minutes, 1), "分鐘"],
        ["導入後仍需人工", round(st.remaining_minutes, 1), "分鐘"],
        ["省下時間", round(st.saved_minutes, 1), "分鐘"],
    ]
    end = _table(ws, 4, ["指標", "數值", "單位"], kpis, [22, 14, 8])
    for r in range(5, end + 1):
        if ws.cell(r, 1).value and "率" in ws.cell(r, 1).value:
            ws.cell(r, 2).number_format = "0.0%"
    saved = ws.cell(end, 2)
    saved.font = Font(name=FONT, bold=True, color="155724")
    saved.fill = GREEN
    ws.auto_filter.ref = None

    # 人工時間基準：讓主管知道省時是怎麼算的
    r = end + 2
    ws.cell(r, 1, "人工時間基準（省時計算依據）").font = Font(name=FONT, bold=True)
    rows = [[k, v[0], v[1], v[2]] for k, v in st.baseline.items()]
    _table(ws, r + 1, ["項目", "分鐘", "內容", "來源"], rows, [22, 14, 30, 22])
    ws.auto_filter.ref = None
    ws.freeze_panes = None
    if st.baseline_is_assumed:
        note = ws.cell(r + 2 + len(rows), 1, "※ 目前是假設值，導入時請以現場計時結果更新：python -m factorypulse baseline")
        note.font = Font(name=FONT, color="C00000")

    # 異常種類長條圖
    if st.anomalies_by_code:
        r0 = 4
        ws.cell(r0, 6, "異常種類").font = Font(name=FONT, bold=True)
        ws.cell(r0, 7, "筆數").font = Font(name=FONT, bold=True)
        for i, (code, n) in enumerate(sorted(st.anomalies_by_code.items(), key=lambda x: -x[1]), 1):
            ws.cell(r0 + i, 6, config.ANOMALY_RULES[code][0])
            ws.cell(r0 + i, 7, n)
        ws.column_dimensions["F"].width = 20
        chart = BarChart()
        chart.type = "bar"
        chart.title = "今日異常種類"
        chart.legend = None
        n = len(st.anomalies_by_code)
        chart.add_data(Reference(ws, min_col=7, min_row=r0, max_row=r0 + n), titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=6, min_row=r0 + 1, max_row=r0 + n))
        chart.height, chart.width = 7, 14
        ws.add_chart(chart, "I4")


def _anomalies(ws, engine: Engine) -> None:
    _title(ws, "異常待處理", "所有尚未結案的異常；紅色＝文件被擋下未入帳，黃色＝已入帳但需處理")
    with engine.connect() as c:
        rows = c.execute(
            select(ops_anomaly, doc_document.c.doc_type, doc_document.c.doc_no, doc_document.c.po_no,
                   erp_vendor.c.vendor_name)
            .join(doc_document, doc_document.c.id == ops_anomaly.c.document_id)
            .join(erp_vendor, erp_vendor.c.vendor_code == doc_document.c.vendor_code, isouter=True)
            .where(ops_anomaly.c.resolved_at.is_(None))
            .order_by(ops_anomaly.c.severity.desc(), ops_anomaly.c.created_at)).all()
    data = [[r.created_at, "未入帳" if r.severity == "review" else "已入帳", config.ANOMALY_RULES[r.code][0],
             r.vendor_name, DOC_TYPE_ZH.get(r.doc_type, r.doc_type), r.doc_no, r.po_no, r.part_no, r.detail,
             config.OWNERS.get(r.owner, r.owner)] for r in rows]
    end = _table(ws, 4, ["發生時間", "入帳", "異常", "供應商", "單據", "單號", "採購單", "料號", "說明", "負責"],
                 data, [17, 9, 18, 26, 10, 13, 13, 13, 60, 16])
    for r in range(5, end + 1):
        ws.cell(r, 1).number_format = "yyyy-mm-dd hh:mm"
    rng = f"A5:J{end}"
    ws.conditional_formatting.add(rng, FormulaRule(formula=['$B5="未入帳"'], fill=RED))
    ws.conditional_formatting.add(rng, FormulaRule(formula=['$B5="已入帳"'], fill=YELLOW))


def _manual(ws, engine: Engine, day: date) -> None:
    _title(ws, "待人工確認", "系統沒有把握、交給人處理的文件與信件")
    start = datetime.combine(day, datetime.min.time())
    with engine.connect() as c:
        docs = c.execute(
            select(doc_document, erp_vendor.c.vendor_name)
            .join(erp_vendor, erp_vendor.c.vendor_code == doc_document.c.vendor_code, isouter=True)
            .where(doc_document.c.status.in_(("needs_review", "needs_ocr", "failed")),
                   doc_document.c.created_at >= start)).all()
        reasons = {}
        for a in c.execute(select(ops_anomaly.c.document_id, ops_anomaly.c.detail)
                           .where(ops_anomaly.c.severity == "review")):
            reasons.setdefault(a.document_id, []).append(a.detail)
        mails = c.execute(select(mail_message).where(mail_message.c.label == UNCLASSIFIED,
                                                     mail_message.c.created_at >= start)).all()
    data = [["文件", STATUS_ZH.get(d.status, d.status), d.vendor_name, DOC_TYPE_ZH.get(d.doc_type, d.doc_type or ""),
             d.doc_no or d.attachment_name, "；".join(reasons.get(d.id, [])) or d.error or ""] for d in docs]
    data += [["信件", "待人工分類", m.sender, "", m.subject, m.reason] for m in mails]
    _table(ws, 4, ["種類", "狀態", "供應商／寄件者", "單據", "單號／主旨", "原因"], data, [8, 14, 30, 10, 30, 70])


def _po_status(ws, engine: Engine, day: date) -> None:
    _title(ws, "採購單到貨", f"以 {day:%Y/%m/%d} 為基準判斷是否逾期")
    with engine.connect() as c:
        recv = {(r.po_no, r.po_line): (int(r.q), r.last) for r in c.execute(
            select(erp_goods_receipt.c.po_no, erp_goods_receipt.c.po_line,
                   func.sum(erp_goods_receipt.c.qty).label("q"), func.max(erp_goods_receipt.c.receipt_date).label("last"))
            .group_by(erp_goods_receipt.c.po_no, erp_goods_receipt.c.po_line))}
        pos = c.execute(select(erp_purchase_order, erp_vendor.c.vendor_name, erp_part.c.name)
                        .join(erp_vendor, erp_vendor.c.vendor_code == erp_purchase_order.c.vendor_code)
                        .join(erp_part, erp_part.c.part_no == erp_purchase_order.c.part_no)
                        .order_by(erp_purchase_order.c.due_date, erp_purchase_order.c.po_no)).all()
    data = []
    for p in pos:
        got, last = recv.get((p.po_no, p.line), (0, None))
        open_qty = p.qty - got
        if open_qty <= 0:
            status = "已結案"
        elif p.due_date < day:
            status = "逾期未交"
        else:
            status = "部分到貨" if got else "未到貨"
        late = (day - p.due_date).days if status == "逾期未交" else 0
        data.append([p.po_no, p.line, p.vendor_name, p.part_no, p.name, p.qty, got, open_qty,
                     p.due_date, last, status, late])
    end = _table(ws, 4, ["採購單", "項", "供應商", "料號", "品名", "採購量", "已收", "未交", "交期", "最後到貨",
                         "狀態", "逾期天數"], data, [13, 5, 26, 13, 16, 10, 10, 10, 12, 12, 11, 9])
    for r in range(5, end + 1):
        for col in (6, 7, 8):
            ws.cell(r, col).number_format = "#,##0"
        for col in (9, 10):
            ws.cell(r, col).number_format = "yyyy-mm-dd"
    ws.conditional_formatting.add(f"A5:L{end}", FormulaRule(formula=['$K5="逾期未交"'], fill=RED))
    ws.conditional_formatting.add(f"A5:L{end}", FormulaRule(formula=['$K5="已結案"'], fill=GREEN))


def _yield(ws, engine: Engine) -> None:
    from .mes import chart_points
    from .spc import RULE_ZH
    _title(ws, "產線良率（AOI p-chart）", "每片取最後一次判定；紅色＝管制圖失控，已自動回查批號")
    with engine.connect() as c:
        sig = {(s.line, s.prod_date): s for s in c.execute(select(mes_yield_signal).order_by(mes_yield_signal.c.id))}
    data = []
    for line, pts in sorted(chart_points(engine).items()):
        for p in pts:
            s = sig.get((line, p.key))
            data.append([p.key, config.LINE_NAMES.get(line, line), p.n, p.ng, p.p, p.center, p.ucl,
                         "、".join(RULE_ZH[r] for r in p.rules) or "正常",
                         f"{s.suspect_part_no} {s.suspect_lot_no}" if s and s.suspect_lot_no else "",
                         s.conclusion if s else ""])
    end = _table(ws, 4, ["日期", "產線", "檢驗片數", "NG 片數", "不良率", "中心線", "管制上限", "判定", "疑似批號",
                         "回查結論"], data, [12, 7, 10, 9, 9, 9, 10, 26, 22, 80])
    for r in range(5, end + 1):
        ws.cell(r, 1).number_format = "yyyy-mm-dd"
        for col in (5, 6, 7):
            ws.cell(r, col).number_format = "0.00%"
    ws.conditional_formatting.add(f"A5:J{end}", FormulaRule(formula=['$H5<>"正常"'], fill=RED))
    if data:
        chart = LineChart()
        chart.title = "AOI 不良率 vs 管制上限"
        chart.y_axis.number_format = "0%"
        chart.height, chart.width = 8, 22
        for col in (5, 7):
            chart.add_data(Reference(ws, min_col=col, min_row=4, max_row=end), titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=5, max_row=end))
        ws.add_chart(chart, "L4")


def _mails(ws, engine: Engine, day: date) -> None:
    _title(ws, "信件分類", "規則＝依附件單據判斷；LLM＝交給模型判斷（含信心）")
    start = datetime.combine(day, datetime.min.time())
    with engine.connect() as c:
        rows = c.execute(select(mail_message).where(mail_message.c.created_at >= start)
                         .order_by(mail_message.c.sent_at)).all()
    method_zh = {"rule": "規則", "llm": "LLM", "none": "—", "manual": "人工"}
    data = []
    for m in rows:
        ext = json.loads(m.extracted or "{}")
        data.append([m.sent_at, m.sender, m.subject, m.label, method_zh.get(m.method, m.method),
                     float(m.confidence) if m.confidence is not None else None,
                     ext.get("summary") or m.reason])
    end = _table(ws, 4, ["寄件時間", "寄件者", "主旨", "分類", "方法", "信心", "摘要／依據"],
                 data, [17, 34, 36, 11, 7, 7, 60])
    for r in range(5, end + 1):
        ws.cell(r, 1).number_format = "yyyy-mm-dd hh:mm"
        ws.cell(r, 6).number_format = "0%"
    ws.conditional_formatting.add(f"D5:D{end}", CellIsRule(operator="equal", formula=[f'"{UNCLASSIFIED}"'], fill=YELLOW))
    ws.conditional_formatting.add(f"D5:D{end}", CellIsRule(operator="equal", formula=['"客訴"'], fill=RED))


def _runs(ws, engine: Engine, day: date) -> None:
    _title(ws, "執行紀錄", "每次排程執行的結果")
    start = datetime.combine(day, datetime.min.time())
    with engine.connect() as c:
        rows = c.execute(select(ops_run_log).where(ops_run_log.c.started_at >= start)
                         .order_by(ops_run_log.c.started_at)).all()
    data = [[r.started_at, r.trigger, r.status, r.emails_seen, r.documents_seen, r.documents_posted,
             r.documents_review, r.documents_failed, r.anomalies, r.llm_tokens, (r.duration_ms or 0) / 1000]
            for r in rows]
    end = _table(ws, 4, ["開始", "觸發", "結果", "信件", "文件", "入帳", "待確認", "失敗", "異常", "LLM tokens", "秒"],
                 data, [17, 8, 8, 7, 7, 7, 8, 7, 7, 11, 7])
    for r in range(5, end + 1):
        ws.cell(r, 1).number_format = "yyyy-mm-dd hh:mm:ss"
        ws.cell(r, 11).number_format = "0.0"
    ws.conditional_formatting.add(f"A5:K{end}", FormulaRule(formula=['$C5="failed"'], fill=RED))
