"""主流程：收信 → 解析附件 → 料號對應 → 驗算與比對 → 寫入 ERP → 信件分類 → 通知 → 執行紀錄。

每份文件一個交易：一份出錯不影響其他份。命令列（資料夾收信）與 API（Mailpit／n8n）共用 Pipeline。
"""
from __future__ import annotations

import json
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection, Engine

from . import config
from .classify import UNCLASSIFIED, Classification, MailClassifier
from .db import (doc_document, doc_line, erp_goods_receipt, erp_incoming_inspection, erp_part,
                 erp_purchase_order, erp_vendor, erp_vendor_part_map, mail_message, ops_anomaly, ops_run_log)
from .llm import LLMClient, default_client
from .mail import Attachment, Mail, read_inbox
from .models import Anomaly, NeedsOCR, ParsedDocument, ParseError
from .normalize import PartResolver
from .notify import Notice, Notifier, OutboxNotifier, build_mail_notice, build_notices
from .parsers import SUPPORTED, parse_document
from .rules import ErpView, PoLine, check_business, check_parse

AUTO = "auto"


@dataclass
class DocResult:
    attachment: str
    status: str
    document_id: int | None = None
    doc_type: str | None = None
    doc_no: str | None = None
    anomalies: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class MailResult:
    source: str
    subject: str
    duplicate: bool = False
    classification: Classification | None = None
    documents: list[DocResult] = field(default_factory=list)


@dataclass
class RunSummary:
    run_id: str
    mails: list[MailResult] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def results(self) -> list[DocResult]:
        return [d for m in self.mails for d in m.documents]

    @property
    def emails_seen(self) -> int:
        return len(self.mails)

    @property
    def emails_skipped(self) -> int:
        return sum(m.duplicate for m in self.mails)

    def count(self, *statuses: str) -> int:
        return sum(r.status in statuses for r in self.results)

    def mail_count(self, method: str | None = None, label: str | None = None) -> int:
        return sum(1 for m in self.mails if m.classification
                   and (method is None or m.classification.method == method)
                   and (label is None or m.classification.label == label))

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "duration_ms": self.duration_ms,
            "emails": self.emails_seen, "duplicates": self.emails_skipped,
            "documents": {s: self.count(s) for s in
                          ("posted", "posted_with_alerts", "needs_review", "needs_ocr", "failed", "duplicate")},
            "mails": [{"source": m.source, "subject": m.subject, "duplicate": m.duplicate,
                       "label": m.classification.label if m.classification else None,
                       "method": m.classification.method if m.classification else None,
                       "documents": [d.__dict__ for d in m.documents]} for m in self.mails],
            "notices": [n.to_dict() for n in self.notices],
        }


