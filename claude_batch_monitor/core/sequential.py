"""Motor inmediato: una petición a la Messages API por orden, en secuencia o en paralelo."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from ..config import Configuracion
from ..job import Job, JobMessage
from ..paths import logger
from ..ratelimit import observar_seguro
from .base import Callbacks, Contadores, Decision, EngineBase, StopReason, build_request_params


class SequentialEngine(EngineBase):
    """Procesa el lote orden a orden contra la API de mensajes."""

    def __init__(self, config: Configuracion, job: Job):
        super().__init__(config, job)

    # ------------------------------------------------------------------
    # Bucle principal
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
        logger.info(
            "Lote %s iniciado: %d órdenes, modelo %s, tope %s",
            self.job.job_id, self.job.total_count(), self.config.modelo, self.config.tope_gasto_usd,
        )
        try:
            while True:
                ciclo = self.job.ciclos_completados + 1
                self._ejecutar_ciclo(ciclo, cb)
                # Siempre, también al abortar (M12): el resumen es justo lo que más
                # falta hace cuando algo ha salido mal.
                self.cerrar_ciclo(ciclo, cb)

                if self.stop_reason is not None or not self.config.modo_continuo:
                    break
                if self._debe_parar():
                    break
                self.job.reset_for_new_cycle()
                self._avisar("Ciclo completado", f"Ciclo {ciclo} terminado. Empieza el {ciclo + 1}.")
                cb.emit("estado", f"Ciclo {ciclo} completado. Pausa larga...", False)
                if not self.esperar(self.config.pausa_ciclos_sec, cb):
                    break
        finally:
            self.cerrar_cliente()
        return self._finalizar()

    def _ejecutar_ciclo(self, ciclo: int, cb: Callbacks) -> None:
        pendientes = self.job.pendientes()
        if not pendientes:
            return
        if self.config.concurrencia <= 1:
            for i, msg in enumerate(pendientes):
                if self._debe_parar():
                    return
                if not self._procesar_mensaje(msg, ciclo, cb):
                    return
                if i < len(pendientes) - 1 and not self._pausa_entre_ordenes(cb):
                    return
            return

        with ThreadPoolExecutor(max_workers=self.config.concurrencia) as pool:
            futuros = [pool.submit(self._tarea, msg, ciclo, cb) for msg in pendientes]
            for f in futuros:
                f.result()  # `_tarea` no lanza: ver más abajo

    def _tarea(self, msg: JobMessage, ciclo: int, cb: Callbacks) -> None:
        """Una orden dentro del pool. Nunca deja escapar una excepción (X6): relanzada
        al consumir el resultado del pool, mataba el hilo del motor con el lote a medias."""
        if self._debe_parar():
            return
        try:
            if not self._procesar_mensaje(msg, ciclo, cb) and self.stop_reason is None:
                self.marcar_parada(StopReason.STOPPED)
        except Exception as error:
            logger.exception("Error inesperado en la orden %s", msg.id)
            self.marcar_fallo(msg, f"Error inesperado: {error}", cb, "unexpected")
            cb.emit("error", f"Error inesperado: {error}")
            self.marcar_parada(StopReason.FATAL)

    def _pausa_entre_ordenes(self, cb: Callbacks) -> bool:
        d = self.config.pausa_entre_mensajes_sec
        if d <= 0:
            return True
        cb.emit("estado", f"Pausa de cortesía ({d}s)...", False)
        return self.esperar(d, cb)

    # ------------------------------------------------------------------
    # Una orden
    # ------------------------------------------------------------------

    def _procesar_mensaje(self, msg: JobMessage, ciclo: int, cb: Callbacks) -> bool:
        """Procesa UNA orden hasta terminarla. False si hay que abortar todo el lote.

        Los 429 transitorios reintentan sin límite (esperando lo que diga la API)
        y NO suman a `msg.errores`; solo los errores reales de servidor/red cuentan
        hacia `max_error_attempts`.
        """
        total = self.job.total_count()
        cont = Contadores()
        while True:
            if self._debe_parar() or not self._throttle_preventivo(cb):
                return False
            coste_max = self.estimar_coste_maximo(msg)
            if not self._reservar(coste_max, cb):
                return False

            cb.emit("inicio_orden", msg)
            cb.emit(
                "estado",
                f"[Ciclo {ciclo} | Orden {msg.id}/{total}] Enviando (fallos: {msg.errores})",
                True,
            )
            try:
                respuesta, ms = self._llamar_api(msg, cb)
            except Exception as error:
                self._liberar(coste_max)
                decision = self.manejar_error(error, cb, cont, msg)
                if decision is Decision.PARAR:
                    return False
                if decision is Decision.SIGUIENTE:
                    return True
                continue
            return self.registrar_respuesta(msg, respuesta, ciclo, cb, ms, reserva=coste_max)

    def _llamar_api(self, msg: JobMessage, cb: Callbacks) -> tuple[Any, int]:
        """Una llamada a la API. Devuelve (respuesta, latencia en ms)."""
        params = build_request_params(self.config, msg.text)
        extra: dict[str, Any] = {}
        # `output_config` no existe como argumento con nombre en el SDK anterior a
        # 0.7x (daba TypeError); `extra_body` existe en todas y se fusiona en el JSON.
        if "output_config" in params:
            extra["extra_body"] = {"output_config": params.pop("output_config")}

        inicio = time.monotonic()
        if self.config.usa_streaming:
            with self.client.messages.stream(**params, **extra) as stream:
                observar_seguro(self.rate_limits, stream)
                for trozo in stream.text_stream:
                    cb.emit("delta", msg.id, trozo)
                respuesta = stream.get_final_message()
        else:
            # Respuesta cruda: las cabeceras de cuota se leen en TODAS las respuestas,
            # no solo al recibir un 429, para frenar antes de chocar con el límite.
            cruda = self.client.messages.with_raw_response.create(**params, **extra)
            observar_seguro(self.rate_limits, cruda)
            respuesta = cruda.parse()
        return respuesta, int((time.monotonic() - inicio) * 1000)

    def _throttle_preventivo(self, cb: Callbacks) -> bool:
        """Frena si la cuota va justa. False si hay que salir durante la espera."""
        espera = self.rate_limits.espera_recomendada()
        if espera <= 0:
            return True
        cb.emit("espera", "cuota", espera)
        cb.emit(
            "estado",
            f"Cuota casi agotada ({self.rate_limits.resumen()}). Frenando {espera:.1f}s antes de seguir.",
            False,
        )
        return self.esperar(espera, cb)
