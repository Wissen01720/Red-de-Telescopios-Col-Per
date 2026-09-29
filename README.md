# Red de Telescopios Colombia–Perú

Sistema en Python para operar dos telescopios básicos, uno en Colombia y otro en Perú, desde una sola página web:

- **Video en vivo** de ambas cámaras.
- **GoTo sincronizado**: ambos telescopios arrancan a la vez, llegan a la vez y apuntan al **mismo cuadrante** del cielo, aunque estén en sitios distintos (cada uno calcula su propia altura y azimut).
- **Coordenadas en vivo** de cada campo: AR/Dec J2000, altura/azimut, ángulo horario, tiempo sidéreo, constelación, esquinas del campo, hora local.
- **Análisis en vivo**: detección de estrellas, enfoque (FWHM), seguimiento (elongación), nubes/gradientes, saturación, Luna/planetas, más **qué se está viendo** según los catálogos (Messier, NGC/IC, estrellas hasta mag 6.5, planetas y Luna).
- Seguridad: no apunta bajo el horizonte ni cerca del Sol; token para los nodos y contraseña de control.

Incluye un **simulador completo** (cámara que renderiza el cielo real + montura), así que todo se puede probar en un PC sin hardware.

---

## Arquitectura

```
 Colombia                                         Perú
 ┌──────────────────────────┐                     ┌──────────────────────────┐
 │ Raspberry Pi             │                     │ Raspberry Pi             │
 │  telescope_node          │                     │  telescope_node          │
 │  ├ cámara (Picamera2/USB)│                     │  ├ cámara                │
 │  ├ montura (pasos/LX200) │                     │  ├ montura               │
 │  ├ seguimiento sidéreo   │                     │  ├ seguimiento sidéreo   │
 │  └ análisis de imagen    │                     │  └ análisis de imagen    │
 └────────────┬─────────────┘                     └─────────────┬────────────┘
              │  WebSocket saliente (video JPEG + telemetría,   │
              │  recibe comandos). No requiere abrir puertos.   │
              └──────────────────►┌──────────────┐◄─────────────┘
                                  │ control_server│  FastAPI
                                  │  · coordina GoTo sincronizado
                                  │  · catálogos y "qué se ve"
                                  │  · video MJPEG a la página
                                  └──────┬───────┘
                                         │ HTTP + WebSocket
                                   Navegador (consola web)
```

| Carpeta | Contenido |
|---|---|
| `astrocore/` | Núcleo astronómico compartido, sin dependencias pesadas: tiempo sidéreo, precesión, RA/Dec↔Alt/Az, refracción, Sol/Luna/planetas (Luna topocéntrica), constelaciones IAU, catálogos, geometría del campo, protocolo. |
| `telescope_node/` | Programa de la **Raspberry Pi** (parte 1). |
| `control_server/` | **Servidor y página web** (parte 2). |
| `config/` | Configuraciones de ejemplo de los nodos. |
| `deploy/` | Servicios systemd y proxy HTTPS (Caddy). |
| `scripts/` | Demostración local y regeneración de catálogos. |
| `tests/` | 86 pruebas automáticas (la astronomía se valida contra astropy). |

---

## Probarlo ya (PC con Windows, macOS o Linux)

Requiere Python 3.11 o superior.

```bash
pip install -r requirements-dev.txt
python scripts/run_demo.py
```

Se abre `http://localhost:8000` con dos telescopios simulados (Bogotá y Lima). Prueba:

1. Pulsa un objeto de **Visibles ahora** (solo aparecen objetos brillantes que en este momento están altos en *ambos* sitios): los dos telescopios arrancan juntos y llegan juntos; la tarjeta de sincronía pasa a **Mismo cuadrante**.
2. Ya con un objetivo, usa **▲ N / ◀ E / O ▶ / ▼ S** para recorrer el cielo con ambos a la vez. (Antes de elegir un objetivo están desactivados: al encender, los telescopios apuntan al horizonte.)
3. En Perú el objeto aparece descentrado a propósito (error de montaje simulado). Elige **Perú** en "Controlar", haz **clic sobre el objeto en su imagen** para centrarlo y luego **Alinear…** con el nombre del objeto. Los siguientes GoTo de Perú quedarán centrados.
4. **Capturar** guarda la imagen de ambos telescopios con sus metadatos en `capturas/`.

> Los nodos de demostración tienen `force_night = true` para ver estrellas aunque sea de día. Las condiciones del sitio (Sol, Luna) que muestra la página son las reales.

