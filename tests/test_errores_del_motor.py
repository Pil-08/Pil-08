"""Cómo reacciona el motor inmediato a cada tipo de error.

Aquí se fijan los dos fallos que se encontraron contrastando el motor con la
documentación de la API (tope mensual y límite propio) y el resto de rutas de error.
"""

from decimal import Decimal as D

import anthropic

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core.base import MAX_429_SIN_INFO, Callbacks, StopReason
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REFUSED,
    Job,
)

from ._http import httpx
from .test_engine import FakeResponse
from .test_errors import TOPE_MENSUAL, error, interno
from .test_integration import FakeClient, connection_error


def preparar(guion, n=3, **kw):
    base = dict(
        modelo="claude-haiku-4-5", max_tokens=1024, tope_gasto_usd=D("10"),
        precio_input_mtok=D("1"), precio_output_mtok=D("5"),
        estimar_antes_de_enviar=False, max_error_attempts=3,
    )
    base.update(kw)
    config = Configuracion(**base)
    job = Job.crear([f"orden {i}" for i in range(1, n + 1)], config)
    job.save()
    motor = SequentialEngine(config, job)
    motor.notificaciones = False
    motor._client = FakeClient(guion)
    motor.inicializar_cliente = lambda *a, **k: None
    motor.espera_error_servidor = lambda e, i: 0.01
    # Las esperas no deben costar tiempo real en los tests.
    motor.esperas = []
    def esperar(segundos, cb):
        motor.esperas.append(segundos)
        return not motor.evento_detener.is_set()
    motor.esperar = esperar
    return motor, job


def llamadas(motor):
    return motor._client.messages.llamadas


class TestTopeMensualDelTier:
    """429 SIN retry-after hasta el día 1: reintentar es inútil."""

    def test_para_enseguida_en_vez_de_reintentar_durante_semanas(self):
        motor, job = preparar([FakeResponse(), error(429, interno(TOPE_MENSUAL)), FakeResponse()])
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.ACCOUNT_LIMIT
        assert llamadas(motor) == 2           # NO siguió reintentando
        assert motor.esperas == []            # ni esperó un solo segundo
        assert "2026-09-01 00:00 UTC" in errores[0] and "tope mensual" in errores[0]

    def test_lo_ya_pagado_queda_guardado_y_la_orden_queda_pendiente(self):
        motor, job = preparar([FakeResponse(), error(429, interno(TOPE_MENSUAL))])
        motor.ejecutar(Callbacks())
        assert job.messages[0].status == STATUS_COMPLETED
        # La orden no tiene la culpa: pendiente, no fallida, para reanudar sin más.
        assert job.messages[1].status == STATUS_PENDING and job.messages[1].errores == 0

    def test_el_lote_se_puede_reanudar_cuando_vuelva_el_acceso(self):
        motor, job = preparar([error(429, interno(TOPE_MENSUAL))], n=2)
        motor.ejecutar(Callbacks())
        motor2 = SequentialEngine(motor.config, job)
        motor2.notificaciones = False
        motor2._client = FakeClient([FakeResponse(), FakeResponse()])
        motor2.inicializar_cliente = lambda *a, **k: None
        assert motor2.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.completed_count() == 2

    def test_el_resumen_del_ciclo_se_escribe_igualmente(self):
        motor, job = preparar([error(429, interno(TOPE_MENSUAL))])
        motor.ejecutar(Callbacks())
        assert job.resumen_path(1).exists()


class TestLimiteFijadoPorElUsuario:
    CUERPO = {
        "type": "invalid_request_error",
        "message": "You have reached your specified API usage limits. You will regain access on 2026-10-01 at 00:00 UTC.",
    }

    def test_no_se_toma_por_peticion_invalida(self):
        motor, job = preparar([FakeResponse(), error(400, self.CUERPO)])
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.ACCOUNT_LIMIT
        assert "que fijaste" in errores[0] and "Spend limits" in errores[0]

    def test_la_orden_no_se_marca_como_fallida(self):
        motor, job = preparar([error(400, self.CUERPO)])
        motor.ejecutar(Callbacks())
        assert job.messages[0].status == STATUS_PENDING and not job.messages[0].error_type


class TestRateLimitSinInformacion:
    def test_429_repetidos_sin_ninguna_pista_acaban_parando(self):
        # Red de seguridad: si no se reconoce un tope de gasto por su texto, al menos no
        # se espera para siempre.
        cuerpo = {"type": "rate_limit_error", "message": "limit"}
        motor, job = preparar([error(429, cuerpo)] * 50)
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.ACCOUNT_LIMIT
        assert llamadas(motor) == MAX_429_SIN_INFO + 1
        assert "sin indicar cuándo" in errores[0]

    def test_con_informacion_si_espera_indefinidamente(self):
        # Un 429 con retry-after es transitorio de verdad: no se pone límite.
        def con_retry_after():
            resp = httpx.Response(429, request=httpx.Request("POST", "https://x"), headers={"retry-after": "1"})
            return anthropic.RateLimitError("rl", response=resp, body=None)

        guion = [con_retry_after() for _ in range(MAX_429_SIN_INFO + 5)] + [FakeResponse()]
        motor, job = preparar(guion, n=1)
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert motor.esperas[0] == 3.0  # 1 + 2 de margen
        assert job.messages[0].errores == 0

    def test_emite_el_evento_estructurado_de_espera(self):
        resp = httpx.Response(429, request=httpx.Request("POST", "https://x"), headers={"retry-after": "7"})
        motor, _ = preparar([anthropic.RateLimitError("rl", response=resp, body=None), FakeResponse()], n=1)
        eventos = []
        motor.ejecutar(Callbacks(espera=lambda motivo, s: eventos.append((motivo, s))))
        assert eventos == [("rate_limit", 9.0)]


