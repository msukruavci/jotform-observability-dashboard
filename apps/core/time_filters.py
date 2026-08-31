from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from django.http import HttpRequest
from django.utils import timezone


TIME_PRESETS = [
    {"key": "1h", "label": "Son 1 Saat", "short_label": "1s", "hours": 1},
    {"key": "6h", "label": "Son 6 Saat", "short_label": "6s", "hours": 6},
    {"key": "24h", "label": "Son 24 Saat", "short_label": "24s", "hours": 24},
    {"key": "3d", "label": "Son 3 Gün", "short_label": "3g", "days": 3},
    {"key": "7d", "label": "Son 7 Gün", "short_label": "7g", "days": 7},
    {"key": "30d", "label": "Son 30 Gün", "short_label": "30g", "days": 30},
    {"key": "all", "label": "Tüm Zamanlar", "short_label": "Tümü", "all": True},
]

PRESET_MAP = {p["key"]: p for p in TIME_PRESETS}


def parse_time_filter(request: HttpRequest) -> tuple[datetime | None, str, dict[str, Any]]:
    """
    Parses 'range' query param from request (e.g. ?range=24h, ?range=7d).
    Falls back to 'all' if not provided or invalid.
    Returns:
        (since_datetime, active_range_key, time_filter_context)
    """
    now = timezone.now()
    raw_range = (request.GET.get("range") or request.COOKIES.get("pulse_time_range") or "all").lower().strip()
    preset = PRESET_MAP.get(raw_range, PRESET_MAP["all"])
    range_key = preset["key"]

    since: datetime | None = None
    if preset.get("hours"):
        since = now - timedelta(hours=preset["hours"])
    elif preset.get("days"):
        since = now - timedelta(days=preset["days"])
    else:
        since = None

    # Helper to generate preset URLs preserving other GET parameters
    current_get = request.GET.copy()

    presets_with_urls = []
    for p in TIME_PRESETS:
        get_copy = current_get.copy()
        if p["key"] == "all":
            get_copy.pop("range", None)
        else:
            get_copy["range"] = p["key"]
        
        # Reset pagination when changing time range
        get_copy.pop("page", None)

        qs = get_copy.urlencode()
        url = f"?{qs}" if qs else "?"

        presets_with_urls.append({
            **p,
            "url": url,
            "is_active": p["key"] == range_key,
        })

    context = {
        "active_time_range": range_key,
        "active_time_label": preset["label"],
        "active_time_short_label": preset["short_label"],
        "active_time_since": since,
        "time_presets": presets_with_urls,
    }
    return since, range_key, context
