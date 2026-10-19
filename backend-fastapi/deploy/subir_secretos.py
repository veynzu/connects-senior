"""Sube los secretos del backend de Connects Senior a Secret Manager (Google Cloud).

Uso (desde backend-fastapi/):

    .venv/Scripts/python deploy/subir_secretos.py [--project ID]

Sin --project se usa el proyecto configurado en gcloud (gcloud config set project ID).

Qué sube (secreto <- origen):

    cs-database-url              <- DATABASE_URL del archivo .env
    cs-firebase-web-api-key      <- FIREBASE_WEB_API_KEY del archivo .env
    cs-daily-api-key             <- DAILY_API_KEY del archivo .env
    cs-daily-domain              <- DAILY_DOMAIN del archivo .env
    cs-voice-signing-key         <- VOICE_MESSAGE_SIGNING_KEY del archivo .env; si no
                                    está, la primera vez se genera una clave nueva
                                    (no se guarda en .env) y después no se toca
    cs-firebase-service-account  <- el archivo JSON que indica FIREBASE_CREDENTIALS_PATH

Antes de subir nada activa la API de Secret Manager (gcloud services enable
secretmanager.googleapis.com), así que sirve en un proyecto nuevo y es el primer paso
del despliegue (después, bash deploy/desplegar.sh <PROJECT_ID>).

Si el secreto no existe se crea; si ya existe se le añade una versión nueva, así que
se puede ejecutar varias veces. Los valores llegan a gcloud solo por la entrada
estándar: nunca se muestran, no se escriben en ningún archivo ni van en la línea de
comandos. La salida solo muestra nombres de secretos y su estado.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BACKEND_DIR / ".env"

# Secretos que salen tal cual de una variable del .env: (secreto, variable).
ENV_SECRETS: tuple[tuple[str, str], ...] = (
    ("cs-database-url", "DATABASE_URL"),
    ("cs-firebase-web-api-key", "FIREBASE_WEB_API_KEY"),
    ("cs-daily-api-key", "DAILY_API_KEY"),
    ("cs-daily-domain", "DAILY_DOMAIN"),
)
VOICE_SECRET = "cs-voice-signing-key"
VOICE_VARIABLE = "VOICE_MESSAGE_SIGNING_KEY"
SERVICE_ACCOUNT_SECRET = "cs-firebase-service-account"
CREDENTIALS_VARIABLE = "FIREBASE_CREDENTIALS_PATH"
SECRET_MANAGER_API = "secretmanager.googleapis.com"
ALL_SECRETS: tuple[str, ...] = (
    *(secret for secret, _ in ENV_SECRETS),
    VOICE_SECRET,
    SERVICE_ACCOUNT_SECRET,
)

PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
STDERR_LINES = 5
CREDENTIALS_NOT_FOUND = "no se encontró el archivo de credenciales de Firebase"

# Rutas habituales del instalador de Windows, por si la terminal se abrió antes de
# instalar gcloud y su PATH todavía no lo incluye.
WINDOWS_SDK_GCLOUD = Path("Google", "Cloud SDK", "google-cloud-sdk", "bin", "gcloud.cmd")
WINDOWS_SDK_BASES = ("LOCALAPPDATA", "ProgramFiles(x86)", "ProgramFiles")


class ConfigurationError(Exception):
    """Falta algo en .env. El mensaje nunca lleva valores ni rutas."""


class GcloudError(Exception):
    """gcloud terminó con error. Solo guarda el código y su stderr (nunca el dato)."""

    def __init__(self, returncode: int, stderr: bytes) -> None:
        super().__init__(returncode)
        self.returncode = returncode
        self.stderr = stderr


class Gcloud:
    """Ejecuta gcloud sin shell; los valores solo viajan por la entrada estándar."""

    def __init__(self, executable: str, project: str | None) -> None:
        self._executable = executable
        self._project = project

    def _command(self, *args: str) -> list[str]:
        command = [self._executable, *args, "--quiet"]
        if self._project:
            command += ["--project", self._project]
        return command

    def enable_secret_manager(self) -> None:
        result = subprocess.run(
            self._command("services", "enable", SECRET_MANAGER_API),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise GcloudError(result.returncode, result.stderr)

    def secret_exists(self, name: str) -> bool:
        result = subprocess.run(
            self._command("secrets", "describe", name),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def upload(self, name: str, value: bytes, *, exists: bool) -> str:
        if exists:
            args = ("secrets", "versions", "add", name, "--data-file=-")
            status = "nueva versión"
        else:
            args = (
                "secrets",
                "create",
                name,
                "--replication-policy=automatic",
                "--data-file=-",
            )
            status = "creado"
        result = subprocess.run(
            self._command(*args),
            input=value,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise GcloudError(result.returncode, result.stderr)
        return status


def find_gcloud() -> str | None:
    found = shutil.which("gcloud")  # en Windows resuelve gcloud.cmd
    if found:
        return found
    if os.name == "nt":
        for variable in WINDOWS_SDK_BASES:
            base = os.environ.get(variable)
            if base:
                candidate = Path(base) / WINDOWS_SDK_GCLOUD
                if candidate.is_file():
                    return str(candidate)
    return None


def read_env() -> dict[str, str | None]:
    if not ENV_FILE.is_file():
        raise ConfigurationError("no se encontró el archivo .env de backend-fastapi")
    return dotenv_values(ENV_FILE, encoding="utf-8")


def required_value(env: dict[str, str | None], variable: str) -> bytes:
    value = env.get(variable)
    if value is None or not value.strip():
        raise ConfigurationError(f"falta la variable {variable} en .env")
    return value.encode("utf-8")


def optional_value(env: dict[str, str | None], variable: str) -> bytes | None:
    value = env.get(variable)
    if value is None or not value.strip():
        return None
    return value.encode("utf-8")


def credentials_file_bytes(env: dict[str, str | None]) -> bytes:
    configured = (env.get(CREDENTIALS_VARIABLE) or "").strip()
    if not configured:
        raise ConfigurationError(CREDENTIALS_NOT_FOUND)
    path = Path(configured)
    if not path.is_absolute():
        path = (BACKEND_DIR / path).resolve()
    try:
        data = path.read_bytes()
    except OSError:
        raise ConfigurationError(CREDENTIALS_NOT_FOUND) from None
    if not data.strip():
        raise ConfigurationError("el archivo de credenciales de Firebase está vacío")
    return data


def stderr_head(stderr: bytes) -> list[str]:
    text = stderr.decode("utf-8", errors="replace")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[:STDERR_LINES]


def use_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # line_buffering: errores y estados salen en orden aunque se redirijan.
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sube los secretos del backend a Secret Manager sin mostrarlos.",
    )
    parser.add_argument(
        "--project",
        metavar="ID",
        help="ID del proyecto de Google Cloud (por defecto, el configurado en gcloud)",
    )
    return parser.parse_args(argv)


def print_summary(results: dict[str, str]) -> None:
    print()
    print("Resumen:")
    for name in ALL_SECRETS:
        print(f"  {name}: {results[name]}")


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.project is not None and not PROJECT_ID_PATTERN.match(args.project):
        print(
            "El ID de proyecto no es válido: 6 a 30 letras minúsculas, números o guiones.",
            file=sys.stderr,
        )
        return 1

    executable = find_gcloud()
    if executable is None:
        print(
            "No se encontró gcloud. Instala Google Cloud CLI, inicia sesión con "
            "'gcloud auth login' y abre una terminal nueva.",
            file=sys.stderr,
        )
        return 1

    # Todo se lee y se valida antes de llamar a gcloud: si falta algo, no se sube nada.
    try:
        env = read_env()
        uploads: list[tuple[str, bytes | None]] = [
            (secret, required_value(env, variable)) for secret, variable in ENV_SECRETS
        ]
        uploads.append((VOICE_SECRET, optional_value(env, VOICE_VARIABLE)))
        uploads.append((SERVICE_ACCOUNT_SECRET, credentials_file_bytes(env)))
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 1

    gcloud = Gcloud(executable, args.project)

    # En un proyecto nuevo la API está apagada y "secrets describe" fallaría siempre.
    try:
        gcloud.enable_secret_manager()
    except GcloudError as error:
        print(
            f"no se pudo activar la API de Secret Manager (código {error.returncode})",
            file=sys.stderr,
        )
        for line in stderr_head(error.stderr):
            print(f"  {line}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "no se pudo ejecutar gcloud para activar la API de Secret Manager",
            file=sys.stderr,
        )
        return 1
    print("API de Secret Manager activada")

    results = dict.fromkeys(ALL_SECRETS, "no procesado")
    failed = False
    for name, value in uploads:
        try:
            exists = gcloud.secret_exists(name)
            if value is not None:
                status = gcloud.upload(name, value, exists=exists)
            elif exists:
                # Solo cs-voice-signing-key sin la variable en .env: se respeta la que hay.
                status = "sin cambios"
            else:
                generated = secrets.token_hex(32).encode("ascii")
                gcloud.upload(name, generated, exists=False)
                status = "creado (clave generada, no se guarda en .env)"
        except GcloudError as error:
            results[name] = f"falló (código {error.returncode})"
            print(f"gcloud falló en {name} (código {error.returncode})", file=sys.stderr)
            for line in stderr_head(error.stderr):
                print(f"  {line}", file=sys.stderr)
            failed = True
            break
        except OSError:
            results[name] = "falló"
            print(f"no se pudo ejecutar gcloud en {name}", file=sys.stderr)
            failed = True
            break
        results[name] = status
        print(f"{name}: {status}")

    print_summary(results)
    return 1 if failed else 0


def main() -> int:
    use_utf8_output()
    try:
        return run()
    except KeyboardInterrupt:
        print("Cancelado.", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - sin detalles: podrían llevar datos
        print(
            f"Error inesperado ({type(error).__name__}); no se muestran detalles "
            "para no exponer datos.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
