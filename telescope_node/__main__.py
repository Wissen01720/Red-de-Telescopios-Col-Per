"""Punto de entrada:  python -m telescope_node --config config/nodo_colombia.toml"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_config
from .node import TelescopeNode


def main() -> int:
    parser = argparse.ArgumentParser(description="Nodo de telescopio (Raspberry Pi)")
    parser.add_argument("-c", "--config", required=True, help="Archivo TOML de configuración")
    parser.add_argument("--server", help="Sobrescribe [node].server_url (ws://host:puerto/ws/node)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    cfg = load_config(args.config)
    if args.server:
        cfg.node.server_url = args.server
    node = TelescopeNode(cfg)
    try:
        asyncio.run(node.run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Nodo detenido por el usuario")
    return 0


if __name__ == "__main__":
    sys.exit(main())
