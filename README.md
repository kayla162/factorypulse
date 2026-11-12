# FactoryPulse 連接器廠進料到產線自動化

供應商每天寄來報價單、出貨單、品檢報告：有數位 PDF、有掃描傳真、有各家自訂的 Excel。
FactoryPulse 自動收信、分類、解析附件、對照採購單、寫入 ERP（MS SQL），
發現單價不符、短交、延遲、品質異常時推送 LINE 給負責人。
產線端匯入 MES 工單、領料批號與 AOI 判定，用 p 管制圖監控良率，良率掉下來時自動回查是哪個供應商的哪一批料。
每天產出主管看的 Excel 日報與網頁儀表板，並記錄每一次執行，算出省下多少人工時間。

> 目前進度：**第三週**。所有公司、料號、人名都是虛構的合成資料；AOI 逐片分數是模擬的。

## 一個完整的故事

1. **進料**（第一週）：宇碩寄來鍍金加工的檢驗報告，批號 `L261012-01` 抽檢不良 2%，超過 AQL 1%，供應商卻判 OK。
   FactoryPulse 發出「不良率超過 AQL」提醒給品保，但這批料照常入帳。
2. **上線**（第三週）：這批料在 10/28～11/05 用在 A 線，AOI 不良率從約 1% 升到 5～7%。
3. **偵測與回查**：p 管制圖在 10/28 失控，系統比對當天用到的每個批號，指出 `L261012-01` 在其他工單的不良率
   是同料號其他批號的 5.2 倍（p≈1e-42），並附上第 1 步那份檢驗報告的警訊，通知品保隔離剩餘庫存。
4. **不亂怪**：B 線 11/10 的設備異常也被偵測到，但當天用的批號在別天都正常，系統判定「跟批號無關，疑似製程或設備」。

## 產線成果（testdata/mes_batch01：2 條線 × 19 個工作天、50 張工單、20,557 筆 AOI 判定）

| 指標 | 結果 |
|---|---|
| ETL 清洗 | 重複列 58/58 剔除、分數缺漏 21/21 擋下、批號格式 19/19 統一、線別與時間格式 1,790 列自動修正 |
| 良率異常偵測 | 埋入 8 天全部抓到；誤報 1 天（38 個線別×日中，屬 3σ 管制圖的隨機誤報） |
| 批號回查 | 壞料 7 天全部指向正確批號；設備異常那天沒有冤枉任何批號 |
| 通知 | 9 個失控點合併成 2 則事件，不洗版 |
| 處理時間 | ETL＋管制圖＋回查約 3 秒 |

完整報告：`python -m factorypulse eval-mes` → [reports/eval_mes_batch01.md](reports/eval_mes_batch01.md)

## 進料成果（testdata/batch01：3 家供應商 × 18 份文件、23 封信、7 筆埋入異常）

| 指標 | 第一週 | 第二週 |
|---|---|---|
| 可自動處理的文件 | 12 / 18 | **18 / 18**（加入掃描檔 OCR） |
| 欄位正確率 | 156 / 156 | **256 / 258**：錯的 2 格被印章蓋住，系統讀不到就主動送人工，沒有亂猜 |
| 異常偵測 | 4 / 7，誤報 0 | **7 / 7，誤報 0** |
| 信件分類 | — | 規則 18 / 18；其餘 5 封交給 LLM（未設金鑰時轉人工，**0 封分錯**） |
| 省下時間（假設的人工基準） | — | 全人工 137 分鐘 → 剩 14 分鐘 |
| 測試 | 56 項 | 69 項（第三週累計 **88 項**，含 MS SQL、OCR、API、產線） |

完整報告：`python -m factorypulse eval` → [reports/eval_batch01.md](reports/eval_batch01.md)

## 架構

```
供應商 ──SMTP──► Mailpit（測試信箱）
                    ▲ 每 5 分鐘
n8n ── POST /api/poll ──► FactoryPulse API ──► 解析 → 比對 → 寫入 MS SQL
 │                              │
 │◄────── 要發送的通知 ─────────┘
 └──► LINE Messaging API（未設定時送到本機假 LINE）
n8n ── 平日 17:30 POST /api/reports/daily ──► Excel 日報 ＋ LINE 摘要
n8n ── 上班時間每小時 POST /api/mes/run ──► MES／AOI 匯出檔 ETL → p-chart → 批號回查 → LINE
n8n ── 任一流程出錯 ──► LINE 警示
瀏覽器 ── /dashboard ──► 自動化成效、良率管制圖、回查結論、批號品質
```

產線的處理：

