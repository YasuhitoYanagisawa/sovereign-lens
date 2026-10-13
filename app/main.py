"""FastAPI entrypoint for Cloud Run: REST API + SSE agent stream + static UI."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, portfolio
from .agent import MODEL as AGENT_DEFAULT_MODEL
from .agent import ALLOWED_TOOLS, plan_reviewer, prices_ready, rationale_researcher, root_agent, universe_with_prices
from .gmi_llm import GmiLlm, gmi_enabled
from .governance import GovernancePlugin, audit, screen_text
from .sources import nbim

logging.basicConfig(level=logging.INFO)
# httpx logs full request URLs at INFO; EDINET puts the API key in the query string.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("sovereign-lens")
APP_NAME = "sovereign_lens"
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Sovereign Lens", version="1.0.0")

# ---------------------------------------------------------------- telemetry
if os.environ.get("ENABLE_CLOUD_TRACE") == "1":
    try:
        from google.adk.telemetry.google_cloud import get_gcp_exporters
        from google.adk.telemetry.setup import maybe_set_otel_providers

        maybe_set_otel_providers([get_gcp_exporters(enable_cloud_tracing=True)])
        log.info("Cloud Trace exporter enabled")
    except Exception as e:  # noqa: BLE001
        log.warning("Cloud Trace setup failed: %s", e)

# ---------------------------------------------------------------- ADK runner (lazy)
_runner = None
_session_service = None
_model_ready: Optional[str] = None


async def _resolve_model() -> str:
    """Pick the first Gemini model that answers on this project/region."""
    global _model_ready
    if _model_ready:
        return _model_ready
    from google import genai
    from google.genai import types

    candidates = [m.strip() for m in os.environ.get("MODEL_CANDIDATES", "").split(",") if m.strip()]
    candidates = [AGENT_DEFAULT_MODEL, *candidates, "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-2.5-flash"]
    client = genai.Client()
    for m in dict.fromkeys(candidates):
        try:
            await client.aio.models.generate_content(model=m, contents="ping")
            _model_ready = m
            break
        except Exception as e:  # noqa: BLE001
            log.warning("model %s unavailable: %s", m, str(e)[:200])
    if not _model_ready:
        raise RuntimeError("No Gemini model available. Check Vertex AI API / region settings.")
    thinking = None
    if _model_ready.startswith("gemini-3"):
        # agentic tool loop: keep reasoning light for latency; the numbers come from deterministic tools
        level = os.environ.get("THINKING_LEVEL", "LOW").upper()
        thinking = types.ThinkingConfig(thinking_level=getattr(types.ThinkingLevel, level, types.ThinkingLevel.LOW))
    for a in (root_agent, rationale_researcher, plan_reviewer):
        if isinstance(a.model, GmiLlm):
            a.model.fallback_model = _model_ready
        else:
            a.model = _model_ready
        if thinking is not None and a.generate_content_config is not None:
            a.generate_content_config = a.generate_content_config.model_copy(update={"thinking_config": thinking})
    audit("model_selected", model=_model_ready)
    return _model_ready


def _get_runner():
    global _runner, _session_service
    if _runner is None:
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        _session_service = InMemorySessionService()
        from google.adk.apps import App

        adk_app = App(
            name=APP_NAME,
            root_agent=root_agent,
            plugins=[GovernancePlugin(ALLOWED_TOOLS, max_tool_calls=int(os.environ.get("MAX_TOOL_CALLS", "30")), per_tool_limits={"plan_reviewer": 2, "build_investment_strategy": 2, "rationale_researcher": 3})],
        )
        _runner = Runner(app=adk_app, session_service=_session_service)
    return _runner, _session_service


@app.on_event("startup")
async def _warm_model() -> None:
    """Resolve the model in the background so the first user request doesn't pay for the probe."""
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") or os.environ.get("GOOGLE_API_KEY"):
        async def _go():
            try:
                await _resolve_model()
            except Exception as e:  # noqa: BLE001
                log.warning("model warmup failed: %s", e)
        asyncio.create_task(_go())


# ---------------------------------------------------------------- rate limit (cost guard)
_hits: dict[str, deque] = defaultdict(deque)
RATE_N = int(os.environ.get("AGENT_RATE_LIMIT", "8"))
RATE_WINDOW = 600


