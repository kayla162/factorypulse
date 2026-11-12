"""設定：資料庫連線、檢驗標準、通知對象。"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
OUTBOX_DIR = ROOT / "outbox"
REPORTS_DIR = ROOT / "reports"
TESTDATA_DIR = ROOT / "testdata" / "batch01"

AQL_LIMIT = Decimal("0.01")      # 進料不良率上限 1%
TAX_RATE = Decimal("0.05")       # 營業稅
AMOUNT_TOLERANCE = Decimal("1")  # 驗算容許誤差（供應商常把金額四捨五入到元）

# 異常代碼 → (中文名稱, 嚴重度, 負責部門)
#   review：文件先擋下，不寫入 ERP，等人工確認
#   alert ：照常寫入 ERP，但通知負責人處理
ANOMALY_RULES: dict[str, tuple[str, str, str]] = {
    "PART_UNKNOWN":       ("料號無法對應", "review", "採購"),
    "PO_NOT_FOUND":       ("找不到採購單", "review", "採購"),
    "VENDOR_MISMATCH":    ("供應商與採購單不符", "review", "採購"),
    "PART_NOT_ON_PO":     ("料號不在採購單上", "review", "採購"),
    "PARSE_CHECK_FAILED": ("文件驗算不通過", "review", "採購"),
    "PRICE_MISMATCH":     ("單價與採購單不符", "alert", "採購"),
    "QTY_SHORT":          ("出貨數量短少", "alert", "採購"),
    "QTY_OVER":           ("出貨數量超過採購", "alert", "採購"),
    "LATE_DELIVERY":      ("交期延誤", "alert", "採購"),
    "PRICE_INCREASE":     ("報價高於現行單價", "alert", "採購"),
    "INSPECTION_NG":      ("供應商判定 NG 仍出貨", "alert", "品保"),
    "DEFECT_RATE_HIGH":   ("不良率超過 AQL", "alert", "品保"),
}

OWNERS = {"採購": "採購部 張小姐", "品保": "品保部 IQC"}
INTERNAL_DOMAIN = "dinglian-connector.example"   # 本公司網域（虛構）
LINE_NAMES = {"A": "A線", "B": "B線"}
MES_TESTDATA_DIR = ROOT / "testdata" / "mes_batch01"

# 人工處理時間基準（分鐘）。目前是「假設值」，導入時要換成現場實際計時的結果。
MANUAL_BASELINE_MINUTES = {
    "mail_triage": (1.0, "開信、判斷該給誰"),
    "quote": (6.0, "報價單逐項比對現行單價、建檔"),
    "delivery": (8.0, "出貨單對採購單、輸入進貨"),
    "inspection": (5.0, "檢驗報告看判定、算不良率、建檔"),
    "review": (3.0, "系統擋下的文件，人工確認一份"),
}


def db_url() -> str:
    url = os.environ.get("FP_DB_URL", "").strip()
    if url:
        return url
    DATA_DIR.mkdir(exist_ok=True)
    return f"sqlite:///{(DATA_DIR / 'factorypulse.db').as_posix()}"
