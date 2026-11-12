"""有文字層的 PDF：取出每段文字與座標，交給共用的表格抽取。"""
from __future__ import annotations

from pathlib import Path

import pymupdf

from ..models import NeedsOCR, ParsedDocument, ParseError
from ..normalize import parse_decimal
from .common import detect_doc_type, find_date, find_field, record_to_line
from .layout import TextBox, extract_table

MIN_TEXT_CHARS = 20  # 少於這個字數視為掃描檔


def page_boxes(page) -> list[TextBox]:
    boxes = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            txt = "".join(s["text"] for s in line["spans"]).strip()
            if txt:
                x0, y0, x1, y1 = line["bbox"]
                boxes.append(TextBox(txt, x0, y0, x1, y1))
    return boxes


def parse_pdf(path: Path) -> ParsedDocument:
    with pymupdf.open(path) as pdf:
        text = "\n".join(p.get_text("text") for p in pdf)
        if len(text.strip()) < MIN_TEXT_CHARS:
            raise NeedsOCR(f"{path.name} 沒有文字層，需要 OCR")
        boxes = [b for p in pdf for b in page_boxes(p)]  # 目前的單據都是單頁
    return build_document(text, boxes, parser="pdf_text")


def build_document(text: str, boxes: list[TextBox], parser: str, row_tol: float = 4.0) -> ParsedDocument:
    """PDF 與 OCR 共用：全文抓表頭欄位、座標抓表格。"""
    doc_type = detect_doc_type(text)
    if doc_type is None:
        raise ParseError("無法判斷文件種類（報價單／出貨單／檢驗報告）")
    doc = ParsedDocument(
        doc_type=doc_type, parser=parser,
        doc_no=find_field(text, "doc_no"),
        doc_date=find_date(text),
        po_no=find_field(text, "po_no"),
        ref_doc_no=find_field(text, "ref_doc_no"),
        subtotal=parse_decimal(find_field(text, "subtotal")),
        tax=parse_decimal(find_field(text, "tax")),
        total=parse_decimal(find_field(text, "total")),
        lines=[record_to_line(r) for r in extract_table(boxes, row_tol)],
    )
    if not doc.doc_no or not doc.lines:
        raise ParseError(f"抓不到單號或明細（單號={doc.doc_no}，明細 {len(doc.lines)} 筆）")
    return doc
