"""El motor contra el SDK de Anthropic REAL, con un servidor simulado por debajo.

Los dobles de los otros tests prueban nuestra lógica; estos prueban nuestras
SUPOSICIONES sobre el SDK: la forma de `error.body`, la cadena de causas de un
fallo de conexión, las cabeceras de la respuesta cruda, el streaming, los lotes…
Se inyecta un transporte falso en el cliente HTTP, así que no hay red y el código
que se ejecuta es el del SDK instalado (0.x o 1.x).
"""

import json
from decimal import Decimal as D

import anthropic
import pytest

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core.base import Callbacks, StopReason
from claude_batch_monitor.core.batch import BatchEngine
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.job import STATUS_COMPLETED, STATUS_PENDING, Job

from ._http import httpx
from .test_errors import TOPE_MENSUAL


def mensaje_json(texto="Hola", entrada=12, salida=7, stop="end_turn", extra_usage=None):
    usage = {"input_tokens": entrada, "output_tokens": salida}
    usage.update(extra_usage or {})
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": texto}] if texto is not None else [],
        "stop_reason": stop, "stop_sequence": None, "usage": usage,
    }


def sse(*eventos):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in eventos).encode()


def flujo(trozos, entrada=10, salida=5):
    ev = [
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": entrada, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ]
    ev += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}} for t in trozos]
    ev += [
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": salida}},
        {"type": "message_stop"},
    ]
    return sse(*ev)


class Servidor:
    """Transporte simulado. `guion` es una lista de respuestas o excepciones, una por petición."""

    def __init__(self, guion):
        self.guion = list(guion)
        self.peticiones = []

    def __call__(self, request):
        cuerpo = request.content.decode() if request.content else ""
        self.peticiones.append((request.method, request.url.path, json.loads(cuerpo) if cuerpo else None, dict(request.headers)))
        siguiente = self.guion.pop(0) if len(self.guion) > 1 else self.guion[0]
        if isinstance(siguiente, BaseException):
            raise siguiente
        return siguiente


def respuesta(status=200, cuerpo=None, headers=None, contenido=None, tipo="application/json"):
    h = {"content-type": tipo}
    h.update(headers or {})
    if contenido is None:
        contenido = json.dumps(cuerpo).encode() if cuerpo is not None else b"{}"
    return httpx.Response(status, content=contenido, headers=h)


def cliente(servidor):
    return anthropic.Anthropic(
        api_key="sk-ant-test", max_retries=0, timeout=5,
        http_client=httpx.Client(transport=httpx.MockTransport(servidor)),
    )


def motor_real(servidor, n=1, motor=SequentialEngine, **kw):
    base = dict(modelo="claude-haiku-4-5", max_tokens=256, tope_gasto_usd=D("10"),
                estimar_antes_de_enviar=False, max_error_attempts=3)
    base.update(kw)
    config = Configuracion(**base)
    job = Job.crear([f"pregunta {i}" for i in range(1, n + 1)], config)
    job.save()
    m = motor(config, job)
    m.notificaciones = False
    m._client = cliente(servidor)
    m.inicializar_cliente = lambda *a, **k: None
    m.espera_error_servidor = lambda e, i: 0.01
    m.esperas = []
    def esperar(s, cb):
        m.esperas.append(s)
        return not m.evento_detener.is_set()
    m.esperar = esperar
    return m, job


class TestPeticionesNormales:
    def test_una_respuesta_real_del_sdk_se_registra(self):
        s = Servidor([respuesta(cuerpo=mensaje_json("Hola mundo"))])
        m, job = motor_real(s)
        assert m.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert "Hola mundo" in job.response_path(1, 1).read_text(encoding="utf-8")
        assert job.messages[0].tokens_input == 12 and job.messages[0].tokens_output == 7

    def test_la_peticion_lleva_lo_que_esperamos(self):
        s = Servidor([respuesta(cuerpo=mensaje_json())])
        m, _ = motor_real(s, system_prompt="Sé breve.", modelo="claude-sonnet-5-5", esfuerzo="medium")
        m.ejecutar(Callbacks())
        metodo, ruta, cuerpo, cabeceras = s.peticiones[0]
        assert (metodo, ruta) == ("POST", "/v1/messages")
        assert cuerpo["model"] == "claude-sonnet-5-5" and cuerpo["max_tokens"] == 256
        assert cuerpo["messages"] == [{"role": "user", "content": "pregunta 1"}]
        assert cuerpo["system"][0]["cache_control"] == {"type": "ephemeral"}
        # X1: `output_config` llega al JSON aunque el SDK no lo tenga como argumento.
        assert cuerpo["output_config"] == {"effort": "medium"}
        assert cabeceras["x-api-key"] == "sk-ant-test"

    def test_las_cabeceras_de_cuota_de_una_respuesta_correcta_se_leen(self):
        # Z1: no hace falta esperar a un 429 para saber cuánto cupo queda.
        h = {
            "anthropic-ratelimit-requests-limit": "1000",
            "anthropic-ratelimit-requests-remaining": "7",
            "anthropic-ratelimit-requests-reset": "40s",
        }
        s = Servidor([respuesta(cuerpo=mensaje_json(), headers=h)])
        m, _ = motor_real(s)
        m.ejecutar(Callbacks())
        assert m.rate_limits.hay_datos and "requests 7/1000" in m.rate_limits.resumen()

    def test_el_uso_de_cache_se_cobra_bien(self):
        uso = {"cache_creation_input_tokens": 1000, "cache_read_input_tokens": 2000,
               "cache_creation": {"ephemeral_5m_input_tokens": 800, "ephemeral_1h_input_tokens": 200}}
        s = Servidor([respuesta(cuerpo=mensaje_json(entrada=100, salida=50, extra_usage=uso))])
        m, job = motor_real(s)
        m.ejecutar(Callbacks())
        # Haiku 4.5 (1 / 5): 100 + 800x1.25 + 200x2 + 2000x0.1 + 50x5 = 1950 -> $0.00195
        assert job.messages[0].coste_usd == D("0.00195")

    def test_rechazo_del_modelo_con_http_200(self):
        cuerpo = mensaje_json(texto=None, stop="refusal")
        cuerpo["stop_details"] = {"type": "safety_violation", "category": "cyber"}
        m, job = motor_real(Servidor([respuesta(cuerpo=cuerpo)]))
        assert m.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.messages[0].status == "refused" and "cyber" in job.messages[0].detalle


