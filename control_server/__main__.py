"""Punto de entrada:  python -m control_server --host 0.0.0.0 --port 8000"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import create_app
from .settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Servidor web de la red de telescopios")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    app = create_app(Settings.from_env())
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level, ws_max_size=8 * 1024 * 1024)
    return 0


if __name__ == "__main__":
    sys.exit(main())
