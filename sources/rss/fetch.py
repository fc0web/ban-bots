"""RSS / Atom source bot.

Fetches one or more RSS 2.0 or Atom feeds and returns description-only
metadata per item: id, title, authors, date, category, license (empty),
URL. Every record is stamped with mark="未測" because this bot does not
evaluate content, it only collects candidates.

We save NO content bodies, no summaries, no media.

Feeds are listed in params["feeds"]; the registry row owns that list.
This bot does not pick feeds on its own.
"""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

ATOM_NS = "{http://www.w3.org/2005/Atom}"


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


def _parse_rss2(root: ET.Element, feed_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        title = _text(item, "title")
        link = _text(item, "link")
        guid = _text(item, "guid") or link
        pub = _text(item, "pubDate")
        author = _text(item, "author") or _text(item, "{http://purl.org/dc/elements/1.1/}creator")
        category = _text(item, "category")
        out.append({
            "source": "rss",
            "id": guid or f"{feed_url}#{len(out)}",
            "title": " ".join(title.split()),
            "authors": [author] if author else [],
            "date": pub,
            "category": category,
            "license": "",
            "url": link,
            "feed": feed_url,
            "mark": "未測",
        })
    return out


def _parse_atom(root: ET.Element, feed_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
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
        out.append({
            "source": "rss",
            "id": atom_id or link or f"{feed_url}#{len(out)}",
            "title": " ".join(title.split()),
            "authors": [a for a in authors if a],
            "date": updated,
            "category": category,
            "license": "",
            "url": link,
            "feed": feed_url,
            "mark": "未測",
        })
    return out


def fetch(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch items from each feed in params["feeds"].

    params:
      feeds:            list of RSS/Atom feed URLs (default [])
      limit_per_feed:   max items per feed (default 10, cap 50)
    """
    import sys
    feeds = params.get("feeds") or []
    limit = min(int(params.get("limit_per_feed", 10)), 50)
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for url in feeds:
        try:
            body = _fetch_bytes(url)
            root = ET.fromstring(body)
            tag = root.tag.lower()
            if tag.endswith("}feed") or tag == "feed":
                items = _parse_atom(root, url)
            else:
                items = _parse_rss2(root, url)
            out.extend(items[:limit])
        except Exception as e:
            errors.append(f"{url}:{type(e).__name__}:{str(e)[:80]}")
    for err in errors:
        print(f"rss: feed failed: {err}", file=sys.stderr)
    # Only raise if we were given feeds and none of them worked.
    if feeds and not out and errors:
        raise RuntimeError("; ".join(errors))
    return out
