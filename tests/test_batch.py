"""Motor de lote (Message Batches API) con un cliente falso.

El cliente falso devuelve los resultados DESORDENADOS, como la API real: la
documentación avisa de que no hay garantía de orden y que hay que emparejar por
`custom_id`.
"""

import random
import re
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS

import anthropic

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core import batch as modulo_batch
from claude_batch_monitor.core.base import Callbacks, StopReason
from claude_batch_monitor.core.batch import BatchEngine, custom_id
from claude_batch_monitor.job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    Job,
)

from ._http import httpx
from .test_engine import FakeResponse
from .test_errors import TOPE_MENSUAL, error, interno


def exito(texto="ok", **kw):
    return NS(type="succeeded", message=FakeResponse(texto, **kw))


def errored(tipo="invalid_request_error", mensaje="malo"):
    return NS(type="errored", error=NS(type="error", error=NS(type=tipo, message=mensaje)))


EXPIRADO = NS(type="expired")
CANCELADO = NS(type="canceled")


class FakeBatches:
    def __init__(self, resultados, estados=("ended",), desordenar=True, falla_en=None):
        self.resultados = resultados          # {custom_id: resultado}
        self.estados = list(estados)
        self.desordenar = desordenar
        self.falla_en = falla_en              # lanzar este error tras N resultados: (n, excepción)
        self.creados, self.cancelados, self.consultas = [], [], 0
        self.errores_al_crear = []
        self.visibles = []                    # lo que `list()` enseña: los lotes creados, como los ve la cuenta

    def create(self, requests):
        if self.errores_al_crear:
            raise self.errores_al_crear.pop(0)
        self.creados.append(requests)
        self.visibles.append(NS(
            id="msgbatch_test", created_at=datetime.now(timezone.utc),
            request_counts=NS(processing=len(requests), succeeded=0, errored=0, canceled=0, expired=0)))
        return NS(id="msgbatch_test")

    def list(self, limit=20):
        """Del más nuevo al más viejo, como la API."""
        return iter(reversed(self.visibles))

    def retrieve(self, batch_id):
        self.consultas += 1
        estado = self.estados.pop(0) if len(self.estados) > 1 else self.estados[0]
        pendientes = len(self.resultados) if estado != "ended" else 0
        hechas = len(self.resultados) - pendientes
        return NS(id=batch_id, processing_status=estado,
                  request_counts=NS(processing=pendientes, succeeded=hechas, errored=0, canceled=0, expired=0))

    def results(self, batch_id):
        entradas = list(self.resultados.items())
        if self.desordenar:
            random.Random(7).shuffle(entradas)
        for i, (cid, res) in enumerate(entradas):
            if self.falla_en and i == self.falla_en[0]:
                raise self.falla_en[1]
            yield NS(custom_id=cid, result=res)

    def cancel(self, batch_id):
        self.cancelados.append(batch_id)


class FakeClientLote:
    def __init__(self, batches):
        self.messages = NS(batches=batches)


def preparar(resultados=None, n=3, batches=None, **kw):
    base = dict(
        modelo="claude-haiku-4-5", modo="lote", max_tokens=1024, tope_gasto_usd=D("100"),
        precio_input_mtok=D("1"), precio_output_mtok=D("5"), max_error_attempts=3,
    )
    base.update(kw)
    config = Configuracion(**base)
    job = Job.crear([f"pregunta {i}" for i in range(1, n + 1)], config)
    job.save()
    if batches is None:
        if resultados is None:
            resultados = {custom_id(m): exito(f"respuesta a {m.text}") for m in job.messages}
        batches = FakeBatches(resultados)
    motor = BatchEngine(config, job)
    motor.notificaciones = False
    motor._client = FakeClientLote(batches)
    motor.inicializar_cliente = lambda *a, **k: None
    motor.intervalo_min = motor.intervalo_max = 0.01
    motor.esperas = []
    def esperar(segundos, cb):
        motor.esperas.append(segundos)
        return not motor.evento_detener.is_set()
    motor.esperar = esperar
    return motor, job, batches


