"""把供應商五花八門的寫法統一：數字、日期、料號。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_ROC = re.compile(r"民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_YMD = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_ROC_SLASH = re.compile(r"^(\d{2,3})[/.](\d{1,2})[/.](\d{1,2})$")


def to_halfwidth(s: str) -> str:
    """全形轉半形（ＦＰ－ＴＭ → FP-TM），並去頭尾空白。"""
    return unicodedata.normalize("NFKC", s).strip()


def parse_decimal(v) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    s = to_halfwidth(str(v)).replace(",", "").replace("$", "").replace("NT", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_int(v) -> int | None:
    d = parse_decimal(v)
    if d is None or d != d.to_integral_value():
        return None
    return int(d)


def parse_date(v) -> date | None:
    """支援 2026-10-18、2026/10/12、民國115年10月18日、115/10/18、Excel 日期。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = to_halfwidth(str(v))
    if m := _ROC.search(s):
        y, mth, d = (int(x) for x in m.groups())
        return date(y + 1911, mth, d)
    if m := _YMD.search(s):
        y, mth, d = (int(x) for x in m.groups())
        return date(y, mth, d)
    if m := _ROC_SLASH.match(s):
        y, mth, d = (int(x) for x in m.groups())
        return date(y + 1911, mth, d)
    return None


_LOT_OCR = re.compile(r"^[L1lI|]\s*(\d{6})[\s\-~_—–]+(\d{2})$")


def normalize_lot(raw: str) -> str:
    """批號格式固定是 L＋6 位數字－2 位流水號（L261026-01）。

    OCR 常把開頭的 L 讀成 1、把 - 讀成 ~；因為格式已知，可以安全地還原。
    不符合格式的批號原樣保留，交給驗算與人工確認。
    """
    s = to_halfwidth(raw).strip()
    if m := _LOT_OCR.match(s):
        return f"L{m.group(1)}-{m.group(2)}"
    return s


def compact(part: str) -> str:
    """去掉連字號、空白並轉大寫，用來容忍 fphs3001 vs FP-HS-3001 這種寫法差異。"""
    return re.sub(r"[\s\-_./]", "", to_halfwidth(part)).upper()


# 掃描或手打最常見的字元混淆
_CONFUSABLE = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"})


@dataclass
class PartResolution:
    part_no: str | None
    method: str              # exact / vendor_map / compact / unknown
    suggestion: str | None = None


class PartResolver:
    """供應商料號 → 本公司 ERP 料號。

    只做「確定的」對應；猜得到但不確定的（例如 0 打成 O）只給建議、不自動改，交給人確認。
    """

    def __init__(self, part_nos: list[str], vendor_map: dict[tuple[str, str], str]):
        self.parts = set(part_nos)
        self.by_compact = {compact(p): p for p in part_nos}
        self.vendor_map = {(v, compact(raw)): p for (v, raw), p in vendor_map.items()}

    def resolve(self, raw: str, vendor_code: str | None) -> PartResolution:
        s = to_halfwidth(raw).upper()
        if s in self.parts:
            return PartResolution(s, "exact")
        c = compact(raw)
        if vendor_code and (vendor_code, c) in self.vendor_map:
            return PartResolution(self.vendor_map[(vendor_code, c)], "vendor_map")
        if c in self.by_compact:
            return PartResolution(self.by_compact[c], "compact")
        return PartResolution(None, "unknown", self._suggest(c, vendor_code))

    def _lookup(self, c: str, vendor_code: str | None) -> str | None:
        if vendor_code and (vendor_code, c) in self.vendor_map:
            return self.vendor_map[(vendor_code, c)]
        return self.by_compact.get(c)

    def _suggest(self, c: str, vendor_code: str | None) -> str | None:
        """一次只換一個易混淆字元（O→0、I→1…），剛好對到唯一一個料號才給建議。"""
        hits = set()
        for i, ch in enumerate(c):
            if ch in "OILSBZ":
                cand = c[:i] + ch.translate(_CONFUSABLE) + c[i + 1:]
                if (p := self._lookup(cand, vendor_code)) is not None:
                    hits.add(p)
        return hits.pop() if len(hits) == 1 else None
