"""arXiv source bot.

Uses the public arXiv Atom API (export.arxiv.org). Fetches a recent list
of papers in given categories, optionally narrowed by keyword phrases,
and returns description-only metadata records.

Rate limit (arXiv's policy): at most 1 request every 3 seconds, single
connection, and a User-Agent that identifies the client and gives a way
to reach us. This module makes one request per call and enforces the
3 s spacing per-process; callers must space calls out themselves if they
loop.

We save only description metadata: id, title, authors, date, category,
license, URL. We do NOT save abstracts, PDFs, or full text. The abstract
is not read; keyword filtering is delegated to arXiv's own `all:` field
search, which runs server-side and returns only matching papers.

Order the keyword filter and the limit must run in (per the owner):
  1. arXiv server returns records matching cats AND keywords, newest first.
  2. Client-side `reldate` filter (days) drops anything older than today.
The net count is bounded by `limit` but the limit is applied by arXiv
AFTER it has run the keyword match on its side, so it is truly "filter
then limit".
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

API_URL = "http://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
JST = timezone(timedelta(hours=9))

# Arxiv's rate-limit policy: at most one request every 3 seconds.
# Enforced per-process as a minimum spacing between calls.
_MIN_INTERVAL_SEC = 3.1
_last_call_ts = 0.0


def _rate_limit() -> None:
    global _last_call_ts
    wait = _MIN_INTERVAL_SEC - (time.monotonic() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.monotonic()


def _rec_date(iso: str):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
    except Exception:
        return None


def fetch(params: dict[str, Any]) -> dict[str, Any]:
    """Fetch metadata records from arXiv.

    params (current):
      categories: list of arXiv category codes, e.g. ["cs.GT","cs.AI","math.CO"].
                  Combined with OR on the server.
      keywords:   list of phrases (quoted on the arXiv side) to search in
                  title + abstract via the `all:` field. OR-combined.
                  Empty or missing = no keyword narrowing.
      required_keywords:
                  list of phrases that MUST all appear. Each is AND'd
                  into the query, before the OR'd `keywords` list. Use
                  this for the pattern `(cats) AND all:req AND (OR...)`.
      limit:      max records (default 20, cap 100). Applied by arXiv
                  AFTER its own category+keyword match.
      reldate:    if set, drop records whose published date is older than
                  (today JST - reldate) days. Client-side filter.

    Back-compat (older rows still use these):
      category:   single category string, used when `categories` is absent.

    Returns:
      {"records": [...], "raw_total": N, "after_filter": M}
        raw_total:   records returned by arXiv (pre-reldate, post-limit)
        after_filter: records kept after reldate filter (= len(records))
    """
    cats = params.get("categories")
    if not cats:
        c = params.get("category", "math.HO")
        cats = [c] if c else []
    cats = [str(x).strip() for x in cats if str(x).strip()]
    if not cats:
        return {"records": [], "raw_total": 0, "after_filter": 0}

    kws = [str(k).strip() for k in (params.get("keywords") or []) if str(k).strip()]
    req = [str(k).strip() for k in (params.get("required_keywords") or []) if str(k).strip()]

    def _kw(k: str) -> str:
        return f'all:"{k.replace(chr(34), "")}"'

    cat_q = " OR ".join(f"cat:{c}" for c in cats)
    kw_q = " OR ".join(_kw(k) for k in kws)
    parts = [f"({cat_q})" if len(cats) > 1 else cat_q]
    for r in req:
        parts.append(_kw(r))
    if kw_q:
        parts.append(f"({kw_q})" if len(kws) > 1 else kw_q)
    search_query = " AND ".join(parts)

    limit = min(int(params.get("limit", 20)), 100)
    reldate = params.get("reldate")
    reldate_int = int(reldate) if reldate is not None else None

    query = urllib.parse.urlencode({
        "search_query": search_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(limit),
    })
    url = f"{API_URL}?{query}"

    _rate_limit()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ban-bots/1.0 (+https://github.com/fc0web/ban-bots)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()

    root = ET.fromstring(body)
    recs: list[dict[str, Any]] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        arxiv_id = (entry.findtext(f"{ATOM_NS}id") or "").strip()
        title = (entry.findtext(f"{ATOM_NS}title") or "").strip()
        published = (entry.findtext(f"{ATOM_NS}published") or "").strip()
        authors = [
            (a.findtext(f"{ATOM_NS}name") or "").strip()
            for a in entry.findall(f"{ATOM_NS}author")
        ]
        primary_cat_el = entry.find(f"{ARXIV_NS}primary_category")
        primary_cat = primary_cat_el.get("term") if primary_cat_el is not None else ""
        link_abs = ""
        link_pdf = ""
        for link in entry.findall(f"{ATOM_NS}link"):
            rel = link.get("rel", "")
            typ = link.get("type", "")
            href = link.get("href", "")
            if rel == "alternate" and typ == "text/html":
                link_abs = href
            elif link.get("title") == "pdf":
                link_pdf = href
        recs.append({
            "source": "arxiv",
            "id": arxiv_id,
            "title": " ".join(title.split()),
            "authors": authors,
            "date": published,
            "category": primary_cat,
            "license": "",
            "url": link_abs or arxiv_id,
            "pdf_url": link_pdf,
        })

    raw_total = len(recs)

    if reldate_int and reldate_int > 0:
        cutoff = (datetime.now(JST) - timedelta(days=reldate_int)).date()
        kept = []
        for r in recs:
            d = _rec_date(r.get("date", ""))
            if d is None or d >= cutoff:
                kept.append(r)
        recs = kept

    return {"records": recs, "raw_total": raw_total, "after_filter": len(recs)}
