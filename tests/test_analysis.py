"""Análisis de imagen sobre imágenes sintéticas con verdad conocida."""

import math
from datetime import datetime, timezone

import numpy as np
import pytest

from astrocore.catalog import default_catalog
from astrocore.coords import Site, julian_date
from astrocore.field import FieldGeometry
from telescope_node.analysis import analyze_frame
from telescope_node.camera.simulated import FieldPose, SimulatedCamera


def synthetic(stars, sigma=1.5, size=(480, 640), sky=20.0, noise=3.0, seed=1, hot=()):
    rng = np.random.default_rng(seed)
    h, w = size
    img = np.full(size, sky, dtype=np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    for x, y, peak in stars:
        img += peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma ** 2))
    img += rng.normal(0, noise, size)
    for x, y in hot:
        img[y, x] = 250
    return np.clip(img, 0, 255).astype(np.uint8)


def test_detects_stars_with_accurate_positions_and_fwhm():
    truth = [(100.3, 80.7, 150), (320.0, 240.0, 90), (500.6, 400.2, 60), (50.0, 420.0, 120), (600.0, 30.0, 40)]
    result = analyze_frame(synthetic(truth, hot=[(200, 200), (400, 100)]))
    assert result["n_stars"] == len(truth)
    assert result["hot_pixels"] >= 1
    for x, y, _ in truth:
        best = min(result["stars"], key=lambda s: math.hypot(s["x"] * 640 - x, s["y"] * 480 - y))
        assert math.hypot(best["x"] * 640 - x, best["y"] * 480 - y) < 0.35
    assert result["fwhm_px"] == pytest.approx(2.3548 * 1.5, abs=0.6)
    assert result["focus"] in ("Bueno", "Aceptable")
    assert result["scene"] == "campo_estelar"


def test_empty_sky_reports_no_stars():
    result = analyze_frame(synthetic([]))
    assert result["n_stars"] == 0
    assert result["scene"] == "sin_estrellas"


def test_bright_sky_is_classified():
    result = analyze_frame(synthetic([], sky=220))
    assert result["scene"] == "cielo_brillante"


def test_moon_disc_is_classified():
    img = synthetic([]).astype(np.float32)
    yy, xx = np.mgrid[0:480, 0:640]
    img[(xx - 320) ** 2 + (yy - 240) ** 2 < 150 ** 2] = 255
    result = analyze_frame(img.astype(np.uint8))
    assert result["scene"] == "luna"


def test_color_frames_are_supported():
    gray = synthetic([(320, 240, 120)])
    result = analyze_frame(np.dstack([gray, gray, gray]))
    assert result["n_stars"] == 1


def test_simulated_camera_clouds_reduce_detections():
    cat = default_catalog()
    site = Site(-12.05, -77.04, 150)
    jd = julian_date(datetime(2026, 12, 15, 4, 0, tzinfo=timezone.utc))
    m45 = cat.resolve("M45", jd)
    pose = FieldPose(FieldGeometry(m45.ra, m45.dec, 1.7, 1.28, 20.0, False, 1280, 960), jd, site)
    counts = []
    for clouds in (0.0, 0.7):
        cam = SimulatedCamera(1280, 960, 200, 4, lambda: pose, cat, clouds=clouds, seed=1, realtime=False)
        counts.append(analyze_frame(cam.render(pose))["n_stars"])
    assert counts[0] > 20
    assert counts[1] < counts[0]
