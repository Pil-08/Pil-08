"""Rutas de datos, logging y escritura atómica.

Todo el estado persistente del programa pasa por aquí. `write_atomic` es la
única forma soportada de escribir a disco: escribe a un `.tmp` único y hace
`replace()`, que es atómico en todos los SO soportados. Un corte a mitad de
escritura deja el fichero anterior intacto en vez de uno truncado.

Los demás módulos leen estas rutas (`RESPONSES_DIR`, `STATE_FILE`…) en el momento de usarlas, no al
importar, de modo que los tests pueden redirigirlas todas con un único `monkeypatch`.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "ClaudeMonitor"
APP_VERSION = "12.4.0"
__version__ = APP_VERSION

# Variable de entorno para guardar los datos en otro sitio (modo portátil, Docker,
# tests). Si no existe se usa el directorio estándar del SO.
ENV_HOME = "CLAUDE_MONITOR_HOME"


def _directorio_base() -> Path:
    override = os.getenv(ENV_HOME)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        # `or`, no el segundo argumento de getenv: una XDG_CONFIG_HOME vacía
        # daría Path("") y los datos acabarían en el directorio de trabajo.
        base = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_NAME


def get_data_dir() -> Path:
    """Directorio de datos por SO, creado si no existe.

    Si no se puede escribir (HOME de solo lectura en un contenedor, por ejemplo)
    cae a un directorio temporal en vez de impedir que el programa arranque.
    """
    path = _directorio_base()
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / "responses").mkdir(exist_ok=True)
    except OSError:
        path = Path(tempfile.gettempdir()) / APP_NAME
        path.mkdir(parents=True, exist_ok=True)
        (path / "responses").mkdir(exist_ok=True)
    return path


DATA_DIR = get_data_dir()
RESPONSES_DIR = DATA_DIR / "responses"
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "claude_monitor.log"


def _configurar_logger() -> logging.Logger:
    log = logging.getLogger(APP_NAME)
    log.setLevel(logging.INFO)
    if not log.handlers:
        try:
            handler: logging.Handler = RotatingFileHandler(
                LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
            )
        except OSError:
            handler = logging.NullHandler()
        log.addHandler(handler)
    return log


logger = _configurar_logger()


# Esperas entre intentos de `replace`: en Windows falla un instante si un antivirus o un indexador tiene abierto el destino.
_ESPERAS_AL_REEMPLAZAR = (0.05, 0.1, 0.2, 0.4)


def _reemplazar(origen: Path, destino: Path) -> None:
    for espera in (*_ESPERAS_AL_REEMPLAZAR, None):
        try:
            origen.replace(destino)
            return
        except PermissionError:
            if espera is None:
                raise
            time.sleep(espera)


_SUSTITUTOS_SUELTOS = re.compile("[\ud800-\udfff]")


def _a_utf8(contenido: str) -> bytes:
    """El texto como UTF-8 EXACTO (sin que Windows convierta `\\n` en `\\r\\n`) y sin fallar por un sustituto suelto."""
    try:
        return contenido.encode("utf-8")
    except UnicodeEncodeError:
        return _SUSTITUTOS_SUELTOS.sub("\ufffd", contenido).encode("utf-8")  # el mismo criterio que `limpiar_sustitutos`


def write_atomic(path: Path, content: str) -> None:
    """Escribe `content` en `path` sin dejar nunca un fichero a medias.

    El temporal lleva el PID y el hilo en el nombre: con concurrencia > 1, dos
    hilos guardando el mismo job a la vez usaban el MISMO `fichero.tmp`, uno
    hacía `replace()` del temporal que el otro aún estaba escribiendo y el
    segundo reventaba con `FileNotFoundError`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    marca = f"{os.getpid()}.{threading.get_ident()}"
    tmp = path.with_name(f"{path.name}.{marca}.tmp")
    try:
        tmp.write_bytes(_a_utf8(content))
        _reemplazar(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
