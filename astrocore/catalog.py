"""Catálogo de estrellas, objetos de cielo profundo y cuerpos del sistema solar.

Permite:
* Resolver un texto a coordenadas: 'M42', 'NGC 5139', 'Júpiter', 'Sirio',
  'alf Ori', 'Nebulosa de Orión', '05:35:17 -05:23:28', ...
* Buscar sugerencias para autocompletar.
* Listar los objetos que caen dentro de un campo de visión.
"""

from __future__ import annotations

import csv
import math
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from .constellations import constellation_name, constellation_of
from .coords import Site, format_dec, format_ra, parse_coordinates
from .ephemeris import BODIES, BODY_INFO, body_position
from .field import FieldGeometry

_DATA = Path(__file__).parent / "data"

GREEK = {
    "Alp": "α", "Bet": "β", "Gam": "γ", "Del": "δ", "Eps": "ε", "Zet": "ζ", "Eta": "η",
    "The": "θ", "Iot": "ι", "Kap": "κ", "Lam": "λ", "Mu": "μ", "Nu": "ν", "Xi": "ξ",
    "Omi": "ο", "Pi": "π", "Rho": "ρ", "Sig": "σ", "Tau": "τ", "Ups": "υ", "Phi": "φ",
    "Chi": "χ", "Psi": "ψ", "Ome": "ω",
}
_GREEK_WORDS = {
    "alpha": "alp", "alfa": "alp", "beta": "bet", "gamma": "gam", "delta": "del",
    "epsilon": "eps", "zeta": "zet", "theta": "the", "iota": "iot", "kappa": "kap",
    "lambda": "lam", "omicron": "omi", "sigma": "sig", "upsilon": "ups", "omega": "ome",
}

DSO_TYPES_ES = {
    "G": "Galaxia", "GPair": "Par de galaxias", "GTrpl": "Trío de galaxias",
    "GGroup": "Grupo de galaxias", "OCl": "Cúmulo abierto", "GCl": "Cúmulo globular",
    "PN": "Nebulosa planetaria", "Neb": "Nebulosa", "HII": "Región HII (nebulosa de emisión)",
    "Cl+N": "Cúmulo con nebulosa", "*Ass": "Asociación estelar", "RfN": "Nebulosa de reflexión",
    "SNR": "Remanente de supernova", "EmN": "Nebulosa de emisión", "DrkN": "Nebulosa oscura",
    "*": "Estrella", "**": "Estrella doble", "Other": "Otro",
}

# Nombres en español (sin tildes, en minúscula) -> identificador del catálogo.
ALIASES_ES = {
    # Estrellas
    "sirio": "Sirius", "canopo": "Canopus", "arturo": "Arcturus", "procion": "Procyon",
    "aldebaran": "Aldebaran", "espiga": "Spica", "polux": "Pollux", "regulo": "Regulus",
    "estrella polar": "Polaris", "polar": "Polaris", "alfa centauri": "Rigil Kentaurus",
    "beta centauri": "Hadar", "betelgeuse": "Betelgeuse", "vega": "Vega",
    # Cielo profundo
    "nebulosa de orion": "M 42", "gran nebulosa de orion": "M 42", "pleyades": "M 45",
    "las pleyades": "M 45", "siete cabritas": "M 45", "galaxia de andromeda": "M 31",
    "andromeda": "M 31", "cumulo de hercules": "M 13", "nebulosa del cangrejo": "M 1",
    "nebulosa de la laguna": "M 8", "nebulosa trifida": "M 20", "nebulosa del anillo": "M 57",
    "nebulosa del aguila": "M 16", "pilares de la creacion": "M 16", "nebulosa omega": "M 17",
    "galaxia del remolino": "M 51", "galaxia del sombrero": "M 104", "pesebre": "M 44",
    "el pesebre": "M 44", "colmena": "M 44", "galaxia del triangulo": "M 33",
    "nebulosa dumbbell": "M 27", "nebulosa de la haltera": "M 27", "omega centauri": "NGC 5139",
    "joyero": "NGC 4755", "caja de joyas": "NGC 4755", "47 tucanae": "NGC 104",
    "47 tuc": "NGC 104", "nebulosa de carina": "NGC 3372", "nebulosa de eta carinae": "NGC 3372",
    "gran nube de magallanes": "ESO 56-115", "pequena nube de magallanes": "NGC 292",
    "hiades": "C 41", "saco de carbon": "C 99", "cabeza de caballo": "B 33",
}


