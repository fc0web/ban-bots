"""arXiv source bot.

Uses the public arXiv Atom API (export.arxiv.org). Fetches a recent list of
papers in a given category and returns description-only metadata records.

Rate limit (arXiv's policy): at most 1 request every 3 seconds, single
connection. This module makes one request per call, so callers must space
calls out themselves if they loop.

We save only description metadata: id, title, authors, date, category,
license, URL. We do NOT save abstracts, PDFs, or full text.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

API_URL = "http://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

# Arxiv's rate-limit policy: at most one request every 3 seconds.
# Enforced per-process as a minimum spacing between calls.
_MIN_INTERVAL_SEC = 3.1
_last_call_ts = 0.0


def _rate_limit() -> None:
    global _last_call_ts
    now = time.monotonic()
    wait = _MIN_INTERVAL_SEC - (now - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.monotonic()


def fetch(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch metadata records from arXiv.

    params:
      category: arXiv category (e.g. "math.HO")
      limit:    max number of records (default 20, cap 100)
    """
    category = params.get("category", "math.HO")
    limit = min(int(params.get("limit", 20)), 100)

    query = urllib.parse.urlencode({
        "search_query": f"cat:{category}",
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
    out: list[dict[str, Any]] = []
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
        out.append({
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
    return out
