"""EDINET API v2 client: watches 大量保有報告書 / 変更報告書 filed by sovereign funds.

docTypeCode 350 = 大量保有報告書・変更報告書, 360 = 訂正報告書.
Requires a free API key (EDINET_API_KEY). Without it the agent falls back to seed data.
"""
from __future__ import annotations

import csv
import asyncio
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
            r, body = None, {}
            for wait in (0, 3, 8, 20):  # back off on EDINET rate limiting (429)
                if wait:
                    await asyncio.sleep(wait)
                try:
                    r = await client.get(
                        f"{API}/documents.json",
                        params={"date": day.isoformat(), "type": 2, "Subscription-Key": key},
                    )
                    body = (r.json() or {}) if r.status_code == 200 else {}
                except Exception as e:  # noqa: BLE001 - one bad day must not abort the scan
                    log.warning("edinet list %s failed: %s", day, type(e).__name__)
                    r = None
                    continue
                st = str((body.get("metadata") or {}).get("status") or body.get("StatusCode") or r.status_code)
                if r.status_code != 429 and st != "429":
                    break
            if r is None:
                continue
            if r.status_code != 200:
                log.warning("edinet list %s -> %s", day, r.status_code)
                continue
            status = str((body.get("metadata") or {}).get("status") or body.get("StatusCode") or "200")
            if status == "401":
                raise RuntimeError("EDINET API returned 401 (check EDINET_API_KEY)")
            if status != "200":
                log.warning("edinet list %s metadata status %s", day, status)
                continue
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


_NA = {"", "－", "―", "-", "該当なし", "該当事項なし", "なし"}


def _num(v: str | None) -> float | None:
    if v is None or v.strip() in _NA:
        return None
    try:
        return float(re.sub(r"[^0-9.\-]", "", v) or "nan")
    except ValueError:
        return None


def classify_purpose(purpose: str | None, proposal: str | None) -> str:
    """Map 保有目的 / 重要提案行為等 free text to a coarse class."""
    p = (purpose or "").replace(" ", "")
    prop = (proposal or "").strip()
    if prop and prop not in _NA:
        return "重要提案行為等"
    if re.search(r"重要提案|株主提案|助言|提案", p) and not re.search(r"(重要提案行為等?|提案)を行う(予定|意図|つもり)は(ない|ありません)", p):
        return "重要提案行為等"
    if re.search(r"支配|経営参加|子会社化|経営権", p):
        return "経営関与"
    if re.search(r"政策投資|業務提携|資本提携|取引関係|安定株主", p):
        return "政策投資・提携"
    if re.search(r"新株予約権|行使|引受", p):
        return "資金調達の引受け"
    if re.search(r"純投資|投資目的|ポートフォリオ|資産運用|割安", p):
        return "純投資"
    return "その他" if p else "不明"


def parse_large_holding_csv(zip_bytes: bytes) -> dict:
    """Extract facts from the XBRL_TO_CSV export (UTF-16 TSV) by XBRL element id.

    Context 'FilingDateInstant' carries the joint (共同保有合算) figures; contexts with a
    suffix carry per-holder figures (funds, purpose).
    """
    joint: dict[str, str] = {}
    per: dict[str, list[str]] = {}
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
                if len(row) < 4 or ":" not in row[0]:
                    continue
                el, ctx, val = row[0].split(":", 1)[1], row[2], row[-1].strip()
                if ctx == "FilingDateInstant":
                    joint.setdefault(el, val)
                per.setdefault(el, []).append(val)

    def first(el: str) -> str | None:
        for v in per.get(el, []):
            if v not in _NA:
                return v
        return None

    out: dict = {
        "doc_title": joint.get("DocumentTitleCoverPage"),
        "issuer_name": joint.get("NameOfIssuer") or first("NameOfIssuer"),
        "sec_code": (joint.get("SecurityCodeOfIssuer") or first("SecurityCodeOfIssuer") or "").strip()[:4] or None,
    }
    ratio = _num(joint.get("HoldingRatioOfShareCertificatesEtc"))
    prior = _num(joint.get("HoldingRatioOfShareCertificatesEtcPerLastReport"))
    # the XBRL ratio is a fraction (0.0510); older/odd filings may already be a percentage
    out["holding_ratio_pct"] = round(ratio * 100, 2) if ratio is not None and ratio <= 1 else ratio
    out["prior_ratio_pct"] = round(prior * 100, 2) if prior is not None and prior <= 1 else prior
    shares = _num(joint.get("TotalNumberOfStocksEtcHeld"))
    out["shares_held"] = int(shares) if shares else None
    funds = [f for f in (_num(v) for v in per.get("TotalAmountOfFundingForAcquisition", [])) if f]
    out["total_funds_jpy"] = int(sum(funds)) if funds else None
    purpose = first("PurposeOfHolding")
    proposal = first("ActOfMakingImportantProposalEtc")
    out["purpose"] = re.sub(r"\s+", " ", purpose)[:400] if purpose else None
    out["important_proposal"] = re.sub(r"\s+", " ", proposal)[:300] if proposal else None
    out["purpose_class"] = classify_purpose(purpose, proposal)
    if out["shares_held"] and out["total_funds_jpy"]:
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
