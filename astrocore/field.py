"""Geometría del campo de visión de una cámara: cielo <-> píxeles.

La orientación de la imagen se describe con ``rotation_deg``: el ángulo de
posición (medido de Norte hacia Este) de la dirección "arriba" de la imagen.
Sin espejo (``flip_x=False``) la imagen se ve como el cielo a ojo: con el
Norte arriba, el Este queda a la izquierda.

En una montura altacimutal con la cámara alineada al eje de altura, "arriba"
apunta al cénit y ``rotation_deg`` = ángulo paraláctico + ajuste de montaje.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .coords import deproject, project


def fov_from_optics(focal_length_mm: float, sensor_mm: float) -> float:
    """Campo de visión (grados) a partir de la focal y el tamaño del sensor."""
    return math.degrees(2.0 * math.atan(sensor_mm / (2.0 * focal_length_mm)))


@dataclass(frozen=True)
class FieldGeometry:
    ra: float               # centro del campo, J2000 (grados)
    dec: float
    fov_w: float            # ancho del campo (grados)
    fov_h: float            # alto del campo (grados)
    rotation_deg: float = 0.0
    flip_x: bool = False
    width: int = 1          # píxeles (1x1 => coordenadas normalizadas 0..1)
    height: int = 1

    @property
    def scale_x(self) -> float:
        return self.fov_w / self.width

    @property
    def scale_y(self) -> float:
        return self.fov_h / self.height

    @property
    def radius(self) -> float:
        """Radio del círculo que contiene todo el campo (media diagonal)."""
        return math.hypot(self.fov_w, self.fov_h) / 2.0

    def _axes(self) -> tuple[tuple[float, float], tuple[float, float]]:
        r = math.radians(self.rotation_deg)
        up = (math.sin(r), math.cos(r))          # (Este, Norte)
        right = (-math.cos(r), math.sin(r))
        if self.flip_x:
            right = (-right[0], -right[1])
        return up, right

    def to_pixel(self, ra: float, dec: float) -> tuple[float, float] | None:
        """(ra, dec) -> (x, y) en píxeles (y crece hacia abajo). None si detrás."""
        std = project(ra, dec, self.ra, self.dec)
        if std is None:
            return None
        xi, eta = std
        up, right = self._axes()
        dx = (xi * right[0] + eta * right[1]) / self.scale_x
        dy = (xi * up[0] + eta * up[1]) / self.scale_y
        return self.width / 2.0 + dx, self.height / 2.0 - dy

    def to_sky(self, x: float, y: float) -> tuple[float, float]:
        """(x, y) en píxeles -> (ra, dec)."""
        up, right = self._axes()
        a = (x - self.width / 2.0) * self.scale_x
        b = (self.height / 2.0 - y) * self.scale_y
        # (xi, eta) = a*right + b*up  (right y up son ortonormales)
        xi = a * right[0] + b * up[0]
        eta = a * right[1] + b * up[1]
        return deproject(xi, eta, self.ra, self.dec)

    def contains(self, ra: float, dec: float, margin: float = 0.0) -> bool:
        p = self.to_pixel(ra, dec)
        if p is None:
            return False
        mx, my = margin * self.width, margin * self.height
        return -mx <= p[0] <= self.width + mx and -my <= p[1] <= self.height + my

    def corners(self) -> list[tuple[float, float]]:
        """Esquinas del campo en RA/Dec: sup-izq, sup-der, inf-der, inf-izq."""
        w, h = self.width, self.height
        return [self.to_sky(0, 0), self.to_sky(w, 0), self.to_sky(w, h), self.to_sky(0, h)]

    def normalized(self) -> "FieldGeometry":
        return FieldGeometry(self.ra, self.dec, self.fov_w, self.fov_h, self.rotation_deg, self.flip_x, 1, 1)

    def to_dict(self) -> dict:
        return asdict(self)