class TestErroresRealesDelSdk:
    def test_tope_mensual_con_el_cuerpo_real(self):
        s = Servidor([respuesta(429, TOPE_MENSUAL)])
        m, job = motor_real(s)
        errores = []
        assert m.ejecutar(Callbacks(error=errores.append)) == StopReason.ACCOUNT_LIMIT
        assert len(s.peticiones) == 1 and m.esperas == []
        assert "2026-09-01 00:00 UTC" in errores[0]
        assert job.messages[0].status == STATUS_PENDING

    def test_limite_propio_de_la_consola(self):
        cuerpo = {"type": "error", "error": {"type": "invalid_request_error",
                  "message": "You have reached your specified API usage limits. You will regain access on 2026-10-01 at 00:00 UTC."}}
        m, job = motor_real(Servidor([respuesta(400, cuerpo)]))
        assert m.ejecutar(Callbacks()) == StopReason.ACCOUNT_LIMIT
        assert job.messages[0].status == STATUS_PENDING

    def test_429_con_retry_after_espera_exactamente_eso(self):
        cuerpo = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
        s = Servidor([respuesta(429, cuerpo, headers={"retry-after": "12"}), respuesta(cuerpo=mensaje_json())])
        m, job = motor_real(s)
        assert m.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert m.esperas == [14.0]  # 12 + 2 de margen
        assert job.messages[0].errores == 0

    def test_529_overloaded_se_reintenta(self):
        cuerpo = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        s = Servidor([respuesta(529, cuerpo), respuesta(cuerpo=mensaje_json())])
        m, job = motor_real(s)
        assert m.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.messages[0].errores == 1

    def test_402_sin_saldo(self):
        cuerpo = {"type": "error", "error": {"type": "billing_error", "message": "billing"}}
        m, _ = motor_real(Servidor([respuesta(402, cuerpo)]))
        assert m.ejecutar(Callbacks()) == StopReason.NO_CREDIT

    def test_401_clave_invalida(self):
        cuerpo = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        m, job = motor_real(Servidor([respuesta(401, cuerpo)]))
        errores = []
        assert m.ejecutar(Callbacks(error=errores.append)) == StopReason.FATAL
        assert "API key" in errores[0]

    def test_404_modelo_inexistente(self):
        cuerpo = {"type": "error", "error": {"type": "not_found_error", "message": "model: claude-viejo"}}
        m, job = motor_real(Servidor([respuesta(404, cuerpo)]))
        assert m.ejecutar(Callbacks()) == StopReason.FATAL
        assert job.messages[0].error_type == "model_not_found"

    def test_timeout_real_se_reintenta_y_no_mata_el_lote(self):
        s = Servidor([httpx.ReadTimeout("lento"), respuesta(cuerpo=mensaje_json())])
        m, job = motor_real(s)
        assert m.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.messages[0].errores == 1

    def test_sin_conexion_real_espera_y_reintenta(self):
        # Valida la suposición clave: el SDK encadena el ConnectError como __cause__.
        s = Servidor([httpx.ConnectError("Name or service not known"), respuesta(cuerpo=mensaje_json())])
        m, job = motor_real(s)
        assert m.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert m.esperas == [5.0] and job.messages[0].errores == 0

    def test_corte_de_conexion_a_mitad_de_respuesta_sigue_parando(self):
        s = Servidor([httpx.ReadError("conexión reiniciada"), respuesta(cuerpo=mensaje_json())])
        m, job = motor_real(s)
        assert m.ejecutar(Callbacks()) == StopReason.NETWORK
        assert len(s.peticiones) == 1


