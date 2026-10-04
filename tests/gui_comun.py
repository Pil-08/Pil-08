"""Lo que comparten las pruebas de las ventanas: cómo esperar a los eventos y cómo cerrar lo que abren.

La fixture `raiz` (la ventana raíz de tkinter, real o virtual) está en `conftest.py`.
Necesitan tkinter y una pantalla: en Linux sin escritorio, `xvfb-run -a pytest`.
"""

import time

_creadas: list = []


def registrar(app):
    """Anota una ventana para cerrarla (y parar sus hilos) al acabar la prueba."""
    _creadas.append(app)
    return app


def cerrar_todo() -> None:
    while _creadas:
        app = _creadas.pop()
        try:
            app._salir()
        except Exception:  # noqa: BLE001 - limpieza: lo que importa es seguir cerrando
            pass


def bombear(raiz, condicion=None, timeout=6.0):
    """Procesa eventos de la ventana hasta que se cumpla la condición (o venza el tiempo)."""
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        raiz.update()
        if condicion is not None and condicion():
            return True
        time.sleep(0.01)
    return condicion is None
