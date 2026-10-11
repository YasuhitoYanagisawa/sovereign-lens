"""Live client for Norges Bank Investment Management's public holdings API.

NBIM publishes every equity it owns (≈9,000 companies) twice a year as JSON:
  https://www.nbim.no/api/investments/v2/init.json         -> list of periods
  https://www.nbim.no/api/investments/v2/{YYYY-MM-DD}.json -> holdings snapshot
Record shape (observed): {"cc": "JP", "n": "Nintendo Co Ltd", "at": 0,
  "a": {"n": <NOK>, "e": <USD>}, "o": <ownership %>, "eq": {"vid": ticker, "s": industry}}

Everything here is defensive: the schema is undocumented, so missing keys are
tolerated and the caller falls back to the curated seed data.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)
BASE = "https://www.nbim.no/api/investments/v2"
HISTORY_DATES = ["1998-12-31", "2005-12-31", "2010-12-31", "2015-12-31", "2020-12-31"]
_TTL = 6 * 3600
_cache: dict[str, tuple[float, Any]] = {}

_SUFFIX = re.compile(
    r"\b(co|ltd|corp|corporation|inc|holdings?|hd|group|kk|plc|the|company|&)\b|[.,()'\-]"
)


def normalize_name(name: str) -> str:
    n = _SUFFIX.sub(" ", (name or "").lower())
    return re.sub(r"\s+", " ", n).strip()


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    r = await client.get(url, timeout=60.0, headers={"User-Agent": "sovereign-lens/1.0"})
    r.raise_for_status()
    data = r.json()
    _cache[url] = (time.time(), data)
    return data


def _records(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            for key in ("holdings", "equities", "companies", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
            return []
        return data if isinstance(data, list) else []
    return payload if isinstance(payload, list) else []


def _japan_equities(payload: Any) -> list[dict]:
    out = []
    for r in _records(payload):
        if not isinstance(r, dict) or r.get("cc") != "JP":
            continue
        if r.get("at", 0) not in (0, "0", None):
            continue
        a = r.get("a") or {}
        eq = r.get("eq") or {}
        vid = str(eq.get("vid") or "")
        m = re.search(r"\b(\d{3}[0-9A-Z])\b", vid)
        out.append(
            {
                "name_en": r.get("n"),
                "code": m.group(1) if m else None,
                "market_value_usd": a.get("e"),
                "market_value_nok": a.get("n"),
                "ownership_pct": r.get("o"),
                "voting_pct": eq.get("v"),
                "industry": eq.get("s"),
            }
        )
    return out


async def latest_period(client: httpx.AsyncClient) -> str:
    init = await _get_json(client, f"{BASE}/init.json")
    details = ((init or {}).get("data") or {}).get("details") or []
    dates = sorted(d.get("date", "")[:10] for d in details if d.get("date"))
    return dates[-1] if dates else "2025-12-31"


async def fetch_japan_holdings(top_n: int = 50, with_history: bool = True) -> dict:
    """Return NBIM's largest Japanese equity holdings with an estimated first-held year."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        period = await latest_period(client)
        current = _japan_equities(await _get_json(client, f"{BASE}/{period}.json"))
        current.sort(key=lambda x: x.get("market_value_usd") or 0, reverse=True)
        first_seen: dict[str, str] = {}
        if with_history:
            for d in HISTORY_DATES:
                try:
                    hist = _japan_equities(await _get_json(client, f"{BASE}/{d}.json"))
                except Exception as e:  # noqa: BLE001
                    log.warning("nbim history %s failed: %s", d, e)
                    continue
                for h in hist:
                    first_seen.setdefault(normalize_name(h["name_en"]), d)
    total_usd = sum(x.get("market_value_usd") or 0 for x in current)
    top = current[:top_n]
    for h in top:
        h["first_seen"] = first_seen.get(normalize_name(h["name_en"]))
    return {
        "period": period,
        "japan_company_count": len(current),
        "japan_total_usd": total_usd,
        "top": top,
    }
