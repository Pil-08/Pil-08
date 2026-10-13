"""Una sola copia del programa gastando a la vez sobre la misma carpeta de datos.

El gasto acumulado vive en `state.json`: cada copia lo lee al empezar y lo reescribe entero con cada respuesta.
Con dos copias abiertas a la vez, la que escribe última borra lo que sumó la otra y el tope de gasto deja de
proteger; además las dos podrían enviar la misma pregunta. Para evitarlo, quien va a gastar toma un bloqueo del
sistema operativo sobre un fichero de la carpeta de datos. Lo suelta el propio sistema cuando el programa
termina —también si lo matan o se va la luz—, así que no puede quedarse «atascado».

Este seguro nunca debe impedir el uso por culpa propia: si el sistema no permite bloquear (carpeta de solo
lectura, un disco de red raro…), se deja pasar.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from . import paths
from .paths import logger

try:  # Linux y macOS
    import fcntl
except ImportError:  # pragma: no cover - depende del sistema
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - depende del sistema
    msvcrt = None  # type: ignore[assignment]

ARCHIVO_DE_BLOQUEO = "instancia.lock"
_RESERVADO = 16  # los primeros bytes son los que se bloquean; el número de proceso se escribe después


class BloqueoDeInstancia:
    """`adquirir()` devuelve False si OTRA copia ya tiene la carpeta de datos en uso."""

    def __init__(self, ruta: Optional[Path] = None):
        self._ruta = ruta
        self._archivo: Any = None
        self.dueno = ""  # número de proceso de la otra copia, si se pudo leer

    @property
    def ruta(self) -> Path:
        return self._ruta or (paths.DATA_DIR / ARCHIVO_DE_BLOQUEO)

    def adquirir(self) -> bool:
        if self._archivo is not None:
            return True
        try:
            archivo = open(self.ruta, "a+b")
        except OSError:
            return True  # no se puede ni crear el fichero: no se impide el uso
        try:
            libre = self._bloquear(archivo)
        except Exception:  # noqa: BLE001 - el seguro no puede ser quien rompa el programa
            logger.exception("No se pudo comprobar si hay otra copia abierta")
            archivo.close()
            return True
        if not libre:
            self.dueno = self._leer_dueno(archivo)
            archivo.close()
            return False
        try:
            archivo.seek(_RESERVADO)
            archivo.truncate()
            archivo.write(str(os.getpid()).encode("ascii"))
            archivo.flush()
        except OSError:
            pass  # solo es información para quien se encuentre el bloqueo ocupado
        self._archivo = archivo
        return True

    def liberar(self) -> None:
        archivo, self._archivo = self._archivo, None
        if archivo is None:
            return
        try:
            self._desbloquear(archivo)
        except Exception:  # noqa: BLE001
            pass
        try:
            archivo.close()
        except OSError:
            pass

    def __enter__(self) -> BloqueoDeInstancia:
        self.adquirir()
        return self

    def __exit__(self, *exc: object) -> None:
        self.liberar()

    # ------------------------------------------------------------------

    @staticmethod
    def _bloquear(archivo: Any) -> bool:
        if fcntl is not None:
            try:
                fcntl.flock(archivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            return True
        if msvcrt is not None:
            archivo.seek(0)
            if os.fstat(archivo.fileno()).st_size < _RESERVADO:
                archivo.write(b"\0" * _RESERVADO)
                archivo.flush()
                archivo.seek(0)
            try:
                msvcrt.locking(archivo.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        return True  # ni uno ni otro: no hay con qué bloquear

    @staticmethod
    def _desbloquear(archivo: Any) -> None:
        if fcntl is not None:
            fcntl.flock(archivo.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            archivo.seek(0)
            msvcrt.locking(archivo.fileno(), msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _leer_dueno(archivo: Any) -> str:
        try:
            archivo.seek(_RESERVADO)
            texto = archivo.read(20).decode("ascii", "ignore").strip()
        except (OSError, ValueError):
            return ""
        return texto if texto.isdigit() else ""


def aviso_de_otra_copia(dueno: str = "") -> str:
    """Lo que se le dice a quien intenta abrir una segunda copia desde la terminal."""
    proceso = f" (proceso {dueno})" if dueno else ""
    return (
        f"Ya hay otra copia de Claude Batch Monitor en marcha{proceso}. Dos copias a la vez se pisarían el gasto "
        "acumulado y el tope de gasto dejaría de proteger. Ciérrala (si no ves su ventana, mira la barra de tareas "
        "o la bandeja del sistema) o usa otra carpeta de datos con la variable CLAUDE_MONITOR_HOME."
    )
