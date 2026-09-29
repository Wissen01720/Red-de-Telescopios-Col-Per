"""Configuración del nodo (archivo TOML).

Ver ``config/nodo_colombia.toml`` y ``config/nodo_hardware_ejemplo.toml``.
"""

from __future__ import annotations

import dataclasses
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrocore.coords import Site
from astrocore.field import fov_from_optics

log = logging.getLogger(__name__)


@dataclass
class NodeSection:
    id: str = "nodo"
    name: str = "Telescopio"
    server_url: str = "ws://localhost:8000/ws/node"
    token: str = "cambia-este-token"
    state_file: str = "estado_nodo.json"


@dataclass
class SiteSection:
    latitude: float = 0.0
    longitude: float = 0.0
    elevation_m: float = 0.0
    utc_offset_hours: float = -5.0


@dataclass
class OpticsSection:
    focal_length_mm: float = 650.0
    aperture_mm: float = 130.0
    # Si se dejan en 0 se calculan con la focal y el tamaño del sensor.
    fov_width_deg: float = 0.0
    fov_height_deg: float = 0.0


@dataclass
class SimulatedCameraSection:
    clouds: float = 0.0            # 0 = despejado, 1 = cubierto
    seeing_px: float = 1.3         # sigma de la PSF en píxeles
    light_pollution: float = 0.2   # 0 = cielo oscuro, 1 = ciudad
    force_night: bool = False      # ignora la luz del día (para demostraciones de día)
    seed: int = 0


@dataclass
class CameraSection:
    driver: str = "simulated"      # simulated | picamera2 | opencv
    width: int = 1280
    height: int = 960
    stream_width: int = 960        # ancho del video que se envía al servidor
    fps: float = 3.0
    jpeg_quality: int = 75
    exposure_ms: float = 200.0
    gain: float = 4.0
    sensor_width_mm: float = 6.287   # Raspberry Pi HQ Camera (IMX477)
    sensor_height_mm: float = 4.712
    rotation_deg: float = 0.0        # giro de la cámara respecto a "arriba = cénit/Norte"
    flip_x: bool = False             # True si hay diagonal/espejo que invierte la imagen
    device: int | str = 0            # índice o ruta para opencv
    simulated: SimulatedCameraSection = field(default_factory=SimulatedCameraSection)


@dataclass
class StepperSection:
    gpio_chip: int = 0               # Pi 5 con kernel antiguo: 4
    alt_step_pin: int = 17
    alt_dir_pin: int = 27
    az_step_pin: int = 22
    az_dir_pin: int = 23
    enable_pin: int = -1             # -1 = sin pin de habilitación
    enable_active_low: bool = True   # A4988 / DRV8825 / TMC2209: EN activo en bajo
    steps_per_rev: int = 200
    microsteps: int = 16
    alt_gear_ratio: float = 100.0    # vueltas del motor por vuelta del eje
    az_gear_ratio: float = 100.0
    alt_invert: bool = False
    az_invert: bool = False
    acceleration_deg_s2: float = 2.0


@dataclass
class LX200Section:
    port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    epoch: str = "jnow"              # jnow | j2000 (coordenadas que espera la montura)
    timeout_s: float = 2.0


@dataclass
class SimulatedMountSection:
    # Error de apuntado "real" que no conoce el software (para practicar la alineación).
    error_alt_deg: float = 0.0
    error_az_deg: float = 0.0


@dataclass
class MountSection:
    driver: str = "simulated"        # simulated | stepper | lx200
    type: str = "altaz"              # altaz | equatorial (define la rotación del campo)
    min_altitude_deg: float = 10.0
    max_altitude_deg: float = 89.5
    max_slew_deg_s: float = 3.0
    home_alt: float = 0.0            # posición al encender / aparcar
    home_az: float = 0.0
    tracking_interval_s: float = 0.5
    sun_avoidance_deg: float = 20.0  # no apuntar más cerca del Sol que esto...
    solar_filter: bool = False       # ...salvo que haya un filtro solar certificado instalado
    stepper: StepperSection = field(default_factory=StepperSection)
    lx200: LX200Section = field(default_factory=LX200Section)
    simulated: SimulatedMountSection = field(default_factory=SimulatedMountSection)


@dataclass
class AnalysisSection:
    interval_s: float = 2.0
    detection_sigma: float = 5.0
    max_stars: int = 40


@dataclass
class NodeConfig:
    node: NodeSection = field(default_factory=NodeSection)
    site: SiteSection = field(default_factory=SiteSection)
    optics: OpticsSection = field(default_factory=OpticsSection)
    camera: CameraSection = field(default_factory=CameraSection)
    mount: MountSection = field(default_factory=MountSection)
    analysis: AnalysisSection = field(default_factory=AnalysisSection)

    @property
    def site_obj(self) -> Site:
        return Site(self.site.latitude, self.site.longitude, self.site.elevation_m)

    @property
    def fov(self) -> tuple[float, float]:
        """(ancho, alto) del campo de visión en grados."""
        w = self.optics.fov_width_deg or fov_from_optics(self.optics.focal_length_mm, self.camera.sensor_width_mm)
        h = self.optics.fov_height_deg or fov_from_optics(self.optics.focal_length_mm, self.camera.sensor_height_mm)
        return w, h


def _build(cls: type, data: dict[str, Any], path: str) -> Any:
    kwargs = {}
    fields = {f.name: f for f in dataclasses.fields(cls)}
    for key, value in data.items():
        if key not in fields:
            log.warning("Clave desconocida en la configuración: %s.%s", path, key)
            continue
        default = fields[key].default_factory() if fields[key].default_factory is not dataclasses.MISSING else None
        if dataclasses.is_dataclass(default):
            kwargs[key] = _build(type(default), value, f"{path}.{key}")
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path) -> NodeConfig:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    cfg = _build(NodeConfig, data, "config")
    if cfg.node.token == NodeSection.token:
        log.warning("Se está usando el token por defecto; cámbialo en [node].token")
    return cfg
