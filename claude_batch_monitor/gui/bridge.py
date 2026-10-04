"""Puente entre los hilos de trabajo y el hilo de la ventana.

tkinter solo es seguro desde el hilo que creó la ventana. Llamar a `root.after()`
desde un hilo trabajador "suele funcionar" y a veces revienta con "main thread is
not in main loop". Aquí los hilos solo meten funciones en una cola (que sí es
segura entre hilos) y es el hilo de la ventana quien las ejecuta, por turnos.
"""

from __future__ import annotations

import queue
from typing import Any, Callable, Optional

from ..paths import logger

MAX_POR_TURNO = 200  # para que una ráfaga de eventos no congele la ventana


class UiBridge:
    def __init__(self, root: Any, intervalo_ms: int = 30):
        self.root = root
        self.intervalo_ms = intervalo_ms
        self._cola: queue.SimpleQueue = queue.SimpleQueue()
        self._vivo = True
        self._after_id: Optional[str] = None
        self._turno()

    def post(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Pide ejecutar `fn(*args)` en el hilo de la ventana. Se puede llamar desde cualquier hilo."""
        if self._vivo:
            self._cola.put((fn, args, kwargs))

    def publicar(self, fn: Callable[[], None]) -> None:
        """Igual que `post`, con la firma que espera `Cola(publicar=...)`."""
        self.post(fn)

    def _turno(self) -> None:
        if not self._vivo:
            return
        for _ in range(MAX_POR_TURNO):
            try:
                fn, args, kwargs = self._cola.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args, **kwargs)
            except Exception:
                # Un evento que falla (p. ej. un widget ya destruido) no debe parar a los demás.
                logger.exception("Fallo al ejecutar un evento de la interfaz")
        try:
            self._after_id = self.root.after(self.intervalo_ms, self._turno)
        except Exception:  # la ventana ya no existe
            self._vivo = False

    def drenar(self) -> None:
        """Ejecuta ya lo pendiente (para tests y para el cierre ordenado)."""
        self._turno_sin_reprogramar()

    def _turno_sin_reprogramar(self) -> None:
        while True:
            try:
                fn, args, kwargs = self._cola.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception("Fallo al ejecutar un evento de la interfaz")

    def cerrar(self) -> None:
        self._vivo = False
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
