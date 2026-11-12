"""資料表定義。SQLAlchemy Core 寫一次，MS SQL 與 SQLite 都能用。

命名：erp_* 模擬 ERP、doc_* 收到的文件、ops_* 營運紀錄（異常、執行紀錄）。
"""
from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (Column, Date, DateTime, ForeignKey, Index, Integer, MetaData, Numeric,
                        PrimaryKeyConstraint, Table, Unicode, UnicodeText, create_engine, delete,
                        insert, text)
from sqlalchemy.engine import Engine, make_url

from . import config

metadata = MetaData()

PRICE = Numeric(12, 4)
MONEY = Numeric(14, 2)

# ---------------------------------------------------------------- 模擬 ERP
erp_vendor = Table(
    "erp_vendor", metadata,
    Column("vendor_code", Unicode(10), primary_key=True),
    Column("vendor_name", Unicode(100), nullable=False),
    Column("email_domain", Unicode(100), nullable=False, unique=True),
)

erp_part = Table(
    "erp_part", metadata,
    Column("part_no", Unicode(30), primary_key=True),
    Column("name", Unicode(100), nullable=False),
    Column("spec", Unicode(200)),
    Column("vendor_code", Unicode(10), ForeignKey("erp_vendor.vendor_code"), nullable=False),
    Column("current_unit_price", PRICE, nullable=False),
)

erp_vendor_part_map = Table(
    "erp_vendor_part_map", metadata,
    Column("vendor_code", Unicode(10), ForeignKey("erp_vendor.vendor_code"), nullable=False),
    Column("vendor_part_no", Unicode(30), nullable=False),
    Column("part_no", Unicode(30), ForeignKey("erp_part.part_no"), nullable=False),
    PrimaryKeyConstraint("vendor_code", "vendor_part_no"),
)

erp_purchase_order = Table(
    "erp_purchase_order", metadata,
    Column("po_no", Unicode(20), nullable=False),
    Column("line", Integer, nullable=False),
    Column("vendor_code", Unicode(10), ForeignKey("erp_vendor.vendor_code"), nullable=False),
    Column("part_no", Unicode(30), ForeignKey("erp_part.part_no"), nullable=False),
    Column("qty", Integer, nullable=False),
    Column("unit_price", PRICE, nullable=False),
    Column("po_date", Date, nullable=False),
    Column("due_date", Date, nullable=False),
    PrimaryKeyConstraint("po_no", "line"),
)

# ---------------------------------------------------------------- 收到的文件
doc_document = Table(
    "doc_document", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Unicode(40), nullable=False),
    Column("source_email", Unicode(200), nullable=False),
    Column("attachment_name", Unicode(200), nullable=False),
    Column("file_sha256", Unicode(64), nullable=False, unique=True),
    Column("vendor_code", Unicode(10), ForeignKey("erp_vendor.vendor_code")),
    Column("doc_type", Unicode(20)),          # quote / delivery / inspection
    Column("doc_no", Unicode(30)),
    Column("doc_date", Date),
    Column("po_no", Unicode(20)),
    Column("ref_doc_no", Unicode(30)),
    Column("parser", Unicode(30)),
    # posted / posted_with_alerts / needs_review / needs_ocr / failed
    Column("status", Unicode(30), nullable=False),
    Column("subtotal", MONEY),
    Column("tax", MONEY),
    Column("total", MONEY),
    Column("error", UnicodeText),
    Column("processing_ms", Integer),
    Column("created_at", DateTime, nullable=False),
)
Index("ix_doc_vendor_no", doc_document.c.vendor_code, doc_document.c.doc_type, doc_document.c.doc_no)

