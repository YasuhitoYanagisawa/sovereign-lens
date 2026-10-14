"""Aggregation, the 'Sovereign Conviction Score' and the entry-timing signal."""
from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_FILES = ["seed_holdings.json", "private_funds.json"]

STATUS_LABEL = {
    "new": "新規取得",
    "increased": "買い増し",
    "holding": "保有継続",
    "reduced": "一部売却",
    "exited": "売却・対象外",
}
CATEGORY_LABEL = {"sovereign": "政府系", "activist": "物言う株主", "financing": "警告：買わない方がよい"}
MOMENTUM_PTS = {"new": 15, "increased": 15, "holding": 10, "reduced": 3, "exited": 0}
_STATUS_RANK = {"new": 4, "increased": 4, "holding": 3, "reduced": 1, "exited": 0}

SCORE_GUIDE = [
    {"key": "保有比率", "max": 25, "desc": "ファンドが発行済株式の何%を持っているか。10%で満点。比率が高いほど本気度（確信度）が高い。"},
    {"key": "コンセンサス", "max": 20, "desc": "別々のファンドが同じ銘柄を持っているか。2グループで12点、3グループ以上で20点。"},
    {"key": "保有期間", "max": 20, "desc": "初めて保有が確認されてからの年数。10年で満点。"},
    {"key": "比率の推移", "max": 15, "desc": "保有比率が増えているか減っているか。新規・買い増しは12点、そのうち買い増し幅が3pt以上か過去最高の比率なら15点。保有継続は9点。売却中は、ピーク比で8割以上残っていれば5点、半分以上なら2点、半分を切ったら0点。"},
    {"key": "取得コスト比", "max": 15, "desc": "現在の株価がファンドの推定取得単価と比べて高いか安いか。同じかそれより安ければ15点、1.2倍以内で10点、1.5倍以内で5点。不明なら5点。"},
    {"key": "保有目的", "max": 5, "desc": "大量保有報告書の保有目的。重要提案行為等（経営への提案意思あり）は5点、経営関与は4点、純投資は3点、不明・その他は2点、政策投資・提携は1点。"},
]
TIMING_GUIDE = (
    "買い時判定は「ファンドの推定取得単価との差」「52週高値からの下落率」「200日移動平均との乖離」「ファンドの直近の売買」の4つを合計した0〜100点。"
    "70点以上＝買い場候補、55点以上＝分割で買い、40点以上＝様子見（押し目待ち）、40点未満＝見送り。過去データからの目安で、将来の値動きは保証しない。"
)


_OVERLAY: dict = {}


def set_overlay(ov: dict) -> None:
    """Apply EDINET-synced positions (ratio, prior, 保有目的) on top of the curated seed."""
    global _OVERLAY
    _OVERLAY = ov or {}
    load_seed.cache_clear()
    _inv_index.cache_clear()


def _status_from(ratio: float, prior: float | None) -> str:
    if ratio <= 0:
        return "exited"
    if prior in (None, 0):
        return "new"
    if ratio > prior + 0.005:
        return "increased"
    if ratio < prior - 0.005:
        return "reduced"
    return "holding"


