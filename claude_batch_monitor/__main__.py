"""Punto de entrada: abre la interfaz o, con `--cli`, la terminal."""

from __future__ import annotations

import sys
import traceback
from typing import Optional

from . import paths
from .platform_utils import comando_para_ejecutar, nombre_de_python

# Con cualquiera de estas opciones se entiende que se quiere la terminal, aunque
# se olvide `--cli`: abrir una ventana en un servidor sin pantalla no ayuda a nadie.
_OPCIONES_DE_TERMINAL = {
    "--cli", "--mensaje", "--reanudar", "--cancelar-lote", "--listar", "--dry-run", "--version", "--json", "--help", "-h",
}


def _es_terminal(argv: list[str]) -> bool:
    return any(a.split("=", 1)[0] in _OPCIONES_DE_TERMINAL for a in argv)


def _avisar_sin_consola() -> None:
    """Un ejecutable de Windows sin ventana de texto no puede imprimir: se explica con un cuadro de diálogo."""
    mensaje = (
        "Esta versión del programa no tiene consola, así que no puede usarse desde la terminal.\n\n"
        "Para la terminal, abre el programa con Python y ejecuta:\n"
        f"    {nombre_de_python()} claude_batch_monitor.py --cli --help"
    )
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, mensaje, "Claude Batch Monitor", 0x40)  # type: ignore[attr-defined]
    except Exception:
        pass


def _avisar_de_error_fatal(error: BaseException) -> int:
    """Un error que nadie esperaba: se anota en el registro y se explica con una ventana.

    Quien abre el programa con doble clic no ve la consola (se cierra sola al terminar): sin esta
    ventana, el programa «simplemente no se abre» y no hay forma de saber por qué.
    """
    from .gui.i18n import fijar_idioma, t

    paths.logger.error("El programa se cerró por un error inesperado", exc_info=error)
    traceback.print_exception(type(error), error, error.__traceback__)
    fijar_idioma("")
    texto = f"{t('fatal_text')}\n\n{type(error).__name__}: {error}\n\n{t('fatal_log', ruta=paths.LOG_FILE)}"
    try:
        import tkinter as tk
        from tkinter import messagebox

        raiz = tk.Tk()
        raiz.withdraw()
        messagebox.showerror(t("fatal_title"), texto, parent=raiz)
        raiz.destroy()
    except Exception:  # sin pantalla o sin tkinter: ya se imprimió arriba
        pass
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    terminal = _es_terminal(argv)

    if terminal and (sys.stdout is None or sys.stderr is None):
        _avisar_sin_consola()
        return 1

    # Lo único que no viene con Python y hace falta es la librería de Anthropic: si falta, se ofrece
    # instalarla (con un botón, o con una pregunta en la terminal) en vez de morir con un error.
    from .instalacion import falta_anthropic, necesita_la_libreria, resolver_falta_de_la_libreria

    if necesita_la_libreria(argv, terminal) and falta_anthropic():
        return resolver_falta_de_la_libreria(terminal)

    if terminal:
        from .cli import main_cli

        return main_cli([a for a in argv if a != "--cli"])

    try:
        import tkinter  # noqa: F401
    except ImportError:
        # Sin tkinter la ventana no puede abrirse, y en Windows un entry point gráfico
        # moriría sin consola y sin explicación. Se dice qué hacer.
        mensaje = (
            "Este Python no incluye tkinter, que hace falta para la interfaz gráfica.\n"
            "  - Linux (Debian/Ubuntu):  sudo apt install python3-tk\n"
            "  - macOS/Windows: reinstala Python desde python.org marcando 'tcl/tk'.\n"
            f"Mientras tanto puedes usar la terminal:  {comando_para_ejecutar()} --cli --help"
        )
        print(mensaje, file=sys.stderr)
        try:  # si no hay consola (ejecutable sin ventana de texto), al menos que se vea
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, mensaje, "Claude Batch Monitor", 0x10)  # type: ignore[attr-defined]
        except Exception:
            pass
        return 1

    from .gui import abrir_interfaz

    try:
        return abrir_interfaz(argv)
    except Exception as error:  # noqa: BLE001 - último recurso: que quien no ve la consola sepa qué pasó
        return _avisar_de_error_fatal(error)


if __name__ == "__main__":
    sys.exit(main())
