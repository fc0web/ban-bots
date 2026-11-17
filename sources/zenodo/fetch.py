"""Zenodo source bot.

Uses Zenodo's public REST API (https://zenodo.org/api/records). Fetches
recent records matching a search query and returns description-only
metadata. We do NOT save abstracts, file contents, or PDFs.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

API_URL = "https://zenodo.org/api/records"


def fetch(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch metadata records from Zenodo.

    params:
      q:     search string (default "mathematics education")
      limit: max number of records (default 10, cap 50)
    """
    q = params.get("q", "mathematics education")
    limit = min(int(params.get("limit", 10)), 50)

    query = urllib.parse.urlencode({
        "q": q,
        "size": str(limit),
        "sort": "newest",
    })
    url = f"{API_URL}?{query}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ban-bots/1.0 (+https://github.com/fc0web/ban-bots)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()

    data = json.loads(body)
    out: list[dict[str, Any]] = []
    for hit in data.get("hits", {}).get("hits", []):
        md = hit.get("metadata", {}) or {}
        creators = md.get("creators") or []
        authors = [(c.get("name") or "").strip() for c in creators if c.get("name")]
        lic_obj = md.get("license") or {}
        if isinstance(lic_obj, dict):
            lic = lic_obj.get("id") or lic_obj.get("name") or ""
        else:
            lic = str(lic_obj)
        resource_type = md.get("resource_type") or {}
        if isinstance(resource_type, dict):
            category = resource_type.get("title") or resource_type.get("type") or ""
        else:
            category = str(resource_type)
        doi = hit.get("doi") or md.get("doi") or ""
        links = hit.get("links") or {}
        url_html = links.get("self_html") or links.get("html") or (
            f"https://doi.org/{doi}" if doi else ""
        )
        out.append({
            "source": "zenodo",
            "id": str(hit.get("id") or doi or ""),
            "title": " ".join(str(md.get("title") or "").split()),
            "authors": authors,
            "date": md.get("publication_date") or hit.get("created") or "",
            "category": category,
            "license": lic,
            "url": url_html,
        })
    return out
