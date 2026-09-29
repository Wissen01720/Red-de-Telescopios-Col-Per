"""Tiempo sidéreo, precesión y conversiones RA/Dec <-> Alt/Az.

Precisión: mejor que ~0.01° (se omiten nutación y aberración, de ~20"),
más que suficiente para telescopios con campos de fracciones de grado.

Convenciones:
* Todos los ángulos en grados.
* Azimut medido desde el Norte hacia el Este (N=0, E=90, S=180, O=270).
* Longitud positiva hacia el Este (Colombia y Perú tienen longitud negativa).
* ``jd`` es la fecha juliana (UTC ~ UT1; la diferencia < 1 s es despreciable).
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

J2000 = 2451545.0
B1875 = 2405889.258550475
UNIX_EPOCH_JD = 2440587.5
# TT - UTC aproximado (s). Solo importa para la Luna (~0.5"/s).
DELTA_T_S = 69.2

_D2R = math.pi / 180.0
_R2D = 180.0 / math.pi


@dataclass(frozen=True)
class Site:
    """Ubicación de un observatorio."""

    latitude: float   # grados, + Norte
    longitude: float  # grados, + Este
    elevation_m: float = 0.0


# --------------------------------------------------------------------------- tiempo

def julian_date(t: float | datetime | None = None) -> float:
    """Fecha juliana a partir de un timestamp Unix, un datetime o 'ahora'."""
    if t is None:
        ts = time.time()
    elif isinstance(t, datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        ts = t.timestamp()
    else:
        ts = float(t)
    return ts / 86400.0 + UNIX_EPOCH_JD


def unix_time(jd: float) -> float:
    return (jd - UNIX_EPOCH_JD) * 86400.0


def gmst_deg(jd: float) -> float:
    """Tiempo sidéreo medio de Greenwich (IAU 1982), en grados."""
    d = jd - J2000
    t = d / 36525.0
    g = 280.46061837 + 360.98564736629 * d + 0.000387933 * t * t - t ** 3 / 38710000.0
    return g % 360.0


def lst_deg(jd: float, longitude: float) -> float:
    """Tiempo sidéreo local, en grados."""
    return (gmst_deg(jd) + longitude) % 360.0


# --------------------------------------------------------------------------- utilidades angulares

def wrap360(a: float) -> float:
    return a % 360.0


def wrap180(a: float) -> float:
    """Normaliza a (-180, 180]."""
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def angular_separation(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Separación angular entre dos puntos (fórmula de Vincenty, estable)."""
    ra1, dec1, ra2, dec2 = (x * _D2R for x in (ra1, dec1, ra2, dec2))
    dra = ra2 - ra1
    sd1, cd1, sd2, cd2 = math.sin(dec1), math.cos(dec1), math.sin(dec2), math.cos(dec2)
    num = math.hypot(cd2 * math.sin(dra), cd1 * sd2 - sd1 * cd2 * math.cos(dra))
    den = sd1 * sd2 + cd1 * cd2 * math.cos(dra)
    return math.atan2(num, den) * _R2D


def offset_by(ra: float, dec: float, east_deg: float, north_deg: float) -> tuple[float, float]:
    """Desplaza (ra, dec) una distancia en el plano tangente (Este, Norte)."""
    return deproject(east_deg, north_deg, ra, dec)


# --------------------------------------------------------------------------- precesión

def precess(ra: float, dec: float, jd_from: float, jd_to: float) -> tuple[float, float]:
    """Precesión entre dos equinoccios (IAU 1976, Meeus cap. 21)."""
    if abs(jd_to - jd_from) < 1e-6:
        return ra, dec
    T = (jd_from - J2000) / 36525.0
    t = (jd_to - jd_from) / 36525.0
    zeta = ((2306.2181 + 1.39656 * T - 0.000139 * T * T) * t
            + (0.30188 - 0.000344 * T) * t * t + 0.017998 * t ** 3) / 3600.0
    z = ((2306.2181 + 1.39656 * T - 0.000139 * T * T) * t
         + (1.09468 + 0.000066 * T) * t * t + 0.018203 * t ** 3) / 3600.0
    theta = ((2004.3109 - 0.85330 * T - 0.000217 * T * T) * t
             - (0.42665 + 0.000217 * T) * t * t - 0.041833 * t ** 3) / 3600.0
    a0, d0 = ra * _D2R, dec * _D2R
    zeta, z, theta = zeta * _D2R, z * _D2R, theta * _D2R
    A = math.cos(d0) * math.sin(a0 + zeta)
    B = math.cos(theta) * math.cos(d0) * math.cos(a0 + zeta) - math.sin(theta) * math.sin(d0)
    C = math.sin(theta) * math.cos(d0) * math.cos(a0 + zeta) + math.cos(theta) * math.sin(d0)
    ra_out = (math.atan2(A, B) + z) * _R2D
    dec_out = math.asin(max(-1.0, min(1.0, C))) * _R2D
    return ra_out % 360.0, dec_out