def _apply_overlay(merged: dict) -> None:
    pos = (_OVERLAY or {}).get("positions") or {}
    if not pos:
        return
    idx = {(h["investor_id"], h["code"]): h for h in merged["holdings"]}
    for p in pos.values():
        k = (p.get("investor_id"), p.get("code"))
        h = idx.get(k)
        ratio = p.get("ownership_pct")
        if ratio is None:
            continue
        status = _status_from(ratio, p.get("prior_pct"))
        if ratio < 5 and status != "exited":
            status = "reduced"
        upd = {
            "ownership_pct": ratio,
            "latest_report": p.get("latest_report"),
            "status": status,
            "prior_pct": p.get("prior_pct"),
            "purpose": p.get("purpose"),
            "important_proposal": p.get("important_proposal"),
            "purpose_class": p.get("purpose_class"),
            "edinet_synced": True,
        }
        if h:
            if (h.get("latest_report") or "") > (p.get("latest_report") or ""):
                # seed is newer than the overlay: keep seed numbers, still take 保有目的
                upd = {k2: upd[k2] for k2 in ("purpose", "important_proposal", "purpose_class", "edinet_synced")}
            h.update({k2: v for k2, v in upd.items() if v is not None})
            h["peak_pct"] = max(h.get("peak_pct") or 0, p.get("peak_pct") or 0, ratio) or None
            if p.get("initial_pct") is not None and h.get("initial_pct") is None:
                h["initial_pct"] = p["initial_pct"]
            if not h.get("est_cost_jpy") and p.get("est_cost_jpy"):
                h["est_cost_jpy"] = p["est_cost_jpy"]
                h["cost_basis_note"] = "EDINET報告書の取得資金合計÷保有株数（自動取得）"
            if p.get("pdf_url"):
                h["sources"] = list(dict.fromkeys([p["pdf_url"], *h.get("sources", [])]))
        elif ratio >= 5:
            merged["holdings"].append(
                {
                    "investor_id": p["investor_id"], "entity": None, "code": p["code"],
                    "name_ja": p.get("name_ja") or p["code"], "sector": "未分類", "size": "small",
                    "ownership_pct": ratio, "peak_pct": p.get("peak_pct"), "initial_pct": p.get("initial_pct"),
                    "est_cost_jpy": p.get("est_cost_jpy") if p["investor_id"] != "nbim" else None,
                    "first_seen": p.get("first_seen"), "latest_report": p.get("latest_report"), "status": status,
                    "prior_pct": p.get("prior_pct"), "purpose": p.get("purpose"), "important_proposal": p.get("important_proposal"),
                    "purpose_class": p.get("purpose_class"), "history": "EDINETから自動取得した新規ポジション",
                    "sources": [p["pdf_url"]] if p.get("pdf_url") else [], "confidence": "medium", "edinet_synced": True,
                }
            )


@lru_cache(maxsize=1)
def load_seed() -> dict:
    merged = {"as_of": None, "investors": [], "holdings": []}
    for name in DATA_FILES:
        p = DATA_DIR / name
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        merged["as_of"] = max(filter(None, [merged["as_of"], d.get("as_of")]))
        for i in d["investors"]:
            merged["investors"].append({**i, "category": i.get("category", "sovereign")})
        merged["holdings"].extend(json.loads(json.dumps(d["holdings"])))
    _apply_overlay(merged)
    if (_OVERLAY or {}).get("updated_at"):
        merged["synced_at"] = _OVERLAY["updated_at"]
    return merged


@lru_cache(maxsize=1)
def _inv_index() -> dict[str, dict]:
    return {i["id"]: i for i in load_seed()["investors"]}


def category_of(inv_id: str) -> str:
    return _inv_index().get(inv_id, {}).get("category", "sovereign")


def investors(category: str | None = None) -> list[dict]:
    counts: dict[str, int] = {}
    for h in load_seed()["holdings"]:
        if h.get("status") != "exited":
            counts[h["investor_id"]] = counts.get(h["investor_id"], 0) + 1
    out = [
        {**i, "category_label": CATEGORY_LABEL.get(i["category"], i["category"]), "japan_holdings_tracked": counts.get(i["id"], 0)}
        for i in load_seed()["investors"]
    ]
    return [i for i in out if not category or i["category"] == category]


def investor_name(inv_id: str, short: bool = False) -> str:
    i = _inv_index().get(inv_id)
    if not i:
        return inv_id
    return i.get("short", i["name_ja"]) if short else i["name_ja"]


def holdings(investor_id: str | None = None, code: str | None = None) -> list[dict]:
    rows = load_seed()["holdings"]
    if investor_id:
        rows = [r for r in rows if r["investor_id"] == investor_id]
    if code:
        rows = [r for r in rows if r["code"].upper() == code.upper()]
    return [
        {**r, "investor_name": investor_name(r["investor_id"]), "category": category_of(r["investor_id"]), "status_label": STATUS_LABEL.get(r.get("status"), r.get("status"))}
        for r in rows
    ]


def _years_since(date_str: str | None, today: dt.date) -> float | None:
    if not date_str:
        return None
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return None
    return round((today - d).days / 365.25, 1)