class TestCaminoFeliz:
    def test_envia_un_unico_lote_con_todas_las_ordenes(self):
        motor, job, b = preparar()
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert len(b.creados) == 1 and len(b.creados[0]) == 3
        assert [r["custom_id"] for r in b.creados[0]] == ["m000001", "m000002", "m000003"]

    def test_los_custom_id_cumplen_el_patron_de_la_api(self):
        _, job, _ = preparar(n=12)
        assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", custom_id(m)) for m in job.messages)

    def test_las_peticiones_llevan_los_parametros_de_mensajes(self):
        motor, job, b = preparar(system_prompt="Sé breve.", modelo="claude-sonnet-5-5", esfuerzo="medium")
        motor.ejecutar(Callbacks())
        params = b.creados[0][0]["params"]
        assert params["model"] == "claude-sonnet-5-5" and params["max_tokens"] == 1024
        assert params["messages"] == [{"role": "user", "content": "pregunta 1"}]
        assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
        # `output_config` viaja dentro del diccionario `params`: aquí no hace falta extra_body.
        assert params["output_config"] == {"effort": "medium"}

    def test_resultados_desordenados_se_emparejan_por_custom_id(self):
        motor, job, b = preparar(n=8)
        assert b.desordenar
        motor.ejecutar(Callbacks())
        for m in job.messages:
            assert m.status == STATUS_COMPLETED
            assert f"respuesta a {m.text}" in job.response_path(m.id, 1).read_text(encoding="utf-8")

    def test_el_coste_lleva_el_descuento_del_50_por_ciento(self):
        motor, job, _ = preparar(n=1)
        motor.ejecutar(Callbacks())
        assert job.messages[0].coste_usd == D("0.00175")  # 1000 in + 500 out a precio de lote

    def test_el_lote_en_el_servidor_se_olvida_al_recogerlo(self):
        motor, job, _ = preparar()
        motor.ejecutar(Callbacks())
        assert job.batch_id == ""

    def test_se_escribe_el_resumen(self):
        motor, job, _ = preparar()
        motor.ejecutar(Callbacks())
        assert "Completados: 3/3" in job.resumen_path(1).read_text(encoding="utf-8")

    def test_emite_un_resultado_por_orden_y_el_progreso(self):
        b = FakeBatches({"m000001": exito(), "m000002": exito()}, estados=("in_progress", "in_progress", "ended"))
        motor, job, _ = preparar(n=2, batches=b)
        resultados, estados = [], []
        motor.ejecutar(Callbacks(resultado=resultados.append, estado=lambda t, a: estados.append(t)))
        assert len(resultados) == 2
        assert any("Lote en curso" in t for t in estados)
        assert b.consultas == 3 and len(motor.esperas) == 2


class TestResultadosMixtos:
    def test_cada_tipo_de_resultado_queda_con_su_categoria(self):
        res = {
            "m000001": exito(),
            "m000002": errored("invalid_request_error", "max_tokens demasiado grande"),
            "m000003": errored("api_error", "oops"),
            "m000004": EXPIRADO,
            "m000005": CANCELADO,
        }
        motor, job, _ = preparar(res, n=6)  # la 6ª no vuelve en el lote
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        tipos = {m.id: (m.status, m.error_type) for m in job.messages}
        assert tipos[1] == (STATUS_COMPLETED, "")
        assert tipos[2] == (STATUS_FAILED, "bad_request")
        assert tipos[3] == (STATUS_FAILED, "server")
        assert tipos[4] == (STATUS_FAILED, "expired")
        assert tipos[5] == (STATUS_FAILED, "canceled")
        assert tipos[6] == (STATUS_FAILED, "missing")
        assert "max_tokens demasiado grande" in job.messages[1].detalle

    def test_las_fallidas_se_pueden_reintentar_en_un_lote_nuevo(self):
        res = {"m000001": exito(), "m000002": EXPIRADO}
        motor, job, b = preparar(res, n=2)
        motor.ejecutar(Callbacks())
        assert job.reintentar_fallidas() == 1

        b2 = FakeBatches({"m000002": exito("ahora sí")})
        motor2 = BatchEngine(motor.config, job)
        motor2.notificaciones = False
        motor2._client = FakeClientLote(b2)
        motor2.inicializar_cliente = lambda *a, **k: None
        motor2.esperar = lambda s, cb: True
        assert motor2.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert [r["custom_id"] for r in b2.creados[0]] == ["m000002"]  # solo la fallida
        assert job.completed_count() == 2

    def test_un_resultado_de_tipo_desconocido(self):
        motor, job, _ = preparar({"m000001": NS(type="mágico")}, n=1)
        motor.ejecutar(Callbacks())
        assert job.messages[0].error_type == "unknown"

    def test_un_custom_id_ajeno_se_ignora(self):
        res = {"m000001": exito(), "zzz-ajeno": exito("no es mío")}
        motor, job, _ = preparar(res, n=1)
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.completed_count() == 1


