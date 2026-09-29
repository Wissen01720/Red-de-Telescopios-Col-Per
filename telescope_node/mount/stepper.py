"""Montura altacimutal con dos motores paso a paso controlados por GPIO.

Pensada para drivers STEP/DIR (A4988, DRV8825, TMC2208/2209) conectados a la
Raspberry Pi. Un único hilo genera los pulsos de ambos ejes con rampas de
aceleración trapezoidales, así que la posición se conoce contando pasos.

Conexión típica por eje:  GPIO STEP -> STEP,  GPIO DIR -> DIR,  GND común,
y opcionalmente un GPIO a EN (habilitación, activo en bajo).

Nota: la temporización por software alcanza unos pocos kHz de pasos. Si se
necesitan velocidades mayores, conviene delegar los pulsos a un
microcontrolador (p. ej. OnStep) y usar el driver ``lx200``.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

from .base import AltAzMount

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- GPIO

class LgpioBackend:
    """Acceso a GPIO con ``lgpio`` (funciona en Raspberry Pi 4 y 5)."""

    def __init__(self, chip: int = 0):
        import lgpio  # disponible en Raspberry Pi OS (python3-lgpio)

        self._lg = lgpio
        self._h = lgpio.gpiochip_open(chip)

    def setup_output(self, pin: int, level: int = 0) -> None:
        self._lg.gpio_claim_output(self._h, pin, level)

    def write(self, pin: int, level: int) -> None:
        self._lg.gpio_write(self._h, pin, level)

    def close(self) -> None:
        self._lg.gpiochip_close(self._h)


class FakeGPIO:
    """GPIO de mentira para pruebas: cuenta flancos de subida por pin."""

    def __init__(self) -> None:
        self.levels: dict[int, int] = {}
        self.rising_edges: dict[int, int] = defaultdict(int)

    def setup_output(self, pin: int, level: int = 0) -> None:
        self.levels[pin] = level

    def write(self, pin: int, level: int) -> None:
        if level and not self.levels.get(pin):
            self.rising_edges[pin] += 1
        self.levels[pin] = level

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- ejes

@dataclass
class AxisConfig:
    step_pin: int
    dir_pin: int
    steps_per_rev: int = 200
    microsteps: int = 16
    gear_ratio: float = 100.0
    invert: bool = False
    max_rate_deg_s: float = 3.0
    accel_deg_s2: float = 2.0

    @property
    def steps_per_deg(self) -> float:
        return self.steps_per_rev * self.microsteps * self.gear_ratio / 360.0


class _Axis:
    def __init__(self, cfg: AxisConfig, gpio) -> None:
        self.cfg = cfg
        self.gpio = gpio
        self.spd = cfg.steps_per_deg
        self.accel = cfg.accel_deg_s2 * self.spd              # pasos/s²
        self.max_rate = cfg.max_rate_deg_s * self.spd         # pasos/s
        self.v_start = math.sqrt(2.0 * self.accel)            # velocidad de arranque
        self.position = 0
        self.target = 0
        self.cruise = self.max_rate
        self.velocity = 0.0
        self.direction = 0
        self.next_time = 0.0
        gpio.setup_output(cfg.step_pin, 0)
        gpio.setup_output(cfg.dir_pin, 0)

    @property
    def busy(self) -> bool:
        return self.direction != 0 or self.position != self.target

    def _floor(self) -> float:
        return min(self.v_start, self.cruise)

    def _set_direction(self, direction: int) -> None:
        self.direction = direction
        level = 1 if (direction > 0) != self.cfg.invert else 0
        self.gpio.write(self.cfg.dir_pin, level)

    def _pulse(self) -> None:
        self.gpio.write(self.cfg.step_pin, 1)
        self.gpio.write(self.cfg.step_pin, 0)  # cada llamada tarda > 1 µs: ancho suficiente
        self.position += self.direction

    def step(self, now: float) -> None:
        """Ejecuta un paso si corresponde y programa el siguiente."""
        remaining = self.target - self.position
        wanted = (remaining > 0) - (remaining < 0)
        if self.direction == 0:
            if wanted == 0:
                return
            self._set_direction(wanted)
            self.velocity = self._floor()
        elif wanted != self.direction:
            if self.velocity <= self.v_start:
                # Suficientemente lento para detenerse o invertir de inmediato.
                if wanted == 0:
                    self.direction = 0
                    self.velocity = 0.0
                    return
                self._set_direction(wanted)
                self.velocity = self._floor()
            else:
                # Frena en la dirección actual antes de invertir.
                self._pulse()
                self.velocity = max(self._floor(), math.sqrt(max(self.velocity ** 2 - 2 * self.accel, 0.0)))
                self.next_time = now + 1.0 / self.velocity
                return
        self._pulse()
        remaining = abs(self.target - self.position)
        v2 = self.velocity ** 2
        if remaining <= v2 / (2.0 * self.accel):
            self.velocity = math.sqrt(max(v2 - 2 * self.accel, self._floor() ** 2))
        elif self.velocity < self.cruise:
            self.velocity = min(self.cruise, math.sqrt(v2 + 2 * self.accel))
        elif self.velocity > self.cruise:
            self.velocity = max(self.cruise, math.sqrt(max(v2 - 2 * self.accel, 0.0)))
        self.next_time = now + 1.0 / self.velocity
        if self.position == self.target and self.velocity <= self.v_start:
            self.direction = 0
            self.velocity = 0.0

    def braking_steps(self) -> int:
        return math.ceil(self.velocity ** 2 / (2.0 * self.accel)) if self.direction else 0


# --------------------------------------------------------------------------- montura

class StepperMount(AltAzMount):
    def __init__(self, alt_axis: AxisConfig, az_axis: AxisConfig, gpio=None, gpio_chip: int = 0,
                 enable_pin: int = -1, enable_active_low: bool = True,
                 home_alt: float = 0.0, home_az: float = 0.0):
        super().__init__(min(alt_axis.max_rate_deg_s, az_axis.max_rate_deg_s))
        self.gpio = gpio if gpio is not None else LgpioBackend(gpio_chip)
        self._enable_pin = enable_pin
        self._enable_level = 0 if enable_active_low else 1
        if enable_pin >= 0:
            self.gpio.setup_output(enable_pin, self._enable_level)
        self._alt = _Axis(alt_axis, self.gpio)
        self._az = _Axis(az_axis, self.gpio)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = True
        self._set_axis_position(home_alt, home_az)
        self._thread = threading.Thread(target=self._run, name="motores", daemon=True)
        self._thread.start()

    # --- interfaz AltAzMount ---------------------------------------------------
    def _axis_position(self) -> tuple[float, float]:
        with self._lock:
            return self._alt.position / self._alt.spd, self._az.position / self._az.spd

    def _set_axis_position(self, alt: float, az: float) -> None:
        with self._lock:
            for axis, deg in ((self._alt, alt), (self._az, az)):
                axis.position = axis.target = round(deg * axis.spd)
                axis.direction = 0
                axis.velocity = 0.0

    def _axis_slew_to(self, alt: float, az: float, duration_s: float | None) -> None:
        with self._lock:
            moves = [(axis, round(deg * axis.spd)) for axis, deg in ((self._alt, alt), (self._az, az))]
            t_min = max(abs(t - a.position) / a.max_rate for a, t in moves)
            duration = max(duration_s or 0.0, t_min)
            now = time.perf_counter()
            for axis, target in moves:
                dist = abs(target - axis.position)
                axis.cruise = axis.max_rate if duration <= 0 else min(axis.max_rate, max(dist / duration, 1.0))
                axis.target = target
                if axis.direction == 0:
                    axis.next_time = now
        self._wake.set()

    def is_moving(self) -> bool:
        with self._lock:
            return self._alt.busy or self._az.busy

    def stop(self) -> None:
        """Frena con la rampa de desaceleración (sin perder pasos)."""
        with self._lock:
            for axis in (self._alt, self._az):
                axis.target = axis.position + axis.direction * axis.braking_steps()
        self._wake.set()

    def close(self) -> None:
        self._running = False
        self._wake.set()
        self._thread.join(timeout=2.0)
        if self._enable_pin >= 0:
            self.gpio.write(self._enable_pin, 1 - self._enable_level)
        self.gpio.close()

    # --- hilo de generación de pulsos ------------------------------------------
    def _run(self) -> None:
        while self._running:
            with self._lock:
                now = time.perf_counter()
                next_due = math.inf
                for axis in (self._alt, self._az):
                    if not axis.busy:
                        continue
                    if axis.next_time <= now:
                        axis.step(now)
                    if axis.busy:
                        next_due = min(next_due, axis.next_time)
            if next_due == math.inf:
                self._wake.wait(0.05)
                self._wake.clear()
                continue
            delay = next_due - time.perf_counter()
            if delay > 0.002:
                time.sleep(delay - 0.001)
            else:
                time.sleep(0)  # espera activa corta, cediendo el GIL a otros hilos
