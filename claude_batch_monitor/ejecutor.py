"""Ejecuta UN lote en un hilo aparte y cuenta lo que pasa a la ventana.

Es el equivalente de `Cola` para el modo avanzado, pero con un lote cerrado: se
lanza, se sigue su progreso y termina. Tampoco sabe nada de ventanas: cada evento
se entrega mediante `publicar` (en la interfaz, el puente que lo ejecuta en el
hilo de la ventana), y el último siempre es `terminado`, así que la ventana sabe
con certeza cuándo volver a activar sus botones.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any, Callable, Optional

from .config import Configuracion
from .core.base import Callbacks, StopReason
from .core.batch import BatchEngine
from .core.sequential import SequentialEngine
from .job import Job, JobMessage
from .paths import logger
from .platform_utils import KeepAwake
from .pricing import Consumo


class EventosLote:
    """Lo que la ventana recibe. Los métodos por defecto no hacen nada."""

    def estado(self, texto: str, activo: bool) -> None: ...
    def inicio_orden(self, msg: JobMessage) -> None: ...
    def resultado(self, msg: JobMessage) -> None: ...
    def fin_ciclo(self, completadas: int, total: int, ciclo: int) -> None: ...
    def error(self, texto: str) -> None: ...
    def tick(self, total: float, restante: float) -> None: ...
    def gasto(self, usd: Decimal, consumo: Consumo) -> None: ...
    def terminado(self, razon: StopReason, job: Job) -> None: ...


def motor_para(config: Configuracion, job: Job) -> Any:
    """El motor que toca según el modo: inmediato (una petición por orden) o lote (Batches API)."""
    return (BatchEngine if config.es_lote else SequentialEngine)(config, job)


class EjecutorLote:
    def __init__(
        self,
        publicar: Optional[Callable[[Callable[[], None]], None]] = None,
        fabrica: Callable[[Configuracion, Job], Any] = motor_para,
    ):
        self._publicar = publicar or (lambda f: f())
        self._fabrica = fabrica
        self._lock = threading.Lock()
        self._hilo: Optional[threading.Thread] = None
        self._motor: Any = None
        self.notificaciones = True

    @property
    def activo(self) -> bool:
        with self._lock:
            return self._hilo is not None and self._hilo.is_alive()

    @property
    def motor(self) -> Any:
        return self._motor

    def iniciar(self, config: Configuracion, job: Job, eventos: EventosLote) -> bool:
        """Lanza el lote. Devuelve False si ya hay uno en marcha (nunca dos a la vez: se pisarían el gasto)."""
        with self._lock:
            if self._hilo is not None and self._hilo.is_alive():
                return False
            try:
                motor = self._fabrica(config, job)
            except Exception as error:
                logger.exception("No se pudo preparar el lote")
                self._entregar(eventos.error, f"No se pudo preparar el lote: {error}")
                self._entregar(eventos.terminado, StopReason.FATAL, job)
                return True
            motor.notificaciones = self.notificaciones
            self._motor = motor
            self._hilo = threading.Thread(
                target=self._trabajo, args=(motor, job, eventos), daemon=True, name="lote"
            )
            self._hilo.start()
        return True

    def detener(self) -> None:
        """Pide parar: la petición en vuelo termina y no se envía ninguna más."""
        motor = self._motor
        if motor is not None:
            motor.detener()

    def cancelar_lote(self, al_terminar: Optional[Callable[[bool], None]] = None) -> bool:
        """Para el seguimiento y cancela en el servidor el lote en curso (en segundo plano, sin bloquear a quien llama).

        `al_terminar(ok)` se avisa por `publicar` cuando el servidor ha contestado. False si no hay un lote que cancelar.
        Primero se para el motor (siempre el mismo desenlace: el lote queda `canceling` y «Reanudar» recoge lo que quede);
        el motor cierra su cliente al terminar, y `cancelar_lote` del motor lo sabe y usa uno propio si hace falta.
        """
        motor = self._motor
        if motor is None or not hasattr(motor, "cancelar_lote") or not getattr(motor.job, "batch_id", ""):
            return False
        motor.detener()

        def trabajo() -> None:
            ok = False
            try:
                ok = bool(motor.cancelar_lote())
            except Exception:
                logger.exception("Fallo al cancelar el lote")
            if al_terminar is not None:
                self._entregar(al_terminar, ok)

        threading.Thread(target=trabajo, daemon=True, name="cancelar-lote").start()
        return True

    def esperar(self, timeout: Optional[float] = None) -> bool:
        """Espera a que termine el hilo (para el cierre ordenado y las pruebas)."""
        hilo = self._hilo
        if hilo is not None:
            hilo.join(timeout)
        return not self.activo

    # ------------------------------------------------------------------

    def _trabajo(self, motor: Any, job: Job, eventos: EventosLote) -> None:
        cb = self._callbacks(eventos)
        razon = StopReason.FATAL
        try:
            # Windows liga `SetThreadExecutionState` al hilo: se activa y se suelta aquí, no en la ventana.
            with KeepAwake():
                razon = motor.ejecutar(cb)
        except Exception as error:
            logger.exception("Fallo inesperado en el lote")
            self._entregar(eventos.error, f"Error inesperado: {error}")
        finally:
            self._motor = None
            self._entregar(eventos.terminado, razon, job)

    def _callbacks(self, eventos: EventosLote) -> Callbacks:
        return Callbacks(
            estado=lambda texto, activo: self._entregar(eventos.estado, texto, activo),
            inicio_orden=lambda msg: self._entregar(eventos.inicio_orden, msg),
            resultado=lambda msg: self._entregar(eventos.resultado, msg),
            fin_ciclo=lambda c, t, n: self._entregar(eventos.fin_ciclo, c, t, n),
            error=lambda texto: self._entregar(eventos.error, texto),
            tick=lambda total, restante: self._entregar(eventos.tick, total, restante),
            gasto=lambda usd, consumo: self._entregar(eventos.gasto, usd, consumo),
        )

    def _entregar(self, metodo: Callable[..., None], *args: Any) -> None:
        def entregar() -> None:
            try:
                metodo(*args)
            except Exception:
                logger.exception("Fallo en un evento del lote")

        self._publicar(entregar)
