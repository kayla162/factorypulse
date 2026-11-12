"""使用追蹤：自動化率、人工介入率、失敗率、省下的時間。

省時 = 「全部人工處理要花的時間」−「導入後仍需人工的時間」
人工時間基準存在 ops_time_baseline，預設是假設值；導入時應以現場計時結果更新（並記錄來源）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import and_, select, true
from sqlalchemy.engine import Engine

from .classify import UNCLASSIFIED
from .db import doc_document, mail_message, ops_anomaly, ops_run_log, ops_time_baseline

DOC_TYPES = ("quote", "delivery", "inspection")
POSTED = ("posted", "posted_with_alerts")
MANUAL = ("needs_review", "needs_ocr", "failed")


@dataclass
class Stats:
    period: str
    mails: int = 0
    mails_by_method: dict[str, int] = field(default_factory=dict)
    mails_by_label: dict[str, int] = field(default_factory=dict)
    docs: int = 0
    docs_by_status: dict[str, int] = field(default_factory=dict)
    anomalies_by_code: dict[str, int] = field(default_factory=dict)
    runs: int = 0
    runs_failed: int = 0
    run_seconds: float = 0.0
    llm_tokens: int = 0
    manual_minutes: float = 0.0       # 全人工要花的時間
    remaining_minutes: float = 0.0    # 導入後仍需人工的時間
    baseline: dict[str, tuple[float, str, str]] = field(default_factory=dict)  # task → (分鐘, 說明, 來源)

    @property
    def saved_minutes(self) -> float:
        return self.manual_minutes - self.remaining_minutes

    @property
    def docs_auto(self) -> int:
        return sum(self.docs_by_status.get(s, 0) for s in POSTED)

    @property
    def docs_manual(self) -> int:
        return sum(self.docs_by_status.get(s, 0) for s in MANUAL)

    @property
    def auto_rate(self) -> float | None:
        return self.docs_auto / self.docs if self.docs else None

    @property
    def doc_intervention_rate(self) -> float | None:
        return self.docs_manual / self.docs if self.docs else None

    @property
    def mail_intervention_rate(self) -> float | None:
        return self.mails_by_label.get(UNCLASSIFIED, 0) / self.mails if self.mails else None

    @property
    def failure_rate(self) -> float | None:
        return self.docs_by_status.get("failed", 0) / self.docs if self.docs else None

    @property
    def baseline_is_assumed(self) -> bool:
        return any("假設" in src for _, _, src in self.baseline.values())


def compute(engine: Engine, day: date | None = None) -> Stats:
    """day=None 代表全部期間。"""
    def in_day(col):
        if day is None:
            return true()
        start = datetime.combine(day, time.min)
        return and_(col >= start, col < start + timedelta(days=1))

    s = Stats(period=day.isoformat() if day else "全部")
    with engine.connect() as c:
        s.baseline = {r.task: (float(r.minutes), r.description or "", r.source)
                      for r in c.execute(select(ops_time_baseline))}
        mails = c.execute(select(mail_message.c.label, mail_message.c.method, mail_message.c.llm_tokens)
                          .where(in_day(mail_message.c.created_at))).all()
        docs = c.execute(select(doc_document.c.doc_type, doc_document.c.status)
                         .where(in_day(doc_document.c.created_at))).all()
        anomalies = c.execute(select(ops_anomaly.c.code).where(in_day(ops_anomaly.c.created_at))).scalars().all()
        runs = c.execute(select(ops_run_log.c.status, ops_run_log.c.duration_ms)
                         .where(in_day(ops_run_log.c.started_at))).all()

    for label, method, tokens in mails:
        s.mails += 1
        s.mails_by_method[method] = s.mails_by_method.get(method, 0) + 1
        s.mails_by_label[label] = s.mails_by_label.get(label, 0) + 1
        s.llm_tokens += tokens or 0
    for code in anomalies:
        s.anomalies_by_code[code] = s.anomalies_by_code.get(code, 0) + 1
    s.runs = len(runs)
    s.runs_failed = sum(st == "failed" for st, _ in runs)
    s.run_seconds = sum((ms or 0) for _, ms in runs) / 1000

    b = {k: v[0] for k, v in s.baseline.items()}
    avg_doc = sum(b.get(t, 0) for t in DOC_TYPES) / len(DOC_TYPES)
    s.manual_minutes = s.mails * b.get("mail_triage", 0)
    s.remaining_minutes = s.mails_by_label.get(UNCLASSIFIED, 0) * b.get("mail_triage", 0)
    for doc_type, status in docs:
        if status == "duplicate":
            continue
        s.docs += 1
        s.docs_by_status[status] = s.docs_by_status.get(status, 0) + 1
        full = b.get(doc_type, avg_doc)
        s.manual_minutes += full
        if status == "needs_review":
            s.remaining_minutes += b.get("review", 0)
        elif status in ("needs_ocr", "failed"):
            s.remaining_minutes += full          # 系統沒處理到，等於全部人工
    return s


def to_text(s: Stats) -> str:
    pct = lambda v: "—" if v is None else f"{v:.0%}"  # noqa: E731
    lines = [
        f"FactoryPulse 營運摘要（{s.period}）",
        f"信件 {s.mails} 封：規則 {s.mails_by_method.get('rule', 0)}、LLM {s.mails_by_method.get('llm', 0)}、"
        f"待人工分類 {s.mails_by_label.get(UNCLASSIFIED, 0)}",
        f"文件 {s.docs} 份：自動入帳 {s.docs_auto}（{pct(s.auto_rate)}）、需人工 {s.docs_manual}（{pct(s.doc_intervention_rate)}）",
        f"異常 {sum(s.anomalies_by_code.values())} 筆",
        f"省下約 {s.saved_minutes:.0f} 分鐘（全人工 {s.manual_minutes:.0f} → 剩 {s.remaining_minutes:.0f}）"
        + ("，人工時間為假設值" if s.baseline_is_assumed else ""),
    ]
    return "\n".join(lines)


def update_baseline(engine: Engine, task: str, minutes: float, source: str) -> None:
    from sqlalchemy import update
    with engine.begin() as c:
        n = c.execute(update(ops_time_baseline).where(ops_time_baseline.c.task == task).values(
            minutes=Decimal(str(minutes)), source=source, updated_at=datetime.now())).rowcount
    if n == 0:
        raise KeyError(f"沒有這個工作項目：{task}")
