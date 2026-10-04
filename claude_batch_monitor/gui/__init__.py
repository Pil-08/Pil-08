"""Interfaces gráficas (tkinter): modo simple y modo avanzado."""

from __future__ import annotations

import sys
from typing import Optional

from ..config import ConfigManager


def _activar_dpi_alto() -> None:
    """En Windows, sin esto las ventanas salen borrosas en pantallas con escala superior al 100 %."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:
        pass  # solo es estética: nunca debe impedir abrir el programa


def modo_pedido(argv: list[str]) -> Optional[str]:
    """`simple` o `avanzada` si se pidió en la línea de órdenes; None si no."""
    if "--avanzado" in argv or "--avanzada" in argv:
        return "avanzada"
    if "--simple" in argv:
        return "simple"
    return None


def run(argv: Optional[list[str]] = None) -> int:
    """Abre la interfaz y alterna entre los dos modos mientras la persona lo pida.

    Sin `--simple` ni `--avanzado` se abre el modo que se usó la última vez (la primera vez,
    el simple). Cambiar de modo desde la ventana guarda la elección para la próxima.
    """
    import tkinter as tk

    modo = modo_pedido(list(argv or [])) or ConfigManager.cargar().interfaz
    _activar_dpi_alto()
    try:
        while True:
            if modo == "avanzada":
                from .app import ejecutar_avanzada

                cambio = ejecutar_avanzada()
            else:
                from .simple import ejecutar_simple

                cambio = ejecutar_simple()
            if cambio in ("simple", "avanzada") and cambio != modo:
                modo = cambio
                continue
            return 0
    except tk.TclError as error:
        # Típico en un servidor o por SSH: no hay pantalla donde abrir la ventana.
        print(
            f"No se pudo abrir la ventana ({error}).\n"
            "Si estás en un servidor sin escritorio, usa la terminal:  "
            "python -m claude_batch_monitor --cli --help",
            file=sys.stderr,
        )
        return 1
