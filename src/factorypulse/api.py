"""HTTP 服務：給 n8n 呼叫。

POST /api/poll            從 Mailpit 收未讀信 → 處理 → 標記已讀，回傳要發送的通知
POST /api/ingest          直接上傳一封 .eml 處理
POST /api/reports/daily   產生 Excel 日報，回傳摘要文字（給 LINE）
GET  /api/stats           自動化率、人工介入率、省時
GET  /health

除了 /health，都要帶 X-API-Key（.env 的 FP_API_TOKEN）。
啟動：uvicorn factorypulse.api:app --port 8000
"""
from __future__ import annotations

import hmac
import json
import os
import threading
from datetime import date, datetime
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import config
from .db import get_engine, init_db
from .llm import default_client
from .mail import parse_eml
from .notify import OutboxNotifier
from .parsers import ocr_enabled
from .pipeline import Pipeline

app = FastAPI(title="FactoryPulse", version="0.2.0")
_engine = None
_lock = threading.Lock()   # 同一時間只跑一批，避免排程重疊時重複入帳
_mock_lock = threading.Lock()


def engine():
    global _engine
    if _engine is None:
        _engine = get_engine()
        init_db(_engine)
    return _engine


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    token = os.environ.get("FP_API_TOKEN", "")
    if not token:
        raise HTTPException(503, "伺服器未設定 FP_API_TOKEN")
    if not x_api_key or not hmac.compare_digest(x_api_key, token):
        raise HTTPException(401, "X-API-Key 錯誤")


def dashboard_public() -> bool:
    return os.environ.get("FP_DASHBOARD_PUBLIC", "0") == "1"


def require_key_unless_public(x_api_key: str | None = Header(default=None)) -> None:
    """FP_DASHBOARD_PUBLIC=1 時，唯讀的儀表板資料不需要金鑰。

    只適合「埠只綁 127.0.0.1」的本機示範；會寫入資料的 API 不受這個設定影響，永遠要金鑰。
    """
    if not dashboard_public():
        require_key(x_api_key)


def _pipeline(trigger: str) -> Pipeline:
    return Pipeline(engine(), OutboxNotifier(echo=False), default_client(), trigger=trigger)


@app.get("/health")
def health():
    llm = default_client()
    return {"status": "ok", "db": engine().dialect.name, "ocr": ocr_enabled(),
            "llm": llm.name if llm else None, "dashboard_public": dashboard_public(),
            "time": datetime.now().isoformat(timespec="seconds")}


@app.post("/api/ingest", dependencies=[Depends(require_key)])
async def ingest(file: UploadFile = File(...)):
    raw = await file.read()
    mail = parse_eml(raw, Path(file.filename or "upload.eml"))
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "另一批正在處理中")
    try:
        return _pipeline("api").run([mail]).to_dict()
    finally:
        _lock.release()


@app.post("/api/poll", dependencies=[Depends(require_key)])
def poll(limit: int = 50):
    base = os.environ.get("FP_MAILPIT_URL", "http://localhost:8025").rstrip("/")
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "另一批正在處理中")
    try:
        with httpx.Client(base_url=base, timeout=30) as mp:
            r = mp.get("/api/v1/search", params={"query": "is:unread", "limit": limit})
            r.raise_for_status()
            ids = [m["ID"] for m in r.json().get("messages", [])]
            mails = [parse_eml(mp.get(f"/api/v1/message/{i}/raw").content, Path(f"mailpit-{i}.eml")) for i in ids]
            summary = _pipeline("n8n").run(mails)
            if ids:  # 處理完才標記已讀；中途失敗下次會重收，靠雜湊去重不會重複入帳
                mp.put("/api/v1/messages", json={"IDs": ids, "Read": True}).raise_for_status()
        return summary.to_dict()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"連不到 Mailpit：{e}") from e
    finally:
        _lock.release()


@app.post("/api/reports/daily", dependencies=[Depends(require_key)])
def daily_report(day: date | None = None):
    from .report import build_daily_report
    from .stats import compute, to_text
    day = day or date.today()
    path = build_daily_report(engine(), day)
    return {"path": str(path), "file": path.name, "text": to_text(compute(engine(), day))}


@app.post("/api/mes/run", dependencies=[Depends(require_key)])
def mes_run():
    """匯入 MES／AOI 匯出資料夾（FP_MES_DROP）→ 良率管制圖 → 回查批號，回傳要發送的通知。"""
    from .mes import run_mes
    folder = Path(os.environ.get("FP_MES_DROP", str(config.MES_TESTDATA_DIR / "drop")))
    if not folder.is_dir():
        raise HTTPException(500, f"找不到 MES 匯出資料夾：{folder}")
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "另一批正在處理中")
    try:
        return run_mes(engine(), folder, OutboxNotifier(echo=False), trigger="n8n").to_dict()
    finally:
        _lock.release()


@app.get("/api/dashboard", dependencies=[Depends(require_key_unless_public)])
def dashboard_api():
    from .dashboard import dashboard_data
    return dashboard_data(engine())


@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    """網頁本身不含資料；資料由頁面帶 ?key= 的金鑰呼叫 /api/dashboard 取得。"""
    return FileResponse(Path(__file__).parent / "static" / "dashboard.html", media_type="text/html")


@app.get("/api/stats", dependencies=[Depends(require_key)])
def stats(day: date | None = None):
    from .stats import compute
    s = compute(engine(), day)
    return {"period": s.period, "mails": s.mails, "mails_by_method": s.mails_by_method,
            "mails_by_label": s.mails_by_label, "docs": s.docs, "docs_by_status": s.docs_by_status,
            "auto_rate": s.auto_rate, "doc_intervention_rate": s.doc_intervention_rate,
            "mail_intervention_rate": s.mail_intervention_rate, "failure_rate": s.failure_rate,
            "manual_minutes": s.manual_minutes, "remaining_minutes": s.remaining_minutes,
            "saved_minutes": s.saved_minutes, "baseline_is_assumed": s.baseline_is_assumed,
            "llm_tokens": s.llm_tokens}


# ---------------------------------------------------------------- 本機測試用的假 LINE
# 沒有 LINE 帳號時，n8n 把訊息送到這裡，確認整條流程通了再換成真的 api.line.me。
if os.environ.get("FP_LINE_MOCK") == "1":
    @app.post("/mock/line/v2/bot/message/push")
    def mock_line_push(payload: dict, authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "缺少 Bearer token")
        config.OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(at=datetime.now().isoformat(timespec="seconds"), **payload), ensure_ascii=False)
        with _mock_lock, open(config.OUTBOX_DIR / "line_mock.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")   # n8n 會平行送出多則，不加鎖會寫成交錯的一行
        return {}
