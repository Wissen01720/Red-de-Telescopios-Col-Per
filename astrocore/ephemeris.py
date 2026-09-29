"""Posiciones aproximadas del Sol, la Luna y los planetas.

* Planetas y Sol: elementos keplerianos aproximados de JPL (Standish,
  válidos 1800–2050). Error típico de unos pocos minutos de arco.
* Luna: términos principales de Meeus (cap. 47), error ~1-2′. Se puede pedir
  la posición topocéntrica, que en la Luna difiere hasta ~1° de la
  geocéntrica y es distinta en Colombia y en Perú.

Todas las funciones devuelven RA/Dec J2000 en grados.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .coords import (DELTA_T_S, J2000, Site, angular_separation, date_to_j2000,
                     gmst_deg)

_D2R = math.pi / 180.0
_R2D = 180.0 / math.pi
AU_KM = 149597870.7
EARTH_RADIUS_KM = 6378.14
OBLIQUITY_J2000 = 23.43928

# Elementos: (a, e, I, L, long_perihelio, long_nodo) y sus tasas por siglo.
_ELEMENTS = {
    "mercury": ((0.38709927, 0.20563593, 7.00497902, 252.25032350, 77.45779628, 48.33076593),
                (0.00000037, 0.00001906, -0.00594749, 149472.67411175, 0.16047689, -0.12534081)),
    "venus": ((0.72333566, 0.00677672, 3.39467605, 181.97909950, 131.60246718, 76.67984255),
              (0.00000390, -0.00004107, -0.00078890, 58517.81538729, 0.00268329, -0.27769418)),
    "earth": ((1.00000261, 0.01671123, -0.00001531, 100.46457166, 102.93768193, 0.0),
              (0.00000562, -0.00004392, -0.01294668, 35999.37244981, 0.32327364, 0.0)),
    "mars": ((1.52371034, 0.09339410, 1.84969142, -4.55343205, -23.94362959, 49.55953891),
             (0.00001847, 0.00007882, -0.00813131, 19140.30268499, 0.44441088, -0.29257343)),
    "jupiter": ((5.20288700, 0.04838624, 1.30439695, 34.39644051, 14.72847983, 100.47390909),
                (-0.00011607, -0.00013253, -0.00183714, 3034.74612775, 0.21252668, 0.20469106)),
    "saturn": ((9.53667594, 0.05386179, 2.48599187, 49.95424423, 92.59887831, 113.66242448),
               (-0.00125060, -0.00050991, 0.00193609, 1222.49362201, -0.41897216, -0.28867794)),
    "uranus": ((19.18916464, 0.04725744, 0.77263783, 313.23810451, 170.95427630, 74.01692503),
               (-0.00196176, -0.00004397, -0.00242939, 428.48202785, 0.40805281, 0.04240589)),
    "neptune": ((30.06992276, 0.00859048, 1.77004347, -55.12002969, 44.96476227, 131.78422574),
                (0.00026291, 0.00005105, 0.00035372, 218.45945325, -0.32241464, -0.00508664)),
}

# Radio ecuatorial (km) y magnitud visual típica (solo orientativa).
BODY_INFO = {
    "sun": {"name": "Sol", "radius_km": 696000.0, "mag": -26.7},
    "moon": {"name": "Luna", "radius_km": 1737.4, "mag": -12.0},
    "mercury": {"name": "Mercurio", "radius_km": 2440.0, "mag": 0.0},
    "venus": {"name": "Venus", "radius_km": 6052.0, "mag": -4.2},
    "mars": {"name": "Marte", "radius_km": 3396.0, "mag": 0.5},
    "jupiter": {"name": "Júpiter", "radius_km": 71492.0, "mag": -2.3},
    "saturn": {"name": "Saturno", "radius_km": 60268.0, "mag": 0.6},
    "uranus": {"name": "Urano", "radius_km": 25559.0, "mag": 5.7},
    "neptune": {"name": "Neptuno", "radius_km": 24764.0, "mag": 7.8},
}
PLANETS = ("mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune")
BODIES = ("sun", "moon") + PLANETS


@dataclass(frozen=True)
class BodyPosition:
    body: str
    ra: float           # J2000, grados
    dec: float
    distance_km: float

    @property
    def name(self) -> str:
        return BODY_INFO[self.body]["name"]

    @property
    def angular_diameter_deg(self) -> float:
        return 2.0 * math.degrees(math.asin(min(1.0, BODY_INFO[self.body]["radius_km"] / self.distance_km)))


def _tt_centuries(jd: float) -> float:
    return (jd + DELTA_T_S / 86400.0 - J2000) / 36525.0


def _heliocentric_ecliptic(planet: str, T: float) -> tuple[float, float, float]:
    base, rate = _ELEMENTS[planet]
    a, e, inc, L, varpi, node = (b + r * T for b, r in zip(base, rate))
    M = (L - varpi + 180.0) % 360.0 - 180.0
    omega = varpi - node
    e_deg = e * _R2D
    E = M + e_deg * math.sin(M * _D2R)
    for _ in range(12):
        dM = M - (E - e_deg * math.sin(E * _D2R))
        dE = dM / (1.0 - e * math.cos(E * _D2R))
        E += dE
        if abs(dE) < 1e-8:
            break
    xp = a * (math.cos(E * _D2R) - e)
    yp = a * math.sqrt(1.0 - e * e) * math.sin(E * _D2R)
    co, so = math.cos(omega * _D2R), math.sin(omega * _D2R)
    cn, sn = math.cos(node * _D2R), math.sin(node * _D2R)
    ci, si = math.cos(inc * _D2R), math.sin(inc * _D2R)
    x = (co * cn - so * sn * ci) * xp + (-so * cn - co * sn * ci) * yp
    y = (co * sn + so * cn * ci) * xp + (-so * sn + co * cn * ci) * yp
    z = (so * si) * xp + (co * si) * yp
    return x, y, z


def _ecliptic_to_radec(x: float, y: float, z: float, obliquity: float) -> tuple[float, float, float]:
    ce, se = math.cos(obliquity * _D2R), math.sin(obliquity * _D2R)
    X, Y, Z = x, y * ce - z * se, y * se + z * ce
    r = math.sqrt(X * X + Y * Y + Z * Z)
    ra = math.atan2(Y, X) * _R2D % 360.0
    dec = math.asin(Z / r) * _R2D
    return ra, dec, r


def planet_position(planet: str, jd: float) -> BodyPosition:
    """Posición geocéntrica J2000 de un planeta (o del Sol con 'sun')."""
    T = _tt_centuries(jd)
    ex, ey, ez = _heliocentric_ecliptic("earth", T)
    if planet == "sun":
        gx, gy, gz = -ex, -ey, -ez
    else:
        px, py, pz = _heliocentric_ecliptic(planet, T)
        gx, gy, gz = px - ex, py - ey, pz - ez
        # Corrección por tiempo de luz (una iteración basta).
        dist_au = math.sqrt(gx * gx + gy * gy + gz * gz)
        T_lt = T - dist_au * AU_KM / 299792.458 / 86400.0 / 36525.0
        px, py, pz = _heliocentric_ecliptic(planet, T_lt)
        gx, gy, gz = px - ex, py - ey, pz - ez
    ra, dec, r = _ecliptic_to_radec(gx, gy, gz, OBLIQUITY_J2000)
    return BodyPosition(planet, ra, dec, r * AU_KM)


# --------------------------------------------------------------------------- Luna

# (D, M, M', F, coef. longitud [1e-6 °], coef. distancia [1e-3 km])
_MOON_LR = (
    (0, 0, 1, 0, 6288774, -20905355), (2, 0, -1, 0, 1274027, -3699111),
    (2, 0, 0, 0, 658314, -2955968), (0, 0, 2, 0, 213618, -569925),
    (0, 1, 0, 0, -185116, 48888), (0, 0, 0, 2, -114332, -3149),
    (2, 0, -2, 0, 58793, 246158), (2, -1, -1, 0, 57066, -152138),
    (2, 0, 1, 0, 53322, -170733), (2, -1, 0, 0, 45758, -204586),
    (0, 1, -1, 0, -40923, -129620), (1, 0, 0, 0, -34720, 108743),
    (0, 1, 1, 0, -30383, 104755), (2, 0, 0, -2, 15327, 10321),
    (0, 0, 1, 2, -12528, 0), (0, 0, 1, -2, 10980, 79661),
    (4, 0, -1, 0, 10675, -34782), (0, 0, 3, 0, 10034, -23210),
    (4, 0, -2, 0, 8548, -21636), (2, 1, -1, 0, -7888, 24208),
    (2, 1, 0, 0, -6766, 30824), (1, 0, -1, 0, -5163, -8379),
    (1, 1, 0, 0, 4987, -16675), (2, -1, 1, 0, 4036, -12831),
    (2, 0, 2, 0, 3994, -10445), (4, 0, 0, 0, 3861, -11650),
    (2, 0, -3, 0, 3665, 14403), (0, 1, -2, 0, -2689, -7003),
    (2, 0, -1, 2, -2602, 0), (2, -1, -2, 0, 2390, 10056),
    (1, 0, 1, 0, -2348, 6322), (2, -2, 0, 0, 2236, -9884),
)
# (D, M, M', F, coef. latitud [1e-6 °])
_MOON_B = (
    (0, 0, 0, 1, 5128122), (0, 0, 1, 1, 280602), (0, 0, 1, -1, 277693),
    (2, 0, 0, -1, 173237), (2, 0, -1, 1, 55413), (2, 0, -1, -1, 46271),
    (2, 0, 0, 1, 32573), (0, 0, 2, 1, 17198), (2, 0, 1, -1, 9266),
    (0, 0, 2, -1, 8822), (2, -1, 0, -1, 8216), (2, 0, -2, -1, 4324),
    (2, 0, 1, 1, 4200), (2, 1, 0, -1, -3359), (2, -1, -1, 1, 2463),
    (2, -1, 0, 1, 2211), (2, -1, -1, -1, 2065), (0, 1, -1, -1, -1870),
    (4, 0, -1, -1, 1828), (0, 1, 0, 1, -1794), (0, 0, 0, 3, -1749),
    (0, 1, -1, 1, -1565), (1, 0, 0, 1, -1491), (0, 1, 1, 1, -1475),
    (0, 1, 1, -1, -1410), (0, 1, 0, -1, -1344), (1, 0, 0, -1, -1335),
    (0, 0, 3, 1, 1107), (4, 0, 0, -1, 1021), (4, 0, -1, 1, 833),
)


def _moon_of_date(jd: float) -> tuple[float, float, float, float]:
    """Luna geocéntrica: (ra, dec) del equinoccio de la fecha, distancia km, oblicuidad."""
    T = _tt_centuries(jd)
    Lp = 218.3164477 + 481267.88123421 * T - 0.0015786 * T * T + T ** 3 / 538841.0 - T ** 4 / 65194000.0
    D = 297.8501921 + 445267.1114034 * T - 0.0018819 * T * T + T ** 3 / 545868.0 - T ** 4 / 113065000.0
    M = 357.5291092 + 35999.0502909 * T - 0.0001536 * T * T + T ** 3 / 24490000.0
    Mp = 134.9633964 + 477198.8675055 * T + 0.0087414 * T * T + T ** 3 / 69699.0 - T ** 4 / 14712000.0
    F = 93.2720950 + 483202.0175233 * T - 0.0036539 * T * T - T ** 3 / 3526000.0 + T ** 4 / 863310000.0
    A1 = 119.75 + 131.849 * T
    A2 = 53.09 + 479264.290 * T
    A3 = 313.45 + 481266.484 * T
    E = 1.0 - 0.002516 * T - 0.0000074 * T * T

    def ecc(m: int) -> float:
        return E ** abs(m)

    sl = sr = sb = 0.0
    for d, m, mp, f, cl, cr in _MOON_LR:
        arg = (d * D + m * M + mp * Mp + f * F) * _D2R
        sl += cl * ecc(m) * math.sin(arg)
        sr += cr * ecc(m) * math.cos(arg)
    for d, m, mp, f, cb in _MOON_B:
        sb += cb * ecc(m) * math.sin((d * D + m * M + mp * Mp + f * F) * _D2R)
    sl += 3958 * math.sin(A1 * _D2R) + 1962 * math.sin((Lp - F) * _D2R) + 318 * math.sin(A2 * _D2R)
    sb += (-2235 * math.sin(Lp * _D2R) + 382 * math.sin(A3 * _D2R)
           + 175 * math.sin((A1 - F) * _D2R) + 175 * math.sin((A1 + F) * _D2R)
           + 127 * math.sin((Lp - Mp) * _D2R) - 115 * math.sin((Lp + Mp) * _D2R))
    lon = (Lp + sl / 1e6) * _D2R
    lat = (sb / 1e6) * _D2R
    dist = 385000.56 + sr / 1000.0
    eps = 23.4392911 - 0.0130042 * T  # oblicuidad media de la fecha
    x = math.cos(lat) * math.cos(lon)
    y = math.cos(lat) * math.sin(lon)
    z = math.sin(lat)
    ra, dec, _ = _ecliptic_to_radec(x, y, z, eps)
    return ra, dec, dist, eps


def moon_position(jd: float, site: Site | None = None) -> BodyPosition:
    """Posición J2000 de la Luna; topocéntrica si se indica ``site``."""
    ra, dec, dist, _ = _moon_of_date(jd)
    if site is not None:
        # Vector Tierra->Luna menos el vector Tierra->observador (equinoccio de la fecha).
        cd, sd = math.cos(dec * _D2R), math.sin(dec * _D2R)
        mx, my, mz = dist * cd * math.cos(ra * _D2R), dist * cd * math.sin(ra * _D2R), dist * sd
        lst = (gmst_deg(jd) + site.longitude) * _D2R
        rho = EARTH_RADIUS_KM + site.elevation_m / 1000.0
        lat = site.latitude * _D2R
        ox, oy, oz = rho * math.cos(lat) * math.cos(lst), rho * math.cos(lat) * math.sin(lst), rho * math.sin(lat)
        mx, my, mz = mx - ox, my - oy, mz - oz
        dist = math.sqrt(mx * mx + my * my + mz * mz)
        ra = math.atan2(my, mx) * _R2D % 360.0
        dec = math.asin(mz / dist) * _R2D
    ra, dec = date_to_j2000(ra, dec, jd)
    return BodyPosition("moon", ra, dec, dist)


def body_position(body: str, jd: float, site: Site | None = None) -> BodyPosition:
    body = body.lower()
    if body == "moon":
        return moon_position(jd, site)
    if body in _ELEMENTS or body == "sun":
        return planet_position(body, jd)
    raise KeyError(f"Cuerpo desconocido: {body}")


def all_bodies(jd: float, site: Site | None = None) -> list[BodyPosition]:
    return [body_position(b, jd, site) for b in BODIES]


# --------------------------------------------------------------------------- fase lunar

_PHASE_NAMES = (
    (0.03, "Luna nueva"), (0.22, "Creciente"), (0.28, "Cuarto creciente"),
    (0.47, "Gibosa creciente"), (0.53, "Luna llena"), (0.72, "Gibosa menguante"),
    (0.78, "Cuarto menguante"), (0.97, "Menguante"), (1.01, "Luna nueva"),
)


def moon_phase(jd: float) -> dict:
    """Fracción iluminada, edad aproximada de la fase (0-1) y nombre en español."""
    sun = planet_position("sun", jd)
    moon = moon_position(jd)
    elong = angular_separation(sun.ra, sun.dec, moon.ra, moon.dec) * _D2R
    phase_angle = math.atan2(sun.distance_km * math.sin(elong),
                             moon.distance_km - sun.distance_km * math.cos(elong))
    illuminated = (1.0 + math.cos(phase_angle)) / 2.0
    # Creciente si la Luna está al Este del Sol (diferencia de AR positiva).
    waxing = ((moon.ra - sun.ra) % 360.0) < 180.0
    cycle = (1.0 - math.cos(elong)) / 4.0 if waxing else 1.0 - (1.0 - math.cos(elong)) / 4.0
    name = next(label for limit, label in _PHASE_NAMES if cycle < limit)
    return {"illuminated": illuminated, "cycle": cycle, "waxing": waxing, "name": name}


def twilight_label(sun_alt: float) -> str:
    if sun_alt > -0.833:
        return "Día"
    if sun_alt > -6.0:
        return "Crepúsculo civil"
    if sun_alt > -12.0:
        return "Crepúsculo náutico"
    if sun_alt > -18.0:
        return "Crepúsculo astronómico"
    return "Noche astronómica"
