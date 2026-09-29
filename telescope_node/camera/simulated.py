"""Cámara simulada: renderiza el cielo que "vería" el telescopio.

Usa el catálogo real (estrellas, objetos de cielo profundo, planetas y la
Luna con su fase), añade estrellas débiles procedurales (fijas en el cielo y
más densas cerca de la Vía Láctea), brillo del cielo según el Sol, la Luna y
la contaminación lumínica, nubes que se desplazan, ruido y píxeles calientes.
Sirve para desarrollar y probar todo el sistema sin hardware.
"""

from __future__ import annotations

import math
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from astrocore.catalog import Catalog, default_catalog
from astrocore.coords import Site, angular_separation, galactic_latitude, offset_by, radec_to_altaz
from astrocore.ephemeris import moon_phase
from astrocore.field import FieldGeometry

from .base import Camera

# Flujo (ADU·px) de una estrella de magnitud 0 con exposición 200 ms y ganancia 4.
_F0 = 480_000.0
_DSO_BOOST = 150.0
_CELL_DEG = 0.5
_FAINT_MAG_RANGE = (6.5, 12.0)


@dataclass
class FieldPose:
    """Dónde apunta realmente el telescopio en el instante de la captura."""

    geometry: FieldGeometry   # en píxeles del sensor
    jd: float
    site: Site
    moving: bool = False


def _mag_to_flux(mag: float, exposure_scale: float) -> float:
    return _F0 * 10.0 ** (-0.4 * mag) * exposure_scale


