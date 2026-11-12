"""LLM 介面：Agent 迴圈只認得 ToolCall，不綁定特定廠商。

正式環境用 Gemini（設定 GEMINI_API_KEY）；測試用 FakeLLM 依劇本回應，驗證工具呼叫流程。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class LLMSession(Protocol):
    usage: Usage

    def next(self, tool_results: list[tuple[str, dict]] | None = None) -> list[ToolCall]:
        """送出上一輪的工具結果，取回模型這一輪要呼叫的工具。"""


class LLMClient(Protocol):
    name: str

    def start(self, system: str, tools: list[dict], user_text: str) -> LLMSession: ...


# ---------------------------------------------------------------- Gemini
class GeminiClient:
    def __init__(self, api_key: str, model: str):
        from google import genai
        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.name = f"gemini:{model}"

    def start(self, system: str, tools: list[dict], user_text: str) -> "GeminiSession":
        return GeminiSession(self, system, tools, user_text)


class GeminiSession:
    def __init__(self, owner: GeminiClient, system: str, tools: list[dict], user_text: str):
        from google.genai import types
        self.t = types
        self.owner = owner
        self.usage = Usage()
        self.contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_text)])]
        self.config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"],
                                          parameters_json_schema=t["parameters"]) for t in tools])],
            # ANY：每一輪都必須呼叫工具，最後一定以 submit_classification 結束，不會只回一段文字
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def next(self, tool_results: list[tuple[str, dict]] | None = None) -> list[ToolCall]:
        t = self.t
        if tool_results:
            self.contents.append(t.Content(role="user", parts=[
                t.Part.from_function_response(name=n, response=r) for n, r in tool_results]))
        resp = self.owner.client.models.generate_content(
            model=self.owner.model, contents=self.contents, config=self.config)
        self.usage.calls += 1
        if resp.usage_metadata:
            self.usage.input_tokens += resp.usage_metadata.prompt_token_count or 0
            self.usage.output_tokens += resp.usage_metadata.candidates_token_count or 0
        if resp.candidates and resp.candidates[0].content:
            self.contents.append(resp.candidates[0].content)  # 原樣保留（含 thought signature）
        return [ToolCall(c.name, dict(c.args or {})) for c in (resp.function_calls or [])]


# ---------------------------------------------------------------- 測試用
@dataclass
class FakeLLM:
    """依劇本回應的假模型：script(user_text, 已收到的工具結果) → 這一輪的工具呼叫。"""
    script: Callable[[str, list[tuple[str, dict]]], list[ToolCall]]
    name: str = "fake"
    sessions: list = field(default_factory=list)

    def start(self, system: str, tools: list[dict], user_text: str) -> "FakeSession":
        s = FakeSession(self.script, user_text)
        self.sessions.append(s)
        return s


@dataclass
class FakeSession:
    script: Callable
    user_text: str
    usage: Usage = field(default_factory=Usage)
    seen: list = field(default_factory=list)

    def next(self, tool_results=None):
        self.seen.extend(tool_results or [])
        self.usage.calls += 1
        return self.script(self.user_text, self.seen)


def default_client() -> LLMClient | None:
    """有金鑰才啟用；FP_LLM=0 可強制關閉（例如 CI）。"""
    if os.environ.get("FP_LLM", "1") == "0":
        return None
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    return GeminiClient(key, os.environ.get("FP_GEMINI_MODEL", "gemini-2.5-flash"))
