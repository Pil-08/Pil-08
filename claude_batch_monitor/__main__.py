"""Punto de entrada: `python -m claude_batch_monitor` (interfaz) o `--cli` (terminal)."""

from __future__ import annotations

import sys
from typing import Optional

# Con cualquiera de estas opciones se entiende que se quiere la terminal, aunque
# se olvide `--cli`: abrir una ventana en un servidor sin pantalla no ayuda a nadie.
_OPCIONES_DE_TERMINAL = {
    "--cli", "--mensaje", "--reanudar", "--listar", "--dry-run", "--version", "--json", "--help", "-h",
}


def _es_terminal(argv: list[str]) -> bool:
    return any(a.split("=", 1)[0] in _OPCIONES_DE_TERMINAL for a in argv)


def _falta_anthropic() -> int:
    print(
        "Falta la librería 'anthropic'. Instálala con:\n\n    pip install anthropic\n",
        file=sys.stderr,
    )
    return 1


def _avisar_sin_consola() -> None:
    """Un ejecutable de Windows sin ventana de texto no puede imprimir: se explica con un cuadro de diálogo."""
    mensaje = (
        "Esta versión del programa no tiene consola, así que no puede usarse desde la terminal.\n\n"
        "Para la terminal, instálalo con Python (pip install) y ejecuta:\n"
        "    python -m claude_batch_monitor --cli --help"
    )
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, mensaje, "Claude Batch Monitor", 0x40)  # type: ignore[attr-defined]
    except Exception:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if _es_terminal(argv) and (sys.stdout is None or sys.stderr is None):
        _avisar_sin_consola()
        return 1

    try:
        import anthropic  # noqa: F401
    except ImportError:
        return _falta_anthropic()

    if _es_terminal(argv):
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
            "Mientras tanto puedes usar la terminal:  python -m claude_batch_monitor --cli --help"
        )
        print(mensaje, file=sys.stderr)
        try:  # si no hay consola (ejecutable sin ventana de texto), al menos que se vea
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, mensaje, "Claude Batch Monitor", 0x10)  # type: ignore[attr-defined]
        except Exception:
            pass
        return 1

    from .gui import run

    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
