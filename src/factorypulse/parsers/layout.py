"""版面無關的表格抽取：輸入「文字方塊＋座標」，輸出表頭欄位與資料列。

PDF 文字層、OCR（第二週）都先轉成 TextBox，共用同一套抽取邏輯；
換一家供應商通常只要在 HEADER_SYNONYMS 補一個欄名。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..normalize import to_halfwidth


@dataclass
class TextBox:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


# 欄位 → 各家供應商的欄名寫法（比對前會去掉空白）
HEADER_SYNONYMS: dict[str, list[str]] = {
    "raw_part_no": ["料號", "品號", "本公司料號", "PartNo", "P/N"],
    "description": ["品名/規格", "品名", "加工內容", "品名規格"],
    "spec": ["規格"],
    "qty": ["數量(PCS)", "數量", "出貨數", "出貨數量"],
    "unit_price": ["單價", "單價(未稅)"],
    "amount": ["金額", "小計", "金額(未稅)"],
    "lot_no": ["批號", "LotNo"],
    "sample_qty": ["抽樣數", "抽樣"],
    "defect_qty": ["不良數", "不良"],
    "result": ["判定", "結果"],
}
_LOOKUP = {to_halfwidth(s).replace(" ", ""): field
           for field, names in HEADER_SYNONYMS.items() for s in names}

PART_LIKE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.]{3,29}$")


def header_field(text: str) -> str | None:
    return _LOOKUP.get(to_halfwidth(text).replace(" ", "").replace("　", ""))


def group_rows(boxes: list[TextBox], tol: float = 4.0) -> list[list[TextBox]]:
    """依垂直位置分列，同一列再依 x 排序。"""
    rows: list[list[TextBox]] = []
    for b in sorted(boxes, key=lambda b: (b.yc, b.x0)):
        if rows and abs(rows[-1][0].yc - b.yc) <= tol:
            rows[-1].append(b)
        else:
            rows.append([b])
    return [sorted(r, key=lambda b: b.x0) for r in rows]


def extract_table(boxes: list[TextBox], row_tol: float = 4.0) -> list[dict[str, str]]:
    """找到含「料號」的表頭列，往下讀到第一列不像料號為止。"""
    rows = group_rows(boxes, tol=row_tol)
    for hi, row in enumerate(rows):
        # 不認得的欄（項次、不良率…）也保留成 None，當作欄位邊界，避免資料被分到隔壁欄
        cols = sorted(((header_field(b.text), b.x0) for b in row), key=lambda c: c[1])
        if not any(f == "raw_part_no" for f, _ in cols):
            continue
        records = []
        for data in rows[hi + 1:]:
            rec: dict[str, str] = {}
            for b in data:
                field = _column_for(b.x0, cols)
                if field:
                    rec[field] = (rec.get(field, "") + " " + b.text).strip()
            part = rec.get("raw_part_no", "")
            if not PART_LIKE.match(to_halfwidth(part)):
                break
            records.append(rec)
        return records
    return []


def _column_for(x: float, cols: list[tuple[str | None, float]], slack: float = 8.0) -> str | None:
    """回傳 x 落在哪一欄：最後一個「起點 ≤ x + slack」的表頭。"""
    chosen = None
    for field, cx in cols:
        if cx <= x + slack:
            chosen = field
        else:
            break
    return chosen
