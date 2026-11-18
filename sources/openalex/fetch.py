"""OpenAlex source bot.

Uses the public OpenAlex REST API (https://api.openalex.org/works). No
API key is required. We deliberately do NOT include a `mailto=` query
parameter (the "polite pool" opt-in) because it would require pinning
an email address to this public repository.

We save only description metadata: id, title, authors, date, category
(primary_topic), license, URL. We do NOT save abstracts, PDFs, or full
text — even when OpenAlex returns an inverted-index abstract in the
same response.

This bot's primary mode is `title_phrases`: all phrases are combined
into ONE API call via OpenAlex's boolean OR syntax in `title.search`
(e.g. `title.search:("a" OR "b" OR ...)`). One call cuts our 429
exposure to 1/N compared with the per-phrase version, and records are
still tagged with `phrase` — reconstructed from the returned title by
substring match — so the page side can see which phrase brought each
item in. Dedup across phrases is by OpenAlex work id.

For single-phrase / concept-id / topic-id style queries the module
still accepts `q`, `concept_ids`, `topic_ids`; those are meant for
other boards and may be dropped if unused.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

API_URL = "https://api.openalex.org/works"

# JST, for stamping the actual time each per-phrase failure occurred.
_JST_TZ = timezone(timedelta(hours=9))


def _jst_now_iso() -> str:
    return datetime.now(_JST_TZ).isoformat(timespec="seconds")


# On 429 Too Many Requests, retry once. If Retry-After is present we
# wait for at least that many seconds; otherwise we fall back to a short
# fixed wait. If the retry also fails, the error bubbles up to the
# caller and is recorded to fetch-errors.jsonl like before.
_RETRY_ATTEMPTS = 1
_RETRY_DEFAULT_WAIT_SEC = 2.0

# Cap on how long we will sit waiting for the retry. OpenAlex sometimes
# tells us to back off for minutes or longer when the common pool is
# saturated; honouring that would hold the daily shard job far past its
# useful window. Over this cap we DO NOT wait: we treat the 429 as a
# failure, record it to fetch-errors.jsonl, and let the rest of the
# board carry on. The cap applies to Retry-After only; the default
# wait (used when Retry-After is absent) is well under it.
_RETRY_MAX_WAIT_SEC = 30.0

# OpenAlex /works caps `per-page` at 200. We use that as a hard ceiling
# on the single combined phrase call.
_PER_PAGE_MAX = 200


def _join_or(ids: list[str]) -> str:
    return "|".join(x.strip() for x in ids if x and str(x).strip())


def _parse_retry_after(value: str | None) -> float | None:
    """OpenAlex returns Retry-After as a decimal number of seconds. The
    RFC also allows an HTTP date, but we have not seen OpenAlex emit one;
    fall back to None so the caller uses its default wait."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


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
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < _RETRY_ATTEMPTS:
                headers = getattr(e, "headers", None)
                retry_after_raw = headers.get("Retry-After") if headers else None
                parsed = _parse_retry_after(retry_after_raw)
                wait = parsed if parsed is not None else _RETRY_DEFAULT_WAIT_SEC
                if wait > _RETRY_MAX_WAIT_SEC:
                    # Server asked us to sleep longer than our cap. Do not
                    # wait, do not retry — fail this attempt so the caller
                    # records the 429 to fetch-errors.jsonl with a reason
                    # that names the over-cap Retry-After.
                    raise urllib.error.HTTPError(
                        url=getattr(e, "url", API_URL),
                        code=e.code,
                        msg=(
                            f"Too Many Requests "
                            f"(Retry-After={retry_after_raw}s "
                            f"> cap {_RETRY_MAX_WAIT_SEC:g}s, not retrying)"
                        ),
                        hdrs=headers,
                        fp=None,
                    )
                time.sleep(wait)
                attempt += 1
                continue
            raise


