"""異常比對規則。全部是確定性的程式判斷，不交給 LLM。

輸入：已解析並對應好料號的文件 ＋ ERP 現況（ErpView）
輸出：Anomaly 清單
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from . import config
from .models import Anomaly, ParsedDocument

CENT = Decimal("0.0001")


@dataclass
class PoLine:
    po_no: str
    line: int
    vendor_code: str
    part_no: str
    qty: int
    unit_price: Decimal
    due_date: date
    received_qty: int = 0

    @property
    def open_qty(self) -> int:
        return self.qty - self.received_qty


@dataclass
class ErpView:
    """規則需要的 ERP 資料快照。"""
    po_lines: dict[str, list[PoLine]]          # po_no → 各行
    current_price: dict[str, Decimal]          # part_no → 現行單價


def check_parse(doc: ParsedDocument) -> list[Anomaly]:
    """驗算：金額＝數量×單價、合計、稅額。對不上代表解析或原始文件有誤，交給人看。"""
    tol = config.AMOUNT_TOLERANCE
    out = []
    for i, ln in enumerate(doc.lines, 1):
        if doc.doc_type == "inspection":
            missing = [name for name, v in (("數量", ln.qty), ("批號", ln.lot_no), ("抽樣數", ln.sample_qty),
                                            ("不良數", ln.defect_qty), ("判定", ln.result)) if v in (None, "")]
            if missing:
                out.append(Anomaly("PARSE_CHECK_FAILED",
                                   f"第 {i} 行（{ln.raw_part_no}）讀不到{'、'.join(missing)}，可能被印章或污損遮住"))
            elif ln.defect_qty > ln.sample_qty:
                out.append(Anomaly("PARSE_CHECK_FAILED", f"第 {i} 行不良數 {ln.defect_qty} 大於抽樣數 {ln.sample_qty}"))
            continue
        if ln.qty is None or ln.unit_price is None:
            out.append(Anomaly("PARSE_CHECK_FAILED", f"第 {i} 行缺數量或單價"))
            continue
        if ln.amount is not None and abs(ln.qty * ln.unit_price - ln.amount) > tol:
            out.append(Anomaly("PARSE_CHECK_FAILED",
                               f"第 {i} 行金額 {ln.amount} ≠ 數量 {ln.qty} × 單價 {ln.unit_price}"))
    if doc.doc_type == "inspection" or out:
        return out
    lines_sum = sum((ln.amount if ln.amount is not None else ln.qty * ln.unit_price) for ln in doc.lines)
    if doc.subtotal is not None and abs(lines_sum - doc.subtotal) > tol:
        out.append(Anomaly("PARSE_CHECK_FAILED", f"明細加總 {lines_sum} ≠ 未稅合計 {doc.subtotal}"))
    base = doc.subtotal if doc.subtotal is not None else lines_sum
    if doc.tax is not None and abs(base * config.TAX_RATE - doc.tax) > tol:
        out.append(Anomaly("PARSE_CHECK_FAILED", f"稅額 {doc.tax} ≠ {base} × 5%"))
    if doc.total is not None:
        expected = base + (doc.tax if doc.tax is not None else base * config.TAX_RATE)
        if abs(expected - doc.total) > tol:
            out.append(Anomaly("PARSE_CHECK_FAILED", f"含稅總計 {doc.total} ≠ 推算值 {expected:.2f}"))
    return out


def check_business(doc: ParsedDocument, vendor_code: str, erp: ErpView) -> list[Anomaly]:
    if doc.doc_type == "delivery":
        return _check_delivery(doc, vendor_code, erp)
    if doc.doc_type == "inspection":
        return _check_inspection(doc)
    if doc.doc_type == "quote":
        return _check_quote(doc, erp)
    return []


def _check_delivery(doc: ParsedDocument, vendor_code: str, erp: ErpView) -> list[Anomaly]:
    lines = erp.po_lines.get(doc.po_no or "")
    if not lines:
        return [Anomaly("PO_NOT_FOUND", f"出貨單 {doc.doc_no} 的採購單號 {doc.po_no} 不存在")]
    if lines[0].vendor_code != vendor_code:
        return [Anomaly("VENDOR_MISMATCH",
                        f"採購單 {doc.po_no} 屬於 {lines[0].vendor_code}，寄件者卻是 {vendor_code}")]
    out: list[Anomaly] = []
    earliest_due: date | None = None
    for ln in doc.lines:
        if ln.part_no is None:
            continue  # 料號對不到，已另外記 PART_UNKNOWN
        po = next((p for p in lines if p.part_no == ln.part_no), None)
        if po is None:
            out.append(Anomaly("PART_NOT_ON_PO", f"{ln.part_no} 不在採購單 {doc.po_no} 上", ln.part_no))
            continue
        if ln.unit_price is not None and abs(ln.unit_price - po.unit_price) > CENT:
            out.append(Anomaly("PRICE_MISMATCH",
                               f"出貨單價 {_fmt(ln.unit_price)} ≠ 採購單價 {_fmt(po.unit_price)}", ln.part_no))
        if ln.qty is not None and ln.qty < po.open_qty:
            out.append(Anomaly("QTY_SHORT",
                               f"出貨 {ln.qty:,} < 未交量 {po.open_qty:,}，短少 {po.open_qty - ln.qty:,}", ln.part_no))
        elif ln.qty is not None and ln.qty > po.open_qty:
            out.append(Anomaly("QTY_OVER",
                               f"出貨 {ln.qty:,} > 未交量 {po.open_qty:,}，超交 {ln.qty - po.open_qty:,}", ln.part_no))
        if doc.doc_date and doc.doc_date > po.due_date:
            earliest_due = min(earliest_due or po.due_date, po.due_date)
    if earliest_due:
        days = (doc.doc_date - earliest_due).days
        out.append(Anomaly("LATE_DELIVERY", f"出貨日 {doc.doc_date} 晚於採購交期 {earliest_due}，延遲 {days} 天"))
    return out


def _check_inspection(doc: ParsedDocument) -> list[Anomaly]:
    out = []
    for ln in doc.lines:
        if ln.part_no is None or ln.sample_qty in (None, 0) or ln.defect_qty is None:
            continue
        rate = Decimal(ln.defect_qty) / Decimal(ln.sample_qty)
        if ln.result in ("NG", "REJECT", "不合格"):
            out.append(Anomaly("INSPECTION_NG",
                               f"批號 {ln.lot_no} 供應商判定 NG（不良 {ln.defect_qty}/{ln.sample_qty}）仍出貨",
                               ln.part_no))
        elif rate > config.AQL_LIMIT:
            out.append(Anomaly("DEFECT_RATE_HIGH",
                               f"批號 {ln.lot_no} 不良率 {rate:.1%} 超過 AQL {config.AQL_LIMIT:.0%}，供應商卻判 {ln.result}",
                               ln.part_no))
    return out


def _check_quote(doc: ParsedDocument, erp: ErpView) -> list[Anomaly]:
    out = []
    for ln in doc.lines:
        cur = erp.current_price.get(ln.part_no or "")
        if cur is None or ln.unit_price is None:
            continue
        if ln.unit_price - cur > CENT:
            pct = (ln.unit_price - cur) / cur
            out.append(Anomaly("PRICE_INCREASE",
                               f"報價 {_fmt(ln.unit_price)} 高於現行單價 {_fmt(cur)}（+{pct:.1%}）", ln.part_no))
    return out


def _fmt(d: Decimal) -> str:
    """單價至少顯示兩位小數（3.60 而不是 3.6），更多位數則照實顯示。"""
    two = d.quantize(Decimal("0.01"))
    return f"{two}" if two == d else f"{d.normalize():f}"
