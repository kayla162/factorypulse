"""拿 testdata 的標準答案評分：欄位正確率、異常偵測的精確率／召回率。

每次評測都從乾淨的資料庫開始跑完整流程，評的是「整條管線」而不是單一函式。
"""
from __future__ import annotations

import csv
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .classify import UNCLASSIFIED
from .db import doc_document, doc_line, init_db, mail_message, ops_anomaly, seed_erp
from .notify import OutboxNotifier
from .llm import default_client
from .pipeline import AUTO, run_inbox

HEADER_FIELDS = ["doc_type", "doc_no", "doc_date", "po_no", "ref_doc_no"]
PRICED_LINE_FIELDS = ["part_no", "qty", "unit_price"]
INSPECTION_LINE_FIELDS = ["part_no", "qty", "lot_no", "sample_qty", "defect_qty", "result"]


@dataclass
class EvalResult:
    total_docs: int = 0
    status_by_doc: dict[str, str] = field(default_factory=dict)
    field_total: int = 0
    field_correct: int = 0
    field_errors: list[str] = field(default_factory=list)
    held_correctly: list[str] = field(default_factory=list)
    field_by_parser: dict[str, list[int]] = field(default_factory=dict)   # parser → [正確, 總數]
    review_flags: list[str] = field(default_factory=list)                  # 驗算不過、送人工的文件
    expected: set = field(default_factory=set)
    predicted: set = field(default_factory=set)
    uncovered_docs: set = field(default_factory=set)
    run_ms: int = 0
    mail_total: int = 0
    mail_by_method: dict[str, list[int]] = field(default_factory=dict)   # method → [正確, 總數]
    mail_errors: list[str] = field(default_factory=list)
    mail_unclassified: list[str] = field(default_factory=list)
    mail_tokens: int = 0
    llm_name: str | None = None

    @property
    def covered_expected(self) -> set:
        return {e for e in self.expected if e[0] not in self.uncovered_docs}

    @property
    def tp(self) -> set:
        return self.expected & self.predicted

    @property
    def fp(self) -> set:
        return self.predicted - self.expected

    @property
    def fn(self) -> set:
        return self.covered_expected - self.predicted


def run_eval(engine: Engine, testdata: Path, llm=AUTO) -> EvalResult:
    client = default_client() if llm == AUTO else llm
    init_db(engine, reset=True)
    seed_erp(engine, testdata / "erp")
    with tempfile.TemporaryDirectory() as tmp:
        summary = run_inbox(engine, testdata / "emails", OutboxNotifier(Path(tmp) / "outbox", echo=False),
                            trigger="eval", attachments_dir=Path(tmp) / "attachments", llm=client)
    res = score(engine, testdata, summary.duration_ms)
    res.llm_name = client.name if client else None
    return res


def score_mail(res: "EvalResult", engine: Engine, testdata: Path) -> None:
    labels = {m["file"]: m["label"] for m in
              json.loads((testdata / "answers" / "emails.json").read_text(encoding="utf-8"))}
    with engine.connect() as conn:
        rows = conn.execute(select(mail_message.c.source_name, mail_message.c.label, mail_message.c.method,
                                   mail_message.c.llm_tokens)).all()
    for name, label, method, tokens in rows:
        if name not in labels:
            continue
        stat = res.mail_by_method.setdefault(method, [0, 0])
        stat[1] += 1
        res.mail_tokens += tokens or 0
        if label == labels[name]:
            stat[0] += 1
        elif label == UNCLASSIFIED:
            res.mail_unclassified.append(f"{name}（應為 {labels[name]}）")
        else:
            res.mail_errors.append(f"{name}：應為 {labels[name]}，分成 {label}")
    res.mail_total = len(labels)