def j2000_to_date(ra: float, dec: float, jd: float) -> tuple[float, float]:
    return precess(ra, dec, J2000, jd)


def date_to_j2000(ra: float, dec: float, jd: float) -> tuple[float, float]:
    return precess(ra, dec, jd, J2000)


# --------------------------------------------------------------------------- refracción

def refraction_true_to_apparent(alt: float) -> float:
    """Corrección (grados) a sumar a una altura verdadera (Saemundsson)."""
    if alt < -1.0:
        return 0.0
    return 1.02 / math.tan((alt + 10.3 / (alt + 5.11)) * _D2R) / 60.0


def refraction_apparent_to_true(alt: float) -> float:
    """Corrección (grados) a restar de una altura aparente (Bennett)."""
    if alt < -1.0:
        return 0.0
    return 1.0 / math.tan((alt + 7.31 / (alt + 4.4)) * _D2R) / 60.0


# --------------------------------------------------------------------------- ecuatorial <-> horizontal

def hadec_to_altaz(ha: float, dec: float, lat: float) -> tuple[float, float]:
    h, d, p = ha * _D2R, dec * _D2R, lat * _D2R
    sin_alt = math.sin(d) * math.sin(p) + math.cos(d) * math.cos(p) * math.cos(h)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    az = math.atan2(-math.sin(h) * math.cos(d),
                    math.sin(d) * math.cos(p) - math.cos(d) * math.sin(p) * math.cos(h))
    return alt * _R2D, (az * _R2D) % 360.0


def altaz_to_hadec(alt: float, az: float, lat: float) -> tuple[float, float]:
    a, z, p = alt * _D2R, az * _D2R, lat * _D2R
    sin_dec = math.sin(a) * math.sin(p) + math.cos(a) * math.cos(p) * math.cos(z)
    dec = math.asin(max(-1.0, min(1.0, sin_dec)))
    ha = math.atan2(-math.sin(z) * math.cos(a),
                    math.sin(a) * math.cos(p) - math.cos(a) * math.sin(p) * math.cos(z))
    return (ha * _R2D) % 360.0, dec * _R2D


def radec_to_altaz(ra: float, dec: float, site: Site, jd: float,
                   refraction: bool = False) -> tuple[float, float]:
    """RA/Dec J2000 -> (altitud, azimut) en el sitio y fecha indicados."""
    ra_d, dec_d = j2000_to_date(ra, dec, jd)
    ha = lst_deg(jd, site.longitude) - ra_d
    alt, az = hadec_to_altaz(ha, dec_d, site.latitude)
    if refraction:
        alt += refraction_true_to_apparent(alt)
    return alt, az


def altaz_to_radec(alt: float, az: float, site: Site, jd: float,
                   refraction: bool = False) -> tuple[float, float]:
    """(altitud, azimut) -> RA/Dec J2000."""
    if refraction:
        alt -= refraction_apparent_to_true(alt)
    ha, dec_d = altaz_to_hadec(alt, az, site.latitude)
    ra_d = (lst_deg(jd, site.longitude) - ha) % 360.0
    return date_to_j2000(ra_d, dec_d, jd)


def hour_angle(ra: float, dec: float, site: Site, jd: float) -> float:
    """Ángulo horario (grados, -180..180) de un objeto J2000."""
    ra_d, _ = j2000_to_date(ra, dec, jd)
    return wrap180(lst_deg(jd, site.longitude) - ra_d)


def parallactic_angle(ra: float, dec: float, site: Site, jd: float) -> float:
    """Ángulo paraláctico: ángulo de posición (N->E) de la dirección al cénit.

    En una montura altacimutal con la cámara alineada con el eje de altura,
    es la rotación del campo respecto al Norte celeste.
    """
    ra_d, dec_d = j2000_to_date(ra, dec, jd)
    h = (lst_deg(jd, site.longitude) - ra_d) * _D2R
    d, p = dec_d * _D2R, site.latitude * _D2R
    q = math.atan2(math.sin(h), math.tan(p) * math.cos(d) - math.sin(d) * math.cos(h))
    return q * _R2D


# --------------------------------------------------------------------------- proyección gnomónica

def project(ra: float, dec: float, ra0: float, dec0: float) -> tuple[float, float] | None:
    """Coordenadas estándar (xi hacia el Este, eta hacia el Norte), en grados.

    Devuelve None si el punto está a 90° o más del centro de proyección.
    """
    a, d, a0, d0 = ra * _D2R, dec * _D2R, ra0 * _D2R, dec0 * _D2R
    cos_c = math.sin(d0) * math.sin(d) + math.cos(d0) * math.cos(d) * math.cos(a - a0)
    if cos_c <= 1e-9:
        return None
    xi = math.cos(d) * math.sin(a - a0) / cos_c
    eta = (math.cos(d0) * math.sin(d) - math.sin(d0) * math.cos(d) * math.cos(a - a0)) / cos_c
    return xi * _R2D, eta * _R2D


