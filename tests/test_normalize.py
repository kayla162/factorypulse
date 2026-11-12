from datetime import date
from decimal import Decimal

import pytest

from factorypulse.normalize import PartResolver, normalize_lot, parse_date, parse_decimal, parse_int


@pytest.mark.parametrize("raw, expected", [
    ("L261026-01", "L261026-01"),
    ("1261026-01", "L261026-01"),    # OCR 把 L 讀成 1
    ("L261026~02", "L261026-02"),    # 把 - 讀成 ~
    ("l 261026 - 03", "L261026-03"),
    ("XYZ-123", "XYZ-123"),          # 不符合格式就原樣保留
])
def test_normalize_lot(raw, expected):
    assert normalize_lot(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("2026-10-18", date(2026, 10, 18)),
    ("2026/9/14", date(2026, 9, 14)),
    ("民國115年10月26日", date(2026, 10, 26)),
    ("115/10/26", date(2026, 10, 26)),
    ("２０２６／１０／０１", date(2026, 10, 1)),   # 全形
    ("不是日期", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_parse_numbers():
    assert parse_decimal("175,875.00") == Decimal("175875.00")
    assert parse_decimal(0.95) == Decimal("0.95")      # 不能變成 0.9499999...
    assert parse_int("20,000") == 20000
    assert parse_int("1.5") is None                     # 數量不該有小數


@pytest.fixture()
def resolver():
    return PartResolver(
        ["FP-HS-3001", "FP-PL-4001", "FP-PL-4002", "FP-TM-1001"],
        {("V002", "fphs3001"): "FP-HS-3001", ("V003", "YS-AU-020"): "FP-PL-4001",
         ("V003", "YS-SN-200"): "FP-PL-4002"})


def test_resolve_exact_and_vendor_map(resolver):
    assert resolver.resolve("FP-TM-1001", "V001").part_no == "FP-TM-1001"
    assert resolver.resolve("ＦＰ－ＴＭ－１００１", "V001").part_no == "FP-TM-1001"
    r = resolver.resolve("FPHS3001", "V002")             # 大小寫、連字號不同
    assert (r.part_no, r.method) == ("FP-HS-3001", "vendor_map")
    assert resolver.resolve("YS-AU-020", "V003").part_no == "FP-PL-4001"


def test_vendor_map_is_per_vendor(resolver):
    # 別家供應商用同樣的寫法，不能套用 V003 的對照
    assert resolver.resolve("YS-AU-020", "V001").part_no is None


def test_typo_is_suggested_not_auto_fixed(resolver):
    r = resolver.resolve("YS-SN-2O0", "V003")             # 數字 0 打成英文 O
    assert r.part_no is None
    assert r.suggestion == "FP-PL-4002"