doc_line = Table(
    "doc_line", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("document_id", Integer, ForeignKey("doc_document.id"), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("raw_part_no", Unicode(40)),
    Column("part_no", Unicode(30)),           # 對應不到時為 NULL
    Column("description", Unicode(200)),
    Column("qty", Integer),
    Column("unit_price", PRICE),
    Column("amount", MONEY),
    Column("lot_no", Unicode(30)),
    Column("sample_qty", Integer),
    Column("defect_qty", Integer),
    Column("result", Unicode(10)),
)

# ---------------------------------------------------------------- 寫入 ERP 的結果
erp_goods_receipt = Table(  # 進貨
    "erp_goods_receipt", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("document_id", Integer, ForeignKey("doc_document.id"), nullable=False),
    Column("po_no", Unicode(20), nullable=False),
    Column("po_line", Integer, nullable=False),
    Column("part_no", Unicode(30), ForeignKey("erp_part.part_no"), nullable=False),
    Column("qty", Integer, nullable=False),
    Column("unit_price", PRICE, nullable=False),
    Column("receipt_date", Date, nullable=False),
    Column("posted_at", DateTime, nullable=False),
)

erp_incoming_inspection = Table(  # 進料檢驗（帶批號，之後做良率追溯用）
    "erp_incoming_inspection", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("document_id", Integer, ForeignKey("doc_document.id"), nullable=False),
    Column("po_no", Unicode(20)),
    Column("delivery_no", Unicode(30)),
    Column("part_no", Unicode(30), ForeignKey("erp_part.part_no"), nullable=False),
    Column("lot_no", Unicode(30), nullable=False),
    Column("qty", Integer),
    Column("sample_qty", Integer),
    Column("defect_qty", Integer),
    Column("vendor_result", Unicode(10)),
    Column("inspected_on", Date),
    Column("posted_at", DateTime, nullable=False),
)

# ---------------------------------------------------------------- MES（產線）
mes_work_order = Table(
    "mes_work_order", metadata,
    Column("wo_no", Unicode(20), primary_key=True),
    Column("product_no", Unicode(30), nullable=False),
    Column("line", Unicode(10), nullable=False),
    Column("prod_date", Date, nullable=False),
    Column("planned_qty", Integer, nullable=False),
    Column("source_file", Unicode(200), nullable=False),
    Column("loaded_at", DateTime, nullable=False),
)

mes_material_issue = Table(  # 領料：工單用了哪些料號的哪些批號（追溯的關鍵）
    "mes_material_issue", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("wo_no", Unicode(20), ForeignKey("mes_work_order.wo_no"), nullable=False),
    Column("part_no", Unicode(30), ForeignKey("erp_part.part_no"), nullable=False),
    Column("lot_no", Unicode(30), nullable=False),
    Column("issued_qty", Integer, nullable=False),
    Column("source_file", Unicode(200), nullable=False),
)
Index("ix_issue_lot", mes_material_issue.c.part_no, mes_material_issue.c.lot_no)

mes_aoi_result = Table(  # AOI 逐片判定（同一片可能被複判多次，以最後一次為準）
    "mes_aoi_result", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("unit_sn", Unicode(40), nullable=False),
    Column("inspected_at", DateTime, nullable=False),
    Column("line", Unicode(10), nullable=False),
    Column("station", Unicode(20), nullable=False),
    Column("wo_no", Unicode(20), ForeignKey("mes_work_order.wo_no"), nullable=False),
    Column("score", Numeric(6, 4), nullable=False),
    Column("threshold", Numeric(6, 4), nullable=False),
    Column("decision", Unicode(4), nullable=False),
    Column("model_version", Unicode(60)),
    Column("source_file", Unicode(200), nullable=False),
)
Index("ix_aoi_unit", mes_aoi_result.c.unit_sn, mes_aoi_result.c.inspected_at)
Index("ix_aoi_wo", mes_aoi_result.c.wo_no)

etl_file_log = Table(
    "etl_file_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Unicode(40), nullable=False),
    Column("file_name", Unicode(200), nullable=False),
    Column("file_sha256", Unicode(64), nullable=False),
    Column("kind", Unicode(20), nullable=False),        # work_order / material_issue / aoi
    Column("rows_in", Integer, nullable=False),
    Column("rows_loaded", Integer, nullable=False),
    Column("rows_duplicate", Integer, nullable=False),
    Column("rows_rejected", Integer, nullable=False),
    Column("rows_fixed", Integer, nullable=False),      # 自動修正（線別、日期、批號格式）
    Column("loaded_at", DateTime, nullable=False),
)

etl_reject = Table(  # 被擋下的資料列：保留原始內容，方便回頭跟 MES／AOI 負責人確認
    "etl_reject", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Unicode(40), nullable=False),
    Column("file_name", Unicode(200), nullable=False),
    Column("row_no", Integer, nullable=False),
    Column("rule", Unicode(30), nullable=False),
    Column("detail", Unicode(200)),
    Column("raw", UnicodeText),
)

mart_wo_yield = Table(  # 每張工單的最終良率（每片取最後一次判定）
    "mart_wo_yield", metadata,
    Column("wo_no", Unicode(20), primary_key=True),
    Column("line", Unicode(10), nullable=False),
    Column("product_no", Unicode(30), nullable=False),
    Column("prod_date", Date, nullable=False),
    Column("units", Integer, nullable=False),
    Column("ng_units", Integer, nullable=False),
    Column("rechecked_units", Integer, nullable=False),
)

