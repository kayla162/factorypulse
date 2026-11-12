"""讀取 .eml：寄件者、主旨、日期、附件。

第一週從資料夾讀信；第二週改由 n8n 從信箱收信後呼叫同一個流程。
"""
from __future__ import annotations

import email
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path


@dataclass
class Attachment:
    filename: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Mail:
    path: Path              # 來源名稱（從 Mailpit 收的信是虛擬路徑）
    sha256: str
    sender: str
    sender_domain: str
    subject: str
    sent_at: datetime | None
    body: str
    attachments: list[Attachment] = field(default_factory=list)


def read_eml(path: Path) -> Mail:
    return parse_eml(path.read_bytes(), path)


def parse_eml(raw: bytes, path: Path) -> Mail:
    msg = email.message_from_bytes(raw, policy=policy.default)
    _, addr = parseaddr(str(msg.get("From", "")))
    try:
        sent = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
    except (TypeError, ValueError):
        sent = None
    body_part = msg.get_body(preferencelist=("plain", "html"))
    atts = [Attachment(part.get_filename(), part.get_payload(decode=True))
            for part in msg.iter_attachments() if part.get_filename()]
    return Mail(path=path, sha256=hashlib.sha256(raw).hexdigest(), sender=addr.lower(), sender_domain=addr.rsplit("@", 1)[-1].lower(),
                subject=str(msg.get("Subject", "")), sent_at=sent,
                body=body_part.get_content() if body_part else "", attachments=atts)


def read_inbox(folder: Path) -> list[Mail]:
    """依寄件時間排序：先到的出貨單先入帳，分批交貨的未交量才算得對。"""
    mails = [read_eml(p) for p in sorted(folder.glob("*.eml"))]
    return sorted(mails, key=lambda m: (m.sent_at is None, m.sent_at or datetime.min, m.path.name))