def entry_timing(price: float | None, ref_cost: float | None, quote: dict | None, momentum: str) -> dict:
    """0-100 entry-timing score from cost gap, drawdown, trend and fund flow."""
    if not price:
        return {"label": "株価取得中", "score": None, "reasons": ["株価データを取得できていません"]}
    q = quote or {}
    pts, reasons = 50, []
    if ref_cost:
        r = price / ref_cost
        if r <= 1.0:
            pts += 20; reasons.append(f"ファンドの推定取得単価（{ref_cost:,.0f}円）以下で買える（{r:.2f}倍）")
        elif r <= 1.2:
            pts += 10; reasons.append(f"ファンドの推定取得単価に近い（{r:.2f}倍）")
        elif r > 1.8:
            pts -= 10; reasons.append(f"ファンドの推定取得単価の{r:.1f}倍まで上昇済み")
        else:
            reasons.append(f"ファンドの推定取得単価の{r:.2f}倍")
    hi = q.get("high52")
    if hi:
        dd = 1 - price / hi
        if dd >= 0.25:
            pts += 15; reasons.append(f"52週高値から{dd*100:.0f}%下落（押し目が深い）")
        elif dd >= 0.15:
            pts += 10; reasons.append(f"52週高値から{dd*100:.0f}%下落（押し目）")
        elif dd <= 0.03:
            pts -= 10; reasons.append("52週高値の近く（高値づかみに注意）")
        else:
            reasons.append(f"52週高値から{dd*100:.0f}%下")
    ma = q.get("ma200")
    if ma:
        g = price / ma
        if g > 1.25:
            pts -= 15; reasons.append(f"200日移動平均より{(g-1)*100:.0f}%上（過熱気味）")
        elif g >= 1.0:
            pts += 5; reasons.append(f"200日移動平均を{(g-1)*100:.0f}%上回る（上昇トレンド）")
        elif g < 0.85:
            pts -= 5; reasons.append(f"200日移動平均を{(1-g)*100:.0f}%下回る（下落トレンド）")
        else:
            reasons.append(f"200日移動平均を{(1-g)*100:.0f}%下回る")
    if momentum in ("new", "increased"):
        pts += 10; reasons.append("ファンドが直近で買い増し（需給が良い）")
    elif momentum == "reduced":
        pts -= 10; reasons.append("ファンドが直近で一部売却（需給に注意）")
    elif momentum == "heavy_selling":
        pts -= 15; reasons.append("ファンドがピークの半分以下まで売却（強い売りシグナル）")
    pts = max(0, min(100, pts))
    label = "買い場候補" if pts >= 70 else "分割で買い" if pts >= 55 else "様子見（押し目待ち）" if pts >= 40 else "見送り推奨"
    return {"label": label, "score": pts, "reasons": reasons, "high52": hi, "low52": q.get("low52"), "ma200": ma}


PURPOSE_PTS = {"重要提案行為等": 5, "経営関与": 4, "純投資": 3, "その他": 2, "不明": 2, "政策投資・提携": 1, "資金調達の引受け": 0}
_PURPOSE_RANK = ["重要提案行為等", "経営関与", "純投資", "その他", "政策投資・提携", "不明", "資金調達の引受け"]


def default_purpose(r: dict) -> str:
    """Before an EDINET sync fills it, fall back to what the filer type almost always states."""
    if r.get("purpose_class"):
        return r["purpose_class"]
    cat = category_of(r["investor_id"])
    if cat == "financing":
        return "資金調達の引受け"
    if r.get("purpose"):
        import re as _re
        if _re.search("重要提案|提案|助言", r["purpose"]):
            return "重要提案行為等"
        return "純投資"
    return "不明"


def best_purpose(rows: list[dict]) -> str:
    classes = [default_purpose(r) for r in rows] or ["不明"]
    return min(classes, key=lambda c: _PURPOSE_RANK.index(c) if c in _PURPOSE_RANK else 99)


def _initial_pct(r: dict) -> float | None:
    if r.get("initial_pct") is not None:
        return r["initial_pct"]
    import re as _re
    m = _re.search(r"([0-9]+\.[0-9]+)%で新規", r.get("history") or "")
    return float(m.group(1)) if m else None