mart_yield_daily = Table(
    "mart_yield_daily", metadata,
    Column("prod_date", Date, nullable=False),
    Column("line", Unicode(10), nullable=False),
    Column("units", Integer, nullable=False),
    Column("ng_units", Integer, nullable=False),
    Column("yield_rate", Numeric(7, 5), nullable=False),
    PrimaryKeyConstraint("prod_date", "line"),
)

mes_yield_signal = Table(  # p-chart 失控點 ＋ 回查結果
    "mes_yield_signal", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("line", Unicode(10), nullable=False),
    Column("prod_date", Date, nullable=False),
    Column("rule", Unicode(30), nullable=False),
    Column("units", Integer, nullable=False),
    Column("ng_rate", Numeric(7, 5), nullable=False),
    Column("center", Numeric(7, 5), nullable=False),
    Column("ucl", Numeric(7, 5), nullable=False),
    Column("suspect_part_no", Unicode(30)),
    Column("suspect_lot_no", Unicode(30)),
    Column("conclusion", Unicode(400), nullable=False),
    Column("evidence", UnicodeText),                    # JSON：各批號比較
    Column("created_at", DateTime, nullable=False),
    Column("notified_at", DateTime),
)
Index("ix_signal_line_date", mes_yield_signal.c.line, mes_yield_signal.c.prod_date, mes_yield_signal.c.rule,
      unique=True)

# ---------------------------------------------------------------- Power Automate Desktop 對照組
pad_delivery_staging = Table(  # PAD 流程讀 Excel 出貨單後寫進來，全部存文字，比對時再轉型
    "pad_delivery_staging", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_tag", Unicode(30), nullable=False),
    Column("file_name", Unicode(200), nullable=False),
    Column("doc_no", Unicode(40)),
    Column("doc_date_text", Unicode(40)),
    Column("po_no", Unicode(40)),
    Column("vendor_part_no", Unicode(40)),
    Column("qty_text", Unicode(40)),
    Column("unit_price_text", Unicode(40)),
    Column("loaded_at", DateTime, nullable=False),
)

# ---------------------------------------------------------------- 營運紀錄
ops_anomaly = Table(
    "ops_anomaly", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("document_id", Integer, ForeignKey("doc_document.id"), nullable=False),
    Column("code", Unicode(30), nullable=False),
    Column("severity", Unicode(10), nullable=False),
    Column("owner", Unicode(20), nullable=False),
    Column("part_no", Unicode(30)),
    Column("detail", Unicode(400), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("notified_at", DateTime),
    Column("resolved_at", DateTime),
)

mail_message = Table(  # 每封信的分類結果
    "mail_message", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Unicode(40), nullable=False),
    Column("file_sha256", Unicode(64), nullable=False, unique=True),
    Column("source_name", Unicode(200), nullable=False),
    Column("sender", Unicode(200), nullable=False),
    Column("subject", Unicode(400)),
    Column("sent_at", DateTime),
    Column("attachments", Integer, nullable=False),
    Column("label", Unicode(20), nullable=False),        # 採購／品保／客訴／其他／待人工分類
    Column("method", Unicode(10), nullable=False),       # rule / llm / none / manual
    Column("confidence", Numeric(4, 3)),
    Column("reason", Unicode(400)),
    Column("extracted", UnicodeText),                    # JSON：摘要、採購單號、新交期
    Column("tool_calls", Unicode(400)),
    Column("llm_calls", Integer),
    Column("llm_tokens", Integer),
    Column("elapsed_ms", Integer),
    Column("created_at", DateTime, nullable=False),
)

ops_time_baseline = Table(  # 人工處理時間基準；導入時用現場計時結果更新
    "ops_time_baseline", metadata,
    Column("task", Unicode(30), primary_key=True),
    Column("minutes", Numeric(6, 2), nullable=False),
    Column("description", Unicode(100)),
    Column("source", Unicode(100), nullable=False),      # 例如「假設值」「2026-11 採購部計時 20 份平均」
    Column("updated_at", DateTime, nullable=False),
)

