"""Deterministic strategy engine with hard guardrails.

The LLM never decides numbers on its own: it calls build_strategy(), which applies
risk-profile limits (position / sector caps, cash reserve, size filters) and
returns an auditable plan plus a list of compliance checks.
"""
from __future__ import annotations

from dataclasses import dataclass

CORE_FUND = {
    "code": "1306",
    "name": "TOPIX連動ETF（例：NEXT FUNDS TOPIX連動型上場投信）または国内株式インデックス投信",
    "why": "NBIMは日本株を約1,400社保有しており、実態は市場全体に近い分散投資。コア部分はその『市場全体を長期保有』という姿勢を低コストで再現する。",
}


@dataclass(frozen=True)
class RiskProfile:
    label: str
    core: float
    satellite: float
    cash: float
    max_single: float
    max_sector: float
    sizes: tuple[str, ...]
    min_score: float


PROFILES = {
    "conservative": RiskProfile("安定重視", 0.65, 0.30, 0.05, 0.08, 0.15, ("large",), 35),
    "balanced": RiskProfile("バランス", 0.45, 0.50, 0.05, 0.12, 0.25, ("large", "mid"), 30),
    "aggressive": RiskProfile("積極運用", 0.25, 0.70, 0.05, 0.18, 0.35, ("large", "mid", "small"), 25),
}

MIN_INVEST = 10_000
MAX_INVEST = 10_000_000_000


class StrategyInputError(ValueError):
    pass


def _cap_weights(items: list[dict], weights: dict[str, float], budget: float, max_single: float, max_sector: float) -> dict[str, float]:
    """Water-filling: allocate `budget` proportionally to weights subject to
    single-name and sector caps. Whatever cannot be placed stays unallocated (-> cash)."""
    sector_of = {it["code"]: it["sector"] for it in items}
    frozen: dict[str, float] = {}
    for _ in range(len(items) * 2 + 2):
        free = [c for c in weights if c not in frozen]
        if not free:
            break
        remaining = budget - sum(frozen.values())
        if remaining <= 1e-12:
            break
        tw = sum(weights[c] for c in free)
        alloc = {**frozen, **{c: remaining * weights[c] / tw for c in free}}
        changed = False
        for c in free:
            if alloc[c] > max_single + 1e-12:
                frozen[c] = max_single
                changed = True
        if changed:
            continue
        sec_tot: dict[str, float] = {}
        for c, a in alloc.items():
            sec_tot[sector_of[c]] = sec_tot.get(sector_of[c], 0) + a
        for sec, tot in sec_tot.items():
            if tot > max_sector + 1e-12:
                for c, a in alloc.items():
                    if sector_of[c] == sec:
                        frozen[c] = a * max_sector / tot
                changed = True
        if not changed:
            return _enforce_sector(alloc, sector_of, max_sector)
    alloc = {c: frozen.get(c, 0.0) for c in weights}
    return _enforce_sector(alloc, sector_of, max_sector)


def _enforce_sector(alloc: dict[str, float], sector_of: dict[str, str], max_sector: float) -> dict[str, float]:
    tot: dict[str, float] = {}
    for c, a in alloc.items():
        tot[sector_of[c]] = tot.get(sector_of[c], 0) + a
    return {c: (a * max_sector / tot[sector_of[c]] if tot[sector_of[c]] > max_sector else a) for c, a in alloc.items()}


