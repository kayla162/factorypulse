"""Excel 單據：表頭不一定在第一列、可能有合併儲存格、可能有好幾張工作表。"""
from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook

from ..models import ParsedDocument, ParseError
from ..normalize import parse_date, parse_decimal, to_halfwidth
from .common import FIELD_PATTERNS, detect_doc_type, record_to_line
from .layout import PART_LIKE, header_field

LABELS = {
    "doc_no": ["單據編號", "單號", "No."],
    "doc_date": ["日期", "出貨日期", "單據日期"],
    "po_no": ["客戶PO", "採購單號", "PO", "P/O"],
    "subtotal": ["未稅合計"],
    "tax": ["營業稅"],
    "total": ["含稅總計", "總計"],
}


def _norm(v) -> str:
    return to_halfwidth(str(v)).replace(" ", "") if v is not None else ""


def _find_header(rows: list[tuple]) -> tuple[int, list[str | None]] | None:
    for i, row in enumerate(rows):
        fields = [header_field(str(c)) if c is not None else None for c in row]
        if "raw_part_no" in fields:
            return i, fields
    return None


def _label_value(rows: list[tuple], labels: list[str]):
    """找到標籤儲存格，取同一列右邊第一個非空值。"""
    wanted = {_norm(x) for x in labels}
    for row in rows:
        for j, c in enumerate(row):
            if _norm(c) in wanted:
                for v in row[j + 1:]:
                    if v not in (None, ""):
                        return v
    return None


def parse_excel(path: Path) -> ParsedDocument:
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        # 挑出有明細表頭的那張工作表（供應商常在後面多放匯款資訊等無關工作表）
        for ws in wb.worksheets:
            rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
            found = _find_header(rows)
            if found:
                break
        else:
            raise ParseError(f"{path.name} 找不到含「料號」的表頭")
    finally:
        wb.close()

    hi, fields = found
    records = []
    for row in rows[hi + 1:]:
        if all(c in (None, "") for c in row):
            break
        rec = {f: c for f, c in zip(fields, row) if f and c not in (None, "")}
        if not PART_LIKE.match(_norm(rec.get("raw_part_no", ""))):
            break
        records.append(rec)

    all_text = "\n".join(str(c) for r in rows for c in r if c is not None)
    doc_type = detect_doc_type("\n".join(str(c) for r in rows[:hi] for c in r if c is not None))
    if doc_type is None:
        raise ParseError(f"{path.name} 無法判斷文件種類")
    ref = re.search(FIELD_PATTERNS["ref_doc_no"][0], all_text)
    doc = ParsedDocument(
        doc_type=doc_type, parser="excel",
        doc_no=_norm(_label_value(rows, LABELS["doc_no"])) or None,
        doc_date=parse_date(_label_value(rows, LABELS["doc_date"])),
        po_no=_norm(_label_value(rows, LABELS["po_no"])) or None,
        ref_doc_no=ref.group(1) if ref else None,
        subtotal=parse_decimal(_label_value(rows, LABELS["subtotal"])),
        tax=parse_decimal(_label_value(rows, LABELS["tax"])),
        total=parse_decimal(_label_value(rows, LABELS["total"])),
        lines=[record_to_line(r) for r in records],
    )
    if not doc.doc_no or not doc.lines:
        raise ParseError(f"{path.name} 抓不到單號或明細")
    return doc
