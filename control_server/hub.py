"""Registro de nodos conectados, último cuadro de video, telemetría y eventos."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections import deque

from fastapi import WebSocket

from astrocore.coords import Site

log = logging.getLogger(__name__)


class NodeSession:
    """Estado de un telescopio (conectado o no)."""

    def __init__(self, node_id: str, name: str):
        self.node_id = node_id
        self.name = name
        self.info: dict = {}
        self.ws: WebSocket | None = None
        self.online = False
        self.connected_at: float | None = None
        self.last_seen: float | None = None
        self.jpeg: bytes | None = None
        self.frame_ts = 0.0
        self.frame_seq = 0
        self._frame_event = asyncio.Event()
        self.telemetry: dict | None = None
        self.analysis: dict | None = None
        self.enrichment: dict | None = None
        self._offsets: deque[float] = deque(maxlen=30)
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ datos derivados
    @property
    def site(self) -> Site | None:
        s = self.info.get("site")
        return Site(s["latitude"], s["longitude"], s.get("elevation_m", 0.0)) if s else None

    @property
    def fov(self) -> tuple[float, float]:
        o = self.info.get("optics", {})
        return o.get("fov_w", 1.0), o.get("fov_h", 0.75)

    @property
    def min_altitude(self) -> float:
        return self.info.get("mount", {}).get("min_altitude_deg", 10.0)

    @property
    def max_rate(self) -> float:
        return self.info.get("mount", {}).get("max_slew_deg_s", 2.0)

    @property
    def clock_offset_s(self) -> float | None:
        """Diferencia aproximada reloj servidor - reloj nodo (incluye la latencia mínima)."""
        return min(self._offsets, key=abs) if self._offsets else None

    # ------------------------------------------------------------------ video
    def set_frame(self, jpeg: bytes, ts: float) -> None:
        self.jpeg, self.frame_ts = jpeg, ts
        self.frame_seq += 1
        event, self._frame_event = self._frame_event, asyncio.Event()
        event.set()

    async def wait_frame(self, timeout: float) -> bool:
        event = self._frame_event
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except TimeoutError:
            return False

    # ------------------------------------------------------------------ mensajes
    def on_telemetry(self, data: dict) -> None:
        self.telemetry = data
        if "t" in data:
            self._offsets.append(time.time() - float(data["t"]))

    def on_ack(self, data: dict) -> None:
        future = self._pending.pop(str(data.get("cmd_id")), None)
        if future is not None and not future.done():
            future.set_result((bool(data.get("ok")), str(data.get("message", ""))))

    async def send(self, msg: dict) -> None:
        if self.ws is None:
            raise ConnectionError(f"{self.name} no está conectado")
        async with self._send_lock:
            await self.ws.send_text(json.dumps(msg))

    async def command(self, msg: dict, timeout: float = 8.0) -> tuple[bool, str]:
        """Envía un comando y espera la confirmación (ack) del nodo."""
        if not self.online:
            return False, f"{self.name} no está conectado"
        cmd_id = f"{self.node_id}-{next(self._ids)}"
        future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future
        try:
            await self.send({**msg, "cmd_id": cmd_id})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            return False, f"{self.name} no respondió a tiempo"
        except Exception as exc:  # noqa: BLE001
            return False, f"{self.name}: {exc}"
        finally:
            self._pending.pop(cmd_id, None)

    def fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_result((False, f"{self.name} se desconectó"))
        self._pending.clear()


class UIClient:
    def __init__(self, ws: WebSocket, authorized: bool):
        self.ws = ws
        self.authorized = authorized
        self._lock = asyncio.Lock()

    async def send_text(self, text: str) -> None:
        async with self._lock:
            await self.ws.send_text(text)


class Hub:
    def __init__(self, expected_nodes: list[tuple[str, str]]):
        self.nodes: dict[str, NodeSession] = {nid: NodeSession(nid, name) for nid, name in expected_nodes}
        self.ui_clients: set[UIClient] = set()
        self.events: deque[dict] = deque(maxlen=200)
        self._event_ids = itertools.count(1)

    def session(self, node_id: str, name: str | None = None) -> NodeSession:
        if node_id not in self.nodes:
            self.nodes[node_id] = NodeSession(node_id, name or node_id)
        return self.nodes[node_id]

    def online(self, node_ids: list[str] | None = None) -> list[NodeSession]:
        return [s for s in self.nodes.values() if s.online and (node_ids is None or s.node_id in node_ids)]

    def log(self, level: str, text: str, node_id: str | None = None) -> dict:
        event = {"id": next(self._event_ids), "t": time.time(), "level": level, "text": text, "node": node_id}
        self.events.append(event)
        log.log(logging.WARNING if level in ("warning", "error") else logging.INFO,
                "%s%s", f"[{node_id}] " if node_id else "", text)
        return event

    def events_after(self, event_id: int) -> list[dict]:
        return [e for e in self.events if e["id"] > event_id]
