"""產生 MES 模擬資料（產線 4 週）：工單、領料批號、AOI 逐片判定。

故事：batch01 的進料檢驗中，宇碩鍍金加工批號 L261012-01 不良率 2% 超過 AQL，供應商卻判 OK
（FactoryPulse 已發出 DEFECT_RATE_HIGH 提醒，但仍入帳）。這批料在 10/28–11/05 用在 A 線，
AOI 不良率從約 1.2% 升到約 5.7%。另外 B 線 11/10 有一天設備異常（跟批號無關），用來檢查
回查功能不會亂怪批號。

AOI 判定格式沿用 aoi-patchcore：分數越高越可疑，分數 ≥ 成本最佳門檻判 NG。
逐片分數是模擬的，不是真的跑模型。

刻意放入 MES／AOI 匯出常見的髒資料：線別寫法不一、日期格式混用、重複列、AOI 複判、
分數缺漏、批號格式不一。數量都記在 answers/mes_truth.json。

用法：python scripts/make_mes_data.py [輸出資料夾]
"""
from __future__ import annotations

import csv
import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 20261113
ROOT = Path(__file__).resolve().parents[1]
BATCH01 = ROOT / "testdata" / "batch01"
START, END = date(2026, 10, 19), date(2026, 11, 12)

# 取自 aoi-patchcore outputs/full_center/metal_nut/results.json（MVTec 沒有連接器，用金屬件模型代表）
AOI_MODEL = {
    "model_version": "patchcore-wrn50-metal_nut-center",
    "source": "kayla162/aoi-patchcore outputs/full_center/metal_nut/results.json",
    "threshold": 0.755866527557373,
    "threshold_basis": "cost_optimal（漏檢:過殺 成本 100:1，不良率先驗 1%）",
    "image_auroc": 0.9990224828934506,
    "latency_ms_per_image": 62.13,
}

LINES = {"A": "A線", "B": "B線"}
LINE_ALIASES = {"A": ["A線", "LINE-A", "Line A", "a線"], "B": ["B線", "LINE-B", "Line B", "b線"]}
PRODUCTS = {
    "A": ("CN-20P-G", "20P 鍍金連接器", [("FP-TM-1001", 20), ("FP-HS-3001", 1), ("FP-PL-4001", 20)]),
    "B": ("CN-40P-S", "40P 鍍錫連接器", [("FP-TM-1002", 40), ("FP-HS-3002", 1), ("FP-PL-4002", 40)]),
}
BASE_DEFECT = {"A": 0.012, "B": 0.010}
BAD_LOT = ("FP-PL-4001", "L261012-01")
BAD_LOT_DAYS = (date(2026, 10, 28), date(2026, 11, 5))
BAD_LOT_EXTRA = 0.045
SPIKE = ("B", date(2026, 11, 10), 0.045)   # 設備異常：跟批號無關
# 前期庫存批號（沒有進料檢驗紀錄，模擬系統導入前就在倉庫的料）
OPENING_LOTS = {
    "FP-TM-1001": ["L260928-11"], "FP-TM-1002": ["L260928-12"],
    "FP-HS-3001": ["L260929-21", "L261003-21"], "FP-HS-3002": ["L260929-22", "L261003-22"],
    "FP-PL-4001": ["L260930-31"], "FP-PL-4002": ["L260930-32"],
}