def normalize(text: str) -> str:
    """Minúsculas, sin tildes y con espacios simples."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[\s_\-]+", " ", text.lower()).strip()
    return text


@dataclass
class SkyObject:
    id: str                 # identificador único ('M 42', 'HIP 32349', 'jupiter')
    name: str               # nombre para mostrar
    kind: str               # 'star' | 'dso' | 'body'
    ra: float               # J2000 grados (para cuerpos: en el instante calculado)
    dec: float
    mag: float | None = None
    type_label: str = ""
    size_arcmin: float | None = None
    con: str = ""
    aliases: list[str] = field(default_factory=list)
    body: str | None = None  # clave del cuerpo del sistema solar ('moon', 'mars', ...)

    def to_dict(self) -> dict:
        con = self.con or constellation_of(self.ra, self.dec)
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "ra": self.ra, "dec": self.dec,
            "ra_str": format_ra(self.ra), "dec_str": format_dec(self.dec), "mag": self.mag,
            "type": self.type_label, "size_arcmin": self.size_arcmin, "body": self.body,
            "constellation": con, "constellation_name": constellation_name(con),
        }


class Catalog:
    def __init__(self, data_dir: Path = _DATA):
        self.stars = self._load_stars(data_dir / "stars.csv")
        self.dsos = self._load_dsos(data_dir / "dso.csv")
        self._index: dict[str, SkyObject] = {}
        for obj in self.dsos + self.stars:  # los DSO tienen prioridad ante nombres repetidos
            for key in [obj.id, obj.name, *obj.aliases]:
                self._index.setdefault(normalize(key), obj)
        for alias, target in ALIASES_ES.items():
            obj = self._index.get(normalize(target))
            if obj is not None:
                self._index.setdefault(alias, obj)
        self._star_xyz = _unit_vectors([s.ra for s in self.stars], [s.dec for s in self.stars])
        self._star_mag = np.array([s.mag for s in self.stars], dtype=float)
        self._dso_xyz = _unit_vectors([d.ra for d in self.dsos], [d.dec for d in self.dsos])
        self._big_dso_idx = [i for i, d in enumerate(self.dsos) if (d.size_arcmin or 0) >= 30.0]

    # ------------------------------------------------------------------ carga
    @staticmethod
    def _load_stars(path: Path) -> list[SkyObject]:
        stars = []
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                aliases = []
                bayer = row["bayer"]
                greek = ""
                if bayer:
                    base, _, sup = bayer.partition("-")
                    greek = GREEK.get(base, base) + (sup or "")
                    aliases += [f"{greek} {row['con']}", f"{base}{sup} {row['con']}", f"{base} {row['con']}"]
                if row["flam"]:
                    aliases.append(f"{row['flam']} {row['con']}")
                if row["hd"]:
                    aliases.append(f"HD {row['hd']}")
                if row["name"]:
                    display = row["name"]
                elif greek:
                    display = f"{greek} {row['con']}"
                elif row["flam"]:
                    display = f"{row['flam']} {row['con']}"
                elif row["hip"]:
                    display = f"HIP {row['hip']}"
                else:
                    display = f"HD {row['hd']}"
                if row["name"] and greek:
                    display = f"{row['name']} ({greek} {row['con']})"
                    aliases.append(row["name"])
                sid = f"HIP {row['hip']}" if row["hip"] else f"HD {row['hd']}"
                stars.append(SkyObject(
                    id=sid, name=display, kind="star", ra=float(row["ra"]), dec=float(row["dec"]),
                    mag=float(row["mag"]), type_label=f"Estrella {row['spect']}".strip(),
                    con=row["con"], aliases=aliases,
                ))
        return stars

    @staticmethod
    def _load_dsos(path: Path) -> list[SkyObject]:
        dsos = []
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                common = [c for c in row["common"].split("|") if c]
                aliases = list(common)
                messier = row["messier"]
                if messier:
                    aliases.append(row["id"])
                    oid = f"M {int(messier)}"
                else:
                    oid = row["id"]
                name = oid
                if common:
                    name = f"{oid} ({common[0]})" if oid != common[0] else oid
                size = float(row["major_arcmin"]) if row["major_arcmin"] else None
                dsos.append(SkyObject(
                    id=oid, name=name, kind="dso", ra=float(row["ra"]), dec=float(row["dec"]),
                    mag=float(row["mag"]) if row["mag"] else None,
                    type_label=DSO_TYPES_ES.get(row["type"], row["type"]),
                    size_arcmin=size, con=row["con"], aliases=aliases,
                ))
        return dsos

    # ------------------------------------------------------------------ cuerpos
    @staticmethod
    def body_object(body: str, jd: float, site: Site | None = None) -> SkyObject:
        pos = body_position(body, jd, site)
        info = BODY_INFO[body]
        label = "Satélite natural" if body == "moon" else ("Estrella" if body == "sun" else "Planeta")
        return SkyObject(
            id=body, name=info["name"], kind="body", ra=pos.ra, dec=pos.dec, mag=info["mag"],
            type_label=label, size_arcmin=pos.angular_diameter_deg * 60.0, body=body,
        )

    @staticmethod
    def _body_key(query: str) -> str | None:
        q = normalize(query)
        for key in BODIES:
            if q in (key, normalize(BODY_INFO[key]["name"])):
                return key
        return None

    # ------------------------------------------------------------------ resolución
    def resolve(self, query: str, jd: float, site: Site | None = None) -> SkyObject | None:
        """Convierte un texto en un objeto con coordenadas J2000."""
        query = query.strip()
        if not query:
            return None
        coords = parse_coordinates(query)
        if coords is not None:
            ra, dec = coords
            return SkyObject(id=f"{ra:.4f},{dec:.4f}", name=f"{format_ra(ra, 0)} {format_dec(dec)}",
                             kind="coords", ra=ra, dec=dec, type_label="Coordenadas")
        body = self._body_key(query)
        if body:
            return self.body_object(body, jd, site)
        q = normalize(query)
        m = re.fullmatch(r"(m|ngc|ic|hip|hd)\s*0*(\d+)", q)
        if m:
            q = f"{m.group(1)} {m.group(2)}"
        obj = self._index.get(q)
        if obj is not None:
            return obj
        words = q.split(" ")
        if len(words) == 2 and words[0] in _GREEK_WORDS:
            obj = self._index.get(f"{_GREEK_WORDS[words[0]]} {words[1]}")
            if obj is not None:
                return obj
        # Coincidencia parcial: el objeto más brillante cuyo nombre contenga el texto.
        best = None
        for key, candidate in self._index.items():
            if q in key:
                if best is None or (candidate.mag or 99) < (best.mag or 99):
                    best = candidate
        return best

    def search(self, prefix: str, limit: int = 10) -> list[dict]:
        """Sugerencias para autocompletar."""
        q = normalize(prefix)
        if not q:
            return []
        results: list[dict] = []
        seen: set[str] = set()
        for key in BODIES:
            name = BODY_INFO[key]["name"]
            if normalize(name).startswith(q) or key.startswith(q):
                results.append({"label": name, "kind": "body", "detail": "Sistema solar"})
                seen.add(key)
        m = re.fullmatch(r"(m|ngc|ic)\s*0*(\d*)", q)
        if m:
            q = f"{m.group(1)} {m.group(2)}".strip()
        matches = []
        for key, obj in self._index.items():
            if key.startswith(q) and obj.id not in seen:
                matches.append(obj)
                seen.add(obj.id)
        matches.sort(key=lambda o: (o.kind != "dso" or not o.id.startswith("M "), o.mag if o.mag is not None else 99))
        for obj in matches[: max(0, limit - len(results))]:
            results.append({"label": obj.id if obj.kind == "dso" else obj.name.split(" (")[0],
                            "kind": obj.kind, "detail": obj.name if obj.kind == "dso" else obj.type_label})
        return results[:limit]

    # ------------------------------------------------------------------ búsqueda por región
    def cone(self, ra: float, dec: float, radius: float, star_mag_limit: float = 6.5,
             dso_mag_limit: float = 12.0, max_stars: int = 2000) -> tuple[list[SkyObject], list[SkyObject]]:
        """Estrellas y DSO a menos de ``radius`` grados de (ra, dec)."""
        center = _unit_vectors([ra], [dec])[0]
        cos_r = math.cos(math.radians(radius))
        star_mask = (self._star_xyz @ center >= cos_r) & (self._star_mag <= star_mag_limit)
        idx = np.nonzero(star_mask)[0]
        idx = idx[np.argsort(self._star_mag[idx])][:max_stars]
        stars = [self.stars[i] for i in idx]
        dso_mask = self._dso_xyz @ center >= cos_r
        # Los DSO grandes pueden asomar aunque su centro esté fuera del radio.
        dsos = [self.dsos[i] for i in np.nonzero(dso_mask)[0]
                if (self.dsos[i].mag is None or self.dsos[i].mag <= dso_mag_limit)]
        return stars, dsos

    def bodies_near(self, ra: float, dec: float, radius: float, jd: float,
                    site: Site | None = None) -> list[SkyObject]:
        out = []
        center = _unit_vectors([ra], [dec])[0]
        for key in BODIES:
            obj = self.body_object(key, jd, site)
            v = _unit_vectors([obj.ra], [obj.dec])[0]
            sep = math.degrees(math.acos(max(-1.0, min(1.0, float(v @ center)))))
            if sep <= radius + (obj.size_arcmin or 0) / 120.0:
                out.append(obj)
        return out

    def objects_in_field(self, geom: FieldGeometry, jd: float, site: Site | None = None,
                         star_mag_limit: float = 6.5, margin: float = 0.0) -> list[dict]:
        """Objetos dentro del rectángulo del campo, con su posición normalizada (0..1)."""
        norm = geom.normalized()
        stars, dsos = self.cone(geom.ra, geom.dec, geom.radius * (1 + margin) + 0.01, star_mag_limit)
        extra = [d for d in self._large_dsos_near(geom) if d not in dsos]
        candidates = self.bodies_near(geom.ra, geom.dec, geom.radius, jd, site) + dsos + extra + stars
        out = []
        for obj in candidates:
            p = norm.to_pixel(obj.ra, obj.dec)
            if p is None:
                continue
            half = (obj.size_arcmin or 0) / 60.0 / 2.0
            mx = margin + half / geom.fov_w
            my = margin + half / geom.fov_h
            if -mx <= p[0] <= 1 + mx and -my <= p[1] <= 1 + my:
                d = {"id": obj.id, "name": obj.name, "kind": obj.kind, "type": obj.type_label,
                     "mag": obj.mag, "size_arcmin": obj.size_arcmin, "x": p[0], "y": p[1],
                     "ra": obj.ra, "dec": obj.dec, "body": obj.body}
                out.append(d)
        return out

    def _large_dsos_near(self, geom: FieldGeometry) -> list[SkyObject]:
        """DSO extensos (p. ej. M31, Pléyades) cuyo centro queda fuera del cono."""
        center = _unit_vectors([geom.ra], [geom.dec])[0]
        out = []
        for i in self._big_dso_idx:
            d = self.dsos[i]
            sep = math.degrees(math.acos(max(-1.0, min(1.0, float(self._dso_xyz[i] @ center)))))
            if sep <= geom.radius + d.size_arcmin / 120.0:
                out.append(d)
        return out

    def objects_near(self, ra: float, dec: float, radius: float, star_mag_limit: float,
                     jd: float, site: Site | None = None, max_stars: int = 1500) -> dict:
        """Datos para dibujar un mapa del cielo alrededor de (ra, dec)."""
        stars, dsos = self.cone(ra, dec, radius, star_mag_limit, dso_mag_limit=11.0, max_stars=max_stars)
        bodies = self.bodies_near(ra, dec, radius, jd, site)
        return {
            "stars": [[round(s.ra, 4), round(s.dec, 4), s.mag, s.name if (s.mag or 9) < 3.5 else None]
                      for s in stars],
            "dsos": [[round(d.ra, 4), round(d.dec, 4), d.mag, d.id, d.type_label, d.size_arcmin]
                     for d in dsos],
            "bodies": [[round(b.ra, 4), round(b.dec, 4), b.mag, b.name, b.size_arcmin] for b in bodies],
        }


def _unit_vectors(ra: list[float], dec: list[float]) -> np.ndarray:
    ra_r = np.radians(np.asarray(ra, dtype=float))
    dec_r = np.radians(np.asarray(dec, dtype=float))
    return np.stack([np.cos(dec_r) * np.cos(ra_r), np.cos(dec_r) * np.sin(ra_r), np.sin(dec_r)], axis=-1)


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    return Catalog()