```
MES 匯出（工單、領料）＋ AOI 匯出（逐片分數與判定）
  → 清洗：線別／日期／時區／批號格式統一，重複列剔除，缺分數擋下（保留原始列到 etl_reject）
  → 寫入 mes_*（同一檔案不重複匯入；當天重新匯出則以新檔取代）
  → 彙總：每片取最後一次判定（AOI 過殺後複判 OK 以複判為準），用 SQL 視窗函數算工單與每日良率
  → p 管制圖：每天樣本數不同，管制界限逐點計算；中心線用 Phase I 迭代估計，避免異常期間把中心線拉高
     規則：超出管制上限、連續 7 點高於中心線
  → 回查：當天用到的每個批號，「排除當天工單後」在其他工單的不良率 vs 同料號其他批號（單尾雙比例 z 檢定）
     是料的問題 → 別的工單也偏高；一天的設備異常 → 同批料在別天正常
  → 連結進料檢驗紀錄與當時發出的異常，合併成事件通知品保
```

單封信的處理：

```
.eml → 依寄件網域認供應商 → 附件
  ├─ 數位 PDF：文字＋座標
  ├─ 掃描 PDF：RapidOCR（CPU、地端）→ 校正歪斜 → 簡轉繁 → 文字＋座標
  └─ Excel：找表頭列、挑對工作表
        ↓ 共用表格抽取（欄名同義詞表）
  料號對應（主檔 → 供應商對照表 → 容忍格式；打錯只給建議）
  驗算（數量×單價＝金額、合計、稅額、檢驗欄位齊全）
  比對 ERP（採購單、未交量、交期、現行單價、AQL）
        ↓
  無異常 → 入帳｜alert → 入帳＋通知｜review → 不入帳、送人工
        ↓
  信件分類：有單據 → 規則；沒有 → LLM Agent（查寄件者、查未結採購單）→ 驗證輸出
```

## 設計取捨

- **異常判斷不用 LLM**：單價、數量、交期都有標準答案，程式比對可重現、可測試、零成本。
- **LLM 只用在規則判斷不了的信**（23 封中的 5 封）。模型透過 Tool Calling 查寄件者身分與未結採購單，
  輸出一律驗證：類別必須在清單內、信心 ≥ 0.7、日期格式正確；模型說的採購單號要回 ERP 查得到才採信。
  API 逾時、額度用完、一直呼叫工具不收尾，都會轉人工，不會讓整批停下。
- **OCR 讀不到就送人工，不猜**：被印章蓋住的抽樣數，系統標示「可能被印章或污損遮住」交給人。
  只有格式固定的欄位（批號 `L`＋6 碼－2 碼）才依格式修正 OCR 常見錯字。
- **兩種嚴重度**：價格或數量不符照常入帳並通知；料號對不到、驗算不過則整份擋下。
- **可重跑**：同一封信、同一份附件、同一張單號換檔名重寄，都不會重複入帳。
- **省時用可查證的方式算**：人工基準存在資料表、記錄來源；目前是假設值，導入時用現場計時更新。

## 快速開始

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[mssql,ocr,llm,api,dev]"

# 不用 Docker：SQLite 跑評測
python -m factorypulse --db-url sqlite:///data/eval.db eval --no-llm
```

### 全套（MS SQL ＋ Mailpit ＋ API ＋ n8n）

```bash
copy .env.example .env            # 填 MSSQL 密碼、FP_API_TOKEN；確認同意 SQL Server 授權條款
docker compose up -d --build
python -m factorypulse init-db
python -m factorypulse seed
python scripts/send_test_mails.py # 把測試信寄到 Mailpit（http://localhost:8025）
```

匯入 n8n 流程（Git Bash 要先 `export MSYS_NO_PATHCONV=1`）：

```bash
docker compose exec n8n n8n import:workflow --separate --input=/workflows
```

打開 http://localhost:5678，對「FactoryPulse 收信與異常通知」「FactoryPulse 產線良率監控」按執行，
或把流程設為啟用讓它照排程跑。沒有設定 LINE 時，訊息會寫到 `outbox/line_mock.jsonl`；日報會出現在 `reports/`。

儀表板：http://localhost:8000/dashboard ，輸入 `.env` 的 `FP_API_TOKEN`（只存在該分頁，不放網址）。
本機示範嫌麻煩時，可在 `.env` 設 `FP_DASHBOARD_PUBLIC=1` 後 `docker compose up -d api`，儀表板就不用輸入金鑰。
這個設定只放行唯讀的儀表板資料，收信、入帳、匯入 MES 等會寫入資料的 API 仍然要金鑰；
因為所有埠只綁 127.0.0.1，只有本機看得到。正式導入應改接公司帳號登入（例如 AD／Microsoft 365 單一登入），不要開這個設定。
Power BI 可直接連 MS SQL 的 `vw_yield_daily`、`vw_lot_traceability`。

### 選填：Gemini 與 LINE

在 `.env` 填入後重新 `docker compose up -d`：

- `GEMINI_API_KEY`：啟用 LLM 信件分類（預設模型 `gemini-2.5-flash`，可用 `FP_GEMINI_MODEL` 換）
- `LINE_CHANNEL_ACCESS_TOKEN`、`LINE_TO`、`LINE_API_BASE=https://api.line.me`：改推真的 LINE

