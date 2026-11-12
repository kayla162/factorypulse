# Power Automate Desktop 版本：宇碩 Excel 出貨單 → 資料庫

同一段流程（讀 Excel 出貨單、寫進資料庫、檢查異常）用 Windows 11 內建的 Power Automate Desktop（PAD）
再做一次，用來比較低程式碼工具和 Python 各自適合什麼情境。比較結論見 [docs/PAD與Python比較.md](../docs/PAD與Python比較.md)。

> 狀態：流程文字與步驟已寫好，**尚未在 PAD 實際執行**（PAD 是圖形介面工具，需要本人登入操作）。
> 跑完後請用下面第 5 步的比對指令驗證，並把實際耗時填進比較文件。

| 檔案 | 用途 |
|---|---|
| `出貨單匯入.txt` | 可直接貼進 PAD 設計工具的流程文字 |
| `檢查.sql` | 匯入後的異常檢查查詢（料號、採購單、單價、數量） |

## 執行步驟

1. **準備資料庫**：`docker compose up -d mssql`，再執行 `python -m factorypulse init-db`（會建立 `pad_delivery_staging` 表）與 `python -m factorypulse seed`。
2. **建立流程**：開啟 Power Automate → 新增流程 → 命名「FactoryPulse 出貨單匯入」。
3. **建立輸入變數**（右側「輸入/輸出變數」→ 新增輸入）：
   - `FolderPath`（文字）：例如 `C:\Users\<你>\OneDrive\桌面\履歷\factorypulse\testdata\batch01\documents`
   - `DbPassword`（文字，**勾選「敏感」**）：`.env` 的 `MSSQL_SA_PASSWORD`。密碼不要寫死在流程裡。
4. **貼上流程**：用記事本打開 `出貨單匯入.txt`，從第 4 行（`DateTime.GetCurrentDateTime...`）複製到最後，
   在設計工具的 Main 頁籤按 Ctrl+V。執行，完成時會跳出「出貨單 2 份、明細 4 列」。
5. **比對結果**：`python -m factorypulse pad-compare`，報告寫到 `reports/pad_compare.md`。
   預期：PAD 讀到 C_delivery_01、C_delivery_02，欄位與 Python 一致；`檢查.sql` 會標出 C_delivery_01 的 `YS-SN-2O0` 料號對不到。

## 貼上失敗時：手動建立（約 20 分鐘）

不同版本的 PAD 動作名稱可能略有差異，貼上若出現錯誤，照下表在設計工具裡拖拉建立：

| # | 動作（左側動作清單） | 主要設定 |
|---|---|---|
| 1 | 日期時間 › 取得目前日期與時間 | 產生 `CurrentDateTime` |
| 2 | 文字 › 將日期時間轉換為文字 | 自訂格式 `yyyyMMdd-HHmmss`，產生 `RunTag` |
| 3 | 變數 › 設定變數 ×2 | `FileCount` = 0、`RowCount` = 0 |
| 4 | 資料庫 › 開啟 SQL 連線 | 連線字串：`Provider=SQLOLEDB;Data Source=127.0.0.1,14330;Initial Catalog=factorypulse;User ID=sa;Password=%DbPassword%;` |
| 5 | 資料夾 › 取得資料夾中的檔案 | 資料夾 `%FolderPath%`，篩選 `*.xlsx`，依名稱排序，產生 `Files` |
| 6 | 迴圈 › For each | 逐一處理 `%Files%`，目前項目 `CurrentFile` |
| 7 | Excel › 啟動 Excel | 開啟 `%CurrentFile%`，不顯示、唯讀，產生 `ExcelInstance` |
| 8 | Excel › 讀取 Excel 工作表 | 儲存格 A2（欄 1、列 2）→ `Title` |
| 9 | 條件 › If | `%Title%` 包含 `出貨` |
| 10 | Excel › 讀取 ×3 | B4 → `DocNo`、E4 → `DocDate`、B5 → `PoNo` |
| 11 | 變數 › 設定變數 | `RowNo` = 8 |
| 12 | 迴圈 › 迴圈條件 | `%RowNo%` ≤ 200 |
| 13 | Excel › 讀取 | 範圍 A`%RowNo%`:D`%RowNo%` → `Cells` |
| 14 | 條件 › If ＋ 結束迴圈 | `%Cells[0][0]%` 是空的 → 結束迴圈 |
| 15 | 文字 › 取代文字 ×6 | 料號、數量、單價、單號、日期、採購單，各把 `'` 換成 `''` |
| 16 | 資料庫 › 執行 SQL 陳述式 | 見 `出貨單匯入.txt` 的 INSERT |
| 17 | 變數 › 增加變數 ×2 | `RowNo` +1、`RowCount` +1；迴圈外 `FileCount` +1 |
| 18 | Excel › 關閉 Excel | 每個檔案處理完關閉 |
| 19 | 資料庫 › 關閉 SQL 連線、訊息方塊 › 顯示訊息 | 顯示份數與列數 |

## 常見問題

- **連不到資料庫**：確認容器在跑（`docker compose ps`）、埠是 14330。`SQLOLEDB` 是 Windows 內建的舊版驅動程式，
  若公司 SQL Server 強制加密連線，要改裝 Microsoft OLE DB Driver for SQL Server，並把 Provider 改成 `MSOLEDBSQL`。
- **Excel 沒有關掉**：流程中途出錯時 Excel 會留在背景。可在 PAD 加「發生錯誤時」區塊關閉 Excel。
- **讀到空白或錯位**：PAD 版本用固定儲存格位置（B4、第 8 列起），供應商改版面就要改流程；Python 版是找「料號」表頭，不受影響。