class TestReanudacion:
    def test_reengancha_un_lote_que_sigue_en_el_servidor_sin_volver_a_enviarlo(self):
        motor, job, b = preparar()
        job.batch_id = "msgbatch_previo"
        job.save()
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert b.creados == []  # no se vuelve a pagar
        assert job.completed_count() == 3

    def test_Y2_una_recogida_repetida_no_vuelve_a_contabilizar_el_coste(self):
        # Una recogida cortada a medias se reanudaba sobre el mismo batch_id y volvía
        # a sumar el coste de lo ya procesado, inflando el gasto acumulado.
        motor, job, _ = preparar(n=2)
        job.batch_id = "msgbatch_previo"
        job.messages[0].status = STATUS_COMPLETED
        job.messages[0].coste_usd = D("0.00175")
        job.save()
        motor = BatchEngine(motor.config, job)
        motor.notificaciones = False
        motor._client = FakeClientLote(FakeBatches({custom_id(m): exito() for m in job.messages}))
        motor.inicializar_cliente = lambda *a, **k: None
        motor.esperar = lambda s, cb: True
        motor.ejecutar(Callbacks())
        assert job.messages[0].coste_usd == D("0.00175")           # intacto
        assert job.coste_total() == D("0.0035")                     # 2 órdenes, no 3
        assert motor.estado.gasto_acumulado_usd == D("0.0035")

    def test_una_descarga_cortada_una_vez_se_repite_sola_sin_contar_dos_veces(self):
        corte = anthropic.APIConnectionError(message="reset", request=httpx.Request("POST", "https://x"))
        corte.__cause__ = httpx.ReadError("se cortó")

        class CortaUnaVez(FakeBatches):
            cortado = False

            def results(self, batch_id):
                for i, entrada in enumerate(super().results(batch_id)):
                    if i == 2 and not self.cortado:
                        self.cortado = True
                        raise corte
                    yield entrada

        b = CortaUnaVez({f"m00000{i}": exito(f"r{i}") for i in (1, 2, 3, 4)})
        motor, job, _ = preparar(n=4, batches=b)
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.completed_count() == 4 and job.batch_id == ""
        assert job.coste_total() == D("0.00175") * 4          # las dos primeras no se cuentan otra vez
        assert motor.estado.gasto_acumulado_usd == job.coste_total()

    def test_una_recogida_que_no_se_arregla_conserva_el_lote_y_se_completa_despues(self):
        corte = anthropic.APIConnectionError(message="reset", request=httpx.Request("POST", "https://x"))
        corte.__cause__ = httpx.ReadError("se cortó")
        b = FakeBatches({f"m00000{i}": exito(f"r{i}") for i in (1, 2, 3, 4)}, falla_en=(2, corte))
        motor, job, _ = preparar(n=4, batches=b)
        assert motor.ejecutar(Callbacks()) == StopReason.FATAL          # tras repetirla las veces permitidas
        assert job.batch_id == "msgbatch_test"            # se conserva para completar la recogida
        hechas = job.completed_count()
        assert hechas == 2
        gasto_parcial = job.coste_total()

        b.falla_en = None
        motor2 = BatchEngine(motor.config, job)
        motor2.notificaciones = False
        motor2._client = FakeClientLote(b)
        motor2.inicializar_cliente = lambda *a, **k: None
        motor2.esperar = lambda s, cb: True
        assert motor2.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.completed_count() == 4
        assert job.coste_total() == gasto_parcial * 2  # 4 órdenes iguales, sin doble cuenta

    def test_un_lote_que_ya_no_existe_se_limpia_y_avisa(self):
        class SinLote(FakeBatches):
            def retrieve(self, batch_id):
                resp = httpx.Response(404, request=httpx.Request("GET", "https://x"))
                raise anthropic.NotFoundError("no existe", response=resp, body=None)

        motor, job, _ = preparar(batches=SinLote({}))
        job.batch_id = "msgbatch_viejo"
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.FATAL
        assert job.batch_id == "" and "29 días" in errores[0]
        assert job.completed_count() == 0 and len(job.pendientes()) == 3


