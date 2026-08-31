from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode

from django.db.models import Q
from django.http import HttpRequest


PLATFORM_PRESETS = [
    {
        "key": "all",
        "label": "Tümü",
        "icon": "🌟",
        "badge_bg": "var(--surface)",
        "badge_color": "var(--muted)",
        "filter": Q(),
        "is_all": True,
    },
    {
        "key": "claude",
        "label": "Claude",
        "icon": "🟣",
        "badge_bg": "#d97706",
        "badge_color": "#fff",
        "text_color": "#f59e0b",
        "filter": Q(provider__icontains="anthropic") | Q(provider__icontains="claude") | Q(model__icontains="claude"),
    },
    {
        "key": "gpt",
        "label": "GPT",
        "icon": "🟢",
        "badge_bg": "#16a34a",
        "badge_color": "#fff",
        "text_color": "#22c55e",
        "filter": Q(provider__icontains="openai") | Q(provider__icontains="gpt") | Q(provider__icontains="chatgpt") | Q(model__icontains="gpt") | Q(model__icontains="o1") | Q(model__icontains="o3"),
    },
    {
        "key": "gemini",
        "label": "Gemini",
        "icon": "🔵",
        "badge_bg": "#2563eb",
        "badge_color": "#fff",
        "text_color": "#3b82f6",
        "filter": Q(provider__icontains="gemini") | Q(provider__icontains="google") | Q(model__icontains="gemini"),
    },
    {
        "key": "mcp",
        "label": "MCP Direct",
        "icon": "⚙️",
        "badge_bg": "#64748b",
        "badge_color": "#fff",
        "text_color": "#94a3b8",
        "filter": (Q(provider="mcp") | Q(provider="")) & ~Q(provider__icontains="test") & ~Q(model__icontains="test") & ~Q(model__icontains="claude") & ~Q(model__icontains="gemini") & ~Q(model__icontains="gpt"),
    },
    {
        "key": "test",
        "label": "Test/CI",
        "icon": "🧪",
        "badge_bg": "#ef4444",
        "badge_color": "#fff",
        "text_color": "#ef4444",
        "filter": Q(provider__icontains="test") | Q(provider__icontains="ci") | Q(model__icontains="test") | Q(model__icontains="pytest"),
    },
]

PLATFORM_MAP = {p["key"]: p for p in PLATFORM_PRESETS}


def parse_platform_filter(request: HttpRequest) -> tuple[Q, str, dict[str, Any]]:
    """
    Parses 'platform' query param from request.
    Falls back to 'all' if not provided.
    Returns:
        (Q_object, active_platform_key, platform_context_dict)
    """
    raw_platform = (request.GET.get("platform") or request.COOKIES.get("pulse_platform") or "all").lower().strip()
    preset = PLATFORM_MAP.get(raw_platform, PLATFORM_MAP["all"])
    platform_key = preset["key"]

    platform_q = preset["filter"]

    # Generate URLs preserving other parameters
    current_get = request.GET.copy()
    presets_with_urls = []
    
    for p in PLATFORM_PRESETS:
        get_copy = current_get.copy()
        if p["key"] == "all":
            get_copy.pop("platform", None)
        else:
            get_copy["platform"] = p["key"]
        
        # Reset pagination when changing platform
        get_copy.pop("page", None)
        
        qs = get_copy.urlencode()
        url = f"?{qs}" if qs else "?"
        
        presets_with_urls.append({
            **p,
            "url": url,
            "is_active": p["key"] == platform_key,
        })
        
    context = {
        "active_platform": platform_key,
        "active_platform_label": preset["label"],
        "active_platform_icon": preset["icon"],
        "platform_presets": presets_with_urls,
    }
    return platform_q, platform_key, context
