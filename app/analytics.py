"""Aggregation and the 'Sovereign Conviction Score' for Japanese stocks held by SWFs."""
from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "seed_holdings.json"

STATUS_LABEL = {
    "new": "新規取得",
    "increased": "買い増し",
    "holding": "保有継続",
    "reduced": "一部売却",
    "exited": "売却・対象外",
}
MOMENTUM_PTS = {"new": 15, "increased": 15, "holding": 10, "reduced": 3, "exited": 0}
_STATUS_RANK = {"new": 4, "increased": 4, "holding": 3, "reduced": 1, "exited": 0}


@lru_cache(maxsize=1)
def load_seed() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def investors() -> list[dict]:
    seed = load_seed()
    counts: dict[str, int] = {}
    for h in seed["holdings"]:
        if h.get("status") != "exited":
            counts[h["investor_id"]] = counts.get(h["investor_id"], 0) + 1
    return [{**i, "japan_holdings_tracked": counts.get(i["id"], 0)} for i in seed["investors"]]


def investor_name(inv_id: str, short: bool = False) -> str:
    for i in load_seed()["investors"]:
        if i["id"] == inv_id:
            return i.get("short", i["name_ja"]) if short else i["name_ja"]
    return inv_id


def holdings(investor_id: str | None = None, code: str | None = None) -> list[dict]:
    rows = load_seed()["holdings"]
    if investor_id:
        rows = [r for r in rows if r["investor_id"] == investor_id]
    if code:
        rows = [r for r in rows if r["code"].upper() == code.upper()]
    return [{**r, "investor_name": investor_name(r["investor_id"]), "status_label": STATUS_LABEL.get(r.get("status"), r.get("status"))} for r in rows]


def _years_since(date_str: str | None, today: dt.date) -> float | None:
    if not date_str:
        return None
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return None
    return round((today - d).days / 365.25, 1)


def score_stock(rows: list[dict], price: float | None, today: dt.date) -> dict:
    active = [r for r in rows if r.get("status") != "exited"]
    base = rows[0]
    groups = sorted({r["investor_id"] for r in active})
    pcts = [r["ownership_pct"] for r in active if r.get("ownership_pct") is not None]
    max_pct = max(pcts) if pcts else None
    years = [y for y in (_years_since(r.get("first_seen"), today) for r in active) if y is not None]
    holding_years = max(years) if years else None
    best = max(active, key=lambda r: _STATUS_RANK.get(r.get("status"), 0)) if active else None
    momentum_status = best.get("status") if best else "exited"
    costs = [r for r in active if r.get("est_cost_jpy")]
    ref = max(costs, key=lambda r: (r.get("confidence") == "high", r.get("ownership_pct") or 0)) if costs else None
    ref_cost = ref["est_cost_jpy"] if ref else None
    ratio = round(price / ref_cost, 3) if (price and ref_cost) else None

    conviction = min(max_pct or 0.5, 10) / 10 * 30
    consensus = {0: 0, 1: 0, 2: 12}.get(len(groups), 20)
    duration = min(holding_years or 0, 10) / 10 * 20
    momentum = MOMENTUM_PTS.get(momentum_status, 0)
    if ratio is None:
        value = 5
    elif ratio <= 1.0:
        value = 15
    elif ratio <= 1.2:
        value = 10
    elif ratio <= 1.5:
        value = 5
    else:
        value = 0
    total = conviction + consensus + duration + momentum + value
    low_conf = all(r.get("confidence") == "low" for r in active) if active else True
    if low_conf:
        total *= 0.7

    signals = []
    if max_pct and max_pct >= 5:
        signals.append(f"政府系ファンドが発行済株式の{max_pct:.2f}%を保有（大量保有報告）")
    if len(groups) >= 2:
        signals.append("複数の政府系ファンドが同時に保有（コンセンサス）")
    if holding_years and holding_years >= 10:
        signals.append(f"{holding_years:.0f}年以上の長期保有")
    if momentum_status in ("new", "increased"):
        signals.append(f"直近の動き：{STATUS_LABEL[momentum_status]}")
    if momentum_status == "reduced":
        signals.append("直近の動き：一部売却（注意）")
    if ratio is not None:
        diff = (ratio - 1) * 100
        signals.append(f"現在値はSWF推定取得コストより{abs(diff):.0f}%{'高い' if diff >= 0 else '安い'}")

    return {
        "code": base["code"],
        "name_ja": base["name_ja"],
        "name_en": base.get("name_en"),
        "sector": base.get("sector"),
        "size": base.get("size"),
        "holders": [
            {
                "investor_id": r["investor_id"],
                "investor_name": investor_name(r["investor_id"]),
                "investor_short": investor_name(r["investor_id"], short=True),
                "entity": r.get("entity"),
                "ownership_pct": r.get("ownership_pct"),
                "est_cost_jpy": r.get("est_cost_jpy"),
                "cost_basis_note": r.get("cost_basis_note"),
                "first_seen": r.get("first_seen"),
                "latest_report": r.get("latest_report"),
                "status": r.get("status"),
                "status_label": STATUS_LABEL.get(r.get("status"), r.get("status")),
                "history": r.get("history"),
                "confidence": r.get("confidence"),
                "sources": r.get("sources", []),
            }
            for r in rows
        ],
        "investor_groups": groups,
        "max_ownership_pct": max_pct,
        "holding_years": holding_years,
        "momentum": momentum_status,
        "ref_cost_jpy": ref_cost,
        "price_jpy": price,
        "price_to_cost": ratio,
        "candidate": bool(active) and not low_conf,
        "score": round(total, 1),
        "score_breakdown": {
            "保有比率": round(conviction, 1),
            "コンセンサス": consensus,
            "保有期間": round(duration, 1),
            "直近の売買": momentum,
            "取得コスト比": value,
        },
        "signals": signals,
    }


def universe(prices: dict[str, float | None] | None = None, today: dt.date | None = None) -> list[dict]:
    prices = prices or {}
    today = today or dt.date.today()
    by_code: dict[str, list[dict]] = {}
    for r in load_seed()["holdings"]:
        by_code.setdefault(r["code"], []).append(r)
    out = [score_stock(rows, prices.get(code), today) for code, rows in by_code.items()]
    out.sort(key=lambda s: s["score"], reverse=True)
    return out


def all_codes() -> list[str]:
    return sorted({r["code"] for r in load_seed()["holdings"]})
