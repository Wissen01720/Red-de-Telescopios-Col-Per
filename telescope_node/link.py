"""Enlace WebSocket del nodo hacia el servidor central, con reconexión automática."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

from astrocore.protocol import pack_frame

if TYPE_CHECKING:
    from .node import TelescopeNode

log = logging.getLogger(__name__)

TELEMETRY_PERIOD_S = 1.0


class ServerLink:
    def __init__(self, node: "TelescopeNode"):
        self.node = node
        self.connected = False
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    async def _send(self, ws: ClientConnection, data: str | bytes) -> None:
        async with self._send_lock:
            await ws.send(data)

    async def _send_json(self, ws: ClientConnection, msg: dict) -> None:
        await self._send(ws, json.dumps(msg))

    async def run(self) -> None:
        cfg = self.node.cfg.node
        backoff = 1.0
        while True:
            try:
                async with connect(cfg.server_url, additional_headers={"Authorization": f"Bearer {cfg.token}"},
                                   max_size=2 ** 22, open_timeout=10) as ws:
                    self.connected = True
                    backoff = 1.0
                    log.info("Conectado al servidor %s", cfg.server_url)
                    await self._send_json(ws, self.node.hello())
                    sender = asyncio.create_task(self._sender(ws))
                    try:
                        async for raw in ws:
                            if isinstance(raw, str):
                                # Los comandos se atienden en paralelo para no bloquear la recepción.
                                task = asyncio.create_task(self._handle(ws, json.loads(raw)))
                                self._tasks.add(task)
                                task.add_done_callback(self._tasks.discard)
                    finally:
                        sender.cancel()
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                code = exc.response.status_code
                reason = "token rechazado" if code in (401, 403) else f"HTTP {code}"
                log.error("El servidor rechazó la conexión (%s)", reason)
            except Exception as exc:  # noqa: BLE001 - red caída, servidor apagado, etc.
                log.warning("Sin conexión con el servidor (%s: %s)", type(exc).__name__, exc)
            self.connected = False
            log.info("Reintentando en %.0f s", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _handle(self, ws: ClientConnection, msg: dict) -> None:
        ok, message = await self.node.handle_command(msg)
        try:
            await self._send_json(ws, {"type": "ack", "cmd_id": msg.get("cmd_id"), "command": msg.get("type"),
                                       "ok": ok, "message": message})
            await self._send_json(ws, await asyncio.to_thread(self.node.telemetry))
        except Exception:  # noqa: BLE001 - la conexión pudo cerrarse
            pass

    async def _sender(self, ws: ClientConnection) -> None:
        loop = asyncio.get_running_loop()
        last_frame = last_analysis = -1
        last_telemetry = 0.0
        while True:
            node = self.node
            if node.frame_seq != last_frame and node.jpeg is not None:
                last_frame = node.frame_seq
                await self._send(ws, pack_frame(node.jpeg, node.frame_ts))
            if loop.time() - last_telemetry >= TELEMETRY_PERIOD_S:
                last_telemetry = loop.time()
                await self._send_json(ws, await asyncio.to_thread(node.telemetry))
            if node.analysis_seq != last_analysis and node.analysis is not None:
                last_analysis = node.analysis_seq
                await self._send_json(ws, {"type": "analysis", "t": node.analysis.get("frame_ts"),
                                           "result": node.analysis})
            for event in await asyncio.to_thread(node.drain_events):
                await self._send_json(ws, event)
            await asyncio.sleep(0.05)
