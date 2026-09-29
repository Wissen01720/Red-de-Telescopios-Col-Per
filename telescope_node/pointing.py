"""Control de apuntado: GoTo, seguimiento, alineación, límites de seguridad y aparcado."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from astrocore.coords import (Site, altaz_to_radec, angular_separation, format_dec, format_ra,
                              hour_angle, julian_date, lst_deg, parallactic_angle, radec_to_altaz)
from astrocore.ephemeris import BODY_INFO, body_position
from astrocore.field import FieldGeometry

from .camera.simulated import FieldPose
from .config import NodeConfig
from .mount.base import AltAzMount, RaDecMount

log = logging.getLogger(__name__)

MAX_START_DELAY_S = 30.0


@dataclass
class Target:
    name: str
    ra: float = 0.0          # J2000 (grados); ignorado si ``body`` está definido
    dec: float = 0.0
    body: str | None = None  # 'moon', 'jupiter', ... (posición recalculada y topocéntrica)

    def coords(self, jd: float, site: Site) -> tuple[float, float]:
        if self.body:
            pos = body_position(self.body, jd, site)
            return pos.ra, pos.dec
        return self.ra, self.dec

    @classmethod
    def from_message(cls, msg: dict) -> "Target":
        body = msg.get("body") or None
        if body and body not in BODY_INFO:
            raise ValueError(f"Cuerpo desconocido: {body}")
        name = msg.get("name") or (BODY_INFO[body]["name"] if body else "Coordenadas")
        return cls(name=name, ra=float(msg.get("ra", 0.0)), dec=float(msg.get("dec", 0.0)), body=body)


class PointingController:
    def __init__(self, mount: AltAzMount | RaDecMount, cfg: NodeConfig,
                 clock: Callable[[], float] = time.time):
        self.mount = mount
        self.cfg = cfg
        self.site = cfg.site_obj
        self.clock = clock
        self.state = "idle"        # idle | waiting | slewing | tracking | parked
        self.target: Target | None = None
        self.tracking = True
        self.aligned = isinstance(mount, RaDecMount)
        self.events: list[tuple[str, str]] = []
        self._pending: tuple[Target, float, float | None] | None = None
        self._last_track = 0.0
        self._last_body_check = 0.0
        self._state_path = Path(cfg.node.state_file)
        self._restore_state()

    # ------------------------------------------------------------------ utilidades
    def _event(self, level: str, text: str) -> None:
        log.log(logging.WARNING if level != "info" else logging.INFO, text)
        self.events.append((level, text))

    def drain_events(self) -> list[tuple[str, str]]:
        events, self.events = self.events, []
        return events

    def _jd(self, t: float | None = None) -> float:
        return julian_date(self.clock() if t is None else t)

    def _altaz_of(self, target: Target, t: float) -> tuple[float, float]:
        jd = self._jd(t)
        ra, dec = target.coords(jd, self.site)
        return radec_to_altaz(ra, dec, self.site, jd, refraction=True)

    def current_radec(self, t: float | None = None) -> tuple[float, float]:
        if isinstance(self.mount, RaDecMount):
            return self.mount.position_radec()
        alt, az = self.mount.position()
        return altaz_to_radec(alt, az, self.site, self._jd(t), refraction=True)

    def current_altaz(self, t: float | None = None) -> tuple[float, float]:
        if isinstance(self.mount, RaDecMount):
            ra, dec = self.mount.position_radec()
            return radec_to_altaz(ra, dec, self.site, self._jd(t), refraction=True)
        return self.mount.position()

    def field_rotation(self, ra: float, dec: float, jd: float) -> float:
        rot = self.cfg.camera.rotation_deg
        if self.cfg.mount.type == "altaz":
            rot += parallactic_angle(ra, dec, self.site, jd)
        return rot

    def field_pose(self) -> FieldPose:
        """Campo que ve la cámara ahora mismo (para la cámara simulada)."""
        t = self.clock()
        jd = self._jd(t)
        true_pos = getattr(self.mount, "true_position", None)
        if true_pos is not None:
            alt, az = true_pos()
            ra, dec = altaz_to_radec(alt, az, self.site, jd, refraction=True)
        else:
            ra, dec = self.current_radec(t)
        fov_w, fov_h = self.cfg.fov
        geom = FieldGeometry(ra, dec, fov_w, fov_h, self.field_rotation(ra, dec, jd),
                             self.cfg.camera.flip_x, self.cfg.camera.width, self.cfg.camera.height)
        return FieldPose(geom, jd, self.site, moving=self.state == "slewing")

    # ------------------------------------------------------------------ seguridad
    def check_target(self, target: Target, t: float) -> str | None:
        """Devuelve un motivo de rechazo o None si el objetivo es seguro y visible."""
        jd = self._jd(t)
        ra, dec = target.coords(jd, self.site)
        alt, _ = radec_to_altaz(ra, dec, self.site, jd, refraction=True)
        if alt < self.cfg.mount.min_altitude_deg:
            return (f"{target.name} está a {alt:.1f}° de altura en {self.cfg.node.name}, "
                    f"por debajo del límite de {self.cfg.mount.min_altitude_deg:.0f}°")
        sun = body_position("sun", jd)
        sun_sep = angular_separation(ra, dec, sun.ra, sun.dec)
        if sun_sep < self.cfg.mount.sun_avoidance_deg and not self.cfg.mount.solar_filter:
            return (f"{target.name} está a {sun_sep:.1f}° del Sol. Por seguridad no se apunta a menos de "
                    f"{self.cfg.mount.sun_avoidance_deg:.0f}° sin filtro solar")
        return None

    # ------------------------------------------------------------------ comandos
    def goto(self, target: Target, start_at: float | None = None,
             duration: float | None = None) -> tuple[bool, str]:
        now = self.clock()
        start = now if start_at is None else min(max(start_at, now), now + MAX_START_DELAY_S)
        reason = self.check_target(target, start + (duration or 0.0))
        if reason:
            self._event("warning", f"GoTo rechazado: {reason}")
            return False, reason
        if self.state == "parked":
            self._save_state(parked=False)
        self._pending = (target, start, duration)
        self.target = target
        if start <= now:
            self._start_slew()
        else:
            self.state = "waiting"
        return True, f"Apuntando a {target.name}"

    def _start_slew(self) -> None:
        assert self._pending is not None
        target, _, duration = self._pending
        self._pending = None
        now = self.clock()
        if isinstance(self.mount, RaDecMount):
            ra, dec = target.coords(self._jd(now), self.site)
            self.mount.goto_radec(ra, dec)
        else:
            alt, az = self._altaz_of(target, now)
            eta = max(duration or 0.0, self.mount.estimate_slew_time(alt, az))
            # Se apunta a donde estará el objeto al llegar.
            alt, az = self._altaz_of(target, now + eta)
            self.mount.slew_to(min(alt, self.cfg.mount.max_altitude_deg), az, duration)
        self.state = "slewing"
        self._event("info", f"Moviendo hacia {target.name}")

    def stop(self) -> tuple[bool, str]:
        self._pending = None
        self.mount.stop()
        self.state = "idle"
        self._event("info", "Movimiento detenido")
        return True, "Detenido"

    def set_tracking(self, enabled: bool) -> tuple[bool, str]:
        self.tracking = enabled
        if isinstance(self.mount, RaDecMount):
            self.mount.set_tracking(enabled)
        if not enabled and self.state == "tracking":
            self.state = "idle"
        elif enabled and self.state == "idle" and self.target is not None:
            self.state = "tracking"
        return True, "Seguimiento activado" if enabled else "Seguimiento desactivado"

    def sync(self, target: Target) -> tuple[bool, str]:
        """Alineación: el objeto ``target`` está centrado en la imagen ahora mismo."""
        if self.state in ("slewing", "waiting", "parking"):
            return False, "No se puede alinear durante un GoTo; espera a que llegue"
        now = self.clock()
        if isinstance(self.mount, RaDecMount):
            ra, dec = target.coords(self._jd(now), self.site)
            self.mount.sync_radec(ra, dec)
        else:
            before = self.current_radec(now)
            alt, az = self._altaz_of(target, now)
            self.mount.sync(alt, az)
            ra, dec = target.coords(self._jd(now), self.site)
            error = angular_separation(before[0], before[1], ra, dec)
            self._event("info", f"Alineado con {target.name} (corrección de {error * 60:.1f} arcmin)")
        self.aligned = True
        self.target = target
        if self.state == "parked":
            self._save_state(parked=False)
        self.state = "tracking" if self.tracking else "idle"
        return True, f"Alineado con {target.name}"

    def park(self) -> tuple[bool, str]:
        self._pending = None
        self.target = None
        if isinstance(self.mount, RaDecMount):
            self.mount.stop()
        else:
            # Se vuelve a la posición física de inicio (deshaciendo vueltas de cable).
            self.mount.slew_axes_to(self.cfg.mount.home_alt, self.cfg.mount.home_az)
        self.state = "parking"
        self._event("info", "Aparcando el telescopio")
        return True, "Aparcando"

    def unpark(self) -> tuple[bool, str]:
        if self.state in ("parked", "parking"):
            self.state = "idle"
            self._save_state(parked=False)
        return True, "Telescopio listo"

    # ------------------------------------------------------------------ ciclo periódico
    def update(self) -> None:
        now = self.clock()
        if self._pending is not None and now >= self._pending[1]:
            self._start_slew()
        if self.state == "parking" and not self.mount.is_moving():
            self.state = "parked"
            self._save_state(parked=True)
            self._event("info", "Telescopio aparcado")
        if self.state == "slewing" and not self.mount.is_moving():
            self.state = "tracking" if self.tracking else "idle"
            self._event("info", f"En posición: {self.target.name if self.target else ''}")
            self._last_track = 0.0
        if self.state == "tracking" and self.target is not None:
            self._track(now)

    def _track(self, now: float) -> None:
        interval = self.cfg.mount.tracking_interval_s
        if now - self._last_track < interval:
            return
        self._last_track = now
        if isinstance(self.mount, RaDecMount):
            # La montura sigue a velocidad sidérea; la Luna y los planetas se desplazan.
            if self.target.body and now - self._last_body_check >= 30.0:
                self._last_body_check = now
                ra, dec = self.target.coords(self._jd(now), self.site)
                cur = self.mount.position_radec()
                if angular_separation(ra, dec, *cur) > 0.05:
                    self.mount.goto_radec(ra, dec)
            return
        alt, az = self._altaz_of(self.target, now + interval)
        if alt < self.cfg.mount.min_altitude_deg:
            self.state = "idle"
            self._event("warning", f"{self.target.name} bajó del límite de altura: seguimiento detenido")
            return
        self.mount.slew_to(min(alt, self.cfg.mount.max_altitude_deg), az, duration_s=interval)

    # ------------------------------------------------------------------ telemetría
    def telemetry(self) -> dict:
        t = self.clock()
        jd = self._jd(t)
        ra, dec = self.current_radec(t)
        alt, az = self.current_altaz(t)
        data = {
            "state": self.state,
            "tracking": self.tracking,
            "aligned": self.aligned,
            "mount_kind": self.mount.kind,
            "pointing": {
                "ra": ra, "dec": dec, "alt": alt, "az": az,
                "ra_str": format_ra(ra), "dec_str": format_dec(dec),
                "rotation": self.field_rotation(ra, dec, jd),
                "lst_deg": lst_deg(jd, self.site.longitude),
                "ha_deg": hour_angle(ra, dec, self.site, jd),
            },
            "target": None,
            "start_in_s": max(0.0, self._pending[1] - t) if self._pending else None,
        }
        if self.target is not None:
            tra, tdec = self.target.coords(jd, self.site)
            talt, taz = radec_to_altaz(tra, tdec, self.site, jd, refraction=True)
            data["target"] = {
                "name": self.target.name, "ra": tra, "dec": tdec, "body": self.target.body,
                "alt": talt, "az": taz, "ra_str": format_ra(tra), "dec_str": format_dec(tdec),
                "error_deg": angular_separation(ra, dec, tra, tdec),
            }
        return data

    # ------------------------------------------------------------------ persistencia
    def _save_state(self, parked: bool) -> None:
        data = {"parked": parked, "aligned": self.aligned,
                "alt_offset": getattr(self.mount, "alt_offset", 0.0),
                "az_offset": getattr(self.mount, "az_offset", 0.0)}
        try:
            self._state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("No se pudo guardar el estado del nodo: %s", exc)

    def _restore_state(self) -> None:
        if isinstance(self.mount, RaDecMount) or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("parked"):
            # Los ejes están en la posición de inicio: la alineación anterior sigue siendo válida.
            self.mount.alt_offset = float(data.get("alt_offset", 0.0))
            self.mount.az_offset = float(data.get("az_offset", 0.0))
            self.aligned = bool(data.get("aligned", False))
            self.state = "parked"
            log.info("Estado restaurado: telescopio aparcado%s", " y alineado" if self.aligned else "")
