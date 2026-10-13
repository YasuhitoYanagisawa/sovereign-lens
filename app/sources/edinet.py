"""EDINET API v2 client: watches 大量保有報告書 / 変更報告書 filed by sovereign funds.

docTypeCode 350 = 大量保有報告書・変更報告書, 360 = 訂正報告書.
Requires a free API key (EDINET_API_KEY). Without it the agent falls back to seed data.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import os
import re
import zipfile

import httpx

log = logging.getLogger(__name__)
API = "https://api.edinet-fsa.go.jp/api/v2"
LARGE_HOLDING_DOC_TYPES = {"350", "360"}

# Japanese item labels in the XBRL->CSV export we want to surface.
_FIELDS = {
    "issuer_name": ["発行者の名称"],
    "sec_code": ["証券コード"],
    "holding_ratio_pct": ["株券等保有割合"],
    "prior_ratio_pct": ["直前の報告書に記載された株券等保有割合"],
    "shares_held": ["保有株券等の数（総数）", "保有株券等の数"],
    "total_funds_jpy": ["取得資金合計"],
    "own_funds_jpy": ["自己資金額"],
    "purpose": ["保有目的"],
}


def api_key() -> str | None:
    return os.environ.get("EDINET_API_KEY") or None


def _match_filer(filer_name: str, keywords: list[str]) -> bool:
    f = (filer_name or "").lower()
    return any(k.lower() in f for k in keywords)


async def list_large_holding_filings(
    keywords_by_investor: dict[str, list[str]], days: int = 14, end: dt.date | None = None
) -> list[dict]:
    key = api_key()
    if not key:
        raise RuntimeError("EDINET_API_KEY is not configured")
    end = end or dt.date.today()
    found: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i in range(days):
            day = end - dt.timedelta(days=i)
            r = await client.get(
                f"{API}/documents.json",
                params={"date": day.isoformat(), "type": 2, "Subscription-Key": key},
            )
            if r.status_code != 200:
                log.warning("edinet list %s -> %s", day, r.status_code)
                continue
            body = r.json() or {}
            status = str((body.get("metadata") or {}).get("status") or body.get("StatusCode") or "200")
            if status != "200":
                raise RuntimeError(f"EDINET API returned status {status} (check EDINET_API_KEY)")
            for doc in body.get("results") or []:
                if doc.get("docTypeCode") not in LARGE_HOLDING_DOC_TYPES:
                    continue
                for inv_id, kws in keywords_by_investor.items():
                    if _match_filer(doc.get("filerName", ""), kws):
                        found.append(
                            {
                                "investor_id": inv_id,
                                "doc_id": doc.get("docID"),
                                "filer_name": doc.get("filerName"),
                                "description": doc.get("docDescription"),
                                "submitted": doc.get("submitDateTime"),
                                "issuer_edinet_code": doc.get("issuerEdinetCode")
                                or doc.get("subjectEdinetCode"),
                                "csv_available": doc.get("csvFlag") == "1",
                                "pdf_url": f"https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{doc.get('docID')}.pdf",
                            }
                        )
                        break
    return found


def parse_large_holding_csv(zip_bytes: bytes) -> dict:
    """Extract key facts from the XBRL_TO_CSV export (UTF-16 TSV)."""
    out: dict = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.lower().endswith(".csv"):
                continue
            raw = z.read(name)
            for enc in ("utf-16", "utf-8-sig", "cp932"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                continue
            for row in csv.reader(io.StringIO(text), delimiter="\t"):
                if len(row) < 3:
                    continue
                label, value = row[1], row[-1]
                for field, labels in _FIELDS.items():
                    if field in out:
                        continue
                    if any(label.strip() == l for l in labels):
                        out[field] = value.strip()
    for f in ("holding_ratio_pct", "prior_ratio_pct"):
        if f in out:
            try:
                v = float(out[f])
                out[f] = round(v * 100, 2) if v <= 1 else v
            except ValueError:
                pass
    for f in ("shares_held", "total_funds_jpy", "own_funds_jpy"):
        if f in out:
            try:
                out[f] = int(float(re.sub(r"[^\d.]", "", out[f]) or 0))
            except ValueError:
                pass
    if out.get("shares_held") and out.get("total_funds_jpy"):
        out["est_cost_jpy"] = round(out["total_funds_jpy"] / out["shares_held"])
    return out


async def fetch_filing_detail(doc_id: str) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("EDINET_API_KEY is not configured")
    if not re.fullmatch(r"S[0-9A-Z]{7}", doc_id or ""):
        raise ValueError("invalid docID")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{API}/documents/{doc_id}", params={"type": 5, "Subscription-Key": key})
        r.raise_for_status()
    detail = parse_large_holding_csv(r.content)
    detail["doc_id"] = doc_id
    return detail
