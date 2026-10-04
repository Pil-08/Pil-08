"""Lo común a los dos motores: contabilidad, esperas, persistencia y parada.

Las reglas que aquí se fijan son las que más dinero y datos protegen:

- **Persistir ANTES de comprobar el tope** (`registrar_respuesta`). Una respuesta
  que ya se pagó nunca se descarta porque el tope se haya alcanzado con ella.
- **Estimar ANTES de enviar** (`_reservar`). El tope se compara con el peor caso
  de la siguiente petición, no solo con lo ya gastado, y con concurrencia se
  reserva el peor caso de cada petición en vuelo para que N hilos no se pasen N veces.
- **Un solo motivo de parada, el más específico** (`marcar_parada`). Un hilo que
  despierta de su espera no puede degradar un "sin saldo" a un "detenido".
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Optional

import anthropic

from ..config import Configuracion
from ..export import MARCA_RESPUESTA, MARCA_SOLICITUD
from ..job import STATUS_COMPLETED, STATUS_FAILED, STATUS_REFUSED, Job, JobMessage
from ..notify import notificar
from ..paths import logger, write_atomic
from ..pricing import (
    ESCRITURA_CACHE_5M,
    Consumo,
    coste_consumo,
    coste_maximo,
    estimar_tokens,
    formatear_usd,
    leer_consumo,
    soporta_esfuerzo,
)
from ..ratelimit import RateLimitTracker, observar_seguro, parse_reset
from ..state import EstadoMonitor, obtener_api_key
from .errors import Categoria, ErrorApi, analizar_error, es_sin_conexion

MAX_BACKOFF_SECONDS = 1800
MARGEN_RATE_LIMIT_SEC = 2.0
# 429 seguidos SIN ninguna indicación de cuándo vuelve el cupo. Pasado esto no es
# un rate limit pasajero (probablemente un tope de gasto) y esperar más no sirve.
MAX_429_SIN_INFO = 10
# Tiempo máximo acumulado esperando a que vuelva la conexión antes de rendirse.
MAX_SIN_CONEXION_SEC = 600
RESPUESTA_VACIA = "(Claude devolvió una respuesta vacía.)"
AVISO_TRUNCADA = "[ADVERTENCIA: respuesta truncada por el límite de tokens]"


class StopReason(str, Enum):
    COMPLETED = "completed"
    STOPPED = "stopped"            # el usuario pulsó Detener
    DEADLINE = "deadline"          # llegó la hora límite
    BUDGET = "budget"              # tope de gasto propio
    NO_CREDIT = "no_credit"        # sin saldo (402)
    ACCOUNT_LIMIT = "account_limit"  # tope mensual del tier o límite fijado en la consola
    NETWORK = "network"
    FATAL = "fatal"


# Lo que se le dice a la persona al terminar, en la terminal y en las ventanas.
FRASES_DE_PARADA = {
    StopReason.COMPLETED: "Completado.",
    StopReason.STOPPED: "Detenido: el lote queda guardado y se puede reanudar.",
    StopReason.DEADLINE: "Hora límite alcanzada: el lote queda guardado y se puede reanudar.",
    StopReason.BUDGET: "Detenido por el tope de gasto. Lo pagado está guardado.",
    StopReason.NO_CREDIT: "Detenido: sin saldo en la cuenta de Anthropic.",
    StopReason.ACCOUNT_LIMIT: "Detenido: límite de gasto de la cuenta de Anthropic.",
    StopReason.NETWORK: "Detenido por un fallo de red. El lote se puede reanudar.",
    StopReason.FATAL: "Detenido por un error. Revisa el mensaje y reanuda el lote.",
}

# Motivos que no explican por qué se paró; cualquier motivo concreto los sustituye.
_DEBILES = {StopReason.STOPPED, StopReason.DEADLINE}


class Decision(Enum):
    """Qué hacer tras un error de la API."""

    REINTENTAR = "reintentar"
    SIGUIENTE = "siguiente"  # esta orden falla, pero las demás pueden seguir
    PARAR = "parar"


@dataclass
class Contadores:
    """Reintentos de UNA operación. Los de rate limit y sin conexión no cuentan como fallo."""

    errores: int = 0  # solo se usa cuando no hay una orden concreta a la que anotarlos
    sin_info_429: int = 0
    intentos_sin_conexion: int = 0
    segundos_sin_conexion: float = 0.0


@dataclass
class Callbacks:
    """Canales por los que el motor informa a la interfaz. Todos opcionales."""

    estado: Optional[Callable[[str, bool], None]] = None      # texto, ¿ocupado?
    resultado: Optional[Callable[[JobMessage], None]] = None  # una orden cambió de estado
    fin_ciclo: Optional[Callable[[int, int, int], None]] = None  # completadas, total, ciclo
    error: Optional[Callable[[str], None]] = None
    tick: Optional[Callable[[float, float], None]] = None     # espera total, restante
    gasto: Optional[Callable[[Decimal, Consumo], None]] = None
    # Para interfaces que quieren dibujar su propio texto en vez de usar el del motor:
    espera: Optional[Callable[[str, float], None]] = None     # motivo, segundos
    inicio_orden: Optional[Callable[[JobMessage], None]] = None
    delta: Optional[Callable[[int, str], None]] = None        # id de orden, texto nuevo (streaming)

    def emit(self, nombre: str, *args: Any) -> None:
        fn = getattr(self, nombre, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            # Un fallo en la interfaz nunca debe tumbar un lote que ya está cobrando.
            logger.exception("Fallo en el callback '%s'", nombre)


def build_request_params(config: Configuracion, texto: str) -> dict:
    """Parámetros de `messages.create` (o de una petición de lote) para una orden."""
    params: dict[str, Any] = {
        "model": config.modelo,
        "max_tokens": config.max_tokens,
        "messages": [{"role": "user", "content": texto}],
    }
    sistema = (config.system_prompt or "").strip()
    if sistema:
        bloque: dict[str, Any] = {"type": "text", "text": sistema}
        if config.usar_cache:
            bloque["cache_control"] = {"type": "ephemeral"}
        params["system"] = [bloque]
    if config.esfuerzo and soporta_esfuerzo(config.modelo, config.esfuerzo) is not False:
        params["output_config"] = {"effort": config.esfuerzo}
    return params


def _categoria_rechazo(response: Any) -> str:
    detalles = getattr(response, "stop_details", None)
    if detalles is None:
        return ""
    cat = detalles.get("category") if isinstance(detalles, dict) else getattr(detalles, "category", "")
    return str(cat or "")


class EngineBase:
    def __init__(self, config: Configuracion, job: Job):
        self.config = config
        self.job = job
        self.precios = config.precios()

        self.estado = EstadoMonitor.cargar()
        self.estado.current_job_id = job.job_id
        if config.reset_gasto_por_lote:
            # Contador por lote: parte de lo que ESTE lote ya lleva gastado (0 si es
            # nuevo). Así reanudar no reinicia el contador ni hereda el de otro lote.
            self.estado.gasto_acumulado_usd = job.coste_total()
        self.estado.guardar()

        self.evento_detener = threading.Event()
        self._client: Optional[Any] = None
        self.deadline: Optional[datetime] = None
        self.stop_reason: Optional[StopReason] = None
        self.rate_limits = RateLimitTracker()
        self.notificaciones = True

        self._lock_estado = threading.RLock()
        self._cond = threading.Condition(self._lock_estado)
        self._reservado = Decimal(0)

    # ------------------------------------------------------------------
    # Cliente
    # ------------------------------------------------------------------

    def inicializar_cliente(self) -> None:
        api_key = obtener_api_key()
        if not api_key:
            raise ValueError(
                "No hay API key de Anthropic. Pégala en la aplicación o define ANTHROPIC_API_KEY."
            )
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=float(self.config.timeout_api_sec), max_retries=0
        )

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("El cliente de la API no está inicializado.")
        return self._client

    def cerrar_cliente(self) -> None:
        """Libera el pool de conexiones (no se anula la referencia: puede consultarse después)."""
        cerrar = getattr(self._client, "close", None)
        if callable(cerrar):
            try:
                cerrar()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Tiempos
    # ------------------------------------------------------------------

    def calcular_deadline(self) -> Optional[datetime]:
        """Próxima ocurrencia de la hora límite, o None si no hay.

        Un lote único no necesita hora límite; un bucle SIEMPRE la necesita (si
        no, solo pararía el tope de gasto), así que se le aplica 23:59.
        """
        hora_txt = (self.config.hora_limite or "").strip()
        if not hora_txt and self.config.modo_continuo:
            hora_txt = "23:59"
        if not hora_txt:
            return None
        try:
            hora = datetime.strptime(hora_txt, "%H:%M").time()
        except ValueError:
            hora = dt_time(23, 59)
        ahora = datetime.now()
        limite = datetime.combine(ahora.date(), hora)
        if limite <= ahora:
            limite += timedelta(days=1)
        return limite

    @staticmethod
    def parse_reset(value: str) -> Optional[float]:
        return parse_reset(value)

    @staticmethod
    def _cabeceras(error: Any) -> dict:
        crudas = getattr(getattr(error, "response", None), "headers", None) or {}
        try:
            return {str(k).lower(): str(v) for k, v in dict(crudas).items()}
        except (TypeError, ValueError):
            return {}

    def espera_rate_limit_detalle(self, error: Any) -> tuple[float, bool]:
        """(segundos, ¿la API dijo cuánto?) para un 429.

        Orden de prioridad: `Retry-After` (la respuesta explícita del servidor) ->
        reset de los cubos agotados -> cualquier reset -> texto del mensaje ->
        un minuto. Sin backoff exponencial: cuando el servidor sabe cuándo vuelve
        el cupo, adivinarlo es peor.
        """
        h = self._cabeceras(error)
        retry_after = h.get("retry-after")
        if retry_after:
            try:
                return float(retry_after) + MARGEN_RATE_LIMIT_SEC, True
            except (TypeError, ValueError):
                pass

        agotados = []
        for fam in ("requests", "tokens", "input-tokens", "output-tokens"):
            restante = h.get(f"anthropic-ratelimit-{fam}-remaining")
            reset = parse_reset(h.get(f"anthropic-ratelimit-{fam}-reset", ""))
            if restante is not None and reset is not None and restante.strip() == "0":
                agotados.append(reset)
        agotados = [s for s in agotados if s > 0]
        if agotados:
            return max(agotados) + MARGEN_RATE_LIMIT_SEC, True

        for fam in ("tokens", "requests", "input-tokens", "output-tokens"):
            segundos = parse_reset(h.get(f"anthropic-ratelimit-{fam}-reset", ""))
            if segundos and segundos > 0:
                return segundos + MARGEN_RATE_LIMIT_SEC, True

        m = re.search(r"try again in\s+([\d.]+)\s*s", str(error), re.IGNORECASE)
        if m:
            return float(m.group(1)) + MARGEN_RATE_LIMIT_SEC, True
        return 60.0, False

    def espera_rate_limit(self, error: Any) -> float:
        return self.espera_rate_limit_detalle(error)[0]

    def espera_error_servidor(self, error: Any, intentos: int) -> float:
        """Backoff para 5xx/timeouts: *full jitter* sobre un techo exponencial.

        Un jitter aditivo pequeño deja a todos los hilos reintentando casi a la
        vez y vuelven a tumbar al servidor. Sortear en todo el intervalo [0, techo]
        los desincroniza.
        """
        retry_after = self._cabeceras(error).get("retry-after")
        if retry_after:
            try:
                return float(retry_after) + 5
            except (TypeError, ValueError):
                pass
        techo = min(30 * (2 ** min(max(intentos - 1, 0), 6)), MAX_BACKOFF_SECONDS)
        return max(1.0, random.uniform(0, techo))

    def esperar(self, segundos: float, cb: Callbacks) -> bool:
        """Espera cancelable. False si se interrumpió (parada o hora límite)."""
        fin = time.monotonic() + segundos
        cb.emit("tick", segundos, segundos)
        while True:
            restante = fin - time.monotonic()
            if restante <= 0:
                return True
            if self._debe_parar():
                return False
            if self.evento_detener.wait(min(0.5, restante)):
                return False
            cb.emit("tick", segundos, max(0.0, fin - time.monotonic()))

    # ------------------------------------------------------------------
    # Parada
    # ------------------------------------------------------------------

    def detener(self) -> None:
        self.marcar_parada(StopReason.STOPPED)

    def marcar_parada(self, razon: StopReason) -> None:
        """Fija el motivo de parada y despierta a todos los hilos.

        Solo escribe si no hay ya un motivo más específico (X5): un hilo que
        despierta de su backoff veía la parada y la degradaba a STOPPED.
        """
        with self._cond:
            actual = self.stop_reason
            if actual is None or (actual in _DEBILES and razon not in _DEBILES):
                self.stop_reason = razon
            self.evento_detener.set()
            self._cond.notify_all()

    def _debe_parar(self) -> bool:
        if self.deadline is not None and datetime.now() >= self.deadline:
            self.marcar_parada(StopReason.DEADLINE)
            return True
        if self.evento_detener.is_set():
            if self.stop_reason is None:
                self.marcar_parada(StopReason.STOPPED)
            return True
        return False

    # ------------------------------------------------------------------
    # Presupuesto
    # ------------------------------------------------------------------

    def tokens_entrada_estimados(self, texto: str) -> int:
        return estimar_tokens(texto) + estimar_tokens(self.config.system_prompt)

    def estimar_coste_maximo(self, msg: JobMessage, lote: bool = False) -> Decimal:
        mult = (
            ESCRITURA_CACHE_5M
            if (self.config.usar_cache and self.config.system_prompt.strip())
            else Decimal(1)
        )
        return coste_maximo(
            self.tokens_entrada_estimados(msg.text), self.config.max_tokens, self.precios, lote, mult
        )

    def gasto_actual(self) -> Decimal:
        with self._lock_estado:
            return self.estado.gasto_acumulado_usd

    def restante_del_tope(self) -> Optional[Decimal]:
        tope = self.config.tope_gasto_usd
        if tope <= 0:
            return None
        return max(Decimal(0), tope - self.gasto_actual())

    def _reservar(self, coste_max: Decimal, cb: Callbacks) -> bool:
        """Reserva el peor caso de una petición. False si el tope impide enviarla.

        Si no cabe pero hay otras peticiones en vuelo, se espera a que terminen:
        casi siempre cuestan bastante menos que su peor caso y liberan margen.
        """
        tope = self.config.tope_gasto_usd
        if tope <= 0 or not self.config.estimar_antes_de_enviar:
            return True
        with self._cond:
            while True:
                if self.evento_detener.is_set():
                    return False
                gastado = self.estado.gasto_acumulado_usd
                if gastado + self._reservado + coste_max <= tope:
                    self._reservado += coste_max
                    return True
                if self._reservado == 0:
                    texto = (
                        f"TOPE DE GASTO: la siguiente orden podría costar hasta {formatear_usd(coste_max)} "
                        f"y solo quedan {formatear_usd(max(Decimal(0), tope - gastado))} de tu tope de "
                        f"{formatear_usd(tope, 2)}. Lote detenido sin enviarla. "
                        "Sube el tope o baja max_tokens."
                    )
                    logger.warning(texto)
                    cb.emit("error", texto)
                    self._avisar("Tope de gasto", texto)
                    self.marcar_parada(StopReason.BUDGET)
                    return False
                self._cond.wait(timeout=1.0)

    def _liberar(self, coste_max: Decimal) -> None:
        with self._cond:
            self._reservado = max(Decimal(0), self._reservado - coste_max)
            self._cond.notify_all()

    def _comprobar_tope(self, cb: Callbacks) -> bool:
        """True si se puede seguir. Se llama DESPUÉS de persistir la respuesta."""
        tope = self.config.tope_gasto_usd
        if tope <= 0:
            return True
        gasto = self.gasto_actual()
        if gasto < tope:
            return True
        texto = (
            f"TOPE DE GASTO alcanzado: {formatear_usd(gasto)} / {formatear_usd(tope, 2)}. "
            "Lote detenido para proteger tu saldo. Todo lo ya pagado está guardado."
        )
        logger.critical(texto)
        cb.emit("error", texto)
        self._avisar("Tope de gasto", texto)
        self.marcar_parada(StopReason.BUDGET)
        return False

    # ------------------------------------------------------------------
    # Registro de resultados
    # ------------------------------------------------------------------

    def registrar_respuesta(
        self,
        msg: JobMessage,
        response: Any,
        ciclo: int,
        cb: Callbacks,
        latency_ms: int = 0,
        lote: bool = False,
        reserva: Decimal = Decimal(0),
        comprobar_tope: bool = True,
    ) -> bool:
        """Guarda una respuesta ya recibida (y pagada). True si se puede seguir.

        El orden importa: primero se contabiliza y se escribe a disco, y solo
        después se mira el tope. Al revés, la respuesta que cruza el tope se
        perdía habiendo sido facturada.

        `reserva` es el peor caso que se reservó antes de enviar: se canjea por el
        coste real en el mismo instante en que se suma el gasto, sin dejar un hueco
        en el que otro hilo pueda reservar de más.
        """
        canjeada = [False]
        try:
            return self._registrar(
                msg, response, ciclo, cb, latency_ms, lote, reserva, canjeada, comprobar_tope
            )
        finally:
            if reserva and not canjeada[0]:  # algo falló antes de canjearla: no dejarla colgada
                self._liberar(reserva)

    def _registrar(
        self,
        msg: JobMessage,
        response: Any,
        ciclo: int,
        cb: Callbacks,
        latency_ms: int,
        lote: bool,
        reserva: Decimal,
        canjeada: list,
        comprobar_tope: bool,
    ) -> bool:
        consumo = leer_consumo(getattr(response, "usage", None))
        coste = coste_consumo(consumo, self.precios, lote)
        stop = getattr(response, "stop_reason", None)
        rechazada = stop == "refusal"

        bloques = [
            b.text
            for b in (getattr(response, "content", None) or [])
            if isinstance(getattr(b, "text", None), str)
        ]
        texto = "\n".join(bloques).strip()
        truncada = stop in ("max_tokens", "model_context_window_exceeded")

        if rechazada:
            categoria = _categoria_rechazo(response)
            etiqueta = f" ({categoria})" if categoria else ""
            detalle = f"El modelo se negó a responder{etiqueta}."
            texto = (texto + "\n\n" if texto else "") + f"[{detalle}]"
        else:
            detalle = ""
            if not texto:
                texto = RESPUESTA_VACIA
            if truncada:
                texto += f"\n\n{AVISO_TRUNCADA}"

        with self._cond:
            self.estado.gasto_acumulado_usd += coste
            self.estado.tokens_input_total += consumo.entrada_total
            self.estado.tokens_output_total += consumo.salida
            if not rechazada:
                self.estado.exitos += 1
                self.estado.ultimo_exito = datetime.now(timezone.utc).isoformat()
            self.estado.guardar()
            gasto = self.estado.gasto_acumulado_usd
            if reserva:  # canje atómico: peor caso reservado -> coste real
                self._reservado = max(Decimal(0), self._reservado - reserva)
                canjeada[0] = True
                self._cond.notify_all()

        try:
            write_atomic(
                self.job.response_path(msg.id, ciclo),
                f"{MARCA_SOLICITUD}\n{msg.text}\n\n{MARCA_RESPUESTA}\n{texto}\n",
            )
        except OSError:
            logger.exception("No se pudo guardar la respuesta de la orden %s", msg.id)

        msg.status = STATUS_REFUSED if rechazada else STATUS_COMPLETED
        msg.truncated = truncada and not rechazada
        msg.tokens_input = consumo.entrada_total
        msg.tokens_output = consumo.salida
        msg.tokens_cache_read = consumo.lectura_cache
        msg.coste_usd += coste
        msg.latency_ms = latency_ms
        msg.detalle = detalle
        msg.error_type = "refusal" if rechazada else ""
        self.job.save()

        cb.emit("resultado", msg)
        cb.emit("gasto", gasto, consumo)
        # En un lote ya está todo pagado en el servidor: el tope se comprueba una vez al final.
        return self._comprobar_tope(cb) if comprobar_tope else True

    def marcar_fallo(self, msg: JobMessage, detalle: str, cb: Callbacks, error_type: str = "other") -> None:
        msg.status = STATUS_FAILED
        msg.detalle = detalle
        msg.error_type = error_type
        self.job.save()
        cb.emit("resultado", msg)

    # ------------------------------------------------------------------
    # Cierre
    # ------------------------------------------------------------------

    def cerrar_ciclo(self, ciclo: int, cb: Callbacks) -> None:
        """Escribe el resumen del ciclo. Se llama SIEMPRE, también al abortar:
        justo cuando algo falla es cuando más falta hace saber qué se hizo y qué se pagó."""
        total = self.job.total_count()
        completados = self.job.completed_count()
        try:
            partes = [
                f"# Resumen del Ciclo {ciclo}",
                f"Fecha: {datetime.now().isoformat()}",
                f"Completados: {completados}/{total}",
                f"Gasto acumulado: {formatear_usd(self.gasto_actual())}",
                "",
                "---",
                "",
            ]
            for m in self.job.messages:
                partes.append(f"## Mensaje {m.id} — {m.status}")
                if m.status == STATUS_COMPLETED or m.status == STATUS_REFUSED:
                    ruta = self.job.response_path(m.id, ciclo)
                    if ruta.exists():
                        partes.append(ruta.read_text(encoding="utf-8"))
                elif m.detalle:
                    partes.append(f"Detalle: {m.detalle}")
                partes += ["", "---", ""]
            write_atomic(self.job.resumen_path(ciclo), "\n".join(partes))
        except Exception:
            logger.exception("Error creando el resumen del ciclo %s", ciclo)
        cb.emit("fin_ciclo", completados, total, ciclo)

    def _avisar(self, titulo: str, texto: str) -> None:
        if self.notificaciones:
            notificar(titulo, texto, self.config.telegram_chat_id)

    def _finalizar(self) -> StopReason:
        """Motivo final del `ejecutar`, y limpieza del estado persistente."""
        razon = self.stop_reason or StopReason.COMPLETED
        if razon in (StopReason.BUDGET, StopReason.STOPPED, StopReason.DEADLINE) and not self.job.hay_trabajo():
            razon = StopReason.COMPLETED  # se paró justo al terminar: no queda nada pendiente
        self.stop_reason = razon
        if razon == StopReason.COMPLETED and not self.config.modo_continuo and not self.job.hay_trabajo():
            with self._lock_estado:
                self.estado.current_job_id = None
                self.estado.guardar()
        return razon

    # ------------------------------------------------------------------
    # Errores de la API (comunes a los dos motores)
    # ------------------------------------------------------------------

    def _fallar(self, msg: Optional[JobMessage], detalle: str, cb: Callbacks, tipo: str) -> None:
        if msg is not None:
            self.marcar_fallo(msg, detalle, cb, tipo)

    def manejar_error(
        self,
        error: Exception,
        cb: Callbacks,
        cont: Contadores,
        msg: Optional[JobMessage] = None,
    ) -> Decision:
        """Decide qué hacer con una excepción de la API. `msg` es la orden afectada, si la hay."""
        observar_seguro(self.rate_limits, error)

        # OJO con el orden: APITimeoutError HEREDA de APIConnectionError. Con el orden
        # inverso un timeout normal se tomaba por "sin red" y abortaba el lote entero (C5).
        if isinstance(error, anthropic.APITimeoutError):
            return self._reintentar_con_backoff(msg, error, cb, cont, "Timeout de la API")

        if isinstance(error, anthropic.APIConnectionError):
            if es_sin_conexion(error):
                return self._esperar_conexion(cb, cont)
            # La conexión se cortó a mitad: no sabemos si el servidor llegó a procesar
            # la petición. Parar evita facturar un duplicado.
            detalle = "Se cortó la conexión con la API. Lote pausado para evitar cobros duplicados."
            self._fallar(msg, detalle, cb, "connection")
            cb.emit("error", detalle)
            self.marcar_parada(StopReason.NETWORK)
            return Decision.PARAR

        if isinstance(error, anthropic.APIStatusError):
            return self._manejar_estado(analizar_error(error), error, cb, cont, msg)

        logger.error("Error inesperado", exc_info=error)
        detalle = f"Error inesperado: {error}"
        self._fallar(msg, detalle, cb, "unexpected")
        cb.emit("error", detalle)
        self.marcar_parada(StopReason.FATAL)
        return Decision.PARAR

    def _manejar_estado(
        self,
        info: ErrorApi,
        error: Exception,
        cb: Callbacks,
        cont: Contadores,
        msg: Optional[JobMessage],
    ) -> Decision:
        c = info.categoria
        etiqueta = f"[Orden {msg.id}] " if msg is not None else ""

        if c is Categoria.RATE_LIMIT:
            segundos, con_info = self.espera_rate_limit_detalle(error)
            cont.sin_info_429 = 0 if con_info else cont.sin_info_429 + 1
            if cont.sin_info_429 > MAX_429_SIN_INFO:
                self._parar_por_limite(info, cb, repetido=True)
                return Decision.PARAR
            cb.emit("espera", "rate_limit", segundos)
            cb.emit(
                "estado",
                f"{etiqueta}Límite de la API. Reintento en {int(segundos)}s (no cuenta como fallo)",
                False,
            )
            return Decision.REINTENTAR if self.esperar(segundos, cb) else Decision.PARAR

        if c is Categoria.LIMITE_CUENTA:
            self._parar_por_limite(info, cb)
            return Decision.PARAR

        if c is Categoria.SIN_SALDO:
            texto = (
                "Sin saldo en tu cuenta de Anthropic. Recarga créditos en la consola (Billing) y "
                "reanuda: la orden queda pendiente y lo ya pagado está guardado."
            )
            logger.critical(texto)
            cb.emit("error", texto)
            self._avisar("Saldo agotado", texto)
            self.marcar_parada(StopReason.NO_CREDIT)
            return Decision.PARAR

        if c is Categoria.SERVIDOR:
            que = f"Error de servidor {info.status or ''}".strip()
            return self._reintentar_con_backoff(msg, error, cb, cont, que)

        if c is Categoria.DEMASIADO_GRANDE and msg is not None:
            # Es culpa de esta orden, no del resto: se marca y se sigue con las demás.
            detalle = f"La orden es demasiado grande para la API (413). {info.mensaje}"
            self.marcar_fallo(msg, detalle, cb, c.value)
            cb.emit("error", detalle)
            return Decision.SIGUIENTE

        textos = {
            Categoria.AUTH: (
                f"Error de autenticación ({info.status}): la API key no es válida o no tiene permiso. "
                "Revísala en la consola de Anthropic."
            ),
            Categoria.MODELO: (
                f"El modelo '{self.config.modelo}' no existe o ya no está disponible. "
                "Elige otro (botón «Cargar modelos»)."
            ),
            Categoria.PETICION: f"La API rechazó la petición ({info.status}): {info.mensaje}",
        }
        detalle = textos.get(c, f"Error HTTP {info.status}: {info.mensaje}")
        self._fallar(msg, detalle, cb, c.value)
        cb.emit("error", detalle)
        self.marcar_parada(StopReason.FATAL)
        return Decision.PARAR

    def _parar_por_limite(self, info: ErrorApi, cb: Callbacks, repetido: bool = False) -> None:
        """Parada por un tope de gasto de la cuenta. La orden NO se marca como fallida."""
        if repetido:
            cuerpo = (
                "La API sigue rechazando las peticiones por límite sin indicar cuándo se levanta. "
                "Lo más probable es un tope de gasto: revisa Billing en la consola de Anthropic."
            )
        elif info.status == 429:
            cuando = (
                f"El acceso se restablece el {info.reanudacion}."
                if info.reanudacion
                else "El acceso vuelve a principios del mes siguiente."
            )
            cuerpo = (
                "Tu organización ha alcanzado el tope mensual de gasto de su nivel de uso. "
                f"{cuando} También puedes pedir un nivel superior en la consola."
            )
        else:
            cuerpo = (
                "Has alcanzado el límite de gasto que fijaste en la consola de Anthropic. "
                "Súbelo o quítalo en Billing → Spend limits para continuar."
            )
        texto = f"{cuerpo} Lo ya pagado está guardado y la orden queda pendiente. (API: «{info.mensaje}»)"
        logger.critical(texto)
        cb.emit("error", texto)
        self._avisar("Límite de gasto de la cuenta", cuerpo)
        self.marcar_parada(StopReason.ACCOUNT_LIMIT)

    def _reintentar_con_backoff(
        self,
        msg: Optional[JobMessage],
        error: Exception,
        cb: Callbacks,
        cont: Contadores,
        que: str,
    ) -> Decision:
        if msg is not None:
            msg.errores += 1
            self.job.save()
            errores = msg.errores
        else:
            cont.errores += 1
            errores = cont.errores
        if errores >= self.config.max_error_attempts:
            donde = f" en la orden {msg.id}" if msg is not None else ""
            detalle = f"{que}. Máximo de errores ({errores}){donde}: lote pausado."
            self._fallar(msg, detalle, cb, "max_retries")
            cb.emit("error", detalle)
            self._avisar("Lote pausado", detalle)
            self.marcar_parada(StopReason.FATAL)
            return Decision.PARAR
        segundos = self.espera_error_servidor(error, errores)
        cb.emit("espera", "server", segundos)
        etiqueta = f"[Orden {msg.id}] " if msg is not None else ""
        cb.emit(
            "estado",
            f"{etiqueta}{que}. Reintento en {int(segundos)}s (fallo {errores}/{self.config.max_error_attempts})",
            False,
        )
        return Decision.REINTENTAR if self.esperar(segundos, cb) else Decision.PARAR

    def _esperar_conexion(self, cb: Callbacks, cont: Contadores) -> Decision:
        """Sin conexión: la petición no salió, así que reintentar no puede duplicar un cobro."""
        paso = float(min(5 * (2 ** cont.intentos_sin_conexion), 60))
        cont.intentos_sin_conexion += 1
        cont.segundos_sin_conexion += paso
        if cont.segundos_sin_conexion > MAX_SIN_CONEXION_SEC:
            texto = (
                "Sin conexión con la API durante más de 10 minutos. Lote pausado; "
                "reanúdalo cuando vuelva internet."
            )
            cb.emit("error", texto)
            self._avisar("Sin conexión", texto)
            self.marcar_parada(StopReason.NETWORK)
            return Decision.PARAR
        cb.emit("espera", "sin_conexion", paso)
        cb.emit("estado", f"Sin conexión con la API. Reintento en {int(paso)}s…", False)
        return Decision.REINTENTAR if self.esperar(paso, cb) else Decision.PARAR
