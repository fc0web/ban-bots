"""RSS / Atom source bot.

Fetches one or more RSS 2.0 or Atom feeds and returns description-only
metadata per item: id, title, authors, date, category, license (empty),
URL. Every record is stamped with mark="未測" because this bot does not
evaluate content, it only collects candidates.

We save NO content bodies, summaries, or media in the output record. The
<description>/<summary> element is read only in-memory for the optional
keyword filter (params["include_keywords"]) and then discarded.

Feeds are listed in params["feeds"]; the registry row owns that list.
This bot does not pick feeds on its own.

Return shape: `{"records": [...], "raw_total": N, "after_filter": M}` so
the runner can record both the fetched total and the post-filter count.
"""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

ATOM_NS = "{http://www.w3.org/2005/Atom}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ban-bots/1.0 (+https://github.com/fc0web/ban-bots)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _text(el, path: str) -> str:
    if el is None:
        return ""
    node = el.find(path)
    return (node.text or "").strip() if node is not None and node.text else ""


def _matches(text: str, keywords: list[str]) -> bool:
    """True if `text` contains any keyword (substring, case-insensitive for
    ASCII; Japanese keywords match as-is). Empty keyword list = pass-through.
    """
    if not keywords:
        return True
    lo = text.lower()
    for kw in keywords:
        if not kw:
            continue
        if kw in text or kw.lower() in lo:
            return True
    return False


def _parse_rss2(root: ET.Element, feed_url: str, keywords: list[str]) -> tuple[list[dict[str, Any]], int]:
    out: list[dict[str, Any]] = []
    raw = 0
    for item in root.findall("./channel/item"):
        raw += 1
        title = _text(item, "title")
        link = _text(item, "link")
        guid = _text(item, "guid") or link
        # pubDate is RSS 2.0; dc:date is common on RSS 1.0 / Dublin-Core feeds
        # (e.g. caa.go.jp's news.rss).
        pub = _text(item, "pubDate") or _text(item, f"{DC_NS}date")
        author = _text(item, "author") or _text(item, f"{DC_NS}creator")
        category = _text(item, "category")
        # Description is read only for the filter check; not stored.
        desc = _text(item, "description")
        if not _matches(f"{title}\n{desc}", keywords):
            continue
        out.append({
            "source": "rss",
            "id": guid or f"{feed_url}#{raw}",
            "title": " ".join(title.split()),
            "authors": [author] if author else [],
            "date": pub,
            "category": category,
            "license": "",
            "url": link,
            "feed": feed_url,
            "mark": "未測",
        })
    return out, raw


def _parse_atom(root: ET.Element, feed_url: str, keywords: list[str]) -> tuple[list[dict[str, Any]], int]:
    out: list[dict[str, Any]] = []
    raw = 0
    for entry in root.findall(f"{ATOM_NS}entry"):
        raw += 1
        title = (entry.findtext(f"{ATOM_NS}title") or "").strip()
        atom_id = (entry.findtext(f"{ATOM_NS}id") or "").strip()
        updated = (
            entry.findtext(f"{ATOM_NS}published")
            or entry.findtext(f"{ATOM_NS}updated")
            or ""
        ).strip()
        authors = [
            (a.findtext(f"{ATOM_NS}name") or "").strip()
            for a in entry.findall(f"{ATOM_NS}author")
        ]
        link = ""
        for ln in entry.findall(f"{ATOM_NS}link"):
            if ln.get("rel", "alternate") == "alternate":
                link = ln.get("href", "")
                break
        cat_el = entry.find(f"{ATOM_NS}category")
        category = cat_el.get("term", "") if cat_el is not None else ""
        summary = (entry.findtext(f"{ATOM_NS}summary") or "").strip()
        if not _matches(f"{title}\n{summary}", keywords):
            continue
        out.append({
            "source": "rss",
            "id": atom_id or link or f"{feed_url}#{raw}",
            "title": " ".join(title.split()),
            "authors": [a for a in authors if a],
            "date": updated,
            "category": category,
            "license": "",
            "url": link,
            "feed": feed_url,
            "mark": "未測",
        })
    return out, raw


def fetch(params: dict[str, Any]) -> dict[str, Any]:
    """Fetch items from each feed in params["feeds"].

    params:
      feeds:            list of RSS/Atom feed URLs (default [])
      limit_per_feed:   max items per feed after filter (default 10, cap 50)
      include_keywords: list of strings; if non-empty, keep only items whose
                        title or description/summary contains any keyword.
                        Default is [] (= no filter).

    Return:
      {"records": [...], "raw_total": N, "after_filter": M}
      - raw_total: items in the feeds before the keyword filter
      - after_filter: items that survived the filter (= len(records))
    """
    import sys
    feeds = params.get("feeds") or []
    limit = min(int(params.get("limit_per_feed", 10)), 50)
    keywords = [str(k) for k in (params.get("include_keywords") or []) if str(k).strip()]
    out: list[dict[str, Any]] = []
    raw_total = 0
    errors: list[str] = []
    for url in feeds:
        try:
            body = _fetch_bytes(url)
            root = ET.fromstring(body)
            tag = root.tag.lower()
            if tag.endswith("}feed") or tag == "feed":
                items, raw = _parse_atom(root, url, keywords)
            else:
                items, raw = _parse_rss2(root, url, keywords)
            raw_total += raw
            out.extend(items[:limit])
        except Exception as e:
            errors.append(f"{url}:{type(e).__name__}:{str(e)[:80]}")
    for err in errors:
        print(f"rss: feed failed: {err}", file=sys.stderr)
    if feeds and not out and errors and raw_total == 0:
        # Every feed failed and nothing came through — surface the error.
        raise RuntimeError("; ".join(errors))
    return {"records": out, "raw_total": raw_total, "after_filter": len(out)}
