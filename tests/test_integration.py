"""Integración del motor secuencial con un cliente falso.

Recorre el bucle completo (reintentos, backoff, tope de gasto, parada) sin tocar
la red. Aquí es donde se fija la corrección C5: `APITimeoutError` HEREDA de
`APIConnectionError`, así que en la v11 un timeout normal caía en la rama de
"parada dura" y abortaba el lote entero.
"""

from decimal import Decimal

import anthropic

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core.base import Callbacks, StopReason
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.job import STATUS_COMPLETED, STATUS_FAILED, Job

from ._http import httpx
from .test_engine import FakeResponse


def test_apitimeouterror_es_subclase_de_apiconnectionerror():
    """La premisa del bug C5, fijada como test para que no se olvide."""
    assert issubclass(anthropic.APITimeoutError, anthropic.APIConnectionError)


# ----------------------------------------------------------------------
# Dobles
# ----------------------------------------------------------------------


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _http_response(status: int) -> httpx.Response:
    return httpx.Response(status_code=status, request=_request())


def timeout_error() -> Exception:
    return anthropic.APITimeoutError(request=_request())


def connection_error() -> Exception:
    return anthropic.APIConnectionError(message="sin red", request=_request())


def status_error(status: int) -> Exception:
    return anthropic.APIStatusError(
        f"http {status}", response=_http_response(status), body=None
    )


def rate_limit_error() -> Exception:
    respuesta = _http_response(429)
    respuesta.headers["anthropic-ratelimit-tokens-reset"] = "0.01s"
    return anthropic.RateLimitError("rate limited", response=respuesta, body=None)


class FakeTokenCount:
    input_tokens = 50


class FakeRawResponse:
    """Lo que devuelve `with_raw_response.create`: cabeceras + `.parse()`."""

    def __init__(self, mensaje, headers):
        self._mensaje = mensaje
        self.headers = headers

    def parse(self):
        return self._mensaje


class FakeRawWrapper:
    def __init__(self, messages):
        self._messages = messages

    def create(self, **kwargs):
        # Las excepciones del guion se propagan igual que en el SDK real.
        return FakeRawResponse(self._messages.create(**kwargs), self._messages.headers)


