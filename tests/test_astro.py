"""Matemática astronómica: se compara contra astropy cuando está instalado."""

import math
import random
from datetime import datetime, timezone

import pytest

from astrocore.catalog import default_catalog
from astrocore.constellations import NAMES_ES, _bounds, constellation_of
from astrocore.coords import (J2000, Site, altaz_to_radec, angular_separation, deproject, format_dec, format_ra,
                              gmst_deg, julian_date, parse_coordinates, precess, project, radec_to_altaz,
                              refraction_apparent_to_true, refraction_true_to_apparent)
from astrocore.ephemeris import PLANETS, body_position, moon_phase
from astrocore.field import FieldGeometry

BOGOTA = Site(4.638, -74.084, 2600)
LIMA = Site(-12.046, -77.043, 150)
WHEN = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)


def astropy_or_skip():
    return pytest.importorskip("astropy")


# --------------------------------------------------------------------------- coordenadas

def test_gmst_matches_astropy():
    astropy_or_skip()
    from astropy.time import Time

    t = Time(WHEN)
    diff = (gmst_deg(t.jd) - t.sidereal_time("mean", "greenwich").deg + 180) % 360 - 180
    assert abs(diff) * 240 < 0.1  # segundos de tiempo


@pytest.mark.parametrize("ra,dec", [(83.82, -5.39), (279.23, 38.78), (101.29, -16.72), (201.7, -47.5), (37.95, 89.26)])
@pytest.mark.parametrize("site", [BOGOTA, LIMA])
def test_radec_to_altaz_matches_astropy(ra, dec, site):
    astropy_or_skip()
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    t = Time(WHEN)
    loc = EarthLocation(lat=site.latitude * u.deg, lon=site.longitude * u.deg, height=site.elevation_m * u.m)
    ref = SkyCoord(ra * u.deg, dec * u.deg).transform_to(AltAz(obstime=t, location=loc, pressure=0))
    alt, az = radec_to_altaz(ra, dec, site, t.jd)
    err = math.hypot(alt - ref.alt.deg, ((az - ref.az.deg + 180) % 360 - 180) * math.cos(math.radians(alt)))
    assert err < 0.012  # < 45": se omiten nutación y aberración


def test_altaz_roundtrip_with_refraction():
    jd = julian_date(WHEN)
    for ra, dec in [(10.0, -30.0), (200.0, 10.0), (300.0, -60.0)]:
        alt, az = radec_to_altaz(ra, dec, LIMA, jd, refraction=True)
        ra2, dec2 = altaz_to_radec(alt, az, LIMA, jd, refraction=True)
        assert angular_separation(ra, dec, ra2, dec2) < 2 / 3600


def test_refraction_formulas_are_consistent():
    for alt in (5, 15, 30, 60, 85):
        apparent = alt + refraction_true_to_apparent(alt)
        assert abs(apparent - refraction_apparent_to_true(apparent) - alt) < 0.3 / 60
    assert 0.4 < refraction_true_to_apparent(0.0) < 0.6


def test_precession_matches_astropy_fk5():
    astropy_or_skip()
    import astropy.units as u
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time

    t = Time(WHEN)
    for ra, dec in [(83.82, -5.39), (201.7, -47.5), (10.68, 41.27)]:
        ref = SkyCoord(ra * u.deg, dec * u.deg, frame=FK5(equinox="J2000")).transform_to(FK5(equinox=t))
        ra2, dec2 = precess(ra, dec, J2000, t.jd)
        assert angular_separation(ra2, dec2, ref.ra.deg, ref.dec.deg) < 1 / 3600


def test_project_deproject_roundtrip():
    rng = random.Random(3)
    for _ in range(200):
        ra0, dec0 = rng.uniform(0, 360), rng.uniform(-85, 85)
        xi, eta = rng.uniform(-3, 3), rng.uniform(-3, 3)
        ra, dec = deproject(xi, eta, ra0, dec0)
        xi2, eta2 = project(ra, dec, ra0, dec0)
        assert abs(xi - xi2) < 1e-9 and abs(eta - eta2) < 1e-9


@pytest.mark.parametrize("text", ["05:35:17 -05:23:28", "5h35m17s -5d23m28s", "5 35 17 -5 23 28",
                                  "83.8208, -5.3911", "83.8208 -5.3911"])
def test_parse_coordinates(text):
    ra, dec = parse_coordinates(text)
    assert abs(ra - 83.8208) < 1e-3 and abs(dec + 5.3911) < 1e-3


def test_parse_coordinates_rejects_names():
    for text in ["M 42", "NGC 224", "58 Ori", "Júpiter"]:
        assert parse_coordinates(text) is None


def test_formatting():
    assert format_ra(83.82083) == "05h 35m 17.0s"
    assert format_dec(-5.39111) == "-05° 23′ 28″"


# --------------------------------------------------------------------------- campo de visión

def test_field_orientation_north_up_east_left():
    g = FieldGeometry(100.0, 20.0, 1.0, 0.75, rotation_deg=0.0, width=1000, height=750)
    cx, cy = g.to_pixel(100.0, 20.0)
    assert abs(cx - 500) < 1e-6 and abs(cy - 375) < 1e-6
    _, y_north = g.to_pixel(100.0, 20.2)
    x_east, _ = g.to_pixel(100.2, 20.0)
    assert y_north < cy and x_east < cx
    flipped = FieldGeometry(100.0, 20.0, 1.0, 0.75, flip_x=True, width=1000, height=750)
    assert flipped.to_pixel(100.2, 20.0)[0] > cx


