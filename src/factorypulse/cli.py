"""命令列入口：python -m factorypulse <指令>"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from . import config
from .db import get_engine, init_db, ops_run_log, seed_erp


def _engine(args):
    url = args.db_url or config.db_url()
    print(f"資料庫：{make_url(url).render_as_string(hide_password=True)}")
    return get_engine(url)


def cmd_init(args):
    eng = _engine(args)
    init_db(eng, reset=args.reset)
    print("資料表已建立" + ("（已清空重建）" if args.reset else ""))


def cmd_seed(args):
    eng = _engine(args)
    init_db(eng)
    print("匯入 ERP：", seed_erp(eng, Path(args.erp)))


def cmd_run(args):
    from .pipeline import run_inbox
    eng = _engine(args)
    s = run_inbox(eng, Path(args.inbox))
    print(f"\n執行 {s.run_id}：信件 {s.emails_seen} 封（重複 {s.emails_skipped}；規則分類 {s.mail_count('rule')}、"
          f"LLM {s.mail_count('llm')}、待人工 {s.mail_count(label='待人工分類')}），文件 {len(s.results)} 份")
    print(f"  入帳 {s.count('posted')}、入帳但有異常 {s.count('posted_with_alerts')}、"
          f"待人工確認 {s.count('needs_review')}、待 OCR {s.count('needs_ocr')}、"
          f"失敗 {s.count('failed')}、重複 {s.count('duplicate')}；耗時 {s.duration_ms / 1000:.1f} 秒")


def cmd_eval(args):
    from .evaluate import run_eval, to_markdown
    eng = _engine(args)
    res = run_eval(eng, Path(args.testdata), **({"llm": None} if args.no_llm else {}))
    md = to_markdown(res, Path(args.testdata).name)
    print(md)
    config.REPORTS_DIR.mkdir(exist_ok=True)
    out = config.REPORTS_DIR / f"eval_{Path(args.testdata).name}.md"
    out.write_text(md, encoding="utf-8")
    print(f"報告已寫入 {out}")
    # 讀不到但已主動送人工的欄位可以接受；其他錯誤（誤報、漏抓、讀錯還入帳、信分錯）都算失敗
    unexplained = [e for e in res.field_errors
                   if res.status_by_doc.get(e.split()[0].split(".")[0]) != "needs_review"]
    if args.strict and (res.fp or res.fn or unexplained or res.mail_errors):
        print("嚴格模式失敗：", res.fp, res.fn, unexplained, res.mail_errors)
        sys.exit(1)


def cmd_report(args):
    from datetime import date
    from .report import build_daily_report
    from .stats import compute, to_text
    eng = _engine(args)
    day = date.fromisoformat(args.date) if args.date else date.today()
    path = build_daily_report(eng, day)
    print(to_text(compute(eng, day)))
    print(f"\n日報已產生：{path}")


def cmd_stats(args):
    from datetime import date
    from .stats import compute, to_text
    eng = _engine(args)
    print(to_text(compute(eng, date.fromisoformat(args.date) if args.date else None)))


def cmd_baseline(args):
    from .db import ops_time_baseline
    from .stats import update_baseline
    eng = _engine(args)
    if args.task:
        update_baseline(eng, args.task, args.minutes, args.source)
    with eng.connect() as c:
        for r in c.execute(select(ops_time_baseline)):
            print(f"{r.task:12} {float(r.minutes):6.1f} 分鐘  {r.description}（來源：{r.source}）")


def cmd_mes(args):
    from .mes import run_mes
    eng = _engine(args)
    init_db(eng)
    s = run_mes(eng, Path(args.drop))
    e = s.to_dict()["etl"]
    print(f"ETL：檔案 {e['files']}（已匯入過 {e['skipped']}），讀入 {e['rows_in']:,} 列、寫入 {e['loaded']:,}、"
          f"重複 {e['duplicate']}、擋下 {e['rejected']} {e['rejects_by_rule']}、自動修正 {e['fixed']}")
    print(f"新的良率異常 {len(s.new_signals)} 個，通知 {len(s.notices)} 則；耗時 {s.duration_ms / 1000:.1f} 秒")


def cmd_eval_mes(args):
    from .evaluate_mes import run_mes_eval, to_markdown
    eng = _engine(args)
    r = run_mes_eval(eng, Path(args.mes), config.TESTDATA_DIR)
    md = to_markdown(r)
    print(md)
    config.REPORTS_DIR.mkdir(exist_ok=True)
    (config.REPORTS_DIR / "eval_mes_batch01.md").write_text(md, encoding="utf-8")
    if args.strict and (not r.etl_ok or r.fn or r.lot_wrong or not r.spike_ok or len(r.fp) > args.max_fp):
        print("嚴格模式失敗")
        sys.exit(1)


def cmd_status(args):
    eng = _engine(args)
    with eng.connect() as conn:
        last = conn.execute(select(ops_run_log).order_by(ops_run_log.c.started_at.desc()).limit(5)).all()
        for r in last:
            print(f"{r.run_id}  {r.status}  文件 {r.documents_seen}  入帳 {r.documents_posted}  "
                  f"待確認 {r.documents_review}  待OCR {r.documents_needs_ocr}  異常 {r.anomalies}")
        if eng.dialect.name == "mssql":
            print("\n採購單到貨狀況（vw_po_line_status）：")
            for r in conn.execute(text("SELECT po_no, line, part_no, po_qty, received_qty, line_status, "
                                       "days_late FROM vw_po_line_status ORDER BY po_no, line")):
                print("  ", *r)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 終端機預設 cp950，印不出 µ 等字元
    p = argparse.ArgumentParser(prog="factorypulse", description="連接器廠進料文件自動化")
    p.add_argument("--db-url", help="覆蓋 .env 的 FP_DB_URL")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init-db", help="建立資料表")
    s.add_argument("--reset", action="store_true", help="清空後重建")
    s.set_defaults(func=cmd_init)
    s = sub.add_parser("seed", help="匯入模擬 ERP 主檔與採購單")
    s.add_argument("--erp", default=str(config.TESTDATA_DIR / "erp"))
    s.set_defaults(func=cmd_seed)
    s = sub.add_parser("run", help="處理收件匣的信件")
    s.add_argument("--inbox", default=str(config.TESTDATA_DIR / "emails"))
    s.set_defaults(func=cmd_run)
    s = sub.add_parser("eval", help="從乾淨資料庫跑完整流程並對標準答案評分")
    s.add_argument("--testdata", default=str(config.TESTDATA_DIR))
    s.add_argument("--strict", action="store_true", help="有誤報、漏抓或欄位錯誤就回傳非零（給 CI 用）")
    s.add_argument("--no-llm", action="store_true", help="不呼叫 LLM（只用規則分類）")
    s.set_defaults(func=cmd_eval)
    s = sub.add_parser("status", help="最近的執行紀錄與採購單狀況")
    s.set_defaults(func=cmd_status)
    s = sub.add_parser("mes", help="匯入 MES／AOI 匯出檔，算良率管制圖並回查批號")
    s.add_argument("--drop", default=str(config.MES_TESTDATA_DIR / "drop"))
    s.set_defaults(func=cmd_mes)
    s = sub.add_parser("eval-mes", help="產線部分評測：ETL、良率異常偵測、批號回查")
    s.add_argument("--mes", default=str(config.MES_TESTDATA_DIR))
    s.add_argument("--strict", action="store_true")
    s.add_argument("--max-fp", type=int, default=2, help="容許的誤報天數（3σ 管制圖本來就會有少量誤報）")
    s.set_defaults(func=cmd_eval_mes)
    s = sub.add_parser("report", help="產生 Excel 日報")
    s.add_argument("--date", help="YYYY-MM-DD，預設今天")
    s.set_defaults(func=cmd_report)
    s = sub.add_parser("stats", help="自動化率、人工介入率、省下的時間")
    s.add_argument("--date", help="YYYY-MM-DD，不給就是全部期間")
    s.set_defaults(func=cmd_stats)
    s = sub.add_parser("baseline", help="查看或更新人工時間基準")
    s.add_argument("task", nargs="?", help="mail_triage / quote / delivery / inspection / review")
    s.add_argument("minutes", nargs="?", type=float)
    s.add_argument("--source", default="現場計時", help="資料來源，例如「2026-11 採購部計時 20 份平均」")
    s.set_defaults(func=cmd_baseline)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
