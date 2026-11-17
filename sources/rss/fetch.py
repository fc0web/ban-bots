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

import re
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

ATOM_NS = "{http://www.w3.org/2005/Atom}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
RSS10_NS = "{http://purl.org/rss/1.0/}"
RDF_NS = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"


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


_XML_DECL_RE = re.compile(rb'<\?xml[^>]*encoding=["\']([^"\']+)["\'][^>]*\?>', re.I)
_XML_DECL_STRIP_RE = re.compile(r'^\s*<\?xml[^>]*\?>\s*', re.I)


def _parse_xml(body: bytes) -> ET.Element:
    """Parse XML bytes, tolerating Shift_JIS / EUC-JP declarations that
    Python's stdlib XML parser refuses to accept directly. We sniff the
    declared encoding, decode to a str, strip the declaration, and feed
    the str to ElementTree.
    """
    m = _XML_DECL_RE.search(body[:256])
    encoding = m.group(1).decode("ascii", "replace").lower() if m else "utf-8"
    try:
        text = body.decode(encoding, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    text = _XML_DECL_STRIP_RE.sub("", text, count=1)
    return ET.fromstring(text)


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


def _parse_rss2(root: ET.Element, feed_url: str, inc_kws: list[str], exc_kws: list[str]) -> tuple[list[dict[str, Any]], int]:
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
        text = f"{title}\n{desc}"
        if not _matches(text, inc_kws):
            continue
        if exc_kws and _matches(text, exc_kws):
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


def _parse_rdf(root: ET.Element, feed_url: str, inc_kws: list[str], exc_kws: list[str]) -> tuple[list[dict[str, Any]], int]:
    """Parse RSS 1.0 / RDF feeds. Items are direct children of rdf:RDF,
    using the http://purl.org/rss/1.0/ namespace. dc:date, dc:creator are
    typical. Many Japanese gov sites use this form (e.g. mhlw.go.jp,
    cao.go.jp, soumu.go.jp).
    """
    out: list[dict[str, Any]] = []
    raw = 0
    for item in root.findall(f"{RSS10_NS}item"):
        raw += 1
        title = (item.findtext(f"{RSS10_NS}title") or "").strip()
        link = (item.findtext(f"{RSS10_NS}link") or "").strip()
        desc = (item.findtext(f"{RSS10_NS}description") or "").strip()
        pub = _text(item, f"{DC_NS}date")
        author = _text(item, f"{DC_NS}creator")
        text = f"{title}\n{desc}"
        if not _matches(text, inc_kws):
            continue
        if exc_kws and _matches(text, exc_kws):
            continue
        rid = item.get(f"{RDF_NS}about") or link
        out.append({
            "source": "rss",
            "id": rid or f"{feed_url}#{raw}",
            "title": " ".join(title.split()),
            "authors": [author] if author else [],
            "date": pub,
            "category": "",
            "license": "",
            "url": link,
            "feed": feed_url,
            "mark": "未測",
        })
    return out, raw


def _parse_atom(root: ET.Element, feed_url: str, inc_kws: list[str], exc_kws: list[str]) -> tuple[list[dict[str, Any]], int]:
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
        text = f"{title}\n{summary}"
        if not _matches(text, inc_kws):
            continue
        if exc_kws and _matches(text, exc_kws):
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
      exclude_keywords: list of strings; if non-empty, drop items whose
                        title or description/summary contains any of them.
                        Default is [] (= no exclusion). Applied AFTER
                        include_keywords, so a kept-by-include item that
                        also contains an exclude term is dropped.

    Order (applied per feed):
      1. parse all items in the feed (raw_total += N)
      2. drop items whose title/description does not match include_keywords
      3. drop items whose title/description matches any exclude_keywords
         (filter-pass count = items left after both steps)
      4. truncate the filtered list to limit_per_feed.
    This is the order the owner requires: filter THEN limit, so a hot
    keyword hit near the bottom of a large feed is not thrown away by a
    small limit.

    Return:
      {"records": [...], "raw_total": N, "after_filter": M}
      - raw_total: total items in the feeds (pre-filter)
      - after_filter: items that passed BOTH include and exclude filters
        (PRE-limit). The records list is further bounded by
        limit_per_feed, so len(records) <= after_filter.
    """
    import sys
    feeds = params.get("feeds") or []
    limit = min(int(params.get("limit_per_feed", 10)), 50)
    inc_kws = [str(k) for k in (params.get("include_keywords") or []) if str(k).strip()]
    exc_kws = [str(k) for k in (params.get("exclude_keywords") or []) if str(k).strip()]
    out: list[dict[str, Any]] = []
    raw_total = 0
    filter_passed_total = 0
    errors: list[str] = []
    for url in feeds:
        try:
            body = _fetch_bytes(url)
            root = _parse_xml(body)
            tag = root.tag.lower()
            if tag.endswith("}feed") or tag == "feed":
                items, raw = _parse_atom(root, url, inc_kws, exc_kws)
            elif tag.endswith("}rdf") or tag == "rdf":
                items, raw = _parse_rdf(root, url, inc_kws, exc_kws)
            else:
                items, raw = _parse_rss2(root, url, inc_kws, exc_kws)
            raw_total += raw
            filter_passed_total += len(items)  # pre-limit count
            out.extend(items[:limit])          # apply limit AFTER filter
        except Exception as e:
            errors.append(f"{url}:{type(e).__name__}:{str(e)[:80]}")
    for err in errors:
        print(f"rss: feed failed: {err}", file=sys.stderr)
    if feeds and not out and errors and raw_total == 0:
        raise RuntimeError("; ".join(errors))
    return {"records": out, "raw_total": raw_total, "after_filter": filter_passed_total}
