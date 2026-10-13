"""El ejecutor de un lote: hilo aparte, eventos en orden y un único «terminado» al final."""

import threading
import time
from decimal import Decimal as D

from claude_batch_monitor import ejecutor as modulo
from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core import base
from claude_batch_monitor.core.base import StopReason
from claude_batch_monitor.core.batch import BatchEngine
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.ejecutor import EjecutorLote, EventosLote, motor_para
from claude_batch_monitor.job import STATUS_COMPLETED, Job

from .test_cola import fabrica_con, hasta
from .test_engine import FakeResponse
from .test_errors import TOPE_MENSUAL, error, interno
from .test_integration import status_error


class Registro(EventosLote):
    def __init__(self):
        self.eventos = []
        self.hilos = set()

    def _anota(self, *e):
        self.hilos.add(threading.current_thread().name)
        self.eventos.append(e)

    def estado(self, texto, activo): self._anota("estado", texto)
    def inicio_orden(self, msg): self._anota("inicio", msg.id)
    def resultado(self, msg): self._anota("resultado", msg.id, msg.status)
    def fin_ciclo(self, c, t, n): self._anota("fin_ciclo", c, t, n)
    def error(self, texto): self._anota("error", texto)
    def tick(self, total, restante): self._anota("tick")
    def gasto(self, usd, consumo): self._anota("gasto", usd)
    def terminado(self, razon, job): self._anota("terminado", razon)

    def tipos(self):
        return [e[0] for e in self.eventos]

    def terminadas(self):
        return [e for e in self.eventos if e[0] == "terminado"]


def cfg(**kw):
    base_ = dict(modelo="claude-haiku-4-5", max_tokens=512, tope_gasto_usd=D("5"),
                 precio_input_mtok=D("1"), precio_output_mtok=D("5"))
    base_.update(kw)
    return Configuracion(**base_)


def lote(n=3, **kw):
    config = cfg(**kw)
    job = Job.crear([f"orden {i}" for i in range(1, n + 1)], config)
    job.save()
    return config, job


class TestEjecucion:
    def test_un_lote_se_ejecuta_y_cuenta_todo_en_orden(self):
        config, job = lote(3)
        ev, ex = Registro(), EjecutorLote(fabrica=fabrica_con([FakeResponse("ok")] * 5))
        assert ex.iniciar(config, job, ev) is True
        assert ex.esperar(8)
        assert [e[2] for e in ev.eventos if e[0] == "resultado"] == [STATUS_COMPLETED] * 3
        assert ev.terminadas() == [("terminado", StopReason.COMPLETED)]
        assert ev.tipos()[-1] == "terminado"            # siempre lo último
        assert "gasto" in ev.tipos() and "fin_ciclo" in ev.tipos()
        assert job.completed_count() == 3 and not ex.activo

    def test_el_trabajo_ocurre_en_un_hilo_aparte(self):
        config, job = lote(1)
        ev, ex = Registro(), EjecutorLote(fabrica=fabrica_con([FakeResponse()]))
        ex.iniciar(config, job, ev)
        ex.esperar(8)
        assert ev.hilos == {"lote"}

    def test_los_eventos_pasan_por_publicar(self):
        # En la ventana, `publicar` los lleva al hilo de tkinter; aquí se comprueba que TODO pasa por ahí.
        pasados = []
        config, job = lote(2)
        ev = Registro()
        ex = EjecutorLote(publicar=lambda f: (pasados.append(f), f())[1],
                          fabrica=fabrica_con([FakeResponse()] * 3))
        ex.iniciar(config, job, ev)
        ex.esperar(8)
        assert len(pasados) == len(ev.eventos) > 0

    def test_no_se_lanzan_dos_lotes_a_la_vez(self):
        suelta = threading.Event()
        fabrica = fabrica_con([FakeResponse()] * 5)
        original = fabrica.cliente.messages._siguiente

        def lenta():
            suelta.wait(5)
            return original()

        fabrica.cliente.messages._siguiente = lenta
        config, job = lote(1)
        otro_config, otro_job = lote(1)
        ex = EjecutorLote(fabrica=fabrica)
        ev = Registro()
        assert ex.iniciar(config, job, ev)
        assert hasta(lambda: ex.activo)
        assert ex.iniciar(otro_config, otro_job, Registro()) is False
        assert len(fabrica.motores) == 1                 # el segundo ni siquiera se preparó
        suelta.set()
        assert ex.esperar(8)
        assert ev.terminadas() == [("terminado", StopReason.COMPLETED)]

    def test_se_puede_lanzar_otro_cuando_el_anterior_acabo(self):
        fabrica = fabrica_con([FakeResponse()] * 10)
        ex = EjecutorLote(fabrica=fabrica)
        for _ in range(2):
            config, job = lote(1)
            ev = Registro()
            assert ex.iniciar(config, job, ev)
            assert ex.esperar(8)
            assert ev.terminadas() == [("terminado", StopReason.COMPLETED)]

    def test_las_notificaciones_se_pueden_apagar(self):
        config, job = lote(1)
        fabrica = fabrica_con([FakeResponse()])
        ex = EjecutorLote(fabrica=fabrica)
        ex.notificaciones = False
        ex.iniciar(config, job, Registro())
        ex.esperar(8)
        assert fabrica.motores[0].notificaciones is False


