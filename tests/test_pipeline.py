"""整條管線：收信 → 解析 → 比對 → 寫入 → 分類 → 通知 → 執行紀錄。"""
import email
from email import policy

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from factorypulse.db import (doc_document, erp_goods_receipt, erp_incoming_inspection, mail_message,
                             ops_anomaly, ops_run_log)
from factorypulse.evaluate import score

from .conftest import TESTDATA, inbox_with, needs_ocr, run_pipeline

ALL_EXPECTED = {
    ("A_delivery_02", "PRICE_MISMATCH"), ("A_inspection_02", "INSPECTION_NG"),
    ("B_delivery_01", "QTY_SHORT"), ("B_delivery_02", "LATE_DELIVERY"), ("B_quote_02", "PRICE_INCREASE"),
    ("C_delivery_01", "PART_UNKNOWN"), ("C_inspection_01", "DEFECT_RATE_HIGH"),
}


def count(engine, table, *where):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(table).where(*where)).scalar()


@needs_ocr
def test_full_batch_with_ocr(engine, tmp_path):
    s, notifier = run_pipeline(engine, tmp_path)
    assert (s.emails_seen, len(s.results)) == (23, 18)
    assert (s.count("posted"), s.count("posted_with_alerts"), s.count("needs_review")) == (9, 6, 3)

    res = score(engine, TESTDATA)
    assert res.uncovered_docs == set()
    assert res.tp == ALL_EXPECTED and res.fp == set() and res.fn == set()
    assert res.field_errors == [
        "B_inspection_01 第3行.sample_qty: 應為 125，抓到 None",
        "B_inspection_02 第3行.sample_qty: 應為 200，抓到 None",
    ]
    assert len(res.review_flags) == 2 and all("印章" in x for x in res.review_flags)
    # 規則分類 18 封全對；沒有 LLM 的 5 封轉人工，沒有一封分錯
    assert res.mail_by_method == {"rule": [18, 18], "none": [0, 5]} and res.mail_errors == []
    # 9 份文件的異常通知 ＋ 5 封待分類信
    assert len(notifier.sent) == 14


def test_full_batch_without_ocr(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("FP_OCR", "0")
    s, _ = run_pipeline(engine, tmp_path)
    assert (s.count("posted"), s.count("posted_with_alerts"), s.count("needs_review"), s.count("needs_ocr")) \
        == (8, 3, 1, 6)
    res = score(engine, TESTDATA)
    assert res.fp == set() and res.fn == set() and len(res.tp) == 4


def test_held_document_is_not_posted(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("FP_OCR", "0")
    run_pipeline(engine, tmp_path)
    with engine.connect() as c:
        held = c.execute(select(doc_document.c.id).where(doc_document.c.doc_no == "D26101201")).scalar()
    assert count(engine, erp_goods_receipt, erp_goods_receipt.c.document_id == held) == 0
    assert count(engine, erp_goods_receipt) == 6       # A 兩張、C 一張，各 2 行
    assert count(engine, erp_incoming_inspection) == 8


def test_rerun_is_idempotent(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("FP_OCR", "0")
    run_pipeline(engine, tmp_path)
    tables = (doc_document, erp_goods_receipt, ops_anomaly, mail_message)
    before = [count(engine, t) for t in tables]
    s, notifier = run_pipeline(engine, tmp_path)
    assert s.emails_skipped == 23 and notifier.sent == []
    assert [count(engine, t) for t in tables] == before
    with engine.connect() as c:
        runs = c.execute(select(ops_run_log).order_by(ops_run_log.c.started_at)).all()
    assert [r.status for r in runs] == ["ok", "ok"] and runs[1].emails_skipped == 23


def test_same_doc_no_resent_with_new_file_is_duplicate(engine, tmp_path):
    """供應商重寄同一張單（檔案內容不同、單號相同）不能重複入帳。"""
    inbox = inbox_with(tmp_path, "C_delivery_02.eml")
    run_pipeline(engine, tmp_path, inbox)

    wb = load_workbook(TESTDATA / "documents" / "C_delivery_02.xlsx")
    wb.active["G8"] = "重寄"                      # 改一格備註，檔案雜湊就不同
    resent = tmp_path / "C_delivery_02_resend.xlsx"
    wb.save(resent)
    msg = email.message_from_bytes((inbox / "C_delivery_02.eml").read_bytes(), policy=policy.default)
    for part in msg.iter_attachments():
        part.set_content(resent.read_bytes(), maintype="application",
                         subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         filename="C_delivery_02_resend.xlsx")
    msg.replace_header("Subject", "RE: 重寄 " + msg["Subject"])
    (inbox / "C_delivery_02.eml").write_bytes(bytes(msg))

    s, _ = run_pipeline(engine, tmp_path, inbox)
    assert [r.status for r in s.results] == ["duplicate"]
    assert count(engine, erp_goods_receipt) == 2
    # 雖然單據重複，信件本身仍依單據種類分到採購
    assert s.mails[0].classification.label == "採購"


@pytest.mark.parametrize("name, label", [("A_inspection_01.eml", "品保"), ("C_quote_01.eml", "採購")])
def test_rule_classification_by_document(engine, tmp_path, name, label):
    s, notifier = run_pipeline(engine, tmp_path, inbox_with(tmp_path, name))
    c = s.mails[0].classification
    assert (c.label, c.method, c.llm_calls) == (label, "rule", 0)
    assert notifier.sent == []    # 規則分類的單據信只在有異常時才通知