def score(engine: Engine, testdata: Path, run_ms: int = 0) -> EvalResult:
    with open(testdata / "answers" / "manifest.csv", encoding="utf-8-sig", newline="") as f:
        manifest = list(csv.DictReader(f))
    res = EvalResult(total_docs=len(manifest), run_ms=run_ms)
    with engine.connect() as conn:
        docs = {r.attachment_name: r for r in conn.execute(select(doc_document))}
        lines: dict[int, list] = {}
        for r in conn.execute(select(doc_line).order_by(doc_line.c.document_id, doc_line.c.line_no)):
            lines.setdefault(r.document_id, []).append(r)
        anomalies = conn.execute(
            select(ops_anomaly.c.document_id, ops_anomaly.c.code, ops_anomaly.c.detail)).all()
    doc_id_by_db = {}
    for m in manifest:
        ans = json.loads((testdata / "answers" / f"{m['doc_id']}.json").read_text(encoding="utf-8"))
        res.expected |= {(m["doc_id"], a["code"]) for a in ans["expected_anomalies"]}
        row = docs.get(m["file"])
        res.status_by_doc[m["doc_id"]] = row.status if row else "missing"
        if row is None or row.status in ("needs_ocr", "failed"):
            res.uncovered_docs.add(m["doc_id"])
            continue
        doc_id_by_db[row.id] = m["doc_id"]
        before = (res.field_correct, res.field_total)
        _score_fields(res, m["doc_id"], ans, row, lines.get(row.id, []))
        acc = res.field_by_parser.setdefault(row.parser, [0, 0])
        acc[0] += res.field_correct - before[0]
        acc[1] += res.field_total - before[1]
    for d, code, detail in anomalies:
        if d not in doc_id_by_db:
            continue
        if code == "PARSE_CHECK_FAILED":
            # 不是業務異常，而是「系統讀不準、交給人」：另外列出，不算進精確率
            res.review_flags.append(f"{doc_id_by_db[d]}：{detail}")
        else:
            res.predicted.add((doc_id_by_db[d], code))
    score_mail(res, engine, testdata)
    return res


def _same(a, b) -> bool:
    if isinstance(a, (Decimal, float)) or isinstance(b, (Decimal, float)):
        return a is not None and b is not None and Decimal(str(a)) == Decimal(str(b))
    return str(a) == str(b)


def _score_fields(res: EvalResult, doc_id: str, ans: dict, row, got_lines: list) -> None:
    expect = dict(doc_type=ans["doc_type"], doc_no=ans["doc_no"], doc_date=ans["doc_date"],
                  po_no=ans.get("po_no"), ref_doc_no=ans.get("ref_delivery"))
    for f in HEADER_FIELDS:
        res.field_total += 1
        got = getattr(row, f)
        got = got.isoformat() if hasattr(got, "isoformat") else got
        if _same(got, expect[f]):
            res.field_correct += 1
        else:
            res.field_errors.append(f"{doc_id}.{f}: 應為 {expect[f]}，抓到 {got}")
    fields = INSPECTION_LINE_FIELDS if ans["doc_type"] == "inspection" else PRICED_LINE_FIELDS
    key = {"sample_qty": "sample", "defect_qty": "defects"}
    # 料號打錯的行，正確行為是「不自動改、擋下給人確認」，不算欄位錯誤
    held_parts = {a["part_no"] for a in ans["expected_anomalies"] if a["code"] == "PART_UNKNOWN"}
    for i, item in enumerate(ans["items"]):
        got = got_lines[i] if i < len(got_lines) else None
        for f in fields:
            res.field_total += 1
            exp = item.get(key.get(f, f))
            val = getattr(got, f) if got is not None else None
            if f == "part_no" and got is not None and val is None:
                if exp in held_parts:
                    res.field_correct += 1
                    res.held_correctly.append(f"{doc_id} 第{i + 1}行 {got.raw_part_no}（應為 {exp}）")
                    continue
                val = f"(未對應:{got.raw_part_no})"
            if _same(val, exp):
                res.field_correct += 1
            else:
                res.field_errors.append(f"{doc_id} 第{i + 1}行.{f}: 應為 {exp}，抓到 {val}")