def _corte():
    e = anthropic.APIConnectionError(message="reset", request=httpx.Request("GET", "https://x"))
    e.__cause__ = httpx.ReadError("se cortó")
    return e


def _tiempo_agotado():
    return anthropic.APITimeoutError(request=httpx.Request("GET", "https://x"))   # sin causa: la petición ya había salido


class FallaAlVerElEstado(FakeBatches):
    def __init__(self, resultados, fallos, **kw):
        super().__init__(resultados, **kw)
        self.fallos = list(fallos)

    def retrieve(self, batch_id):
        if self.fallos:
            raise self.fallos.pop(0)
        return super().retrieve(batch_id)


class TestConsultasDeSoloLectura:
    """Ver el estado de un lote o bajar sus resultados no cuesta nada: un corte o un tiempo agotado se repiten."""

    @staticmethod
    def _reanudar(n, **kw):
        motor, job, b = preparar(n=n, max_error_attempts=8, **kw)
        job.batch_id = "msgbatch_previo"
        job.save()
        return motor, job, b

    def _lote(self, fallos, n=3):
        job_ids = [f"m{i:06d}" for i in range(1, n + 1)]
        return FallaAlVerElEstado({cid: exito(f"r{cid}") for cid in job_ids}, fallos)

    def test_un_corte_al_ver_el_estado_se_repite_en_vez_de_parar(self):
        motor, job, b = self._reanudar(3, batches=self._lote([_corte(), _corte()]))
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.completed_count() == 3 and b.creados == []          # y no se envió otro lote

    def test_mas_de_tres_tiempos_agotados_seguidos_no_paran_el_trabajo(self):
        motor, job, _ = self._reanudar(3, batches=self._lote([_tiempo_agotado()] * 5))
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.completed_count() == 3

    def test_si_no_se_arregla_nunca_para_con_el_lote_conservado(self):
        motor, job, _ = preparar(n=3, max_error_attempts=3, batches=self._lote([_corte()] * 50))
        job.batch_id = "msgbatch_previo"
        job.save()
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.FATAL
        assert job.batch_id == "msgbatch_previo" and job.completed_count() == 0
        assert "Máximo de errores" in errores[-1]

    def test_un_5xx_y_un_429_siguen_su_politica_de_siempre(self):
        resp = lambda c: httpx.Response(c, request=httpx.Request("GET", "https://x"))  # noqa: E731
        fallos = [anthropic.InternalServerError("x", response=resp(500), body=None),
                  anthropic.RateLimitError("x", response=resp(429), body=None)]
        motor, job, _ = self._reanudar(3, batches=self._lote(fallos))
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED

    def test_una_clave_invalida_no_se_reintenta(self):
        resp = httpx.Response(401, request=httpx.Request("GET", "https://x"))
        fallos = [anthropic.AuthenticationError("x", response=resp, body=None)]
        motor, job, _ = self._reanudar(3, batches=self._lote(fallos))
        assert motor.ejecutar(Callbacks()) == StopReason.FATAL and job.batch_id == "msgbatch_previo"

    def test_una_descarga_que_avanza_cada_vez_no_agota_los_reintentos(self):
        """Los fallos que cuentan son los que no avanzan: una descarga larga puede cortarse varias veces y terminar."""
        class Avanza(FakeBatches):
            intento = 0

            def results(self, batch_id):
                self.intento += 1
                for i, entrada in enumerate(super().results(batch_id)):
                    if i == 2 * self.intento:
                        raise _corte()
                    yield entrada

        b = Avanza({f"m00000{i}": exito(f"r{i}") for i in range(1, 7)}, desordenar=False)
        motor, job, _ = preparar(n=6, batches=b, max_error_attempts=2)
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert job.completed_count() == 6 and b.intento == 3
        assert motor.estado.gasto_acumulado_usd == job.coste_total()   # sin contar nada dos veces

    def test_una_descarga_que_no_avanza_si_agota_los_reintentos(self):
        class SiempreIgual(FakeBatches):
            def results(self, batch_id):
                for i, entrada in enumerate(super().results(batch_id)):
                    if i == 2:
                        raise _corte()
                    yield entrada

        b = SiempreIgual({f"m00000{i}": exito(f"r{i}") for i in range(1, 7)}, desordenar=False)
        motor, job, _ = preparar(n=6, batches=b, max_error_attempts=3)
        assert motor.ejecutar(Callbacks()) == StopReason.FATAL
        assert job.batch_id == "msgbatch_test" and job.completed_count() == 2

    def test_una_descarga_con_tiempo_agotado_se_repite(self):
        class Lenta(FakeBatches):
            lenta = 3

            def results(self, batch_id):
                if self.lenta:
                    self.lenta -= 1
                    raise _tiempo_agotado()
                yield from super().results(batch_id)

        b = Lenta({f"m00000{i}": exito(f"r{i}") for i in (1, 2, 3)})
        motor, job, _ = preparar(n=3, batches=b, max_error_attempts=8)
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED and job.completed_count() == 3


