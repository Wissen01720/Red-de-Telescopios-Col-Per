"""Montura GoTo por puerto serie con protocolo LX200.

Compatible con OnStep/OnStepX, Meade LX200/Autostar y la mayoría de
controladores que emulan LX200. La montura hace su propio seguimiento.

Requiere ``pyserial`` (pip install pyserial).
"""

from __future__ import annotations

import logging
import re
import threading
import time

from astrocore.coords import date_to_j2000, j2000_to_date, julian_date

from .base import RaDecMount

log = logging.getLogger(__name__)


def format_ra_lx200(ra: float) -> str:
    total = round((ra % 360.0) / 15.0 * 3600.0) % 86400
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def format_dec_lx200(dec: float) -> str:
    sign = "-" if dec < 0 else "+"
    total = round(abs(dec) * 3600.0)
    return f"{sign}{total // 3600:02d}*{total % 3600 // 60:02d}:{total % 60:02d}"


def parse_lx200_angle(text: str, hours: bool) -> float:
    """Lee 'HH:MM:SS', 'HH:MM.T', 'sDD*MM:SS', 'sDD*MM' o 'sDD\xdfMM:SS'."""
    s = text.strip().rstrip("#")
    negative = s.startswith("-")
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums:
        raise ValueError(f"Respuesta LX200 inválida: {text!r}")
    values = [float(n) for n in nums] + [0.0, 0.0]
    value = values[0] + values[1] / 60.0 + values[2] / 3600.0
    value = -value if negative else value
    return value * 15.0 if hours else value


class LX200Mount(RaDecMount):
    def __init__(self, port: str, baudrate: int = 9600, epoch: str = "jnow", timeout_s: float = 2.0,
                 serial_port=None):
        if serial_port is None:
            import serial  # pyserial

            serial_port = serial.Serial(port, baudrate, timeout=timeout_s)
        self._ser = serial_port
        self._jnow = epoch.lower() == "jnow"
        self._lock = threading.Lock()
        self._ensure_high_precision()

    # --- comunicación -----------------------------------------------------------
    def _read_until_hash(self) -> str:
        buf = bytearray()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            ch = self._ser.read(1)
            if not ch:
                break
            if ch == b"#":
                break
            buf += ch
        return buf.decode("latin-1")

    def _command(self, cmd: str, reply: str = "hash") -> str:
        """Envía un comando. ``reply``: 'hash' (hasta '#'), 'char' (1 byte) o 'none'."""
        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write(cmd.encode("ascii"))
            if reply == "none":
                return ""
            if reply == "char":
                return self._ser.read(1).decode("latin-1")
            return self._read_until_hash()

    def _ensure_high_precision(self) -> None:
        ra = self._command(":GR#")
        if "." in ra:  # formato corto HH:MM.T -> alternar a precisión alta
            self._command(":U#", reply="none")

    # --- interfaz RaDecMount ------------------------------------------------------
    def position_radec(self) -> tuple[float, float]:
        ra = parse_lx200_angle(self._command(":GR#"), hours=True)
        dec = parse_lx200_angle(self._command(":GD#"), hours=False)
        if self._jnow:
            ra, dec = date_to_j2000(ra, dec, julian_date())
        return ra, dec

    def _set_target(self, ra: float, dec: float) -> None:
        if self._jnow:
            ra, dec = j2000_to_date(ra, dec, julian_date())
        if self._command(f":Sr{format_ra_lx200(ra)}#", reply="char") != "1":
            raise RuntimeError("La montura rechazó la AR del objetivo")
        if self._command(f":Sd{format_dec_lx200(dec)}#", reply="char") != "1":
            raise RuntimeError("La montura rechazó la declinación del objetivo")

    def goto_radec(self, ra: float, dec: float) -> None:
        self._set_target(ra, dec)
        code = self._command(":MS#", reply="char")
        if code != "0":
            detail = self._read_until_hash()
            raise RuntimeError(f"La montura no puede ir al objetivo ({code}): {detail}")

    def sync_radec(self, ra: float, dec: float) -> None:
        self._set_target(ra, dec)
        self._command(":CM#")

    def is_moving(self) -> bool:
        # ':D#' devuelve una barra de distancia: vacía ('#') cuando no hay GoTo en curso.
        return self._command(":D#").strip() != ""

    def stop(self) -> None:
        self._command(":Q#", reply="none")

    def set_tracking(self, enabled: bool) -> None:
        try:
            self._command(":Te#" if enabled else ":Td#", reply="char")  # OnStep
        except Exception:  # noqa: BLE001 - no todas las monturas lo soportan
            log.info("La montura no acepta comandos de seguimiento")

    def close(self) -> None:
        self._ser.close()
