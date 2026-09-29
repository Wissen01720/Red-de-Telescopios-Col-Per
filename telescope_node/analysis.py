"""Análisis de imagen en el nodo.

A partir de un cuadro de la cámara calcula:
* Fondo de cielo (mapa por bloques) y ruido.
* Detección de estrellas: posición, flujo, FWHM, elongación y SNR.
* Objetos extensos (Luna, planetas, nebulosas, zonas iluminadas).
* Diagnóstico en español: tipo de escena, calidad de enfoque, seguimiento,
  nubes/gradientes y saturación.

El servidor complementa esto con los catálogos para decir *qué* objetos
astronómicos hay en el campo.
"""

from __future__ import annotations

import math
import time

import cv2
import numpy as np

_BOX = 48


def _to_gray(frame: np.ndarray) -> tuple[np.ndarray, float]:
    if frame.ndim == 3:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    saturation = 255.0 if frame.dtype == np.uint8 else 65535.0 if frame.dtype == np.uint16 else float(frame.max())
    return frame.astype(np.float32), saturation


def _robust_std(values: np.ndarray) -> float:
    med = float(np.median(values))
    mad = float(np.median(np.abs(values - med)))
    sigma = 1.4826 * mad
    if sigma <= 0:
        sigma = float(values.std())
    clipped = values[np.abs(values - med) < 5 * max(sigma, 1e-6)]
    if clipped.size > 10:
        sigma = 1.4826 * float(np.median(np.abs(clipped - np.median(clipped))))
    return max(sigma, 1e-3)


def estimate_background(gray: np.ndarray, box: int = _BOX) -> tuple[np.ndarray, np.ndarray]:
    """Mapa de fondo (mediana por bloques, filtrada y reescalada) y la malla de bloques."""
    h, w = gray.shape
    box = max(8, min(box, h, w))
    ny, nx = h // box, w // box
    crop = gray[: ny * box, : nx * box].reshape(ny, box, nx, box)[:, ::4, :, ::4]
    grid = np.median(crop.transpose(0, 2, 1, 3).reshape(ny, nx, -1), axis=2).astype(np.float32)
    if ny >= 3 and nx >= 3:
        grid = cv2.medianBlur(grid, 3)
    bkg = cv2.resize(grid, (w, h), interpolation=cv2.INTER_LINEAR)
    return bkg, grid


def _measure(resid: np.ndarray, gray: np.ndarray, x: int, y: int, bw: int, bh: int,
             noise: float, pad: int = 2) -> dict | None:
    h, w = resid.shape
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
    cut = np.clip(resid[y0:y1, x0:x1] - 0.5 * noise, 0.0, None)
    flux = float(cut.sum())
    if flux <= 0:
        return None
    yy, xx = np.mgrid[y0:y1, x0:x1]
    cx = float((cut * xx).sum() / flux)
    cy = float((cut * yy).sum() / flux)
    mxx = float((cut * (xx - cx) ** 2).sum() / flux)
    myy = float((cut * (yy - cy) ** 2).sum() / flux)
    mxy = float((cut * (xx - cx) * (yy - cy)).sum() / flux)
    tr, det = mxx + myy, mxx * myy - mxy * mxy
    disc = math.sqrt(max(tr * tr / 4 - det, 0.0))
    l1, l2 = tr / 2 + disc, max(tr / 2 - disc, 1e-6)
    # Nitidez: pico frente a sus 4 vecinos. Una estrella muestreada decae suave; un píxel caliente no.
    box = resid[y:y + bh, x:x + bw]
    py, px = np.unravel_index(int(np.argmax(box)), box.shape)
    py, px = py + y, px + x
    neighbors = [resid[j, i] for j, i in ((py - 1, px), (py + 1, px), (py, px - 1), (py, px + 1))
                 if 0 <= j < h and 0 <= i < w]
    sharpness = float(resid[py, px]) / max(float(np.mean(neighbors)), noise)
    return {
        "x": cx, "y": cy, "flux": flux,
        "fwhm": 2.3548 * math.sqrt(max(tr / 2, 0.0)),
        "elongation": math.sqrt(l1 / l2),
        "peak": float(gray[y:y + bh, x:x + bw].max()),
        "snr": flux / (noise * math.sqrt(cut.size)),
        "sharpness": sharpness,
    }


