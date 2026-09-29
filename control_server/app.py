"""Aplicación web: WebSockets de nodos y de la interfaz, video MJPEG y API REST."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from astrocore import __version__
from astrocore.catalog import default_catalog
from astrocore.coords import julian_date
from astrocore.protocol import unpack_frame

from .coordinator import Coordinator
from .hub import Hub, NodeSession, UIClient
from .settings import Settings

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
CONTROL_COMMANDS = {"goto", "nudge", "center", "stop", "tracking", "park", "unpark", "align", "camera", "capture"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    catalog = default_catalog()
    hub = Hub(settings.expected_nodes)
    coordinator = Coordinator(hub, catalog)

    # ------------------------------------------------------------------ estado para la interfaz
    def node_state(s: NodeSession, now: float) -> dict:
        return {
            "id": s.node_id, "name": s.name, "online": s.online, "info": s.info,
            "telemetry": s.telemetry, "analysis": s.analysis, "enrichment": s.enrichment,
            "frame": {"seq": s.frame_seq, "ts": s.frame_ts, "age_s": now - s.frame_ts if s.frame_ts else None},
            "clock_offset_s": s.clock_offset_s,
            "last_seen_s": now - s.last_seen if s.last_seen else None,
        }

    def build_state() -> dict:
        now = time.time()
        for s in hub.nodes.values():
            try:
                s.enrichment = coordinator.enrich(s, now) if s.online else None
            except Exception:  # noqa: BLE001 - nunca debe tumbar la difusión
                log.exception("Error enriqueciendo el estado de %s", s.node_id)
        return {
            "type": "state", "t": now, "version": __version__,
            "nodes": [node_state(s, now) for s in hub.nodes.values()],
            "target": coordinator.common_target,
            "sync": coordinator.sync_status(),
        }

    async def broadcast_loop() -> None:
        last_event = 0
        while True:
            await asyncio.sleep(settings.state_interval_s)
            if not hub.ui_clients:
                last_event = hub.events[-1]["id"] if hub.events else 0
                continue
            try:
                messages = []
                new_events = hub.events_after(last_event)
                if new_events:
                    last_event = new_events[-1]["id"]
                    messages.append(json.dumps({"type": "events", "events": new_events}))
                messages.append(json.dumps(build_state()))
            except Exception:  # noqa: BLE001
                log.exception("Error construyendo el estado")
                continue
            for client in list(hub.ui_clients):
                try:
                    for text in messages:
                        await asyncio.wait_for(client.send_text(text), 2.0)
                except Exception:  # noqa: BLE001 - cliente lento o desconectado
                    hub.ui_clients.discard(client)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        if not settings.control_password:
            log.warning("OBS_CONTROL_PASSWORD vacío: cualquiera que abra la página puede mover los telescopios")
        task = asyncio.create_task(broadcast_loop())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Red de telescopios Colombia–Perú", version=__version__, lifespan=lifespan)
    app.state.hub = hub
    app.state.coordinator = coordinator
    app.state.settings = settings

    # ------------------------------------------------------------------ nodos
    @app.websocket("/ws/node")
    async def ws_node(ws: WebSocket) -> None:
        auth = ws.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(token.encode(), settings.node_token.encode()):
            await ws.close(code=1008)
            hub.log("warning", f"Conexión de nodo rechazada (token inválido) desde {ws.client.host if ws.client else '?'}")
            return
        await ws.accept()
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10.0))
        except Exception:  # noqa: BLE001
            await ws.close(code=1002)
            return
        node_id = str(hello.get("node_id", "")).strip()
        if hello.get("type") != "hello" or not node_id:
            await ws.close(code=1002)
            return
        session = hub.session(node_id, hello.get("name"))
        if session.online and session.ws is not None:
            with contextlib.suppress(Exception):
                await session.ws.close(code=1012)
        session.name = hello.get("name") or session.name
        session.info, session.ws, session.online = hello, ws, True
        session.connected_at = session.last_seen = time.time()
        hub.log("info", f"{session.name} conectado", node_id)
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                session.last_seen = time.time()
                if message.get("bytes") is not None:
                    try:
                        jpeg, ts = unpack_frame(message["bytes"])
                    except ValueError:
                        continue
                    session.set_frame(jpeg, ts)
                    continue
                data = json.loads(message.get("text") or "{}")
                kind = data.get("type")
                if kind == "telemetry":
                    session.on_telemetry(data)
                elif kind == "analysis":
                    session.analysis = data.get("result")
                elif kind == "event":
                    hub.log(data.get("level", "info"), f"{session.name}: {data.get('text', '')}", node_id)
                elif kind == "ack":
                    session.on_ack(data)
        except WebSocketDisconnect:
            pass
        finally:
            if session.ws is ws:
                session.online, session.ws = False, None
                session.fail_pending()
                hub.log("warning", f"{session.name} desconectado", node_id)

    # ------------------------------------------------------------------ interfaz web
    async def handle_ui(client: UIClient, msg: dict) -> dict:
        kind = msg.get("type")
        if kind == "auth":
            ok = bool(settings.control_password) and hmac.compare_digest(
                str(msg.get("password", "")).encode(), settings.control_password.encode())
            client.authorized = client.authorized or ok
            return {"ok": client.authorized,
                    "message": "Control habilitado" if client.authorized else "Contraseña incorrecta",
                    "authorized": client.authorized}
        if kind not in CONTROL_COMMANDS:
            return {"ok": False, "message": f"Comando desconocido: {kind}"}
        if not client.authorized:
            return {"ok": False, "message": "Se requiere la contraseña de control"}
        return await run_command(msg)

    async def run_command(msg: dict) -> dict:
        kind = msg.get("type")
        nodes = msg.get("nodes")
        node_ids = None if nodes in (None, "all") else list(nodes)
        if kind == "goto":
            return await coordinator.goto(msg.get("target", ""), node_ids)
        if kind == "nudge":
            return await coordinator.nudge(str(msg.get("direction", "")), float(msg.get("arcmin", 10)), node_ids)
        if kind in ("stop", "park", "unpark"):
            return await coordinator.broadcast(kind, node_ids)
        if kind == "tracking":
            return await coordinator.broadcast("tracking", node_ids, enabled=bool(msg.get("enabled", True)))
        if kind == "center":
            return await coordinator.center(str(msg.get("node")), float(msg.get("x", 0.5)), float(msg.get("y", 0.5)))
        if kind == "align":
            return await coordinator.align(str(msg.get("node")), msg.get("target", ""))
        if kind == "camera":
            fields = {k: float(msg[k]) for k in ("exposure_ms", "gain") if msg.get(k) is not None}
            return await coordinator.broadcast("camera", node_ids, **fields)
        if kind == "capture":
            return await asyncio.to_thread(coordinator.save_capture, settings.captures_dir)
        return {"ok": False, "message": f"Comando desconocido: {kind}"}

    @app.websocket("/ws/ui")
    async def ws_ui(ws: WebSocket) -> None:
        await ws.accept()
        client = UIClient(ws, authorized=not settings.control_password)
        await client.send_text(json.dumps({
            "type": "welcome", "control_required": bool(settings.control_password),
            "authorized": client.authorized, "events": list(hub.events)[-60:],
        }))
        await client.send_text(json.dumps(build_state()))
        hub.ui_clients.add(client)
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                try:
                    result = await handle_ui(client, msg)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Error atendiendo %s", msg.get("type"))
                    result = {"ok": False, "message": f"Error: {exc}"}
                await client.send_text(json.dumps({"type": "result", "req_id": msg.get("req_id"),
                                                   "command": msg.get("type"), **result}))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.ui_clients.discard(client)

    # ------------------------------------------------------------------ video
    @app.get("/stream/{node_id}.mjpg")
    async def mjpeg(node_id: str) -> StreamingResponse:
        if node_id not in hub.nodes:
            raise HTTPException(404, "Nodo desconocido")
        session = hub.nodes[node_id]

        async def frames():
            last = -1
            while True:
                if session.jpeg is not None and session.frame_seq != last:
                    last = session.frame_seq
                    jpeg = session.jpeg
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                await session.wait_frame(5.0)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/snapshot/{node_id}.jpg")
    async def snapshot(node_id: str) -> Response:
        session = hub.nodes.get(node_id)
        if session is None or session.jpeg is None:
            raise HTTPException(404, "Sin imagen")
        return Response(session.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ API REST
    @app.get("/api/state")
    async def api_state() -> dict:
        return build_state()

    @app.get("/api/search")
    async def api_search(q: str = Query("", max_length=80)) -> list[dict]:
        return catalog.search(q, limit=12)

    @app.get("/api/resolve")
    async def api_resolve(q: str = Query(..., max_length=120)) -> dict:
        now = time.time()
        obj = coordinator.resolve(q, now)
        if obj is None:
            raise HTTPException(404, f"No se encontró '{q}'")
        vis = {s.node_id: coordinator.visibility(obj, s, now) for s in hub.nodes.values() if s.site}
        return {"object": obj.to_dict(), "visibility": vis}

    @app.get("/api/visible")
    async def api_visible() -> dict:
        return await asyncio.to_thread(coordinator.visible_targets)

    @app.get("/api/skymap")
    async def api_skymap(ra: float, dec: float, radius: float = Query(5.0, ge=0.3, le=45.0)) -> dict:
        maglim = 7.0 if radius <= 2 else 6.5 if radius <= 5 else 5.5 if radius <= 12 else 4.5
        return catalog.objects_near(ra % 360.0, max(-90.0, min(90.0, dec)), radius, maglim, julian_date())

    @app.post("/api/command")
    async def api_command(msg: dict, x_control_password: str = Header("")) -> dict:
        """Mismos comandos que la interfaz (útil para scripts). Requiere X-Control-Password si hay contraseña."""
        if settings.control_password and not hmac.compare_digest(
                x_control_password.encode(), settings.control_password.encode()):
            raise HTTPException(401, "Contraseña de control incorrecta")
        if msg.get("type") not in CONTROL_COMMANDS:
            raise HTTPException(400, "Comando desconocido")
        return await run_command(msg)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "nodes_online": [s.node_id for s in hub.online()]})

    # ------------------------------------------------------------------ archivos estáticos
    settings.captures_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/capturas", StaticFiles(directory=settings.captures_dir), name="capturas")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app
