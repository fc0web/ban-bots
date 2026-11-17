"""OpenAlex source bot.

Uses the public OpenAlex REST API (https://api.openalex.org/works). No
API key is required. We deliberately do NOT include a `mailto=` query
parameter (the "polite pool" opt-in) because it would require pinning
an email address to this public repository.

We save only description metadata: id, title, authors, date, category
(primary_topic), license, URL. We do NOT save abstracts, PDFs, or full
text — even when OpenAlex returns an inverted-index abstract in the
same response.

This bot's primary mode is `title_phrases`: one API call per phrase,
each capped at `limit_per_phrase` records (so one broad phrase can't
push narrower phrases out). Records are tagged with `phrase` so the
page side can see which phrase brought each item in. Dedup across
phrases is by OpenAlex work id; later phrases lose.

For single-phrase / concept-id / topic-id style queries the module
still accepts `q`, `concept_ids`, `topic_ids`; those are meant for
other boards and may be dropped if unused.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any

API_URL = "https://api.openalex.org/works"

# Be polite between consecutive calls (OpenAlex common-pool limit is
# ~10 req/s). The multi-phrase mode makes N calls per fetch; N stays
# small (7 for senkou-kenkyuu-kanshi) so a short fixed sleep is fine.
_CALL_INTERVAL_SEC = 0.2


def _join_or(ids: list[str]) -> str:
    return "|".join(x.strip() for x in ids if x and str(x).strip())


def _call(filter_str: str, limit: int) -> dict[str, Any]:
    qs = urllib.parse.urlencode({
        "filter": filter_str,
        "per-page": str(limit),
        "sort": "publication_date:desc",
    })
    url = f"{API_URL}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ban-bots/1.0 (+https://github.com/fc0web/ban-bots)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _record_from_work(work: dict[str, Any], phrase: str | None) -> dict[str, Any]:
    oa_id = str(work.get("id") or "")
    title = " ".join(str(work.get("title") or work.get("display_name") or "").split())
    authors: list[str] = []
    for a in (work.get("authorships") or []):
        au = (a.get("author") or {}).get("display_name")
        if au:
            authors.append(str(au))
    primary = work.get("primary_topic") or {}
    category = str(primary.get("display_name") or "")
    pl = work.get("primary_location") or {}
    lic_obj = pl.get("license") or ""
    lic = str(lic_obj) if lic_obj else ""
    doi = work.get("doi") or ""
    link_url = pl.get("landing_page_url") or ""
    if not link_url and doi:
        link_url = doi if str(doi).startswith("http") else f"https://doi.org/{doi}"
    if not link_url:
        link_url = oa_id
    rec: dict[str, Any] = {
        "source": "openalex",
        "id": oa_id or link_url,
        "title": title,
        "authors": authors,
        "date": work.get("publication_date") or "",
        "category": category,
        "license": lic,
        "url": link_url,
    }
    if phrase:
        rec["phrase"] = phrase
    return rec


def fetch(params: dict[str, Any]) -> dict[str, Any]:
    """Fetch metadata records from OpenAlex.

    Primary mode — multi-phrase title search:
      title_phrases:   list of phrases to pass to OpenAlex's `title.search`
                       filter one by one. Each phrase is fetched separately
                       and capped at `limit_per_phrase` records; results are
                       unioned and deduped across phrases. Records are
                       tagged with `phrase` to show which phrase matched.
      limit_per_phrase: max records per phrase (default 10, cap 50).
      reldate:         int days. If > 0, each title-search is narrowed to
                       `from_publication_date` = today - reldate.

    Fallback modes (for other boards):
      q:               free-text search (OpenAlex `search=`).
      concept_ids:     list of OpenAlex concept IDs (OR'd); legacy taxonomy.
      topic_ids:       list of OpenAlex topic IDs (OR'd); current taxonomy.
      limit:           max records for the fallback modes (default 25, cap 50).

    Returns {"records": [...], "raw_total": N, "after_filter": M}:
      raw_total    — sum of OpenAlex's reported hit-counts across sub-calls
      after_filter — count of unique records actually returned
    `records` is capped by limit_per_phrase × number of phrases, then
    deduped by OpenAlex work id.
    """
    phrases = [str(p).strip() for p in (params.get("title_phrases") or []) if str(p).strip()]
    reldate = int(params.get("reldate") or 0)

    since_filter = ""
    if reldate > 0:
        since = (date.today() - timedelta(days=reldate)).isoformat()
        since_filter = f"from_publication_date:{since}"

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_total = 0

    if phrases:
        limit_per = min(int(params.get("limit_per_phrase", 10)), 50)
        for i, phrase in enumerate(phrases):
            if i > 0:
                time.sleep(_CALL_INTERVAL_SEC)
            inner = f'title.search:"{phrase}"'
            if since_filter:
                inner = f"{inner},{since_filter}"
            try:
                data = _call(inner, limit_per)
            except Exception:
                # one phrase failing should not kill the others
                continue
            raw_total += int((data.get("meta") or {}).get("count") or 0)
            for work in (data.get("results") or []):
                rec = _record_from_work(work, phrase)
                rid = rec["id"]
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                out.append(rec)
        return {"records": out, "raw_total": raw_total, "after_filter": len(out)}

    # Fallback single-call modes.
    q = str(params.get("q") or "").strip()
    concept_ids = list(params.get("concept_ids") or [])
    topic_ids = list(params.get("topic_ids") or [])
    limit = min(int(params.get("limit", 25)), 50)

    filters: list[str] = []
    if concept_ids:
        filters.append("concepts.id:" + _join_or(concept_ids))
    if topic_ids:
        filters.append("topics.id:" + _join_or(topic_ids))
    if since_filter:
        filters.append(since_filter)
    filter_str = ",".join(filters) if filters else ""

    qs = {
        "per-page": str(limit),
        "sort": "publication_date:desc",
    }
    if filter_str:
        qs["filter"] = filter_str
    if q:
        qs["search"] = q
    url = f"{API_URL}?{urllib.parse.urlencode(qs)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ban-bots/1.0 (+https://github.com/fc0web/ban-bots)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    raw_total = int((data.get("meta") or {}).get("count") or 0)
    for work in data.get("results") or []:
        out.append(_record_from_work(work, None))
    return {"records": out, "raw_total": raw_total, "after_filter": len(out)}