class FakeStream:
    """Lo que devuelve `messages.stream(...)`: gestor de contexto con texto por trozos."""

    def __init__(self, respuesta, headers):
        self._respuesta = respuesta
        self.response = type("R", (), {"headers": headers})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        for bloque in self._respuesta.content:
            texto = getattr(bloque, "text", None)
            if isinstance(texto, str) and texto:
                medio = max(1, len(texto) // 2)
                yield texto[:medio]
                if texto[medio:]:
                    yield texto[medio:]

    def get_final_message(self):
        return self._respuesta


class FakeMessages:
    """Devuelve un guion de respuestas/excepciones, una por llamada."""

    def __init__(self, guion, headers=None):
        self.guion = list(guion)
        self.llamadas = 0
        self.peticiones = []  # los parámetros de cada petición, para poder comprobar qué se envió
        self.headers = headers or {}
        self.with_raw_response = FakeRawWrapper(self)

    def _siguiente(self):
        self.llamadas += 1
        if not self.guion:
            return FakeResponse()
        siguiente = self.guion.pop(0)
        if isinstance(siguiente, BaseException):
            raise siguiente
        if callable(siguiente):
            raise siguiente()
        return siguiente

    def create(self, **kwargs):
        self.peticiones.append(kwargs)
        return self._siguiente()

    def stream(self, **kwargs):
        self.peticiones.append(kwargs)
        return FakeStream(self._siguiente(), self.headers)

    def count_tokens(self, **kwargs):
        return FakeTokenCount()


class FakeClient:
    def __init__(self, guion, headers=None):
        self.messages = FakeMessages(guion, headers)


def _preparar(guion, config=None, n_mensajes=3):
    config = config or Configuracion(
        modelo="claude-haiku-4-5",
        max_tokens=1024,
        tope_gasto_usd=Decimal("10"),
        precio_input_mtok=Decimal("1.0"),
        precio_output_mtok=Decimal("5.0"),
        pausa_entre_mensajes_sec=0,
        estimar_antes_de_enviar=False,
        max_error_attempts=3,
    )
    job = Job.crear([f"orden {i}" for i in range(1, n_mensajes + 1)], config)
    job.save()
    engine = SequentialEngine(config, job)
    engine._client = FakeClient(guion)
    engine.inicializar_cliente = lambda *a, **k: None  # type: ignore[method-assign]
    # Backoff instantáneo: interesa la lógica de decisión, no el reloj.
    engine.espera_error_servidor = lambda error, intentos: 0.01  # type: ignore[method-assign]
    return engine, job


# ----------------------------------------------------------------------


class TestCaminoFeliz:
    def test_todas_las_ordenes_se_completan(self):
        engine, job = _preparar([FakeResponse(f"respuesta {i}") for i in range(3)])
        assert engine.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.completed_count() == 3
        assert not job.pendientes()

    def test_se_escribe_un_fichero_por_orden(self):
        engine, job = _preparar([FakeResponse("hola")] * 3)
        engine.ejecutar(Callbacks())
        for msg in job.messages:
            assert job.response_path(msg.id, 1).exists()

    def test_se_genera_el_resumen_del_ciclo(self):
        engine, job = _preparar([FakeResponse()] * 3)
        engine.ejecutar(Callbacks())
        resumen = job.file_path.with_name(f"job_{job.job_id}_ciclo_1_resumen.md")
        assert resumen.exists()
        assert "Completados: 3/3" in resumen.read_text(encoding="utf-8")

    def test_el_resumen_se_genera_tambien_al_abortar(self):
        # M12: la v11 salía por `break` antes de escribirlo, justo en el caso en
        # el que más falta hace saber qué se hizo y qué se pagó.
        engine, job = _preparar([FakeResponse(), connection_error(), FakeResponse()])
        assert engine.ejecutar(Callbacks()) == StopReason.NETWORK

        resumen = job.file_path.with_name(f"job_{job.job_id}_ciclo_1_resumen.md")
        assert resumen.exists()
        contenido = resumen.read_text(encoding="utf-8")
        assert "Completados: 1/3" in contenido
        assert "failed" in contenido


class TestReintentos:
    def test_un_timeout_se_reintenta_y_no_mata_el_lote(self):
        # C5: en la v11 esto abortaba las 3 órdenes.
        engine, job = _preparar(
            [timeout_error(), FakeResponse("ok tras el timeout")] + [FakeResponse()] * 2
        )
        assert engine.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.completed_count() == 3
        assert job.messages[0].errores == 1

    def test_un_429_se_reintenta_y_no_cuenta_como_fallo(self):
        engine, job = _preparar([rate_limit_error(), FakeResponse()] + [FakeResponse()] * 2)
        assert engine.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.messages[0].errores == 0  # los 429 NO consumen intentos

    def test_un_500_se_reintenta_con_backoff(self):
        engine, job = _preparar([status_error(500), FakeResponse()] + [FakeResponse()] * 2)
        assert engine.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.messages[0].errores == 1

    def test_un_529_overloaded_tambien_se_reintenta(self):
        engine, job = _preparar([status_error(529), FakeResponse()] + [FakeResponse()] * 2)
        assert engine.ejecutar(Callbacks()) == StopReason.COMPLETED

    def test_se_rinde_al_alcanzar_el_maximo_de_errores(self):
        engine, job = _preparar([status_error(500)] * 10)
        assert engine.ejecutar(Callbacks()) == StopReason.FATAL
        assert job.messages[0].status == STATUS_FAILED


class TestParadasDuras:
    def test_un_fallo_de_conexion_para_el_lote(self):
        engine, job = _preparar([connection_error()] + [FakeResponse()] * 2)
        assert engine.ejecutar(Callbacks()) == StopReason.NETWORK
        assert job.completed_count() == 0

    def test_un_402_para_el_lote_por_falta_de_saldo(self):
        engine, job = _preparar([FakeResponse(), status_error(402), FakeResponse()])
        assert engine.ejecutar(Callbacks()) == StopReason.NO_CREDIT
        # La primera, que sí se pagó, queda guardada.
        assert job.messages[0].status == STATUS_COMPLETED

    def test_un_401_es_fatal(self):
        engine, job = _preparar([status_error(401)] * 3)
        assert engine.ejecutar(Callbacks()) == StopReason.FATAL


class TestTopeDeGasto:
    def test_para_al_alcanzar_el_tope_conservando_lo_pagado(self):
        config = Configuracion(
            modelo="claude-haiku-4-5",
            max_tokens=1024,
            # Una respuesta = 1000 tok in ($0.001) + 500 tok out ($0.0025)
            tope_gasto_usd=Decimal("0.005"),
            precio_input_mtok=Decimal("1.0"),
            precio_output_mtok=Decimal("5.0"),
            pausa_entre_mensajes_sec=0,
            estimar_antes_de_enviar=False,
        )
        engine, job = _preparar([FakeResponse()] * 5, config, n_mensajes=5)

        assert engine.ejecutar(Callbacks()) == StopReason.BUDGET
        completadas = job.completed_count()
        assert 0 < completadas < 5
        # C1: todas las respuestas pagadas están en disco.
        for msg in job.messages[:completadas]:
            assert job.response_path(msg.id, 1).exists()

    def test_la_estimacion_previa_impide_pasarse(self):
        config = Configuracion(
            modelo="claude-haiku-4-5",
            max_tokens=1_000_000,  # una sola respuesta puede costar $5
            tope_gasto_usd=Decimal("0.01"),
            precio_input_mtok=Decimal("1.0"),
            precio_output_mtok=Decimal("5.0"),
            pausa_entre_mensajes_sec=0,
            estimar_antes_de_enviar=True,  # C2
        )
        engine, job = _preparar([FakeResponse()] * 3, config)

        assert engine.ejecutar(Callbacks()) == StopReason.BUDGET
        # Ni siquiera se llegó a enviar la primera.
        assert engine._client.messages.llamadas == 0
        assert job.completed_count() == 0


class TestReanudacion:
    def test_solo_se_reenvian_los_pendientes(self):
        engine, job = _preparar([FakeResponse()] * 3)
        engine.ejecutar(Callbacks())

        # Segunda pasada sobre el mismo job ya completo.
        engine2 = SequentialEngine(engine.config, job)
        engine2._client = FakeClient([])
        engine2.inicializar_cliente = lambda *a, **k: None  # type: ignore[method-assign]
        engine2.ejecutar(Callbacks())
        assert engine2._client.messages.llamadas == 0


class TestCallbacksDelMotor:
    def test_se_emite_un_resultado_por_orden(self):
        recibidos = []
        engine, job = _preparar([FakeResponse()] * 3)
        engine.ejecutar(Callbacks(resultado=recibidos.append))
        assert len(recibidos) == 3
        assert [r.id for r in recibidos] == [1, 2, 3]
        assert all(r.status == STATUS_COMPLETED for r in recibidos)

    def test_el_gasto_se_reporta_creciente(self):
        gastos = []
        engine, job = _preparar([FakeResponse()] * 3)
        engine.ejecutar(Callbacks(gasto=lambda usd, usage: gastos.append(usd)))
        assert len(gastos) == 3
        assert gastos == sorted(gastos)


class TestConcurrencia:
    def test_con_varios_hilos_se_procesan_todas(self):
        config = Configuracion(
            modelo="claude-haiku-4-5",
            max_tokens=1024,
            concurrencia=4,
            tope_gasto_usd=Decimal("100"),
            precio_input_mtok=Decimal("1.0"),
            precio_output_mtok=Decimal("5.0"),
            estimar_antes_de_enviar=False,
        )
        engine, job = _preparar([FakeResponse()] * 8, config, n_mensajes=8)
        engine.ejecutar(Callbacks())
        assert job.completed_count() == 8
