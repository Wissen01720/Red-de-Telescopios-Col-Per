"""Montura altacimutal simulada: movimiento lineal a velocidad limitada."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .base import AltAzMount


class SimulatedMount(AltAzMount):
    def __init__(self, max_rate_deg_s: float = 3.0, home_alt: float = 0.0, home_az: float = 0.0,
                 error_alt_deg: float = 0.0, error_az_deg: float = 0.0,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__(max_rate_deg_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._start = (home_alt, home_az)
        self._target = (home_alt, home_az)
        self._t0 = self._t1 = clock()
        # Error que el software desconoce: la cámara simulada ve el cielo "real".
        self.error_alt_deg = error_alt_deg
        self.error_az_deg = error_az_deg

    def _axis_position(self) -> tuple[float, float]:
        with self._lock:
            return self._interpolate(self._clock())

    def _interpolate(self, t: float) -> tuple[float, float]:
        if t >= self._t1:
            return self._target
        f = (t - self._t0) / (self._t1 - self._t0)
        return (self._start[0] + (self._target[0] - self._start[0]) * f,
                self._start[1] + (self._target[1] - self._start[1]) * f)

    def _axis_slew_to(self, alt: float, az: float, duration_s: float | None) -> None:
        with self._lock:
            now = self._clock()
            cur = self._interpolate(now)
            dist = max(abs(alt - cur[0]), abs(az - cur[1]))
            duration = max(duration_s or 0.0, dist / self.max_rate_deg_s)
            self._start, self._target = cur, (alt, az)
            self._t0, self._t1 = now, now + duration

    def _set_axis_position(self, alt: float, az: float) -> None:
        with self._lock:
            self._start = self._target = (alt, az)
            self._t0 = self._t1 = self._clock()

    def is_moving(self) -> bool:
        with self._lock:
            return self._clock() < self._t1

    def stop(self) -> None:
        with self._lock:
            now = self._clock()
            cur = self._interpolate(now)
            self._start = self._target = cur
            self._t0 = self._t1 = now

    def true_position(self) -> tuple[float, float]:
        """Dónde apunta realmente el tubo: ejes + error de montaje (la alineación lo compensa)."""
        alt, az = self._axis_position()
        return alt + self.error_alt_deg, (az + self.error_az_deg) % 360.0