class TestStreaming:
    def test_el_texto_llega_por_trozos_y_se_registra_completo(self):
        s = Servidor([respuesta(contenido=flujo(["Hola", " a", " todos"]), tipo="text/event-stream",
                                headers={"anthropic-ratelimit-requests-limit": "50",
                                         "anthropic-ratelimit-requests-remaining": "49",
                                         "anthropic-ratelimit-requests-reset": "5s"})])
        m, job = motor_real(s, streaming=True)
        trozos = []
        assert m.ejecutar(Callbacks(delta=lambda i, t: trozos.append((i, t)))) == StopReason.COMPLETED
        assert trozos == [(1, "Hola"), (1, " a"), (1, " todos")]
        assert "Hola a todos" in job.response_path(1, 1).read_text(encoding="utf-8")
        assert job.messages[0].tokens_input == 10 and job.messages[0].tokens_output == 5
        assert s.peticiones[0][2]["stream"] is True
        assert m.rate_limits.hay_datos  # las cabeceras también se leen en streaming

    def test_mas_de_16000_tokens_activa_el_streaming_solo(self):
        s = Servidor([respuesta(contenido=flujo(["ok"]), tipo="text/event-stream")])
        m, _ = motor_real(s, max_tokens=20_000)
        m.ejecutar(Callbacks())
        assert s.peticiones[0][2]["stream"] is True

    def test_error_en_mitad_del_stream_se_trata_como_error_de_servidor(self):
        ev = sse(
            {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                                  "model": "x", "content": [], "stop_reason": None,
                                                  "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}},
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        )
        s = Servidor([respuesta(contenido=ev, tipo="text/event-stream"),
                      respuesta(contenido=flujo(["ya"]), tipo="text/event-stream")])
        m, job = motor_real(s, streaming=True)
        assert m.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.messages[0].errores == 1


class TestLoteReal:
    def _jsonl(self, filas):
        return "\n".join(json.dumps(f) for f in filas).encode()

    def test_flujo_completo_con_resultados_desordenados(self):
        lote = {"id": "msgbatch_1", "type": "message_batch", "processing_status": "in_progress",
                "request_counts": {"processing": 3, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
                "created_at": "2026-10-04T10:00:00Z", "expires_at": "2026-10-05T10:00:00Z",
                "ended_at": None, "cancel_initiated_at": None, "archived_at": None,
                "results_url": None}
        terminado = dict(lote, processing_status="ended", results_url="https://api.anthropic.com/v1/messages/batches/msgbatch_1/results",
                         request_counts={"processing": 0, "succeeded": 2, "errored": 1, "canceled": 0, "expired": 0})
        def ok(i, texto):
            return {"custom_id": f"m00000{i}", "result": {"type": "succeeded", "message": mensaje_json(texto)}}
        malo = {"custom_id": "m000002", "result": {"type": "errored", "error": {
            "type": "error", "error": {"type": "invalid_request_error", "message": "prompt demasiado largo"}}}}
        s = Servidor([
            respuesta(200, lote),                    # create
            respuesta(200, lote),                    # retrieve (en curso)
            respuesta(200, terminado),               # retrieve (terminado)
            respuesta(200, terminado),               # el propio SDK hace otro retrieve dentro de results()
            respuesta(contenido=self._jsonl([ok(3, "tercera"), malo, ok(1, "primera")]), tipo="application/x-jsonl"),
        ])
        m, job = motor_real(s, n=3, motor=BatchEngine, modo="lote")
        m.intervalo_min = m.intervalo_max = 0.01
        assert m.ejecutar(Callbacks()) == StopReason.COMPLETED

        metodo, ruta, cuerpo, _ = s.peticiones[0]
        assert (metodo, ruta) == ("POST", "/v1/messages/batches")
        assert [r["custom_id"] for r in cuerpo["requests"]] == ["m000001", "m000002", "m000003"]
        assert job.messages[0].status == STATUS_COMPLETED
        assert "primera" in job.response_path(1, 1).read_text(encoding="utf-8")
        assert "tercera" in job.response_path(3, 1).read_text(encoding="utf-8")
        assert job.messages[1].status == "failed" and job.messages[1].error_type == "bad_request"
        assert "prompt demasiado largo" in job.messages[1].detalle
        assert job.batch_id == ""


class TestListaDeModelos:
    def test_el_sdk_pagina_y_la_lista_completa_llega(self):
        # La v11 usaba `.data`: solo la primera página (20 modelos por defecto).
        def pagina(ids, mas, ultimo):
            return {"data": [{"id": i, "type": "model", "display_name": i, "created_at": "2026-01-01T00:00:00Z"} for i in ids],
                    "has_more": mas, "first_id": ids[0], "last_id": ultimo}
        s = Servidor([respuesta(cuerpo=pagina(["m1", "m2"], True, "m2")), respuesta(cuerpo=pagina(["m3"], False, "m3"))])
        ids = [mod.id for mod in cliente(s).models.list(limit=2)]
        assert ids == ["m1", "m2", "m3"]


@pytest.mark.parametrize("version_minima", ["0.69"])
def test_version_del_sdk_soportada(version_minima):
    from importlib.metadata import version
    mayor = tuple(int(x) for x in version("anthropic").split(".")[:2])
    minimo = tuple(int(x) for x in version_minima.split("."))
    assert mayor >= minimo
