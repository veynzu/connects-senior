#!/usr/bin/env bash
# Despliega el backend de Connects Senior en Cloud Run (Google Cloud).
#
# Uso (Git Bash; funciona desde cualquier carpeta):
#   deploy/desplegar.sh <PROJECT_ID> [REGION]
#   MODO_AHORRO=1 deploy/desplegar.sh <PROJECT_ID> [REGION]
#
# REGION por defecto: southamerica-east1 (la misma zona que la base de Neon).
# Orden (los dos comandos desde backend-fastapi/; vale también en un proyecto nuevo):
#   1) .venv/Scripts/python deploy/subir_secretos.py --project <PROJECT_ID>
#      (activa la API de Secret Manager y sube los seis secretos)
#   2) bash deploy/desplegar.sh <PROJECT_ID>
# Todos los pasos se pueden repetir sin romper nada.
# Este script no lee .env ni muestra valores de secretos: solo sus nombres.
# MODO_AHORRO=1: cero instancias en reposo y CPU solo durante las peticiones (casi
# gratis, pero recordatorios y reenvíos de alertas solo corren mientras alguien usa
# la app y la primera petición tras un rato sin uso tarda unos segundos).

set -euo pipefail

# Git Bash convierte los argumentos que empiezan por / (/secrets/..., /mnt/...) en
# rutas de Windows al llamar a programas de Windows; esto lo desactiva.
export MSYS_NO_PATHCONV=1
# gcloud no hace preguntas: acepta los valores por defecto (por ejemplo, crear el
# repositorio de Artifact Registry que necesita --source).
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

if [ "$#" -lt 1 ] || [ -z "${1:-}" ]; then
  echo "Uso: deploy/desplegar.sh <PROJECT_ID> [REGION]" >&2
  echo "     REGION por defecto: southamerica-east1" >&2
  echo "     Modo ahorro: MODO_AHORRO=1 deploy/desplegar.sh <PROJECT_ID> [REGION]" >&2
  exit 1
fi

PROJECT_ID="$1"
REGION="${2:-southamerica-east1}"

if ! [[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
  echo "El ID de proyecto no es válido: 6 a 30 letras minúsculas, números o guiones." >&2
  exit 1
fi

# backend-fastapi/: es la carpeta que sube --source .
cd "$(dirname "$0")/.."

# --- Cómo se llama a gcloud ---
# En Git Bash (Windows) se ejecuta lib/gcloud.py con el Python de la instalación
# ("Bundled Python") y la ruta de gcloud.py en formato Windows, en vez de:
#  - gcloud.cmd: pasa por cmd.exe, que interpreta ^ y | y rompe el argumento
#    "^|^..." de --set-env-vars (cmd intenta ejecutar el texto que sigue a "|");
#  - el script "gcloud" (sh): le pasa a Python la ruta de gcloud.py como /c/...,
#    que con MSYS_NO_PATHCONV=1 ya no se convierte, y además puede tomar el
#    python3 de Microsoft Store en vez de un Python real.
# En Linux, macOS o Cloud Shell se usa gcloud tal cual.
sdk_root_windows() {
  local exe dir
  for exe in gcloud gcloud.cmd; do
    if command -v "$exe" > /dev/null 2>&1; then
      dirname "$(dirname "$(command -v "$exe")")"
      return 0
    fi
  done
  # Rutas habituales del instalador, por si el PATH de esta terminal es anterior
  # a la instalación.
  for dir in \
    "${LOCALAPPDATA:+$(cygpath -u "$LOCALAPPDATA")/Google/Cloud SDK/google-cloud-sdk}" \
    "/c/Program Files (x86)/Google/Cloud SDK/google-cloud-sdk" \
    "/c/Program Files/Google/Cloud SDK/google-cloud-sdk"; do
    if [ -n "$dir" ] && [ -f "$dir/lib/gcloud.py" ]; then
      echo "$dir"
      return 0
    fi
  done
  return 1
}

case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*)
    if ! SDK_ROOT="$(sdk_root_windows)" || [ ! -f "$SDK_ROOT/lib/gcloud.py" ]; then
      echo "No se encontró Google Cloud CLI. Instálalo y abre una terminal nueva." >&2
      exit 1
    fi
    if [ -n "${CLOUDSDK_PYTHON:-}" ]; then
      GCLOUD_PYTHON="$(cygpath -u "$CLOUDSDK_PYTHON")"
    else
      GCLOUD_PYTHON="$SDK_ROOT/platform/bundledpython/python.exe"
    fi
    if ! command -v "$GCLOUD_PYTHON" > /dev/null 2>&1; then
      echo "No se encontró el Python de Google Cloud CLI: reinstálalo con la opción" >&2
      echo "'Bundled Python' o define CLOUDSDK_PYTHON con la ruta de un python.exe." >&2
      exit 1
    fi
    GCLOUD_PY="$(cygpath -w "$SDK_ROOT/lib/gcloud.py")"
    gcloud() { "$GCLOUD_PYTHON" "$GCLOUD_PY" "$@"; }
    ;;
  *)
    if ! command -v gcloud > /dev/null 2>&1; then
      echo "No se encontró gcloud. Instala Google Cloud CLI." >&2
      exit 1
    fi
    ;;
