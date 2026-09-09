from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from django.db import DatabaseError

from apps.core.models import DashboardCache


def stable_signature(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def cache_key_for(key: str, signature_text: str) -> str:
    return f"{key}:{sha256(signature_text.encode('utf-8')).hexdigest()[:24]}"


def get_cached_value(key: str, signature: Any) -> Any | None:
    signature_text = stable_signature(signature)
    physical_key = cache_key_for(key, signature_text)
    try:
        row = DashboardCache.objects.filter(key=physical_key, signature=signature_text).first()
    except DatabaseError:
        return None
    return row.value if row else None


def set_cached_value(key: str, signature: Any, value: Any) -> None:
    signature_text = stable_signature(signature)
    physical_key = cache_key_for(key, signature_text)
    try:
        DashboardCache.objects.update_or_create(
            key=physical_key,
            defaults={"signature": signature_text, "value": value},
        )
    except (DatabaseError, TypeError, ValueError):
        return