def _rate_ok(ip: str) -> bool:
    q = _hits[ip]
    now = time.time()
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_N:
        return False
    q.append(now)
    return True


# ---------------------------------------------------------------- schemas
class Profile(BaseModel):
    investable_jpy: int = Field(..., ge=portfolio.MIN_INVEST, le=portfolio.MAX_INVEST)
    risk: Literal["conservative", "balanced", "aggressive"] = "balanced"
    horizon_years: int = Field(10, ge=1, le=50)
    use_nisa: bool = True
    exclude_codes: list[str] = Field(default_factory=list, max_length=30)
    include_activists: bool = False
    note: str = Field("", max_length=500)


class AgentRequest(BaseModel):
    message: Optional[str] = Field(None, max_length=2000)
    profile: Optional[Profile] = None


# ---------------------------------------------------------------- REST
@app.get("/api/health")
async def health():
    if isinstance(plan_reviewer.model, GmiLlm):
        m = plan_reviewer.model
        reviewer = f"GMI Cloud / {m.model}"
        if m.last_provider.startswith("gemini:"):
            reviewer += f"（GMI応答なしのため {m.fallback_model} で代替中）"
    else:
        reviewer = _model_ready or str(plan_reviewer.model)
    return {"ok": True, "model": _model_ready or root_agent.model, "reviewer": reviewer, "edinet": bool(os.environ.get("EDINET_API_KEY"))}


@app.get("/api/investors")
async def api_investors():
    return {"as_of": analytics.load_seed()["as_of"], "investors": analytics.investors()}


@app.get("/api/universe")
async def api_universe():
    stocks = await universe_with_prices(wait=False)
    return {"as_of": analytics.load_seed()["as_of"], "prices_loading": not prices_ready(), "score_guide": analytics.SCORE_GUIDE, "timing_guide": analytics.TIMING_GUIDE, "stocks": stocks}


@app.get("/api/quotes")
async def api_quotes(codes: str):
    """Quotes for the user's own holdings (portfolio tracker). Codes: comma separated, max 50."""
    import re as _re

    from .sources import prices as _prices

    wanted = [c for c in (x.strip().upper() for x in codes.split(",")) if _re.fullmatch(r"[0-9]{3}[0-9A-Z]", c)][:50]
    qs = await _prices.get_quotes(wanted)
    tracked = {s["code"]: s for s in await universe_with_prices(wait=False)}
    out = {}
    for c in wanted:
        q = qs.get(c) or {}
        t = tracked.get(c)
        out[c] = {
            "price": q.get("price"),
            "prev_close": q.get("prev_close"),
            "tracked": bool(t),
            "name_ja": t["name_ja"] if t else None,
            "timing": t["timing"] if t else None,
            "momentum": t["momentum"] if t else None,
            "fund_alert": (f"ファンドが一部売却（{', '.join(h['investor_short'] for h in t['holders'] if h['status'] in ('reduced','exited'))}）" if t and any(h["status"] in ("reduced", "exited") for h in t["holders"]) else None),
        }
    return {"quotes": out}


@app.get("/api/stocks/{code}")
async def api_stock(code: str):
    for s in await universe_with_prices():
        if s["code"] == code.upper():
            return s
    raise HTTPException(404, "not tracked")


@app.get("/api/nbim/live")
async def api_nbim(top_n: int = 30):
    try:
        return await nbim.fetch_japan_holdings(top_n=min(max(top_n, 1), 50))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"NBIM API unavailable: {type(e).__name__}") from e


@app.post("/api/strategy/preview")
async def api_preview(p: Profile):
    """Deterministic plan without the LLM (instant, used for the first render)."""
    try:
        return portfolio.build_strategy(await universe_with_prices(), **p.model_dump(exclude={"note"}))
    except portfolio.StrategyInputError as e:
        raise HTTPException(400, str(e)) from e


