"""Clasificación de errores de la API.

Los cuerpos JSON son los de la documentación oficial:
https://platform.claude.com/docs/en/api/errors
https://platform.claude.com/docs/en/api/rate-limits#reaching-your-spend-cap
"""

import anthropic
import pytest

from claude_batch_monitor.core.errors import (
    Categoria,
    analizar_error,
    cadena_de_causas,
    es_sin_conexion,
    fecha_de_reanudacion,
)

from ._http import httpx

TOPE_MENSUAL = {
    "type": "error",
    "error": {
        "type": "rate_limit_error",
        "message": (
            "You have reached your API usage limits: your organization has crossed its monthly "
            "API usage threshold, set based on your organization's API tier. "
            "You will regain access on 2026-09-01 at 00:00 UTC."
        ),
        "details": {"error_code": "enforced_spend_limit_reached"},
    },
    "request_id": "req_018EeWyXxfu5pfWkrYcMdjWG",
}


def _req():
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def error(status, cuerpo=None, mensaje="http error"):
    resp = httpx.Response(status_code=status, request=_req())
    return anthropic.APIStatusError(mensaje, response=resp, body=cuerpo)


def interno(cuerpo):
    """El SDK entrega `body` como el objeto `error` interno."""
    return cuerpo["error"]


class TestLimiteDeCuenta:
    def test_tope_mensual_del_tier_con_el_cuerpo_interno(self):
        info = analizar_error(error(429, interno(TOPE_MENSUAL)))
        assert info.categoria is Categoria.LIMITE_CUENTA
        assert info.codigo == "enforced_spend_limit_reached"
        assert info.reanudacion == "2026-09-01 00:00 UTC"

    def test_tope_mensual_con_el_envoltorio_completo(self):
        assert analizar_error(error(429, TOPE_MENSUAL)).categoria is Categoria.LIMITE_CUENTA

    def test_se_reconoce_por_el_texto_aunque_falte_el_codigo(self):
        cuerpo = {"type": "rate_limit_error", "message": "You have reached your API usage limits. You will regain access on 2026-11-01."}
        assert analizar_error(error(429, cuerpo)).categoria is Categoria.LIMITE_CUENTA

    def test_limite_fijado_por_el_usuario_llega_como_400(self):
        cuerpo = {
            "type": "invalid_request_error",
            "message": "You have reached your specified API usage limits. You will regain access on 2026-10-01 at 00:00 UTC.",
        }
        info = analizar_error(error(400, cuerpo))
        # La orden es válida: NO es una petición inválida.
        assert info.categoria is Categoria.LIMITE_CUENTA
        assert info.reanudacion == "2026-10-01 00:00 UTC"

    def test_limite_de_workspace(self):
        cuerpo = {"type": "invalid_request_error", "message": "You have reached your specified workspace API usage limits."}
        assert analizar_error(error(400, cuerpo)).categoria is Categoria.LIMITE_CUENTA


class TestRateLimitTransitorio:
    def test_429_normal_es_rate_limit(self):
        cuerpo = {"type": "rate_limit_error", "message": "Number of request tokens has exceeded your per-minute rate limit"}
        assert analizar_error(error(429, cuerpo)).categoria is Categoria.RATE_LIMIT

    def test_429_sin_cuerpo(self):
        assert analizar_error(error(429, None)).categoria is Categoria.RATE_LIMIT

    def test_error_de_stream_con_status_200(self):
        cuerpo = {"type": "rate_limit_error", "message": "slow down"}
        assert analizar_error(error(200, cuerpo)).categoria is Categoria.RATE_LIMIT


class TestSinSaldo:
    def test_402_billing_error(self):
        cuerpo = {"type": "billing_error", "message": "There's an issue with your billing or payment information."}
        assert analizar_error(error(402, cuerpo)).categoria is Categoria.SIN_SALDO

    def test_402_sin_cuerpo(self):
        assert analizar_error(error(402, None)).categoria is Categoria.SIN_SALDO

    def test_saldo_bajo_llega_a_veces_como_400(self):
        cuerpo = {
            "type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.",
        }
        assert analizar_error(error(400, cuerpo)).categoria is Categoria.SIN_SALDO