class TestSinSaldo:
    def test_400_credit_balance_too_low(self):
        cuerpo = {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API."}
        motor, job = preparar([FakeResponse(), error(400, cuerpo)])
        assert motor.ejecutar(Callbacks()) == StopReason.NO_CREDIT
        assert job.messages[1].status == STATUS_PENDING

    def test_402_deja_la_orden_pendiente(self):
        motor, job = preparar([error(402)])
        motor.ejecutar(Callbacks())
        assert job.messages[0].status == STATUS_PENDING


class TestOtrosErroresHttp:
    def test_modelo_que_ya_no_existe(self):
        cuerpo = {"type": "not_found_error", "message": "model: claude-viejo"}
        motor, job = preparar([error(404, cuerpo)])
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.FATAL
        assert job.messages[0].error_type == "model_not_found"
        assert "Cargar modelos" in errores[0]

    def test_clave_invalida(self):
        motor, job = preparar([error(401, {"type": "authentication_error", "message": "invalid x-api-key"})])
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.FATAL
        assert job.messages[0].error_type == "auth" and "API key" in errores[0]

    def test_peticion_invalida(self):
        motor, job = preparar([error(400, {"type": "invalid_request_error", "message": "messages: too long"})])
        assert motor.ejecutar(Callbacks()) == StopReason.FATAL
        assert job.messages[0].error_type == "bad_request"
        assert "messages: too long" in job.messages[0].detalle

    def test_orden_demasiado_grande_no_para_el_resto(self):
        # 413 es culpa de ESA orden: se marca y se sigue con las demás.
        motor, job = preparar([error(413, {"type": "request_too_large", "message": "big"}), FakeResponse(), FakeResponse()])
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED or job.completed_count() == 2
        assert job.messages[0].status == STATUS_FAILED and job.messages[0].error_type == "too_large"
        assert job.completed_count() == 2

    def test_error_de_servidor_en_mitad_del_stream_se_reintenta(self):
        cuerpo = {"type": "overloaded_error", "message": "Overloaded"}
        motor, job = preparar([error(200, cuerpo), FakeResponse()], n=1)
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.messages[0].errores == 1

    def test_excepcion_inesperada_marca_fallo_y_para(self):
        motor, job = preparar([RuntimeError("bug raro")])
        assert motor.ejecutar(Callbacks()) == StopReason.FATAL
        assert job.messages[0].status == STATUS_FAILED and job.messages[0].error_type == "unexpected"


class TestSinConexion:
    def _sin_conexion(self):
        e = anthropic.APIConnectionError(message="Connection error.", request=httpx.Request("POST", "https://x"))
        e.__cause__ = httpx.ConnectError("sin ruta")
        return e

    def test_la_peticion_no_salio_asi_que_espera_y_reintenta_sin_contar_fallo(self):
        motor, job = preparar([self._sin_conexion(), self._sin_conexion(), FakeResponse()], n=1)
        eventos = []
        assert motor.ejecutar(Callbacks(espera=lambda m, s: eventos.append(m))) == StopReason.COMPLETED
        assert job.messages[0].errores == 0
        assert eventos == ["sin_conexion", "sin_conexion"]
        assert motor.esperas == [5.0, 10.0]  # backoff creciente

    def test_se_rinde_tras_diez_minutos_sin_conexion(self):
        motor, job = preparar([self._sin_conexion()] * 100, n=1)
        assert motor.ejecutar(Callbacks()) == StopReason.NETWORK
        assert sum(motor.esperas) <= 600 + 60
        assert job.messages[0].status == STATUS_PENDING

    def test_corte_a_mitad_de_respuesta_sigue_parando(self):
        e = anthropic.APIConnectionError(message="reset", request=httpx.Request("POST", "https://x"))
        e.__cause__ = httpx.ReadError("conexión reiniciada")
        motor, job = preparar([e, FakeResponse()])
        assert motor.ejecutar(Callbacks()) == StopReason.NETWORK
        assert job.messages[0].error_type == "connection"

    def test_sin_causa_conocida_para_por_prudencia(self):
        motor, job = preparar([connection_error(), FakeResponse()])
        assert motor.ejecutar(Callbacks()) == StopReason.NETWORK


class TestRechazosEnElMotor:
    def test_un_rechazo_no_para_el_lote(self):
        rechazo = FakeResponse(content=[], stop_reason="refusal", stop_details={"category": "cyber"})
        motor, job = preparar([FakeResponse(), rechazo, FakeResponse()])
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert [m.status for m in job.messages] == [STATUS_COMPLETED, STATUS_REFUSED, STATUS_COMPLETED]
        assert job.messages[1].error_type == "refusal"


class TestFalloDentroDelPool:
    def test_una_excepcion_en_un_hilo_no_mata_el_motor(self):
        # X6: antes se relanzaba al consumir el pool y escapaba de `ejecutar`.
        motor, job = preparar([RuntimeError("boom")] + [FakeResponse()] * 5, n=6, concurrencia=3)
        assert motor.ejecutar(Callbacks()) == StopReason.FATAL
        assert any(m.error_type == "unexpected" for m in job.messages)

    def test_el_motivo_concreto_sobrevive_a_los_demas_hilos(self):
        # X5: un hilo que despierta de su espera no degrada "sin saldo" a "detenido".
        motor, job = preparar([error(402)] + [FakeResponse()] * 7, n=8, concurrencia=4)
        assert motor.ejecutar(Callbacks()) == StopReason.NO_CREDIT
