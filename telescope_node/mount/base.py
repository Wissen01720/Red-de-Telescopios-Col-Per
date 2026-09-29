"""Interfaces de montura.

Hay dos familias:

* :class:`AltAzMount` — el software calcula Alt/Az y hace el seguimiento
  (motores paso a paso propios, simulador).
* :class:`RaDecMount` — la montura acepta RA/Dec y sigue por sí misma
  (monturas GoTo comerciales u OnStep por protocolo LX200).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from astrocore.coords import wrap180


class AltAzMount(ABC):
    kind = "altaz"

    def __init__(self, max_rate_deg_s: float):
        self.max_rate_deg_s = max_rate_deg_s
        # Diferencia entre la posición del cielo y la de los ejes (alineación).
        self.alt_offset = 0.0
        self.az_offset = 0.0

    # --- implementadas por cada driver, en coordenadas de los ejes -------------
    @abstractmethod
    def _axis_position(self) -> tuple[float, float]: ...

    @abstractmethod
    def _axis_slew_to(self, alt: float, az: float, duration_s: float | None) -> None: ...

    @abstractmethod
    def is_moving(self) -> bool: ...

    @abstractmethod
    def stop(self) -> None: ...

    def close(self) -> None:
        pass

    # --- interfaz común en coordenadas del cielo ----------------------------------
    def position(self) -> tuple[float, float]:
        """(altitud, azimut) a los que apunta el telescopio."""
        alt, az = self._axis_position()
        return alt + self.alt_offset, (az + self.az_offset) % 360.0

    def _axis_target(self, alt: float, az: float) -> tuple[float, float]:
        cur_alt, cur_az = self._axis_position()
        target_az = cur_az + wrap180(az - self.az_offset - cur_az)  # camino más corto
        return alt - self.alt_offset, target_az

    def slew_to(self, alt: float, az: float, duration_s: float | None = None) -> None:
        """Mueve a (alt, az). Con ``duration_s`` ajusta la velocidad para llegar en ese tiempo."""
        axis_alt, axis_az = self._axis_target(alt, az)
        self._axis_slew_to(axis_alt, axis_az, duration_s)

    def slew_axes_to(self, alt: float, az: float) -> None:
        """Mueve los ejes a una posición física, sin aplicar la alineación (p. ej. para aparcar)."""
        self._axis_slew_to(alt, az, None)

    def estimate_slew_time(self, alt: float, az: float) -> float:
        cur_alt, cur_az = self.position()
        dist = max(abs(alt - cur_alt), abs(wrap180(az - cur_az)))
        return dist / self.max_rate_deg_s

    def sync(self, alt: float, az: float) -> None:
        """Declara que el telescopio apunta ahora a (alt, az)."""
        axis_alt, axis_az = self._axis_position()
        self.alt_offset = alt - axis_alt
        self.az_offset = wrap180(az - axis_az)

    def reset_axes(self, alt: float, az: float) -> None:
        """Define la posición física de los ejes (p. ej. al arrancar en posición de aparcado)."""
        self.alt_offset = 0.0
        self.az_offset = 0.0
        self._set_axis_position(alt, az)

    def _set_axis_position(self, alt: float, az: float) -> None:
        raise NotImplementedError


class RaDecMount(ABC):
    kind = "radec"
    max_rate_deg_s = 3.0

    @abstractmethod
    def position_radec(self) -> tuple[float, float]:
        """(ra, dec) J2000 a los que apunta la montura."""

    @abstractmethod
    def goto_radec(self, ra: float, dec: float) -> None: ...

    @abstractmethod
    def sync_radec(self, ra: float, dec: float) -> None: ...

    @abstractmethod
    def is_moving(self) -> bool: ...

    @abstractmethod
    def stop(self) -> None: ...

    def set_tracking(self, enabled: bool) -> None:
        pass

    def close(self) -> None:
        pass
