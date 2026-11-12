"""產生 FactoryPulse 測試文件（第一批）。

3 家虛構供應商 × 3 種文件（報價單、出貨單、品檢報告）× 各 2 份，
外加模擬 ERP 資料表、Email（.eml）與每份文件的標準答案（JSON）。

所有公司名稱、料號、人名皆為虛構。固定亂數種子，重跑結果相同。

用法：python scripts/make_test_docs.py [輸出資料夾]
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import random
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pymupdf as fitz
import numpy as np
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SEED = 20261112
FONT_FILE = "C:/Windows/Fonts/msjh.ttc"  # 微軟正黑體（英數字為比例字型，不會溢出表格）
STAMP_FONT = "C:/Windows/Fonts/kaiu.ttf"
TAX = 0.05

# ---------------------------------------------------------------- 主檔資料
VENDORS = {
    "A": dict(code="V001", name="永晟精密金屬股份有限公司", short="永晟精密",
              domain="yongsheng-metal.example", contact="林育成", fmt="pdf"),
    "B": dict(code="V002", name="恆昇塑膠工業有限公司", short="恆昇塑膠",
              domain="hengsheng-plastic.example", contact="陳美玲", fmt="scan"),
    "C": dict(code="V003", name="宇碩表面處理科技股份有限公司", short="宇碩表面",
              domain="yushuo-surface.example", contact="王志豪", fmt="xlsx"),
}

# part_no, 品名, 規格, 供應商, ERP 現行單價, 供應商自己的料號寫法
PARTS = [
    ("FP-TM-1001", "端子 公端", "磷青銅 0.3t 鍍金 0.2µm", "A", 1.85, "FP-TM-1001"),
    ("FP-TM-1002", "端子 母端", "磷青銅 0.3t 鍍錫", "A", 1.42, "FP-TM-1002"),
    ("FP-SH-2001", "金屬外殼", "SUS304 0.2t", "A", 3.60, "FP-SH-2001"),
    ("FP-HS-3001", "塑膠本體 20P", "LCP 黑色 UL94V-0", "B", 2.75, "fphs3001"),
    ("FP-HS-3002", "塑膠本體 40P", "LCP 黑色 UL94V-0", "B", 4.10, "fphs3002"),
    ("FP-CP-3101", "防塵蓋", "PBT 白色", "B", 0.58, "fpcp3101"),
    ("FP-PL-4001", "電鍍加工 鍍金", "端子 Au 0.2µm / Ni 1.5µm", "C", 0.95, "YS-AU-020"),
    ("FP-PL-4002", "電鍍加工 鍍錫", "端子 Sn 2µm", "C", 0.32, "YS-SN-200"),
]

# 每家第 i 份文件要埋的錯（anomaly code）
PLANTED = {
    ("A", 0): {"delivery": [], "inspection": [], "quote": []},
    ("A", 1): {"delivery": ["PRICE_MISMATCH"], "inspection": ["INSPECTION_NG"], "quote": []},
    ("B", 0): {"delivery": ["QTY_SHORT"], "inspection": [], "quote": []},
    ("B", 1): {"delivery": ["LATE_DELIVERY"], "inspection": [], "quote": ["PRICE_INCREASE"]},
    ("C", 0): {"delivery": ["PART_UNKNOWN"], "inspection": ["DEFECT_RATE_HIGH"], "quote": []},
    ("C", 1): {"delivery": [], "inspection": [], "quote": []},
}
AQL_LIMIT = 0.01  # 不良率上限 1%


def roc(d: date) -> str:
    return f"民國{d.year - 1911}年{d.month:02d}月{d.day:02d}日"


# ---------------------------------------------------------------- PDF 繪圖
def pdf_text(page, x, y, s, size=10, color=(0, 0, 0)):
    page.insert_font(fontname="msjh", fontfile=FONT_FILE)
    page.insert_text((x, y), str(s), fontname="msjh", fontsize=size, color=color)


def pdf_table(page, x, y, widths, rows, row_h=20, size=9):
    total_w = sum(widths)
    for r, row in enumerate(rows):
        top = y + r * row_h
        page.draw_rect(fitz.Rect(x, top, x + total_w, top + row_h), color=(0, 0, 0), width=0.6)
        cx = x
        for w, cell in zip(widths, row):
            page.draw_line((cx, top), (cx, top + row_h), color=(0, 0, 0), width=0.6)
            pdf_text(page, cx + 4, top + row_h - 6, cell, size)
            cx += w
    return y + len(rows) * row_h


def render_pdf_layout_a(doc_type, d):
    """供應商 A：標準數位 PDF，表頭靠左、欄位齊全。"""
    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)
    v = VENDORS[d["vendor"]]
    pdf_text(page, 40, 60, v["name"], 15)
    pdf_text(page, 40, 78, f"Tel 03-4xx-xxxx　聯絡人：{v['contact']}　sales@{v['domain']}", 8)
    title = {"quote": "報 價 單", "delivery": "出 貨 單", "inspection": "出 貨 檢 驗 報 告"}[doc_type]
    pdf_text(page, 420, 60, title, 16)
    pdf_text(page, 40, 115, "客戶：鼎聯連接器股份有限公司 採購部")
    pdf_text(page, 360, 115, f"單號：{d['doc_no']}")
    pdf_text(page, 360, 132, f"日期：{d['doc_date']}")
    if d.get("po_no"):
        pdf_text(page, 40, 132, f"貴司採購單號：{d['po_no']}")
    y = 155
    if doc_type == "inspection":
        y = inspection_block(page, d, y)
    else:
        rows = [["項次", "料號", "品名 / 規格", "數量(PCS)", "單價", "金額"]]
        for i, it in enumerate(d["items"], 1):
            rows.append([i, it["raw_part"], f"{it['name']} {it['spec']}", f"{it['qty']:,}",
                         f"{it['unit_price']:.2f}", f"{it['amount']:,.2f}"])
        y = pdf_table(page, 40, y, [32, 85, 220, 65, 50, 63], rows)
        y = totals_block(page, d, y)
    footer(page, d, doc_type, y)
    return pdf


def render_pdf_layout_b(doc_type, d):
    """供應商 B：版面置中、欄位順序不同、民國日期、無格線（之後會轉成掃描檔）。"""
    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)
    v = VENDORS[d["vendor"]]
    title = {"quote": "估價單", "delivery": "送貨單", "inspection": "品質檢驗成績書"}[doc_type]
    pdf_text(page, 200, 55, v["name"], 14)
    pdf_text(page, 255, 82, title, 18)
    pdf_text(page, 40, 120, f"No. {d['doc_no']}")
    pdf_text(page, 360, 120, roc(date.fromisoformat(d["doc_date"])))
    pdf_text(page, 40, 138, "TO：鼎聯連接器 採購 張小姐")
    if d.get("po_no"):
        pdf_text(page, 360, 138, f"P/O：{d['po_no']}")
    y = 165
    if doc_type == "inspection":
        y = inspection_block(page, d, y)
    else:
        heads = ["數量", "品號", "品名", "規格", "單價", "小計"]
        xs = [40, 100, 190, 290, 430, 490]
        for x, h in zip(xs, heads):
            pdf_text(page, x, y, h, 10)
        page.draw_line((40, y + 6), (555, y + 6), width=0.8)
        y += 24
        for it in d["items"]:
            for x, s in zip(xs, [f"{it['qty']}", it["raw_part"], it["name"], it["spec"],
                                 f"{it['unit_price']}", f"{it['amount']:.0f}"]):
                pdf_text(page, x, y, s, 9)
            y += 20
        page.draw_line((40, y - 10), (555, y - 10), width=0.8)
        y = totals_block(page, d, y)
    footer(page, d, doc_type, y)
    return pdf


def totals_block(page, d, y):
    y += 22
    pdf_text(page, 380, y, f"未稅合計：{d['subtotal']:,.2f}")
    pdf_text(page, 380, y + 16, f"營業稅 5%：{d['tax']:,.2f}")
    pdf_text(page, 380, y + 32, f"含稅總計：{d['total']:,.2f}", 11)
    return y + 40


def inspection_block(page, d, y):
    rows = [["料號", "批號", "出貨數", "抽樣數", "不良數", "不良率", "判定"]]
    for it in d["items"]:
        rows.append([it["raw_part"], it["lot_no"], f"{it['qty']:,}", it["sample"], it["defects"],
                     f"{it['defects'] / it['sample']:.1%}", it["result"]])
    y = pdf_table(page, 40, y, [85, 95, 65, 55, 55, 55, 50], rows)
    y += 22
    pdf_text(page, 40, y, f"對應出貨單：{d['ref_delivery']}　抽樣依據：MIL-STD-105E 一般檢驗 II 級")
    pdf_text(page, 40, y + 16, "檢驗項目：外觀、尺寸（卡尺）、鍍層厚度（X-ray）、插拔力")
    return y + 30


def footer(page, d, doc_type, y):
    note = {"quote": f"報價有效期限：{d.get('valid_until', '')}　付款條件：月結 60 天",
            "delivery": f"預計送達：{d.get('ship_date', '')}　送貨地址：桃園市中壢區（虛構）",
            "inspection": "檢驗員：　　　　　主管："}[doc_type]
    pdf_text(page, 40, max(y + 30, 520), note, 9)
    pdf_text(page, 40, 800, "※ 本文件為 FactoryPulse 測試用合成資料，公司與內容皆為虛構", 7, (0.5, 0.5, 0.5))


# ---------------------------------------------------------------- 掃描效果
def to_scan(pdf: fitz.Document, rng: random.Random, stamp: str) -> Image.Image:
    pix = pdf[0].get_pixmap(dpi=110)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    # 印章（紅色，掃描後變深灰），刻意壓到部分文字
    w, h = img.size
    stamp_img = Image.new("L", (190, 190), 255)
    sd = ImageDraw.Draw(stamp_img)
    sd.ellipse((5, 5, 185, 185), outline=90, width=6)
    font = ImageFont.truetype(STAMP_FONT, 34)
    sd.text((95, 95), stamp, fill=90, font=font, anchor="mm")
    stamp_img = stamp_img.rotate(rng.uniform(-25, 25), fillcolor=255)
    sx, sy = int(w * 0.42), int(h * 0.27)  # 壓在合計附近
    region = img.crop((sx, sy, sx + 190, sy + 190))
    img.paste(Image.fromarray(np.minimum(np.array(region), np.array(stamp_img))), (sx, sy))
    # 歪斜、模糊、雜訊、對比變差
    img = img.rotate(rng.uniform(-2.2, 2.2), expand=True, fillcolor=235)
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.4, 0.9)))
    arr = np.array(img).astype(np.float32)
    arr = arr * 0.85 + 25 + np.random.default_rng(rng.randint(0, 10**6)).normal(0, 10, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=55)  # 傳真／掃描常見的壓縮痕跡
    return Image.open(buf)


# ---------------------------------------------------------------- Excel（供應商 C）
def render_xlsx(doc_type, d, path: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = {"quote": "報價", "delivery": "出貨明細", "inspection": "檢驗"}[doc_type]
    v = VENDORS[d["vendor"]]
    bold = Font(bold=True, size=14)
    ws.merge_cells("A1:G1")
    ws["A1"] = v["name"]
    ws["A1"].font = bold
    ws["A1"].alignment = Alignment(horizontal="center")
    ws.merge_cells("A2:G2")
    ws["A2"] = {"quote": "加工報價單", "delivery": "加工完成出貨單", "inspection": "鍍層檢驗報告"}[doc_type]
    ws["A2"].alignment = Alignment(horizontal="center")
    dd = date.fromisoformat(d["doc_date"])
    ws["A4"], ws["B4"] = "單據編號", d["doc_no"]
    ws["D4"], ws["E4"] = "日期", f"{dd.year}/{dd.month}/{dd.day}"  # 文字型日期，非 Excel 日期
    if d.get("po_no"):
        ws["A5"], ws["B5"] = "客戶PO", d["po_no"]
    thin = Side(style="thin")
    start = 7  # 表頭不在第一列
    if doc_type == "inspection":
        heads = ["本公司料號", "批號", "數量", "抽樣", "不良", "鍍層厚度(µm)", "判定"]
        rows = [[it["raw_part"], it["lot_no"], it["qty"], it["sample"], it["defects"],
                 it["thickness"], it["result"]] for it in d["items"]]
    else:
        heads = ["本公司料號", "加工內容", "數量", "單價(未稅)", "金額(未稅)", "金額(含稅)", "備註"]
        rows = [[it["raw_part"], f"{it['name']} {it['spec']}", it["qty"], it["unit_price"],
                 it["amount"], round(it["amount"] * (1 + TAX), 2), it.get("note", "")]
                for it in d["items"]]
    for c, h in enumerate(heads, 1):
        cell = ws.cell(start, c, h)
        cell.font = Font(bold=True)
    for r, row in enumerate(rows, start + 1):
        for c, val in enumerate(row, 1):
            ws.cell(r, c, val)
    for row in ws.iter_rows(min_row=start, max_row=start + len(rows), max_col=7):
        for cell in row:
            cell.border = Border(top=thin, bottom=thin, left=thin, right=thin)
    end = start + len(rows) + 2
    if doc_type != "inspection":
        ws.cell(end, 5, "含稅總計")
        ws.cell(end, 6, d["total"])
    else:
        ws.cell(end, 1, f"對應出貨單：{d['ref_delivery']}")
    ws.cell(end + 2, 1, "※ 測試用合成資料，公司與內容皆為虛構")
    for col, wdt in zip("ABCDEFG", [14, 30, 10, 12, 14, 14, 16]):
        ws.column_dimensions[col].width = wdt
    # 第二張工作表放無關內容，考驗解析程式會不會讀錯表
    ws2 = wb.create_sheet("匯款資訊")
    ws2["A1"] = "請匯款至：虛構銀行 中壢分行（測試資料）"
    wb.save(path)


# ---------------------------------------------------------------- 主流程
def main(out: Path):
    rng = random.Random(SEED)
    for sub in ["erp", "documents", "emails", "answers"]:
        (out / sub).mkdir(parents=True, exist_ok=True)

    parts = {p[0]: dict(part_no=p[0], name=p[1], spec=p[2], vendor=p[3], price=p[4], vendor_part=p[5])
             for p in PARTS}

    # ---- ERP 主檔
    write_csv(out / "erp/vendors.csv", ["vendor_code", "vendor_name", "email_domain"],
              [[v["code"], v["name"], v["domain"]] for v in VENDORS.values()])
    write_csv(out / "erp/parts.csv", ["part_no", "name", "spec", "vendor_code", "current_unit_price"],
              [[p["part_no"], p["name"], p["spec"], VENDORS[p["vendor"]]["code"], p["price"]]
               for p in parts.values()])
    write_csv(out / "erp/vendor_part_map.csv", ["vendor_code", "vendor_part_no", "part_no"],
              [[VENDORS[p["vendor"]]["code"], p["vendor_part"], p["part_no"]]
               for p in parts.values() if p["vendor_part"] != p["part_no"]])

    po_rows, manifest, email_labels = [], [], []
    base_day = date(2026, 9, 21)
    doc_seq = 0
    po_seq = 0

    for vk, v in VENDORS.items():
        vparts = [p for p in parts.values() if p["vendor"] == vk]
        for i in range(2):
            plant = PLANTED[(vk, i)]
            po_seq += 1
            po_no = f"PO2610{po_seq:04d}"
            po_date = base_day + timedelta(days=rng.randint(0, 10))
            due = po_date + timedelta(days=rng.choice([14, 21, 28]))
            chosen = rng.sample(vparts, k=rng.randint(2, len(vparts)))
            po_items = []
            for line, p in enumerate(chosen, 1):
                qty = rng.choice([5000, 10000, 20000, 30000, 50000])
                po_items.append(dict(p, qty=qty))
                po_rows.append([po_no, line, v["code"], p["part_no"], qty, p["price"],
                                po_date.isoformat(), due.isoformat()])

            # ---------- 報價單（對 ERP 現行價格）
            q_date = po_date - timedelta(days=rng.randint(10, 20))
            q_items = []
            for p in chosen:
                price = p["price"]
                if "PRICE_INCREASE" in plant["quote"] and not q_items:
                    price = round(price * 1.12, 2)  # 第一項漲價 12%
                q_items.append(item(p, rng.choice([10000, 20000, 50000]), price))
            quote = doc(vk, "quote", f"Q{q_date:%y%m}{rng.randint(100, 999)}", q_date, None, q_items)
            quote["valid_until"] = (q_date + timedelta(days=30)).isoformat()
            quote["expected_anomalies"] = [
                dict(code="PRICE_INCREASE", part_no=q_items[0]["part_no"],
                     detail=f"報價 {q_items[0]['unit_price']} 高於 ERP 現行單價 {parts[q_items[0]['part_no']]['price']}")
            ] if plant["quote"] else []

            # ---------- 出貨單（對採購單）
            ship = due - timedelta(days=rng.randint(1, 4))
            d_items = []
            anomalies = []
            for k, p in enumerate(po_items):
                qty, price, raw = p["qty"], p["price"], p["vendor_part"]
                if k == 0 and "PRICE_MISMATCH" in plant["delivery"]:
                    price = round(price + 0.15, 2)
                    anomalies.append(dict(code="PRICE_MISMATCH", part_no=p["part_no"],
                                          detail=f"出貨單價 {price} ≠ 採購單價 {p['price']}"))
                if k == 0 and "QTY_SHORT" in plant["delivery"]:
                    qty = p["qty"] - 1200
                    anomalies.append(dict(code="QTY_SHORT", part_no=p["part_no"],
                                          detail=f"出貨 {qty} < 採購 {p['qty']}，短少 1200"))
                if k == 1 and "PART_UNKNOWN" in plant["delivery"]:
                    raw = raw.replace("0", "O", 1)  # 數字 0 打成英文 O
                    anomalies.append(dict(code="PART_UNKNOWN", part_no=p["part_no"],
                                          detail=f"料號 {raw} 不在料號對照表，疑似 {p['vendor_part']} 打錯"))
                it = item(p, qty, price)
                it["raw_part"] = raw
                d_items.append(it)
            if "LATE_DELIVERY" in plant["delivery"]:
                ship = due + timedelta(days=6)
                anomalies.append(dict(code="LATE_DELIVERY", part_no=None,
                                      detail=f"出貨日 {ship} 晚於採購交期 {due}，延遲 6 天"))
            deliv = doc(vk, "delivery", f"D{ship:%y%m%d}{i + 1:02d}", ship, po_no, d_items)
            deliv["ship_date"] = ship.isoformat()
            deliv["expected_anomalies"] = anomalies

            # ---------- 品檢報告（對出貨單）
            i_items, i_anom = [], []
            for k, it in enumerate(d_items):
                sample = 200 if it["qty"] >= 10000 else 125
                defects = rng.randint(0, 1)
                result = "OK"
                if k == 0 and "INSPECTION_NG" in plant["inspection"]:
                    defects, result = 9, "NG"
                    i_anom.append(dict(code="INSPECTION_NG", part_no=it["part_no"],
                                       detail="供應商判定 NG 仍出貨"))
                if k == 0 and "DEFECT_RATE_HIGH" in plant["inspection"]:
                    defects = 4  # 2%，但供應商仍判 OK
                    i_anom.append(dict(code="DEFECT_RATE_HIGH", part_no=it["part_no"],
                                       detail=f"不良率 {defects / sample:.1%} 超過 AQL {AQL_LIMIT:.0%}，供應商卻判 OK"))
                # 品檢報告由供應商品保另外開立，料號用正確寫法（出貨單的手誤不會帶過來）
                i_items.append(dict(part_no=it["part_no"], raw_part=parts[it["part_no"]]["vendor_part"],
                                    name=it["name"],
                                    lot_no=f"L{ship:%y%m%d}-{k + 1:02d}", qty=it["qty"], sample=sample,
                                    defects=defects, result=result,
                                    thickness=round(rng.uniform(0.21, 0.26), 3) if vk == "C" else None))
            insp = doc(vk, "inspection", f"QC{ship:%y%m%d}{i + 1:02d}", ship, po_no, i_items, priced=False)
            insp["ref_delivery"] = deliv["doc_no"]
            insp["expected_anomalies"] = i_anom

            for dtype, d in [("quote", quote), ("delivery", deliv), ("inspection", insp)]:
                doc_seq += 1
                doc_id = f"{vk}_{dtype}_{i + 1:02d}"
                files = render(dtype, d, vk, out, doc_id, rng)
                d.update(doc_id=doc_id, files=files, vendor_code=v["code"], vendor_name=v["name"])
                (out / "answers" / f"{doc_id}.json").write_text(
                    json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                manifest.append([doc_id, v["short"], dtype, VENDORS[vk]["fmt"], files[0],
                                 d["doc_no"], d.get("po_no") or "",
                                 ";".join(a["code"] for a in d["expected_anomalies"]) or "-"])
                email_labels.append(make_email(out, d, dtype, files, rng))

    write_csv(out / "erp/purchase_orders.csv",
              ["po_no", "line", "vendor_code", "part_no", "qty", "unit_price", "po_date", "due_date"], po_rows)
    email_labels += noise_emails(out, rng)
    (out / "answers/emails.json").write_text(json.dumps(email_labels, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(out / "answers/manifest.csv",
              ["doc_id", "vendor", "type", "format", "file", "doc_no", "po_no", "expected_anomalies"], manifest)
    print(f"完成：{len(manifest)} 份文件、{len(email_labels)} 封 Email → {out}")


def item(p, qty, price):
    return dict(part_no=p["part_no"], raw_part=p["vendor_part"], name=p["name"], spec=p["spec"],
                qty=qty, unit_price=price, amount=round(qty * price, 2))


def doc(vk, dtype, doc_no, d_date, po_no, items, priced=True):
    d = dict(vendor=vk, doc_type=dtype, doc_no=doc_no, doc_date=d_date.isoformat(), po_no=po_no, items=items)
    if priced:
        sub = round(sum(i["amount"] for i in items), 2)
        d.update(subtotal=sub, tax=round(sub * TAX, 2), total=round(sub * (1 + TAX), 2))
    return d


def render(dtype, d, vk, out, doc_id, rng):
    fmt = VENDORS[vk]["fmt"]
    if fmt == "pdf":
        name = f"{doc_id}.pdf"
        pdf = render_pdf_layout_a(dtype, d)
        pdf.subset_fonts()  # 只嵌入用到的字，否則整套正黑體（約 21 MB）會塞進每份 PDF
        pdf.save(out / "documents" / name, garbage=4, deflate=True)
        return [name]
    if fmt == "scan":
        img = to_scan(render_pdf_layout_b(dtype, d), rng, {"quote": "估價", "delivery": "已出貨",
                                                           "inspection": "品管"}[dtype])
        name = f"{doc_id}_scan.pdf"  # 純影像 PDF，沒有文字層，必須 OCR
        img.convert("RGB").save(out / "documents" / name, "PDF", resolution=110)
        return [name]
    name = f"{doc_id}.xlsx"
    render_xlsx(dtype, d, out / "documents" / name)
    return [name]


SUBJECTS = {
    "quote": ["【報價】{short} {doc_no} 報價單", "RE: 詢價回覆 - {short}", "{short}報價 {doc_no}"],
    "delivery": ["出貨通知 {doc_no}（PO {po_no}）", "{short} 送貨單 {doc_no}", "FW: 今日出貨 {po_no}"],
    "inspection": ["{doc_no} 出貨檢驗報告", "品檢報告 - {short} 批次 {doc_no}", "QC Report {doc_no}"],
}
LABEL = {"quote": "採購", "delivery": "採購", "inspection": "品保"}


def make_email(out, d, dtype, files, rng):
    v = VENDORS[d["vendor"]]
    msg = EmailMessage()
    msg["From"] = f"{v['contact']} <sales@{v['domain']}>"
    msg["To"] = "purchasing@dinglian-connector.example"
    msg["Subject"] = rng.choice(SUBJECTS[dtype]).format(short=v["short"], doc_no=d["doc_no"], po_no=d.get("po_no"))
    msg["Date"] = format_datetime(datetime.fromisoformat(d["doc_date"] + "T09:30:00+08:00")
                                  + timedelta(minutes=rng.randint(0, 480)))
    msg.set_content(f"張小姐您好：\n\n附件為{'報價單' if dtype == 'quote' else '出貨單' if dtype == 'delivery' else '檢驗報告'}，"
                    f"請查收。\n如有問題請與我聯繫。\n\n{v['name']}\n{v['contact']}\n（測試用合成資料）")
    for f in files:
        data = (out / "documents" / f).read_bytes()
        mt, st = (mimetypes.guess_type(f)[0] or "application/octet-stream").split("/")
        msg.add_attachment(data, maintype=mt, subtype=st, filename=f)
    name = f"{d['doc_id']}.eml"
    (out / "emails" / name).write_bytes(bytes(msg))
    return dict(file=name, label=LABEL[dtype], doc_type=dtype, doc_id=d["doc_id"])


def noise_emails(out, rng):
    """沒有附件或不相關的信，考驗分類器。"""
    cases = [
        ("客訴", "林經理 <qa@customer-auto.example>", "【緊急】3 月批次連接器插拔不良，請回覆 8D",
         "貴司 11/3 出貨批號 L261103 之 40P 連接器，客戶端組裝時發現插拔力偏低，請於 3 日內回覆 8D 報告。"),
        ("其他", "活動小組 <news@expo.example>", "2026 台北國際電子展 早鳥報名倒數",
         "早鳥優惠只到月底，立即報名！"),
        ("採購", "陳美玲 <sales@hengsheng-plastic.example>", "交期延後通知",
         "因模具保養，原訂 11/20 交貨之 40P 本體將延後至 11/27，造成不便敬請見諒。（內文含交期資訊、無附件）"),
        ("其他", "IT 部 <it@dinglian-connector.example>", "本週六系統維護公告",
         "ERP 將於週六 22:00–02:00 停機維護。"),
        ("品保", "王志豪 <sales@yushuo-surface.example>", "鍍金液更換通知",
         "本公司將於 12 月起更換鍍金液供應商，鍍層規格不變，附上變更說明，請貴司品保確認。"),
    ]
    labels = []
    for n, (label, frm, subj, body) in enumerate(cases, 1):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = frm, "purchasing@dinglian-connector.example", subj
        msg["Date"] = format_datetime(datetime(2026, 11, 3 + n, 10, rng.randint(0, 59)).astimezone())
        msg.set_content(body + "\n\n（測試用合成資料）")
        name = f"noise_{n:02d}.eml"
        (out / "emails" / name).write_bytes(bytes(msg))
        labels.append(dict(file=name, label=label, doc_type=None, doc_id=None))
    return labels


def write_csv(path, head, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM：Excel 開啟中文不亂碼
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "testdata/batch01")