def analyze_frame(frame: np.ndarray, detection_sigma: float = 5.0, max_stars: int = 40,
                  min_area: int = 3, max_components: int = 400, max_fwhm_px: float = 15.0) -> dict:
    t0 = time.perf_counter()
    gray, sat_level = _to_gray(frame)
    h, w = gray.shape
    bkg, grid = estimate_background(gray)
    resid = gray - bkg
    smooth = cv2.GaussianBlur(resid, (0, 0), 1.0)
    noise = _robust_std(resid[::3, ::3].ravel())
    noise_s = _robust_std(smooth[::3, ::3].ravel())
    mask = (smooth > detection_sigma * noise_s).astype(np.uint8)
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)

    max_star_area = max(400, int(0.002 * h * w))
    stars, extended = [], []
    hot_pixels = 0
    comps = stats[1:]
    order = np.argsort(-comps[:, cv2.CC_STAT_AREA])[:max_components] if len(comps) else []
    for i in order:
        x, y, bw, bh, area = (int(v) for v in comps[i])
        if area < min_area:
            continue
        region = gray[y:y + bh, x:x + bw]
        big = area > max_star_area or bw > 60 or bh > 60
        if big or (area > 150 and region.max() >= 0.98 * sat_level):
            extended.append({
                "x": (x + bw / 2) / w, "y": (y + bh / 2) / h, "w": bw / w, "h": bh / h,
                "area_fraction": area / (h * w),
                "fill": area / (bw * bh),
                "aspect": bw / bh,
                "saturated": bool((region >= 0.98 * sat_level).mean() > 0.2),
                "mean_level": float(resid[y:y + bh, x:x + bw].mean()),
            })
            continue
        m = _measure(resid, gray, x, y, bw, bh, noise)
        if m is None:
            continue
        if m["fwhm"] < 1.0 or m["sharpness"] > 5.0:  # demasiado puntual: píxel caliente o rayo cósmico
            hot_pixels += 1
            continue
        if m["fwhm"] > max_fwhm_px or m["peak"] - float(bkg[int(m["y"]), int(m["x"])]) < detection_sigma * noise:
            continue  # mancha difusa de poco contraste (borde de nube, gradiente), no una estrella
        m["saturated"] = m["peak"] >= 0.98 * sat_level
        stars.append(m)

    stars.sort(key=lambda s: -s["flux"])
    good = [s for s in stars if not s["saturated"] and s["snr"] > 10]
    fwhm = float(np.median([s["fwhm"] for s in good])) if good else None
    elong = float(np.median([s["elongation"] for s in good])) if good else None
    bkg_med = float(np.median(grid))
    bkg_var = float((np.percentile(grid, 90) - np.percentile(grid, 10)) / max(bkg_med, 1.0))
    saturated_fraction = float((gray[::2, ::2] >= 0.98 * sat_level).mean())

    result = {
        "width": w, "height": h,
        "background": bkg_med,
        "background_norm": bkg_med / sat_level,
        "background_variation": bkg_var,
        "noise": noise,
        "n_stars": len(stars),
        "hot_pixels": hot_pixels,
        "fwhm_px": fwhm,
        "elongation": elong,
        "saturated_fraction": saturated_fraction,
        "extended": extended[:10],
        "stars": [
            {"x": s["x"] / w, "y": s["y"] / h, "flux": round(s["flux"], 1), "fwhm": round(s["fwhm"], 2),
             "snr": round(s["snr"], 1), "saturated": s["saturated"]}
            for s in stars[:max_stars]
        ],
    }
    result.update(classify(result))
    result["processing_ms"] = (time.perf_counter() - t0) * 1000.0
    return result


def classify(r: dict) -> dict:
    """Interpreta las métricas y produce un diagnóstico legible."""
    n = r["n_stars"]
    notes: list[str] = []
    big = [e for e in r["extended"] if e["area_fraction"] > 0.03]
    compact = [e for e in r["extended"]
               if e["area_fraction"] <= 0.03 and e["fill"] > 0.6 and 0.7 < e["aspect"] < 1.4 and e["saturated"]]
    diffuse = [e for e in r["extended"] if not e["saturated"] and e["area_fraction"] > 0.002]

    if r["background_norm"] > 0.55:
        scene, label = "cielo_brillante", "Cielo muy brillante: de día, en el crepúsculo o sobreexpuesto"
    elif big and any(e["saturated"] or e["mean_level"] > 60 for e in big):
        scene, label = "luna", "Objeto brillante y extenso: probablemente la Luna"
    elif compact and n < 15:
        scene, label = "objeto_brillante", "Objeto brillante compacto: planeta o estrella muy brillante"
    elif n == 0:
        scene, label = "sin_estrellas", ("No se detectan estrellas: nubes, tapa puesta, "
                                         "desenfoque o exposición insuficiente")
    elif n < 5:
        scene, label = "pocas_estrellas", f"Pocas estrellas ({n}): posibles nubes o exposición corta"
    else:
        scene, label = "campo_estelar", f"Campo estelar con {n} estrellas detectadas"
        if compact:
            notes.append("Hay un objeto brillante compacto (planeta o estrella muy brillante)")

    # Con nubes o gradientes fuertes las métricas de estrellas no son confiables.
    irregular = r["background_variation"] > 0.25 and scene not in ("luna", "cielo_brillante")
    if irregular:
        notes.append("Fondo irregular: posibles nubes o gradiente de luz")
    elif diffuse and scene in ("campo_estelar", "pocas_estrellas"):
        notes.append("Hay un objeto difuso extenso (posible nebulosa, galaxia o cúmulo)")

    fwhm = r["fwhm_px"]
    if fwhm is None:
        focus = "Sin datos"
    elif irregular:
        focus = "Incierto"
    elif fwhm < 2.5:
        focus = "Bueno"
    elif fwhm < 4.5:
        focus = "Aceptable"
    else:
        focus = "Desenfocado"
        notes.append(f"Estrellas grandes (FWHM {fwhm:.1f} px): revisar el enfoque")

    if r["elongation"] is None:
        tracking = "Sin datos"
    elif r["elongation"] > 1.6 and not irregular:
        tracking = "Estrellas alargadas"
        notes.append("Estrellas alargadas: revisar seguimiento, viento o vibraciones")
    else:
        tracking = "Correcto"

    if r["saturated_fraction"] > 0.05 and scene != "cielo_brillante":
        notes.append("Imagen parcialmente saturada: reducir exposición o ganancia")

    return {"scene": scene, "scene_label": label, "focus": focus, "tracking_quality": tracking, "notes": notes}