def deproject(xi: float, eta: float, ra0: float, dec0: float) -> tuple[float, float]:
    """Inversa de :func:`project`."""
    x, y = xi * _D2R, eta * _D2R
    a0, d0 = ra0 * _D2R, dec0 * _D2R
    den = math.cos(d0) - y * math.sin(d0)
    ra = a0 + math.atan2(x, den)
    dec = math.atan2(math.sin(d0) + y * math.cos(d0), math.hypot(x, den))
    return (ra * _R2D) % 360.0, dec * _R2D


def galactic_latitude(ra: float, dec: float) -> float:
    """Latitud galáctica (grados) de una posición J2000."""
    ra_ngp, dec_ngp = 192.85948 * _D2R, 27.12825 * _D2R
    a, d = ra * _D2R, dec * _D2R
    sin_b = math.sin(d) * math.sin(dec_ngp) + math.cos(d) * math.cos(dec_ngp) * math.cos(a - ra_ngp)
    return math.asin(max(-1.0, min(1.0, sin_b))) * _R2D


# --------------------------------------------------------------------------- formato y lectura

def format_ra(ra: float, decimals: int = 1) -> str:
    """83.822 -> '05h 35m 17.3s'."""
    total = (ra % 360.0) / 15.0 * 3600.0
    total = round(total, decimals)
    if total >= 86400.0:
        total -= 86400.0
    h = int(total // 3600)
    m = int((total - h * 3600) // 60)
    s = total - h * 3600 - m * 60
    return f"{h:02d}h {m:02d}m {s:0{3 + decimals if decimals else 2}.{decimals}f}s"


def format_dec(dec: float, decimals: int = 0) -> str:
    """-5.391 -> '-05° 23′ 28″'."""
    sign = "-" if dec < 0 else "+"
    total = round(abs(dec) * 3600.0, decimals)
    d = int(total // 3600)
    m = int((total - d * 3600) // 60)
    s = total - d * 3600 - m * 60
    width = 3 + decimals if decimals else 2
    return f"{sign}{d:02d}° {m:02d}′ {s:0{width}.{decimals}f}″"


_NUM = r"[+-]?\d+(?:\.\d+)?"


def parse_angle(text: str, hours: bool) -> float:
    """Lee '05:35:17.3', '5h35m17s', '5 35 17', '-05°23′28″' o un decimal.

    Si ``hours`` es True y el texto es sexagesimal o termina en 'h', se
    interpreta en horas; un número decimal suelto se interpreta en grados.
    """
    s = text.strip().lower().replace(",", ".")
    if re.fullmatch(_NUM, s):
        return float(s)
    if re.fullmatch(_NUM + r"\s*h", s):
        return float(s[:-1]) * 15.0
    negative = s.startswith("-")
    parts = re.findall(r"\d+(?:\.\d+)?", s)
    if not parts or len(parts) > 3:
        raise ValueError(f"No se reconoce el ángulo: {text!r}")
    values = [float(p) for p in parts] + [0.0] * (3 - len(parts))
    value = values[0] + values[1] / 60.0 + values[2] / 3600.0
    if negative:
        value = -value
    return value * 15.0 if hours else value


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """Intenta leer 'RA Dec' de un texto. Devuelve (ra, dec) en grados o None.

    Formatos aceptados: '05:35:17 -05:23:28', '5h35m17s -5d23m28s',
    '83.82 -5.39', '83.82, -5.39'.
    """
    s = text.strip()
    candidates = []
    m = re.fullmatch(r"(.+?)\s*[,;]\s*(.+)", s)
    if m:
        candidates.append((m.group(1), m.group(2)))
    m = re.fullmatch(r"(\S+(?:\s*[hm]\s*\S+)*?)\s+([+-]\S.*)", s)
    if m:
        candidates.append((m.group(1), m.group(2)))
    tokens = s.split()
    if len(tokens) == 2:
        candidates.append((tokens[0], tokens[1]))
    if len(tokens) == 6:
        candidates.append((" ".join(tokens[:3]), " ".join(tokens[3:])))
    for ra_txt, dec_txt in candidates:
        try:
            ra = parse_angle(ra_txt, hours=True)
            dec = parse_angle(dec_txt, hours=False)
        except ValueError:
            continue
        if 0.0 <= ra < 360.0 and -90.0 <= dec <= 90.0:
            return ra, dec
    return None
