# Imagen del backend de Connects Senior para Cloud Run (se construye con Cloud Build
# desde backend-fastapi/: gcloud run deploy --source .).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY app ./app

EXPOSE 8080

# PORT lo fija Cloud Run (8080 si nadie lo fija).
# --proxy-headers y --forwarded-allow-ips="*": uvicorn confía en las cabeceras
# X-Forwarded-* del proxy de Cloud Run, así request.base_url sale con https (las
# audio_url de V2 no provocan contenido mixto en el front).
# Corre como root porque el volumen de Cloud Storage (/mnt/voice) se monta como
# root; se endurecerá más adelante.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips="*"
