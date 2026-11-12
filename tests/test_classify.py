"""LLM 信件分類：用依劇本回應的 FakeLLM 驗證工具呼叫流程與防呆。

這裡測的是「流程與驗證邏輯」，不是模型分得準不準；準確度要用真的模型跑 eval。
"""
from factorypulse.classify import MAX_TURNS, UNCLASSIFIED
from factorypulse.llm import FakeLLM, ToolCall

from .conftest import inbox_with, run_pipeline


def submit(**kw):
    base = dict(label="其他", confidence=0.9, reason="測試", summary="測試摘要")
    return [ToolCall("submit_classification", base | kw)]


def delay_agent(user_text, seen):
    """模擬模型：先查寄件者，再查未結採購單，最後回報交期變更。"""
    if not seen:
        return [ToolCall("lookup_sender", {"email": "sales@hengsheng-plastic.example"})]
    if len(seen) == 1:
        return [ToolCall("find_open_purchase_orders", {"vendor_code": seen[0][1]["vendor_code"]})]
    return submit(label="採購", confidence=0.92, reason="供應商通知 40P 本體交期延後",
                  summary="恆昇 40P 本體因模具保養延後一週", po_no="PO26100004", part_no="FP-HS-3002",
                  new_due_date="2026-11-27")


def test_tool_calling_flow_and_delay_notice(engine, tmp_path):
    llm = FakeLLM(delay_agent)
    s, notifier = run_pipeline(engine, tmp_path, inbox_with(tmp_path, "noise_03.eml"), llm=llm)
    c = s.mails[0].classification
    assert (c.label, c.method) == ("採購", "llm")
    assert c.tool_calls == ["lookup_sender", "find_open_purchase_orders", "submit_classification"]
    # 工具結果真的有回傳給模型：寄件者被認出是 V002，未結採購單包含 PO26100004
    seen = llm.sessions[0].seen
    assert seen[0] == ("lookup_sender", {"type": "vendor", "vendor_code": "V002",
                                         "vendor_name": "恆昇塑膠工業有限公司"})
    assert any(line["po_no"] == "PO26100004" for line in seen[1][1]["open_lines"])
    # 通知裡的原交期是回 ERP 查證過的，不是模型說的
    [n] = notifier.sent
    assert n.title.startswith("【交期變更】恆昇塑膠")
    assert "原交期 2026-10-29 → 新交期 2026-11-27" in n.lines[1]


def test_hallucinated_po_is_not_trusted(engine, tmp_path):
    llm = FakeLLM(lambda u, seen: submit(label="採購", po_no="PO99999999", new_due_date="2026-11-27"))
    _, notifier = run_pipeline(engine, tmp_path, inbox_with(tmp_path, "noise_03.eml"), llm=llm)
    assert "查無對應" in notifier.sent[0].lines[1]


def test_complaint_is_urgent_to_qa(engine, tmp_path):
    llm = FakeLLM(lambda u, seen: submit(label="客訴", summary="客戶要求 8D"))
    _, notifier = run_pipeline(engine, tmp_path, inbox_with(tmp_path, "noise_01.eml"), llm=llm)
    [n] = notifier.sent
    assert (n.owner, n.urgent) == ("品保", True) and n.title.startswith("【客訴】")


def test_ad_is_not_notified(engine, tmp_path):
    llm = FakeLLM(lambda u, seen: submit(label="其他"))
    s, notifier = run_pipeline(engine, tmp_path, inbox_with(tmp_path, "noise_02.eml"), llm=llm)
    assert s.mails[0].classification.label == "其他" and notifier.sent == []


def test_low_confidence_goes_to_human(engine, tmp_path):
    llm = FakeLLM(lambda u, seen: submit(label="品保", confidence=0.4, reason="可能是變更通知"))
    s, notifier = run_pipeline(engine, tmp_path, inbox_with(tmp_path, "noise_05.eml"), llm=llm)
    c = s.mails[0].classification
    assert c.label == UNCLASSIFIED and "建議「品保」" in c.reason
    assert notifier.sent[0].title.startswith("【待分類】")


def test_invalid_label_goes_to_human(engine, tmp_path):
    llm = FakeLLM(lambda u, seen: submit(label="財務"))
    s, _ = run_pipeline(engine, tmp_path, inbox_with(tmp_path, "noise_04.eml"), llm=llm)
    assert s.mails[0].classification.label == UNCLASSIFIED


def test_api_error_does_not_stop_batch(engine, tmp_path):
    def boom(u, seen):
        raise TimeoutError("模擬 API 逾時")
    s, _ = run_pipeline(engine, tmp_path, inbox_with(tmp_path, "noise_01.eml", "noise_02.eml"), llm=FakeLLM(boom))
    assert [m.classification.label for m in s.mails] == [UNCLASSIFIED, UNCLASSIFIED]
    assert "TimeoutError" in s.mails[0].classification.reason


def test_endless_tool_calls_are_cut_off(engine, tmp_path):
    llm = FakeLLM(lambda u, seen: [ToolCall("lookup_sender", {"email": "x@y.example"})])
    s, _ = run_pipeline(engine, tmp_path, inbox_with(tmp_path, "noise_02.eml"), llm=llm)
    c = s.mails[0].classification
    assert c.label == UNCLASSIFIED and c.llm_calls == MAX_TURNS


def test_documents_never_reach_llm(engine, tmp_path):
    llm = FakeLLM(lambda u, seen: submit(label="其他"))
    run_pipeline(engine, tmp_path, inbox_with(tmp_path, "A_delivery_01.eml", "C_quote_01.eml"), llm=llm)
    assert llm.sessions == []     # 規則判得出來的信，不花 LLM 費用
