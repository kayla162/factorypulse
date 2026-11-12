"""各解析器共用：判斷文件種類、抓表頭欄位、把表格紀錄轉成 ParsedLine。"""
from __future__ import annotations

import re

from ..models import ParsedLine
from ..normalize import normalize_lot, parse_date, parse_decimal, parse_int, to_halfwidth

# 順序有意義：「出貨檢驗報告」同時含「出貨」與「檢驗」，要先判檢驗
DOC_TYPE_KEYWORDS = [
    ("inspection", ["檢驗報告", "檢驗成績", "成績書", "品檢", "QCReport", "檢驗"]),
    ("quote", ["報價單", "估價單", "報價"]),
    ("delivery", ["出貨單", "送貨單", "交貨單", "出貨明細"]),
]

NUM = r"([\d,]+(?:\.\d+)?)"
FIELD_PATTERNS = {
    "doc_no": [r"(?:單號|單據編號|No\.)\s*[：:]?\s*([A-Z]{1,3}\d{6,})"],
    "po_no": [r"(PO\d{6,})"],
    "ref_doc_no": [r"對應出貨單\s*[：:]\s*([A-Z]{1,3}\d{6,})"],
    "subtotal": [r"未稅合計\s*[：:]\s*" + NUM],
    "tax": [r"營業稅[^：:\n]*[：:]\s*" + NUM],
    "total": [r"含稅總計\s*[：:]\s*" + NUM],
}


def detect_doc_type(text: str) -> str | None:
    s = re.sub(r"\s+", "", to_halfwidth(text))
    for doc_type, words in DOC_TYPE_KEYWORDS:
        if any(w in s for w in words):
            return doc_type
    return None


def find_field(text: str, name: str) -> str | None:
    for pat in FIELD_PATTERNS[name]:
        if m := re.search(pat, text):
            return m.group(1)
    return None


def find_date(text: str):
    """先找「日期：」後面的值，找不到再找全文第一個像日期的字串（例如民國日期）。"""
    if m := re.search(r"日期\s*[：:]\s*([^\s　]+)", text):
        if d := parse_date(m.group(1)):
            return d
    for line in text.splitlines():
        if "有效" in line or "送達" in line:  # 跳過報價有效期、預計送達這類次要日期
            continue
        if d := parse_date(line):
            return d
    return None


def record_to_line(rec: dict) -> ParsedLine:
    desc = " ".join(x for x in (rec.get("description"), rec.get("spec")) if x) or None
    result = rec.get("result")
    return ParsedLine(
        raw_part_no=to_halfwidth(str(rec["raw_part_no"])),
        description=desc,
        qty=parse_int(rec.get("qty")),
        unit_price=parse_decimal(rec.get("unit_price")),
        amount=parse_decimal(rec.get("amount")),
        lot_no=normalize_lot(str(rec["lot_no"])) if rec.get("lot_no") else None,
        sample_qty=parse_int(rec.get("sample_qty")),
        defect_qty=parse_int(rec.get("defect_qty")),
        result=to_halfwidth(str(result)).upper() if result else None,
    )
