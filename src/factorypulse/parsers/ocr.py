"""掃描檔：RapidOCR 辨識 → 校正歪斜 → 轉成 TextBox，交給和數位 PDF 相同的抽取邏輯。

RapidOCR 用 ONNX Runtime 在 CPU 上跑，不需要 GPU，也不用把文件送到外部服務。
"""
from __future__ import annotations

import math
import statistics
from functools import lru_cache
from pathlib import Path

import numpy as np
import pymupdf

from ..models import NeedsOCR, ParsedDocument
from .layout import TextBox, group_rows
from .pdf_text import build_document

RENDER_DPI = 200
MIN_SCORE = 0.5   # 信心太低的方塊直接丟掉（多半是印章、雜訊）

# OCR 模型偶爾把繁體字辨識成簡體；只收錄單據上會出現的字，避免誤改
S2T = str.maketrans({
    "数": "數", "规": "規", "税": "稅", "业": "業", "货": "貨", "单": "單", "预": "預", "计": "計",
    "盖": "蓋", "体": "體", "号": "號", "购": "購", "张": "張", "检": "檢", "验": "驗", "质": "質",
    "书": "書", "价": "價", "额": "額", "总": "總", "订": "訂", "务": "務", "构": "構", "虚": "虛",
    "达": "達", "际": "際", "币": "幣", "项": "項", "备": "備", "胶": "膠", "厂": "廠",
    "库": "庫", "间": "間", "条": "條", "无": "無", "长": "長", "钢": "鋼",
})


class OCRUnavailable(NeedsOCR):
    """沒有安裝 OCR 套件。"""


@lru_cache(maxsize=1)
def _engine():
    try:
        from rapidocr import RapidOCR
    except ImportError as e:
        raise OCRUnavailable("未安裝 rapidocr，請 pip install -e .[ocr]") from e
    return RapidOCR()


def page_image(pdf: pymupdf.Document, page: pymupdf.Page) -> np.ndarray:
    """掃描檔通常是一整頁的內嵌影像：直接取原圖，避免重新取樣讓字變糊。"""
    imgs = page.get_images()
    if len(imgs) == 1:
        pix = pymupdf.Pixmap(pdf, imgs[0][0])
        if pix.n - pix.alpha < 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    else:
        pix = page.get_pixmap(dpi=RENDER_DPI)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)


def ocr_boxes(img: np.ndarray) -> list[TextBox]:
    res = _engine()(img)
    if res.boxes is None:
        return []
    quads = [(np.asarray(b, dtype=float), t, float(s))
             for b, t, s in zip(res.boxes, res.txts, res.scores) if float(s) >= MIN_SCORE]
    angle = _skew_angle([q for q, t, _ in quads if len(t) >= 4])
    cx, cy = img.shape[1] / 2, img.shape[0] / 2
    cos, sin = math.cos(-angle), math.sin(-angle)
    boxes = []
    for q, txt, _ in quads:
        # 把四個角繞頁面中心反轉 skew 角度，同一列的字就會回到同一個高度
        xs = cx + (q[:, 0] - cx) * cos - (q[:, 1] - cy) * sin
        ys = cy + (q[:, 0] - cx) * sin + (q[:, 1] - cy) * cos
        boxes.append(TextBox(txt.translate(S2T).strip(), xs.min(), ys.min(), xs.max(), ys.max()))
    return boxes


def _skew_angle(quads: list[np.ndarray]) -> float:
    """取每個文字方塊上緣斜率的中位數（弧度）。印章等歪斜的方塊會被中位數排除。"""
    angles = []
    for q in quads:
        dx, dy = q[1, 0] - q[0, 0], q[1, 1] - q[0, 1]
        if dx > 20:
            angles.append(math.atan2(dy, dx))
    return statistics.median(angles) if angles else 0.0


def parse_scanned_pdf(path: Path) -> ParsedDocument:
    with pymupdf.open(path) as pdf:
        boxes = [b for page in pdf for b in ocr_boxes(page_image(pdf, page))]
    if not boxes:
        raise NeedsOCR(f"{path.name} OCR 沒有辨識出文字")
    tol = 0.6 * statistics.median(b.y1 - b.y0 for b in boxes)
    rows = group_rows(boxes, tol=max(tol, 4.0))
    text = "\n".join(" ".join(b.text for b in row) for row in rows)
    return build_document(text, boxes, parser="ocr", row_tol=max(tol, 4.0))