### 常用指令

| 指令 | 用途 |
|---|---|
| `python -m factorypulse run` | 處理 testdata 的信件（不經 Mailpit） |
| `python -m factorypulse mes` | 匯入 MES／AOI 匯出檔、算良率管制圖、回查批號 |
| `python -m factorypulse eval-mes --strict` | 產線評測：ETL、良率異常偵測、批號回查 |
| `python -m factorypulse report` | 產生今天的 Excel 日報 |
| `python -m factorypulse stats` | 自動化率、人工介入率、省下的時間 |
| `python -m factorypulse baseline delivery 7.5 --source "採購部計時 20 份平均"` | 更新人工時間基準 |
| `python -m factorypulse status` | 最近執行紀錄＋採購單到貨（T-SQL View） |
| `python -m factorypulse eval --strict --no-llm` | 對標準答案評分（CI 使用） |
| `pytest -q` | 全部測試；有開 SQL Server 時一併跑 MS SQL 整合測試 |

## 資料表

| 資料表 | 用途 |
|---|---|
| `erp_vendor`、`erp_part`、`erp_vendor_part_map`、`erp_purchase_order` | 模擬 ERP 主檔與採購單 |
| `erp_goods_receipt`、`erp_incoming_inspection` | 寫入結果：進貨、進料檢驗（含批號，第三週做良率追溯） |
| `doc_document`、`doc_line` | 收到的每份文件與明細（解析器、狀態、處理毫秒） |
| `mail_message` | 每封信的分類、方法（規則／LLM）、信心、工具呼叫、token |
| `ops_anomaly`、`ops_run_log`、`ops_time_baseline` | 異常、每次執行統計、人工時間基準 |
| `mes_work_order`、`mes_material_issue`、`mes_aoi_result` | MES 工單、領料批號、AOI 逐片判定 |
| `etl_file_log`、`etl_reject` | 每個匯入檔案的統計、被擋下的原始資料列 |
| `mart_wo_yield`、`mart_yield_daily`、`mes_yield_signal` | 工單與每日良率、管制圖失控點與回查結論 |

T-SQL View（[sql/mssql/views.sql](sql/mssql/views.sql)）：`vw_po_line_status`、`vw_open_anomaly`、`vw_vendor_incoming_quality`、
`vw_yield_daily`、`vw_lot_traceability`。

## 驗證狀態

| 項目 | 狀態 |
|---|---|
| PDF／Excel／OCR 解析、比對、入帳、通知 | 已用測試資料與 MS SQL 驗證 |
| n8n 收信、日報、產線良率流程 → API → 假 LINE | 已在本機 Docker（MS SQL）實際執行驗證 |
| 產線 ETL、p-chart、批號回查 | 已用 SQLite 與 MS SQL 驗證；**AOI 分數是模擬的**，尚未接真的 PatchCore 推論 |
| 網頁儀表板 | 已在瀏覽器載入確認（2 張管制圖、表格、無主控台錯誤）；未做手機版實機檢查 |
| LLM 分類流程與防呆 | 用假模型驗證流程；**尚未用真的 Gemini 跑過**，準確度待有金鑰後實測 |
| n8n 錯誤警示流程 | **未驗證**：n8n 只在排程等正式執行出錯時觸發，手動執行不會 |
| 推送真的 LINE | **未驗證**：需要 LINE 官方帳號金鑰 |

## 路線圖

- [x] **第一週**：MS SQL schema 與 View、PDF／Excel 解析、料號對應、驗算、異常比對、通知、執行紀錄、評測與 CI
- [x] **第二週**：掃描檔 OCR、信件分類（規則＋Gemini Tool Calling）、API、Mailpit、n8n 排程與 LINE、Excel 日報、省時統計
- [x] **第三週**：AOI 判定接入 MES、ETL、良率 p-chart、良率異常回查供應商批號、儀表板
- [ ] **第四週**：Power Automate Desktop 版本與比較、一頁式操作說明、交接文件

## 已知限制

- 合成文件比真實文件乾淨，正確率會偏高。之後要另外準備一組「實拍困難集」分開計分。
- 信件分類的 LLM 測試集只有 5 封，太少，數字參考價值有限；要擴充題庫（含提示注入的惡意信）。
- 人工時間基準是假設值，省時數字要等現場計時後才有意義。
- 目前一份文件只處理第一頁。
- 產線資料是模擬的，而且問題很「乾淨」（壞料只有一批、設備異常只有一天）。真實產線會有多個原因疊加，
  回查結論只能當作「優先調查方向」，最後要由品保現場確認。
- 回查用的是相關性檢定：一起使用的批號會互相牽連，系統會列出所有顯著的批號並提醒「可能是一起使用造成」。
