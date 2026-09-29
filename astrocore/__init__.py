"""Núcleo astronómico compartido por el nodo (Raspberry Pi) y el servidor web.

Solo depende de la biblioteca estándar y de numpy, para que funcione igual en
una Raspberry Pi que en un servidor.  Las coordenadas de catálogo se manejan
siempre en J2000 (grados); se precesan a la fecha solo al convertir a Alt/Az.
"""

__version__ = "0.1.0"