def trend_of(r: dict) -> dict:
    """比率の推移: build-up size, distance from peak, and heavy selling."""
    st, pct, peak = r.get("status"), r.get("ownership_pct"), r.get("peak_pct")
    init, prior = _initial_pct(r), r.get("prior_pct")
    if pct is None:
        return {"pts": MOMENTUM_PTS.get(st, 0), "text": STATUS_LABEL.get(st), "heavy_selling": False}
    if st in ("new", "increased"):
        gain = pct - init if init is not None else None
        at_peak = peak is None or pct >= peak * 0.98
        strong = (gain is not None and gain >= 3) or (at_peak and st == "increased")
        txt = f"{init:.2f}%→{pct:.2f}%（+{gain:.1f}pt）に買い増し" if gain is not None and gain > 0 else (f"直前{prior:.2f}%→{pct:.2f}%に買い増し" if prior else (f"{pct:.2f}%まで買い増し" if st == "increased" else f"{pct:.2f}%で新規取得"))
        if at_peak and st == "increased":
            txt += "・過去最高の比率"
        return {"pts": 15 if strong else 12, "text": txt, "heavy_selling": False}
    if st == "holding":
        return {"pts": 9, "text": f"{pct:.2f}%で保有継続", "heavy_selling": False}
    if st == "reduced":
        if peak:
            keep = pct / peak
            txt = f"ピーク{peak:.2f}%→{pct:.2f}%（-{peak - pct:.1f}pt）に売却"
            if keep < 0.5:
                return {"pts": 0, "text": txt + "・ピークの半分以下", "heavy_selling": True}
            return {"pts": 5 if keep >= 0.8 else 2, "text": txt, "heavy_selling": False}
        return {"pts": 3, "text": f"{pct:.2f}%に一部売却", "heavy_selling": False}
    return {"pts": 0, "text": STATUS_LABEL.get(st), "heavy_selling": False}