class TestTope:
    def test_no_se_envia_si_el_peor_caso_no_cabe_en_el_tope(self):
        motor, job, b = preparar(n=100, max_tokens=8000, tope_gasto_usd=D("1"))
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.BUDGET
        assert b.creados == []            # nada salió: no se puede frenar un lote ya enviado
        assert "max_tokens" in errores[0] and "peor caso" in errores[0]

    def test_la_pista_de_max_tokens_realmente_cabe(self):
        motor, job, _ = preparar(n=100, max_tokens=8000, tope_gasto_usd=D("1"))
        errores = []
        motor.ejecutar(Callbacks(error=errores.append))
        pista = int(re.search(r"max_tokens ≈ ([\d,]+)", errores[0]).group(1).replace(",", ""))
        # con ese max_tokens el peor caso ya no supera el tope
        motor2, job2, _ = preparar(n=100, max_tokens=pista, tope_gasto_usd=D("1"))
        assert motor2.ejecutar(Callbacks()) == StopReason.COMPLETED

    def test_cabe_si_se_baja_max_tokens(self):
        motor, job, b = preparar(n=100, max_tokens=500, tope_gasto_usd=D("1"))
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert len(b.creados) == 1

    def test_sin_tope_no_hay_comprobacion(self):
        motor, job, b = preparar(n=3, max_tokens=64000, tope_gasto_usd=D(0))
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED

    def test_el_tope_se_vuelve_a_comprobar_al_recoger(self):
        # Sin estimación previa el gasto real puede cruzar el tope: se avisa al final,
        # pero todo lo recogido queda guardado.
        motor, job, _ = preparar(n=3, tope_gasto_usd=D("0.001"), estimar_antes_de_enviar=False)
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.COMPLETED
        assert job.completed_count() == 3 and len(errores) == 1  # un solo aviso, no uno por orden
        assert "TOPE DE GASTO" in errores[0]


