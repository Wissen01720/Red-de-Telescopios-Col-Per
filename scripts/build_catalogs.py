"""Descarga y filtra los catálogos públicos que usa el sistema.

Genera los archivos de ``astrocore/data/``:

* ``stars.csv``      Estrellas hasta magnitud 6.5 (HYG v4.1, CC BY-SA 4.0).
* ``dso.csv``        Objetos de cielo profundo: Messier, con nombre común o
                     más brillantes que magnitud 10 (OpenNGC, CC BY-SA 4.0).
* ``constellation_bounds.dat``  Límites IAU de constelaciones, equinoccio
                     B1875 (Roman 1987, VizieR VI/42).

Los archivos generados ya vienen incluidos en el repositorio; este script
solo hace falta para regenerarlos o cambiar los límites de magnitud.

Uso:  python scripts/build_catalogs.py
"""

from __future__ import annotations

import csv
import io
import re
import sys
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "astrocore" / "data"

HYG_URL = "https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v41.csv"
NGC_URL = "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv"
ADDENDUM_URL = "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/addendum.csv"
CONST_URL = "https://cdsarc.cds.unistra.fr/ftp/VI/42/data.dat"

STAR_MAG_LIMIT = 6.5
DSO_MAG_LIMIT = 10.0

# Tipos de OpenNGC que no son objetos de cielo profundo útiles para apuntar.
DSO_SKIP_TYPES = {"Dup", "NonEx", "*", "**", "Other", "Nova"}


def download(url: str) -> str:
    print(f"Descargando {url} ...")
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read().decode("utf-8")


def build_stars() -> int:
    reader = csv.DictReader(io.StringIO(download(HYG_URL)))
    out_rows = []
    for row in reader:
        if row["id"] == "0" or not row["mag"]:  # id 0 es el Sol
            continue
        mag = float(row["mag"])
        if mag > STAR_MAG_LIMIT:
            continue
        out_rows.append({
            "hip": row["hip"],
            "hd": row["hd"],
            "name": row["proper"],
            "bayer": row["bayer"],
            "flam": row["flam"],
            "con": row["con"],
            "ra": f"{float(row['ra']) * 15.0:.6f}",
            "dec": f"{float(row['dec']):.6f}",
            "mag": f"{mag:.2f}",
            "spect": row["spect"],
        })
    out_rows.sort(key=lambda r: float(r["mag"]))
    with open(DATA_DIR / "stars.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)
    return len(out_rows)


def _sexagesimal(text: str, hours: bool) -> float:
    sign = -1.0 if text.strip().startswith("-") else 1.0
    parts = [float(p) for p in text.strip().lstrip("+-").split(":")]
    value = parts[0] + parts[1] / 60.0 + (parts[2] if len(parts) > 2 else 0.0) / 3600.0
    return sign * value * (15.0 if hours else 1.0)


def _pretty_name(raw: str) -> str:
    """'NGC0224' -> 'NGC 224', 'IC0434' -> 'IC 434', 'Mel022' -> 'Mel 22'."""
    m = re.match(r"^([A-Za-z]+)0*(\d+)(.*)$", raw)
    if not m:
        return raw
    return f"{m.group(1)} {m.group(2)}{m.group(3)}"


def build_dso() -> int:
    rows = []
    for url in (NGC_URL, ADDENDUM_URL):
        rows += list(csv.DictReader(io.StringIO(download(url)), delimiter=";"))
    out_rows = []
    for row in rows:
        if not row["RA"] or not row["Dec"]:
            continue
        messier = int(row["M"]) if row["M"] else None
        if row["Type"] == "Dup" or (row["Type"] in DSO_SKIP_TYPES and messier is None):
            continue
        vmag = float(row["V-Mag"]) if row["V-Mag"] else None
        bmag = float(row["B-Mag"]) if row["B-Mag"] else None
        mag = vmag if vmag is not None else bmag
        bright = mag is not None and mag <= DSO_MAG_LIMIT + (0.5 if vmag is None else 0.0)
        if not (messier or row["Common names"] or bright):
            continue
        out_rows.append({
            "id": _pretty_name(row["Name"]),
            "messier": messier or "",
            "type": row["Type"],
            "ra": f"{_sexagesimal(row['RA'], hours=True):.5f}",
            "dec": f"{_sexagesimal(row['Dec'], hours=False):.5f}",
            "mag": f"{mag:.2f}" if mag is not None else "",
            "major_arcmin": row["MajAx"],
            "minor_arcmin": row["MinAx"],
            "pa_deg": row["PosAng"],
            "con": row["Const"],
            "common": row["Common names"].replace(",", "|"),
        })
    out_rows.sort(key=lambda r: (r["messier"] == "", int(r["messier"] or 0), r["id"]))
    with open(DATA_DIR / "dso.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)
    return len(out_rows)


def build_constellations() -> int:
    text = download(CONST_URL)
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    (DATA_DIR / "constellation_bounds.dat").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Estrellas: {build_stars()}")
    print(f"Objetos de cielo profundo: {build_dso()}")
    print(f"Tramos de límites de constelaciones: {build_constellations()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
