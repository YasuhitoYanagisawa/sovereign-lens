import asyncio

import httpx
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from app import gmi_llm


def _req():
    return LlmRequest(
        model="x",
        contents=[types.Content(role="user", parts=[types.Part(text="審査して")])],
        config=types.GenerateContentConfig(system_instruction="あなたは審査役"),
    )


def test_messages_conversion():
    msgs = gmi_llm._to_messages(_req())
    assert msgs[0] == {"role": "system", "content": "あなたは審査役"}
    assert msgs[1]["role"] == "user"


def test_gmi_success(monkeypatch):
    monkeypatch.setenv("GMI_API_KEY", "test-key")
    seen = {}

    async def fake_post(self, url, headers=None, json=None):
        seen["url"], seen["auth"], seen["model"] = url, headers["Authorization"], json["model"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "承認可"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    llm = gmi_llm.GmiLlm(model="deepseek-ai/DeepSeek-V4-Pro", fallback_model="gemini-3.8-flash")

    async def run():
        return [r async for r in llm.generate_content_async(_req())]

    out = asyncio.run(run())
    assert "承認可" in out[0].content.parts[0].text
    assert seen["url"].endswith("/chat/completions") and seen["auth"] == "Bearer test-key"
    assert llm.last_provider.startswith("gmi:")


def test_gmi_falls_back_to_gemini(monkeypatch):
    monkeypatch.setenv("GMI_API_KEY", "test-key")

    async def boom(self, *a, **k):
        raise httpx.ConnectError("down")

    async def fake_gemini(self, llm_request, stream=False):
        from google.adk.models.llm_response import LlmResponse
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=f"fallback:{llm_request.model}")]))

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    monkeypatch.setattr(gmi_llm.Gemini, "generate_content_async", fake_gemini)
    llm = gmi_llm.GmiLlm(model="m", fallback_model="gemini-3.8-flash")

    async def run():
        return [r async for r in llm.generate_content_async(_req())]

    out = asyncio.run(run())
    assert out[0].content.parts[0].text == "fallback:gemini-3.8-flash"
    assert llm.last_provider == "gemini:gemini-3.8-flash"
