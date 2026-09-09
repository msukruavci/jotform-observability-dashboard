from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from django.http import HttpRequest
from django.utils import timezone


TIME_PRESETS = [
    {"key": "1h", "label": "Last 1 Hour", "short_label": "1h", "hours": 1},
    {"key": "6h", "label": "Last 6 Hours", "short_label": "6h", "hours": 6},
    {"key": "24h", "label": "Last 24 Hours", "short_label": "24h", "hours": 24},
    {"key": "3d", "label": "Last 3 Days", "short_label": "3d", "days": 3},
    {"key": "7d", "label": "Last 7 Days", "short_label": "7d", "days": 7},
    {"key": "30d", "label": "Last 30 Days", "short_label": "30d", "days": 30},
    {"key": "all", "label": "All Time", "short_label": "All", "all": True},
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
