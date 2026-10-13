"""GMI Cloud (OpenAI-compatible) model adapter for ADK, with automatic Gemini fallback.

Used for the independent plan reviewer so that a model from a different vendor /
model family cross-checks the plan produced by Gemini (reduces correlated errors).
Text-only: the reviewer has no tools, so tool-call translation is not needed.

Env:
  GMI_API_KEY   (Secret Manager)  - enables GMI; without it the reviewer uses Gemini
  GMI_MODEL     default deepseek-ai/DeepSeek-V4-Pro
  GMI_BASE_URL  default https://api.gmi-serving.com/v1
"""
from __future__ import annotations

import logging
import os
import time
from typing import AsyncGenerator

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_response import LlmResponse
from google.genai import types

log = logging.getLogger(__name__)
DEFAULT_GMI_MODEL = "deepseek-ai/DeepSeek-V4-Pro"
DEFAULT_BASE = "https://api.gmi-serving.com/v1"


def gmi_enabled() -> bool:
    return bool(os.environ.get("GMI_API_KEY"))


def _to_messages(llm_request) -> list[dict]:
    msgs: list[dict] = []
    sys_inst = getattr(llm_request.config, "system_instruction", None) if llm_request.config else None
    if sys_inst:
        if isinstance(sys_inst, str):
            text = sys_inst
        elif isinstance(sys_inst, types.Content):
            text = "".join(p.text or "" for p in sys_inst.parts or [])
        else:
            text = str(sys_inst)
        if text.strip():
            msgs.append({"role": "system", "content": text})
    for c in llm_request.contents or []:
        text = "".join(p.text or "" for p in (c.parts or []) if not getattr(p, "thought", False))
        if not text.strip():
            continue
        msgs.append({"role": "assistant" if c.role == "model" else "user", "content": text})
    return msgs


class GmiLlm(BaseLlm):
    """ADK model that calls GMI Cloud; on any failure it transparently falls back to Gemini."""

    model: str = DEFAULT_GMI_MODEL
    fallback_model: str = "gemini-2.5-flash"
    last_provider: str = ""

    async def generate_content_async(self, llm_request, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        key = os.environ.get("GMI_API_KEY")
        if key:
            t0 = time.time()
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    r = await client.post(
                        f"{os.environ.get('GMI_BASE_URL', DEFAULT_BASE).rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model": self.model, "messages": _to_messages(llm_request), "temperature": 0.2, "max_tokens": 1500},
                    )
                r.raise_for_status()
                data = r.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                if not text.strip():
                    raise RuntimeError("empty completion")
                u = data.get("usage") or {}
                self.last_provider = f"gmi:{self.model}"
                log.info("gmi reviewer ok model=%s latency=%.1fs", self.model, time.time() - t0)
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(text=f"{text}\n\n（審査モデル: GMI Cloud / {self.model}）")]),
                    usage_metadata=types.GenerateContentResponseUsageMetadata(
                        prompt_token_count=u.get("prompt_tokens"), candidates_token_count=u.get("completion_tokens")
                    ),
                )
                return
            except Exception as e:  # noqa: BLE001
                # never log the key; httpx errors include URL only
                log.warning("gmi reviewer failed (%s); falling back to Gemini", type(e).__name__)
        self.last_provider = f"gemini:{self.fallback_model}"
        llm_request.model = self.fallback_model
        async for resp in Gemini(model=self.fallback_model).generate_content_async(llm_request, stream=stream):
            yield resp
