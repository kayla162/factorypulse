-- PAD 匯入後的異常檢查（T-SQL）。PAD 只負責搬資料，比對規則寫在這裡。
-- 在 SSMS／Azure Data Studio 執行，或在 PAD 加一個「執行 SQL 陳述式」動作。
DECLARE @run nvarchar(30) = (SELECT MAX(run_tag) FROM dbo.pad_delivery_staging);

WITH s AS (
    SELECT s.*,
           TRY_CAST(s.qty_text AS int)               AS qty,
           TRY_CAST(s.unit_price_text AS decimal(12, 4)) AS unit_price,
           COALESCE(p.part_no, m.part_no)            AS part_no
    FROM dbo.pad_delivery_staging AS s
    LEFT JOIN dbo.erp_part AS p ON p.part_no = UPPER(LTRIM(RTRIM(s.vendor_part_no)))
    LEFT JOIN dbo.erp_vendor_part_map AS m
           ON m.vendor_code = 'V003' AND UPPER(m.vendor_part_no) = UPPER(LTRIM(RTRIM(s.vendor_part_no)))
    WHERE s.run_tag = @run
)
SELECT s.file_name, s.doc_no, s.po_no, s.vendor_part_no, s.part_no, s.qty, s.unit_price,
       po.qty AS po_qty, po.unit_price AS po_price,
       CASE
           WHEN s.part_no IS NULL               THEN N'料號對不到'
           WHEN po.po_no IS NULL                THEN N'料號不在採購單上'
           WHEN s.unit_price <> po.unit_price   THEN N'單價與採購單不符'
           WHEN s.qty < po.qty                  THEN N'出貨數量短少'
           WHEN s.qty > po.qty                  THEN N'出貨數量超過採購'
           ELSE N'正常'
       END AS check_result
FROM s
LEFT JOIN dbo.erp_purchase_order AS po ON po.po_no = s.po_no AND po.part_no = s.part_no
ORDER BY s.file_name, s.id;