class TestParadas:
    def test_detener_termina_la_peticion_en_curso_y_no_envia_mas(self):
        entrada, suelta = threading.Event(), threading.Event()
        fabrica = fabrica_con([FakeResponse()] * 10)
        original = fabrica.cliente.messages._siguiente

        def lenta():
            entrada.set()
            suelta.wait(5)
            return original()

        fabrica.cliente.messages._siguiente = lenta
        config, job = lote(4)
        ev, ex = Registro(), EjecutorLote(fabrica=fabrica)
        ex.iniciar(config, job, ev)
        assert entrada.wait(5)
        ex.detener()
        suelta.set()
        assert ex.esperar(8)
        assert ev.terminadas() == [("terminado", StopReason.STOPPED)]
        assert job.completed_count() == 1 and len(job.pendientes()) == 3   # lo demás queda para reanudar

    def test_detener_sin_lote_no_hace_nada(self):
        EjecutorLote().detener()

    def test_el_tope_de_gasto_se_comunica(self):
        config, job = lote(5, tope_gasto_usd=D("0.004"))
        ev, ex = Registro(), EjecutorLote(fabrica=fabrica_con([FakeResponse()] * 10))
        ex.iniciar(config, job, ev)
        ex.esperar(8)
        assert ev.terminadas() == [("terminado", StopReason.BUDGET)]

    def test_sin_saldo(self):
        config, job = lote(2)
        ev, ex = Registro(), EjecutorLote(fabrica=fabrica_con([status_error(402)]))
        ex.iniciar(config, job, ev)
        ex.esperar(8)
        assert ev.terminadas() == [("terminado", StopReason.NO_CREDIT)]
        assert len(job.pendientes()) == 2               # no se marca como fallado: se reanuda tras recargar

    def test_limite_de_la_cuenta(self):
        config, job = lote(2)
        ev, ex = Registro(), EjecutorLote(fabrica=fabrica_con([error(429, interno(TOPE_MENSUAL))]))
        ex.iniciar(config, job, ev)
        ex.esperar(8)
        assert ev.terminadas() == [("terminado", StopReason.ACCOUNT_LIMIT)]
        assert any(e[0] == "error" for e in ev.eventos)


class TestFallos:
    def test_si_la_fabrica_falla_se_dice_y_se_termina(self):
        def mala(config, job):
            raise RuntimeError("no hay motor")

        config, job = lote(1)
        ev, ex = Registro(), EjecutorLote(fabrica=mala)
        assert ex.iniciar(config, job, ev) is True
        assert ev.tipos() == ["error", "terminado"]
        assert "no hay motor" in ev.eventos[0][1] and ev.terminadas() == [("terminado", StopReason.FATAL)]
        assert not ex.activo

    def test_un_fallo_inesperado_del_motor_no_deja_la_ventana_esperando(self):
        class Roto:
            notificaciones = True

            def ejecutar(self, cb):
                raise RuntimeError("explotó")

            def detener(self):
                pass

        config, job = lote(1)
        ev, ex = Registro(), EjecutorLote(fabrica=lambda c, j: Roto())
        ex.iniciar(config, job, ev)
        ex.esperar(8)
        assert ev.tipos() == ["error", "terminado"] and ev.terminadas()[0][1] is StopReason.FATAL
        assert ex.motor is None and not ex.activo

    def test_un_evento_que_falla_no_tumba_el_lote(self):
        class Malo(Registro):
            def resultado(self, msg):
                raise ValueError("ventana destruida")

        config, job = lote(2)
        ev, ex = Malo(), EjecutorLote(fabrica=fabrica_con([FakeResponse()] * 3))
        ex.iniciar(config, job, ev)
        ex.esperar(8)
        assert ev.terminadas() == [("terminado", StopReason.COMPLETED)] and job.completed_count() == 2

    def test_el_ahorro_de_energia_se_activa_y_se_suelta_en_el_hilo_del_lote(self, monkeypatch):
        hilos = []
        monkeypatch.setattr(modulo.KeepAwake, "enable", lambda self: hilos.append(threading.current_thread().name))
        monkeypatch.setattr(modulo.KeepAwake, "disable", lambda self: hilos.append(threading.current_thread().name))
        config, job = lote(1)
        ex = EjecutorLote(fabrica=fabrica_con([FakeResponse()]))
        ex.iniciar(config, job, Registro())
        ex.esperar(8)
        assert hilos == ["lote", "lote"]


