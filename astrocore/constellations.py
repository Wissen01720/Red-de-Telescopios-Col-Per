"""Identificación de la constelación IAU que contiene una posición.

Algoritmo de Roman (1987): se precesa la posición al equinoccio B1875 y se
busca el primer tramo de la tabla cuya declinación inferior sea <= Dec y cuyo
rango de AR contenga la AR.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .coords import B1875, J2000, precess

_DATA_FILE = Path(__file__).parent / "data" / "constellation_bounds.dat"

NAMES_ES = {
    "And": "Andrómeda", "Ant": "Máquina Neumática", "Aps": "Ave del Paraíso", "Aqr": "Acuario",
    "Aql": "Águila", "Ara": "Altar", "Ari": "Aries", "Aur": "Auriga", "Boo": "Boyero",
    "Cae": "Cincel", "Cam": "Jirafa", "Cnc": "Cáncer", "CVn": "Perros de Caza",
    "CMa": "Can Mayor", "CMi": "Can Menor", "Cap": "Capricornio", "Car": "Quilla",
    "Cas": "Casiopea", "Cen": "Centauro", "Cep": "Cefeo", "Cet": "Ballena", "Cha": "Camaleón",
    "Cir": "Compás", "Col": "Paloma", "Com": "Cabellera de Berenice", "CrA": "Corona Austral",
    "CrB": "Corona Boreal", "Crv": "Cuervo", "Crt": "Copa", "Cru": "Cruz del Sur",
    "Cyg": "Cisne", "Del": "Delfín", "Dor": "Dorado", "Dra": "Dragón", "Equ": "Caballo Menor",
    "Eri": "Erídano", "For": "Horno", "Gem": "Géminis", "Gru": "Grulla", "Her": "Hércules",
    "Hor": "Reloj", "Hya": "Hidra", "Hyi": "Hidra Macho", "Ind": "Indio", "Lac": "Lagarto",
    "Leo": "Leo", "LMi": "León Menor", "Lep": "Liebre", "Lib": "Libra", "Lup": "Lobo",
    "Lyn": "Lince", "Lyr": "Lira", "Men": "Mesa", "Mic": "Microscopio", "Mon": "Unicornio",
    "Mus": "Mosca", "Nor": "Escuadra", "Oct": "Octante", "Oph": "Ofiuco", "Ori": "Orión",
    "Pav": "Pavo", "Peg": "Pegaso", "Per": "Perseo", "Phe": "Fénix", "Pic": "Pintor",
    "Psc": "Piscis", "PsA": "Pez Austral", "Pup": "Popa", "Pyx": "Brújula", "Ret": "Retículo",
    "Sge": "Flecha", "Sgr": "Sagitario", "Sco": "Escorpio", "Scl": "Escultor", "Sct": "Escudo",
    "Ser": "Serpiente", "Sex": "Sextante", "Tau": "Tauro", "Tel": "Telescopio",
    "Tri": "Triángulo", "TrA": "Triángulo Austral", "Tuc": "Tucán", "UMa": "Osa Mayor",
    "UMi": "Osa Menor", "Vel": "Vela", "Vir": "Virgo", "Vol": "Pez Volador", "Vul": "Zorra",
}


@lru_cache(maxsize=1)
def _bounds() -> tuple[tuple[float, float, float, str], ...]:
    rows = []
    for line in _DATA_FILE.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 4:
            rows.append((float(parts[0]), float(parts[1]), float(parts[2]), parts[3]))
    return tuple(rows)


def constellation_of(ra: float, dec: float, jd_equinox: float = J2000) -> str:
    """Abreviatura IAU (p. ej. 'Ori') de la constelación que contiene (ra, dec)."""
    ra1875, dec1875 = precess(ra, dec, jd_equinox, B1875)
    ra_h = ra1875 / 15.0
    for ra_low, ra_up, dec_low, abbr in _bounds():
        if dec1875 >= dec_low and ra_low <= ra_h < ra_up:
            return abbr
    return "Oct"  # Solo alcanzable muy cerca del polo sur celeste.


def constellation_name(abbr: str) -> str:
    return NAMES_ES.get(abbr, abbr)