def _profile_prompt(p: Profile) -> str:
    risk = {"conservative": "安定重視", "balanced": "バランス", "aggressive": "積極運用"}[p.risk]
    ex = "、".join(p.exclude_codes) or "なし"
    return (
        f"私の条件で、政府系ファンドの日本株投資に追随する投資戦略を作ってください。\n"
        f"- 投資可能額: {p.investable_jpy:,}円\n- リスク許容度: {risk}（{p.risk}）\n- 投資期間: {p.horizon_years}年\n"
        f"- NISA利用: {'する' if p.use_nisa else 'しない'}\n- 除外したい銘柄コード: {ex}\n"
        f"- 物言う株主（アクティビスト）の保有銘柄も対象にする: {'はい（include_activists=true, category=all）' if p.include_activists else 'いいえ（政府系のみ）'}\n- 補足: {p.note or 'なし'}"
    )


def _sse(kind: str, data: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/agent/run")
async def api_agent(req: AgentRequest, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if not req.message and not req.profile:
        raise HTTPException(400, "message or profile is required")
    text = _profile_prompt(req.profile) if req.profile else req.message
    ok, reason = screen_text(req.message or (req.profile.note if req.profile else ""))
    if not ok:
        audit("input_blocked", ip=ip, reason=reason, severity="WARNING")
        raise HTTPException(400, reason)
    if not _rate_ok(ip):
        raise HTTPException(429, "利用回数の上限に達しました。10分後に再度お試しください。")

    async def stream():
        from google.genai import types

        t0 = time.time()
        try:
            model = await _resolve_model()
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"message": str(e)})
            return
        runner, sessions = _get_runner()
        uid = "web-" + uuid.uuid4().hex[:8]
        session = await sessions.create_session(app_name=APP_NAME, user_id=uid)
        yield _sse("start", {"session": session.id, "model": model})
        usage = {"prompt": 0, "output": 0}
        final_text = ""
        try:
            from google.adk.agents.run_config import RunConfig, StreamingMode

            async for ev in runner.run_async(
                user_id=uid,
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=text)]),
                run_config=RunConfig(streaming_mode=StreamingMode.SSE),
            ):
                um = getattr(ev, "usage_metadata", None)
                if um and not ev.partial:
                    usage["prompt"] += um.prompt_token_count or 0
                    usage["output"] += um.candidates_token_count or 0
                if not ev.content or not ev.content.parts:
                    continue
                for part in ev.content.parts:
                    if part.function_call and not ev.partial:
                        yield _sse("tool_call", {"t": round(time.time() - t0, 1), "agent": ev.author, "tool": part.function_call.name, "args": part.function_call.args})
                    elif part.function_response:
                        resp = part.function_response.response or {}
                        preview = json.dumps(resp, ensure_ascii=False, default=str)
                        err = resp.get("error") if isinstance(resp, dict) else None
                        yield _sse("tool_result", {"t": round(time.time() - t0, 1), "tool": part.function_response.name, "chars": len(preview), "error": err, "cached": bool(isinstance(resp, dict) and resp.get("_cached")), "preview": preview[:400]})
                        if part.function_response.name == "build_investment_strategy" and isinstance(resp, dict) and not err and resp.get("allocation"):
                            yield _sse("plan", resp)  # show the numbers as soon as they exist
                    elif part.text and not getattr(part, "thought", False) and ev.author == root_agent.name:
                        if ev.partial:
                            yield _sse("delta", {"text": part.text})
                        else:
                            final = ev.is_final_response()
                            if final:
                                final_text = part.text
                            yield _sse("text", {"t": round(time.time() - t0, 1), "agent": ev.author, "text": part.text, "final": final})
            s = await sessions.get_session(app_name=APP_NAME, user_id=uid, session_id=session.id)
            plan = (s.state or {}).get("plan") if s else None
            if plan:
                yield _sse("plan", plan)
            yield _sse("done", {"elapsed_s": round(time.time() - t0, 1), "tokens": usage, "model": model})
            audit("agent_run", ip=ip, elapsed_s=round(time.time() - t0, 1), tokens=usage, has_plan=bool(plan))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("agent run failed")
            yield _sse("error", {"message": f"{type(e).__name__}: {str(e)[:300]}"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- UI
app.mount("/static", StaticFiles(directory=STATIC), name="static")


_ASSET_V = str(int(max((STATIC / f).stat().st_mtime for f in ("app.js", "style.css", "index.html"))))


@app.get("/")
async def index():
    """Serve index with cache-busted asset URLs so a redeploy is picked up immediately."""
    from fastapi.responses import HTMLResponse

    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__V__", _ASSET_V)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