@pytest.mark.parametrize("rotation,flip", [(0, False), (37, False), (-120, True)])
def test_field_pixel_roundtrip(rotation, flip):
    g = FieldGeometry(250.0, -40.0, 1.5, 1.1, rotation, flip, 1280, 960)
    for x, y in [(0, 0), (1280, 0), (640, 480), (100, 900)]:
        ra, dec = g.to_sky(x, y)
        x2, y2 = g.to_pixel(ra, dec)
        assert abs(x - x2) < 1e-6 and abs(y - y2) < 1e-6
    assert len(g.corners()) == 4


# --------------------------------------------------------------------------- efemérides

@pytest.mark.parametrize("body", ("sun",) + PLANETS)
def test_planets_match_astropy(body):
    astropy_or_skip()
    from astropy.coordinates import get_body
    from astropy.time import Time

    t = Time(WHEN)
    ref = get_body(body, t)
    pos = body_position(body, t.jd)
    assert angular_separation(pos.ra, pos.dec, ref.ra.deg, ref.dec.deg) * 60 < 6.0  # minutos de arco


def test_moon_geocentric_and_topocentric_match_astropy():
    astropy_or_skip()
    import astropy.units as u
    from astropy.coordinates import EarthLocation, get_body
    from astropy.time import Time

    t = Time(WHEN)
    geo = body_position("moon", t.jd)
    ref = get_body("moon", t)
    assert angular_separation(geo.ra, geo.dec, ref.ra.deg, ref.dec.deg) * 60 < 1.0
    assert abs(geo.distance_km - ref.distance.to(u.km).value) < 300
    for site in (BOGOTA, LIMA):
        loc = EarthLocation(lat=site.latitude * u.deg, lon=site.longitude * u.deg, height=site.elevation_m * u.m)
        topo = body_position("moon", t.jd, site)
        ref_topo = get_body("moon", t, loc)
        assert angular_separation(topo.ra, topo.dec, ref_topo.ra.deg, ref_topo.dec.deg) * 60 < 1.0


def test_moon_parallax_differs_between_sites():
    jd = julian_date(WHEN)
    a, b = body_position("moon", jd, BOGOTA), body_position("moon", jd, LIMA)
    assert 0.05 < angular_separation(a.ra, a.dec, b.ra, b.dec) < 1.0


def test_moon_phase_is_consistent_with_elongation():
    jd = julian_date(WHEN)
    phase = moon_phase(jd)
    assert 0.0 <= phase["illuminated"] <= 1.0
    sun, moon = body_position("sun", jd), body_position("moon", jd)
    elong = angular_separation(sun.ra, sun.dec, moon.ra, moon.dec)
    assert abs(phase["illuminated"] - (1 - math.cos(math.radians(elong))) / 2) < 0.02


# --------------------------------------------------------------------------- constelaciones

def test_constellation_table_has_all_88():
    abbrs = {row[3] for row in _bounds()}
    assert abbrs == set(NAMES_ES) and len(abbrs) == 88


@pytest.mark.parametrize("name,con", [("Betelgeuse", "Ori"), ("Sirio", "CMa"), ("Polaris", "UMi"),
                                      ("Acrux", "Cru"), ("Omega Centauri", "Cen"), ("M 31", "And")])
def test_constellation_of_known_objects(name, con):
    obj = default_catalog().resolve(name, julian_date(WHEN))
    assert constellation_of(obj.ra, obj.dec) == con


def test_constellations_match_astropy():
    astropy_or_skip()
    import astropy.units as u
    from astropy.coordinates import SkyCoord, get_constellation

    rng = random.Random(7)
    points = [(rng.uniform(0, 360), math.degrees(math.asin(rng.uniform(-1, 1)))) for _ in range(800)]
    ref = get_constellation(SkyCoord([p[0] for p in points] * u.deg, [p[1] for p in points] * u.deg), short_name=True)
    mismatches = sum(constellation_of(ra, dec) != r for (ra, dec), r in zip(points, ref))
    assert mismatches <= 2  # solo puntos a segundos de arco de un límite (astropy incluye aberración)


# --------------------------------------------------------------------------- catálogo

@pytest.mark.parametrize("query,expected_id", [
    ("M42", "M 42"), ("m 31", "M 31"), ("NGC1976", "M 42"), ("Nebulosa de Orión", "M 42"),
    ("pleyades", "M 45"), ("Omega Centauri", "NGC 5139"), ("Sirio", "HIP 32349"), ("alfa ori", "HIP 27989"),
    ("α Ori", "HIP 27989"), ("Betelgeuse", "HIP 27989"), ("estrella polar", "HIP 11767"),
    ("Júpiter", "jupiter"), ("luna", "moon"),
])
def test_resolve_names(query, expected_id):
    obj = default_catalog().resolve(query, julian_date(WHEN))
    assert obj is not None and obj.id == expected_id


def test_resolve_coordinates_and_unknown():
    cat = default_catalog()
    obj = cat.resolve("05:35:17 -05:23:28", julian_date(WHEN))
    assert obj.kind == "coords" and abs(obj.ra - 83.8208) < 1e-3
    assert cat.resolve("objeto-inexistente-xyz", julian_date(WHEN)) is None


def test_search_suggestions():
    labels = [s["label"] for s in default_catalog().search("ju")]
    assert "Júpiter" in labels
    assert default_catalog().search("m4")[0]["label"].startswith("M 4")


def test_objects_in_field_centered():
    cat = default_catalog()
    m42 = cat.resolve("M42", julian_date(WHEN))
    geom = FieldGeometry(m42.ra, m42.dec, 1.5, 1.1, 0.0)
    found = {o["id"]: o for o in cat.objects_in_field(geom, julian_date(WHEN))}
    assert "M 42" in found and abs(found["M 42"]["x"] - 0.5) < 1e-6
    assert all(-0.2 <= o["x"] <= 1.2 and -0.2 <= o["y"] <= 1.2 for o in found.values())
