"""比對 Power Automate Desktop 流程與 Python 解析的結果。

PAD 流程（pad/出貨單匯入.txt）把宇碩 Excel 出貨單逐列寫進 pad_delivery_staging；
這裡用 Python 解析同一批檔案，逐欄比對，並看 PAD＋SQL 檢查能不能抓到和 Python 一樣的異常。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from .db import erp_part, erp_purchase_order, erp_vendor_part_map, pad_delivery_staging
from .normalize import PartResolver, parse_date, parse_decimal, parse_int
from .parsers.excel import parse_excel


@dataclass
class PadCompare:
    run_tag: str | None
    files: list[str] = field(default_factory=list)
    fields_total: int = 0
    fields_same: int = 0
    diffs: list[str] = field(default_factory=list)
    pad_findings: list[str] = field(default_factory=list)   # PAD＋SQL 檢查找到的問題
    missing_files: list[str] = field(default_factory=list)  # Python 認得是出貨單、PAD 卻沒讀到


def latest_run(engine: Engine) -> str | None:
    with engine.connect() as c:
        return c.execute(select(func.max(pad_delivery_staging.c.run_tag))).scalar()


def compare(engine: Engine, documents: Path, run_tag: str | None = None) -> PadCompare:
    run_tag = run_tag or latest_run(engine)
    res = PadCompare(run_tag)
    with engine.connect() as c:
        rows = c.execute(select(pad_delivery_staging).where(pad_delivery_staging.c.run_tag == run_tag)
                         .order_by(pad_delivery_staging.c.id)).all() if run_tag else []
        resolver = PartResolver(list(c.execute(select(erp_part.c.part_no)).scalars()),
                                {(r.vendor_code, r.vendor_part_no): r.part_no
                                 for r in c.execute(select(erp_vendor_part_map))})
        po = {(r.po_no, r.part_no): r for r in c.execute(select(erp_purchase_order))}

    by_file: dict[str, list] = {}
    for r in rows:
        by_file.setdefault(r.file_name, []).append(r)
    res.files = sorted(by_file)

    for path in sorted(documents.glob("*.xlsx")):
        doc = parse_excel(path)
        if doc.doc_type != "delivery":
            continue
        got = by_file.get(path.name)
        if not got:
            res.missing_files.append(path.name)
            continue
        if len(got) != len(doc.lines):
            res.diffs.append(f"{path.name}：明細 PAD {len(got)} 列、Python {len(doc.lines)} 列")
        for i, (p, ln) in enumerate(zip(got, doc.lines), 1):
            checks = [("單號", p.doc_no, doc.doc_no), ("日期", parse_date(p.doc_date_text), doc.doc_date),
                      ("採購單", p.po_no, doc.po_no), ("料號", (p.vendor_part_no or "").strip(), ln.raw_part_no),
                      ("數量", parse_int(p.qty_text), ln.qty), ("單價", parse_decimal(p.unit_price_text), ln.unit_price)]
            for name, a, b in checks:
                res.fields_total += 1
                if a == b:
                    res.fields_same += 1
                else:
                    res.diffs.append(f"{path.name} 第{i}行 {name}：PAD「{a}」≠ Python「{b}」")

    # PAD 只負責搬資料；異常要靠 pad/檢查.sql 這類查詢。這裡用同樣的邏輯模擬那份 SQL 的結果
    for r in rows:
        part = resolver.resolve(r.vendor_part_no or "", "V003").part_no
        if part is None:
            res.pad_findings.append(f"{r.file_name}：料號 {r.vendor_part_no} 對不到")
            continue
        line = po.get((r.po_no, part))
        if line is None:
            res.pad_findings.append(f"{r.file_name}：{part} 不在採購單 {r.po_no}")
            continue
        price = parse_decimal(r.unit_price_text)
        if price is not None and abs(price - line.unit_price) > Decimal("0.0001"):
            res.pad_findings.append(f"{r.file_name}：{part} 單價 {price} ≠ 採購 {line.unit_price}")
    return res


def to_markdown(r: PadCompare) -> str:
    out = [f"# PAD 與 Python 結果比對（批次 {r.run_tag or '無'}）", ""]
    if not r.run_tag:
        return "\n".join(out + ["資料庫裡還沒有 PAD 寫入的資料，請先執行 pad/ 的流程。", ""])
    out += [f"- PAD 讀到的出貨單：{len(r.files)} 份（{'、'.join(r.files)}）",
            f"- 欄位一致：{r.fields_same}/{r.fields_total}",
            f"- PAD 漏讀的出貨單：{'、'.join(r.missing_files) or '無'}",
            "", "## 不一致的欄位", ""]
    out += [f"- {d}" for d in r.diffs] or ["- 無"]
    out += ["", "## PAD＋SQL 檢查找到的問題", ""]
    out += [f"- {d}" for d in r.pad_findings] or ["- 無"]
    return "\n".join(out) + "\n"
