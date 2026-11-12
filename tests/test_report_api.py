"""使用追蹤、Excel 日報、HTTP API。"""
import importlib
from datetime import date

import httpx
import pytest
from openpyxl import load_workbook

from factorypulse.report import build_daily_report
from factorypulse.stats import compute, update_baseline

from .conftest import TESTDATA, inbox_with, run_pipeline


@pytest.fixture()
def processed(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("FP_OCR", "0")
    run_pipeline(engine, tmp_path)
    return engine


def test_stats(processed):
    s = compute(processed, date.today())
    assert (s.mails, s.docs, s.docs_auto, s.docs_manual) == (23, 18, 11, 7)   # 7 = 1 待確認 ＋ 6 待 OCR
    # 全人工：23 封 ×1 ＋ 報價 6×6 ＋ 出貨 6×8 ＋ 檢驗 6×5 = 137 分鐘
    assert s.manual_minutes == pytest.approx(137)
    # 剩下：11 封待分類 ×1（5 封雜信 ＋ 6 封讀不出單據的掃描檔信）＋ 1 份待確認 ×3
    #       ＋ 6 份待 OCR 全人工（種類未知，用三種單據平均 19/3 分鐘 × 6 = 38）
    assert s.mails_by_label["待人工分類"] == 11
    assert s.remaining_minutes == pytest.approx(11 + 3 + 38)
    assert s.baseline_is_assumed


def test_baseline_update_changes_savings(processed):
    before = compute(processed).saved_minutes
    update_baseline(processed, "delivery", 12, "2026-11 採購部計時 20 份平均")
    after = compute(processed)
    assert after.saved_minutes == pytest.approx(before + 4 * 4)   # 4 份自動入帳的出貨單各多省 4 分鐘
    assert after.baseline["delivery"][2] == "2026-11 採購部計時 20 份平均"


def test_daily_report(processed, tmp_path):
    path = build_daily_report(processed, date.today(), tmp_path / "日報.xlsx")
    wb = load_workbook(path)
    assert wb.sheetnames == ["摘要", "異常待處理", "待人工確認", "採購單到貨", "產線良率", "信件分類", "執行紀錄"]
    assert len(wb["摘要"]._charts) == 1
    kpi = {r[0]: r[1] for r in wb["摘要"].iter_rows(min_row=5, max_row=16, max_col=2, values_only=True)}
    assert kpi["處理文件"] == 18 and kpi["收到信件"] == 23
    manual = [r for r in wb["待人工確認"].iter_rows(min_row=5, values_only=True) if r[0]]
    assert len(manual) == 1 + 6 + 11     # 待確認文件、待 OCR 文件、待分類信件
    assert wb["異常待處理"].freeze_panes == "A5"


# ---------------------------------------------------------------- API
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("FP_DB_URL", f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    monkeypatch.setenv("FP_API_TOKEN", "test-token")
    monkeypatch.setenv("FP_LINE_MOCK", "1")
    monkeypatch.setenv("FP_OCR", "0")
    from factorypulse import api, config, db
    monkeypatch.setattr(config, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    api = importlib.reload(api)
    db.seed_erp(api.engine(), TESTDATA / "erp")
    return TestClient(api.app), api


H = {"X-API-Key": "test-token"}


def test_api_auth(client):
    c, _ = client
    assert c.get("/health").json()["status"] == "ok"
    assert c.post("/api/poll").status_code == 401
    assert c.post("/api/poll", headers={"X-API-Key": "wrong"}).status_code == 401


def test_api_ingest_and_report(client):
    c, _ = client
    raw = (TESTDATA / "emails" / "A_delivery_02.eml").read_bytes()
    r = c.post("/api/ingest", headers=H, files={"file": ("A_delivery_02.eml", raw, "message/rfc822")})
    body = r.json()
    assert body["documents"]["posted_with_alerts"] == 1
    assert "單價與採購單不符" in body["notices"][0]["text"]
    r = c.post("/api/reports/daily", headers=H).json()
    assert r["file"].endswith(".xlsx") and "文件 1 份" in r["text"]


def test_api_poll_mailpit(client, monkeypatch):
    """用假的 Mailpit 回應測試收信：處理完要標記已讀。"""
    c, api = client
    raws = {str(i): (TESTDATA / "emails" / n).read_bytes()
            for i, n in enumerate(["B_quote_02.eml", "noise_01.eml"])}
    marked = []

    def handler(req: httpx.Request):
        if req.url.path == "/api/v1/search":
            return httpx.Response(200, json={"messages": [{"ID": i} for i in raws]})
        if req.url.path.endswith("/raw"):
            return httpx.Response(200, content=raws[req.url.path.split("/")[-2]])
        if req.method == "PUT":
            marked.append(req.read())
            return httpx.Response(200, json={})
        return httpx.Response(404)

    real = httpx.Client
    monkeypatch.setattr(api.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    body = c.post("/api/poll", headers=H).json()
    assert body["emails"] == 2 and len(marked) == 1
    # OCR 關閉時掃描報價單讀不出種類，規則無法判斷，和雜信一樣轉人工（不會亂分）
    assert [m["label"] for m in body["mails"]] == ["待人工分類", "待人工分類"]
    assert [d["status"] for d in body["mails"][0]["documents"]] == ["needs_ocr"]


def test_api_mes_run_and_dashboard(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("FP_MES_DROP", str(TESTDATA.parent / "mes_batch01" / "drop"))
    body = c.post("/api/mes/run", headers=H).json()
    assert body["etl"]["rejected"] == 21 and len(body["notices"]) == 2
    assert c.get("/api/dashboard").status_code == 401
    d = c.get("/api/dashboard", headers=H).json()
    assert set(d["charts"]) == {"A", "B"} and d["lots"][0]["lot_no"] == "L261012-01"   # 產線 NG 率最高的批號
    page = c.get("/dashboard")
    assert page.status_code == 200 and "chart.js@4.4.1" in page.text and "?key=" not in page.text


def test_dashboard_public_only_opens_read_only_data(client, monkeypatch):
    c, _ = client
    assert c.get("/api/dashboard").status_code == 401            # 預設關閉
    monkeypatch.setenv("FP_DASHBOARD_PUBLIC", "1")
    assert c.get("/api/dashboard").status_code == 200
    assert c.get("/health").json()["dashboard_public"] is True
    # 會寫入資料的 API 不受影響，仍然要金鑰
    for path in ("/api/poll", "/api/mes/run", "/api/reports/daily"):
        assert c.post(path).status_code == 401, path
    assert c.post("/api/ingest", files={"file": ("x.eml", b"", "message/rfc822")}).status_code == 401
    assert c.get("/api/stats").status_code == 401


def test_line_mock_requires_bearer(client):
    c, _ = client
    assert c.post("/mock/line/v2/bot/message/push", json={"to": "x", "messages": []}).status_code == 401
    ok = c.post("/mock/line/v2/bot/message/push", json={"to": "x", "messages": [{"type": "text", "text": "hi"}]},
                headers={"Authorization": "Bearer t"})
    assert ok.status_code == 200