class TestResto:
    @pytest.mark.parametrize(
        "status,cuerpo,esperada",
        [
            (401, {"type": "authentication_error", "message": "invalid x-api-key"}, Categoria.AUTH),
            (403, {"type": "permission_error", "message": "nope"}, Categoria.AUTH),
            (404, {"type": "not_found_error", "message": "model: claude-viejo"}, Categoria.MODELO),
            (404, {"type": "not_found_error", "message": "The requested resource could not be found."}, Categoria.NO_ENCONTRADO),
            (413, {"type": "request_too_large", "message": "too big"}, Categoria.DEMASIADO_GRANDE),
            (400, {"type": "invalid_request_error", "message": "max_tokens: field required"}, Categoria.PETICION),
            (422, None, Categoria.PETICION),
            (500, {"type": "api_error", "message": "oops"}, Categoria.SERVIDOR),
            (504, {"type": "timeout_error", "message": "timeout"}, Categoria.SERVIDOR),
            (529, {"type": "overloaded_error", "message": "Overloaded"}, Categoria.SERVIDOR),
            (200, {"type": "overloaded_error", "message": "Overloaded"}, Categoria.SERVIDOR),
            (408, None, Categoria.SERVIDOR),   # el propio SDK reintenta 408 y 409: son transitorios
            (409, None, Categoria.SERVIDOR),
            (418, None, Categoria.OTRO),
        ],
    )
    def test_categorias(self, status, cuerpo, esperada):
        assert analizar_error(error(status, cuerpo)).categoria is esperada

    def test_un_error_sin_status_ni_cuerpo_no_revienta(self):
        assert analizar_error(RuntimeError("raro")).categoria is Categoria.OTRO

    def test_body_basura(self):
        assert analizar_error(error(500, "no es un dict")).categoria is Categoria.SERVIDOR
        assert analizar_error(error(429, {"error": "texto", "details": 3})).categoria is Categoria.RATE_LIMIT


class TestFechaDeReanudacion:
    def test_con_hora(self):
        assert fecha_de_reanudacion("You will regain access on 2026-09-01 at 00:00 UTC.") == "2026-09-01 00:00 UTC"

    def test_sin_hora(self):
        assert fecha_de_reanudacion("regain access on 2026-11-01.") == "2026-11-01"

    def test_sin_fecha(self):
        assert fecha_de_reanudacion("nada que ver") == ""
        assert fecha_de_reanudacion("") == ""


class TestSinConexion:
    def _conexion_con_causa(self, causa):
        e = anthropic.APIConnectionError(message="Connection error.", request=_req())
        e.__cause__ = causa
        return e

    def test_connect_error_es_sin_conexion(self):
        # DNS caído, red apagada, conexión rechazada: la petición no salió.
        assert es_sin_conexion(self._conexion_con_causa(httpx.ConnectError("no route")))

    def test_corte_a_mitad_de_respuesta_no_lo_es(self):
        # Aquí el servidor pudo llegar a procesar: reintentar podría duplicar el cobro.
        assert not es_sin_conexion(self._conexion_con_causa(httpx.ReadError("reset")))

    def test_sin_causa_no_lo_es(self):
        assert not es_sin_conexion(anthropic.APIConnectionError(message="x", request=_req()))

    def test_la_causa_puede_estar_anidada(self):
        interna = httpx.ConnectError("dns")
        intermedia = OSError("fallo")
        intermedia.__cause__ = interna
        assert es_sin_conexion(self._conexion_con_causa(intermedia))

    def test_los_ciclos_en_la_cadena_de_causas_no_cuelgan(self):
        a, b = RuntimeError("a"), RuntimeError("b")
        a.__cause__, b.__cause__ = b, a
        assert len(cadena_de_causas(a)) == 2