class TestMotorPara:
    def test_el_modo_decide_el_motor(self):
        _, job = lote(1)
        assert isinstance(motor_para(cfg(modo="inmediato"), job), SequentialEngine)
        assert isinstance(motor_para(cfg(modo="lote"), job), BatchEngine)

    def test_esperar_sin_lote_devuelve_true(self):
        assert EjecutorLote().esperar(0.1) is True


def test_el_ejecutor_no_guarda_el_motor_tras_terminar():
    config, job = lote(1)
    ex = EjecutorLote(fabrica=fabrica_con([FakeResponse()]))
    ex.iniciar(config, job, Registro())
    ex.esperar(8)
    assert ex.motor is None
    time.sleep(0)  # el hilo ya terminó; nada que esperar
    assert base.StopReason.COMPLETED.value == "completed"


class TestCancelarElLote:
    """Se para el motor al momento y se cancela en segundo plano (el motor ya sabe usar un cliente propio si el suyo se cerró)."""

    class MotorFalso:
        def __init__(self, job, orden, ok=True):
            self.job, self.orden, self.ok, self.notificaciones = job, orden, ok, False

        def cancelar_lote(self):
            self.orden.append("cancelar")
            return self.ok

        def detener(self):
            self.orden.append("detener")

    def _ejecutor_con(self, motor):
        ej = EjecutorLote()
        ej._motor = motor
        return ej

    def test_se_para_al_momento_y_se_cancela_en_segundo_plano_avisando_del_resultado(self):
        job = Job.crear(["a"], cfg(modo="lote"))
        job.batch_id = "msgbatch_x"
        orden, avisos = [], []
        ej = self._ejecutor_con(self.MotorFalso(job, orden))
        assert ej.cancelar_lote(avisos.append) is True
        hasta(lambda: avisos)
        assert orden == ["detener", "cancelar"] and avisos == [True]

    def test_si_el_servidor_no_puede_se_dice(self):
        job = Job.crear(["a"], cfg(modo="lote"))
        job.batch_id = "msgbatch_x"
        orden, avisos = [], []
        ej = self._ejecutor_con(self.MotorFalso(job, orden, ok=False))
        ej.cancelar_lote(avisos.append)
        hasta(lambda: avisos)
        assert orden == ["detener", "cancelar"] and avisos == [False]

    def test_si_la_cancelacion_revienta_se_dice_que_no_pudo(self):
        job = Job.crear(["a"], cfg(modo="lote"))
        job.batch_id = "msgbatch_x"
        orden, avisos = [], []
        motor = self.MotorFalso(job, orden)

        def revienta():
            orden.append("cancelar")
            raise RuntimeError("boom")

        motor.cancelar_lote = revienta
        self._ejecutor_con(motor).cancelar_lote(avisos.append)
        hasta(lambda: avisos)
        assert orden == ["detener", "cancelar"] and avisos == [False]

    def test_sin_lote_en_el_servidor_no_hace_nada(self):
        job = Job.crear(["a"], cfg(modo="lote"))
        orden = []
        assert self._ejecutor_con(self.MotorFalso(job, orden)).cancelar_lote() is False and orden == []

    def test_sin_motor_en_marcha_no_hace_nada(self):
        assert EjecutorLote().cancelar_lote() is False
