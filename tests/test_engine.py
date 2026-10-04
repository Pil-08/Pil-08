"""Contabilidad y reglas del motor, sin red.

También define los dobles (`FakeResponse`…) que reutilizan otros tests.
"""

import threading
import time
from decimal import Decimal as D

import pytest

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core.base import (
    AVISO_TRUNCADA,
    RESPUESTA_VACIA,
    Callbacks,
    EngineBase,
    StopReason,
    build_request_params,
)
from claude_batch_monitor.job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REFUSED,
    Job,
)
from claude_batch_monitor.state import EstadoMonitor

# ----------------------------------------------------------------------
# Dobles reutilizables
# ----------------------------------------------------------------------


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeThinking:
    """Bloque de razonamiento: no tiene `.text`."""

    type = "thinking"
    thinking = "pensando…"


class FakeUsage:
    def __init__(self, input_tokens=1000, output_tokens=500,
                 cache_creation_input_tokens=0, cache_read_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class FakeResponse:
    """Respuesta de la API. Por defecto 1000 tokens de entrada y 500 de salida."""

    def __init__(self, text="respuesta de prueba", input_tokens=1000, output_tokens=500,
                 stop_reason="end_turn", stop_details=None, content=None, **cache):
        self.content = content if content is not None else [FakeBlock(text)]
        self.usage = FakeUsage(input_tokens, output_tokens, **cache)
        self.stop_reason = stop_reason
        self.stop_details = stop_details


# ----------------------------------------------------------------------


def _cfg(**kw):
    base = dict(
        modelo="claude-haiku-4-5",
        max_tokens=1024,
        tope_gasto_usd=D("10"),
        precio_input_mtok=D("1.0"),
        precio_output_mtok=D("5.0"),
        estimar_antes_de_enviar=False,
    )
    base.update(kw)
    return Configuracion(**base)


def _motor(n=2, **kw):
    config = _cfg(**kw)
    job = Job.crear([f"orden {i}" for i in range(1, n + 1)], config)
    job.save()
    motor = EngineBase(config, job)
    motor.notificaciones = False
    return motor, job


class TestRegistrarRespuesta:
    def test_guarda_la_respuesta_y_actualiza_estado_y_gasto(self):
        motor, job = _motor()
        msg = job.messages[0]
        assert motor.registrar_respuesta(msg, FakeResponse("hola"), 1, Callbacks(), latency_ms=420)

        assert msg.status == STATUS_COMPLETED
        assert (msg.tokens_input, msg.tokens_output, msg.latency_ms) == (1000, 500, 420)
        assert msg.coste_usd == D("0.0035")
        texto = job.response_path(1, 1).read_text(encoding="utf-8")
        assert "orden 1" in texto and "hola" in texto
        assert motor.estado.gasto_acumulado_usd == D("0.0035")
        assert motor.estado.exitos == 1
        # y quedó en disco, no solo en memoria
        assert EstadoMonitor.cargar().gasto_acumulado_usd == D("0.0035")
        assert job.coste_total() == D("0.0035")

    def test_C1_persiste_antes_de_comprobar_el_tope(self):
        # La respuesta que cruza el tope YA está pagada: no puede perderse.
        motor, job = _motor(tope_gasto_usd=D("0.001"))
        msg = job.messages[0]
        seguir = motor.registrar_respuesta(msg, FakeResponse("pagada"), 1, Callbacks())
        assert seguir is False
        assert motor.stop_reason == StopReason.BUDGET
        assert msg.status == STATUS_COMPLETED
        assert "pagada" in job.response_path(1, 1).read_text(encoding="utf-8")

    def test_sin_tope_nunca_para(self):
        motor, job = _motor(tope_gasto_usd=D(0))
        assert motor.registrar_respuesta(job.messages[0], FakeResponse(), 1, Callbacks())

    def test_el_error_del_tope_se_notifica_a_la_interfaz(self):
        motor, job = _motor(tope_gasto_usd=D("0.001"))
        errores = []
        motor.registrar_respuesta(job.messages[0], FakeResponse(), 1, Callbacks(error=errores.append))
        assert len(errores) == 1 and "TOPE DE GASTO" in errores[0] and "guardado" in errores[0]

    def test_respuesta_truncada_se_marca_y_avisa(self):
        motor, job = _motor()
        msg = job.messages[0]
        motor.registrar_respuesta(msg, FakeResponse("a medias", stop_reason="max_tokens"), 1, Callbacks())
        assert msg.truncated
        assert AVISO_TRUNCADA in job.response_path(1, 1).read_text(encoding="utf-8")

    def test_contexto_agotado_cuenta_como_truncada(self):
        motor, job = _motor()
        msg = job.messages[0]
        motor.registrar_respuesta(
            msg, FakeResponse(stop_reason="model_context_window_exceeded"), 1, Callbacks()
        )
        assert msg.truncated

    def test_respuesta_vacia_deja_un_marcador(self):
        motor, job = _motor()
        motor.registrar_respuesta(job.messages[0], FakeResponse(content=[]), 1, Callbacks())
        assert RESPUESTA_VACIA in job.response_path(1, 1).read_text(encoding="utf-8")

    def test_los_bloques_de_razonamiento_no_se_cuelan_en_el_texto(self):
        motor, job = _motor()
        resp = FakeResponse(content=[FakeThinking(), FakeBlock("solo esto"), FakeBlock("y esto")])
        motor.registrar_respuesta(job.messages[0], resp, 1, Callbacks())
        texto = job.response_path(1, 1).read_text(encoding="utf-8")
        assert "solo esto\ny esto" in texto and "pensando" not in texto

    def test_el_coste_incluye_la_cache(self):
        motor, job = _motor(modelo="claude-haiku-4-5")
        resp = FakeResponse(input_tokens=100, output_tokens=50,
                            cache_creation_input_tokens=1000, cache_read_input_tokens=2000)
        motor.registrar_respuesta(job.messages[0], resp, 1, Callbacks())
        assert job.messages[0].coste_usd == D("0.0018")
        assert job.messages[0].tokens_input == 3100  # 100 + 1000 + 2000
        assert job.messages[0].tokens_cache_read == 2000

    def test_modo_lote_aplica_el_descuento(self):
        motor, job = _motor()
        motor.registrar_respuesta(job.messages[0], FakeResponse(), 1, Callbacks(), lote=True)
        assert job.messages[0].coste_usd == D("0.00175")

    def test_el_coste_de_una_orden_se_acumula_entre_ciclos(self):
        motor, job = _motor()
        m = job.messages[0]
        motor.registrar_respuesta(m, FakeResponse(), 1, Callbacks())
        motor.registrar_respuesta(m, FakeResponse(), 2, Callbacks())
        assert m.coste_usd == D("0.0070")

    def test_emite_resultado_y_gasto(self):
        motor, job = _motor()
        vistos, gastos = [], []
        motor.registrar_respuesta(
            job.messages[0], FakeResponse(), 1,
            Callbacks(resultado=vistos.append, gasto=lambda usd, uso: gastos.append((usd, uso.salida))),
        )
        assert vistos == [job.messages[0]] and gastos == [(D("0.0035"), 500)]

    def test_un_callback_que_falla_no_tumba_el_motor(self):
        motor, job = _motor()
        def roto(*_):
            raise RuntimeError("la interfaz explotó")
        assert motor.registrar_respuesta(job.messages[0], FakeResponse(), 1, Callbacks(resultado=roto))
        assert job.messages[0].status == STATUS_COMPLETED


class TestRechazos:
    """HTTP 200 con stop_reason=refusal: se cobra, pero no es una respuesta."""

    def test_se_marca_rechazada_con_su_categoria_y_se_cobra(self):
        motor, job = _motor()
        msg = job.messages[0]
        resp = FakeResponse(content=[], stop_reason="refusal",
                            stop_details={"type": "safety_violation", "category": "hate_speech"})
        assert motor.registrar_respuesta(msg, resp, 1, Callbacks())
        assert msg.status == STATUS_REFUSED
        assert msg.error_type == "refusal" and "hate_speech" in msg.detalle
        assert msg.coste_usd == D("0.0035")
        assert motor.estado.exitos == 0  # no cuenta como éxito

    def test_stop_details_como_objeto(self):
        class Detalles:
            category = "cyber"

        motor, job = _motor()
        resp = FakeResponse(content=[], stop_reason="refusal", stop_details=Detalles())
        motor.registrar_respuesta(job.messages[0], resp, 1, Callbacks())
        assert "cyber" in job.messages[0].detalle

    def test_un_rechazo_no_para_el_lote(self):
        motor, job = _motor()
        assert motor.registrar_respuesta(
            job.messages[0], FakeResponse(content=[], stop_reason="refusal"), 1, Callbacks()
        )
        assert motor.stop_reason is None

    def test_conserva_el_texto_parcial(self):
        motor, job = _motor()
        resp = FakeResponse("empiezo y…", stop_reason="refusal")
        motor.registrar_respuesta(job.messages[0], resp, 1, Callbacks())
        assert "empiezo y…" in job.response_path(1, 1).read_text(encoding="utf-8")


class TestMarcarParada:
    def test_un_motivo_concreto_no_se_degrada_a_detenido(self):
        # X5: un hilo que despierta de su backoff veía la parada y la pisaba con STOPPED.
        motor, _ = _motor()
        motor.marcar_parada(StopReason.NO_CREDIT)
        motor.marcar_parada(StopReason.STOPPED)
        assert motor.stop_reason == StopReason.NO_CREDIT

    def test_un_motivo_concreto_sustituye_a_uno_debil(self):
        motor, _ = _motor()
        motor.marcar_parada(StopReason.STOPPED)
        motor.marcar_parada(StopReason.BUDGET)
        assert motor.stop_reason == StopReason.BUDGET

    def test_el_primer_motivo_concreto_gana(self):
        motor, _ = _motor()
        motor.marcar_parada(StopReason.BUDGET)
        motor.marcar_parada(StopReason.FATAL)
        assert motor.stop_reason == StopReason.BUDGET

    def test_despierta_a_los_hilos_en_espera(self):
        motor, _ = _motor()
        motor.marcar_parada(StopReason.FATAL)
        assert motor.evento_detener.is_set()

    def test_detener_marca_stopped(self):
        motor, _ = _motor()
        motor.detener()
        assert motor.stop_reason == StopReason.STOPPED


class TestEsperar:
    def test_espera_completa(self):
        motor, _ = _motor()
        t0 = time.monotonic()
        assert motor.esperar(0.2, Callbacks())
        assert time.monotonic() - t0 >= 0.19

    def test_se_interrumpe_al_detener(self):
        motor, _ = _motor()
        threading.Timer(0.1, motor.detener).start()
        t0 = time.monotonic()
        assert motor.esperar(30, Callbacks()) is False
        assert time.monotonic() - t0 < 2

    def test_emite_ticks(self):
        motor, _ = _motor()
        ticks = []
        motor.esperar(0.6, Callbacks(tick=lambda total, resto: ticks.append((total, resto))))
        assert ticks[0] == (0.6, 0.6) and len(ticks) >= 2


class TestDeadline:
    def test_sin_hora_limite_no_hay_deadline(self):
        motor, _ = _motor()
        assert motor.calcular_deadline() is None

    def test_un_bucle_siempre_tiene_hora_limite(self):
        motor, _ = _motor(modo_continuo=True)
        d = motor.calcular_deadline()
        assert d is not None and (d.hour, d.minute) == (23, 59)

    def test_hora_explicita(self):
        motor, _ = _motor(hora_limite="07:15")
        d = motor.calcular_deadline()
        assert (d.hour, d.minute) == (7, 15)

    def test_hora_invalida_cae_a_2359(self):
        motor, _ = _motor(hora_limite="mañana")
        d = motor.calcular_deadline()
        assert (d.hour, d.minute) == (23, 59)

    def test_si_la_hora_ya_paso_es_la_de_manana(self):
        from datetime import datetime, timedelta
        hace_un_minuto = (datetime.now() - timedelta(minutes=1)).strftime("%H:%M")
        motor, _ = _motor(hora_limite=hace_un_minuto)
        assert motor.calcular_deadline() > datetime.now()


class TestInicioDelMotor:
    def test_lote_nuevo_empieza_el_contador_a_cero(self):
        EstadoMonitor(gasto_acumulado_usd=D("9.99")).guardar()
        motor, _ = _motor()
        assert motor.estado.gasto_acumulado_usd == 0

    def test_reanudar_no_reinicia_el_gasto_del_propio_lote(self):
        # El contador parte de lo que ESTE lote ya gastó, no de cero ni del de otro lote.
        config = _cfg()
        job = Job.crear(["a", "b"], config)
        job.messages[0].status = STATUS_COMPLETED
        job.messages[0].coste_usd = D("1.5")
        job.save()
        EstadoMonitor(gasto_acumulado_usd=D("99")).guardar()  # basura de otro lote
        motor = EngineBase(config, job)
        assert motor.estado.gasto_acumulado_usd == D("1.5")

    def test_sin_reset_el_contador_es_global(self):
        EstadoMonitor(gasto_acumulado_usd=D("3")).guardar()
        motor, _ = _motor(reset_gasto_por_lote=False)
        assert motor.estado.gasto_acumulado_usd == D("3")

    def test_marca_el_lote_en_curso(self):
        motor, job = _motor()
        assert EstadoMonitor.cargar().current_job_id == job.job_id


class TestReservaDePresupuesto:
    def test_cabe(self):
        motor, _ = _motor(estimar_antes_de_enviar=True, tope_gasto_usd=D("1"))
        assert motor._reservar(D("0.5"), Callbacks())
        assert motor._reservado == D("0.5")

    def test_no_cabe_ni_sola_para_el_lote(self):
        motor, _ = _motor(estimar_antes_de_enviar=True, tope_gasto_usd=D("0.1"))
        errores = []
        assert motor._reservar(D("0.5"), Callbacks(error=errores.append)) is False
        assert motor.stop_reason == StopReason.BUDGET
        assert "podría costar" in errores[0]

    def test_sin_tope_o_sin_estimacion_no_reserva(self):
        motor, _ = _motor(estimar_antes_de_enviar=True, tope_gasto_usd=D(0))
        assert motor._reservar(D("999"), Callbacks()) and motor._reservado == 0
        motor2, _ = _motor(estimar_antes_de_enviar=False, tope_gasto_usd=D("0.1"))
        assert motor2._reservar(D("999"), Callbacks()) and motor2._reservado == 0

    def test_con_peticiones_en_vuelo_espera_a_que_liberen_margen(self):
        # Dos peticiones de peor caso 0.6 con tope 1: la segunda no cabe MIENTRAS la
        # primera está en vuelo, pero sí cuando ésta termina costando poco.
        motor, _ = _motor(estimar_antes_de_enviar=True, tope_gasto_usd=D("1"))
        assert motor._reservar(D("0.6"), Callbacks())
        resultado = []
        t = threading.Thread(target=lambda: resultado.append(motor._reservar(D("0.6"), Callbacks())))
        t.start()
        time.sleep(0.3)
        assert not resultado  # sigue esperando
        motor._liberar(D("0.6"))
        t.join(timeout=5)
        assert resultado == [True]

    def test_la_parada_despierta_al_que_espera(self):
        motor, _ = _motor(estimar_antes_de_enviar=True, tope_gasto_usd=D("1"))
        motor._reservar(D("0.6"), Callbacks())
        resultado = []
        t = threading.Thread(target=lambda: resultado.append(motor._reservar(D("0.6"), Callbacks())))
        t.start()
        time.sleep(0.2)
        motor.marcar_parada(StopReason.FATAL)
        t.join(timeout=5)
        assert resultado == [False]

    def test_el_canje_libera_la_reserva(self):
        motor, job = _motor(estimar_antes_de_enviar=True)
        motor._reservar(D("0.5"), Callbacks())
        motor.registrar_respuesta(job.messages[0], FakeResponse(), 1, Callbacks(), reserva=D("0.5"))
        assert motor._reservado == 0

    def test_si_registrar_falla_la_reserva_no_se_queda_colgada(self):
        motor, job = _motor(estimar_antes_de_enviar=True)
        motor._reservar(D("0.5"), Callbacks())

        class Rota:
            @property
            def usage(self):
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            motor.registrar_respuesta(job.messages[0], Rota(), 1, Callbacks(), reserva=D("0.5"))
        assert motor._reservado == 0


class TestFinalizar:
    def test_parada_por_tope_justo_al_terminar_es_completado(self):
        motor, job = _motor(n=1, tope_gasto_usd=D("0.001"))
        motor.registrar_respuesta(job.messages[0], FakeResponse(), 1, Callbacks())
        assert motor.stop_reason == StopReason.BUDGET
        assert motor._finalizar() == StopReason.COMPLETED

    def test_parada_por_tope_con_trabajo_pendiente_sigue_siendo_tope(self):
        motor, job = _motor(n=3, tope_gasto_usd=D("0.001"))
        motor.registrar_respuesta(job.messages[0], FakeResponse(), 1, Callbacks())
        assert motor._finalizar() == StopReason.BUDGET

    def test_completado_limpia_el_lote_en_curso(self):
        motor, job = _motor(n=1)
        motor.registrar_respuesta(job.messages[0], FakeResponse(), 1, Callbacks())
        motor._finalizar()
        assert EstadoMonitor.cargar().current_job_id is None

    def test_fallido_conserva_el_lote_en_curso_para_reanudar(self):
        motor, job = _motor(n=1)
        motor.marcar_fallo(job.messages[0], "x", Callbacks(), "server")
        motor.marcar_parada(StopReason.FATAL)
        assert motor._finalizar() == StopReason.FATAL
        assert EstadoMonitor.cargar().current_job_id == job.job_id


class TestCerrarCiclo:
    def test_el_resumen_lista_cada_orden_con_su_estado(self):
        motor, job = _motor(n=3)
        motor.registrar_respuesta(job.messages[0], FakeResponse("buena"), 1, Callbacks())
        motor.marcar_fallo(job.messages[1], "clave inválida", Callbacks(), "auth")
        fin = []
        motor.cerrar_ciclo(1, Callbacks(fin_ciclo=lambda c, t, n: fin.append((c, t, n))))
        resumen = job.resumen_path(1).read_text(encoding="utf-8")
        assert "Completados: 1/3" in resumen
        assert "## Mensaje 1 — completed" in resumen and "buena" in resumen
        assert "## Mensaje 2 — failed" in resumen and "clave inválida" in resumen
        assert "## Mensaje 3 — pending" in resumen
        assert fin == [(1, 3, 1)]


class TestParametrosDeLaPeticion:
    def test_minimos(self):
        p = build_request_params(_cfg(), "hola")
        assert p == {"model": "claude-haiku-4-5", "max_tokens": 1024,
                     "messages": [{"role": "user", "content": "hola"}]}

    def test_system_prompt_con_cache(self):
        p = build_request_params(_cfg(system_prompt="Sé breve."), "hola")
        assert p["system"] == [{"type": "text", "text": "Sé breve.", "cache_control": {"type": "ephemeral"}}]

    def test_system_prompt_sin_cache(self):
        p = build_request_params(_cfg(system_prompt="Sé breve.", usar_cache=False), "hola")
        assert "cache_control" not in p["system"][0]

    def test_system_prompt_en_blanco_no_se_envia(self):
        assert "system" not in build_request_params(_cfg(system_prompt="  \n"), "hola")

    def test_esfuerzo_en_modelo_que_lo_admite(self):
        p = build_request_params(_cfg(modelo="claude-sonnet-5-5", esfuerzo="medium"), "hola")
        assert p["output_config"] == {"effort": "medium"}

    def test_esfuerzo_no_se_envia_a_haiku_45(self):
        # Mandarlo a un modelo que no lo soporta es un 400.
        assert "output_config" not in build_request_params(_cfg(esfuerzo="high"), "hola")

    def test_esfuerzo_en_modelo_desconocido_se_envia(self):
        p = build_request_params(_cfg(modelo="modelo-nuevo", esfuerzo="high"), "hola")
        assert p["output_config"] == {"effort": "high"}

    def test_sin_esfuerzo_no_hay_output_config(self):
        assert "output_config" not in build_request_params(_cfg(modelo="claude-sonnet-5-5"), "hola")


def test_marcar_fallo():
    motor, job = _motor()
    vistos = []
    motor.marcar_fallo(job.messages[0], "boom", Callbacks(resultado=vistos.append), "server")
    m = job.messages[0]
    assert (m.status, m.detalle, m.error_type) == (STATUS_FAILED, "boom", "server")
    assert vistos == [m]
