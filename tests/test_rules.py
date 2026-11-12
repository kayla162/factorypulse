"""比對規則的單元測試：用人工構造的小例子，涵蓋 testdata 沒有的情況。"""
from datetime import date
from decimal import Decimal as D

from factorypulse.models import ParsedDocument, ParsedLine
from factorypulse.rules import ErpView, PoLine, check_business, check_parse


def po(received=0, due=date(2026, 10, 20)):
    return ErpView(
        po_lines={"PO1": [PoLine("PO1", 1, "V001", "P-1", 10000, D("1.50"), due, received),
                          PoLine("PO1", 2, "V001", "P-2", 5000, D("2.00"), due)]},
        current_price={"P-1": D("1.50"), "P-2": D("2.00")})


def delivery(*lines, day=date(2026, 10, 18), po_no="PO1"):
    return ParsedDocument("delivery", "test", "D1", day, po_no,
                          lines=[ParsedLine(p, qty=q, unit_price=D(u), amount=q * D(u), part_no=p)
                                 for p, q, u in lines])


def codes(anoms):
    return sorted(a.code for a in anoms)


def test_clean_delivery_has_no_anomaly():
    assert check_business(delivery(("P-1", 10000, "1.50"), ("P-2", 5000, "2.00")), "V001", po()) == []


def test_partial_delivery_uses_open_qty():
    # 已收 6000，剩 4000；這次送 4000 是剛好交齊，不是短交
    assert check_business(delivery(("P-1", 4000, "1.50")), "V001", po(received=6000)) == []
    assert codes(check_business(delivery(("P-1", 3000, "1.50")), "V001", po(received=6000))) == ["QTY_SHORT"]
    assert codes(check_business(delivery(("P-1", 5000, "1.50")), "V001", po(received=6000))) == ["QTY_OVER"]


def test_price_and_late():
    out = check_business(delivery(("P-1", 10000, "1.65"), day=date(2026, 10, 23)), "V001", po())
    assert codes(out) == ["LATE_DELIVERY", "PRICE_MISMATCH"]
    assert "延遲 3 天" in next(a.detail for a in out if a.code == "LATE_DELIVERY")


def test_po_problems():
    assert codes(check_business(delivery(("P-1", 1, "1.50"), po_no="PO9"), "V001", po())) == ["PO_NOT_FOUND"]
    assert codes(check_business(delivery(("P-1", 1, "1.50")), "V002", po())) == ["VENDOR_MISMATCH"]
    assert codes(check_business(delivery(("P-9", 1, "1.50")), "V001", po())) == ["PART_NOT_ON_PO"]


def test_inspection_ng_takes_priority_over_rate():
    doc = ParsedDocument("inspection", "test", "QC1", lines=[
        ParsedLine("P-1", qty=10000, lot_no="L1", sample_qty=200, defect_qty=9, result="NG", part_no="P-1"),
        ParsedLine("P-2", qty=5000, lot_no="L2", sample_qty=200, defect_qty=2, result="OK", part_no="P-2"),
        ParsedLine("P-2", qty=5000, lot_no="L3", sample_qty=200, defect_qty=3, result="OK", part_no="P-2"),
    ])
    # 9/200 判 NG → 只報 NG；2/200 = 1% 剛好在上限內；3/200 = 1.5% 判 OK → 超標
    assert codes(check_business(doc, "V001", po())) == ["DEFECT_RATE_HIGH", "INSPECTION_NG"]


def test_quote_price_increase():
    doc = ParsedDocument("quote", "test", "Q1", lines=[
        ParsedLine("P-1", qty=1, unit_price=D("1.68"), part_no="P-1"),
        ParsedLine("P-2", qty=1, unit_price=D("1.90"), part_no="P-2")])   # 降價不報
    out = check_business(doc, "V001", po())
    assert codes(out) == ["PRICE_INCREASE"] and "+12.0%" in out[0].detail


def test_parse_check_catches_bad_totals():
    doc = delivery(("P-1", 10000, "1.50"))
    doc.subtotal, doc.tax, doc.total = D("15000"), D("750"), D("15750")
    assert check_parse(doc) == []
    doc.total = D("17500")   # OCR 把 15750 讀成 17500
    assert codes(check_parse(doc)) == ["PARSE_CHECK_FAILED"]
    doc.lines[0].amount = D("1500")   # 少一個 0
    assert codes(check_parse(doc)) == ["PARSE_CHECK_FAILED"]