def to_markdown(r: EvalResult, title: str = "batch01") -> str:
    pct = lambda a, b: f"{a / b:.1%}" if b else "—"  # noqa: E731
    covered = r.total_docs - len(r.uncovered_docs)
    statuses: dict[str, int] = {}
    for s in r.status_by_doc.values():
        statuses[s] = statuses.get(s, 0) + 1
    tp, fp, fn = len(r.tp), len(r.fp), len(r.fn)
    out = [
        f"# 評測報告：{title}",
        "",
        f"產生時間：{datetime.now():%Y-%m-%d %H:%M}　整批處理耗時：{r.run_ms / 1000:.1f} 秒"
        f"　LLM：{r.llm_name or '未啟用'}",
        "",
        "## 涵蓋範圍",
        "",
        f"- 文件 {r.total_docs} 份，可自動處理 {covered} 份（{pct(covered, r.total_docs)}）",
        "- 狀態：" + "、".join(f"{k} {v}" for k, v in sorted(statuses.items())),
        f"- 無法自動處理：{len(r.uncovered_docs)} 份" + (f"（{'、'.join(sorted(r.uncovered_docs))}）" if r.uncovered_docs else ""),
        "",
        "## 欄位正確率（只算已處理的文件）",
        "",
        f"- {r.field_correct}/{r.field_total} = **{pct(r.field_correct, r.field_total)}**",
        "- 依解析方式：" + "、".join(f"{p} {c}/{t}（{pct(c, t)}）" for p, (c, t) in sorted(r.field_by_parser.items())),
    ]
    out += [f"  - 錯誤：{e}" for e in r.field_errors[:20]]
    out += [f"  - 正確擋下（料號打錯，不自動修正、送人工確認）：{e}" for e in r.held_correctly]
    out += [
        "",
        "## 異常偵測",
        "",
        "| 指標 | 數值 |",
        "|---|---|",
        f"| 埋入異常總數 | {len(r.expected)} |",
        f"| 其中落在已處理文件 | {len(r.covered_expected)} |",
        f"| 抓到（TP） | {tp} |",
        f"| 誤報（FP） | {fp} |",
        f"| 漏抓（FN，已處理文件中） | {fn} |",
        f"| 精確率 | {pct(tp, tp + fp)} |",
        f"| 召回率（已處理文件） | {pct(tp, tp + fn)} |",
        f"| 召回率（全部，含未涵蓋） | {pct(tp, len(r.expected))} |",
        "",
    ]
    for label, items in (("誤報", r.fp), ("漏抓", r.fn)):
        if items:
            out.append(f"{label}：" + "、".join(f"{d} {c}" for d, c in sorted(items)))
    not_yet = sorted(e for e in r.expected if e[0] in r.uncovered_docs)
    if not_yet:
        out.append("未涵蓋文件中的異常：" + "、".join(f"{d} {c}" for d, c in not_yet))
    if r.review_flags:
        out += ["", "## 系統讀不準、主動送人工確認", ""]
        out += [f"- {x}" for x in sorted(r.review_flags)]
    if r.mail_total:
        correct = sum(c for c, _ in r.mail_by_method.values())
        method_zh = {"rule": "規則", "llm": "LLM", "none": "未分類（無 LLM）"}
        out += ["", "## 信件分類", "",
                f"- 正確 {correct}/{r.mail_total}（{pct(correct, r.mail_total)}），"
                f"分錯 {len(r.mail_errors)}、轉人工 {len(r.mail_unclassified)}",
                "- 依方法：" + "、".join(f"{method_zh.get(m, m)} {c}/{t}" for m, (c, t) in sorted(r.mail_by_method.items())),
                f"- LLM token 用量：{r.mail_tokens:,}"]
        out += [f"  - 分錯：{e}" for e in r.mail_errors]
        out += [f"  - 轉人工：{e}" for e in r.mail_unclassified]
    return "\n".join(out) + "\n"
