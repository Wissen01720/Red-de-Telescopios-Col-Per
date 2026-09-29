"""Drivers de cámara y fábrica a partir de la configuración."""

from __future__ import annotations

from collections.abc import Callable

from ..config import NodeConfig
from .base import Camera
from .simulated import FieldPose, SimulatedCamera


def create_camera(cfg: NodeConfig, pose_provider: Callable[[], FieldPose]) -> Camera:
    c = cfg.camera
    if c.driver == "simulated":
        s = c.simulated
        return SimulatedCamera(c.width, c.height, c.exposure_ms, c.gain, pose_provider,
                               clouds=s.clouds, seeing_px=s.seeing_px,
                               light_pollution=s.light_pollution, seed=s.seed, force_night=s.force_night)
    if c.driver == "picamera2":
        from .picam import PiCamera

        return PiCamera(c.width, c.height, c.exposure_ms, c.gain)
    if c.driver == "opencv":
        from .opencv_cam import OpenCVCamera

        return OpenCVCamera(c.device, c.width, c.height, c.exposure_ms, c.gain)
    raise ValueError(f"Driver de cámara desconocido: {c.driver!r}")


__all__ = ["Camera", "FieldPose", "SimulatedCamera", "create_camera"]
