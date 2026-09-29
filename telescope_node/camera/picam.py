"""Cámaras oficiales de Raspberry Pi (HQ Camera, Camera Module 3, ...) con Picamera2.

Picamera2 se instala con el sistema:  sudo apt install python3-picamera2
(el entorno virtual debe crearse con --system-site-packages).
"""

from __future__ import annotations

import logging

import numpy as np

from .base import Camera

log = logging.getLogger(__name__)


class PiCamera(Camera):
    def __init__(self, width: int, height: int, exposure_ms: float, gain: float):
        super().__init__(width, height, exposure_ms, gain)
        from picamera2 import Picamera2

        self._cam = Picamera2()
        config = self._cam.create_still_configuration(
            main={"size": (width, height), "format": "RGB888"}, buffer_count=2, queue=False)
        self._cam.configure(config)
        self._cam.start()
        self.set_controls(exposure_ms, gain)

    def set_controls(self, exposure_ms: float | None = None, gain: float | None = None) -> None:
        super().set_controls(exposure_ms, gain)
        exposure_us = int(self.exposure_ms * 1000)
        frame_us = max(exposure_us + 1000, 33333)
        controls = {
            "AeEnable": False,
            "ExposureTime": exposure_us,
            "AnalogueGain": float(self.gain),
            "FrameDurationLimits": (frame_us, frame_us),
        }
        self._cam.set_controls(controls)
        log.info("Cámara: exposición %.0f ms, ganancia %.1f", self.exposure_ms, self.gain)

    def capture(self) -> np.ndarray:
        # 'RGB888' en Picamera2 entrega los bytes en orden BGR, como espera OpenCV.
        return self._cam.capture_array("main")

    def close(self) -> None:
        self._cam.stop()
        self._cam.close()
