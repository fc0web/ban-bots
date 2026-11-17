"""No-op source.

Used by boards that track other boards (e.g. the "mokuroku" catalog/monitor)
and have no external data to fetch themselves. Returns an empty list.
"""

from __future__ import annotations

from typing import Any


def fetch(params: dict[str, Any]) -> list[dict[str, Any]]:
    return []
