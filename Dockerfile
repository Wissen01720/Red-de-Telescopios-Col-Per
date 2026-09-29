# Demostración en la nube: servidor web + dos telescopios simulados en un solo contenedor.
# Funciona en hosts con procesos persistentes y WebSockets (Render, Hugging Face Spaces, Fly.io, una VM).
FROM python:3.12-slim

WORKDIR /app
COPY requirements-servidor.txt ./
RUN pip install --no-cache-dir -r requirements-servidor.txt opencv-python-headless "websockets>=13"

COPY astrocore ./astrocore
COPY telescope_node ./telescope_node
COPY control_server ./control_server
COPY config ./config
COPY scripts ./scripts

ENV PORT=8000 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
EXPOSE 8000
# Para que solo ustedes puedan mover los telescopios, definir OBS_CONTROL_PASSWORD en el host.
CMD ["sh", "-c", "python scripts/run_demo.py --host 0.0.0.0 --port ${PORT} --no-browser --light"]
