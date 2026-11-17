"""PubMed source bot.

Uses NCBI E-utilities (esearch + esummary). Free, no API key required.
Without an API key NCBI asks that clients make at most 3 requests per
second — we enforce a conservative 0.4s minimum between calls to stay
below that even when esearch and esummary run back to back.

We save only description-only metadata: id, title, authors, date,
category (journal), license (empty — PubMed does not ship a license
field here), URL. Every record is stamped with mark="未測" because this
bot does not evaluate content, it only collects candidates.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

_MIN_INTERVAL_SEC = 0.4  # < 3 req/s even with esearch + esummary back to back
_last_call_ts = 0.0


def _rate_limit() -> None:
    global _last_call_ts
    wait = _MIN_INTERVAL_SEC - (time.monotonic() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.monotonic()


def _get_json(url: str) -> Any:
    _rate_limit()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ban-bots/1.0 (+https://github.com/fc0web/ban-bots)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _fetch_one_query(q: str, limit: int, reldate: int | None = None) -> list[dict[str, Any]]:
    if limit <= 0 or not q:
        return []
    qp: dict[str, str] = {
        "db": "pubmed",
        "term": q,
        "retmax": str(limit),
        "retmode": "json",
        "sort": "date",
    }
    if reldate and reldate > 0:
        # Restrict esearch to papers whose publication date is within the
        # last `reldate` days. Combined with sort=date, the window shifts
        # forward every day so the top-N result set also shifts — sparse
        # niches no longer stay pinned to the same PMIDs forever.
        qp["reldate"] = str(reldate)
        qp["datetype"] = "pdat"  # publication date
    search = _get_json(f"{ESEARCH_URL}?" + urllib.parse.urlencode(qp))
    ids = (search.get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []
    summ = _get_json(
        f"{ESUMMARY_URL}?"
        + urllib.parse.urlencode({
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "json",
        })
    )
    result = summ.get("result") or {}
    out: list[dict[str, Any]] = []
    for pmid in ids:
        rec = result.get(pmid)
        if not isinstance(rec, dict):
            continue
        authors = [
            (a.get("name") or "").strip()
            for a in (rec.get("authors") or []) if a.get("name")
        ]
        out.append({
            "source": "pubmed",
            "id": f"pubmed:{pmid}",
            "title": " ".join(str(rec.get("title") or "").split()),
            "authors": authors,
            "date": rec.get("pubdate") or rec.get("epubdate") or "",
            "category": rec.get("fulljournalname") or rec.get("source") or "",
            "license": "",
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "query": q,
            "mark": "未測",
        })
    return out


def fetch(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch PubMed metadata records.

    params (either shape):
      queries: list of {"q": "...", "limit": N, "reldate": D}
               Each query runs independently; results concatenated in
               order, de-duped on pubmed id. Each item may carry its own
               `reldate` (days); otherwise the top-level `reldate` applies.
      q, limit, reldate: single-query shortcut.
      reldate (top-level): default days window for every query that does
               not set its own. Omit or 0 to query all-time (which tends
               to pin the result set on sparse niches).
    """
    queries = params.get("queries")
    default_reldate = params.get("reldate")
    default_reldate_int = int(default_reldate) if default_reldate is not None else None

    if isinstance(queries, list) and queries:
        default_limit = int(params.get("limit", 5))
        query_specs = []
        for item in queries:
            if not isinstance(item, dict):
                continue
            reldate = item.get("reldate", default_reldate)
            reldate_int = int(reldate) if reldate is not None else None
            query_specs.append((
                str(item.get("q", "")),
                int(item.get("limit", default_limit)),
                reldate_int,
            ))
    else:
        query_specs = [(
            str(params.get("q", "astrology")),
            int(params.get("limit", 5)),
            default_reldate_int,
        )]

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q, limit, reldate in query_specs:
        for rec in _fetch_one_query(q, min(limit, 50), reldate):
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            out.append(rec)
    return out
