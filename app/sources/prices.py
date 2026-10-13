"""Quotes for TSE codes: latest price + 52-week range + 200-day MA.

Yahoo Finance chart API (1y daily) with Stooq daily CSV as fallback. Cached 1h.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import time

import httpx

log = logging.getLogger(__name__)
_TTL = 3600
_cache: dict[str, tuple[float, dict | None]] = {}
_UA = {"User-Agent": "Mozilla/5.0 (sovereign-lens)"}


def _summarize(closes: list[float], last: float | None = None) -> dict | None:
    closes = [c for c in closes if c]
    if not closes:
        return None
    price = last or closes[-1]
    window = closes[-250:]
    ma_src = closes[-200:]
    return {
        "price": float(price),
        "high52": float(max(window)),
        "low52": float(min(window)),
        "ma200": round(sum(ma_src) / len(ma_src), 2) if len(ma_src) >= 120 else None,
        "prev_close": float(closes[-2]) if len(closes) >= 2 else None,
    }


async def _yahoo(client: httpx.AsyncClient, code: str) -> dict | None:
    r = await client.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T",
        params={"range": "1y", "interval": "1d"},
        headers=_UA,
    )
    if r.status_code != 200:
        return None
    res = (r.json().get("chart") or {}).get("result") or []
    if not res:
        return None
    meta = res[0].get("meta") or {}
    closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    return _summarize(closes, meta.get("regularMarketPrice"))


async def _stooq(client: httpx.AsyncClient, code: str) -> dict | None:
    r = await client.get("https://stooq.com/q/d/l/", params={"s": f"{code.lower()}.jp", "i": "d"}, headers=_UA)
    if r.status_code != 200 or "Close" not in r.text[:200]:
        return None
    rows = list(csv.DictReader(io.StringIO(r.text)))[-260:]
    closes = []
    for row in rows:
        try:
            closes.append(float(row["Close"]))
        except (KeyError, ValueError):
            pass
    return _summarize(closes)


async def get_quote(code: str, client: httpx.AsyncClient | None = None) -> dict | None:
    hit = _cache.get(code)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    own = client is None
    client = client or httpx.AsyncClient(timeout=8.0, follow_redirects=True)
    q = None
    try:
        for fn in (_yahoo, _stooq):
            try:
                q = await fn(client, code)
            except Exception as e:  # noqa: BLE001
                log.info("quote %s via %s failed: %s", code, fn.__name__, type(e).__name__)
            if q:
                break
    finally:
        if own:
            await client.aclose()
    _cache[code] = (time.time(), q)
    return q


async def get_quotes(codes: list[str]) -> dict[str, dict | None]:
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        sem = asyncio.Semaphore(8)

        async def one(c: str):
            async with sem:
                return c, await get_quote(c, client)

        return dict(await asyncio.gather(*(one(c) for c in dict.fromkeys(codes))))


async def get_prices(codes: list[str]) -> dict[str, float | None]:
    qs = await get_quotes(codes)
    return {c: (q or {}).get("price") for c, q in qs.items()}
