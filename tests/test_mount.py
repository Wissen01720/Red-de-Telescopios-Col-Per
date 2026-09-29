"""Control de apuntado (con reloj simulado) y generación de pasos de los motores."""

import time
from datetime import datetime, timezone

import pytest

from astrocore.coords import angular_separation, julian_date, radec_to_altaz
from astrocore.ephemeris import body_position
from telescope_node.config import NodeConfig
from telescope_node.mount.lx200 import format_dec_lx200, format_ra_lx200, parse_lx200_angle
from telescope_node.mount.simulated import SimulatedMount
from telescope_node.mount.stepper import AxisConfig, FakeGPIO, StepperMount
from telescope_node.pointing import PointingController, Target

NIGHT = datetime(2026, 12, 15, 4, 0, tzinfo=timezone.utc).timestamp()  # 23:00 en Lima: Orión alto
M42 = Target("M 42", 83.8187, -5.3897)


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_controller(tmp_path, error=(0.0, 0.0), **mount_kwargs):
    cfg = NodeConfig()
    cfg.site.latitude, cfg.site.longitude, cfg.site.elevation_m = -12.046, -77.043, 150
    cfg.node.state_file = str(tmp_path / "estado.json")
    clock = Clock(NIGHT)
    mount = SimulatedMount(max_rate_deg_s=5.0, error_alt_deg=error[0], error_az_deg=error[1], clock=clock,
                           **mount_kwargs)
    return PointingController(mount, cfg, clock=clock), clock, mount


def run(ctl, clock, seconds, step=0.1):
    for _ in range(int(seconds / step)):
        clock.t += step
        ctl.update()


def test_goto_waits_for_start_time_then_tracks(tmp_path):
    ctl, clock, _ = make_controller(tmp_path)
    ok, _ = ctl.goto(M42, start_at=clock.t + 2.0, duration=30.0)
    assert ok and ctl.state == "waiting"
    run(ctl, clock, 1.5)
    assert ctl.state == "waiting"
    run(ctl, clock, 1.0)
    assert ctl.state == "slewing"
    run(ctl, clock, 29.0)
    assert ctl.state == "slewing"  # se respeta la duración común (sincronía)
    run(ctl, clock, 3.0)
    assert ctl.state == "tracking"
    tel = ctl.telemetry()
    assert tel["target"]["error_deg"] < 0.02


def test_tracking_follows_sidereal_motion(tmp_path):
    ctl, clock, _ = make_controller(tmp_path)
    ctl.goto(M42)
    run(ctl, clock, 60)
    alt0 = ctl.current_altaz()[0]
    run(ctl, clock, 600, step=0.25)   # 10 minutos
    assert abs(ctl.current_altaz()[0] - alt0) > 0.5  # la montura se movió con el cielo...
    ra, dec = ctl.current_radec()
    assert angular_separation(ra, dec, M42.ra, M42.dec) < 0.02  # ...y sigue centrada en M42


def test_rejects_targets_below_horizon(tmp_path):
    ctl, clock, _ = make_controller(tmp_path)
    ok, reason = ctl.goto(Target("Polaris", 37.95, 89.26))  # invisible desde Lima
    assert not ok and "límite" in reason
    assert ctl.state == "idle"


def test_rejects_targets_near_the_sun(tmp_path):
    ctl, clock, _ = make_controller(tmp_path)
    clock.t = datetime(2026, 12, 15, 17, 0, tzinfo=timezone.utc).timestamp()  # mediodía en Lima
    sun = body_position("sun", julian_date(clock.t))
    ok, reason = ctl.goto(Target("Cerca del Sol", sun.ra + 3.0, sun.dec))
    assert not ok and "Sol" in reason
    ctl.cfg.mount.solar_filter = True
    assert ctl.goto(Target("Cerca del Sol", sun.ra + 3.0, sun.dec))[0]


