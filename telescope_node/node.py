"""Orquestación del nodo: captura, análisis, control de la montura y enlace con el servidor."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import cv2
import numpy as np

from astrocore import __version__
from astrocore.protocol import PROTOCOL_VERSION

from .analysis import analyze_frame
from .camera import create_camera
from .config import NodeConfig
from .link import ServerLink
from .mount import create_mount
from .pointing import PointingController, Target

log = logging.getLogger(__name__)


class TelescopeNode:
    def __init__(self, cfg: NodeConfig):
        self.cfg = cfg
        self.mount = create_mount(cfg)
        self.pointing = PointingController(self.mount, cfg)
        self.camera = create_camera(cfg, self.pointing.field_pose)
        self._ctl_lock = threading.Lock()   # serializa el acceso a la montura
        self._frame: np.ndarray | None = None
        self.jpeg: bytes | None = None
        self.frame_ts = 0.0
        self.frame_seq = 0
        self.analysis: dict | None = None
        self.analysis_seq = 0
        self.fps_actual = 0.0
        self.link = ServerLink(self)

    # ------------------------------------------------------------------ mensajes
    def hello(self) -> dict:
        c, m, fov = self.cfg, self.cfg.mount, self.cfg.fov
        return {
            "type": "hello", "protocol": PROTOCOL_VERSION, "version": __version__,
            "node_id": c.node.id, "name": c.node.name,
            "site": {"latitude": c.site.latitude, "longitude": c.site.longitude,
                     "elevation_m": c.site.elevation_m, "utc_offset_hours": c.site.utc_offset_hours},
            "optics": {"focal_length_mm": c.optics.focal_length_mm, "aperture_mm": c.optics.aperture_mm,
                       "fov_w": fov[0], "fov_h": fov[1]},
            "camera": {"driver": c.camera.driver, "width": c.camera.width, "height": c.camera.height,
                       "flip_x": c.camera.flip_x, "rotation_deg": c.camera.rotation_deg},
            "mount": {"driver": m.driver, "kind": self.mount.kind, "type": m.type,
                      "min_altitude_deg": m.min_altitude_deg, "max_slew_deg_s": m.max_slew_deg_s,
                      "sun_avoidance_deg": m.sun_avoidance_deg, "solar_filter": m.solar_filter},
        }

    def telemetry(self) -> dict:
        with self._ctl_lock:
            data = self.pointing.telemetry()
        data.update({
            "type": "telemetry", "t": time.time(),
            "camera": {"exposure_ms": self.camera.exposure_ms, "gain": self.camera.gain,
                       "fps": round(self.fps_actual, 2)},
            "frame": {"seq": self.frame_seq, "ts": self.frame_ts},
        })
        return data

    def drain_events(self) -> list[dict]:
        with self._ctl_lock:
            events = self.pointing.drain_events()
        return [{"type": "event", "level": lvl, "text": text, "t": time.time()} for lvl, text in events]

    # ------------------------------------------------------------------ comandos
    def _run_command(self, msg: dict) -> tuple[bool, str]:
        kind = msg.get("type")
        with self._ctl_lock:
            if kind == "goto":
                return self.pointing.goto(Target.from_message(msg), msg.get("start_at"), msg.get("duration"))
            if kind == "stop":
                return self.pointing.stop()
            if kind == "tracking":
                return self.pointing.set_tracking(bool(msg.get("enabled", True)))
            if kind == "sync":
                return self.pointing.sync(Target.from_message(msg))
            if kind == "park":
                return self.pointing.park()
            if kind == "unpark":
                return self.pointing.unpark()
        if kind == "camera":
            self.camera.set_controls(msg.get("exposure_ms"), msg.get("gain"))
            return True, f"Cámara: {self.camera.exposure_ms:.0f} ms, ganancia {self.camera.gain:.1f}"
        return False, f"Comando desconocido: {kind}"

    async def handle_command(self, msg: dict) -> tuple[bool, str]:
        try:
            return await asyncio.to_thread(self._run_command, msg)
        except Exception as exc:  # noqa: BLE001 - se informa al servidor
            log.exception("Error ejecutando %s", msg.get("type"))
            return False, f"Error: {exc}"

    # ------------------------------------------------------------------ bucles
    def _encode(self, frame: np.ndarray) -> bytes:
        sw = self.cfg.camera.stream_width
        if sw and frame.shape[1] > sw:
            sh = round(frame.shape[0] * sw / frame.shape[1])
            frame = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.camera.jpeg_quality])
        if not ok:
            raise RuntimeError("No se pudo codificar el cuadro en JPEG")
        return buf.tobytes()

    async def capture_loop(self) -> None:
        interval = 1.0 / max(self.cfg.camera.fps, 0.05)
        last = time.monotonic()
        while True:
            t0 = time.monotonic()
            try:
                frame = await asyncio.to_thread(self.camera.capture)
                ts = time.time()
                jpeg = await asyncio.to_thread(self._encode, frame)
            except Exception as exc:  # noqa: BLE001
                log.error("Fallo de captura: %s", exc)
                self.pointing.events.append(("error", f"Fallo de la cámara: {exc}"))
                await asyncio.sleep(2.0)
                continue
            self._frame, self.jpeg, self.frame_ts = frame, jpeg, ts
            self.frame_seq += 1
            now = time.monotonic()
            self.fps_actual = 0.8 * self.fps_actual + 0.2 / max(now - last, 1e-3)
            last = now
            await asyncio.sleep(max(0.0, interval - (now - t0)))

    async def analysis_loop(self) -> None:
        a = self.cfg.analysis
        while True:
            await asyncio.sleep(a.interval_s)
            frame = self._frame
            if frame is None:
                continue
            try:
                result = await asyncio.to_thread(analyze_frame, frame, a.detection_sigma, a.max_stars)
            except Exception:  # noqa: BLE001
                log.exception("Fallo en el análisis de imagen")
                continue
            result["frame_ts"] = self.frame_ts
            self.analysis = result
            self.analysis_seq += 1

    async def control_loop(self) -> None:
        def step() -> None:
            with self._ctl_lock:
                self.pointing.update()

        while True:
            try:
                await asyncio.to_thread(step)
            except Exception:  # noqa: BLE001
                log.exception("Fallo en el control de la montura")
            await asyncio.sleep(0.1)

    async def run(self) -> None:
        log.info("Nodo '%s' (%s) iniciado. Campo: %.2f° x %.2f°", self.cfg.node.id, self.cfg.node.name, *self.cfg.fov)
        try:
            await asyncio.gather(self.capture_loop(), self.analysis_loop(), self.control_loop(), self.link.run())
        finally:
            self.close()

    def close(self) -> None:
        for closer in (self.camera.close, self.mount.close):
            try:
                closer()
            except Exception:  # noqa: BLE001
                log.exception("Error al cerrar")
