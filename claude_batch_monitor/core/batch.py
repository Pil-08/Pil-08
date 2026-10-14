"""Motor de lote: Message Batches API.

50 % más barato en todos los tokens, hasta 100 000 órdenes (o 256 MB) por lote,
resultados normalmente en menos de una hora y como máximo en 24. A cambio no se
ve nada hasta que termina, y no hay forma de parar a mitad sin cancelar el lote.

Dos consecuencias para el gasto:
- El tope se comprueba ANTES de enviar contra el peor caso de todo el lote, porque
  una vez enviado no se puede frenar. Por eso, aquí, bajar `max_tokens` es la
  palanca que hace caber un lote en un tope.
- Los resultados llegan SIN ORDEN: se emparejan siempre por `custom_id`.

Referencia: https://platform.claude.com/docs/en/build-with-claude/batch-processing
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Optional

from ..config import Configuracion
from ..job import STATUS_PENDING, Job, JobMessage, listar_jobs
from ..paths import logger
from ..pricing import formatear_usd
from .base import (  # `anthropic` viene de aquí: base.py decide qué hacer si la librería no está
    Callbacks,
    Contadores,
    Decision,
    EngineBase,
    StopReason,
    anthropic,
    build_request_params,
)
from .errors import pudo_haberse_procesado

LIMITE_ORDENES = 100_000
LIMITE_BYTES = 256 * 1024 * 1024
SOBRECARGA_POR_PETICION = 400  # bytes de JSON alrededor de cada orden
INTERVALO_MIN_SEC = 10.0
INTERVALO_MAX_SEC = 60.0
# Para reconocer un lote que el servidor creó sin que nos llegara su respuesta.
HOLGURA_RELOJES = timedelta(minutes=10)  # lo que pueden diferir el reloj de esta máquina y el del servidor
MAX_LOTES_A_MIRAR = 200
# Al recoger un lote, el estado y el gasto se escriben a intervalos, no con cada resultado: son del tamaño de TODO el lote.
RESULTADOS_ENTRE_PUNTOS_DE_CONTROL = 200
SEGUNDOS_ENTRE_PUNTOS_DE_CONTROL = 20.0


class _Busqueda(Enum):
    ENCONTRADO = "found"     # el servidor sí lo creó: se sigue con ese
    SIN_RASTRO = "none"      # no hay ningún lote así: se puede enviar
    PARAR = "stop"           # no se pudo comprobar, o hay dudas: no se envía


def custom_id(msg: JobMessage) -> str:
    """Id estable de una orden dentro del lote (`^[a-zA-Z0-9_-]{1,64}$`)."""
    return f"m{msg.id:06d}"


def _fecha(valor: Any) -> Optional[datetime]:
    """Una fecha de la API (datetime o texto ISO) como datetime con zona horaria; None si no se entiende."""
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(valor, datetime):
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)


def _ordenes_del_lote(lote: Any) -> int:
    contadores = getattr(lote, "request_counts", None)
    return sum(
        int(getattr(contadores, clave, 0) or 0)
        for clave in ("processing", "succeeded", "errored", "canceled", "expired")
    )


def _cerrado(cliente: Any) -> bool:
    try:
        return bool(cliente.is_closed())
    except Exception:
        return False  # un doble sin `is_closed`, o cualquier otra cosa: se usa tal cual


def _datos_del_error(resultado: Any) -> tuple[str, str]:
    """(tipo, mensaje) de un resultado `errored`; tolera las dos formas del objeto."""
    envoltorio = getattr(resultado, "error", None)
    interno = getattr(envoltorio, "error", envoltorio)
    return str(getattr(interno, "type", "") or ""), str(getattr(interno, "message", "") or "")


class BatchEngine(EngineBase):
    """Envía todas las órdenes pendientes como un único lote y recoge los resultados."""

    def __init__(self, config: Configuracion, job: Job):
        super().__init__(config, job)
        self.intervalo_min = INTERVALO_MIN_SEC
        self.intervalo_max = INTERVALO_MAX_SEC

    # ------------------------------------------------------------------

    def ejecutar(self, cb: Optional[Callbacks] = None) -> StopReason:
        cb = cb or Callbacks()
        try:
            self.inicializar_cliente()
        except Exception as error:
            cb.emit("error", str(error))
            self.marcar_parada(StopReason.FATAL)
            return self._finalizar()

        self.deadline = self.calcular_deadline()
        ciclo = self.job.ciclos_completados + 1
        try:
            if self.job.batch_id:
                cb.emit("estado", f"Reenganchando el lote {self.job.batch_id}, que sigue en el servidor…", True)
            elif self.job.pendientes():
                self._enviar(cb)
            if self.stop_reason is None and self.job.batch_id:
                self._seguir(cb, ciclo)
        finally:
            # Sin retornos tempranos: el resumen se escribe siempre (M12).
            self.cerrar_ciclo(ciclo, cb)
            self.cerrar_cliente()
        return self._finalizar()

    # ------------------------------------------------------------------
    # Llamadas con reintentos
    # ------------------------------------------------------------------

    def _llamar(
        self,
        fn: Callable[[], Any],
        cb: Callbacks,
        al_no_existir: Optional[Callable[[], None]] = None,
    ) -> Any:
        """Una consulta de solo lectura a la API (estado de un lote, lista de lotes), con reintentos.

        No cuesta nada ni cambia nada en el servidor, así que se puede repetir sin miedo a cobrar dos veces (véase
        `manejar_error_de_consulta`). Devuelve None si hay que abandonar (la razón ya está en `stop_reason`).
        """
        cont = Contadores()
        while True:
            if self._debe_parar():
                return None
            try:
                return fn()
            except anthropic.NotFoundError as error:
                if al_no_existir is not None:
                    al_no_existir()
                    return None
                if self.manejar_error(error, cb, cont) is Decision.PARAR:
                    return None
            except Exception as error:
                if self.manejar_error_de_consulta(error, cb, cont) is not Decision.REINTENTAR:
                    return None

    # ------------------------------------------------------------------
    # Envío
    # ------------------------------------------------------------------

    def _enviar(self, cb: Callbacks) -> None:
        pendientes = self.job.pendientes()
        if not pendientes:
            return

        if len(pendientes) > LIMITE_ORDENES:
            self._abortar(
                cb,
                f"Un lote admite como máximo {LIMITE_ORDENES:,} órdenes y hay {len(pendientes):,}. "
                "Divide el fichero en varios lotes.",
            )
            return
        sistema = len(self.config.system_prompt.encode("utf-8"))
        peso = sum(len(m.text.encode("utf-8")) + sistema + SOBRECARGA_POR_PETICION for m in pendientes)
        if peso > LIMITE_BYTES:
            self._abortar(
                cb,
                f"El lote pesaría ~{peso // (1024 * 1024)} MB y el máximo es 256 MB. Divídelo en varios lotes.",
            )
            return

        if not self._cabe_en_el_tope(pendientes, cb):
            return
        if not self._esperar_franja(cb):  # (recoger resultados no es enviar: solo el envío espera a la franja)
            return

        peticiones = [
            {"custom_id": custom_id(m), "params": build_request_params(self.config, m.text)}
            for m in pendientes
        ]
        cb.emit("estado", f"Enviando {len(pendientes):,} órdenes como un lote…", True)
        self._crear_lote(peticiones, cb)

    # ------------------------------------------------------------------
    # Crear el lote sin crear dos
    # ------------------------------------------------------------------

    def _crear_lote(self, peticiones: list[dict], cb: Callbacks) -> bool:
        """Crea el lote en el servidor y apunta su id. Nunca crea dos por un mismo envío.

        Si la petición pudo llegar pero no su respuesta (tiempo agotado, corte, un 5xx, o el programa se cerró en
        pleno envío), el servidor pudo crear —y cobrar— el lote sin que lo supiéramos, y repetir el envío
        crearía otro. Por eso el envío se apunta en disco ANTES de hacerlo, y antes de repetirlo (en esta
        ejecución o en la siguiente) se busca ese lote entre los recientes de la cuenta.
        """
        cont = Contadores()
        n = len(peticiones)
        if self.job.envio_incierto_desde:  # un envío anterior quedó sin respuesta
            busqueda = self._buscar_envio_anterior(cb)
            if busqueda is not _Busqueda.SIN_RASTRO:
                return busqueda is _Busqueda.ENCONTRADO
        while True:
            if self._debe_parar():
                return False
            if not self.job.envio_incierto_desde:
                # Antes de apuntarlo, se anota cuál es el último lote de la cuenta: lo que aparezca después de él
                # es lo único que puede ser el nuestro (no depende de que los relojes coincidan).
                self.job.envio_incierto_tras = self._ultimo_lote_de_la_cuenta()
                self.job.envio_incierto_desde = datetime.now(timezone.utc).isoformat()
                self.job.envio_incierto_ordenes = n
                self.job.save()
            try:
                lote = self.client.messages.batches.create(requests=peticiones)
            except Exception as error:
                if not pudo_haberse_procesado(error):
                    self._olvidar_envio()  # el servidor ni lo vio (o lo rechazó): no hay nada que buscar
                if self.manejar_error(error, cb, cont) is not Decision.REINTENTAR:
                    return False
                if self.job.envio_incierto_desde:  # ya esperó lo que tocaba: ¿llegó a crearse?
                    busqueda = self._buscar_envio_anterior(cb)
                    if busqueda is not _Busqueda.SIN_RASTRO:
                        return busqueda is _Busqueda.ENCONTRADO
                continue
            self._fijar_lote(lote, n, False, cb)
            return True

    def _ultimo_lote_de_la_cuenta(self) -> Optional[str]:
        """Id del lote más reciente de la cuenta ("" si no hay ninguno); None si no se pudo mirar."""
        try:
            for lote in self.client.messages.batches.list(limit=1):
                return str(lote.id)
            return ""
        except Exception:
            logger.warning("No se pudo mirar cuál es el último lote de la cuenta", exc_info=True)
            return None  # sin ese dato, la búsqueda se limita por la fecha (con holgura para los relojes)

    def _olvidar_envio(self) -> None:
        if self.job.envio_incierto_desde:
            self.job.envio_incierto_desde = ""
            self.job.envio_incierto_ordenes = 0
            self.job.envio_incierto_tras = None
            self.job.save()

    def _fijar_lote(self, lote: Any, n: int, adoptado: bool, cb: Callbacks) -> None:
        self.job.batch_id = str(lote.id)
        self.job.envio_incierto_desde = ""
        self.job.envio_incierto_ordenes = 0
        self.job.envio_incierto_tras = None
        self.job.save()
        if adoptado:
            texto = f"El envío anterior sí había creado el lote {lote.id} ({n:,} órdenes): sigo con ese en vez de enviar otro."
            logger.warning(texto)
        else:
            texto = f"Lote enviado ({n:,} órdenes). Id: {lote.id}"
            logger.info("Lote %s enviado: %d órdenes (id del servidor %s)", self.job.job_id, n, lote.id)
        cb.emit("estado", texto, True)

    def _buscar_envio_anterior(self, cb: Callbacks) -> _Busqueda:
        """¿Llegó a crear el servidor el lote de un envío que quedó sin respuesta?

        Se busca entre los lotes de la cuenta creados después del último que había al enviar (si se anotó cuál era;
        si no, entre los de los últimos minutos, con holgura para los relojes) uno con el mismo número de órdenes
        y que no sea ya de otra tarea nuestra. Si hay exactamente uno, es el nuestro.
        """
        desde = _fecha(self.job.envio_incierto_desde)
        n = self.job.envio_incierto_ordenes
        if desde is None or n <= 0:
            self._olvidar_envio()
            return _Busqueda.SIN_RASTRO
        desde -= HOLGURA_RELOJES
        tras = self.job.envio_incierto_tras
        cb.emit("estado", "Comprobando si el envío anterior llegó a crear el lote…", True)

        def recientes() -> list:
            salida: list = []
            for lote in self.client.messages.batches.list(limit=100):  # del más nuevo al más viejo
                if tras is not None:
                    if str(lote.id) == tras:
                        break  # de aquí hacia atrás ya existía antes de enviar: no puede ser el nuestro
                else:
                    creado = _fecha(getattr(lote, "created_at", None))
                    if creado is not None and creado < desde:
                        break
                salida.append(lote)
                if len(salida) >= MAX_LOTES_A_MIRAR:
                    break
            return salida

        vistos = self._llamar(recientes, cb)
        if vistos is None:
            return _Busqueda.PARAR  # no se pudo comprobar: sin saberlo, no se envía otro
        ajenos = {d["batch_id"] for d in listar_jobs() if d["batch_id"] and d["job_id"] != self.job.job_id}
        candidatos = [
            lote for lote in vistos
            if (tras is not None or _fecha(getattr(lote, "created_at", None)) is not None)  # sin línea base, hace falta fecha
            and _ordenes_del_lote(lote) == n and str(lote.id) not in ajenos
        ]
        if not candidatos:
            self._olvidar_envio()
            return _Busqueda.SIN_RASTRO
        if len(candidatos) == 1:
            self._fijar_lote(candidatos[0], n, True, cb)
            return _Busqueda.ENCONTRADO

        ids = ", ".join(str(lote.id) for lote in candidatos[:5])
        texto = (
            f"El envío anterior quedó sin respuesta y hay {len(candidatos)} lotes recientes de {n:,} órdenes en tu "
            f"cuenta ({ids}): no sé cuál es el tuyo y enviar otro podría cobrarte dos veces. Míralos en la consola "
            "de Anthropic (Batches). Si ninguno es el tuyo, pulsa Reanudar otra vez y se enviará uno nuevo."
        )
        self._olvidar_envio()  # el aviso se da una vez; la siguiente reanudación ya es decisión de la persona
        logger.warning(texto)
        cb.emit("error", texto)
        self.marcar_parada(StopReason.FATAL)
        return _Busqueda.PARAR

    def _cabe_en_el_tope(self, pendientes: list[JobMessage], cb: Callbacks) -> bool:
        """El peor caso del lote entero debe caber en lo que queda del tope."""
        restante = self.restante_del_tope()
        if restante is None or not self.config.estimar_antes_de_enviar:
            return True
        peor = sum((self.estimar_coste_maximo(m, lote=True) for m in pendientes), Decimal(0))
        if peor <= restante:
            return True

        # Cuántos max_tokens de salida cabrían, grosso modo, repartidos entre las órdenes.
        pista = ""
        n = Decimal(len(pendientes))
        entrada_total = peor - n * self._coste_salida_lote(self.config.max_tokens)
        por_token = n * self._coste_salida_lote(1)
        if por_token > 0 and restante > entrada_total:
            cabrian = int((restante - entrada_total) / por_token)
            if cabrian >= 1:
                pista = f" Con max_tokens ≈ {cabrian:,} sí cabría."
        texto = (
            f"TOPE DE GASTO: en el peor caso este lote costaría hasta {formatear_usd(peor, 2)} "
            f"y solo quedan {formatear_usd(restante, 2)} de tu tope. No se envía: en modo lote no se puede "
            f"frenar a mitad. Sube el tope, baja max_tokens o divide el lote.{pista}"
        )
        logger.warning(texto)
        cb.emit("error", texto)
        self._avisar("Tope de gasto", texto)
        self.marcar_parada(StopReason.BUDGET)
        return False

    def _coste_salida_lote(self, tokens: int) -> Decimal:
        return Decimal(tokens) * self.precios.salida / Decimal(1_000_000) / 2

    def _abortar(self, cb: Callbacks, texto: str) -> None:
        cb.emit("error", texto)
        self.marcar_parada(StopReason.FATAL)

    # ------------------------------------------------------------------
    # Espera y recogida
    # ------------------------------------------------------------------

    @staticmethod
    def _progreso(lote: Any) -> str:
        rc = getattr(lote, "request_counts", None)
        if rc is None:
            return "Lote en curso…"
        ok = int(getattr(rc, "succeeded", 0) or 0)
        mal = sum(int(getattr(rc, k, 0) or 0) for k in ("errored", "canceled", "expired"))
        en_curso = int(getattr(rc, "processing", 0) or 0)
        total = ok + mal + en_curso
        extra = f", {mal} con problema" if mal else ""
        return f"Lote en curso: {ok + mal:,}/{total:,} procesadas ({ok:,} correctas{extra})"

    def _seguir(self, cb: Callbacks, ciclo: int) -> None:
        intervalo = self.intervalo_min
        while True:
            lote = self._llamar(
                lambda: self.client.messages.batches.retrieve(self.job.batch_id),
                cb,
                al_no_existir=lambda: self._lote_perdido(cb),
            )
            if lote is None:
                return
            cb.emit("estado", self._progreso(lote), True)
            if getattr(lote, "processing_status", "") == "ended":
                break
            # Si el usuario cierra o detiene, el lote sigue en el servidor y se reengancha.
            if not self.esperar(intervalo, cb):
                return
            intervalo = min(self.intervalo_max, intervalo * 1.5)
        self._recoger(cb, ciclo)

    def _lote_perdido(self, cb: Callbacks) -> None:
        texto = (
            f"El lote {self.job.batch_id} ya no existe en el servidor (los resultados se conservan 29 días). "
            "Las órdenes siguen pendientes: si reanudas se enviarán en un lote nuevo."
        )
        self.job.batch_id = ""
        self.job.save()
        cb.emit("error", texto)
        self.marcar_parada(StopReason.FATAL)

    def _recoger(self, cb: Callbacks, ciclo: int) -> None:
        por_id = {custom_id(m): m for m in self.job.messages}
        vistos: set[str] = set()
        cont = Contadores()
        cb.emit("estado", "Lote terminado. Recogiendo resultados…", True)
        while True:
            antes = len(vistos)
            try:
                # Los resultados llegan en cualquier orden: se emparejan por custom_id.
                if not self._apuntar_resultados(por_id, vistos, ciclo, cb):
                    return  # no se puede escribir en disco: el lote sigue en el servidor y el resto se recoge al reanudar
                break
            except Exception as error:
                # Bajar los resultados no cuesta nada, así que repetirlo nunca duplica un cobro: una descarga cortada,
                # una página de un proxy o un tiempo agotado se repiten con espera. Lo demás sigue la política de siempre.
                if len(vistos) > antes:
                    cont = Contadores()  # esta vez se avanzó: los fallos se cuentan sin avance, no en toda una descarga larga
                if self.manejar_error_de_consulta(error, cb, cont) is not Decision.REINTENTAR:
                    return  # recogida a medias: se conserva el batch_id y al reanudar se completa

        for msg in self.job.pendientes():
            if custom_id(msg) not in vistos:
                self.marcar_fallo(msg, "El lote no devolvió resultado para esta orden.", cb, "missing", guardar=False)
        logger.info("Lote %s recogido (servidor %s)", self.job.job_id, self.job.batch_id)
        self.job.batch_id = ""
        if self.punto_de_control(cb):
            self._comprobar_tope(cb)  # ahora que está todo pagado y guardado

    def _apuntar_resultados(self, por_id: dict, vistos: set, ciclo: int, cb: Callbacks) -> bool:
        """Recorre los resultados del servidor y apunta los que faltan. False si hay que dejar de recoger (el disco no admite más).

        El estado del lote y el gasto se escriben cada cierto número de resultados y, pase lo que pase (un fallo de la
        descarga, Ctrl+C), con lo que se haya apuntado hasta ese momento.
        """
        sin_guardar = 0
        ultimo = time.monotonic()
        cada = max(RESULTADOS_ENTRE_PUNTOS_DE_CONTROL, len(por_id) // 20)
        try:
            for entrada in self.client.messages.batches.results(self.job.batch_id):
                msg = por_id.get(entrada.custom_id)
                if msg is None:
                    continue
                vistos.add(entrada.custom_id)
                # Una recogida cortada y reanudada vuelve a pasar por aquí: lo que ya
                # está resuelto no se vuelve a contabilizar (inflaría el gasto) (Y2).
                if msg.status != STATUS_PENDING:
                    continue
                if not self._aplicar(msg, entrada.result, ciclo, cb):
                    return False
                sin_guardar += 1
                if sin_guardar >= cada or time.monotonic() - ultimo > SEGUNDOS_ENTRE_PUNTOS_DE_CONTROL:
                    guardado, sin_guardar = self.punto_de_control(cb), 0
                    if not guardado:
                        return False
                    ultimo = time.monotonic()
            return True
        finally:
            if sin_guardar:
                self.punto_de_control(cb)

    def _aplicar(self, msg: JobMessage, resultado: Any, ciclo: int, cb: Callbacks) -> bool:
        """Apunta el resultado de una orden. False si hay que dejar de recoger (el disco no admite más)."""
        tipo = getattr(resultado, "type", "")
        if tipo == "succeeded":
            return self.registrar_respuesta(
                msg, resultado.message, ciclo, cb, lote=True, comprobar_tope=False, guardar_ya=False
            )
        if tipo == "errored":
            tipo_error, mensaje = _datos_del_error(resultado)
            detalle = f"La API devolvió un error ({tipo_error or 'desconocido'})."
            if tipo_error.startswith("invalid_request"):
                detalle += " La petición es inválida; corrígela antes de reintentar."
                categoria = "bad_request"
            else:
                detalle += " Es un error de servidor; se puede reintentar."
                categoria = "server"
            if mensaje:
                detalle += f" ({mensaje})"
            self.marcar_fallo(msg, detalle, cb, categoria, guardar=False)
        elif tipo == "expired":
            self.marcar_fallo(msg, "Caducada: el lote superó las 24 h. Reintenta la orden.", cb, "expired", guardar=False)
        elif tipo == "canceled":
            self.marcar_fallo(msg, "Cancelada antes de procesarse.", cb, "canceled", guardar=False)
        else:
            self.marcar_fallo(msg, f"Resultado desconocido: {tipo!r}", cb, "unknown", guardar=False)
        return True

    # ------------------------------------------------------------------

    def cancelar_lote(self) -> bool:
        """Cancela en el servidor el lote en curso. Lo ya procesado se cobra y se recoge al reanudar.

        Puede llamarse desde otro hilo mientras el motor termina: éste cierra su cliente al acabar, y una cancelación que
        lo estuviera usando fallaría justo entonces. Por eso, si falla con el cliente del motor, se repite con uno propio.
        """
        if not self.job.batch_id:
            return False
        compartido = self._client
        propio_primero = compartido is None or _cerrado(compartido)
        for intento in (0, 1):
            usar_propio = propio_primero or intento == 1
            cliente: Any = None
            try:
                cliente = self._cliente_nuevo() if usar_propio else compartido
                cliente.messages.batches.cancel(self.job.batch_id)
                logger.info("Cancelación pedida para el lote %s", self.job.batch_id)
                return True
            except Exception:
                logger.exception("No se pudo cancelar el lote %s", self.job.batch_id)
                if usar_propio:
                    return False
            finally:
                cerrar = getattr(cliente, "close", None) if usar_propio else None
                if callable(cerrar):
                    try:
                        cerrar()
                    except Exception:
                        pass
        return False
