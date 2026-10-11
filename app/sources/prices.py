"""Latest close prices for TSE codes (Yahoo Finance chart API, Stooq fallback)."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import time

import httpx

log = logging.getLogger(__name__)
_TTL = 3600
_cache: dict[str, tuple[float, float | None]] = {}
_UA = {"User-Agent": "Mozilla/5.0 (sovereign-lens)"}


async def _yahoo(client: httpx.AsyncClient, code: str) -> float | None:
    r = await client.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T",
        params={"range": "5d", "interval": "1d"},
        headers=_UA,
    )
    if r.status_code != 200:
        return None
    res = (r.json().get("chart") or {}).get("result") or []
    if not res:
        return None
    meta = res[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price:
        return float(price)
    closes = [c for c in ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close", []) if c]
    return float(closes[-1]) if closes else None


async def _stooq(client: httpx.AsyncClient, code: str) -> float | None:
    r = await client.get(
        "https://stooq.com/q/l/", params={"s": f"{code.lower()}.jp", "f": "sd2t2ohlcv", "h": "", "e": "csv"}, headers=_UA
    )
    if r.status_code != 200:
        return None
    rows = list(csv.DictReader(io.StringIO(r.text)))
    try:
        return float(rows[0]["Close"])
    except (IndexError, KeyError, ValueError):
        return None


async def get_price(code: str, client: httpx.AsyncClient | None = None) -> float | None:
    hit = _cache.get(code)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    own = client is None
    client = client or httpx.AsyncClient(timeout=8.0, follow_redirects=True)
    price = None
    try:
        for fn in (_yahoo, _stooq):
            try:
                price = await fn(client, code)
            except Exception as e:  # noqa: BLE001
                log.info("price %s via %s failed: %s", code, fn.__name__, e)
            if price:
                break
    finally:
        if own:
            await client.aclose()
    _cache[code] = (time.time(), price)
    return price


async def get_prices(codes: list[str]) -> dict[str, float | None]:
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        sem = asyncio.Semaphore(6)

        async def one(c: str):
            async with sem:
                return c, await get_price(c, client)

        return dict(await asyncio.gather(*(one(c) for c in codes)))
