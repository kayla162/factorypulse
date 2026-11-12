"""在真的 SQL Server 上跑一次完整評測（含 OCR）與 T-SQL View。

需要：docker compose up -d mssql，且 .env 的 FP_DB_URL 指向 MS SQL。
使用獨立的 factorypulse_test 資料庫，不會動到開發資料。
"""
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from factorypulse import config
from factorypulse.db import get_engine
from factorypulse.evaluate import run_eval

from .conftest import TESTDATA, needs_ocr

pytestmark = [pytest.mark.mssql, needs_ocr]


@pytest.fixture(scope="module")
def mssql():
    url = config.db_url()
    if make_url(url).get_backend_name() != "mssql":
        pytest.skip("FP_DB_URL 不是 MS SQL")
    url = make_url(url).set(database="factorypulse_test")
    try:
        eng = get_engine(url.render_as_string(hide_password=False))
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:  # 容器沒開就跳過，不讓整套測試失敗
        pytest.skip(f"連不到 SQL Server：{e}")
    res = run_eval(eng, TESTDATA, llm=None)
    yield eng, res
    eng.dispose()


def test_mes_on_mssql(mssql):
    """產線 2 萬多列 AOI 寫進 MS SQL，視窗函數彙總與 T-SQL View 要和 SQLite 結果一致。"""
    from factorypulse.evaluate_mes import run_mes_eval
    eng, _ = mssql
    r = run_mes_eval(eng, TESTDATA.parent / "mes_batch01", TESTDATA)
    assert r.etl_ok and not r.fn and not r.lot_wrong and r.spike_ok and len(r.lot_correct) == 7
    with eng.connect() as c:
        top = c.execute(text("SELECT TOP 1 part_no, lot_no, iqc_vendor_result FROM vw_lot_traceability "
                             "ORDER BY line_ng_rate DESC")).first()
        assert tuple(top) == ("FP-PL-4001", "L261012-01", "OK")   # 供應商判 OK，上線後 NG 率最高
        signals = c.execute(text("SELECT COUNT(*) FROM vw_yield_daily WHERE is_signal = 1")).scalar()
        assert signals == len(r.flagged)
    run_eval(eng, TESTDATA, llm=None)   # 還原成文件評測的狀態，給下一個測試用


def test_eval_on_mssql(mssql):
    _, res = mssql
    assert res.fp == set() and res.fn == set() and len(res.tp) == 7
    assert res.mail_errors == []


def test_tsql_views(mssql):
    eng, _ = mssql
    with eng.connect() as c:
        status = dict(c.execute(text(
            "SELECT line_status, COUNT(*) FROM vw_po_line_status GROUP BY line_status")).all())
        # PO26100005 的出貨單被擋下（料號打錯）→ 2 行未交；PO26100003 第 1 行短交 1,200 → 未交
        assert status["已結案"] == 11 and sum(status.values()) == 14
        short = c.execute(text("SELECT open_qty FROM vw_po_line_status "
                               "WHERE po_no = 'PO26100003' AND line = 1")).scalar()
        assert short == 1200
        late = c.execute(text("SELECT MAX(days_late) FROM vw_po_line_status WHERE po_no = 'PO26100004'")).scalar()
        assert late == 6
        open_codes = sorted(c.execute(text("SELECT code FROM vw_open_anomaly")).scalars())
        assert open_codes == ["DEFECT_RATE_HIGH", "INSPECTION_NG", "LATE_DELIVERY", "PARSE_CHECK_FAILED",
                              "PARSE_CHECK_FAILED", "PART_UNKNOWN", "PRICE_INCREASE", "PRICE_MISMATCH", "QTY_SHORT"]
        q = c.execute(text("SELECT ng_lots FROM vw_vendor_incoming_quality WHERE part_no = 'FP-SH-2001'")).scalar()
        assert q == 1
        name = c.execute(text("SELECT vendor_name FROM erp_vendor WHERE vendor_code = 'V002'")).scalar()
        assert name == "恆昇塑膠工業有限公司"   # 中文存進 NVARCHAR 再讀出來要一致
