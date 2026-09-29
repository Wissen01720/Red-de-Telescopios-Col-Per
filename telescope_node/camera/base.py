"""Interfaz común de cámara."""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class Camera(ABC):
    def __init__(self, width: int, height: int, exposure_ms: float, gain: float):
        self.width = width
        self.height = height
        self.exposure_ms = exposure_ms
        self.gain = gain

    @abstractmethod
    def capture(self) -> np.ndarray:
        """Devuelve un cuadro (alto x ancho) en gris o (alto x ancho x 3) en BGR."""

    def set_controls(self, exposure_ms: float | None = None, gain: float | None = None) -> None:
        if exposure_ms is not None:
            self.exposure_ms = float(exposure_ms)
        if gain is not None:
            self.gain = float(gain)

    def close(self) -> None:
        pass
