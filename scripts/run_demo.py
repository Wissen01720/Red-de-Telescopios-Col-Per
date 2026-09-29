"""Demostración local: servidor + dos nodos simulados (Colombia y Perú).

Uso:  python scripts/run_demo.py [--port 8000] [--no-browser] [--host 0.0.0.0] [--light]

Abre http://localhost:8000 . Ctrl+C detiene todo.
--light usa resolución y cuadros por segundo reducidos (para servidores gratuitos en la nube).
El puerto también se puede dar con la variable de entorno PORT (Render, Hugging Face, ...).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def wait_for(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para aceptar conexiones externas")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--light", action="store_true", help="configuración liviana para la nube")
    args = parser.parse_args()

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    env.setdefault("OBS_NODE_TOKEN", "demo-token-cambiar")
    server_ws = f"ws://127.0.0.1:{args.port}/ws/node"
    procs = [subprocess.Popen([sys.executable, "-m", "control_server", "--host", args.host,
                               "--port", str(args.port)], cwd=ROOT, env=env)]
    try:
        if not wait_for(f"http://127.0.0.1:{args.port}/healthz"):
            print("El servidor no arrancó")
            return 1
        folder = "config/demo_ligero" if args.light else "config"
        for cfg in (f"{folder}/nodo_colombia.toml", f"{folder}/nodo_peru.toml"):
            procs.append(subprocess.Popen([sys.executable, "-m", "telescope_node", "-c", cfg,
                                           "--server", server_ws], cwd=ROOT, env=env))
        url = f"http://localhost:{args.port}"
        print(f"\n  Consola lista en {url}  (Ctrl+C para detener)\n")
        if not args.no_browser:
            webbrowser.open(url)
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        print("Un proceso terminó inesperadamente; deteniendo la demostración")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
