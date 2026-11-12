"""把 testdata 的 .eml 透過 SMTP 寄到本機 Mailpit，模擬供應商來信。

用法：python scripts/send_test_mails.py [--host localhost] [--port 1025] [信件資料夾]
Mailpit 只收信不外寄，不會真的寄到任何人。
"""
from __future__ import annotations

import argparse
import smtplib
import sys
from email import message_from_bytes, policy
from email.utils import getaddresses
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("folder", nargs="?", default=str(ROOT / "testdata" / "batch01" / "emails"))
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=1025)
    a = p.parse_args()
    if a.host not in ("localhost", "127.0.0.1", "mailpit"):
        raise SystemExit("只允許寄到本機測試信箱")
    files = sorted(Path(a.folder).glob("*.eml"))
    with smtplib.SMTP(a.host, a.port) as smtp:
        for f in files:
            raw = f.read_bytes()
            msg = message_from_bytes(raw, policy=policy.default)
            sender = getaddresses([msg["From"]])[0][1]
            rcpts = [addr for _, addr in getaddresses(msg.get_all("To", []))]
            smtp.sendmail(sender, rcpts, raw)
    print(f"已寄出 {len(files)} 封到 {a.host}:{a.port}")


if __name__ == "__main__":
    main()
