"""Comportamientos del motor inmediato que no son errores: ritmo, ciclos, streaming, cliente."""

from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core.base import Callbacks, EngineBase, StopReason
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.job import Job
from claude_batch_monitor.state import KEY_API

from .test_engine import FakeResponse
from .test_integration import FakeClient


def preparar(guion=None, n=3, headers=None, **kw):
    base = dict(
        modelo="claude-haiku-4-5", max_tokens=1024, tope_gasto_usd=D("10"),
        precio_input_mtok=D("1"), precio_output_mtok=D("5"), estimar_antes_de_enviar=False,
    )
    base.update(kw)
    config = Configuracion(**base)
    job = Job.crear([f"orden {i}" for i in range(1, n + 1)], config)
    job.save()
    motor = SequentialEngine(config, job)
    motor.notificaciones = False
    motor._client = FakeClient(guion if guion is not None else [FakeResponse()] * (n + 5), headers)
    motor.inicializar_cliente = lambda *a, **k: None
    motor.esperas = []
    def esperar(segundos, cb):
        motor.esperas.append(segundos)
        return not motor.evento_detener.is_set()
    motor.esperar = esperar
    return motor, job


class TestRitmo:
    def test_pausa_de_cortesia_entre_ordenes_pero_no_tras_la_ultima(self):
        motor, _ = preparar(n=4, pausa_entre_mensajes_sec=2)
        motor.ejecutar(Callbacks())
        assert motor.esperas == [2, 2, 2]

    def test_sin_pausa_configurada_no_espera(self):
        motor, _ = preparar(n=4, pausa_entre_mensajes_sec=0)
        motor.ejecutar(Callbacks())
        assert motor.esperas == []

    def test_Z1_frena_antes_de_chocar_si_la_cuota_va_justa(self):
        # Las cabeceras de una respuesta CORRECTA dicen que quedan 5 de 1000 peticiones.
        h = {"anthropic-ratelimit-requests-limit": "1000",
             "anthropic-ratelimit-requests-remaining": "5",
             "anthropic-ratelimit-requests-reset": "50s"}
        motor, job = preparar(n=3, headers=h)
        eventos = []
        motor.ejecutar(Callbacks(espera=lambda m, s: eventos.append(m)))
        assert "cuota" in eventos
        assert job.completed_count() == 3  # frena, pero no falla ni pierde nada

    def test_con_cuota_de_sobra_no_frena(self):
        h = {"anthropic-ratelimit-requests-limit": "1000",
             "anthropic-ratelimit-requests-remaining": "900",
             "anthropic-ratelimit-requests-reset": "50s"}
        motor, _ = preparar(n=3, headers=h)
        eventos = []
        motor.ejecutar(Callbacks(espera=lambda m, s: eventos.append(m)))
        assert eventos == []


class TestCiclos:
    def test_modo_bucle_repite_el_lote_y_guarda_cada_ciclo(self):
        motor, job = preparar(n=2, modo_continuo=True, hora_limite="23:59", pausa_ciclos_sec=1)
        def al_acabar_el_ciclo(completados, total, ciclo):
            if ciclo == 2:
                motor.detener()
        assert motor.ejecutar(Callbacks(fin_ciclo=al_acabar_el_ciclo)) == StopReason.COMPLETED
        assert job.ciclos_completados == 1
        assert job.response_path(1, 1).exists() and job.response_path(1, 2).exists()
        assert job.resumen_path(1).exists() and job.resumen_path(2).exists()
        assert job.coste_total() == D("0.014")  # 2 ciclos x 2 órdenes x $0.0035

    def test_un_solo_ciclo_si_no_hay_bucle(self):
        motor, job = preparar(n=2)
        motor.ejecutar(Callbacks())
        assert job.ciclos_completados == 0 and not job.response_path(1, 2).exists()


class TestHoraLimiteYParada:
    def test_hora_limite_ya_pasada_no_envia_nada(self):
        motor, job = preparar(n=3)
        motor.calcular_deadline = lambda: datetime.now() - timedelta(seconds=1)
        assert motor.ejecutar(Callbacks()) == StopReason.DEADLINE
        assert motor._client.messages.llamadas == 0 and len(job.pendientes()) == 3

    def test_el_lote_se_puede_reanudar_tras_la_hora_limite(self):
        motor, job = preparar(n=3)
        motor.calcular_deadline = lambda: datetime.now() - timedelta(seconds=1)
        motor.ejecutar(Callbacks())
        motor2, _ = preparar(n=3)
        motor2.job = job
        assert motor2.ejecutar(Callbacks()) == StopReason.COMPLETED

    def test_detener_a_mitad_conserva_lo_hecho(self):
        motor, job = preparar(n=5)
        def tras_la_segunda(msg):
            if msg.id == 2:
                motor.detener()
        assert motor.ejecutar(Callbacks(resultado=tras_la_segunda)) == StopReason.STOPPED
        assert job.completed_count() == 2 and len(job.pendientes()) == 3


class TestCliente:
    def test_sin_clave_falla_con_un_mensaje_claro(self):
        config = Configuracion()
        job = Job.crear(["a"], config)
        motor = SequentialEngine(config, job)
        motor.notificaciones = False
        errores = []
        assert motor.ejecutar(Callbacks(error=errores.append)) == StopReason.FATAL
        assert "API key" in errores[0] and "ANTHROPIC_API_KEY" in errores[0]

    def test_con_clave_del_entorno_construye_el_cliente_real_sin_reintentos_propios(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-prueba")
        config = Configuracion(timeout_api_sec=77)
        motor = EngineBase(config, Job.crear(["a"], config))
        motor.inicializar_cliente()
        assert motor.client.timeout == 77.0 and motor.client.max_retries == 0
        assert motor.client.api_key == "sk-ant-prueba"

    def test_la_clave_del_llavero_tambien_sirve(self, llavero, monkeypatch):
        llavero.set_password("claude_monitor", KEY_API, "sk-ant-llavero")
        config = Configuracion()
        motor = EngineBase(config, Job.crear(["a"], config))
        motor.inicializar_cliente()
        assert motor.client.api_key == "sk-ant-llavero"

    def test_acceder_al_cliente_sin_inicializar_da_un_error_explicito(self):
        config = Configuracion()
        motor = EngineBase(config, Job.crear(["a"], config))
        with pytest.raises(RuntimeError):
            _ = motor.client

    def test_el_cliente_se_cierra_al_terminar(self):
        motor, _ = preparar(n=1)
        cerrado = []
        motor._client.close = lambda: cerrado.append(True)
        motor.ejecutar(Callbacks())
        assert cerrado == [True]

    def test_un_close_que_falla_no_rompe_nada(self):
        motor, _ = preparar(n=1)
        def boom():
            raise RuntimeError("x")
        motor._client.close = boom
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED


class TestInformeDeEstado:
    def test_el_motor_informa_de_cada_envio(self):
        motor, _ = preparar(n=2)
        textos, ordenes = [], []
        motor.ejecutar(Callbacks(estado=lambda t, a: textos.append(t), inicio_orden=lambda m: ordenes.append(m.id)))
        assert ordenes == [1, 2]
        assert any("Orden 1/2" in t for t in textos)