class Pipeline:
    def __init__(self, engine: Engine, notifier: Notifier | None = None,
                 llm: LLMClient | None | str = AUTO, attachments_dir: Path | None = None,
                 trigger: str = "cli"):
        self.engine = engine
        self.notifier = notifier or OutboxNotifier()
        self.classifier = MailClassifier(engine, default_client() if llm == AUTO else llm)
        self.attachments_dir = attachments_dir or config.DATA_DIR / "attachments"
        self.trigger = trigger

    # ------------------------------------------------------------ 一批信
    def run(self, mails: Iterable[Mail]) -> RunSummary:
        summary = RunSummary(run_id=f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}")
        t0 = time.perf_counter()
        with self.engine.begin() as conn:
            conn.execute(insert(ops_run_log).values(run_id=summary.run_id, trigger=self.trigger,
                                                    started_at=datetime.now(), status="running"))
        status, error = "ok", None
        try:
            vendors = self._vendors()
            for mail in mails:
                summary.mails.append(self.process_mail(summary, mail, vendors))
        except Exception:  # 記錄後再拋出，執行紀錄不會停在 running
            status, error = "failed", traceback.format_exc()
            raise
        finally:
            summary.duration_ms = int((time.perf_counter() - t0) * 1000)
            self._finish_run(summary, status, error)
        return summary

    # ------------------------------------------------------------ 一封信
    def process_mail(self, summary: RunSummary, mail: Mail, vendors: dict[str, dict]) -> MailResult:
        res = MailResult(mail.path.name, mail.subject)
        with self.engine.connect() as conn:
            if conn.execute(select(mail_message.c.id).where(mail_message.c.file_sha256 == mail.sha256)).first():
                res.duplicate = True
                return res
        vendor = vendors.get(mail.sender_domain)
        if vendor:
            for att in mail.attachments:
                if Path(att.filename).suffix.lower() in SUPPORTED:
                    res.documents.append(self.process_attachment(summary, mail, att, vendor))
        doc_types = [d.doc_type for d in res.documents if d.doc_type and d.status != "failed"]
        res.classification = self.classifier.classify(mail, doc_types)
        self._save_mail(summary.run_id, mail, res.classification)
        if res.classification.method != "rule":
            if notice := build_mail_notice(mail, res.classification, vendor, self._po_due(res.classification)):
                self._send(summary, notice)
        return res

    # ------------------------------------------------------------ 一份附件
    def process_attachment(self, summary: RunSummary, mail: Mail, att: Attachment, vendor: dict) -> DocResult:
        t0 = time.perf_counter()
        sha = att.sha256
        with self.engine.connect() as conn:
            dup = conn.execute(select(doc_document.c.doc_type, doc_document.c.doc_no)
                               .where(doc_document.c.file_sha256 == sha)).first()
            if dup:
                return DocResult(att.filename, "duplicate", doc_type=dup.doc_type, doc_no=dup.doc_no)

        path = _save_attachment(att, self.attachments_dir)
        base = dict(run_id=summary.run_id, source_email=mail.path.name, attachment_name=att.filename,
                    file_sha256=sha, vendor_code=vendor["vendor_code"], created_at=datetime.now())
        try:
            parsed = parse_document(path)
        except NeedsOCR as e:
            return self._record_unparsed(base, "needs_ocr", str(e), att.filename, t0)
        except ParseError as e:
            return self._record_unparsed(base, "failed", str(e), att.filename, t0)

        with self.engine.begin() as conn:
            # 同一張單換檔名重寄（內容不同、單號相同）也視為重複
            same = conn.execute(select(doc_document.c.id).where(
                doc_document.c.vendor_code == vendor["vendor_code"],
                doc_document.c.doc_type == parsed.doc_type,
                doc_document.c.doc_no == parsed.doc_no,
                doc_document.c.status != "failed")).first()
            if same:
                return DocResult(att.filename, "duplicate", doc_type=parsed.doc_type, doc_no=parsed.doc_no)

            anomalies = _resolve_parts(conn, parsed, vendor["vendor_code"])
            anomalies += check_parse(parsed)
            anomalies += check_business(parsed, vendor["vendor_code"], _erp_view(conn, parsed))
            rows = [_anomaly_row(a) for a in anomalies]
            held = any(r["severity"] == "review" for r in rows)
            status = "needs_review" if held else ("posted_with_alerts" if rows else "posted")

            doc_id = conn.execute(insert(doc_document).values(
                **base, doc_type=parsed.doc_type, doc_no=parsed.doc_no, doc_date=parsed.doc_date,
                po_no=parsed.po_no, ref_doc_no=parsed.ref_doc_no, parser=parsed.parser, status=status,
                subtotal=parsed.subtotal, tax=parsed.tax, total=parsed.total,
            )).inserted_primary_key[0]
            conn.execute(insert(doc_line), [
                dict(document_id=doc_id, line_no=i, raw_part_no=ln.raw_part_no, part_no=ln.part_no,
                     description=ln.description, qty=ln.qty, unit_price=ln.unit_price, amount=ln.amount,
                     lot_no=ln.lot_no, sample_qty=ln.sample_qty, defect_qty=ln.defect_qty, result=ln.result)
                for i, ln in enumerate(parsed.lines, 1)])
            if rows:
                now = datetime.now()
                conn.execute(insert(ops_anomaly), [dict(r, document_id=doc_id, created_at=now) for r in rows])
            if not held:
                _post_to_erp(conn, doc_id, parsed)
            conn.execute(update(doc_document).where(doc_document.c.id == doc_id).values(
                processing_ms=int((time.perf_counter() - t0) * 1000)))

        if rows:
            for n in build_notices(doc_id, vendor["vendor_name"], parsed.doc_type, parsed.doc_no,
                                   parsed.po_no, held, rows):
                self._send(summary, n)
            with self.engine.begin() as conn:
                conn.execute(update(ops_anomaly).where(ops_anomaly.c.document_id == doc_id)
                             .values(notified_at=datetime.now()))
        return DocResult(att.filename, status, doc_id, parsed.doc_type, parsed.doc_no, [r["code"] for r in rows])

    # ------------------------------------------------------------ 內部步驟
    def _send(self, summary: RunSummary, notice: Notice) -> None:
        self.notifier.send(notice)
        summary.notices.append(notice)

    def _vendors(self) -> dict[str, dict]:
        with self.engine.connect() as conn:
            return {r.email_domain.lower(): dict(r._mapping) for r in conn.execute(select(erp_vendor))}

    def _record_unparsed(self, base: dict, status: str, error: str, name: str, t0: float) -> DocResult:
        with self.engine.begin() as conn:
            doc_id = conn.execute(insert(doc_document).values(
                **base, status=status, error=error,
                processing_ms=int((time.perf_counter() - t0) * 1000))).inserted_primary_key[0]
        return DocResult(name, status, doc_id, error=error)

    def _po_due(self, c: Classification) -> date | None:
        """LLM 抽出的採購單號要回 ERP 查證，查不到就不採信。"""
        po_no, part_no = c.extracted.get("po_no"), c.extracted.get("part_no")
        if not po_no:
            return None
        q = select(erp_purchase_order.c.due_date).where(erp_purchase_order.c.po_no == po_no)
        if part_no:
            q = q.where(erp_purchase_order.c.part_no == part_no)
        with self.engine.connect() as conn:
            return conn.execute(q).scalars().first()

    def _save_mail(self, run_id: str, mail: Mail, c: Classification) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(mail_message).values(
                run_id=run_id, file_sha256=mail.sha256, source_name=mail.path.name, sender=mail.sender,
                subject=mail.subject[:400], sent_at=mail.sent_at.replace(tzinfo=None) if mail.sent_at else None,
                attachments=len(mail.attachments), label=c.label, method=c.method, confidence=c.confidence,
                reason=c.reason[:400], extracted=json.dumps(c.extracted, ensure_ascii=False),
                tool_calls=",".join(c.tool_calls)[:400], llm_calls=c.llm_calls, llm_tokens=c.tokens,
                elapsed_ms=c.elapsed_ms, created_at=datetime.now()))

    def _finish_run(self, s: RunSummary, status: str, error: str | None) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(ops_run_log).where(ops_run_log.c.run_id == s.run_id).values(
                finished_at=datetime.now(), status=status, error=error,
                emails_seen=s.emails_seen, emails_skipped=s.emails_skipped,
                emails_rule=s.mail_count("rule"), emails_llm=s.mail_count("llm"),
                emails_unclassified=s.mail_count(label=UNCLASSIFIED),
                llm_calls=sum(m.classification.llm_calls for m in s.mails if m.classification),
                llm_tokens=sum(m.classification.tokens for m in s.mails if m.classification),
                documents_seen=len(s.results),
                documents_posted=s.count("posted", "posted_with_alerts"),
                documents_review=s.count("needs_review"),
                documents_needs_ocr=s.count("needs_ocr"),
                documents_failed=s.count("failed"),
                documents_duplicate=s.count("duplicate"),
                anomalies=sum(len(r.anomalies) for r in s.results),
                duration_ms=s.duration_ms))


