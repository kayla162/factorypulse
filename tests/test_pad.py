"""PAD 對照組：模擬 PAD 流程的讀法（固定儲存格、全部讀成文字）寫進暫存表，再驗證比對工具。

這裡驗證的是比對工具與 SQL 檢查邏輯，不是 PAD 本身；PAD 要在 Windows 上實際執行。
"""
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import insert

from factorypulse.db import pad_delivery_staging
from factorypulse.pad_compare import compare, to_markdown

from .conftest import TESTDATA

DOCS = TESTDATA / "documents"


def simulate_pad(engine, run_tag="20261113-090000", folder: Path = DOCS, tamper=None):
    """照 pad/出貨單匯入.txt 的邏輯：A2 含「出貨」才處理，B4/E4/B5 表頭，第 8 列起讀到 A 欄空白。"""
    rows = []
    for path in sorted(folder.glob("*.xlsx")):
        ws = load_workbook(path, read_only=True).worksheets[0]
        cell = lambda r, c: ws.cell(r, c).value  # noqa: E731
        if "出貨" not in str(cell(2, 1) or ""):
            continue
        r = 8
        while r <= 200 and cell(r, 1) not in (None, ""):
            rows.append(dict(run_tag=run_tag, file_name=path.name, doc_no=str(cell(4, 2)), doc_date_text=str(cell(4, 5)),
                             po_no=str(cell(5, 2)), vendor_part_no=str(cell(r, 1)), qty_text=str(cell(r, 3)),
                             unit_price_text=str(cell(r, 4)), loaded_at=datetime.now()))
            r += 1
    if tamper:
        tamper(rows)
    with engine.begin() as c:
        c.execute(insert(pad_delivery_staging), rows)
    return rows


def test_pad_matches_python(engine):
    rows = simulate_pad(engine)
    assert len(rows) == 4                           # 宇碩出貨單 2 份 × 2 行
    r = compare(engine, DOCS)
    assert r.files == ["C_delivery_01.xlsx", "C_delivery_02.xlsx"]
    assert (r.fields_same, r.fields_total) == (24, 24) and r.diffs == [] and r.missing_files == []
    # PAD＋SQL 檢查也抓得到打錯的料號
    assert r.pad_findings == ["C_delivery_01.xlsx：料號 YS-SN-2O0 對不到"]


def test_compare_reports_differences(engine):
    def tamper(rows):
        rows[0]["qty_text"] = "1000"                # 少讀一個 0
        rows[1]["unit_price_text"] = "0.23"
    simulate_pad(engine, tamper=tamper)
    r = compare(engine, DOCS)
    assert r.fields_same == 22 and len(r.diffs) == 2
    assert "數量" in r.diffs[0] and "單價" in r.diffs[1]


def test_compare_uses_latest_run_and_flags_missing_files(engine):
    def drop_first_file(rows):
        del rows[:2]                                # 模擬 PAD 漏讀 C_delivery_01（前 2 列）

    simulate_pad(engine, run_tag="20261113-090000")
    simulate_pad(engine, run_tag="20261113-100000", tamper=drop_first_file)
    r = compare(engine, DOCS)
    assert r.run_tag == "20261113-100000" and r.missing_files == ["C_delivery_01.xlsx"]


def test_no_pad_run_yet(engine):
    assert "還沒有 PAD 寫入的資料" in to_markdown(compare(engine, DOCS))
