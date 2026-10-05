"""Utilidades dependientes del sistema operativo: anti-suspensión, abrir rutas, el tema y cómo se llama Python."""

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


def nombre_de_python() -> str:
    """Cómo se llama Python en una terminal de este sistema: `py` en Windows, `python3` en los demás."""
    return "py" if sys.platform == "win32" else "python3"


def comando_para_instalar(libreria: str) -> str:
    """El comando que se le enseña a la persona para instalar una librería a mano."""
    return f"{nombre_de_python()} -m pip install {libreria}"


def comando_para_ejecutar() -> str:
    """Lo que se escribe en una terminal para abrir este programa, tal como se abrió ahora.

    Sirve para los mensajes de ayuda: el archivo único (`python3 claude_batch_monitor.py`), el paquete
    (`python3 -m claude_batch_monitor`) o el ejecutable descargado.
    """
    if getattr(sys, "frozen", False):
        nombre = Path(sys.executable).name
        return nombre if sys.platform == "win32" else f"./{nombre}"
    if __package__:  # `python -m claude_batch_monitor`, o instalado con pip
        return f"{nombre_de_python()} -m {__package__.split('.')[0]}"
    return f"{nombre_de_python()} {Path(__file__).name}"  # el archivo único


def tema_del_sistema() -> str:
    """`oscuro` si el sistema usa el tema oscuro; `claro` en cualquier otro caso, también si no se puede saber."""
    try:
        if sys.platform == "win32":
            import winreg

            clave = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, clave) as abierta:
                return "claro" if winreg.QueryValueEx(abierta, "AppsUseLightTheme")[0] else "oscuro"
        if sys.platform == "darwin":
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                               capture_output=True, text=True, timeout=3)
            return "oscuro" if r.stdout.strip().lower() == "dark" else "claro"
        if shutil.which("gsettings"):  # GNOME y los que lo imitan (Ubuntu, Fedora, Cinnamon…)
            r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                               capture_output=True, text=True, timeout=3)
            if "dark" in r.stdout.lower():
                return "oscuro"
        if "dark" in os.environ.get("GTK_THEME", "").lower():
            return "oscuro"
    except Exception as e:  # solo es estética: ante la duda, claro
        logger.info("No se pudo saber el tema del sistema: %s", e)
    return "claro"
