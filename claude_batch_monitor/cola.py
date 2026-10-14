"""La cola de preguntas del modo simple.

Una persona escribe preguntas; la cola las guarda en disco, las envía una a una
en un hilo aparte y, si la API pide esperar (límite por minuto, servidor saturado,
sin conexión), espera sola y continúa. Se pueden añadir preguntas mientras se
envía y, si se cierra el programa, lo pendiente sigue ahí al volver a abrirlo.

Esta clase NO sabe nada de ventanas: informa de lo que ocurre a través de
`EventosCola`, y cada evento se entrega mediante `publicar` (en la interfaz,
el puente que lo ejecuta en el hilo de la ventana). Eso la hace probable sin
pantalla.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Optional

from .config import Configuracion
from .core.base import Callbacks, StopReason
from .ejecutor import motor_para
from .job import STATUS_FAILED, STATUS_PENDING, STATUS_SKIPPED, Job, JobMessage, listar_jobs
from .paths import logger
from .platform_utils import KeepAwake

ETIQUETA = "cola"


@dataclass(frozen=True)
class EstadoCola:
    """Qué está haciendo la cola ahora mismo (lo que se le cuenta a la persona)."""

    tipo: str = "idle"  # idle | sending | waiting | paused | stopped
    motivo: str = ""    # waiting: rate_limit | server | sin_conexion | cuota | plan | franja
    razon: Optional[StopReason] = None  # stopped: por qué
    msg_id: Optional[int] = None
    segundos: float = 0.0
    texto: str = ""     # detalle (mensaje de error de la API, ya redactado)
    codigo: str = ""    # stopped, con el plan de Claude: el problema en una palabra (no_instalado, sin_sesion, uso_extra…)


class EventosCola:
    """Lo que la interfaz recibe. Los métodos por defecto no hacen nada."""

    def orden_cambiada(self, msg: JobMessage) -> None: ...
    def estado(self, estado: EstadoCola) -> None: ...
    def cuenta_atras(self, restante: float) -> None: ...
    def delta(self, msg_id: int, texto: str) -> None: ...
    def gasto(self, usd: Decimal) -> None: ...
    def plan(self, estado: Any) -> None: ...  # plan de Claude: lo último que se sabe de su cupo
    def error(self, texto: str) -> None: ...


FabricaMotor = Callable[[Configuracion, Job], Any]


class Cola:
    def __init__(
        self,
        config: Configuracion,
        eventos: Optional[EventosCola] = None,
        publicar: Optional[Callable[[Callable[[], None]], None]] = None,
        fabrica: FabricaMotor = motor_para,
        job: Optional[Job] = None,
    ):
        self.config = config
        self.eventos = eventos or EventosCola()
        self._publicar = publicar or (lambda f: f())
        self._fabrica = fabrica
        self.job = job
        self.notificaciones = True

        self._lock = threading.RLock()
        self._hilo: Optional[threading.Thread] = None
        self._motor: Any = None
        self._pausada = False
        self._enviando: Optional[int] = None
        self.estado = EstadoCola()
        self.ultimo_error = ""

    # ------------------------------------------------------------------
    # Estado
    # ------------------------------------------------------------------

    @property
    def activa(self) -> bool:
        with self._lock:
            return self._hilo is not None and self._hilo.is_alive()

    @property
    def pausada(self) -> bool:
        return self._pausada

    def preguntas(self) -> list[JobMessage]:
        return list(self.job.messages) if self.job else []

    def pendientes(self) -> int:
        return len(self.job.pendientes()) if self.job else 0

    def gastado(self) -> Decimal:
        return self.job.coste_total() if self.job else Decimal(0)

    def cargar_ultima(self) -> int:
        """Recupera la cola de la sesión anterior. Devuelve cuántas preguntas quedan pendientes."""
        lotes = listar_jobs(etiqueta=ETIQUETA)
        if lotes:
            self.job = Job.load(lotes[0]["job_id"])
        return self.pendientes()

    def _config_de_envio(self) -> Configuracion:
        """La configuración con la que se envía, con los ajustes que fija el modo simple."""
        return replace(
            self.config,
            modo="plan" if self.config.es_plan else "inmediato",  # el modo lote es de la ventana avanzada
            concurrencia=1,
            streaming=True,
            pausa_entre_mensajes_sec=0,
            estimar_antes_de_enviar=True,
            reset_gasto_por_lote=True,  # el tope es por cola: parte de lo que esta cola ya gastó
            modo_continuo=False,
            hora_limite="",
        )

    # ------------------------------------------------------------------
    # Acciones de la persona
    # ------------------------------------------------------------------

    def agregar(self, texto: str) -> Optional[JobMessage]:
        """Añade una pregunta a la cola y, si no está pausada, empieza a enviarla."""
        texto = (texto or "").strip()
        if not texto:
            return None
        with self._lock:
            if self.job is None:
                self.job = Job.crear([], self._config_de_envio(), etiqueta=ETIQUETA)
                self.job.save()
            msg = self.job.agregar_orden(texto)
        self._emitir(self.eventos.orden_cambiada, msg)
        self._despertar()
        return msg

    def pausar(self) -> None:
        with self._lock:
            self._pausada = True
            if self._motor is not None:
                self._motor.detener()  # termina la pregunta en curso y no envía más
        self._set_estado(EstadoCola("paused"))

    def reanudar(self) -> None:
        """Quita la pausa y vuelve a intentar lo pendiente (y lo que falló: tras arreglar la causa)."""
        with self._lock:
            self._pausada = False
            self.ultimo_error = ""
            if self.job is not None and self.job.reintentar_fallidas():
                for m in self.job.messages:
                    self._emitir(self.eventos.orden_cambiada, m)
        self._despertar()

    def quitar(self, msg_id: int) -> bool:
        """Saca una pregunta de la cola si aún no se ha enviado."""
        with self._lock:
            msg = self.job.mensaje(msg_id) if self.job else None
            if msg is None or msg.status != STATUS_PENDING or self._enviando == msg_id:
                return False
            msg.status = STATUS_SKIPPED
            msg.detalle = "Quitada de la cola."
            self.job.save()
        self._emitir(self.eventos.orden_cambiada, msg)
        return True

    def nueva(self) -> bool:
        """Empieza una cola en blanco (la anterior queda guardada en disco). Solo si está en reposo."""
        with self._lock:
            if self.activa:
                return False
            self.job = None
            self._pausada = False
            self.ultimo_error = ""
        self._set_estado(EstadoCola("idle"))
        return True

    def actualizar_config(self, config: Configuracion) -> None:
        """Los cambios (tope, modelo…) valen para el siguiente envío."""
        with self._lock:
            self.config = config

    def cerrar(self, timeout: float = 5.0) -> None:
        """Para el hilo. Lo pendiente queda guardado y seguirá la próxima vez."""
        with self._lock:
            self._pausada = True
            motor, hilo = self._motor, self._hilo
            if motor is not None:
                motor.detener()
        if hilo is not None and hilo.is_alive():
            hilo.join(timeout)

    # ------------------------------------------------------------------
    # El hilo
    # ------------------------------------------------------------------

    def _despertar(self) -> None:
        with self._lock:
            if self._pausada or self.job is None or not self.job.pendientes():
                return
            if self._hilo is not None and self._hilo.is_alive():
                return  # el bucle recogerá lo nuevo cuando termine con lo actual
            self._hilo = threading.Thread(target=self._bucle, name="cola-de-preguntas", daemon=True)
            self._hilo.start()

    def _bucle(self) -> None:
        while True:
            with self._lock:
                if self._pausada or self.job is None or not self.job.pendientes():
                    # Decidir salir y soltar el hilo en el MISMO cerrojo: así una pregunta
                    # añadida justo ahora no se queda esperando a nadie.
                    self._hilo = None
                    self._enviando = None
                    self._set_estado(EstadoCola("paused" if self._pausada and self.pendientes() else "idle"))
                    return
                try:
                    envio = self._config_de_envio()
                    if (self.job.modo, self.job.modelo) != (envio.modo, envio.nombre_de_modelo):
                        # Cambió cómo se usa Claude (plan/clave) o el modelo: la cola lo cuenta tal como sigue ahora
                        # (si no, exportarla o reanudarla desde la terminal diría —y haría— lo que ya no es).
                        self.job.modo, self.job.modelo = envio.modo, envio.nombre_de_modelo
                        self.job.save()
                    motor = self._fabrica(envio, self.job)
                    motor.notificaciones = self.notificaciones
                except Exception as error:
                    # Sin esto el hilo moría con la excepción sin capturar y la ventana se
                    # quedaba sin saber nada: ni error, ni estado, ni forma de reintentar.
                    logger.exception("No se pudo preparar el envío")
                    self.ultimo_error = f"No se pudo preparar el envío: {error}"
                    self._emitir(self.eventos.error, self.ultimo_error)
                    self._hilo = None
                    self._enviando = None
                    self._set_estado(EstadoCola("stopped", razon=StopReason.FATAL, texto=self.ultimo_error))
                    return
                self._motor = motor
            problema = ""
            try:
                # Mientras hay algo que enviar o esperando, el equipo no debe dormirse (si no, «espera sola» sería falso).
                # En Windows esto es POR HILO, así que se activa y se suelta aquí, en el hilo que trabaja.
                with KeepAwake():
                    razon = motor.ejecutar(self._callbacks())
                problema = str(getattr(motor, "problema", "") or "")
            except Exception as error:  # pragma: no cover - defensa; el motor ya captura casi todo
                logger.exception("Fallo inesperado en la cola")
                self.ultimo_error = f"Error inesperado: {error}"
                self._emitir(self.eventos.error, self.ultimo_error)
                razon = StopReason.FATAL
            with self._lock:
                self._motor = None
                self._enviando = None
                if razon is StopReason.COMPLETED:
                    continue  # ¿llegó algo mientras tanto? el bucle lo comprueba arriba
                if razon is StopReason.STOPPED and self._pausada:
                    continue  # la pausa se resuelve arriba, con el estado correcto
                self._hilo = None
                self._set_estado(EstadoCola("stopped", razon=razon, texto=self.ultimo_error, codigo=problema))
                return

    def _callbacks(self) -> Callbacks:
        def estado(texto: str, activo: bool) -> None:
            pass  # el texto del motor está en español; la interfaz redacta el suyo

        def inicio_orden(msg: JobMessage) -> None:
            self._enviando = msg.id
            self._set_estado(EstadoCola("sending", msg_id=msg.id))

        def resultado(msg: JobMessage) -> None:
            if self._enviando == msg.id and msg.status != STATUS_PENDING:
                self._enviando = None
            self._emitir(self.eventos.orden_cambiada, msg)

        def espera(motivo: str, segundos: float) -> None:
            self._set_estado(EstadoCola("waiting", motivo=motivo, segundos=segundos, msg_id=self._enviando))

        def tick(total: float, restante: float) -> None:
            self._emitir(self.eventos.cuenta_atras, restante)

        def gasto(usd: Decimal, consumo: Any) -> None:
            self._emitir(self.eventos.gasto, usd)

        def plan(estado: Any) -> None:
            self._emitir(self.eventos.plan, estado)

        def error(texto: str) -> None:
            self.ultimo_error = texto
            self._emitir(self.eventos.error, texto)

        def delta(msg_id: int, texto: str) -> None:
            self._emitir(self.eventos.delta, msg_id, texto)

        return Callbacks(
            estado=estado, inicio_orden=inicio_orden, resultado=resultado, espera=espera,
            tick=tick, gasto=gasto, error=error, delta=delta, plan=plan,
        )

    # ------------------------------------------------------------------

    def _set_estado(self, estado: EstadoCola) -> None:
        self.estado = estado
        self._emitir(self.eventos.estado, estado)

    def _emitir(self, metodo: Callable[..., None], *args: Any) -> None:
        def entregar() -> None:
            try:
                metodo(*args)
            except Exception:
                logger.exception("Fallo en un evento de la cola")

        self._publicar(entregar)


def hay_fallidas(job: Optional[Job]) -> bool:
    return bool(job and any(m.status == STATUS_FAILED for m in job.messages))