def lot_pool() -> dict[str, list[tuple[str, date]]]:
    pool: dict[str, list[tuple[str, date]]] = {p: [(lot, date(2026, 10, 1)) for lot in lots]
                                               for p, lots in OPENING_LOTS.items()}
    for f in sorted((BATCH01 / "answers").glob("*_inspection_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for it in d["items"]:
            entry = (it["lot_no"], date.fromisoformat(d["doc_date"]))
            if it["part_no"] in pool and entry not in pool[it["part_no"]]:
                pool[it["part_no"]].append(entry)
    return pool


def workdays():
    d = START
    while d <= END:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def messy_lot(lot: str, rng: random.Random) -> str:
    r = rng.random()
    if r < 0.06:
        return lot.lower()
    if r < 0.12:
        return lot.replace("-", " ")
    if r < 0.15:
        return f" {lot} "
    return lot


def main(out: Path):
    rng = random.Random(SEED)
    drop = out / "drop"
    (out / "answers").mkdir(parents=True, exist_ok=True)
    drop.mkdir(parents=True, exist_ok=True)
    (drop / "aoi_model.json").write_text(json.dumps(AOI_MODEL, ensure_ascii=False, indent=2), encoding="utf-8")
    thr = AOI_MODEL["threshold"]
    pool = lot_pool()
    truth = {"bad_lot": {"part_no": BAD_LOT[0], "lot_no": BAD_LOT[1], "line": "A",
                         "from": BAD_LOT_DAYS[0].isoformat(), "to": BAD_LOT_DAYS[1].isoformat()},
             "spike": {"line": SPIKE[0], "date": SPIKE[1].isoformat(), "cause": "設備異常，與批號無關"},
             "expected_signal_days": {"A": [], "B": []}, "dirty": {}, "true_rates": {}}
    dirty = {"wo_duplicate_rows": 0, "wo_line_alias": 0, "wo_slash_dates": 0, "issue_messy_lot": 0,
             "aoi_duplicate_rows": 0, "aoi_missing_score": 0, "aoi_retest_rows": 0, "aoi_line_alias": 0,
             "aoi_slash_timestamps": 0}

    wo_rows, issue_rows = [], []
    aoi_by_day: dict[date, list[list]] = {}
    for day in workdays():
        for line in "AB":
            product, _, bom = PRODUCTS[line]
            n_wo = 2 if rng.random() < 0.3 else 1
            for k in range(1, n_wo + 1):
                wo = f"WO{day:%y%m%d}{line}{k}"
                qty = rng.randint(220, 300) if n_wo == 2 else rng.randint(450, 600)
                alias = LINES[line] if rng.random() > 0.15 else rng.choice(LINE_ALIASES[line])
                dirty["wo_line_alias"] += alias != LINES[line]
                dstr = day.isoformat() if rng.random() > 0.2 else f"{day.year}/{day.month}/{day.day}"
                dirty["wo_slash_dates"] += "/" in dstr
                row = [wo, product, alias, dstr, qty]
                wo_rows.append(row)
                if rng.random() < 0.03:
                    wo_rows.append(list(row))
                    dirty["wo_duplicate_rows"] += 1

                # 領料：每個 BOM 料號挑一個「已到貨」的批號
                used = {}
                for part, per in bom:
                    avail = [lot for lot, recv in pool[part] if recv <= day]
                    if (part, line) == (BAD_LOT[0], "A"):
                        if BAD_LOT_DAYS[0] <= day <= BAD_LOT_DAYS[1]:
                            lot = BAD_LOT[1]
                        else:
                            lot = rng.choice([x for x in avail if x != BAD_LOT[1]])
                    else:
                        lot = rng.choice(avail)
                    used[part] = lot
                    raw = messy_lot(lot, rng)
                    dirty["issue_messy_lot"] += raw != lot
                    issue_rows.append([wo, part, raw, qty * per])

                p = BASE_DEFECT[line]
                if used.get(BAD_LOT[0]) == BAD_LOT[1]:
                    p += BAD_LOT_EXTRA
                if (line, day) == SPIKE[:2]:
                    p = SPIKE[2]
                truth["true_rates"][wo] = round(p, 4)

                for i in range(1, qty + 1):
                    sn = f"{wo}-{i:04d}"
                    defect = rng.random() < p
                    score = rng.gauss(0.88, 0.06) if defect else rng.gauss(0.45, 0.08)
                    score = min(max(score, 0.0), 1.0)
                    t = datetime(day.year, day.month, day.day, 8) + timedelta(minutes=rng.randint(0, 660),
                                                                               seconds=rng.randint(0, 59))
                    ts = t.strftime("%Y-%m-%dT%H:%M:%S+08:00")
                    if rng.random() < 0.05:
                        ts = t.strftime("%Y/%m/%d %H:%M:%S")
                        dirty["aoi_slash_timestamps"] += 1
                    la = LINES[line] if rng.random() > 0.05 else rng.choice(LINE_ALIASES[line])
                    dirty["aoi_line_alias"] += la != LINES[line]
                    decision = "NG" if score >= thr else "OK"
                    s_txt = f"{score:.4f}"
                    if rng.random() < 0.001:
                        s_txt = ""   # 影像讀取失敗，分數缺漏
                        dirty["aoi_missing_score"] += 1
                    row = [ts, la, "AOI-1", wo, sn, f"{sn}.png", s_txt, f"{thr:.4f}",
                           decision if s_txt else "", AOI_MODEL["model_version"]]
                    rows = aoi_by_day.setdefault(day, [])
                    rows.append(row)
                    if rng.random() < 0.002:
                        rows.append(list(row))
                        dirty["aoi_duplicate_rows"] += 1
                    # AOI 判 NG 但其實是良品（過殺）：送複判站重拍一次，最後判定以最後一次為準
                    if decision == "NG" and not defect and s_txt:
                        t2 = t + timedelta(minutes=12)
                        rows.append([t2.strftime("%Y-%m-%dT%H:%M:%S+08:00"), la, "AOI-RECHECK", wo, sn,
                                     f"{sn}_r.png", "0.5100", f"{thr:.4f}", "OK", AOI_MODEL["model_version"]])
                        dirty["aoi_retest_rows"] += 1

        for line in "AB":
            in_bad = line == "A" and BAD_LOT_DAYS[0] <= day <= BAD_LOT_DAYS[1]
            if in_bad or (line, day) == SPIKE[:2]:
                truth["expected_signal_days"][line].append(day.isoformat())

    head_aoi = ["inspected_at", "line", "station", "work_order", "unit_sn", "image_file", "anomaly_score",
                "threshold", "decision", "model_version"]
    for day, rows in aoi_by_day.items():
        rng.shuffle(rows)   # AOI 匯出不保證依時間排序
        write_csv(drop / f"aoi_results_{day:%Y%m%d}.csv", head_aoi, rows)
    write_csv(drop / "work_orders.csv", ["wo_no", "product_no", "line", "prod_date", "planned_qty"], wo_rows)
    write_csv(drop / "material_issues.csv", ["wo_no", "part_no", "lot_no", "issued_qty"], issue_rows)
    write_csv(out / "products.csv", ["product_no", "name", "line", "bom"],
              [[p, n, LINES[l], ";".join(f"{a}x{b}" for a, b in bom)] for l, (p, n, bom) in PRODUCTS.items()])
    truth["dirty"] = dirty
    (out / "answers" / "mes_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    n_aoi = sum(len(r) for r in aoi_by_day.values())
    print(f"完成：工單 {len(wo_rows)} 列、領料 {len(issue_rows)} 列、AOI {n_aoi:,} 列（{len(aoi_by_day)} 天）→ {out}")


def write_csv(path, head, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "testdata" / "mes_batch01")
