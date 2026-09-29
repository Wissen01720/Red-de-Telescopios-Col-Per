"""Coordinación de los telescopios y análisis enriquecido con catálogos.

* GoTo sincronizado: los nodos reciben el mismo objetivo, la misma hora de
  inicio (``start_at``) y una duración común para llegar a la vez.
* Desplazamientos (N/S/E/O) manteniendo ambos telescopios en el mismo cuadrante.
* "Qué se está viendo": objetos de catálogo dentro del campo de cada cámara,
  constelación, condiciones del cielo y resumen legible.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrocore.catalog import Catalog, SkyObject
from astrocore.constellations import constellation_name, constellation_of
from astrocore.coords import (angular_separation, format_dec, format_ra, julian_date, offset_by, project,
                              radec_to_altaz, wrap180)
from astrocore.ephemeris import PLANETS, body_position, moon_phase, twilight_label
from astrocore.field import FieldGeometry

from .hub import Hub, NodeSession

log = logging.getLogger(__name__)

START_DELAY_S = 1.5   # margen para que el comando llegue a todos los nodos antes de arrancar
NUDGE_DIRECTIONS = {"N": (0.0, 1.0), "S": (0.0, -1.0), "E": (1.0, 0.0), "O": (-1.0, 0.0), "W": (-1.0, 0.0)}


class Coordinator:
    def __init__(self, hub: Hub, catalog: Catalog):
        self.hub = hub
        self.catalog = catalog
        self.common_target: dict | None = None

    # ------------------------------------------------------------------ objetivos
    def resolve(self, target: str | dict, t: float | None = None) -> SkyObject | None:
        jd = julian_date(t)
        if isinstance(target, dict):
            if target.get("body"):
                return self.catalog.body_object(target["body"], jd)
            return SkyObject(id="coords", name=target.get("name") or f"{format_ra(target['ra'], 0)} "
                             f"{format_dec(target['dec'])}", kind="coords",
                             ra=float(target["ra"]), dec=float(target["dec"]), type_label="Coordenadas")
        return self.catalog.resolve(str(target), jd)

    def visibility(self, obj: SkyObject, session: NodeSession, t: float) -> dict:
        site = session.site
        if site is None:
            return {"ok": False, "reason": "Sin datos del sitio"}
        jd = julian_date(t)
        if obj.body:
            pos = body_position(obj.body, jd, site)
            ra, dec = pos.ra, pos.dec
        else:
            ra, dec = obj.ra, obj.dec
        alt, az = radec_to_altaz(ra, dec, site, jd, refraction=True)
        sun = body_position("sun", jd)
        sun_sep = angular_separation(ra, dec, sun.ra, sun.dec)
        mount = session.info.get("mount", {})
        reason = None
        if alt < session.min_altitude:
            reason = f"a {alt:.1f}° de altura (mínimo {session.min_altitude:.0f}°)"
        elif sun_sep < mount.get("sun_avoidance_deg", 20.0) and not mount.get("solar_filter"):
            reason = f"a {sun_sep:.0f}° del Sol"
        return {"ok": reason is None, "reason": reason, "alt": alt, "az": az, "sun_sep": sun_sep}

    def _slew_estimate(self, session: NodeSession, alt: float, az: float) -> float:
        tel = session.telemetry or {}
        p = tel.get("pointing")
        if not p:
            return 30.0
        dist = max(abs(alt - p["alt"]), abs(wrap180(az - p["az"])))
        return dist / max(session.max_rate, 0.1)

    @staticmethod
    def _target_message(obj: SkyObject, name: str | None = None) -> dict:
        display = name or (obj.name if obj.kind == "coords" else obj.name.split(" (")[0])
        msg = {"name": display, "ra": obj.ra, "dec": obj.dec}
        if obj.body:
            msg["body"] = obj.body
        return msg

    # ------------------------------------------------------------------ GoTo
    async def goto(self, target: str | dict, node_ids: list[str] | None, name: str | None = None) -> dict:
        t = time.time()
        obj = self.resolve(target, t)
        if obj is None:
            return {"ok": False, "message": f"No se encontró el objeto '{target}'"}
        sessions = self.hub.online(node_ids)
        if not sessions:
            return {"ok": False, "message": "No hay telescopios conectados para ese comando"}

        plans, details = [], []
        for s in sessions:
            vis = self.visibility(obj, s, t)
            if not vis["ok"]:
                details.append({"node": s.node_id, "ok": False, "message": f"{obj.name} está {vis['reason']}"})
                self.hub.log("warning", f"{s.name}: no se envía GoTo, {obj.name} está {vis['reason']}", s.node_id)
                continue
            plans.append((s, self._slew_estimate(s, vis["alt"], vis["az"])))

        msg = self._target_message(obj, name)
        synced = len(plans) > 1
        if synced:
            # Misma hora de inicio y misma duración: los telescopios arrancan y llegan a la vez.
            msg["start_at"] = t + START_DELAY_S
            msg["duration"] = round(max(eta for _, eta in plans) + 0.5, 2)
        results = await asyncio.gather(*(s.command({"type": "goto", **msg}) for s, _ in plans))
        for (s, _), (ok, message) in zip(plans, results):
            details.append({"node": s.node_id, "ok": ok, "message": message})
            self.hub.log("info" if ok else "warning", f"{s.name}: {message}", s.node_id)

        accepted = [d for d in details if d["ok"]]
        if accepted:
            self.common_target = {**msg, "id": obj.id, "full_name": obj.name, "type": obj.type_label,
                                  "mag": obj.mag, "nodes": [d["node"] for d in accepted], "set_at": t}
            self.common_target.pop("start_at", None)
        text = f"GoTo a {obj.name}" + (f" sincronizado ({msg['duration']:.0f} s)" if synced else "")
        if accepted:
            self.hub.log("info", text)
        if not accepted:
            reasons = "; ".join(f"{self.hub.nodes[d['node']].name}: {d['message']}" for d in details)
            text = f"Ningún telescopio puede ir a {obj.name}. {reasons}"
        return {"ok": bool(accepted), "message": text, "details": details, "target": obj.to_dict()}

    async def nudge(self, direction: str, arcmin: float, node_ids: list[str] | None) -> dict:
        if direction.upper() not in NUDGE_DIRECTIONS:
            return {"ok": False, "message": f"Dirección inválida: {direction}"}
        east, north = NUDGE_DIRECTIONS[direction.upper()]
        step = float(arcmin) / 60.0
        sessions = self.hub.online(node_ids)
        if not sessions:
            return {"ok": False, "message": "No hay telescopios conectados"}
        # Se desplaza desde el objetivo actual (el común si se mueven juntos), nunca desde la
        # posición de inicio: esta apunta al horizonte y el resultado quedaría bajo el límite.
        if len(sessions) > 1:
            base = self.common_target
        else:
            base = (sessions[0].telemetry or {}).get("target")
        if not base:
            return {"ok": False, "message": "Primero apunta a un objeto: los botones de mover desplazan "
                                            "el campo a partir del objetivo actual."}
        ra, dec = offset_by(base["ra"], base["dec"], east * step, north * step)
        label = f"{format_ra(ra, 0)} {format_dec(dec)}"
        return await self.goto({"ra": ra, "dec": dec, "name": label}, [s.node_id for s in sessions], name=label)

    async def broadcast(self, command: str, node_ids: list[str] | None, **fields) -> dict:
        sessions = self.hub.online(node_ids)
        if not sessions:
            return {"ok": False, "message": "No hay telescopios conectados"}
        results = await asyncio.gather(*(s.command({"type": command, **fields}) for s in sessions))
        details = [{"node": s.node_id, "ok": ok, "message": m} for s, (ok, m) in zip(sessions, results)]
        for s, (ok, m) in zip(sessions, results):
            self.hub.log("info" if ok else "warning", f"{s.name}: {m}", s.node_id)
        if command in ("stop", "park"):
            self.common_target = None
        return {"ok": all(d["ok"] for d in details), "message": "; ".join(d["message"] for d in details),
                "details": details}

    async def center(self, node_id: str, x: float, y: float) -> dict:
        """Centra en un telescopio el punto (x, y) de su imagen (coordenadas normalizadas 0..1)."""
        session = self.hub.nodes.get(node_id)
        tel = session.telemetry if session else None
        if session is None or not session.online or not tel or not tel.get("pointing"):
            return {"ok": False, "message": "Ese telescopio no está conectado"}
        if tel["pointing"]["alt"] < session.min_altitude:
            return {"ok": False, "message": f"{session.name} apunta bajo el límite de altura (posición de inicio). "
                                            "Primero elige un objeto visible."}
        p = tel["pointing"]
        fov_w, fov_h = session.fov
        geom = FieldGeometry(p["ra"], p["dec"], fov_w, fov_h, p.get("rotation", 0.0),
                             session.info.get("camera", {}).get("flip_x", False))
        ra, dec = geom.to_sky(min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0))
        label = f"{format_ra(ra, 0)} {format_dec(dec)}"
        return await self.goto({"ra": ra, "dec": dec, "name": label}, [node_id], name=label)

    async def align(self, node_id: str, target: str | dict) -> dict:
        """Alineación de un nodo: el objeto indicado está centrado en su cámara."""
        session = self.hub.nodes.get(node_id)
        if session is None or not session.online:
            return {"ok": False, "message": "Ese telescopio no está conectado"}
        obj = self.resolve(target)
        if obj is None:
            return {"ok": False, "message": f"No se encontró el objeto '{target}'"}
        ok, message = await session.command({"type": "sync", **self._target_message(obj)})
        self.hub.log("info" if ok else "warning", f"{session.name}: {message}", node_id)
        return {"ok": ok, "message": message}

    # ------------------------------------------------------------------ sugerencias
    SHOWCASE = ("M 42", "M 45", "M 31", "M 44", "M 41", "M 7", "M 6", "M 8", "M 22", "M 13", "M 11",
                "M 4", "M 5", "M 3", "M 35", "M 47", "M 36", "M 37", "M 39", "M 57", "M 27", "M 17",
                "NGC 5139", "NGC 104", "NGC 3372", "NGC 4755", "NGC 869", "NGC 3532", "NGC 2070")

    def visible_targets(self, t: float | None = None, limit: int = 14) -> dict:
        """Objetos brillantes visibles ahora desde todos los telescopios conectados.

        Pensado para telescopios pequeños: Luna, planetas, estrellas muy brillantes y
        cúmulos o nebulosas vistosos. Se ordenan por la altura mínima entre los sitios.
        """
        t = time.time() if t is None else t
        jd = julian_date(t)
        sessions = [s for s in self.hub.online() if s.site] or [s for s in self.hub.nodes.values() if s.site]
        if not sessions:
            return {"targets": [], "sites": []}
        candidates = [self.catalog.body_object(b, jd) for b in ("moon",) + PLANETS]
        candidates += [s for s in self.catalog.stars if s.mag is not None and s.mag < 1.6]
        candidates += [o for o in (self.catalog.resolve(q, jd) for q in self.SHOWCASE) if o is not None]
        found = []
        for obj in candidates:
            vis = [self.visibility(obj, s, t) for s in sessions]
            if all(v["ok"] and v["alt"] >= s.min_altitude + 5 for v, s in zip(vis, sessions)):
                label = obj.name if obj.kind == "body" else obj.id if obj.kind == "dso" else obj.name.split(" (")[0]
                rank = {"body": 0, "dso": 1, "star": 2}[obj.kind]
                found.append((rank, -min(v["alt"] for v in vis), {
                    "label": label, "name": obj.name, "kind": obj.kind, "type": obj.type_label, "mag": obj.mag,
                    "min_alt": min(v["alt"] for v in vis)}))
        found.sort(key=lambda f: (f[0], f[1]))
        sun = body_position("sun", jd)
        sites = [{"node": s.node_id, "name": s.name,
                  "sun_alt": radec_to_altaz(sun.ra, sun.dec, s.site, jd)[0]} for s in sessions]
        for site in sites:
            site["twilight"] = twilight_label(site["sun_alt"])
        return {"targets": [f[2] for f in found[:limit]], "sites": sites}

    # ------------------------------------------------------------------ enriquecimiento
    def enrich(self, session: NodeSession, t: float) -> dict | None:
        tel, site = session.telemetry, session.site
        if not tel or not site or not tel.get("pointing"):
            return None
        jd = julian_date(t)
        p = tel["pointing"]
        ra, dec = p["ra"], p["dec"]
        fov_w, fov_h = session.fov
        flip = session.info.get("camera", {}).get("flip_x", False)
        geom = FieldGeometry(ra, dec, fov_w, fov_h, p.get("rotation", 0.0), flip)
        in_field = self.catalog.objects_in_field(geom, jd, site)
        con = constellation_of(ra, dec)

        sun = body_position("sun", jd)
        sun_alt, _ = radec_to_altaz(sun.ra, sun.dec, site, jd)
        moon = body_position("moon", jd, site)
        moon_alt, moon_az = radec_to_altaz(moon.ra, moon.dec, site, jd, refraction=True)
        phase = moon_phase(jd)
        offset_h = session.info.get("site", {}).get("utc_offset_hours", 0.0)
        local = datetime.fromtimestamp(t, timezone.utc) + timedelta(hours=offset_h)

        corners = [{"ra": c[0], "dec": c[1], "ra_str": format_ra(c[0], 0), "dec_str": format_dec(c[1])}
                   for c in geom.corners()]
        primary = self._primary_object(in_field)
        summary = self._summary(session, primary, con, in_field, sun_alt)
        return {
            "constellation": con,
            "constellation_name": constellation_name(con),
            "in_field": in_field[:40],
            "primary": primary,
            "summary": summary,
            "field": {"fov_w": fov_w, "fov_h": fov_h, "rotation": p.get("rotation", 0.0), "flip_x": flip,
                      "corners": corners},
            "sky": {
                "sun_alt": sun_alt, "twilight": twilight_label(sun_alt),
                "moon_alt": moon_alt, "moon_az": moon_az,
                "moon_illuminated": phase["illuminated"], "moon_phase": phase["name"],
                "moon_sep": angular_separation(ra, dec, moon.ra, moon.dec),
            },
            "local_time": local.strftime("%H:%M:%S"),
            "utc_offset_hours": offset_h,
        }

    @staticmethod
    def _primary_object(in_field: list[dict]) -> dict | None:
        """El objeto más relevante del campo: cuerpos > Messier > DSO > estrellas brillantes."""
        def score(o: dict) -> float:
            centrality = math.hypot(o["x"] - 0.5, o["y"] - 0.5)
            base = {"body": 0.0, "dso": 2.0, "star": 4.0}.get(o["kind"], 5.0)
            if o["kind"] == "dso" and o["id"].startswith("M "):
                base = 1.0
            mag = o["mag"] if o["mag"] is not None else 10.0
            return base + centrality * 2.0 + (mag / 10.0 if o["kind"] == "star" else 0.0)

        return min(in_field, key=score) if in_field else None

    @staticmethod
    def _summary(session: NodeSession, primary: dict | None, con: str, in_field: list[dict],
                 sun_alt: float) -> str:
        parts = []
        if primary:
            parts.append(f"Viendo {primary['name']} ({primary['type'].lower()}) en {constellation_name(con)}")
        else:
            parts.append(f"Campo en {constellation_name(con)} sin objetos catalogados brillantes")
        others = len(in_field) - (1 if primary else 0)
        if others > 0:
            parts.append(f"{others} objeto{'s' if others != 1 else ''} más de catálogo en el campo")
        analysis = session.analysis
        if analysis:
            parts.append(analysis.get("scene_label", ""))
        if sun_alt > -0.833:
            parts.append(f"es de día en {session.name}")
        elif sun_alt > -12:
            parts.append(f"crepúsculo en {session.name}")
        return ". ".join(p[0].upper() + p[1:] for p in parts if p).rstrip(".") + "."

    # ------------------------------------------------------------------ sincronía
    def sync_status(self) -> dict | None:
        sessions = [s for s in self.hub.online() if s.telemetry and s.telemetry.get("pointing")]
        if len(sessions) < 2:
            return None
        a, b = sessions[0], sessions[1]
        pa, pb = a.telemetry["pointing"], b.telemetry["pointing"]
        sep = angular_separation(pa["ra"], pa["dec"], pb["ra"], pb["dec"])
        std = project(pb["ra"], pb["dec"], pa["ra"], pa["dec"])
        (wa, ha), (wb, hb) = a.fov, b.fov
        overlap = 0.0
        if std is not None:
            # Superposición de los rectángulos de campo (sin considerar su rotación).
            ox = max(0.0, min(wa / 2, std[0] + wb / 2) - max(-wa / 2, std[0] - wb / 2))
            oy = max(0.0, min(ha / 2, std[1] + hb / 2) - max(-ha / 2, std[1] - hb / 2))
            overlap = ox * oy / min(wa * ha, wb * hb)
        if overlap > 0.8:
            status, label = "mismo", "Ambos telescopios apuntan al mismo cuadrante"
        elif overlap > 0.05:
            status, label = "parcial", "Los campos se superponen parcialmente"
        else:
            status, label = "distinto", "Los telescopios apuntan a regiones distintas"
        result = {"nodes": [a.node_id, b.node_id], "separation_deg": sep, "overlap": overlap,
                  "status": status, "label": label, "moving": any(
                      s.telemetry.get("state") in ("slewing", "waiting") for s in (a, b))}
        target = self.common_target
        if target and target.get("body") == "moon" and a.site and b.site:
            jd = julian_date()
            ma, mb = body_position("moon", jd, a.site), body_position("moon", jd, b.site)
            result["parallax_deg"] = angular_separation(ma.ra, ma.dec, mb.ra, mb.dec)
        return result

    # ------------------------------------------------------------------ capturas
    def save_capture(self, captures_dir: Path) -> dict:
        sessions = [s for s in self.hub.online() if s.jpeg]
        if not sessions:
            return {"ok": False, "message": "No hay imágenes para capturar"}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        folder = captures_dir / stamp
        folder.mkdir(parents=True, exist_ok=True)
        meta = {"utc": stamp, "target": self.common_target, "nodes": {}}
        for s in sessions:
            (folder / f"{s.node_id}.jpg").write_bytes(s.jpeg)
            meta["nodes"][s.node_id] = {"name": s.name, "frame_ts": s.frame_ts, "info": s.info,
                                        "telemetry": s.telemetry, "analysis": s.analysis,
                                        "enrichment": s.enrichment}
        times = [s.frame_ts for s in sessions]
        meta["time_skew_s"] = max(times) - min(times)
        (folder / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        text = f"Captura guardada en {folder} ({len(sessions)} imágenes, desfase {meta['time_skew_s']:.2f} s)"
        self.hub.log("info", text)
        return {"ok": True, "message": text, "folder": stamp, "files": [f"{s.node_id}.jpg" for s in sessions]}
