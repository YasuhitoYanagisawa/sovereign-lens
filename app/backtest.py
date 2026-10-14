"""Backtest: 「過去のある日にこの戦略どおり買っていたら、今いくらか」.

Rebuilds the universe as of a past start date S using only holdings whose first report
was on or before S, scores it with prices up to S, runs the same deterministic strategy
engine, then values the plan at today's prices (Yahoo adjusted close = dividends and
splits included). Benchmarks: 100% TOPIX ETF (1306), and the same core/cash weights
with the stock sleeve replaced by TOPIX (isolates the stock-picking effect).

Known biases (shown to the user): ownership ratios and estimated costs are the latest
reported values, and the tracked-fund list was assembled recently (survivorship).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

import httpx

from . import analytics, portfolio
from .sources.prices import _UA, _summarize

log = logging.getLogger(__name__)
JST = dt.timezone(dt.timedelta(hours=9))
TOPIX_ETF = "1306"
_hist_cache: dict[str, tuple[float, list[tuple[dt.date, float, float]] | None]] = {}
_result_cache: dict[tuple, tuple[float, dict]] = {}


async def _history(client: httpx.AsyncClient, code: str) -> list[tuple[dt.date, float, float]] | None:
    """[(date, close, adjclose)] for ~5 years."""
    hit = _hist_cache.get(code)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    out = None
    try:
        r = await client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T",
            params={"range": "5y", "interval": "1d", "events": "div,splits"},
            headers=_UA,
        )
        if r.status_code == 200:
            res = ((r.json().get("chart") or {}).get("result") or [None])[0] or {}
            ts = res.get("timestamp") or []
            ind = res.get("indicators") or {}
            close = ((ind.get("quote") or [{}])[0]).get("close") or []
            adj = ((ind.get("adjclose") or [{}])[0]).get("adjclose") or close
            out = [
                (dt.datetime.fromtimestamp(t, JST).date(), float(c), float(a or c))
                for t, c, a in zip(ts, close, adj)
                if c
            ]
    except Exception as e:  # noqa: BLE001
        log.info("history %s failed: %s", code, type(e).__name__)
    if out:
        out = _despike(out)
    _hist_cache[code] = (time.time(), out)
    return out


def _despike(rows: list[tuple[dt.date, float, float]]) -> list[tuple[dt.date, float, float]]:
    """Drop one-day data glitches (e.g. a single bar at 1/10 of its neighbours)."""
    keep = []
    for i, r in enumerate(rows):
        if 0 < i < len(rows) - 1:
            prev, nxt = rows[i - 1][2], rows[i + 1][2]
            up, down = r[2] / prev, r[2] / nxt
            if (up < 0.6 and down < 0.6) or (up > 1.6 and down > 1.6):
                continue
        keep.append(r)
    return keep


async def histories(codes: list[str]) -> dict[str, list]:
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        sem = asyncio.Semaphore(8)

        async def one(c):
            async with sem:
                return c, await _history(client, c)

        return dict(await asyncio.gather(*(one(c) for c in dict.fromkeys(codes))))


def _quote_at(h: list, day: dt.date) -> dict | None:
    closes = [c for d, c, _ in h if d <= day]
    if not closes or (day - max(d for d, _, _ in h if d <= day)).days > 10:
        return None
    return _summarize(closes)


def _adj_at(h: list, day: dt.date) -> float | None:
    v = None
    for d, _, a in h:
        if d > day:
            break
        v = a
    return v


def rows_as_of(start: dt.date) -> list[dict]:
    """Holdings known on `start`, with post-start information stripped."""
    out = []
    for r in analytics.load_seed()["holdings"]:
        fs = r.get("first_seen")
        if not fs or fs[:10] > start.isoformat():
            continue
        r = dict(r)
        lr = (r.get("latest_report") or "")[:10]
        if lr and lr > start.isoformat():
            if r.get("status") == "exited" or r.get("status") in ("reduced", "increased", "new"):
                r["status"] = "holding"  # what happened after `start` is unknown on `start`
            r["peak_pct"] = r["prior_pct"] = None
            r["history"] = ""
            r["latest_report"] = start.isoformat()
        out.append(r)
    return out


def universe_as_of(start: dt.date, quotes: dict) -> list[dict]:
    by_code: dict[str, list[dict]] = {}
    order = {"sovereign": 0, "activist": 1, "financing": 2}
    for r in sorted(rows_as_of(start), key=lambda r: order.get(analytics.category_of(r["investor_id"]), 9)):
        by_code.setdefault(r["code"], []).append(r)
    u = [analytics.score_stock(rs, quotes.get(c), start) for c, rs in by_code.items()]
    u.sort(key=lambda s: s["score"], reverse=True)
    return u


async def run(start: dt.date, amount: int = 3_000_000, risk: str = "balanced", include_activists: bool = False) -> dict:
    key = (start, amount, risk, include_activists)
    hit = _result_cache.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    codes = sorted({r["code"] for r in rows_as_of(start)} | {TOPIX_ETF})
    hist = await histories(codes)
    if not hist.get(TOPIX_ETF):
        raise RuntimeError("株価履歴を取得できませんでした（時間をおいて再試行してください）")
    quotes = {c: _quote_at(h, start) for c, h in hist.items() if h}
    uni = universe_as_of(start, quotes)
    plan = portfolio.build_strategy(uni, amount, risk=risk, horizon_years=10, include_activists=include_activists)

    t_hist = hist[TOPIX_ETF]
    t0 = _adj_at(t_hist, start)
    days = [d for d, _, _ in t_hist if d > start]
    end = days[-1] if days else start
    a = plan["allocation"]
    legs = []  # (label, amount, hist)
    for p in plan["positions"]:
        h = hist.get(p["code"])
        if h and _adj_at(h, start):
            legs.append((p, p["amount_jpy"], h))

    def value_on(d: dt.date) -> tuple[float, float, float]:
        tr = _adj_at(t_hist, d) / t0
        core = a["core_index_jpy"] * tr
        sat = sum(amt * (_adj_at(h, d) / _adj_at(h, start)) for _, amt, h in legs)
        sat_in = sum(amt for _, amt, _ in legs)
        cash = amount - a["core_index_jpy"] - sat_in
        plan_v = core + sat + cash
        same_mix_topix = core + sat_in * tr + cash
        return plan_v, amount * tr, same_mix_topix

    series = []
    for i, d in enumerate(days):
        if i % 5 == 0 or d == end:
            pv, tv, mv = value_on(d)
            series.append({"date": d.isoformat(), "plan": round(pv), "topix": round(tv), "same_mix_topix": round(mv)})
    pv, tv, mv = value_on(end)

    def mdd(key):
        peak, worst = 0.0, 0.0
        for s in series:
            peak = max(peak, s[key])
            worst = min(worst, s[key] / peak - 1)
        return round(worst * 100, 1)

    positions = []
    for p, amt, h in legs:
        r = _adj_at(h, end) / _adj_at(h, start) - 1
        positions.append(
            {
                "code": p["code"], "name": p["name"], "sector": p["sector"], "score": p["score"],
                "timing_at_start": p.get("timing"), "funds": p["followed_investors"],
                "amount_jpy": amt, "return_pct": round(r * 100, 1), "pnl_jpy": round(amt * r),
            }
        )
    positions.sort(key=lambda x: x["return_pct"], reverse=True)
    sat_in = sum(x["amount_jpy"] for x in positions)
    sat_pnl = sum(x["pnl_jpy"] for x in positions)
    t_ret = tv / amount - 1
    out = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "years": round((end - start).days / 365.25, 2),
        "amount_jpy": amount,
        "risk": risk,
        "include_activists": include_activists,
        "allocation": a,
        "result": {
            "plan_value_jpy": round(pv),
            "plan_return_pct": round((pv / amount - 1) * 100, 1),
            "topix_return_pct": round(t_ret * 100, 1),
            "same_mix_topix_return_pct": round((mv / amount - 1) * 100, 1),
            "stock_sleeve_return_pct": round(sat_pnl / sat_in * 100, 1) if sat_in else None,
            "topix_same_period_pct": round(t_ret * 100, 1),
            "plan_max_drawdown_pct": mdd("plan"),
            "topix_max_drawdown_pct": mdd("topix"),
        },
        "positions": positions,
        "series": series,
        "universe_size_at_start": len(uni),
        "caveats": [
            "開始日時点で報告済み（初回報告日が開始日以前）の保有だけを使い、それ以降の売買情報は使っていません。",
            "ただし保有比率・推定取得単価は最新の報告値を使っており、追跡ファンドの選定も現時点で行っているため、生存者バイアスが残ります。",
            "配当・株式分割は調整後終値で反映。手数料・税金・分割購入は考慮せず、開始日に一括購入して保有し続けた場合の試算です。",
            "過去の成績は将来の成果を保証しません。",
        ],
    }
    _result_cache[key] = (time.time(), out)
    return out
