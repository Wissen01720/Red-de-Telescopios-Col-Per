"""Drivers de montura y fábrica a partir de la configuración."""

from __future__ import annotations

from ..config import NodeConfig
from .base import AltAzMount, RaDecMount
from .simulated import SimulatedMount


def create_mount(cfg: NodeConfig) -> AltAzMount | RaDecMount:
    m = cfg.mount
    if m.driver == "simulated":
        return SimulatedMount(m.max_slew_deg_s, m.home_alt, m.home_az,
                              m.simulated.error_alt_deg, m.simulated.error_az_deg)
    if m.driver == "stepper":
        from .stepper import AxisConfig, StepperMount

        s = m.stepper
        common = dict(steps_per_rev=s.steps_per_rev, microsteps=s.microsteps,
                      max_rate_deg_s=m.max_slew_deg_s, accel_deg_s2=s.acceleration_deg_s2)
        alt = AxisConfig(s.alt_step_pin, s.alt_dir_pin, gear_ratio=s.alt_gear_ratio, invert=s.alt_invert, **common)
        az = AxisConfig(s.az_step_pin, s.az_dir_pin, gear_ratio=s.az_gear_ratio, invert=s.az_invert, **common)
        return StepperMount(alt, az, gpio_chip=s.gpio_chip, enable_pin=s.enable_pin,
                            enable_active_low=s.enable_active_low, home_alt=m.home_alt, home_az=m.home_az)
    if m.driver == "lx200":
        from .lx200 import LX200Mount

        return LX200Mount(m.lx200.port, m.lx200.baudrate, m.lx200.epoch, m.lx200.timeout_s)
    raise ValueError(f"Driver de montura desconocido: {m.driver!r}")


__all__ = ["AltAzMount", "RaDecMount", "SimulatedMount", "create_mount"]
