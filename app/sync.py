"""EDINET → overlay sync (保有比率・保有目的の定期更新).

A Cloud Scheduler job POSTs /api/admin/sync daily. We scan EDINET for new
大量保有報告書/変更報告書 by every tracked filer, parse ratio / prior ratio / funds /
保有目的 / 重要提案行為等, and merge them into an overlay JSON stored in Cloud Storage
(OVERLAY_BUCKET). Every instance loads the overlay at startup and reloads it periodically,
so new filings change scores and plans without a redeploy.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

from . import analytics
from .sources import edinet

log = logging.getLogger(__name__)
OVERLAY_OBJECT = "edinet_overlay.json"
LOCAL_PATH = Path(os.environ.get("OVERLAY_LOCAL_PATH", "/tmp/edinet_overlay.json"))
_loaded_at = 0.0


def _bucket():
    name = os.environ.get("OVERLAY_BUCKET")
    if not name:
        return None
    from google.cloud import storage

    return storage.Client().bucket(name)


def load_overlay() -> dict:
    b = _bucket()
    try:
        if b is not None:
            blob = b.blob(OVERLAY_OBJECT)
            if blob.exists():
                return json.loads(blob.download_as_text())
        elif LOCAL_PATH.exists():
            return json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("overlay load failed: %s", e)
    return {"positions": {}, "docs_seen": [], "updated_at": None, "scanned": []}


def save_overlay(ov: dict) -> None:
    data = json.dumps(ov, ensure_ascii=False)
    b = _bucket()
    if b is not None:
        b.blob(OVERLAY_OBJECT).upload_from_string(data, content_type="application/json")
    else:
        LOCAL_PATH.write_text(data, encoding="utf-8")


def refresh(max_age: int = 1800, force: bool = False) -> bool:
    """(Re)load the overlay into analytics if stale. Returns True when reloaded."""
    global _loaded_at
    if not force and time.time() - _loaded_at < max_age:
        return False
    analytics.set_overlay(load_overlay())
    _loaded_at = time.time()
    return True


def merge_filing(ov: dict, inv_id: str, f: dict, d: dict) -> str | None:
    code = d.get("sec_code")
    ratio = d.get("holding_ratio_pct")
    if not code or ratio is None:
        return None
    key = f"{inv_id}|{code}"
    date = (f.get("submitted") or "")[:10]
    pos = ov["positions"].get(key, {})
    if pos.get("latest_report") and pos["latest_report"] > date:
        # older filing than what we have: only use it to widen history
        pos["first_seen"] = min(pos.get("first_seen") or date, date)
        pos["peak_pct"] = max(pos.get("peak_pct") or 0, ratio)
        ov["positions"][key] = pos
        return key
    history = pos.get("history", [])
    history.append({"date": date, "pct": ratio, "doc_id": f.get("doc_id")})
    pos.update(
        {
            "investor_id": inv_id,
            "code": code,
            "name_ja": d.get("issuer_name") or pos.get("name_ja"),
            "ownership_pct": ratio,
            "prior_pct": d.get("prior_ratio_pct"),
            "latest_report": date,
            "first_seen": min(pos.get("first_seen") or date, date),
            "initial_pct": pos.get("initial_pct") if pos.get("initial_pct") is not None else (d.get("prior_ratio_pct") if d.get("prior_ratio_pct") else ratio),
            "peak_pct": max(pos.get("peak_pct") or 0, ratio),
            "purpose": d.get("purpose") or pos.get("purpose"),
            "important_proposal": d.get("important_proposal"),
            "purpose_class": d.get("purpose_class") or pos.get("purpose_class"),
            "doc_title": d.get("doc_title"),
            "doc_id": f.get("doc_id"),
            "pdf_url": f.get("pdf_url"),
            "history": history[-20:],
        }
    )
    if d.get("est_cost_jpy") and inv_id != "nbim":  # NBIM funds include lending turnover
        pos["est_cost_jpy"] = d["est_cost_jpy"]
    ov["positions"][key] = pos
    return key


async def run_sync(days: int = 3, end: dt.date | None = None, max_docs: int = 150) -> dict:
    """Scan `days` days ending at `end` and merge new filings into the overlay."""
    if not edinet.api_key():
        return {"status": "disabled", "reason": "EDINET_API_KEY not set"}
    t0 = time.time()
    ov = load_overlay()
    seen = set(ov.get("docs_seen", []))
    kws = {i["id"]: i.get("edinet_filer_keywords", []) for i in analytics.investors()}
    try:
        filings = await edinet.list_large_holding_filings(kws, days=days, end=end)
    except Exception as e:  # noqa: BLE001
        log.warning("sync list failed: %s", e)
        return {"status": "error", "reason": str(e)[:200]}
    filings.sort(key=lambda f: f.get("submitted") or "")
    updated, skipped, errors = [], 0, 0
    for f in filings:
        if f["doc_id"] in seen or not f.get("csv_available"):
            skipped += 1
            continue
        if len(updated) + errors >= max_docs or time.time() - t0 > 240:
            break
        try:
            d = await edinet.fetch_filing_detail(f["doc_id"])
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning("detail %s failed: %s", f["doc_id"], type(e).__name__)
            continue
        try:
            key = merge_filing(ov, f["investor_id"], f, d)
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning("merge %s failed: %s", f["doc_id"], e)
            continue
        seen.add(f["doc_id"])
        if key:
            updated.append(key)
    end = end or dt.date.today()
    ov["docs_seen"] = sorted(seen)[-5000:]
    ov["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    ov.setdefault("scanned", []).append({"end": end.isoformat(), "days": days, "at": ov["updated_at"], "updated": len(updated)})
    ov["scanned"] = ov["scanned"][-60:]
    save_overlay(ov)
    analytics.set_overlay(ov)
    return {
        "status": "ok",
        "window": f"{(end - dt.timedelta(days=days - 1)).isoformat()}..{end.isoformat()}",
        "filings_found": len(filings),
        "updated": len(updated),
        "skipped": skipped,
        "errors": errors,
        "positions_total": len(ov["positions"]),
        "elapsed_s": round(time.time() - t0, 1),
    }
