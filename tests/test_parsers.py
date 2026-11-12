"""解析器對標準答案逐欄比對。A：數位 PDF、B：掃描檔（OCR）、C：Excel。"""
from decimal import Decimal

import pytest

from factorypulse.models import NeedsOCR
from factorypulse.parsers import parse_document

from .conftest import TESTDATA, answer, needs_ocr

A_AND_C = [f"{v}_{t}_{i:02d}" for v in "AC" for t in ("quote", "delivery", "inspection") for i in (1, 2)]
B_DOCS = [f"B_{t}_{i:02d}" for t in ("quote", "delivery", "inspection") for i in (1, 2)]
# 被品管印章蓋住、人眼也看不清的格子：正確行為是讀不到 → 送人工，而不是猜
STAMPED = {("B_inspection_01", 2, "sample"), ("B_inspection_02", 2, "sample")}


def _file(doc_id):
    return TESTDATA / "documents" / answer(doc_id)["files"][0]


def _check_header(doc, ans):
    assert doc.doc_type == ans["doc_type"]
    assert doc.doc_no == ans["doc_no"]
    assert doc.doc_date.isoformat() == ans["doc_date"]
    assert doc.po_no == ans["po_no"]
    if ans["doc_type"] == "inspection":
        assert doc.ref_doc_no == ans["ref_delivery"]


def _check_lines(doc_id, doc, ans):
    assert len(doc.lines) == len(ans["items"])
    for i, (got, exp) in enumerate(zip(doc.lines, ans["items"])):
        assert got.raw_part_no == exp["raw_part"]
        assert got.qty == exp["qty"]
        if ans["doc_type"] == "inspection":
            assert (got.lot_no, got.defect_qty, got.result) == (exp["lot_no"], exp["defects"], exp["result"])
            if (doc_id, i, "sample") in STAMPED:
                assert got.sample_qty is None
            else:
                assert got.sample_qty == exp["sample"]
        else:
            assert got.unit_price == Decimal(str(exp["unit_price"]))


@pytest.mark.parametrize("doc_id", A_AND_C)
def test_digital_documents(doc_id):
    ans = answer(doc_id)
    doc = parse_document(_file(doc_id))
    assert doc.parser in ("pdf_text", "excel")
    _check_header(doc, ans)
    _check_lines(doc_id, doc, ans)


@needs_ocr
@pytest.mark.parametrize("doc_id", B_DOCS)
def test_scanned_documents_via_ocr(doc_id):
    ans = answer(doc_id)
    doc = parse_document(_file(doc_id))
    assert doc.parser == "ocr"
    _check_header(doc, ans)       # 民國日期、P/O 寫法、欄位順序不同
    _check_lines(doc_id, doc, ans)
    if ans["doc_type"] != "inspection":
        assert doc.total == Decimal(str(ans["total"]))


@pytest.mark.parametrize("doc_id", ["A_delivery_01", "A_quote_02"])
def test_pdf_totals(doc_id):
    ans = answer(doc_id)
    doc = parse_document(_file(doc_id))
    assert (doc.subtotal, doc.tax, doc.total) == tuple(Decimal(str(ans[k])) for k in ("subtotal", "tax", "total"))


def test_excel_skips_unrelated_sheet():
    # C 的檔案第二張工作表是匯款資訊，不能讀錯
    doc = parse_document(_file("C_delivery_02"))
    assert doc.doc_type == "delivery" and len(doc.lines) == 2


def test_scanned_pdf_without_ocr_is_flagged(monkeypatch):
    monkeypatch.setenv("FP_OCR", "0")
    with pytest.raises(NeedsOCR):
        parse_document(_file("B_delivery_01"))