Para ejecutar las piezas por separado:

```bash
python -m control_server --port 8000
python -m telescope_node -c config/nodo_colombia.toml
python -m telescope_node -c config/nodo_peru.toml
```

---

## Parte 1: Raspberry Pi en el telescopio

### Hardware soportado

| Componente | Opciones (`driver`) |
|---|---|
| Cámara | `picamera2`: Raspberry Pi HQ Camera (recomendada, montura C/CS con adaptador a 1.25"), Camera Module 3. `opencv`: webcams y cámaras planetarias UVC. `simulated`. |
| Montura | `stepper`: dos motores paso a paso (NEMA 17 + A4988/DRV8825/TMC2209) en una montura altacimutal. `lx200`: monturas GoTo por puerto serie (OnStep/OnStepX, Meade, compatibles LX200). `simulated`. |
| Raspberry | Pi 4 o Pi 5 con Raspberry Pi OS Bookworm (64 bits). |

### Instalación

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-lgpio python3-numpy python3-opencv git
git clone <este-repositorio> ~/telescopios && cd ~/telescopios
python3 -m venv --system-site-packages .venv
.venv/bin/pip install "websockets>=13" pyserial   # la versión de apt (10.x) es muy antigua
cp config/nodo_hardware_ejemplo.toml config/nodo.toml
nano config/nodo.toml        # id, token, servidor, coordenadas del sitio, cámara, montura
.venv/bin/python -m telescope_node -c config/nodo.toml
```

Para que arranque solo al encender: `deploy/telescopio-nodo.service` (instrucciones dentro del archivo).

**El reloj debe estar sincronizado** (NTP): el GoTo sincronizado usa una hora de inicio común. Verifica con `timedatectl` que diga *System clock synchronized: yes*. La página avisa si el reloj de un nodo se desvía más de 2 s.

### Cableado de motores (driver `stepper`)

Por eje: GPIO STEP → STEP del driver, GPIO DIR → DIR, GND común con la Pi, y opcionalmente un GPIO a EN. Los pines por defecto (numeración BCM) están en `config/nodo_hardware_ejemplo.toml`. Los motores se alimentan con su propia fuente (12 V), **nunca** desde la Raspberry.

Ajusta `steps_per_rev`, `microsteps` y `*_gear_ratio` a tu mecánica: la relación determina cuántos pasos corresponden a un grado del eje. Si un eje gira al revés, cambia `alt_invert`/`az_invert`.

### Puesta en marcha en el sitio

1. **Nivela** la montura y deja el tubo en la posición de inicio (`home_alt`, `home_az`: por defecto horizontal apuntando al Norte).
2. Enciende el nodo. Aparecerá en la página como *En línea · sin alinear*.
3. Haz GoTo a una estrella brillante (p. ej. Sirio, Vega, Canopo). Si queda descentrada, haz clic sobre ella en la imagen para centrarla y pulsa **Alinear…** con su nombre.
4. Ajusta `camera.rotation_deg` y `flip_x` hasta que las etiquetas de catálogo coincidan con lo que se ve.
5. Al terminar, **Aparcar**: el telescopio vuelve a la posición de inicio y conserva la alineación para la próxima noche.

---

## Parte 2: servidor y página web

```bash
pip install -r requirements-servidor.txt
export OBS_NODE_TOKEN="un-token-largo-y-secreto"      # el mismo en la config de cada nodo
export OBS_CONTROL_PASSWORD="contraseña-para-mover"    # vacío = cualquiera puede mover
python -m control_server --host 0.0.0.0 --port 8000
```

| Variable | Uso |
|---|---|
| `OBS_NODE_TOKEN` | Token que presentan los nodos al conectarse. |
| `OBS_CONTROL_PASSWORD` | Contraseña para mover los telescopios desde la página (ver sin contraseña sigue permitido). |
| `OBS_NODES` | Nodos esperados, `id:Nombre` separados por comas (por defecto `colombia:Colombia,peru:Perú`). |
| `OBS_CAPTURES_DIR` | Carpeta de capturas (por defecto `capturas`). |

**¿Dónde ponerlo?** El servidor necesita un **proceso que quede corriendo** y **WebSockets** (los telescopios mantienen una conexión abierta y el video se transmite continuamente).

> **Vercel no sirve para este servidor.** Vercel ejecuta funciones *serverless*: cada petición vive unos segundos, no hay proceso permanente, no guarda estado entre peticiones y no acepta WebSockets. Allí solo se podría alojar la página estática, que igual necesitaría este servidor corriendo en otro lugar.

### Demostración pública (telescopios simulados)

El `Dockerfile` levanta en un solo contenedor el servidor y los dos telescopios simulados, en modo liviano (640×480, ~1.5 cuadros/s):

- **Hugging Face Spaces** (gratis, recomendado para la vista previa): crea un Space de tipo *Docker* y sube el proyecto. El `README.md` del Space debe empezar con un encabezado YAML que incluya `sdk: docker` y `app_port: 8000` (el puerto en el que escucha el contenedor).
- **Render** (plan gratuito; se duerme tras 15 min sin visitas y tarda ~1 min en despertar): *New → Web Service* desde el repositorio, entorno *Docker*. Render define `PORT` automáticamente.
- Localmente: `docker build -t red-telescopios . && docker run -p 8000:8000 red-telescopios`.

Si defines `OBS_CONTROL_PASSWORD` en el host, cualquiera podrá ver la demo pero solo quien tenga la contraseña podrá mover los telescopios.

### Operación real

Los nodos solo necesitan poder *salir* a internet hacia el servidor:

- **Servidor en la nube / servidor de la universidad con IP pública**: usa `deploy/telescopio-servidor.service` y `deploy/Caddyfile` (HTTPS automático). Los nodos usan `wss://tu-dominio/ws/node`.
- **Sin IP pública**: instala Tailscale o ZeroTier en el servidor y en ambas Raspberry (red privada entre países), o publica la página con Cloudflare Tunnel.

API útil para scripts: `POST /api/command` acepta los mismos comandos que la página (encabezado `X-Control-Password`), por ejemplo `{"type": "goto", "target": "M42"}`. También: `GET /api/state`, `/api/resolve?q=`, `/api/search?q=`, `/api/skymap`, `/snapshot/<nodo>.jpg`, `/stream/<nodo>.mjpg`.

---

## Cómo funciona la sincronización

1. El servidor resuelve el objetivo a **AR/Dec J2000** (el mismo para ambos sitios) y verifica en cada sitio que esté sobre el límite de altura y lejos del Sol.
2. Estima el tiempo de giro de cada montura con su posición actual y velocidad máxima, y envía a ambos nodos el mismo `start_at` (hora UTC de inicio, 1.5 s en el futuro) y la misma `duration` (la del más lento).
3. Cada nodo convierte AR/Dec a su propia altura/azimut (latitud, longitud, tiempo sidéreo, precesión y refracción) y ajusta la velocidad para llegar justo al final de `duration`.
4. Luego cada nodo hace **seguimiento sidéreo** recalculando su posición cada 0.5 s.
5. La página compara las posiciones reales de ambos campos: separación angular y porcentaje de superposición → *Mismo cuadrante / Parcial / Distinto*.

Para la **Luna y los planetas** cada nodo calcula la posición **topocéntrica** desde su sitio. Con la Luna, Colombia y Perú ven el mismo objeto en posiciones del cielo que difieren hasta ~0.3° (paralaje); la página muestra ese valor esperado. Un proyecto bonito: medir ese paralaje con capturas simultáneas y estimar la distancia a la Luna.

---

## Pruebas

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Incluyen: conversiones de coordenadas, precesión, planetas, Luna (geocéntrica y topocéntrica) y constelaciones contra **astropy**; detección de estrellas en imágenes sintéticas; seguimiento, alineación, límites de seguridad y aparcado con reloj simulado; conteo exacto de pasos de los motores; protocolo del servidor; y una prueba de punta a punta servidor + nodo simulado.

Precisión medida frente a astropy: Alt/Az < 25″, Luna topocéntrica ~0.1′, planetas < 5′.

---

## Próximos pasos sugeridos

- **Resolución astrométrica (plate solving)** con ASTAP en la Raspberry: confirmar el apuntado real a partir de la imagen y alinear automáticamente.
- Video por **WebRTC** para menor latencia y ancho de banda.
- Captura simultánea a una hora UTC exacta (para paralaje lunar, ocultaciones, meteoros).
- Montura ecuatorial con motores propios (hoy el driver `stepper` es altacimutal).

## Datos

Catálogos en `astrocore/data/` (ver `FUENTES.md`): HYG v4.1 y OpenNGC (CC BY-SA 4.0), límites de constelaciones de Roman (1987). Se regeneran con `python scripts/build_catalogs.py`.
