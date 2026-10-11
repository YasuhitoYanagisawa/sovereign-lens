"""End-to-end ADK plumbing test with a scripted fake LLM (no network / credentials)."""
import asyncio
from typing import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app import agent as ag
from app.governance import GovernancePlugin


class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    step: int = 0

    async def generate_content_async(self, llm_request, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        self.step += 1
        if self.step == 1:
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="rank_swf_conviction_stocks", args={"top_n": 3}))]))
        elif self.step == 2:
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="build_investment_strategy", args={"investable_jpy": 2_000_000, "risk": "balanced", "horizon_years": 10, "use_nisa": True, "exclude_codes": []}))]))
        elif self.step == 3:
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="delete_all_files", args={}))]))
        else:
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="### 結論\nテスト完了")]))


def test_runner_with_governance(monkeypatch):
    async def fake_prices(codes):
        return {c: 3000.0 for c in codes}

    monkeypatch.setattr(ag.prices, "get_prices", fake_prices)
    ag._universe_cache = None
    original_model = ag.root_agent.model
    ag.root_agent.model = ScriptedLlm()
    try:
        async def run():
            ss = InMemorySessionService()
            runner = Runner(app_name="t", agent=ag.root_agent, session_service=ss, plugins=[GovernancePlugin(ag.ALLOWED_TOOLS)])
            s = await ss.create_session(app_name="t", user_id="u")
            calls, responses, texts = [], [], []
            async for ev in runner.run_async(user_id="u", session_id=s.id, new_message=types.Content(role="user", parts=[types.Part(text="1000万円で戦略を")])):
                for p in (ev.content.parts if ev.content else []) or []:
                    if p.function_call:
                        calls.append(p.function_call.name)
                    if p.function_response:
                        responses.append((p.function_response.name, p.function_response.response))
                    if p.text:
                        texts.append(p.text)
            st = (await ss.get_session(app_name="t", user_id="u", session_id=s.id)).state
            return calls, responses, texts, st

        calls, responses, texts, state = asyncio.run(run())
    finally:
        ag.root_agent.model = original_model
    assert calls[:2] == ["rank_swf_conviction_stocks", "build_investment_strategy"]
    assert "plan" in state and state["plan"]["all_checks_passed"]
    blocked = [r for n, r in responses if n == "delete_all_files"]
    assert blocked and "error" in blocked[0]
    assert any("結論" in t for t in texts)
