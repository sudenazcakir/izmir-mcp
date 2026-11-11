"""Basit süreç içi TTL önbellek.

2. haftada Bahar bunu aynı arayüzle Upstash Redis'e taşıyacak (get/set + son iyi değer).
"""

import time
from typing import Any


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            return None
        return value

    def get_stale(self, key: str) -> Any | None:
        """Süresi dolmuş olsa bile son iyi değer (kaynak çökünce kullanılır)."""
        item = self._data.get(key)
        return item[1] if item else None

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._data[key] = (time.monotonic() + ttl_seconds, value)

    def clear(self) -> None:
        self._data.clear()


cache = TTLCache()