def _diverse_picks(pool: list[dict], n: int) -> list[dict]:
    """Take the top-n by score, but at most ~1/3 of the names (min 1, max 3) from one sector."""
    per_sector = max(1, min(3, n // 3))
    picks, count = [], {}
    for s in pool:
        if count.get(s["sector"], 0) >= per_sector:
            continue
        picks.append(s)
        count[s["sector"]] = count.get(s["sector"], 0) + 1
        if len(picks) >= n:
            break
    return picks


def build_strategy(
    universe: list[dict],
    investable_jpy: int,
    risk: str = "balanced",
    horizon_years: int = 10,
    use_nisa: bool = True,
    exclude_codes: list[str] | None = None,
    max_names: int | None = None,
    include_activists: bool = False,
) -> dict:
    if risk not in PROFILES:
        raise StrategyInputError(f"risk must be one of {list(PROFILES)}")
    if not isinstance(investable_jpy, (int, float)) or not (MIN_INVEST <= investable_jpy <= MAX_INVEST):
        raise StrategyInputError(f"investable_jpy must be between {MIN_INVEST:,} and {MAX_INVEST:,}")
    if not (1 <= int(horizon_years) <= 50):
        raise StrategyInputError("horizon_years must be 1-50")
    p = PROFILES[risk]
    exclude = {c.upper() for c in (exclude_codes or [])}
    warnings: list[str] = []

    core_w, sat_w, cash_w = p.core, p.satellite, p.cash
    if horizon_years < 3:
        moved = sat_w * 0.5
        sat_w -= moved
        cash_w += moved
        warnings.append("投資期間が3年未満のため、個別株枠の半分を現金・短期資産に移しました。")

    pool = [
        s
        for s in universe
        if s["candidate"]
        and not s.get("heavy_selling")
        and ("sovereign" in s.get("categories", ["sovereign"]) or (include_activists and "activist" in s.get("categories", [])))
        and s["code"].upper() not in exclude
        and s.get("size") in p.sizes
        and s["score"] >= p.min_score
    ]
    if max_names is None:
        max_names = max(3, min(10, int(investable_jpy * sat_w // 100_000) or 3))
    picks = _diverse_picks(pool, max_names)
    sat_budget = investable_jpy * sat_w
    weights = {s["code"]: max(s["score"], 1) for s in picks}
    alloc_frac = _cap_weights(
        [{"code": s["code"], "sector": s["sector"]} for s in picks],
        {k: v for k, v in weights.items()},
        sat_w,
        p.max_single,
        p.max_sector,
    ) if picks else {}

    positions = []
    sat_used = 0.0
    for s in picks:
        target = investable_jpy * alloc_frac.get(s["code"], 0)
        price = s.get("price_jpy")
        shares = lots = None
        method = "金額指定（株価取得不可のため株数は発注時に確定）"
        amount = target
        if price:
            n = int(target // price)
            if n >= 100:
                shares = (n // 100) * 100
                lots = shares // 100
                method = f"単元株（{lots}単元＝{shares:,}株）"
            elif n >= 1:
                shares = n
                method = f"単元未満株（S株・ミニ株等で{n}株）"
            else:
                shares = 0
                method = "予算不足（1株未満）→ 見送り"
            amount = shares * price
        if amount <= 0:
            continue
        sat_used += amount
        ref = s.get("ref_cost_jpy")
        timing = s.get("timing") or {}
        if timing.get("score") is not None and timing["score"] < 40:
            entry = f"【{timing['label']}】今は買わず、押し目（52週高値から15%程度の下落）を待つ"
        elif timing.get("score") is not None and timing["score"] >= 70:
            entry = f"【{timing['label']}】2回に分けて購入"
        elif price and ref:
            if price <= ref * 1.1:
                entry = "SWFの推定取得コスト付近。2回に分けて購入"
            else:
                entry = f"SWF推定コストより{(price / ref - 1) * 100:.0f}%高い。3か月かけて3回に分けて購入（時間分散）"
        else:
            entry = "3回に分けて時間分散で購入"
        positions.append(
            {
                "code": s["code"],
                "name": s["name_ja"],
                "sector": s["sector"],
                "score": s["score"],
                "weight_pct": round(amount / investable_jpy * 100, 2),
                "amount_jpy": round(amount),
                "price_jpy": price,
                "shares": shares,
                "order_method": method,
                "entry_plan": entry,
                "timing": (s.get("timing") or {}).get("label"),
                "trend": s.get("trend"),
                "purpose_class": s.get("purpose_class"),
                "timing_score": (s.get("timing") or {}).get("score"),
                "categories": s.get("categories", []),
                "followed_investors": list(dict.fromkeys(h.get("investor_short", h["investor_name"]) for h in s["holders"] if h["status"] != "exited")),
                "signals": s["signals"],
            }
        )

    core_amt = investable_jpy * core_w
    leftover = sat_budget - sat_used
    cash_amt = investable_jpy * cash_w + leftover
    total = core_amt + sat_used + cash_amt

    sector_exposure: dict[str, float] = {}
    for pos in positions:
        sector_exposure[pos["sector"]] = sector_exposure.get(pos["sector"], 0) + pos["amount_jpy"] / investable_jpy * 100

    checks = [
        {"rule": "1銘柄あたりの上限", "limit": f"{p.max_single * 100:.0f}%", "passed": all(pos["weight_pct"] <= p.max_single * 100 + 0.01 for pos in positions)},
        {"rule": "1業種あたりの上限", "limit": f"{p.max_sector * 100:.0f}%", "passed": all(v <= p.max_sector * 100 + 0.01 for v in sector_exposure.values())},
        {"rule": "現金比率の下限", "limit": f"{p.cash * 100:.0f}%以上", "passed": cash_amt >= investable_jpy * p.cash - 1},
        {"rule": "投資可能額を超えない", "limit": f"{investable_jpy:,.0f}円", "passed": total <= investable_jpy + 1},
        {"rule": "売却済み・非公開化銘柄を除外", "limit": "status≠exited", "passed": all(s["candidate"] for s in picks)},
        {"rule": "リスク許容度に合う銘柄規模", "limit": "/".join(p.sizes), "passed": all(s.get("size") in p.sizes for s in picks)},
    ]
    if not positions:
        warnings.append("条件に合う個別銘柄がありませんでした。コアのインデックスと現金のみの構成です。")
    if any(pos["price_jpy"] is None for pos in positions):
        warnings.append("一部銘柄の株価が取得できなかったため、株数ではなく金額で表示しています。")
    if use_nisa:
        warnings.append("NISA成長投資枠は年240万円まで。超える分は特定口座で購入してください。")

    return {
        "inputs": {"include_activists": include_activists, "investable_jpy": investable_jpy, "risk": risk, "risk_label": p.label, "horizon_years": horizon_years, "use_nisa": use_nisa, "exclude_codes": sorted(exclude)},
        "allocation": {
            "core_index_jpy": round(core_amt),
            "satellite_stocks_jpy": round(sat_used),
            "cash_jpy": round(cash_amt),
            "core_pct": round(core_amt / investable_jpy * 100, 1),
            "satellite_pct": round(sat_used / investable_jpy * 100, 1),
            "cash_pct": round(cash_amt / investable_jpy * 100, 1),
        },
        "core": {**CORE_FUND, "amount_jpy": round(core_amt)},
        "positions": positions,
        "sector_exposure_pct": {k: round(v, 1) for k, v in sorted(sector_exposure.items(), key=lambda x: -x[1])},
        "compliance_checks": checks,
        "all_checks_passed": all(c["passed"] for c in checks),
        "warnings": warnings,
        "requires_human_approval": True,
        "disclaimer": "本プランは公開情報に基づく情報提供であり、投資助言・勧誘ではありません。発注は行いません。最終判断はご自身で行ってください。",
    }
