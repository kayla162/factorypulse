# 產線測試資料 mes_batch01（合成資料）

由 `scripts/make_mes_data.py` 產生，固定亂數種子，重跑結果相同。

| 檔案 | 內容 |
|---|---|
| `drop/work_orders.csv` | MES 工單（2026-10-19～11-12 平日，A 線 20P 鍍金、B 線 40P 鍍錫） |
| `drop/material_issues.csv` | 領料：每張工單用了哪些料號的哪些批號（批號來自 batch01 的進料檢驗＋前期庫存） |
| `drop/aoi_results_YYYYMMDD.csv` | AOI 逐片判定，格式與門檻沿用 aoi-patchcore（分數 ≥ 0.7559 判 NG） |
| `drop/aoi_model.json` | 門檻來源：aoi-patchcore metal_nut 模型的成本最佳門檻 |
| `answers/mes_truth.json` | 埋入的問題與髒資料數量 |

## 埋入的問題

| 問題 | 說明 |
|---|---|
| 壞料 | 宇碩鍍金批號 L261012-01（進料抽檢 2% 超過 AQL、供應商判 OK）在 10/28～11/05 用於 A 線，不良率約 1.2% → 5.7% |
| 設備異常 | B 線 11/10 單日不良率升高，與批號無關（用來檢查回查不會冤枉批號） |

## 埋入的髒資料

線別寫法不一（A線／LINE-A／Line A／a線）、日期與時間格式混用、完全重複的列、
AOI 分數缺漏、AOI 過殺後複判、批號格式不一（小寫、空格、前後空白）。

## 限制

- AOI 逐片分數是模擬的，不是真的跑 PatchCore 推論；分數分布依「良品 / 瑕疵品」各自的常態分布產生。
- 不做庫存扣帳：同一個批號可以被很多張工單領用，數量不受批量限制。