def score_stock(rows: list[dict], quote, today: dt.date) -> dict:
    """quote: float price or dict {price, high52, low52, ma200}."""
    q = quote if isinstance(quote, dict) else ({"price": quote} if quote else {})
    price = q.get("price")
    active_all = [r for r in rows if r.get("status") != "exited"]
    financing = [r for r in active_all if category_of(r["investor_id"]) == "financing"]
    active = [r for r in active_all if category_of(r["investor_id"]) != "financing"]
    base = rows[0]
    cats = sorted({category_of(r["investor_id"]) for r in active_all})
    groups = sorted({r["investor_id"] for r in active})
    pcts = [r["ownership_pct"] for r in active if r.get("ownership_pct") is not None]
    max_pct = max(pcts) if pcts else None
    years = [y for y in (_years_since(r.get("first_seen"), today) for r in active) if y is not None]
    holding_years = max(years) if years else None
    best = max(active, key=lambda r: _STATUS_RANK.get(r.get("status"), 0)) if active else None
    momentum_status = best.get("status") if best else "exited"
    costs = [r for r in active if r.get("est_cost_jpy")]
    ref = max(costs, key=lambda r: (category_of(r["investor_id"]) == "sovereign", r.get("confidence") == "high", r.get("ownership_pct") or 0)) if costs else None
    ref_cost = ref["est_cost_jpy"] if ref else None
    ratio = round(price / ref_cost, 3) if (price and ref_cost) else None

    conviction = min(max_pct or 0.5, 10) / 10 * 25
    consensus = {0: 0, 1: 0, 2: 12}.get(len(groups), 20)
    duration = min(holding_years or 0, 10) / 10 * 20
    trend = trend_of(best) if best else {"pts": 0, "text": None, "heavy_selling": False}
    momentum = trend["pts"]
    pclass = best_purpose(active)
    purpose_pts = PURPOSE_PTS.get(pclass, 2)
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
    total = conviction + consensus + duration + momentum + value + purpose_pts
    low_conf = all(r.get("confidence") == "low" for r in active) if active else True
    if low_conf:
        total *= 0.7

    signals = []
    top = max((r for r in active if r.get("ownership_pct") is not None), key=lambda r: r["ownership_pct"], default=None)
    if top and top["ownership_pct"] >= 5:
        signals.append(f"{investor_name(top['investor_id'], short=True)}（{CATEGORY_LABEL.get(category_of(top['investor_id']))}）が発行済株式の{top['ownership_pct']:.2f}%を保有（大量保有報告）")
    if len(groups) >= 2:
        signals.append("複数のファンドが同時に保有（コンセンサス）")
    if "sovereign" in cats and "activist" in cats:
        signals.append("政府系と物言う株主の両方が保有")
    if holding_years and holding_years >= 10:
        signals.append(f"{holding_years:.0f}年以上の長期保有")
    if trend.get("text"):
        signals.append(("⚠ " if trend.get("heavy_selling") else "") + "比率の推移：" + trend["text"])
    if pclass not in ("不明",):
        signals.append(f"保有目的：{pclass}")
    if ratio is not None:
        diff = (ratio - 1) * 100
        signals.append(f"現在値はファンドの推定取得単価より{abs(diff):.0f}%{'高い' if diff >= 0 else '安い'}")
    warning = None
    if financing:
        names = "・".join(dict.fromkeys(investor_name(r["investor_id"], short=True) for r in financing))
        warning = f"【警告：買わない方がよい】資金調達型の投資家（{names}）が新株予約権などを引き受けて保有しています。行使して得た株を市場で売るため、株価が下がりやすく、株式の希薄化も起きます。"
        signals.append(f"⚠ 警告：資金調達型（{names}）が保有。買わない方がよい")

    return {
        "code": base["code"],
        "name_ja": base["name_ja"],
        "name_en": base.get("name_en"),
        "sector": base.get("sector"),
        "size": base.get("size"),
        "categories": cats,
        "holders": [
            {
                "investor_id": r["investor_id"],
                "investor_name": investor_name(r["investor_id"]),
                "investor_short": investor_name(r["investor_id"], short=True),
                "category": category_of(r["investor_id"]),
                "entity": r.get("entity"),
                "ownership_pct": r.get("ownership_pct"),
                "est_cost_jpy": r.get("est_cost_jpy"),
                "cost_basis_note": r.get("cost_basis_note"),
                "first_seen": r.get("first_seen"),
                "latest_report": r.get("latest_report"),
                "status": r.get("status"),
                "status_label": STATUS_LABEL.get(r.get("status"), r.get("status")),
                "history": r.get("history"),
                "peak_pct": r.get("peak_pct"),
                "prior_pct": r.get("prior_pct"),
                "trend": trend_of(r)["text"],
                "purpose": r.get("purpose"),
                "purpose_class": r.get("purpose_class") or default_purpose(r),
                "important_proposal": r.get("important_proposal"),
                "edinet_synced": bool(r.get("edinet_synced")),
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
        "candidate": bool(active) and not low_conf and not financing,
        "warning": warning,
        "score": round(total, 1) if active else 0,
        "score_breakdown": {
            "保有比率": round(conviction, 1),
            "コンセンサス": consensus,
            "保有期間": round(duration, 1),
            "比率の推移": momentum,
            "取得コスト比": value,
            "保有目的": purpose_pts,
        },
        "trend": trend.get("text"),
        "heavy_selling": trend.get("heavy_selling", False),
        "purpose_class": pclass,
        "timing": entry_timing(price, ref_cost, q, "heavy_selling" if trend.get("heavy_selling") else momentum_status),
        "signals": signals,
    }


def universe(quotes: dict | None = None, today: dt.date | None = None) -> list[dict]:
    quotes = quotes or {}
    today = today or dt.date.today()
    by_code: dict[str, list[dict]] = {}
    order = {"sovereign": 0, "activist": 1, "financing": 2}
    rows = sorted(load_seed()["holdings"], key=lambda r: order.get(category_of(r["investor_id"]), 9))
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)
    out = [score_stock(rs, quotes.get(code), today) for code, rs in by_code.items()]
    out.sort(key=lambda s: s["score"], reverse=True)
    return out


def all_codes() -> list[str]:
    return sorted({r["code"] for r in load_seed()["holdings"]})
