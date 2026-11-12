"""解析結果的資料結構。不論來源是 PDF、Excel 或之後的 OCR，都轉成同一種格式。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class ParsedLine:
    raw_part_no: str
    description: str | None = None
    qty: int | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    lot_no: str | None = None
    sample_qty: int | None = None
    defect_qty: int | None = None
    result: str | None = None
    part_no: str | None = None  # 對應到 ERP 料號後填入


@dataclass
class ParsedDocument:
    doc_type: str               # quote / delivery / inspection
    parser: str
    doc_no: str | None = None
    doc_date: date | None = None
    po_no: str | None = None
    ref_doc_no: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    lines: list[ParsedLine] = field(default_factory=list)


@dataclass
class Anomaly:
    code: str
    detail: str
    part_no: str | None = None


class NeedsOCR(Exception):
    """純影像文件，沒有文字層。"""


class ParseError(Exception):
    """看得懂檔案，但抓不到必要欄位。"""
