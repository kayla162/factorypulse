"""產線流程：ETL → 良率 p-chart → 新的失控點做批號回查 → 合併成事件通知品保。"""
from __future__ import annotations

import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from . import config
from .db import mart_wo_yield, mart_yield_daily, mes_yield_signal, ops_run_log
from .etl import EtlResult, run_etl
from .notify import Notice, Notifier, OutboxNotifier
from .spc import RULE_ZH, ChartPoint, p_chart
from .trace import RootCause, investigate

INCIDENT_GAP_DAYS = 4   # 同一條線的失控點相隔 4 天內（跨週末）視為同一事件


@dataclass
class Signal:
    line: str
    prod_date: date
    rules: list[str]
    point: ChartPoint
    cause: RootCause


@dataclass
class MesSummary:
    run_id: str
    etl: EtlResult
    new_signals: list[Signal] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        e = self.etl
        return {
            "run_id": self.run_id, "duration_ms": self.duration_ms,
            "etl": {"files": len(e.files), "skipped": sum(f.skipped for f in e.files),
                    "rows_in": e.total("rows_in"), "loaded": e.total("loaded"),
                    "duplicate": e.total("duplicate"), "rejected": e.total("rejected"), "fixed": e.total("fixed"),
                    "rejects_by_rule": e.rejects(), "warnings": e.warnings()},
            "signals": [{"line": s.line, "date": s.prod_date.isoformat(), "rules": s.rules,
                         "ng_rate": s.point.p, "ucl": s.point.ucl, "conclusion": s.cause.conclusion,
                         "suspect": [s.cause.suspect.part_no, s.cause.suspect.lot_no] if s.cause.suspect else None}
                        for s in self.new_signals],
            "notices": [n.to_dict() for n in self.notices],
        }


def chart_points(engine: Engine) -> dict[str, list[ChartPoint]]:
    with engine.connect() as c:
        rows = c.execute(select(mart_yield_daily).order_by(mart_yield_daily.c.prod_date)).all()
    by_line: dict[str, list[ChartPoint]] = {}
    for r in rows:
        by_line.setdefault(r.line, []).append(ChartPoint(r.prod_date, r.units, r.ng_units))
    return {line: p_chart(pts) for line, pts in by_line.items()}


def run_mes(engine: Engine, folder: Path, notifier: Notifier | None = None, trigger: str = "cli") -> MesSummary:
    notifier = notifier or OutboxNotifier()
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    t0 = time.perf_counter()
    with engine.begin() as c:
        c.execute(insert(ops_run_log).values(run_id=run_id, trigger=f"mes-{trigger}"[:20],
                                             started_at=datetime.now(), status="running"))
    status, error = "ok", None
    summary = MesSummary(run_id, EtlResult())
    try:
        summary.etl = run_etl(engine, folder, run_id)
        summary.new_signals = _detect_new_signals(engine)
        for n in build_incident_notices(summary.new_signals):
            notifier.send(n)
            summary.notices.append(n)
        if summary.new_signals:
            with engine.begin() as c:
                c.execute(update(mes_yield_signal).where(mes_yield_signal.c.notified_at.is_(None))
                          .values(notified_at=datetime.now()))
    except Exception:
        status, error = "failed", traceback.format_exc()
        raise
    finally:
        summary.duration_ms = int((time.perf_counter() - t0) * 1000)
        with engine.begin() as c:
            c.execute(update(ops_run_log).where(ops_run_log.c.run_id == run_id).values(
                finished_at=datetime.now(), status=status, error=error, anomalies=len(summary.new_signals),
                duration_ms=summary.duration_ms))
    return summary


def _detect_new_signals(engine: Engine) -> list[Signal]:
    charts = chart_points(engine)
    with engine.connect() as c:
        seen = {(r.line, r.prod_date, r.rule) for r in c.execute(select(mes_yield_signal))}
    out = []
    with engine.begin() as c:
        for line, pts in charts.items():
            for p in pts:
                new_rules = [r for r in p.rules if (line, p.key, r) not in seen]
                if not new_rules:
                    continue
                cause = investigate(c, line, p.key)
                for rule in new_rules:
                    c.execute(insert(mes_yield_signal).values(
                        line=line, prod_date=p.key, rule=rule, units=p.n, ng_rate=_d(p.p), center=_d(p.center),
                        ucl=_d(p.ucl), suspect_part_no=cause.suspect.part_no if cause.suspect else None,
                        suspect_lot_no=cause.suspect.lot_no if cause.suspect else None,
                        conclusion=cause.conclusion[:400], evidence=cause.evidence_json(), created_at=datetime.now()))
                out.append(Signal(line, p.key, new_rules, p, cause))
    return out


def _d(x: float) -> Decimal:
    return Decimal(str(round(x, 5)))


def build_incident_notices(signals: list[Signal]) -> list[Notice]:
    """連續幾天的失控點合併成一則，避免同一個問題洗版。"""
    notices = []
    for line in sorted({s.line for s in signals}):
        group: list[Signal] = []
        for s in sorted((s for s in signals if s.line == line), key=lambda s: s.prod_date):
            if group and (s.prod_date - group[-1].prod_date).days > INCIDENT_GAP_DAYS:
                notices.append(_incident(group))
                group = []
            group.append(s)
        if group:
            notices.append(_incident(group))
    return notices


def _incident(group: list[Signal]) -> Notice:
    first, last = group[0], group[-1]
    span = f"{first.prod_date:%m/%d}" if first is last else f"{first.prod_date:%m/%d}～{last.prod_date:%m/%d}"
    rates = [s.point.p for s in group]
    units = sum(s.point.n for s in group)
    ng = sum(s.point.ng for s in group)
    rules = sorted({RULE_ZH[r] for s in group for r in s.rules})
    # 事件期間最常被指認的批號；沒有就用第一天的結論
    suspects = [s for s in group if s.cause.suspect]
    lead = max(suspects, key=lambda s: sum(1 for x in suspects if x.cause.suspect.lot_no == s.cause.suspect.lot_no)) \
        if suspects else first
    rate_txt = f"{rates[0]:.1%}" if len(rates) == 1 else f"{min(rates):.1%}～{max(rates):.1%}"
    lines = [
        f"{len(group)} 天{'、'.join(rules)}：不良率 {rate_txt}"
        f"（中心線 {first.point.center:.1%}，管制上限約 {first.point.ucl:.1%}）",
        f"影響 {len({w for s in group for w in s.cause.work_orders})} 張工單、{units:,} 片，其中 NG {ng:,} 片",
        lead.cause.conclusion,
    ]
    if lead.cause.suspect:
        lines.append(f"建議：隔離批號 {lead.cause.suspect.lot_no} 的剩餘庫存與在製品，通知供應商提出改善，相關料號加嚴進料檢驗")
    else:
        lines.append("建議：請製造與設備確認當天機台、治具、參數是否有變更")
    return Notice("品保", config.OWNERS["品保"], f"【良率異常】{config.LINE_NAMES.get(first.line, first.line)} {span}",
                  lines, f"yield:{first.line}:{first.prod_date}", urgent=True)
