"""Utilidades dependientes del sistema operativo: anti-suspensión y abrir rutas."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .paths import logger

# Windows: ES_CONTINUOUS | ES_SYSTEM_REQUIRED. (La v11 añadía además
# ES_AWAYMODE_REQUIRED, pensado para grabadores multimedia, no para esto.)
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """Evita que el sistema se suspenda mientras corre un lote.

    En Windows `SetThreadExecutionState` es POR HILO: hay que activarlo y
    desactivarlo desde el mismo hilo (el que ejecuta el lote), no desde el de la
    interfaz. Por eso se usa como contexto dentro del hilo trabajador:

        with KeepAwake():
            engine.ejecutar(...)
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._windows = False

    def enable(self) -> None:
        sistema = platform.system()
        try:
            if sistema == "Windows":
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
                )
                self._windows = True
            elif sistema == "Darwin":
                self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            elif shutil.which("systemd-inhibit"):
                self._proc = subprocess.Popen(
                    [
                        "systemd-inhibit",
                        "--what=idle:sleep",
                        "--who=Claude Batch Monitor",
                        "--why=Procesando un lote",
                        "sleep",
                        "infinity",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception as e:
            logger.warning("No se pudo bloquear la suspensión: %s", e)

    def disable(self) -> None:
        try:
            if self._windows:
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)  # type: ignore[attr-defined]
                self._windows = False
            if self._proc is not None:
                self._proc.terminate()
                self._proc = None
        except Exception:
            pass

    def __enter__(self) -> KeepAwake:
        self.enable()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disable()


def abrir_ruta(path: Path) -> bool:
    """Abre un fichero o carpeta con la aplicación por defecto del sistema."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception as e:
        logger.warning("No se pudo abrir %s: %s", path, e)
        return False
