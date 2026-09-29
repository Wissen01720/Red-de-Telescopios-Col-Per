"""Servidor: autenticación, protocolo de nodos, GoTo sincronizado y prueba de punta a punta."""

import asyncio
import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from astrocore.coords import Site, altaz_to_radec, julian_date, radec_to_altaz
from astrocore.ephemeris import body_position
from astrocore.protocol import pack_frame
from control_server.app import create_app
from control_server.settings import Settings

TOKEN = "token-de-prueba"
SITES = {"colombia": Site(4.638, -74.084, 2600), "peru": Site(-12.046, -77.043, 150)}


def settings(tmp_path, password=""):
    return Settings(node_token=TOKEN, control_password=password, captures_dir=tmp_path / "capturas",
                    state_interval_s=0.1)


def hello(node_id):
    s = SITES[node_id]
    return {"type": "hello", "node_id": node_id, "name": node_id.title(),
            "site": {"latitude": s.latitude, "longitude": s.longitude, "elevation_m": s.elevation_m,
                     "utc_offset_hours": -5},
            "optics": {"fov_w": 1.2, "fov_h": 0.9},
            "camera": {"flip_x": False},
            "mount": {"min_altitude_deg": 10, "max_slew_deg_s": 3.0, "sun_avoidance_deg": 20}}


def telemetry(node_id, ra=10.0, dec=-10.0):
    return {"type": "telemetry", "t": time.time(), "state": "idle", "tracking": True, "aligned": True,
            "pointing": {"ra": ra, "dec": dec, "alt": 45.0, "az": 90.0, "rotation": 0.0, "lst_deg": 0, "ha_deg": 0},
            "target": None}


def visible_target():
    """Coordenadas a 60° de altura en ambos sitios y lejos del Sol, sea la hora que sea."""
    jd = julian_date()
    sun = body_position("sun", jd)
    _, sun_az = radec_to_altaz(sun.ra, sun.dec, SITES["colombia"], jd)
    ra, dec = altaz_to_radec(70.0, (sun_az + 180) % 360, SITES["colombia"], jd)
    return f"{ra:.4f} {dec:.4f}"


def receive_until(ws, kind):
    for _ in range(200):
        msg = ws.receive_json()
        if msg.get("type") == kind:
            return msg
    raise AssertionError(f"No llegó un mensaje {kind}")


