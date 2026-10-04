"""Icono en la bandeja del sistema (opcional: necesita `pystray` y `Pillow`).

Permite cerrar la ventana y dejar la cola trabajando en segundo plano. Si las
librerías o el entorno gráfico no lo permiten, `disponible()` devuelve False y la
interfaz simplemente no ofrece esa opción.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from ..paths import APP_NAME, logger


def disponible() -> bool:
    try:
        import PIL.Image  # noqa: F401
        import pystray  # noqa: F401

        return True
    except Exception:
        return False


def _imagen():
    from .icono import dibujar

    return dibujar(64)


class BandejaDelSistema:
    """`al_abrir` y `al_salir` se llaman desde el hilo del icono: que cada uno
    se encargue de pasar el trabajo al hilo de la ventana (por ejemplo con el puente)."""

    def __init__(
        self,
        titulo: str,
        texto_abrir: str,
        texto_salir: str,
        al_abrir: Callable[[], None],
        al_salir: Callable[[], None],
    ):
        self.titulo, self.texto_abrir, self.texto_salir = titulo, texto_abrir, texto_salir
        self.al_abrir, self.al_salir = al_abrir, al_salir
        self._icono: Optional[object] = None

    def mostrar(self) -> bool:
        if self._icono is not None:
            return True
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem(self.texto_abrir, lambda *_: self.al_abrir(), default=True),
                pystray.MenuItem(self.texto_salir, lambda *_: self.al_salir()),
            )
            icono = pystray.Icon(APP_NAME, _imagen(), self.titulo, menu)
            self._icono = icono
            threading.Thread(target=self._ejecutar, args=(icono,), daemon=True).start()
            return True
        except Exception:
            logger.warning("No se pudo crear el icono de la bandeja", exc_info=True)
            self._icono = None
            return False

    def _ejecutar(self, icono: object) -> None:
        try:
            icono.run()  # type: ignore[attr-defined]
        except Exception:
            logger.warning("El icono de la bandeja terminó con un error", exc_info=True)

    def ocultar(self) -> None:
        icono, self._icono = self._icono, None
        if icono is not None:
            try:
                icono.stop()  # type: ignore[attr-defined]
            except Exception:
                pass
