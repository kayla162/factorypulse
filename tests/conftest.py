from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from factorypulse.db import init_db, seed_erp
from factorypulse.notify import OutboxNotifier
from factorypulse.pipeline import Pipeline
from factorypulse.mail import read_inbox

TESTDATA = Path(__file__).resolve().parents[1] / "testdata" / "batch01"
HAS_OCR = importlib.util.find_spec("rapidocr") is not None
needs_ocr = pytest.mark.skipif(not HAS_OCR, reason="未安裝 rapidocr（pip install -e .[ocr]）")


@pytest.fixture(autouse=True)
def _isolate_from_local_env(monkeypatch):
    """測試不受本機 .env 影響：
    - 一律不打真的 LLM API，就算 .env 有金鑰也不會花錢
    - 儀表板公開模式回到預設關閉（本機示範可能設了 FP_DASHBOARD_PUBLIC=1）
    """
    monkeypatch.setenv("FP_LLM", "0")
    monkeypatch.delenv("FP_DASHBOARD_PUBLIC", raising=False)


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{(tmp_path / 'fp.db').as_posix()}")
    init_db(eng)
    seed_erp(eng, TESTDATA / "erp")
    yield eng
    eng.dispose()


def answer(doc_id: str) -> dict:
    return json.loads((TESTDATA / "answers" / f"{doc_id}.json").read_text(encoding="utf-8"))


def run_pipeline(engine, tmp_path, inbox: Path | None = None, llm=None):
    notifier = OutboxNotifier(tmp_path / "outbox", echo=False)
    p = Pipeline(engine, notifier, llm=llm, attachments_dir=tmp_path / "att")
    return p.run(read_inbox(inbox or TESTDATA / "emails")), notifier


def inbox_with(tmp_path, *names: str) -> Path:
    box = tmp_path / "inbox"
    box.mkdir(exist_ok=True)
    for n in names:
        shutil.copy(TESTDATA / "emails" / n, box)
    return box
