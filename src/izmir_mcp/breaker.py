"""Kaynak başına devre kesici (circuit breaker).

Bir kaynak art arda `breaker_failure_threshold` kez hata verirse devre AÇILIR: `breaker_cooldown_seconds`
boyunca o kaynağa hiç istek atılmaz, `get_json` hemen "kaynak kapalı" hatası döner (Claude beklemeden
açıklama yapabilsin, çökmüş API'ye yük binmesin). Süre dolunca tek bir deneme isteğine izin verilir
(YARI AÇIK): başarılıysa devre kapanır, başarısızsa yeniden açılır.

Durumlar `get_source_status` aracıyla dışarı verilir.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .config import get_settings


class BreakerState(StrEnum):
    CLOSED = "closed"  # normal çalışıyor
    OPEN = "open"  # kaynak kapalı kabul ediliyor, istek atılmıyor
    HALF_OPEN = "half_open"  # bekleme süresi doldu, deneme isteği yapılıyor


def _monotonic() -> float:
    return time.monotonic()  # testlerde monkeypatch ile ileri sarılıyor


@dataclass
class SourceHealth:
    source: str
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None  # monotonic
    trial_in_flight: bool = False
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None

    def retry_in_seconds(self) -> int | None:
        if self.state != BreakerState.OPEN or self.opened_at is None:
            return None
        left = get_settings().breaker_cooldown_seconds - (_monotonic() - self.opened_at)
        return max(0, round(left))


class CircuitBreakers:
    def __init__(self) -> None:
        self._sources: dict[str, SourceHealth] = {}

    def health(self, source: str) -> SourceHealth:
        if source not in self._sources:
            self._sources[source] = SourceHealth(source)
        return self._sources[source]

    def known(self) -> dict[str, SourceHealth]:
        return dict(self._sources)

    def allow(self, source: str) -> bool:
        """Bu kaynağa şimdi istek atılabilir mi? Yarı açık durumda aynı anda tek deneme isteğine izin verir."""
        h = self.health(source)
        if h.state == BreakerState.CLOSED:
            return True
        if h.state == BreakerState.OPEN:
            if h.opened_at is not None and _monotonic() - h.opened_at >= get_settings().breaker_cooldown_seconds:
                h.state = BreakerState.HALF_OPEN
                h.trial_in_flight = False
            else:
                return False
        # HALF_OPEN
        if h.trial_in_flight:
            return False
        h.trial_in_flight = True
        return True

    def record_success(self, source: str) -> None:
        h = self.health(source)
        h.state = BreakerState.CLOSED
        h.consecutive_failures = 0
        h.opened_at = None
        h.trial_in_flight = False
        h.last_success_at = datetime.now(UTC)

    def record_failure(self, source: str, error: str) -> None:
        h = self.health(source)
        h.consecutive_failures += 1
        h.last_failure_at = datetime.now(UTC)
        h.last_error = error
        h.trial_in_flight = False
        if h.state == BreakerState.HALF_OPEN or h.consecutive_failures >= get_settings().breaker_failure_threshold:
            h.state = BreakerState.OPEN
            h.opened_at = _monotonic()

    def release(self, source: str) -> None:
        """Sonucu belirsiz biten deneme (ör. iptal edilen istek): durumu değiştirmeden kilidi bırak."""
        self.health(source).trial_in_flight = False

    def reset(self) -> None:
        self._sources.clear()


breakers = CircuitBreakers()