esac

# Repite un comando hasta 5 veces con 10 s de espera (la salida normal se descarta):
# una cuenta de servicio recién creada tarda unos segundos en ser visible para IAM.
reintentar() {
  local intento
  for intento in 1 2 3 4 5; do
    if "$@" > /dev/null; then
      return 0
    fi
    if [ "$intento" -lt 5 ]; then
      echo "   no se pudo; reintento $((intento + 1)) de 5 en 10 s..." >&2
      sleep 10
    fi
  done
  return 1
}

SERVICE=connects-senior-api
SA_NAME=connects-senior-api
SA="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
BUCKET="$PROJECT_ID-voice-messages"
CORS_ORIGINS="https://connectsenior.site,https://www.connectsenior.site,https://$PROJECT_ID.web.app,https://$PROJECT_ID.firebaseapp.com"
SECRETS=(cs-database-url cs-firebase-web-api-key cs-daily-api-key cs-daily-domain cs-voice-signing-key cs-firebase-service-account)

echo "== paso 1: proyecto $PROJECT_ID, región $REGION =="
gcloud config set project "$PROJECT_ID"

echo "== paso 2: activar las APIs (Cloud Run, Cloud Build, Artifact Registry, Secret Manager, Cloud Storage) =="
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com storage.googleapis.com

echo "== paso 3: comprobar que existen los seis secretos =="
faltan=()
for s in "${SECRETS[@]}"; do
  if ! gcloud secrets describe "$s" > /dev/null 2>&1; then
    faltan+=("$s")
  fi
done
if [ "${#faltan[@]}" -gt 0 ]; then
  echo "Faltan estos secretos en Secret Manager: ${faltan[*]}" >&2
  echo "Súbelos primero (desde backend-fastapi/) con:" >&2
  echo "  .venv/Scripts/python deploy/subir_secretos.py --project $PROJECT_ID" >&2
  exit 1
fi

echo "== paso 4: cuenta de servicio $SA =="
gcloud iam service-accounts describe "$SA" > /dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --display-name "Connects Senior API"

echo "== paso 5: permiso de lectura de los secretos para la cuenta de servicio =="
for s in "${SECRETS[@]}"; do
  reintentar gcloud secrets add-iam-policy-binding "$s" \
    --member "serviceAccount:$SA" \
    --role roles/secretmanager.secretAccessor \
    --quiet
done

echo "== paso 6: bucket privado de audios gs://$BUCKET =="
gcloud storage buckets describe "gs://$BUCKET" > /dev/null 2>&1 \
  || gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access --public-access-prevention
reintentar gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$SA" \
  --role roles/storage.objectAdmin

echo "== paso 7: construir y publicar $SERVICE en Cloud Run (tarda varios minutos) =="
if [ "${MODO_AHORRO:-0}" = "1" ]; then
  echo "   modo ahorro: 0 instancias en reposo y CPU solo durante las peticiones"
  ESCALADO=(--min-instances 0 --cpu-throttling)
else
  # Una sola instancia siempre encendida y con CPU fuera de las peticiones: el
  # planificador (recordatorios y reenvíos) corre dentro del proceso y debe haber
  # un único proceso contra la base (D-R3-24).
  ESCALADO=(--min-instances 1 --no-cpu-throttling)
fi
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --service-account "$SA" \
  --execution-environment gen2 \
  --cpu 1 \
  --memory 512Mi \
  "${ESCALADO[@]}" \
  --max-instances 1 \
  --concurrency 80 \
  --timeout 120 \
  --set-env-vars "^|^ENVIRONMENT=production|FIREBASE_PROJECT_ID=$PROJECT_ID|FIREBASE_CREDENTIALS_PATH=/secrets/firebase/firebase.json|VOICE_MESSAGES_DIR=/mnt/voice|CORS_ORIGINS=$CORS_ORIGINS" \
  --set-secrets "DATABASE_URL=cs-database-url:latest,FIREBASE_WEB_API_KEY=cs-firebase-web-api-key:latest,DAILY_API_KEY=cs-daily-api-key:latest,DAILY_DOMAIN=cs-daily-domain:latest,VOICE_MESSAGE_SIGNING_KEY=cs-voice-signing-key:latest,/secrets/firebase/firebase.json=cs-firebase-service-account:latest" \
  --add-volume name=voice,type=cloud-storage,bucket="$BUCKET" \
  --add-volume-mount volume=voice,mount-path=/mnt/voice

echo "== paso 8: dirección del backend =="
URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')
echo "Backend publicado en $URL (Swagger: $URL/docs)"