ops_run_log = Table(
    "ops_run_log", metadata,
    Column("run_id", Unicode(40), primary_key=True),
    Column("trigger", Unicode(20), nullable=False),
    Column("started_at", DateTime, nullable=False),
    Column("finished_at", DateTime),
    Column("emails_seen", Integer, default=0),
    Column("emails_skipped", Integer, default=0),        # 重複的信
    Column("emails_rule", Integer, default=0),
    Column("emails_llm", Integer, default=0),
    Column("emails_unclassified", Integer, default=0),
    Column("llm_calls", Integer, default=0),
    Column("llm_tokens", Integer, default=0),
    Column("documents_seen", Integer, default=0),
    Column("documents_posted", Integer, default=0),
    Column("documents_review", Integer, default=0),
    Column("documents_needs_ocr", Integer, default=0),
    Column("documents_failed", Integer, default=0),
    Column("documents_duplicate", Integer, default=0),
    Column("anomalies", Integer, default=0),
    Column("duration_ms", Integer),
    Column("status", Unicode(20), nullable=False),
    Column("error", UnicodeText),
)


# ---------------------------------------------------------------- 建立與初始化
def get_engine(url: str | None = None) -> Engine:
    url = url or config.db_url()
    if make_url(url).get_backend_name() == "mssql":
        ensure_mssql_database(url)
    return create_engine(url, future=True)


def ensure_mssql_database(url: str) -> None:
    """MS SQL 不會自動建資料庫：先連 master 建立。"""
    u = make_url(url)
    name = u.database
    master = create_engine(u.set(database="master"), isolation_level="AUTOCOMMIT")
    with master.connect() as conn:
        conn.execute(text(f"IF DB_ID(N'{name}') IS NULL CREATE DATABASE [{name}]"))
    master.dispose()


def init_db(engine: Engine, reset: bool = False) -> None:
    if reset:
        drop_views(engine)
        metadata.drop_all(engine)
    metadata.create_all(engine)
    if engine.dialect.name == "mssql":
        apply_sql_file(engine, config.ROOT / "sql" / "mssql" / "views.sql")


def drop_views(engine: Engine) -> None:
    if engine.dialect.name != "mssql":
        return
    with engine.begin() as conn:
        for v in conn.execute(text("SELECT name FROM sys.views WHERE name LIKE 'vw[_]%'")).scalars():
            conn.execute(text(f"DROP VIEW [{v}]"))


def apply_sql_file(engine: Engine, path: Path) -> None:
    """執行含 GO 分隔的 T-SQL 檔。"""
    batches, buf = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().upper() == "GO":
            batches.append("\n".join(buf))
            buf = []
        else:
            buf.append(line)
    batches.append("\n".join(buf))
    with engine.begin() as conn:
        for b in batches:
            if b.strip():
                conn.exec_driver_sql(b)


def _read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def seed_erp(engine: Engine, erp_dir: Path) -> dict[str, int]:
    """把模擬 ERP 的 CSV 匯入資料庫（會覆蓋既有主檔與採購單）。"""
    from datetime import date

    vendors = _read_csv(erp_dir / "vendors.csv")
    parts = _read_csv(erp_dir / "parts.csv")
    maps = _read_csv(erp_dir / "vendor_part_map.csv")
    pos = _read_csv(erp_dir / "purchase_orders.csv")
    with engine.begin() as conn:
        for t in (erp_purchase_order, erp_vendor_part_map):
            conn.execute(delete(t))
        if conn.execute(text("SELECT COUNT(*) FROM doc_document")).scalar() == 0:
            conn.execute(delete(erp_part))
            conn.execute(delete(erp_vendor))
            conn.execute(insert(erp_vendor), vendors)
            conn.execute(insert(erp_part), [
                dict(p, current_unit_price=Decimal(p["current_unit_price"])) for p in parts])
        conn.execute(insert(erp_vendor_part_map), maps)
        if conn.execute(text("SELECT COUNT(*) FROM ops_time_baseline")).scalar() == 0:
            now = datetime.now()
            conn.execute(insert(ops_time_baseline), [
                dict(task=k, minutes=Decimal(str(m)), description=d, source="假設值，待現場計時", updated_at=now)
                for k, (m, d) in config.MANUAL_BASELINE_MINUTES.items()])
        conn.execute(insert(erp_purchase_order), [
            dict(p, line=int(p["line"]), qty=int(p["qty"]), unit_price=Decimal(p["unit_price"]),
                 po_date=date.fromisoformat(p["po_date"]), due_date=date.fromisoformat(p["due_date"]))
            for p in pos])
    return dict(vendors=len(vendors), parts=len(parts), vendor_part_map=len(maps), po_lines=len(pos))
