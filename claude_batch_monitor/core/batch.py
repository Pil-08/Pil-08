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

from decimal import Decimal
from typing import Any, Callable, Optional

import anthropic

from ..config import Configuracion
from ..job import STATUS_PENDING, Job, JobMessage
from ..paths import logger
from ..pricing import formatear_usd
from .base import (
    Callbacks,
    Contadores,
    Decision,
    EngineBase,
    StopReason,
    build_request_params,
)

LIMITE_ORDENES = 100_000
LIMITE_BYTES = 256 * 1024 * 1024
SOBRECARGA_POR_PETICION = 400  # bytes de JSON alrededor de cada orden
INTERVALO_MIN_SEC = 10.0
INTERVALO_MAX_SEC = 60.0


def custom_id(msg: JobMessage) -> str:
    """Id estable de una orden dentro del lote (`^[a-zA-Z0-9_-]{1,64}$`)."""
    return f"m{msg.id:06d}"


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
        """Una llamada a la API con la misma política de errores que el motor inmediato.

        Devuelve None si hay que abandonar (la razón ya está en `stop_reason`).
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
                if self.manejar_error(error, cb, cont) is not Decision.REINTENTAR:
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

        peticiones = [
            {"custom_id": custom_id(m), "params": build_request_params(self.config, m.text)}
            for m in pendientes
        ]
        cb.emit("estado", f"Enviando {len(pendientes):,} órdenes como un lote…", True)
        lote = self._llamar(lambda: self.client.messages.batches.create(requests=peticiones), cb)
        if lote is None:
            return
        self.job.batch_id = str(lote.id)
        self.job.save()
        logger.info("Lote %s enviado: %d órdenes (id del servidor %s)", self.job.job_id, len(pendientes), lote.id)
        cb.emit("estado", f"Lote enviado ({len(pendientes):,} órdenes). Id: {lote.id}", True)

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
        cb.emit("estado", "Lote terminado. Recogiendo resultados…", True)
        try:
            # Los resultados llegan en cualquier orden: se emparejan por custom_id.
            for entrada in self.client.messages.batches.results(self.job.batch_id):
                msg = por_id.get(entrada.custom_id)
                if msg is None:
                    continue
                vistos.add(entrada.custom_id)
                # Una recogida cortada y reanudada vuelve a pasar por aquí: lo que ya
                # está resuelto no se vuelve a contabilizar (inflaría el gasto) (Y2).
                if msg.status != STATUS_PENDING:
                    continue
                self._aplicar(msg, entrada.result, ciclo, cb)
        except Exception as error:
            # Recogida a medias: se conserva el batch_id y al reanudar se completa.
            self.manejar_error(error, cb, Contadores())
            return

        for msg in self.job.pendientes():
            if custom_id(msg) not in vistos:
                self.marcar_fallo(msg, "El lote no devolvió resultado para esta orden.", cb, "missing")
        logger.info("Lote %s recogido (servidor %s)", self.job.job_id, self.job.batch_id)
        self.job.batch_id = ""
        self.job.save()
        self._comprobar_tope(cb)  # ahora que está todo pagado y guardado

    def _aplicar(self, msg: JobMessage, resultado: Any, ciclo: int, cb: Callbacks) -> None:
        tipo = getattr(resultado, "type", "")
        if tipo == "succeeded":
            self.registrar_respuesta(
                msg, resultado.message, ciclo, cb, lote=True, comprobar_tope=False
            )
        elif tipo == "errored":
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
            self.marcar_fallo(msg, detalle, cb, categoria)
        elif tipo == "expired":
            self.marcar_fallo(msg, "Caducada: el lote superó las 24 h. Reintenta la orden.", cb, "expired")
        elif tipo == "canceled":
            self.marcar_fallo(msg, "Cancelada antes de procesarse.", cb, "canceled")
        else:
            self.marcar_fallo(msg, f"Resultado desconocido: {tipo!r}", cb, "unknown")

    # ------------------------------------------------------------------

    def cancelar_lote(self) -> bool:
        """Cancela en el servidor el lote en curso. Lo ya procesado se cobra y se recoge al reanudar."""
        if not self.job.batch_id:
            return False
        try:
            if self._client is None:
                self.inicializar_cliente()
            self.client.messages.batches.cancel(self.job.batch_id)
            return True
        except Exception:
            logger.exception("No se pudo cancelar el lote %s", self.job.batch_id)
            return False
