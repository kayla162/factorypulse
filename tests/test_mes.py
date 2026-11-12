"""產線：ETL 清洗、p-chart、批號回查、事件通知、MES 評測。"""
import csv
import shutil
from datetime import date

import pytest
from sqlalchemy import func, select

from factorypulse.db import etl_reject, mart_wo_yield, mes_aoi_result, mes_yield_signal
from factorypulse.etl import normalize_line, parse_timestamp, run_etl
from factorypulse.evaluate_mes import run_mes_eval
from factorypulse.mes import build_incident_notices, run_mes
from factorypulse.notify import OutboxNotifier
from factorypulse.spc import ChartPoint, estimate_center, limits, p_chart
from factorypulse.trace import investigate, two_proportion_p

from .conftest import TESTDATA, run_pipeline

MES = TESTDATA.parent / "mes_batch01"


# ---------------------------------------------------------------- 清洗函式
@pytest.mark.parametrize("raw, expected", [
    ("A線", "A"), ("LINE-A", "A"), ("Line B", "B"), ("b線", "B"), ("Ａ線", "A"), ("第三線", None), ("AB", None)])
def test_normalize_line(raw, expected):
    assert normalize_line(raw) == expected


def test_parse_timestamp_timezones():
    assert parse_timestamp("2026-10-28T08:12:33+08:00").hour == 8
    assert parse_timestamp("2026-10-28T00:12:33+00:00").hour == 8      # UTC → 台灣時間
    assert parse_timestamp("2026/10/28 08:12:33").minute == 12
    assert parse_timestamp("昨天下午") is None


# ---------------------------------------------------------------- p-chart
def test_p_chart_limits_and_rules():
    pts = [ChartPoint(date(2026, 10, d), 500, 5) for d in range(1, 11)]    # 穩定 1%
    pts.append(ChartPoint(date(2026, 10, 11), 500, 20))                   # 4% 失控
    out = p_chart(pts)
    assert out[0].center == pytest.approx(0.01)                           # 失控點不會把中心線拉高
    ucl, _ = limits(0.01, 500)
    assert ucl == pytest.approx(0.01 + 3 * (0.01 * 0.99 / 500) ** 0.5)
    assert out[-1].rules == ["OUT_OF_CONTROL"] and all(not p.rules for p in out[:-1])


def test_run_rule_catches_small_sustained_shift():
    base = [ChartPoint(date(2026, 9, d), 500, 5) for d in range(1, 21)]
    shift = [ChartPoint(date(2026, 10, d), 500, 8) for d in range(1, 8)]  # 1.6%，沒超過上限但持續偏高
    out = p_chart(base + shift)
    assert [p.rules for p in out[-7:-1]] == [[]] * 6
    assert out[-1].rules == ["RUN_ABOVE_CENTER"]


def test_two_proportion_p():
    assert two_proportion_p(60, 1000, 10, 1000) < 1e-8
    assert two_proportion_p(10, 1000, 10, 1000) == pytest.approx(0.5)
    assert two_proportion_p(0, 0, 1, 10) is None


# ---------------------------------------------------------------- 整條產線流程
@pytest.fixture()
def mes_run(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("FP_OCR", "0")
    run_pipeline(engine, tmp_path)               # 先有進料檢驗紀錄，回查才連得到
    s = run_mes(engine, MES / "drop", OutboxNotifier(tmp_path / "ob", echo=False))
    return engine, s


def test_etl_counts_match_planted_dirt(mes_run):
    engine, s = mes_run
    import json
    dirty = json.loads((MES / "answers" / "mes_truth.json").read_text(encoding="utf-8"))["dirty"]
    e = s.to_dict()["etl"]
    assert e["duplicate"] == dirty["wo_duplicate_rows"] + dirty["aoi_duplicate_rows"]
    assert e["rejects_by_rule"] == {"MISSING_SCORE": dirty["aoi_missing_score"]}
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(etl_reject)).scalar() == dirty["aoi_missing_score"]
        lines = set(c.execute(select(mes_aoi_result.c.line)).scalars())
    assert lines == {"A", "B"}                   # 各種寫法都統一了


def test_bad_lot_is_traced_and_spike_is_not_blamed(mes_run):
    engine, s = mes_run
    by_day = {(x.line, x.prod_date): x for x in s.new_signals}
    for d in (28, 29, 30):
        cause = by_day[("A", date(2026, 10, d))].cause
        assert (cause.suspect.part_no, cause.suspect.lot_no) == ("FP-PL-4001", "L261012-01")
        assert cause.vendor_name == "宇碩表面處理科技股份有限公司"
        assert any("QC26101201" in a for a in cause.iqc_alerts)   # 連到第一週的進料檢驗警訊
    spike = by_day[("B", date(2026, 11, 10))].cause
    assert spike.suspect is None and "疑似製程或設備" in spike.conclusion


def test_incident_notices_are_grouped(mes_run):
    _, s = mes_run
    titles = [n.title for n in s.notices]
    assert titles == ["【良率異常】A線 10/27～11/05", "【良率異常】B線 11/10"]
    assert all(n.urgent and n.owner == "品保" for n in s.notices)
    assert "隔離批號 L261012-01" in s.notices[0].text()


def test_rerun_is_idempotent(mes_run, tmp_path):
    engine, _ = mes_run
    with engine.connect() as c:
        before = c.execute(select(func.count()).select_from(mes_aoi_result)).scalar()
    s2 = run_mes(engine, MES / "drop", OutboxNotifier(tmp_path / "ob2", echo=False))
    assert all(f.skipped for f in s2.etl.files) and s2.new_signals == [] and s2.notices == []
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(mes_aoi_result)).scalar() == before


def test_reexported_file_replaces_old_rows(engine, tmp_path, monkeypatch):
    """AOI 當天重新匯出（內容變了、檔名相同）→ 以新檔為準，不會重複計算。"""
    drop = tmp_path / "drop"
    drop.mkdir()
    for f in ("work_orders.csv", "material_issues.csv", "aoi_results_20261019.csv"):
        shutil.copy(MES / "drop" / f, drop)
    run_etl(engine, drop, "r1")
    path = drop / "aoi_results_20261019.csv"
    rows = list(csv.reader(path.open(encoding="utf-8-sig")))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows[:-10])      # 新檔少 10 列
    run_etl(engine, drop, "r2")
    with engine.connect() as c:
        n = c.execute(select(func.count()).select_from(mes_aoi_result)).scalar()
        units = c.execute(select(func.sum(mart_wo_yield.c.units))).scalar()
    assert n <= len(rows) - 1 - 10 and units == len({r[4] for r in rows[1:-10] if r[6]})


def test_mes_eval_strict(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("FP_OCR", "0")
    r = run_mes_eval(engine, MES, TESTDATA)
    assert r.etl_ok and r.fn == set() and r.lot_wrong == [] and r.spike_ok
    assert len(r.lot_correct) == 7 and len(r.fp) <= 1
