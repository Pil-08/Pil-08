"""Simulacros del modo lote (Message Batches API): el programa de verdad contra el servidor falso.

Un lote es un trato distinto: lo enviado no se puede frenar, los resultados llegan sin orden y el único
cobro posible por duplicado es CREAR DOS LOTES. De ahí que casi todo aquí gire en torno a eso.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import anthropic
import pytest

from claude_batch_monitor import cli, paths
from claude_batch_monitor.core.base import Callbacks, EngineBase, StopReason
from claude_batch_monitor.core.batch import BatchEngine
from claude_batch_monitor.core.errors import pudo_haberse_procesado
from claude_batch_monitor.job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REFUSED,
    Job,
)

from ._http import httpx
from .simulacros_comun import comprobar_invariantes, configuracion, correr
from .test_errors import error as error_http

pytestmark = pytest.mark.simulacro

CUATRO = [f"Pregunta del lote {i}" for i in range(1, 5)]


def lote(**kw):
    return configuracion(modo="lote", **kw)


def respuesta_de(c, n):
    return c.job.response_path(n, c.job.ciclos_completados + 1).read_text(encoding="utf-8")


def con_marca(config, preguntas, desde=None, ordenes=None, tras=None):
    """Un trabajo como lo deja un programa cerrado a mitad de un envío: con la marca puesta y sin id de lote.

    `tras`: el último lote que había en la cuenta al enviar ("" = ninguno); sin él, la búsqueda se limita por fecha.
    """
    job = Job.crear(preguntas, config)
    job.envio_incierto_desde = (desde or datetime.now(timezone.utc)).isoformat()
    job.envio_incierto_ordenes = len(preguntas) if ordenes is None else ordenes
    job.envio_incierto_tras = tras
    job.save()
    return job


class TestCaminoFeliz:
    def test_un_lote_se_envia_una_vez_y_cada_respuesta_va_con_su_pregunta(self, api, esperas):
        preguntas = [f"Pregunta {i}" for i in range(1, 10)]
        c = correr(preguntas, lote())
        assert c.razon is StopReason.COMPLETED and c.completadas == 9
        assert len(api.lotes) == 1 and len(api.peticiones_a("/v1/messages/batches")) == 1
        for m in c.job.messages:  # el servidor los devuelve barajados: se emparejan por custom_id
            assert f"Respuesta a: {m.text}" in respuesta_de(c, m.id)
        assert c.job.batch_id == "" and c.job.envio_incierto_desde == ""
        comprobar_invariantes(api, c)

    def test_se_paga_la_mitad_que_por_la_api_normal(self, api, esperas):
        c = correr(CUATRO, lote())
        normal = sum(cobro.entrada * 3.0 + cobro.salida * 15.0 for cobro in api.cobros) / 1_000_000
        assert float(c.job.coste_total()) == pytest.approx(normal / 2, rel=1e-6)

    def test_resultados_de_todos_los_tipos(self, api, esperas):
        preguntas = [
            "uno", "dos [[plan:errored]]", "tres [[plan:expired]]", "cuatro [[plan:canceled]]",
            "cinco [[plan:refusal]]", "seis [[plan:maxtokens]]",
        ]
        c = correr(preguntas, lote())
        assert [c.estado_de(i) for i in range(1, 7)] == [
            STATUS_COMPLETED, STATUS_FAILED, STATUS_FAILED, STATUS_FAILED, STATUS_REFUSED, STATUS_COMPLETED]
        assert c.job.mensaje(2).error_type == "bad_request"
        assert c.job.mensaje(3).error_type == "expired" and c.job.mensaje(4).error_type == "canceled"
        assert c.job.mensaje(6).truncated
        comprobar_invariantes(api, c)


class TestElTope:
    def test_si_el_peor_caso_no_cabe_no_se_envia_nada(self, api, esperas):
        c = correr(CUATRO, lote(tope_gasto_usd=Decimal("0.0001")))
        assert c.razon is StopReason.BUDGET and c.pendientes == 4
        assert len(api.lotes) == 0 and any("TOPE DE GASTO" in e for e in c.errores)
        comprobar_invariantes(api, c)


class TestParadaYReanudacion:
    def test_cerrar_con_el_lote_en_el_servidor_y_reanudar_no_crea_otro(self, api, esperas):
        api.retraso_lote = 1.0
        parar = {}

        def estado(texto, ocupado):
            if "Lote enviado" in texto and "m" in parar:
                parar["m"].detener()

        c1 = correr(CUATRO, lote(), antes=lambda m: parar.update(m=m), callbacks=Callbacks(estado=estado))
        assert c1.razon is StopReason.STOPPED
        assert c1.job.batch_id and c1.pendientes == 4 and len(api.lotes) == 1

        job = Job.load(c1.job.job_id)  # como si se hubiera cerrado y abierto el programa
        c2 = correr([], lote(), job=job)
        assert c2.razon is StopReason.COMPLETED and c2.completadas == 4
        assert len(api.lotes) == 1                      # se reengancha, no se vuelve a enviar
        comprobar_invariantes(api, c2)

    def test_un_lote_que_el_servidor_ya_no_tiene_deja_las_ordenes_pendientes(self, api, esperas):
        api.retraso_lote = 1.0

        def estado(texto, ocupado):
            if "Lote enviado" in texto:
                api.lotes.clear()

        c = correr(CUATRO, lote(), callbacks=Callbacks(estado=estado))
        assert c.razon is StopReason.FATAL and c.pendientes == 4 and c.job.batch_id == ""
        assert any("ya no existe" in e for e in c.errores)


class TestEnvioQueQuedoSinRespuesta:
    """El servidor pudo crear el lote sin que llegara su respuesta: nunca debe crearse un segundo."""

    def test_si_el_envio_agota_el_tiempo_no_se_crea_un_segundo_lote(self, api, esperas):
        api.lote_creado_con_retraso = 1.6
        c = correr(CUATRO, lote(timeout_api_sec=1))
        assert c.razon is StopReason.COMPLETED and c.completadas == 4
        assert len(api.lotes) == 1
        assert any("sí había creado" in e for e in c.estados)
        assert c.job.envio_incierto_desde == ""
        comprobar_invariantes(api, c)

    def test_si_se_corta_la_conexion_se_para_y_al_reanudar_se_encuentra_el_lote(self, api, esperas):
        api.guion_crear = ["perdida"]                      # el servidor lo crea y la respuesta no llega
        c1 = correr(CUATRO, lote())
        assert c1.razon is StopReason.NETWORK and len(api.lotes) == 1
        assert c1.job.envio_incierto_desde and c1.job.envio_incierto_ordenes == 4

        job = Job.load(c1.job.job_id)                      # la marca sobrevive a cerrar el programa
        assert job.envio_incierto_desde
        c2 = correr([], lote(), job=job)
        assert c2.razon is StopReason.COMPLETED and c2.completadas == 4
        assert len(api.lotes) == 1 and api.veces_facturada("Pregunta del lote 1") == 1
        comprobar_invariantes(api, c2)

    def test_un_5xx_al_crear_busca_antes_de_reintentar_y_luego_envia_uno_solo(self, api, esperas):
        api.guion_crear = ["500", "ok"]
        c = correr(CUATRO, lote())
        assert c.razon is StopReason.COMPLETED and c.completadas == 4
        assert len(api.lotes) == 1
        assert api.peticiones_a("/v1/messages/batches", "GET"), "debía mirar si el 500 había llegado a crearlo"
        comprobar_invariantes(api, c)

    def test_un_5xx_que_llego_despues_de_crearlo_se_adopta_en_vez_de_duplicar(self, api, esperas):
        api.guion_crear = ["creado500", "ok"]            # una pasarela falla DESPUÉS de que el servidor lo creara
        c = correr(CUATRO, lote())
        assert c.razon is StopReason.COMPLETED and c.completadas == 4
        assert len(api.lotes) == 1 and api.veces_facturada("Pregunta del lote 1") == 1
        comprobar_invariantes(api, c)

    def test_un_error_definitivo_al_crear_no_deja_marca_ni_lote(self, api, esperas):
        api.guion_crear = ["400"]
        c = correr(CUATRO, lote())
        assert c.razon is StopReason.FATAL and len(api.lotes) == 0
        assert c.job.envio_incierto_desde == "" and c.pendientes == 4

    def test_con_la_marca_puesta_y_ningun_lote_en_el_servidor_se_envia_normalmente(self, api, esperas):
        config = lote()
        job = con_marca(config, CUATRO)
        c = correr([], config, job=job)
        assert c.razon is StopReason.COMPLETED and len(api.lotes) == 1
        assert c.job.envio_incierto_desde == ""
        comprobar_invariantes(api, c)

    def test_con_la_marca_y_un_lote_reciente_del_mismo_tamano_se_adopta(self, api, esperas):
        config = lote()
        job = con_marca(config, CUATRO)
        # El lote «nuestro» lo crea la primera ejecución a medias: se reproduce enviándolo de verdad.
        api.guion_crear = ["perdida"]
        c1 = correr([], config, job=job)
        assert c1.razon is StopReason.NETWORK and len(api.lotes) == 1
        c2 = correr([], config, job=Job.load(job.job_id))
        assert c2.razon is StopReason.COMPLETED and len(api.lotes) == 1

    def test_un_lote_de_otro_tamano_no_se_confunde_con_el_nuestro(self, api, esperas):
        config = lote()
        job = con_marca(config, CUATRO)
        api.sembrar_lote(7)
        c = correr([], config, job=job)
        assert c.razon is StopReason.COMPLETED and len(api.lotes) == 2     # el ajeno + el nuestro
        comprobar_invariantes(api, c)

    def test_un_lote_viejo_del_mismo_tamano_no_se_adopta(self, api, esperas):
        config = lote()
        job = con_marca(config, CUATRO)
        api.sembrar_lote(4, hace=2 * 3600)
        c = correr([], config, job=job)
        assert c.razon is StopReason.COMPLETED and len(api.lotes) == 2

    def test_un_lote_que_ya_sigue_otra_tarea_no_se_adopta(self, api, esperas):
        config = lote()
        ajeno = api.sembrar_lote(4)
        otra = Job.crear(["otra"] * 4, config)
        otra.batch_id = ajeno
        otra.save()
        job = con_marca(config, CUATRO)
        c = correr([], config, job=job)
        assert c.razon is StopReason.COMPLETED and len(api.lotes) == 2
        assert c.job.batch_id == "" and Job.load(otra.job_id).batch_id == ajeno

    def test_con_dos_candidatos_se_para_sin_enviar_y_la_segunda_vez_se_envia(self, api, esperas):
        config = lote()
        job = con_marca(config, CUATRO)
        api.sembrar_lote(4)
        api.sembrar_lote(4)
        c1 = correr([], config, job=job)
        assert c1.razon is StopReason.FATAL and len(api.lotes) == 2
        assert any("2 lotes recientes" in e for e in c1.errores)
        assert c1.job.envio_incierto_desde == "" and c1.pendientes == 4
        c2 = correr([], config, job=Job.load(job.job_id))
        assert c2.razon is StopReason.COMPLETED and len(api.lotes) == 3

    @pytest.mark.parametrize("minutos", [-5, 5])
    def test_relojes_con_unos_minutos_de_diferencia_no_impiden_encontrarlo(self, api, esperas, minutos):
        api.desfase_reloj = timedelta(minutes=minutos)
        api.guion_crear = ["perdida"]
        config = lote()
        c1 = correr(CUATRO, config)
        assert c1.razon is StopReason.NETWORK
        c2 = correr([], config, job=Job.load(c1.job.job_id))
        assert c2.razon is StopReason.COMPLETED and len(api.lotes) == 1

    def test_si_no_se_puede_comprobar_no_se_envia_otro(self, api, esperas):
        config = lote()
        job = con_marca(config, CUATRO)
        api.caer()
        c = correr([], config, job=job, limite_s=60)
        assert c.razon is StopReason.NETWORK and c.job.batch_id == ""
        assert c.job.envio_incierto_desde, "debe seguir pendiente de comprobar en la próxima ejecución"

    # -- la línea base: el último lote que había en la cuenta al enviar ----------------------------------------

    def test_al_enviar_se_anota_cual_era_el_ultimo_lote_de_la_cuenta(self, api, esperas):
        anterior = api.sembrar_lote(9)
        api.guion_crear = ["perdida"]
        c = correr(CUATRO, lote())
        assert c.razon is StopReason.NETWORK
        assert Job.load(c.job.job_id).envio_incierto_tras == anterior

    def test_en_una_cuenta_vacia_la_linea_base_es_ninguno_y_no_desconocido(self, api, esperas):
        api.guion_crear = ["perdida"]
        c = correr(CUATRO, lote())
        assert Job.load(c.job.job_id).envio_incierto_tras == ""

    def test_un_lote_anterior_del_mismo_tamano_nunca_se_toma_por_el_propio(self, api, esperas):
        """Dentro de la ventana de tiempo, pero creado ANTES de enviar: es de otra persona y no tiene nuestras órdenes."""
        api.sembrar_lote(4, hace=120)
        api.guion_crear = ["cierre"]                      # el envío no llegó a crear nada
        c1 = correr(CUATRO, lote())
        assert c1.razon is StopReason.NETWORK and len(api.lotes) == 1
        c2 = correr([], lote(), job=Job.load(c1.job.job_id))
        assert c2.razon is StopReason.COMPLETED and c2.completadas == 4
        assert len(api.lotes) == 2                         # el ajeno y el nuestro, enviado ahora
        for m in c2.job.messages:
            assert f"Respuesta a: {m.text}" in respuesta_de(c2, m.id)
        comprobar_invariantes(api, c2)

    def test_con_la_linea_base_se_ignora_el_reloj_y_se_encuentra_el_lote_propio(self, api, esperas):
        """Con el reloj del servidor 45 min por detrás del de la máquina, la fecha sola descartaba el lote propio y se creaba otro."""
        api.desfase_reloj = timedelta(minutes=-45)
        api.guion_crear = ["perdida"]
        c1 = correr(CUATRO, lote())
        assert c1.razon is StopReason.NETWORK
        c2 = correr([], lote(), job=Job.load(c1.job.job_id))
        assert c2.razon is StopReason.COMPLETED and len(api.lotes) == 1
        assert api.veces_facturada("Pregunta del lote 1") == 1
        comprobar_invariantes(api, c2)

    @pytest.mark.parametrize("minutos", [-45, 45])
    def test_con_la_linea_base_y_el_servidor_adelantado_o_atrasado_no_se_adopta_uno_viejo(self, api, esperas, minutos):
        api.desfase_reloj = timedelta(minutes=minutos)
        api.sembrar_lote(4, hace=300)
        api.guion_crear = ["cierre"]
        c1 = correr(CUATRO, lote())
        c2 = correr([], lote(), job=Job.load(c1.job.job_id))
        assert c2.razon is StopReason.COMPLETED and len(api.lotes) == 2
        comprobar_invariantes(api, c2)

    def test_sin_linea_base_se_limita_por_fecha_como_antes(self, api, esperas):
        """Si al enviar no se pudo mirar la cuenta, la búsqueda sigue funcionando con la ventana de tiempo."""
        api.guion_consulta = ["500"]                       # falla la lectura de la línea base
        api.guion_crear = ["perdida"]
        c1 = correr(CUATRO, lote())
        assert c1.razon is StopReason.NETWORK
        assert Job.load(c1.job.job_id).envio_incierto_tras is None
        c2 = correr([], lote(), job=Job.load(c1.job.job_id))
        assert c2.razon is StopReason.COMPLETED and len(api.lotes) == 1
        comprobar_invariantes(api, c2)

    def test_la_linea_base_sobrevive_al_cierre_y_se_borra_al_encontrar_o_descartar(self, api, esperas):
        config = lote()
        antes = api.sembrar_lote(3)
        job = con_marca(config, CUATRO, tras=antes)
        c = correr([], config, job=job)
        assert c.razon is StopReason.COMPLETED
        assert c.job.envio_incierto_tras is None and c.job.envio_incierto_desde == ""
        assert Job.load(job.job_id).envio_incierto_tras is None

    def test_la_linea_base_se_guarda_y_se_lee_del_disco(self, api, esperas):
        config = lote()
        for valor in ("msgbatch_0007", "", None):
            job = con_marca(config, CUATRO, tras=valor)
            assert Job.load(job.job_id).envio_incierto_tras == valor

    def test_el_envio_se_apunta_en_disco_antes_de_hacerlo(self, api, esperas, monkeypatch):
        """Si el programa muere durante el POST, en disco ya consta que pudo llegar."""
        visto = []
        original = Job.save

        def guardar(self_):
            visto.append((self_.envio_incierto_desde, self_.envio_incierto_ordenes, bool(api.lotes)))
            return original(self_)

        monkeypatch.setattr(Job, "save", guardar)
        c = correr(CUATRO, lote())
        assert c.razon is StopReason.COMPLETED
        con_marca_y_sin_lote = [v for v in visto if v[0] and v[1] == 4 and not v[2]]
        assert con_marca_y_sin_lote, "la marca debía escribirse cuando el servidor aún no tenía el lote"


class TestPudoHaberseProcesado:
    @pytest.mark.parametrize("estado,esperado", [
        (400, False), (401, False), (402, False), (403, False), (404, False), (413, False), (422, False), (429, False),
        (408, True), (409, True), (500, True), (502, True), (504, True), (529, True),
    ])
    def test_segun_el_estado_http(self, estado, esperado):
        assert pudo_haberse_procesado(error_http(estado)) is esperado

    def test_sin_conexion_la_peticion_ni_salio(self):
        peticion = httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")
        e = anthropic.APIConnectionError(message="Connection error.", request=peticion)
        e.__cause__ = httpx.ConnectError("Name or service not known")
        assert pudo_haberse_procesado(e) is False

    def test_tiempo_agotado_esperando_la_respuesta_si_pudo(self):
        peticion = httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")
        e = anthropic.APITimeoutError(request=peticion)
        e.__cause__ = httpx.ReadTimeout("lento")
        assert pudo_haberse_procesado(e) is True

    def test_tiempo_agotado_al_conectar_no(self):
        peticion = httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")
        e = anthropic.APITimeoutError(request=peticion)
        e.__cause__ = httpx.ConnectTimeout("no conecta")
        assert pudo_haberse_procesado(e) is False

    def test_un_corte_a_mitad_si_pudo(self):
        peticion = httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")
        e = anthropic.APIConnectionError(message="Connection error.", request=peticion)
        e.__cause__ = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        assert pudo_haberse_procesado(e) is True

    def test_lo_desconocido_se_supone_que_si(self):
        assert pudo_haberse_procesado(RuntimeError("raro")) is True


class TestCancelar:
    """Un lote enviado no se frena solo dejando de esperar: sigue en el servidor y se cobra. Hay que cancelarlo."""

    @staticmethod
    def en_vuelo(api):
        api.retraso_lote = 60                      # no termina por sí solo durante la prueba
        motor = {}

        def estado(texto, ocupado):
            if "Lote enviado" in texto:
                motor["m"].detener()

        return correr(CUATRO, lote(), antes=lambda m: motor.update(m=m), callbacks=Callbacks(estado=estado))

    def test_desde_la_terminal_se_cancela_y_luego_se_recoge_lo_que_quede(self, api, esperas, capsys):
        c = self.en_vuelo(api)
        assert c.razon is StopReason.STOPPED and c.job.batch_id
        assert cli.main_cli(["--cancelar-lote", c.job.job_id]) == 0
        assert api.cancelados == [c.job.batch_id]
        assert "--reanudar" in capsys.readouterr().out
        codigo = cli.main_cli(["--reanudar", c.job.job_id])
        job = Job.load(c.job.job_id)
        assert [m.error_type for m in job.messages] == ["canceled"] * 4 and job.batch_id == ""
        assert codigo == cli.EXIT_FALLO                       # quedan órdenes fallidas (canceladas): se reintentan con --reanudar
        assert api.facturado_usd(3.0, 15.0) == 0              # lo cancelado antes de procesarse no se cobra

    def test_un_lote_sin_nada_en_curso_no_se_puede_cancelar(self, api, esperas, capsys):
        c = correr(CUATRO, lote())                              # terminado: sin batch_id
        assert cli.main_cli(["--cancelar-lote", c.job.job_id]) == cli.EXIT_USO
        assert "nada en curso" in capsys.readouterr().err
        assert api.cancelados == []

    def test_un_lote_que_no_existe(self, api, esperas, capsys):
        assert cli.main_cli(["--cancelar-lote", "noexiste"]) == cli.EXIT_USO
        assert "No existe el lote" in capsys.readouterr().err

    def test_si_el_servidor_ya_no_lo_tiene_lo_dice(self, api, esperas, capsys):
        c = self.en_vuelo(api)
        api.lotes.clear()
        assert cli.main_cli(["--cancelar-lote", c.job.job_id]) == cli.EXIT_FALLO
        assert "No se pudo cancelar" in capsys.readouterr().err

    def test_con_la_terminal_ocupada_por_otra_copia_igualmente_se_puede_cancelar(self, api, esperas):
        """Cancelar no gasta ni toca el gasto acumulado: no necesita el bloqueo de una sola copia."""
        from claude_batch_monitor.instancia_unica import BloqueoDeInstancia

        c = self.en_vuelo(api)
        with BloqueoDeInstancia():
            assert cli.main_cli(["--cancelar-lote", c.job.job_id]) == 0

    def test_el_motor_ya_terminado_cancela_con_un_cliente_propio(self, api, esperas):
        """El hilo que cancela desde la ventana llega cuando el motor ya cerró su cliente."""
        c = self.en_vuelo(api)
        assert c.motor._client is not None                    # cerrado, pero la referencia sigue
        assert c.motor.cancelar_lote() is True
        assert api.cancelados == [c.job.batch_id]

    def test_si_el_cliente_del_motor_se_cierra_a_mitad_se_repite_con_uno_propio(self, api, esperas):
        """Al parar, el motor cierra su cliente: una cancelación que lo estuviera usando no puede perderse por eso."""
        from types import SimpleNamespace as NS

        c = self.en_vuelo(api)

        def cerrado(_id):
            raise RuntimeError("Cannot send a request, as the client has been closed.")

        c.motor._client = NS(messages=NS(batches=NS(cancel=cerrado)), is_closed=lambda: False)
        assert c.motor.cancelar_lote() is True
        assert api.cancelados == [c.job.batch_id]

    def test_sin_lote_en_curso_el_motor_dice_que_no(self, api, esperas):
        c = correr(CUATRO[:1], lote())
        assert c.motor.cancelar_lote() is False

    def test_ctrl_c_en_lote_explica_como_cancelar(self, api, esperas, capsys, monkeypatch):
        api.retraso_lote = 60
        from claude_batch_monitor.core.batch import BatchEngine

        def interrumpido(self, cb=None):
            self.inicializar_cliente()
            self._enviar(cb or Callbacks())      # el lote llega a enviarse…
            raise KeyboardInterrupt              # …y entonces se pulsa Ctrl+C

        monkeypatch.setattr(BatchEngine, "ejecutar", interrumpido)
        codigo = cli.main_cli(["--mensaje", "hola", "--modo", "lote"])
        cap = capsys.readouterr()
        assert codigo == cli.EXIT_INCOMPLETO
        assert "--cancelar-lote" in cap.out + cap.err


class TestRecogidaInterrumpida:
    def test_ctrl_c_a_mitad_de_la_recogida_conserva_lo_apuntado_hasta_ese_momento(self, api, esperas, monkeypatch):
        llamadas = {"n": 0}
        original = EngineBase.registrar_respuesta

        def interrumpe(self_, *a, **k):
            llamadas["n"] += 1
            if llamadas["n"] == 18:
                raise KeyboardInterrupt
            return original(self_, *a, **k)

        monkeypatch.setattr(EngineBase, "registrar_respuesta", interrumpe)
        config = lote(tope_gasto_usd=Decimal("50"))
        job = Job.crear([f"Pregunta {i}" for i in range(1, 41)], config)
        job.save()
        motor = BatchEngine(config, job)
        motor.notificaciones = False
        motor.intervalo_min = motor.intervalo_max = 0.05
        with pytest.raises(KeyboardInterrupt):
            motor.ejecutar(Callbacks())
        monkeypatch.setattr(EngineBase, "registrar_respuesta", original)

        en_disco = Job.load(job.job_id)
        assert sum(m.status == STATUS_COMPLETED for m in en_disco.messages) == 17      # nada de lo apuntado se pierde
        gasto = Decimal(json.loads(paths.STATE_FILE.read_text(encoding="utf-8"))["gasto_acumulado_usd"])
        assert gasto == en_disco.coste_total()
        c = correr([], config, job=en_disco)
        assert c.razon is StopReason.COMPLETED and c.completadas == 40
        comprobar_invariantes(api, c)


class TestRecogida:
    """Bajar los resultados es gratis y repetible: un corte o un 5xx se repiten y no se pierde ni se duplica nada."""

    @pytest.mark.parametrize("fallos", [["500"], ["corte"], ["basura"], ["500", "corte", "basura"]])
    def test_si_la_descarga_falla_se_repite_y_se_completa_el_lote(self, api, esperas, fallos):
        api.guion_resultados = [*fallos, "ok"]
        c = correr([f"Pregunta de la recogida {i}" for i in range(1, 9)], lote())
        assert c.razon is StopReason.COMPLETED and c.completadas == 8, (c.razon, c.errores)
        assert len(api.lotes) == 1 and c.job.batch_id == ""
        for m in c.job.messages:
            assert f"Respuesta a: {m.text}" in respuesta_de(c, m.id)
        comprobar_invariantes(api, c)             # y se pagó una sola vez

    def test_si_la_descarga_no_se_arregla_nunca_para_y_el_lote_queda_para_reanudar(self, api, esperas):
        api.guion_resultados = ["500"] * 50
        c = correr(CUATRO, lote(max_error_attempts=3))
        assert c.razon is StopReason.FATAL and c.job.batch_id and c.pendientes == 4
        api.guion_resultados = []
        c2 = correr([], lote(), job=Job.load(c.job.job_id))
        assert c2.razon is StopReason.COMPLETED and c2.completadas == 4 and len(api.lotes) == 1
        comprobar_invariantes(api, c2)

    def test_una_descarga_cortada_a_medias_conserva_lo_ya_apuntado(self, api, esperas, monkeypatch):
        from claude_batch_monitor.core import batch as modulo

        monkeypatch.setattr(modulo, "RESULTADOS_ENTRE_PUNTOS_DE_CONTROL", 1000)   # nada se guarda hasta el final
        api.guion_resultados = ["corte", "ok"]
        c = correr([f"Pregunta {i}" for i in range(1, 13)], lote())
        assert c.razon is StopReason.COMPLETED and c.completadas == 12
        comprobar_invariantes(api, c)

    def test_el_estado_y_el_gasto_se_guardan_a_intervalos_no_con_cada_resultado(self, api, esperas, monkeypatch):
        """Con 100 000 resultados, reescribir el lote entero con cada uno costaría más que el propio lote."""
        guardados = []
        original = Job.save

        def guardar(self_):
            guardados.append(sum(m.status == STATUS_COMPLETED for m in self_.messages))
            return original(self_)

        monkeypatch.setattr(Job, "save", guardar)
        c = correr([f"Pregunta {i}" for i in range(1, 601)], lote(tope_gasto_usd=Decimal("50")), limite_s=120)
        assert c.razon is StopReason.COMPLETED and c.completadas == 600
        durante_la_recogida = [n for n in guardados if 0 < n < 600]
        assert len(durante_la_recogida) <= 12, f"{len(durante_la_recogida)} guardados mientras se recogían 600 resultados"
        comprobar_invariantes(api, c)

    def test_si_se_muere_a_mitad_de_la_recogida_al_reabrir_todo_cuadra_sin_cobrar_ni_contar_dos_veces(
            self, api, esperas, monkeypatch):
        """Un fallo brusco entre dos puntos de control deshace JUNTOS el estado y el gasto: al reanudar se recoge otra vez."""
        total = 30
        llamadas = {"n": 0}
        muerto = {"si": False}
        original = EngineBase.registrar_respuesta
        original_control = EngineBase.punto_de_control

        def muere(self_, *a, **k):
            llamadas["n"] += 1
            if llamadas["n"] == 12:
                muerto["si"] = True
                raise SystemExit("el proceso muere aquí")

            return original(self_, *a, **k)

        def sin_escribir_tras_morir(self_, cb):
            return True if muerto["si"] else original_control(self_, cb)   # un proceso muerto ya no escribe nada

        monkeypatch.setattr(EngineBase, "registrar_respuesta", muere)
        monkeypatch.setattr(EngineBase, "punto_de_control", sin_escribir_tras_morir)
        config = lote(tope_gasto_usd=Decimal("50"))
        job = Job.crear([f"Pregunta {i}" for i in range(1, total + 1)], config)
        job.save()
        motor = BatchEngine(config, job)
        motor.notificaciones = False
        motor.intervalo_min = motor.intervalo_max = 0.05
        hilo = threading.Thread(target=lambda: _ignorar_salida(lambda: motor.ejecutar(Callbacks())), daemon=True)
        hilo.start()
        hilo.join(60)
        assert not hilo.is_alive()
        monkeypatch.setattr(EngineBase, "registrar_respuesta", original)
        monkeypatch.setattr(EngineBase, "punto_de_control", original_control)

        en_disco = Job.load(job.job_id)
        assert en_disco.batch_id, "el lote sigue pendiente de recoger"
        gasto_en_disco = Decimal(json.loads(paths.STATE_FILE.read_text(encoding="utf-8"))["gasto_acumulado_usd"])             if paths.STATE_FILE.exists() else Decimal(0)
        assert gasto_en_disco == sum((m.coste_usd for m in en_disco.messages), Decimal(0))   # estado y gasto, juntos

        c = correr([], config, job=en_disco)
        assert c.razon is StopReason.COMPLETED and c.completadas == total
        comprobar_invariantes(api, c)           # el gasto anotado == lo que facturó el servidor, una sola vez cada una


class TestConsultasDeSoloLectura:
    """Ver el estado de un lote o listar los recientes no cuesta nada: un fallo se repite y no para el trabajo."""

    @pytest.mark.parametrize("fallos", [
        ["500"], ["529"], ["basura"], ["cierre"], ["reset"], ["corte"], ["500", "corte", "basura", "reset", "529"]])
    def test_un_fallo_al_ver_el_estado_se_repite_y_el_lote_se_completa(self, api, esperas, fallos):
        api.retraso_lote = 0.3
        api.guion_consulta = ["ok", *fallos, "ok"]        # el primero lo gasta la lectura de la línea base
        c = correr(CUATRO, lote(max_error_attempts=8))
        assert c.razon is StopReason.COMPLETED and c.completadas == 4, (c.razon, c.errores)
        assert len(api.lotes) == 1 and api.veces_facturada("Pregunta del lote 1") == 1
        comprobar_invariantes(api, c)

    def test_tres_tiempos_agotados_seguidos_al_ver_el_estado_no_paran_el_trabajo(self, api, esperas):
        """Con un envío, tres tiempos agotados seguidos paran para no cobrar tres veces; con una lectura no hay nada que cobrar."""
        api.retraso_lote = 0.3
        api.guion_consulta = ["ok", "lento(1.3)", "lento(1.3)", "lento(1.3)", "ok"]
        c = correr(CUATRO, lote(timeout_api_sec=1, max_error_attempts=8), limite_s=60)
        assert c.razon is StopReason.COMPLETED and c.completadas == 4, (c.razon, c.errores)
        assert len(api.lotes) == 1
        comprobar_invariantes(api, c)

    def test_si_la_lectura_no_se_arregla_nunca_para_y_el_lote_sigue_en_el_servidor_para_reanudar(self, api, esperas):
        api.guion_consulta = ["ok"] + ["500"] * 100
        c1 = correr(CUATRO, lote(max_error_attempts=3))
        assert c1.razon is StopReason.FATAL and c1.pendientes == 4
        assert c1.job.batch_id and len(api.lotes) == 1
        api.guion_consulta = []
        c2 = correr([], lote(), job=Job.load(c1.job.job_id))
        assert c2.razon is StopReason.COMPLETED and c2.completadas == 4 and len(api.lotes) == 1
        comprobar_invariantes(api, c2)

    def test_un_corte_al_listar_los_recientes_se_repite_antes_de_decidir_si_hay_que_enviar_otro(self, api, esperas):
        """La lista decide si se envía otro lote: si falla, se vuelve a pedir y nunca se envía sin saberlo."""
        config = lote(max_error_attempts=8)
        creado = api.sembrar_lote(0)                       # solo para tener un punto de partida distinto de «ninguno»
        job = con_marca(config, CUATRO, tras=creado)
        api.guion_consulta = ["reset", "cierre", "corte"]  # tres fallos seguidos al listar
        c = correr([], config, job=job)
        assert c.razon is StopReason.COMPLETED and c.completadas == 4
        assert len(api.lotes) == 2                         # el ajeno y el nuestro
        comprobar_invariantes(api, c)

    def test_una_descarga_que_se_corta_tres_veces_sigue_reintentandose(self, api, esperas):
        api.guion_resultados = ["corte", "corte", "corte", "ok"]
        c = correr(CUATRO, lote(max_error_attempts=8))
        assert c.razon is StopReason.COMPLETED and c.completadas == 4, (c.razon, c.errores)
        comprobar_invariantes(api, c)


def _ignorar_salida(f):
    try:
        return f()
    except SystemExit:
        return None
