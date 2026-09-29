"""Configuración del servidor por variables de entorno.

OBS_NODE_TOKEN        Token compartido que deben presentar los nodos.
OBS_CONTROL_PASSWORD  Contraseña para mover los telescopios desde la página.
                      Si está vacía, cualquiera que abra la página puede controlar.
OBS_NODES             Nodos esperados, 'id:Nombre' separados por comas
                      (se muestran aunque estén desconectados).
OBS_CAPTURES_DIR      Carpeta donde se guardan las capturas simultáneas.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _parse_nodes(text: str) -> list[tuple[str, str]]:
    nodes = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        node_id, _, name = item.partition(":")
        nodes.append((node_id.strip(), (name or node_id).strip()))
    return nodes


@dataclass
class Settings:
    node_token: str = "demo-token-cambiar"
    control_password: str = ""
    expected_nodes: list[tuple[str, str]] = field(default_factory=lambda: [("colombia", "Colombia"), ("peru", "Perú")])
    captures_dir: Path = Path("capturas")
    state_interval_s: float = 0.5

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.node_token = os.environ.get("OBS_NODE_TOKEN", s.node_token)
        s.control_password = os.environ.get("OBS_CONTROL_PASSWORD", s.control_password)
        if "OBS_NODES" in os.environ:
            s.expected_nodes = _parse_nodes(os.environ["OBS_NODES"])
        s.captures_dir = Path(os.environ.get("OBS_CAPTURES_DIR", str(s.captures_dir)))
        return s
