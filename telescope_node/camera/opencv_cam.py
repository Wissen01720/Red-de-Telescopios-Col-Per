"""Cámaras USB / UVC (webcams modificadas, cámaras planetarias con UVC) vía OpenCV."""

from __future__ import annotations

import logging

import cv2
import numpy as np

from .base import Camera

log = logging.getLogger(__name__)


class OpenCVCamera(Camera):
    def __init__(self, device: int | str, width: int, height: int, exposure_ms: float, gain: float):
        super().__init__(width, height, exposure_ms, gain)
        self._cap = cv2.VideoCapture(device)
        if not self._cap.isOpened():
            raise RuntimeError(f"No se pudo abrir la cámara {device!r}")
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.set_controls(exposure_ms, gain)

    def set_controls(self, exposure_ms: float | None = None, gain: float | None = None) -> None:
        super().set_controls(exposure_ms, gain)
        # Las unidades de exposición dependen del driver; en V4L2 suelen ser 100 µs.
        self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)  # 1 = manual en V4L2
        self._cap.set(cv2.CAP_PROP_EXPOSURE, self.exposure_ms * 10.0)
        self._cap.set(cv2.CAP_PROP_GAIN, self.gain)

    def capture(self) -> np.ndarray:
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise RuntimeError("La cámara no entregó imagen")
        return frame

    def close(self) -> None:
        self._cap.release()