def test_node_with_wrong_token_is_rejected(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/node", headers={"Authorization": "Bearer malo"}) as ws:
                ws.receive_text()


def test_node_frames_and_state(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/node", headers={"Authorization": f"Bearer {TOKEN}"}) as node:
            node.send_json(hello("colombia"))
            node.send_json(telemetry("colombia", 83.82, -5.39))
            node.send_bytes(pack_frame(b"\xff\xd8JPEGDATA\xff\xd9", time.time()))
            node.send_json({"type": "analysis", "result": {"n_stars": 12, "scene_label": "Campo estelar"}})
            for _ in range(50):
                state = client.get("/api/state").json()
                col = next(n for n in state["nodes"] if n["id"] == "colombia")
                if col["online"] and col["analysis"] and col["frame"]["seq"]:
                    break
                time.sleep(0.05)
            assert col["online"] and col["analysis"]["n_stars"] == 12
            assert col["enrichment"]["constellation"] == "Ori"
            assert any(o["id"] == "M 42" for o in col["enrichment"]["in_field"])
            assert client.get("/snapshot/colombia.jpg").content.startswith(b"\xff\xd8")
        time.sleep(0.2)
        state = client.get("/api/state").json()
        assert not next(n for n in state["nodes"] if n["id"] == "colombia")["online"]


def test_synchronized_goto_reaches_both_nodes(tmp_path):
    app = create_app(settings(tmp_path))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app) as client, \
            client.websocket_connect("/ws/node", headers=auth) as col, \
            client.websocket_connect("/ws/node", headers=auth) as per, \
            client.websocket_connect("/ws/ui") as ui:
        for ws, nid in ((col, "colombia"), (per, "peru")):
            ws.send_json(hello(nid))
            ws.send_json(telemetry(nid))
        time.sleep(0.3)
        ui.send_json({"type": "goto", "target": visible_target(), "nodes": "all", "req_id": 1})
        commands = []
        for ws in (col, per):
            cmd = ws.receive_json()
            commands.append(cmd)
            ws.send_json({"type": "ack", "cmd_id": cmd["cmd_id"], "ok": True, "message": "Apuntando"})
        assert all(c["type"] == "goto" for c in commands)
        assert commands[0]["start_at"] == commands[1]["start_at"] > time.time()   # arranque simultáneo
        assert commands[0]["duration"] == commands[1]["duration"] > 0              # llegada simultánea
        result = receive_until(ui, "result")
        assert result["ok"] and len(result["details"]) == 2


def test_control_password_is_enforced(tmp_path):
    app = create_app(settings(tmp_path, password="secreta"))
    with TestClient(app) as client, client.websocket_connect("/ws/ui") as ui:
        welcome = ui.receive_json()
        assert welcome["control_required"] and not welcome["authorized"]
        ui.send_json({"type": "stop", "req_id": 1})
        assert not receive_until(ui, "result")["ok"]
        ui.send_json({"type": "auth", "password": "mala", "req_id": 2})
        assert not receive_until(ui, "result")["authorized"]
        ui.send_json({"type": "auth", "password": "secreta", "req_id": 3})
        assert receive_until(ui, "result")["authorized"]
        r = client.post("/api/command", json={"type": "stop"})
        assert r.status_code == 401


def test_search_and_resolve_api(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        assert any(s["label"] == "M 42" for s in client.get("/api/search?q=M42").json())
        assert client.get("/api/resolve?q=Omega Centauri").json()["object"]["id"] == "NGC 5139"
        assert client.get("/api/resolve?q=nada-xyz").status_code == 404
        sky = client.get("/api/skymap?ra=83.8&dec=-5.4&radius=3").json()
        assert sky["stars"] and any(d[3] == "M 42" for d in sky["dsos"])


# --------------------------------------------------------------------------- punta a punta

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_end_to_end_with_simulated_node(tmp_path):
    import httpx
    import uvicorn

    from telescope_node.config import NodeConfig
    from telescope_node.node import TelescopeNode

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings(tmp_path)), host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()

    cfg = NodeConfig()
    cfg.node.id, cfg.node.name, cfg.node.token = "peru", "Perú", TOKEN
    cfg.node.server_url = f"ws://127.0.0.1:{port}/ws/node"
    cfg.node.state_file = str(tmp_path / "estado.json")
    cfg.site.latitude, cfg.site.longitude = SITES["peru"].latitude, SITES["peru"].longitude
    cfg.camera.width, cfg.camera.height, cfg.camera.fps = 640, 480, 4
    cfg.camera.simulated.force_night = True
    cfg.mount.max_slew_deg_s = 20.0
    cfg.analysis.interval_s = 0.5
    loop = asyncio.new_event_loop()
    node = TelescopeNode(cfg)
    task = loop.create_task(node.run())

    def run_node():
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass

    threading.Thread(target=run_node, daemon=True).start()

    base = f"http://127.0.0.1:{port}"
    try:
        def peru():
            return next(n for n in httpx.get(f"{base}/api/state").json()["nodes"] if n["id"] == "peru")

        deadline = time.time() + 20
        while time.time() < deadline and not (peru()["online"] and peru()["analysis"]):
            time.sleep(0.3)
        assert peru()["online"]
        # Objetivo visible desde Lima ahora mismo y lejos del Sol.
        jd = julian_date()
        sun = body_position("sun", jd)
        _, sun_az = radec_to_altaz(sun.ra, sun.dec, SITES["peru"], jd)
        ra, dec = altaz_to_radec(60.0, (sun_az + 180) % 360, SITES["peru"], jd)
        r = httpx.post(f"{base}/api/command", json={"type": "goto", "target": f"{ra:.4f} {dec:.4f}"}, timeout=15).json()
        assert r["ok"], r
        deadline = time.time() + 30
        while time.time() < deadline and peru()["telemetry"]["state"] != "tracking":
            time.sleep(0.3)
        tel = peru()["telemetry"]
        assert tel["state"] == "tracking" and tel["target"]["error_deg"] < 0.05
        assert httpx.get(f"{base}/snapshot/peru.jpg").content[:2] == b"\xff\xd8"
        assert peru()["enrichment"]["summary"]
    finally:
        loop.call_soon_threadsafe(task.cancel)
        server.should_exit = True
        time.sleep(0.5)


def test_nudge_without_target_explains_instead_of_pointing_at_horizon(tmp_path):
    app = create_app(settings(tmp_path))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app) as client, client.websocket_connect("/ws/node", headers=auth) as col, \
            client.websocket_connect("/ws/ui") as ui:
        col.send_json(hello("colombia"))
        home = telemetry("colombia", 45.0, 84.9)          # posición de inicio: horizonte norte
        home["pointing"]["alt"] = 0.0
        col.send_json(home)
        time.sleep(0.3)
        for req, msg in enumerate(({"type": "nudge", "direction": "N", "arcmin": 15, "nodes": "all"},
                                   {"type": "center", "node": "colombia", "x": 0.7, "y": 0.3}), start=1):
            ui.send_json({**msg, "req_id": req})
            result = receive_until(ui, "result")
            assert not result["ok"]
            assert "Primero" in result["message"]


def test_visible_targets_are_above_limit_at_every_site(tmp_path):
    app = create_app(settings(tmp_path))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app) as client, \
            client.websocket_connect("/ws/node", headers=auth) as col, \
            client.websocket_connect("/ws/node", headers=auth) as per:
        for ws, nid in ((col, "colombia"), (per, "peru")):
            ws.send_json(hello(nid))
            ws.send_json(telemetry(nid))
        time.sleep(0.3)
        data = client.get("/api/visible").json()
        assert {s["node"] for s in data["sites"]} == {"colombia", "peru"}
        for t in data["targets"]:
            assert t["min_alt"] >= 15
            resolved = client.get(f"/api/resolve?q={t['label']}").json()
            assert all(v["ok"] for v in resolved["visibility"].values())
