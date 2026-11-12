"""通知：文件異常（每份文件、每個負責部門一則）與信件通知（客訴、交期變更、待分類）。

OutboxNotifier 寫入 outbox/（JSON Lines）；API 會把同一批通知回傳給 n8n，由 n8n 推送 LINE。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from . import config

if TYPE_CHECKING:
    from .classify import Classification
    from .mail import Mail

DOC_TYPE_ZH = {"quote": "報價單", "delivery": "出貨單", "inspection": "檢驗報告"}


@dataclass
class Notice:
    owner: str            # 部門
    recipient: str
    title: str
    lines: list[str]
    ref: str              # doc:<id> 或 mail:<來源>
    urgent: bool = False

    def text(self) -> str:
        return "\n".join([self.title, *[f"・{x}" for x in self.lines], f"負責：{self.recipient}"])

    def to_dict(self) -> dict:
        return dict(asdict(self), text=self.text())


class Notifier(Protocol):
    def send(self, notice: Notice) -> None: ...


class OutboxNotifier:
    def __init__(self, folder: Path | None = None, echo: bool = True):
        self.folder = folder or config.OUTBOX_DIR
        self.echo = echo
        self.sent: list[Notice] = []

    def send(self, notice: Notice) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{datetime.now():%Y%m%d}.jsonl"
        rec = dict(at=datetime.now().isoformat(timespec="seconds"), **notice.to_dict())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.sent.append(notice)
        if self.echo:
            print(notice.text(), end="\n\n")


def build_notices(document_id: int, vendor_name: str, doc_type: str, doc_no: str | None,
                  po_no: str | None, held: bool, anomalies: list[dict]) -> list[Notice]:
    by_owner: dict[str, list[dict]] = {}
    for a in anomalies:
        by_owner.setdefault(a["owner"], []).append(a)
    head = f"{vendor_name} {DOC_TYPE_ZH.get(doc_type, doc_type)} {doc_no or ''}"
    if po_no:
        head += f"（採購單 {po_no}）"
    tag = "【待確認・未入帳】" if held else "【異常提醒】"
    notices = []
    for owner, items in by_owner.items():
        lines = [f"{config.ANOMALY_RULES[a['code']][0]}" + (f" {a['part_no']}" if a["part_no"] else "")
                 + f"：{a['detail']}" for a in items]
        notices.append(Notice(owner, config.OWNERS.get(owner, owner), f"{tag}{head}", lines, f"doc:{document_id}"))
    return notices


def build_mail_notice(mail: "Mail", c: "Classification", vendor: dict | None,
                      verified_due: date | None) -> Notice | None:
    """沒有單據附件的信：客訴要馬上通知品保；供應商交期變更通知採購；判斷不了的交給人。"""
    who = vendor["vendor_name"] if vendor else mail.sender
    summary = c.extracted.get("summary") or c.reason
    if c.label == "客訴":
        return Notice("品保", config.OWNERS["品保"], f"【客訴】{mail.subject}",
                      [summary, f"來信：{mail.sender}"], f"mail:{mail.path.name}", urgent=True)
    if c.label == "採購" and c.extracted.get("new_due_date"):
        po = c.extracted.get("po_no")
        if po and verified_due:
            due_line = f"採購單 {po} {c.extracted.get('part_no', '')}：原交期 {verified_due} → 新交期 {c.extracted['new_due_date']}"
        else:
            due_line = f"新交期 {c.extracted['new_due_date']}（模型抽取的採購單號 {po or '無'} 在 ERP 查無對應，請人工確認）"
        return Notice("採購", config.OWNERS["採購"], f"【交期變更】{who}：{mail.subject}",
                      [summary, due_line], f"mail:{mail.path.name}")
    if c.label == "品保":
        return Notice("品保", config.OWNERS["品保"], f"【品保來信】{who}：{mail.subject}",
                      [summary], f"mail:{mail.path.name}")
    if c.label == "待人工分類":
        return Notice("採購", config.OWNERS["採購"], f"【待分類】{mail.subject}",
                      [c.reason, f"來信：{mail.sender}"], f"mail:{mail.path.name}")
    return None
