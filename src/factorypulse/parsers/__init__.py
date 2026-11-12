"""依副檔名與內容挑解析器：數位 PDF → 文字層；掃描 PDF → OCR；Excel → 儲存格。"""
from __future__ import annotations

import os
from pathlib import Path

from ..models import NeedsOCR, ParsedDocument, ParseError
from .excel import parse_excel
from .pdf_text import parse_pdf

SUPPORTED = {".pdf", ".xlsx", ".xlsm"}


def ocr_enabled() -> bool:
    return os.environ.get("FP_OCR", "1") != "0"


def parse_document(path: Path) -> ParsedDocument:
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            return parse_pdf(path)
        except NeedsOCR:
            if not ocr_enabled():
                raise
            from .ocr import parse_scanned_pdf  # OCR 模型載入較慢，用到才 import
            return parse_scanned_pdf(path)
    if ext in (".xlsx", ".xlsm"):
        return parse_excel(path)
    raise ParseError(f"不支援的附件格式：{path.name}")


__all__ = ["parse_document", "NeedsOCR", "ParseError", "SUPPORTED"]
