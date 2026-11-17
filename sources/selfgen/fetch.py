"""Self-generating source.

Boards whose content is produced from a deterministic rule (e.g. "a daily
puzzle made from today's date"). No network access. This is a placeholder
that returns an empty list — real generators get plugged in per-kind when
their boards are enabled.
"""

from __future__ import annotations

from typing import Any


def fetch(params: dict[str, Any]) -> list[dict[str, Any]]:
    kind = params.get("kind", "placeholder")
    if kind == "placeholder":
        return []
    raise NotImplementedError(f"selfgen kind not implemented: {kind}")