class TestLimites:
    def test_demasiadas_ordenes(self, monkeypatch):
        monkeypatch.setattr(modulo_batch, "LIMITE_ORDENES", 2)
        motor, job, b = preparar(n=3)
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.FATAL
        assert b.creados == [] and "máximo" in errores[0]

    def test_demasiado_peso(self, monkeypatch):
        monkeypatch.setattr(modulo_batch, "LIMITE_BYTES", 1000)
        motor, job, b = preparar(n=5)
        assert motor.ejecutar(Callbacks()) == StopReason.FATAL and b.creados == []


class TestParadaYErrores:
    def test_detener_mientras_espera_deja_el_lote_en_el_servidor(self):
        b = FakeBatches({"m000001": exito()}, estados=("in_progress",))
        motor, job, _ = preparar(n=1, batches=b)
        def esperar(segundos, cb):
            motor.detener()
            return False
        motor.esperar = esperar
        assert motor.ejecutar(Callbacks()) == StopReason.STOPPED
        assert job.batch_id == "msgbatch_test" and job.pendientes()

    def test_tope_mensual_de_la_cuenta_al_enviar(self):
        b = FakeBatches({})
        b.errores_al_crear = [error(429, interno(TOPE_MENSUAL))]
        motor, job, _ = preparar(batches=b)
        assert motor.ejecutar(Callbacks()) == StopReason.ACCOUNT_LIMIT
        assert job.batch_id == "" and len(job.pendientes()) == 3

    def test_429_transitorio_al_enviar_espera_y_reintenta(self):
        resp = httpx.Response(429, request=httpx.Request("POST", "https://x"), headers={"retry-after": "3"})
        b = FakeBatches({"m000001": exito()})
        b.errores_al_crear = [anthropic.RateLimitError("rl", response=resp, body=None)]
        motor, job, _ = preparar(n=1, batches=b)
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert motor.esperas[0] == 5.0

    def test_error_de_servidor_al_enviar_se_reintenta_con_backoff(self):
        b = FakeBatches({"m000001": exito()})
        b.errores_al_crear = [error(500, {"type": "api_error", "message": "x"})]
        motor, job, _ = preparar(n=1, batches=b)
        motor.espera_error_servidor = lambda e, i: 0.01
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED

    def test_clave_invalida_al_enviar(self):
        b = FakeBatches({})
        b.errores_al_crear = [error(401, {"type": "authentication_error", "message": "bad key"})]
        motor, job, _ = preparar(batches=b)
        assert motor.ejecutar(Callbacks()) == StopReason.FATAL

    def test_sin_clave_no_se_envia_nada(self):
        motor, job, b = preparar()
        def sin_clave():
            raise ValueError("No hay API key")
        motor.inicializar_cliente = sin_clave
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.FATAL
        assert "API key" in errores[0]

    def test_el_resumen_se_escribe_aunque_falle(self):
        b = FakeBatches({})
        b.errores_al_crear = [error(401)]
        motor, job, _ = preparar(batches=b)
        motor.ejecutar(Callbacks())
        assert job.resumen_path(1).exists()


class TestCancelar:
    def test_cancela_el_lote_del_servidor(self):
        motor, job, b = preparar()
        job.batch_id = "msgbatch_x"
        assert motor.cancelar_lote()
        assert b.cancelados == ["msgbatch_x"]

    def test_sin_lote_en_curso_no_hace_nada(self):
        motor, job, b = preparar()
        assert motor.cancelar_lote() is False and b.cancelados == []

    def test_si_la_api_falla_devuelve_false(self):
        motor, job, b = preparar()
        job.batch_id = "x"
        def boom(_):
            raise RuntimeError("no")
        b.cancel = boom
        assert motor.cancelar_lote() is False
