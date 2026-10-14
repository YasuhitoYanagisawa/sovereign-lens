"""Gemini with model-level failover.

When Vertex AI answers 429 RESOURCE_EXHAUSTED / 503 for the primary model (capacity
spikes happen), the google-genai client's own retries can stall a run for a minute or
more. This wrapper retries briefly, then moves to the next model in the chain
(e.g. gemini-3.8-flash -> 3.7 -> 3.6 -> 2.5) before anything has been streamed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_response import LlmResponse
from google.genai import types

log = logging.getLogger(__name__)
_RETRYABLE = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "overloaded", "DEADLINE_EXCEEDED")
FIRST_CHUNK_TIMEOUT = float(os.environ.get("FIRST_CHUNK_TIMEOUT", "25"))
_cooldown: dict[str, float] = {}  # model -> until (skip a model that just failed)


def _retryable(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}"
    return any(k in s for k in _RETRYABLE)


class FallbackGemini(BaseLlm):
    model: str = "gemini-3.8-flash"
    chain: list[str] = []
    last_model: str = ""

    def _order(self) -> list[str]:
        models = list(dict.fromkeys([self.model, *self.chain]))
        now = time.time()
        ok = [m for m in models if _cooldown.get(m, 0) < now]
        return ok + [m for m in models if m not in ok]

    async def generate_content_async(self, llm_request, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        models = self._order()
        last_err: Exception | None = None
        for i, m in enumerate(models):
            req = llm_request.model_copy(deep=True)
            req.model = m
            cfg = req.config
            if cfg is not None and cfg.thinking_config is not None and not m.startswith("gemini-3"):
                cfg.thinking_config = None  # thinking_level is a Gemini 3 setting
            g = Gemini(model=m, retry_options=types.HttpRetryOptions(attempts=2, initial_delay=1.0, max_delay=4.0))
            yielded = False
            agen = g.generate_content_async(req, stream=stream)
            try:
                try:
                    # a congested model can take a minute to start answering: fail over instead
                    first = await asyncio.wait_for(agen.__anext__(), timeout=(FIRST_CHUNK_TIMEOUT if stream else FIRST_CHUNK_TIMEOUT * 3) if i < len(models) - 1 else None)
                except StopAsyncIteration:
                    first = None
                except asyncio.TimeoutError:
                    await agen.aclose()
                    raise TimeoutError(f"DEADLINE_EXCEEDED: no response from {m}")
                if first is not None:
                    yielded = True
                    yield first
                    async for resp in agen:
                        fr = getattr(resp, "finish_reason", None)
                        if fr and str(fr) not in ("FinishReason.STOP", "STOP"):
                            log.warning("model %s finished with %s", m, fr)
                        yield resp
                self.last_model = m
                if i:
                    log.warning("served by fallback model %s", m)
                return
            except Exception as e:  # noqa: BLE001
                if yielded or not _retryable(e) or i == len(models) - 1:
                    raise
                _cooldown[m] = time.time() + 300
                last_err = e
                log.warning("model %s busy (%s); failing over", m, str(e)[:120])
        if last_err:
            raise last_err
