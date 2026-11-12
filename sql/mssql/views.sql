-- FactoryPulse 報表用 View（T-SQL，SQL Server 2016 SP1 以上）
-- 由 `python -m factorypulse init-db` 在 MS SQL 上自動建立。

-- 採購單每一行的到貨狀況：已收、未收、是否逾期
CREATE OR ALTER VIEW dbo.vw_po_line_status AS
SELECT
    po.po_no,
    po.line,
    po.vendor_code,
    v.vendor_name,
    po.part_no,
    p.name                                         AS part_name,
    po.qty                                         AS po_qty,
    ISNULL(r.received_qty, 0)                      AS received_qty,
    po.qty - ISNULL(r.received_qty, 0)             AS open_qty,
    po.unit_price                                  AS po_unit_price,
    po.due_date,
    r.last_receipt_date,
    CASE
        WHEN ISNULL(r.received_qty, 0) >= po.qty THEN N'已結案'
        WHEN po.due_date < CAST(GETDATE() AS date) THEN N'逾期未交'
        WHEN r.received_qty > 0 THEN N'部分到貨'
        ELSE N'未到貨'
    END                                            AS line_status,
    CASE WHEN r.last_receipt_date > po.due_date
         THEN DATEDIFF(day, po.due_date, r.last_receipt_date) ELSE 0 END AS days_late
FROM dbo.erp_purchase_order AS po
JOIN dbo.erp_vendor AS v ON v.vendor_code = po.vendor_code
JOIN dbo.erp_part   AS p ON p.part_no = po.part_no
OUTER APPLY (
    SELECT SUM(gr.qty) AS received_qty, MAX(gr.receipt_date) AS last_receipt_date
    FROM dbo.erp_goods_receipt AS gr
    WHERE gr.po_no = po.po_no AND gr.po_line = po.line
) AS r;
GO

-- 尚未處理的異常，給每日通知與儀表板使用
CREATE OR ALTER VIEW dbo.vw_open_anomaly AS
SELECT
    a.id,
    a.code,
    a.severity,
    a.owner,
    v.vendor_name,
    d.doc_type,
    d.doc_no,
    d.po_no,
    a.part_no,
    a.detail,
    a.created_at,
    a.notified_at,
    DATEDIFF(hour, a.created_at, GETDATE()) AS open_hours
FROM dbo.ops_anomaly AS a
JOIN dbo.doc_document AS d ON d.id = a.document_id
LEFT JOIN dbo.erp_vendor AS v ON v.vendor_code = d.vendor_code
WHERE a.resolved_at IS NULL;
GO

-- 供應商來料品質：依批號彙總不良率，之後串接產線良率做追溯
CREATE OR ALTER VIEW dbo.vw_vendor_incoming_quality AS
SELECT
    p.vendor_code,
    v.vendor_name,
    ii.part_no,
    COUNT(*)                                                    AS lots,
    SUM(ii.sample_qty)                                          AS sampled,
    SUM(ii.defect_qty)                                          AS defects,
    CAST(SUM(ii.defect_qty) * 1.0 / NULLIF(SUM(ii.sample_qty), 0) AS decimal(9, 4)) AS defect_rate,
    SUM(CASE WHEN ii.vendor_result = N'NG' THEN 1 ELSE 0 END)   AS ng_lots
FROM dbo.erp_incoming_inspection AS ii
JOIN dbo.erp_part   AS p ON p.part_no = ii.part_no
JOIN dbo.erp_vendor AS v ON v.vendor_code = p.vendor_code
GROUP BY p.vendor_code, v.vendor_name, ii.part_no;
GO

-- 每日良率（給 Power BI／Excel 樞紐直接連）
CREATE OR ALTER VIEW dbo.vw_yield_daily AS
SELECT
    y.prod_date,
    y.line,
    y.units,
    y.ng_units,
    y.yield_rate,
    CAST(y.ng_units * 1.0 / NULLIF(y.units, 0) AS decimal(7, 5)) AS ng_rate,
    s.ucl,
    s.center,
    CASE WHEN s.id IS NULL THEN 0 ELSE 1 END AS is_signal,
    s.suspect_lot_no,
    s.conclusion
FROM dbo.mart_yield_daily AS y
OUTER APPLY (
    SELECT TOP 1 * FROM dbo.mes_yield_signal AS x
    WHERE x.line = y.line AND x.prod_date = y.prod_date
    ORDER BY x.id
) AS s;
GO

-- 批號追溯：每個供應商批號的進料抽檢結果 vs 上線後的 AOI 不良率
CREATE OR ALTER VIEW dbo.vw_lot_traceability AS
SELECT
    mi.part_no,
    mi.lot_no,
    v.vendor_name,
    COUNT(DISTINCT mi.wo_no)                                         AS work_orders,
    SUM(w.units)                                                     AS units,
    SUM(w.ng_units)                                                  AS ng_units,
    CAST(SUM(w.ng_units) * 1.0 / NULLIF(SUM(w.units), 0) AS decimal(7, 5)) AS line_ng_rate,
    MAX(iqc.defect_rate)                                             AS iqc_defect_rate,
    MAX(iqc.vendor_result)                                           AS iqc_vendor_result
FROM dbo.mes_material_issue AS mi
JOIN dbo.mart_wo_yield AS w ON w.wo_no = mi.wo_no
JOIN dbo.erp_part AS p ON p.part_no = mi.part_no
JOIN dbo.erp_vendor AS v ON v.vendor_code = p.vendor_code
OUTER APPLY (
    SELECT CAST(SUM(ii.defect_qty) * 1.0 / NULLIF(SUM(ii.sample_qty), 0) AS decimal(7, 5)) AS defect_rate,
           MAX(ii.vendor_result) AS vendor_result
    FROM dbo.erp_incoming_inspection AS ii
    WHERE ii.part_no = mi.part_no AND ii.lot_no = mi.lot_no
) AS iqc
GROUP BY mi.part_no, mi.lot_no, v.vendor_name;
GO