def run_inbox(engine: Engine, inbox: Path, notifier: Notifier | None = None, trigger: str = "cli",
              attachments_dir: Path | None = None, llm: LLMClient | None | str = AUTO) -> RunSummary:
    return Pipeline(engine, notifier, llm, attachments_dir, trigger).run(read_inbox(inbox))


def _save_attachment(att: Attachment, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{att.sha256[:12]}_{Path(att.filename).name}"
    if not path.exists():
        path.write_bytes(att.data)
    return path


def _resolve_parts(conn: Connection, doc: ParsedDocument, vendor_code: str) -> list[Anomaly]:
    resolver = PartResolver(
        list(conn.execute(select(erp_part.c.part_no)).scalars()),
        {(r.vendor_code, r.vendor_part_no): r.part_no for r in conn.execute(select(erp_vendor_part_map))})
    out = []
    for ln in doc.lines:
        res = resolver.resolve(ln.raw_part_no, vendor_code)
        ln.part_no = res.part_no
        if res.part_no is None:
            hint = f"，疑似 {res.suggestion} 打錯" if res.suggestion else ""
            out.append(Anomaly("PART_UNKNOWN", f"料號 {ln.raw_part_no} 不在料號主檔或對照表{hint}",
                               res.suggestion))
    return out


def _erp_view(conn: Connection, doc: ParsedDocument) -> ErpView:
    po_lines: dict[str, list[PoLine]] = {}
    if doc.po_no:
        received = dict(conn.execute(
            select(erp_goods_receipt.c.po_line, func.sum(erp_goods_receipt.c.qty))
            .where(erp_goods_receipt.c.po_no == doc.po_no)
            .group_by(erp_goods_receipt.c.po_line)).all())
        for r in conn.execute(select(erp_purchase_order).where(erp_purchase_order.c.po_no == doc.po_no)):
            po_lines.setdefault(r.po_no, []).append(PoLine(
                r.po_no, r.line, r.vendor_code, r.part_no, r.qty, r.unit_price, r.due_date,
                int(received.get(r.line) or 0)))
    prices = dict(conn.execute(select(erp_part.c.part_no, erp_part.c.current_unit_price)).all())
    return ErpView(po_lines, prices)


def _anomaly_row(a: Anomaly) -> dict:
    _, severity, owner = config.ANOMALY_RULES[a.code]
    return dict(code=a.code, severity=severity, owner=owner, part_no=a.part_no, detail=a.detail)


def _post_to_erp(conn: Connection, doc_id: int, doc: ParsedDocument) -> None:
    now = datetime.now()
    if doc.doc_type == "delivery":
        po = {r.part_no: r for r in conn.execute(
            select(erp_purchase_order).where(erp_purchase_order.c.po_no == doc.po_no))}
        conn.execute(insert(erp_goods_receipt), [
            dict(document_id=doc_id, po_no=doc.po_no, po_line=po[ln.part_no].line, part_no=ln.part_no,
                 qty=ln.qty, unit_price=ln.unit_price, receipt_date=doc.doc_date, posted_at=now)
            for ln in doc.lines])
    elif doc.doc_type == "inspection":
        conn.execute(insert(erp_incoming_inspection), [
            dict(document_id=doc_id, po_no=doc.po_no, delivery_no=doc.ref_doc_no, part_no=ln.part_no,
                 lot_no=ln.lot_no, qty=ln.qty, sample_qty=ln.sample_qty, defect_qty=ln.defect_qty,
                 vendor_result=ln.result, inspected_on=doc.doc_date, posted_at=now)
            for ln in doc.lines])
    # 報價單只留存與比價，不動 ERP 單價（改價要走核准流程）
