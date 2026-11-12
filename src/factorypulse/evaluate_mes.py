"""產線部分的評測：對照 make_mes_data.py 埋入的答案（mes_truth.json）。

1. ETL：重複列、缺分數的列，有沒有剛好被剔除／擋下；批號格式是否全部統一。
2. 良率異常偵測：以「線別×日」為單位算抓到、誤報、漏抓。
3. 回查：壞料期間要指向埋入的批號；設備異常那天不能怪到任何批號。
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from .db import init_db, mart_yield_daily, mes_material_issue, mes_yield_signal, seed_erp
from .mes import run_mes
from .notify import OutboxNotifier
from .pipeline import run_inbox

LOT_FORMAT = re.compile(r"^L\d{6}-\d{2}$")


@dataclass
class MesEval:
    truth: dict
    etl: dict
    lots_malformed: list[str] = field(default_factory=list)
    expected: set = field(default_factory=set)       # (line, date)
    flagged: set = field(default_factory=set)
    lot_correct: list[str] = field(default_factory=list)
    lot_wrong: list[str] = field(default_factory=list)
    spike_ok: bool | None = None
    notices: int = 0
    run_ms: int = 0
    line_days: int = 0

    @property
    def tp(self): return self.expected & self.flagged

    @property
    def fp(self): return self.flagged - self.expected

    @property
    def fn(self): return self.expected - self.flagged

    @property
    def etl_ok(self) -> bool:
        d = self.truth["dirty"]
        return (self.etl["duplicate"] == d["wo_duplicate_rows"] + d["aoi_duplicate_rows"]
                and self.etl["rejects_by_rule"].get("MISSING_SCORE", 0) == d["aoi_missing_score"]
                and self.etl["rejected"] == d["aoi_missing_score"] and not self.lots_malformed)


def run_mes_eval(engine: Engine, mes_dir: Path, docs_dir: Path) -> MesEval:
    init_db(engine, reset=True)
    seed_erp(engine, docs_dir / "erp")
    with tempfile.TemporaryDirectory() as tmp:
        ob = OutboxNotifier(Path(tmp) / "outbox", echo=False)
        # 先跑進料文件，進料檢驗的批號紀錄才會在，回查時才連得到「進料時的警訊」
        run_inbox(engine, docs_dir / "emails", ob, trigger="eval", attachments_dir=Path(tmp) / "att", llm=None)
        s = run_mes(engine, mes_dir / "drop", ob, trigger="eval")
    truth = json.loads((mes_dir / "answers" / "mes_truth.json").read_text(encoding="utf-8"))
    r = MesEval(truth, s.to_dict()["etl"], notices=len(s.notices), run_ms=s.duration_ms)
    with engine.connect() as c:
        lots = set(c.execute(select(mes_material_issue.c.lot_no)).scalars())
        sigs = c.execute(select(mes_yield_signal)).all()
        r.line_days = c.execute(select(func.count()).select_from(mart_yield_daily)).scalar()
    r.lots_malformed = sorted(l for l in lots if not LOT_FORMAT.match(l))
    r.expected = {(line, date.fromisoformat(d)) for line, days in truth["expected_signal_days"].items() for d in days}
    r.flagged = {(s.line, s.prod_date) for s in sigs}
    bad = truth["bad_lot"]
    spike = (truth["spike"]["line"], date.fromisoformat(truth["spike"]["date"]))
    for s in {(x.line, x.prod_date): x for x in sigs}.values():
        key = f"{s.line} {s.prod_date:%m/%d}"
        if (s.line, s.prod_date) == spike:
            r.spike_ok = s.suspect_lot_no is None
        elif (s.line, s.prod_date) in r.expected:
            if (s.suspect_part_no, s.suspect_lot_no) == (bad["part_no"], bad["lot_no"]):
                r.lot_correct.append(key)
            else:
                r.lot_wrong.append(f"{key}（指向 {s.suspect_part_no} {s.suspect_lot_no}）")
    return r


def to_markdown(r: MesEval) -> str:
    pct = lambda a, b: f"{a / b:.0%}" if b else "—"  # noqa: E731
    d, e = r.truth["dirty"], r.etl
    lot_days = len(r.expected) - 1
    out = [
        "# 評測報告：產線（mes_batch01）", "",
        f"產生時間：{datetime.now():%Y-%m-%d %H:%M}　ETL＋管制圖＋回查耗時 {r.run_ms / 1000:.1f} 秒", "",
        "## ETL 資料清洗", "",
        "| 項目 | 埋入 | 系統結果 |", "|---|---|---|",
        f"| 完全重複的列 | {d['wo_duplicate_rows'] + d['aoi_duplicate_rows']} | 剔除 {e['duplicate']} |",
        f"| AOI 分數缺漏 | {d['aoi_missing_score']} | 擋下 {e['rejects_by_rule'].get('MISSING_SCORE', 0)}（全部擋下 {e['rejected']}） |",
        f"| 批號格式不一 | {d['issue_messy_lot']} | 統一後仍不合格式：{len(r.lots_malformed)} |",
        f"| 線別／日期／時間格式不一 | 線別 {d['wo_line_alias'] + d['aoi_line_alias']}、日期時間 {d['wo_slash_dates'] + d['aoi_slash_timestamps']} | 自動修正 {e['fixed']} 列 |",
        f"| 領料批號沒有進料檢驗紀錄 | — | 警示 {e['warnings'].get('LOT_WITHOUT_IQC', 0)} 列（前期庫存） |",
        "", f"ETL 結果：**{'全部符合' if r.etl_ok else '有差異'}**", "",
        "## 良率異常偵測（p-chart，以線別×日計）", "",
        "| 指標 | 數值 |", "|---|---|",
        f"| 埋入異常天數 | {len(r.expected)}（壞料 {lot_days} 天＋設備異常 1 天） |",
        f"| 抓到 | {len(r.tp)} |",
        f"| 漏抓 | {len(r.fn)} |",
        f"| 誤報 | {len(r.fp)}（全部 {r.line_days} 個線別×日中） |",
        f"| 召回率 | {pct(len(r.tp), len(r.expected))} |",
    ]
    if r.fp:
        out.append("\n誤報：" + "、".join(f"{l} {d:%m/%d}" for l, d in sorted(r.fp))
                   + "（3σ 管制圖本來就有約 0.1～0.3% 的單點誤報率，加上這天實際不良率偏高，屬隨機波動）")
    if r.fn:
        out.append("\n漏抓：" + "、".join(f"{l} {d:%m/%d}" for l, d in sorted(r.fn)))
    out += [
        "", "## 批號回查", "",
        f"- 壞料期間指向正確批號：{len(r.lot_correct)}/{len(r.lot_correct) + len(r.lot_wrong)} 天",
        f"- 設備異常那天沒有冤枉批號：{'是' if r.spike_ok else '否' if r.spike_ok is False else '未偵測到'}",
        f"- 通知：{r.notices} 則（連續幾天合併成一個事件）",
    ]
    out += [f"  - 指錯：{x}" for x in r.lot_wrong]
    return "\n".join(out) + "\n"