class SimulatedCamera(Camera):
    def __init__(self, width: int, height: int, exposure_ms: float, gain: float,
                 pose_provider: Callable[[], FieldPose], catalog: Catalog | None = None,
                 clouds: float = 0.0, seeing_px: float = 1.3, light_pollution: float = 0.2,
                 seed: int = 0, realtime: bool = True, force_night: bool = False):
        super().__init__(width, height, exposure_ms, gain)
        self._pose = pose_provider
        self._catalog = catalog or default_catalog()
        self.clouds = clouds
        self.seeing_px = seeing_px
        self.light_pollution = light_pollution
        self._seed = seed
        self._rng = np.random.default_rng(seed + 12345)
        self._realtime = realtime
        self.force_night = force_night
        self._cells: OrderedDict[tuple[int, int], np.ndarray] = OrderedDict()
        self._cloud_field: np.ndarray | None = None
        hot = np.random.default_rng(seed + 99)
        self._hot_pixels = (hot.integers(0, height, 25), hot.integers(0, width, 25))

    # ------------------------------------------------------------------ captura
    def capture(self) -> np.ndarray:
        t_start = time.monotonic()
        img = self.render(self._pose())
        if self._realtime:
            remaining = min(self.exposure_ms / 1000.0, 2.0) - (time.monotonic() - t_start)
            if remaining > 0:
                time.sleep(remaining)
        return img

    def render(self, pose: FieldPose) -> np.ndarray:
        g = pose.geometry
        h, w = g.height, g.width
        scale = (self.exposure_ms / 200.0) * (self.gain / 4.0)
        sky = self._sky_background(pose, scale)
        img = np.full((h, w), sky, dtype=np.float32)
        # Gradiente: más brillo hacia el horizonte (abajo en montura altacimutal).
        img *= np.linspace(0.9, 1.15, h, dtype=np.float32)[:, None]

        cover = self._cloud_cover(h, w)
        stars = self._star_list(g)
        self._draw_stars(img, stars, g, scale)
        self._draw_dsos(img, pose, scale)
        self._draw_bodies(img, pose, scale)
        if cover is not None:
            # Las nubes apagan la luz de los astros y reflejan la del suelo.
            img = img * (1.0 - 0.95 * cover) + cover * (25.0 + 120.0 * self.light_pollution) * scale

        noise_sigma = np.sqrt(4.0 + 0.3 * np.clip(img, 0, None))
        img += self._rng.standard_normal(img.shape, dtype=np.float32) * noise_sigma
        img[self._hot_pixels] = 220.0
        return np.clip(img, 0, 255).astype(np.uint8)

    # ------------------------------------------------------------------ fondo de cielo
    def _sky_background(self, pose: FieldPose, scale: float) -> float:
        sun = self._catalog.body_object("sun", pose.jd)
        sun_alt, _ = radec_to_altaz(sun.ra, sun.dec, pose.site, pose.jd)
        base = 8.0 + 40.0 * self.light_pollution
        if sun_alt > -18.0 and not self.force_night:
            # Crepúsculo: el brillo crece exponencialmente hasta el día.
            base += 400.0 * 10.0 ** ((min(sun_alt, 5.0) - 5.0) / 6.0)
        moon = self._catalog.body_object("moon", pose.jd, pose.site)
        moon_alt, _ = radec_to_altaz(moon.ra, moon.dec, pose.site, pose.jd)
        if moon_alt > 0:
            sep = angular_separation(moon.ra, moon.dec, pose.geometry.ra, pose.geometry.dec)
            illum = moon_phase(pose.jd)["illuminated"]
            base += 60.0 * illum * math.sin(math.radians(moon_alt)) * math.exp(-sep / 35.0)
        return base * scale

    @staticmethod
    def _make_cloud_field(seed: int, shape: tuple[int, int] = (96, 160)) -> np.ndarray:
        """Ruido suave y periódico (filtrado en Fourier) para simular nubes."""
        noise = np.random.default_rng(seed + 7).random(shape)
        fy = np.fft.fftfreq(shape[0])[:, None]
        fx = np.fft.fftfreq(shape[1])[None, :]
        field = np.fft.ifft2(np.fft.fft2(noise) * np.exp(-(fx ** 2 + fy ** 2) * 2 * (np.pi * 3.0) ** 2)).real
        field += 0.35 * np.fft.ifft2(np.fft.fft2(noise.T.copy()).T * np.exp(-(fx ** 2 + fy ** 2) * 2 * (np.pi * 1.2) ** 2)).real
        return ((field - field.min()) / (field.max() - field.min())).astype(np.float32)

    def _cloud_cover(self, h: int, w: int) -> np.ndarray | None:
        if self.clouds <= 0:
            return None
        if self._cloud_field is None:
            self._cloud_field = self._make_cloud_field(self._seed)
        field = self._cloud_field
        shift = int(time.time() * 0.4) % field.shape[1]   # las nubes se desplazan
        window = np.roll(field, -shift, axis=1)[:24, :32]
        small = cv2.resize(window, (max(1, w // 8), max(1, h // 8)), interpolation=cv2.INTER_CUBIC)
        threshold = float(np.quantile(small, min(self.clouds, 0.999)))
        cover = np.clip((threshold - small) / 0.1 + 0.5, 0.0, 1.0)
        return cv2.resize(cover, (w, h), interpolation=cv2.INTER_LINEAR)

    # ------------------------------------------------------------------ estrellas
    def _faint_cell(self, j: int, i: int, n_ra: int) -> np.ndarray:
        key = (j, i)
        cached = self._cells.get(key)
        if cached is not None:
            self._cells.move_to_end(key)
            return cached
        dec_lo = -90.0 + j * _CELL_DEG
        ra_lo, ra_w = i * 360.0 / n_ra, 360.0 / n_ra
        dec_c = dec_lo + _CELL_DEG / 2
        area = _CELL_DEG * ra_w * math.cos(math.radians(dec_c))
        b = galactic_latitude(ra_lo + ra_w / 2, dec_c)
        density = 60.0 * (0.4 + 2.4 * math.exp(-abs(b) / 12.0))
        rng = np.random.default_rng((self._seed, j + 1000, i))
        n = rng.poisson(density * area)
        ra = ra_lo + rng.random(n) * ra_w
        dec = dec_lo + rng.random(n) * _CELL_DEG
        lo, hi = (10 ** (0.35 * m) for m in _FAINT_MAG_RANGE)
        mag = np.log10(lo + rng.random(n) * (hi - lo)) / 0.35
        cell = np.stack([ra, dec, mag], axis=1)
        self._cells[key] = cell
        if len(self._cells) > 400:
            self._cells.popitem(last=False)
        return cell

    def _star_list(self, g: FieldGeometry) -> np.ndarray:
        r = g.radius + 0.05
        stars, _ = self._catalog.cone(g.ra, g.dec, r)
        rows = [np.array([[s.ra, s.dec, s.mag] for s in stars]).reshape(-1, 3)]
        dec_min, dec_max = max(-90.0, g.dec - r), min(90.0, g.dec + r)
        near_pole = abs(g.dec) + r >= 89.0
        for j in range(int((dec_min + 90) // _CELL_DEG), int((dec_max + 90) // _CELL_DEG) + 1):
            band_lo = -90.0 + j * _CELL_DEG
            n_ra = max(1, round(360.0 * math.cos(math.radians(band_lo + _CELL_DEG / 2)) / _CELL_DEG))
            cell_w = 360.0 / n_ra
            max_abs = max(abs(band_lo), abs(band_lo + _CELL_DEG))
            half = 180.0 if near_pole or max_abs >= 89.9 else min(180.0, r / math.cos(math.radians(max_abs)))
            first = math.floor(((g.ra - half) % 360.0) / cell_w)
            count = min(n_ra, math.ceil(2 * half / cell_w) + 1)
            for k in range(count):
                rows.append(self._faint_cell(j, (first + k) % n_ra, n_ra))
        return np.concatenate(rows, axis=0)

    def _draw_psf(self, img: np.ndarray, x: float, y: float, flux: float, sigma: float) -> None:
        peak = flux / (2 * math.pi * sigma * sigma)
        if peak < 0.3:
            return
        radius = int(math.ceil(sigma * math.sqrt(2 * math.log(max(peak, 1.0) / 0.3)))) + 1
        h, w = img.shape
        x0, x1 = max(0, int(x) - radius), min(w, int(x) + radius + 1)
        y0, y1 = max(0, int(y) - radius), min(h, int(y) + radius + 1)
        if x0 >= x1 or y0 >= y1:
            return
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        img[y0:y1, x0:x1] += peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma * sigma))

    def _draw_stars(self, img: np.ndarray, stars: np.ndarray, g: FieldGeometry, scale: float) -> None:
        for ra, dec, mag in stars:
            p = g.to_pixel(ra, dec)
            if p is None or not (-20 <= p[0] <= g.width + 20 and -20 <= p[1] <= g.height + 20):
                continue
            self._draw_psf(img, p[0], p[1], _mag_to_flux(mag, scale), self.seeing_px)

    # ------------------------------------------------------------------ cielo profundo
    def _draw_dsos(self, img: np.ndarray, pose: FieldPose, scale: float) -> None:
        g = pose.geometry
        _, dsos = self._catalog.cone(g.ra, g.dec, g.radius + 1.0)
        arcmin_px = 1.0 / (60.0 * g.scale_x)
        for d in dsos:
            if d.mag is None:
                continue
            p = g.to_pixel(d.ra, d.dec)
            size_px = (d.size_arcmin or 1.0) * arcmin_px
            if p is None or not (-size_px <= p[0] <= g.width + size_px and -size_px <= p[1] <= g.height + size_px):
                continue
            if d.type_label.startswith(("Cúmulo abierto", "Asociación", "Cúmulo con")):
                self._draw_open_cluster(img, d, g, scale)
                if not d.type_label.startswith("Cúmulo con"):
                    continue
            # Halo extenso + núcleo más brillante, como en las nebulosas y galaxias reales.
            flux = _mag_to_flux(d.mag, scale) * _DSO_BOOST
            self._draw_psf(img, p[0], p[1], 0.7 * flux, max(self.seeing_px, size_px / 6.0))
            self._draw_psf(img, p[0], p[1], 0.3 * flux, max(self.seeing_px, size_px / 20.0))

    def _draw_open_cluster(self, img: np.ndarray, d, g: FieldGeometry, scale: float) -> None:
        rng = np.random.default_rng(zlib.crc32(d.id.encode()))
        size = d.size_arcmin or 10.0
        radius_deg = size / 60.0 / 3.0
        for _ in range(int(min(60, max(15, size * 2)))):
            ra, dec = offset_by(d.ra, d.dec, rng.normal(0, radius_deg), rng.normal(0, radius_deg))
            p = g.to_pixel(ra, dec)
            if p is not None:
                mag = (d.mag or 7.0) + 3.0 + rng.exponential(1.5)
                self._draw_psf(img, p[0], p[1], _mag_to_flux(mag, scale), self.seeing_px)

    # ------------------------------------------------------------------ sistema solar
    def _draw_bodies(self, img: np.ndarray, pose: FieldPose, scale: float) -> None:
        g = pose.geometry
        for body in self._catalog.bodies_near(g.ra, g.dec, g.radius + 0.6, pose.jd, pose.site):
            p = g.to_pixel(body.ra, body.dec)
            if p is None or (body.body == "sun" and self.force_night):
                continue
            radius_px = (body.size_arcmin or 0) * 60.0 / (g.scale_x * 3600.0) / 2.0
            if radius_px < 1.5:
                self._draw_psf(img, p[0], p[1], _mag_to_flux(body.mag, scale), self.seeing_px)
                continue
            if body.body == "sun":
                img[:] = 255.0
                return
            h, w = img.shape
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
            u = (xx - p[0]) / radius_px
            v = (yy - p[1]) / radius_px
            rr = u * u + v * v
            disc = np.clip((1.0 - rr) * radius_px, 0.0, 1.0)  # borde suave de ~1 px
            if body.body == "moon":
                lit = self._moon_lit_mask(u, v, body, pose)
                surface = 900.0 * scale * (0.75 + 0.25 * np.sqrt(np.clip(1.0 - rr, 0, 1)))
                img += disc * lit * surface + disc * (1 - lit) * 2.0
            else:
                brightness = 350.0 * scale * 10 ** (-0.4 * (body.mag + 2.0))
                img += disc * max(brightness, 60.0 * scale)

    def _moon_lit_mask(self, u: np.ndarray, v: np.ndarray, moon, pose: FieldPose) -> np.ndarray:
        """Máscara 0..1 de la parte iluminada según la fase y la dirección del Sol."""
        g = pose.geometry
        k = moon_phase(pose.jd)["illuminated"]
        sun = self._catalog.body_object("sun", pose.jd)
        # Ángulo de posición del Sol visto desde la Luna.
        dra = math.radians(sun.ra - moon.ra)
        d1, d2 = math.radians(moon.dec), math.radians(sun.dec)
        pa = math.atan2(math.sin(dra) * math.cos(d2),
                        math.cos(d1) * math.sin(d2) - math.sin(d1) * math.cos(d2) * math.cos(dra))
        a = g.to_pixel(moon.ra, moon.dec)
        b = g.to_pixel(*offset_by(moon.ra, moon.dec, 0.05 * math.sin(pa), 0.05 * math.cos(pa)))
        if a is None or b is None:
            return np.ones_like(u)
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy) or 1.0
        su, sv = dx / n, dy / n                   # dirección del Sol en la imagen
        along = u * su + v * sv
        across = -u * sv + v * su
        limit = (1.0 - 2.0 * k) * np.sqrt(np.clip(1.0 - across * across, 0, 1))
        return np.clip((along - limit) * 20.0 + 0.5, 0.0, 1.0)
