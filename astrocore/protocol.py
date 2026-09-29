"""Protocolo de mensajes entre los nodos (Raspberry Pi) y el servidor.

Transporte: un WebSocket que abre el nodo hacia el servidor
(``/ws/node``, autenticado con ``Authorization: Bearer <token>``).
Así los telescopios no necesitan IP pública ni abrir puertos.

* Mensajes de texto: JSON con un campo ``type``.
* Mensajes binarios: cuadros de video JPEG con una cabecera de 12 bytes
  (``FRM1`` + marca de tiempo de captura en float64 little-endian).

Nodo -> servidor
    hello      {node_id, name, site, optics, camera, mount, version}
    telemetry  {t, pointing, target, state, tracking, aligned, camera, frame}
    analysis   {t, result, pointing}
    event      {level, text}
    ack        {cmd_id, ok, message}

Servidor -> nodo
    goto       {cmd_id, ra, dec, name, body?, start_at?, duration?}
    stop       {cmd_id}
    tracking   {cmd_id, enabled}
    sync       {cmd_id, ra, dec, name, body?}   (alineación: "estoy apuntando a...")
    park       {cmd_id}
    unpark     {cmd_id}
    camera     {cmd_id, exposure_ms?, gain?}
"""

from __future__ import annotations

import struct

PROTOCOL_VERSION = 1

FRAME_MAGIC = b"FRM1"
_FRAME_HEADER = struct.Struct("<4sd")


def pack_frame(jpeg: bytes, capture_ts: float) -> bytes:
    return _FRAME_HEADER.pack(FRAME_MAGIC, capture_ts) + jpeg


def unpack_frame(data: bytes) -> tuple[bytes, float]:
    if len(data) < _FRAME_HEADER.size:
        raise ValueError("Cuadro demasiado corto")
    magic, ts = _FRAME_HEADER.unpack_from(data)
    if magic != FRAME_MAGIC:
        raise ValueError("Cabecera de cuadro inválida")
    return data[_FRAME_HEADER.size:], ts