def _phrase_needles(phrases: list[str]) -> list[tuple[str, set[str]]]:
    """Precompute each phrase's substring-match forms. Hyphenated phrases
    are also matched in their "spaces" and "joined" forms because OpenAlex
    sometimes stores the hyphen variant and sometimes not (e.g. "Wyner-Ziv"
    vs "wyner ziv", "non-termination" vs "nontermination")."""
    out: list[tuple[str, set[str]]] = []
    for p in phrases:
        low = p.lower()
        needles = {low, low.replace("-", " "), low.replace("-", "")}
        needles.discard("")
        out.append((p, needles))
    return out


def _match_phrase(title: str, needles_by_phrase: list[tuple[str, set[str]]]) -> str:
    """Return the first phrase whose any-form appears in `title`, or "".

    Stemming inside OpenAlex can cause a result to match title.search for
    one of our phrases while the title itself doesn't contain the exact
    phrase as a substring; those come back with an empty phrase tag.
    """
    tlow = title.lower()
    for p, needles in needles_by_phrase:
        if any(n in tlow for n in needles):
            return p
    return ""


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
      title_phrases:   list of phrases, combined into ONE call via
                       `title.search:("p1" OR "p2" OR ...)`. Each returned
                       record is tagged with the first of the given phrases
                       whose text appears in the title (case-insensitive,
                       hyphen-insensitive). A record OpenAlex matched via
                       stemming whose title shares no substring with any
                       phrase gets an empty `phrase` tag.
      limit_per_phrase: shapes the combined per-page as
                       `min(limit_per_phrase * len(phrases), 200)`; default
                       10 per phrase, OpenAlex's own per-page max is 200.
      reldate:         int days. If > 0, the title-search is narrowed to
                       `from_publication_date` = today - reldate.

    Fallback modes (for other boards):
      q:               free-text search (OpenAlex `search=`).
      concept_ids:     list of OpenAlex concept IDs (OR'd); legacy taxonomy.
      topic_ids:       list of OpenAlex topic IDs (OR'd); current taxonomy.
      limit:           max records for the fallback modes (default 25, cap 50).

    Returns {"records": [...], "raw_total": N, "after_filter": M, "errors": [...]}:
      raw_total    — OpenAlex's reported hit-count for the combined query
      after_filter — count of unique records actually returned
      errors       — one record per failed call (429 after cap, etc.).
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
    errors: list[dict[str, str]] = []

    if phrases:
        limit_per = min(int(params.get("limit_per_phrase", 10)), 50)
        # One combined call: title.search:("a" OR "b" OR ...). Cuts our 429
        # exposure from N to 1 and keeps the shard well inside its window
        # even when OpenAlex's common pool is busy. The per-page cap keeps
        # rough parity with the old (limit_per_phrase * N) ceiling, under
        # OpenAlex's /works per-page max.
        inner_or = " OR ".join(f'"{p}"' for p in phrases)
        filter_str = f"title.search:({inner_or})"
        if since_filter:
            filter_str = f"{filter_str},{since_filter}"
        combined_limit = min(limit_per * len(phrases), _PER_PAGE_MAX)
        try:
            data = _call(filter_str, combined_limit)
        except Exception as e:
            # Single call failing means no OpenAlex records this run; the
            # board's other sources still carry on. Record the failure the
            # same way we used to for a per-phrase miss, with phrase=""
            # since we no longer run phrase-by-phrase.
            errors.append({
                "time": _jst_now_iso(),
                "source": "openalex",
                "phrase": "",
                "error": f"{type(e).__name__}: {str(e)[:120]}",
            })
            return {
                "records": out,
                "raw_total": raw_total,
                "after_filter": len(out),
                "errors": errors,
            }
        raw_total = int((data.get("meta") or {}).get("count") or 0)
        needles_by_phrase = _phrase_needles(phrases)
        for work in (data.get("results") or []):
            title = str(work.get("title") or work.get("display_name") or "")
            matched = _match_phrase(title, needles_by_phrase)
            rec = _record_from_work(work, matched)
            rid = rec["id"]
            if not rid or rid in seen:
                continue
            seen.add(rid)
            out.append(rec)
        return {
            "records": out,
            "raw_total": raw_total,
            "after_filter": len(out),
            "errors": errors,
        }

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