def test_sync_corrects_mount_error(tmp_path):
    ctl, clock, mount = make_controller(tmp_path, error=(0.3, -0.4))

    def true_radec():
        alt, az = mount.true_position()
        from astrocore.coords import altaz_to_radec
        return altaz_to_radec(alt, az, ctl.site, julian_date(clock.t), refraction=True)

    ctl.goto(M42)
    run(ctl, clock, 60)
    assert angular_separation(*true_radec(), M42.ra, M42.dec) > 0.2  # la cámara ve M42 descentrada
    # El operador centra M42 a mano (clic en la imagen) y luego alinea.
    ra, dec = true_radec()
    offset_target = Target("ajuste", M42.ra + (M42.ra - ra), M42.dec + (M42.dec - dec))
    ctl.goto(offset_target)
    run(ctl, clock, 20)
    assert angular_separation(*true_radec(), M42.ra, M42.dec) < 0.03
    ok, _ = ctl.sync(M42)
    assert ok and ctl.aligned
    ctl.goto(Target("Rigel", 78.634, -8.2016))
    run(ctl, clock, 30)
    assert angular_separation(*true_radec(), 78.634, -8.2016) < 0.03  # los siguientes GoTo ya son precisos


def test_park_persists_alignment(tmp_path):
    ctl, clock, mount = make_controller(tmp_path)
    ctl.goto(M42)
    run(ctl, clock, 60)
    ctl.sync(M42)
    ctl.park()
    run(ctl, clock, 80)
    assert ctl.state == "parked"
    ctl2, _, _ = make_controller(tmp_path)
    assert ctl2.state == "parked" and ctl2.aligned


def test_moon_target_is_topocentric(tmp_path):
    ctl, clock, _ = make_controller(tmp_path)
    jd = julian_date(clock.t)
    moon_here = body_position("moon", jd, ctl.site)
    target = Target.from_message({"body": "moon"})
    ra, dec = target.coords(jd, ctl.site)
    assert angular_separation(ra, dec, moon_here.ra, moon_here.dec) < 1e-6
    geo = body_position("moon", jd)
    assert angular_separation(ra, dec, geo.ra, geo.dec) > 0.1


# --------------------------------------------------------------------------- motores paso a paso

def wait_idle(mount, timeout=5.0):
    deadline = time.monotonic() + timeout
    while mount.is_moving() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not mount.is_moving()


@pytest.fixture
def stepper():
    gpio = FakeGPIO()
    fast = dict(steps_per_rev=200, microsteps=16, gear_ratio=10, max_rate_deg_s=20.0, accel_deg_s2=60.0)
    mount = StepperMount(AxisConfig(17, 27, **fast), AxisConfig(22, 23, **fast), gpio=gpio)
    yield mount, gpio
    mount.close()


def test_stepper_moves_exact_number_of_steps(stepper):
    mount, gpio = stepper
    spd = 200 * 16 * 10 / 360.0
    mount.slew_to(2.0, 350.0)          # 350° se alcanza por el camino corto: -10°
    wait_idle(mount)
    alt, az = mount.position()
    assert alt == pytest.approx(2.0, abs=1 / spd)
    assert az == pytest.approx(350.0, abs=1 / spd)
    assert gpio.rising_edges[17] == round(2.0 * spd)
    assert gpio.rising_edges[22] == round(10.0 * spd)


def test_stepper_reverses_without_losing_steps(stepper):
    mount, gpio = stepper
    spd = 200 * 16 * 10 / 360.0
    mount.slew_to(10.0, 0.0)
    time.sleep(0.15)
    mount.slew_to(-1.0, 0.0)           # invierte en pleno movimiento
    wait_idle(mount)
    assert mount.position()[0] == pytest.approx(-1.0, abs=1 / spd)
    # Pasos emitidos = ida + vuelta; la posición contada coincide con la real.
    assert gpio.rising_edges[17] >= round(1.0 * spd)


def test_stepper_stop_decelerates(stepper):
    mount, _ = stepper
    mount.slew_to(30.0, 0.0)
    time.sleep(0.3)
    mount.stop()
    wait_idle(mount)
    assert 0.0 < mount.position()[0] < 30.0


# --------------------------------------------------------------------------- LX200

def test_lx200_format_and_parse():
    assert format_ra_lx200(83.8208) == "05:35:17"
    assert format_dec_lx200(-5.3911) == "-05*23:28"
    assert parse_lx200_angle("05:35:17#", hours=True) == pytest.approx(83.8208, abs=1e-3)
    assert parse_lx200_angle("-05*23:28#", hours=False) == pytest.approx(-5.3911, abs=1e-3)
    assert parse_lx200_angle("05:35.3#", hours=True) == pytest.approx(83.825, abs=1e-2)
