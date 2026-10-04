"""La cola del modo simple: se envía sola, espera cuando hace falta y sobrevive a cerrar el programa."""

import threading
import time
from dataclasses import FrozenInstanceError
from decimal import Decimal as D

import anthropic
import pytest

from claude_batch_monitor.cola import ETIQUETA, Cola, EstadoCola, EventosCola
from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core import base
from claude_batch_monitor.core.base import StopReason
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.job import (
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_SKIPPED,
    Job,
)

from ._http import httpx
from .test_engine import FakeResponse
from .test_errors import TOPE_MENSUAL, error, interno
from .test_integration import FakeClient, status_error


class Registro(EventosCola):
    """Guarda todo lo que la cola cuenta, para poder afirmar sobre ello."""

    def __init__(self):
        self.estados, self.ordenes, self.cuentas, self.deltas, self.errores, self.gastos = [], [], [], [], [], []

    def estado(self, e): self.estados.append(e)
    def orden_cambiada(self, m): self.ordenes.append((m.id, m.status))
    def cuenta_atras(self, r): self.cuentas.append(r)
    def delta(self, i, t): self.deltas.append((i, t))
    def error(self, t): self.errores.append(t)
    def gasto(self, u): self.gastos.append(u)

    def tipos(self):
        return [e.tipo for e in self.estados]


def hasta(condicion, timeout=8.0):
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if condicion():
            return True
        time.sleep(0.01)
    return False


def fabrica_con(guion, **atributos):
    """Fábrica de motores con un cliente falso COMPARTIDO entre ejecuciones."""
    cliente = FakeClient(guion)
    motores = []

    def fabricar(config, job):
        m = SequentialEngine(config, job)
        m.notificaciones = False
        m._client = cliente
        m.inicializar_cliente = lambda *a, **k: None
        m.espera_error_servidor = lambda e, i: 0.01
        for k, v in atributos.items():
            setattr(m, k, v)
        motores.append(m)
        return m

    fabricar.cliente, fabricar.motores = cliente, motores
    return fabricar


def cfg(**kw):
    base_ = dict(modelo="claude-haiku-4-5", max_tokens=512, tope_gasto_usd=D("5"),
                 precio_input_mtok=D("1"), precio_output_mtok=D("5"))
    base_.update(kw)
    return Configuracion(**base_)


@pytest.fixture
def margen_corto(monkeypatch):
    monkeypatch.setattr(base, "MARGEN_RATE_LIMIT_SEC", 0.0)


class TestEnvioAutomatico:
    def test_una_pregunta_se_envia_sola_y_queda_completada(self):
        ev = Registro()
        cola = Cola(cfg(), ev, fabrica=fabrica_con([FakeResponse("la respuesta")] * 3))
        msg = cola.agregar("¿Qué es un token?")
        assert msg.id == 1
        assert hasta(lambda: msg.status == STATUS_COMPLETED)
        assert hasta(lambda: not cola.activa)
        assert (1, STATUS_COMPLETED) in ev.ordenes and ev.estados[-1].tipo == "idle"
        assert "la respuesta" in cola.job.response_path(1, 1).read_text(encoding="utf-8")

    def test_las_preguntas_se_envian_en_orden(self):
        cola = Cola(cfg(), fabrica=fabrica_con([FakeResponse()] * 10))
        for t in ("uno", "dos", "tres"):
            cola.agregar(t)
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)
        assert [m.text for m in cola.preguntas()] == ["uno", "dos", "tres"]
        assert all(m.status == STATUS_COMPLETED for m in cola.preguntas())

    def test_se_puede_preguntar_mientras_se_envia_otra(self):
        # Una sola petición en vuelo a la vez: la segunda espera su turno y no se pierde.
        suelta = threading.Event()
        cliente = FakeClient([FakeResponse("a"), FakeResponse("b")])
        original = cliente.messages._siguiente
        entradas = []
        def bloqueante():
            entradas.append(time.monotonic())
            if len(entradas) == 1:
                suelta.wait(5)
            return original()
        cliente.messages._siguiente = bloqueante
        def fab(config, job):
            m = SequentialEngine(config, job)
            m.notificaciones = False
            m._client = cliente
            m.inicializar_cliente = lambda *a, **k: None
            return m
        cola = Cola(cfg(), fabrica=fab)
        cola.agregar("primera")
        assert hasta(lambda: len(entradas) == 1)
        cola.agregar("segunda")         # llega con la primera todavía en vuelo
        assert len(entradas) == 1
        suelta.set()
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)
        assert [m.status for m in cola.preguntas()] == [STATUS_COMPLETED, STATUS_COMPLETED]
        assert cliente.messages.llamadas == 2

    def test_una_pregunta_vacia_se_ignora(self):
        cola = Cola(cfg(), fabrica=fabrica_con([]))
        assert cola.agregar("   \n ") is None and cola.job is None

    def test_el_gasto_y_el_estado_se_cuentan(self):
        ev = Registro()
        cola = Cola(cfg(), ev, fabrica=fabrica_con([FakeResponse()] * 2))
        cola.agregar("a")
        cola.agregar("b")
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)
        assert cola.gastado() == D("0.0070") and ev.gastos[-1] == D("0.0070")
        assert "sending" in ev.tipos()

    def test_streaming_y_ajustes_del_modo_simple(self):
        # El modo simple fuerza: inmediato, un hilo, streaming, sin pausas, con estimación previa.
        fab = fabrica_con([FakeResponse()])
        cola = Cola(cfg(modo="lote", concurrencia=8, pausa_entre_mensajes_sec=30), fabrica=fab)
        cola.agregar("a")
        assert hasta(lambda: cola.pendientes() == 0)
        c = fab.motores[0].config
        assert (c.modo, c.concurrencia, c.streaming, c.pausa_entre_mensajes_sec, c.estimar_antes_de_enviar) == (
            "inmediato", 1, True, 0, True)


class TestEsperasAutomaticas:
    """Lo que la persona describió: si la IA llega a su límite, la cola espera y sigue sola."""

    def test_rate_limit_la_cola_espera_y_continua_sin_intervencion(self, margen_corto):
        resp = httpx.Response(429, request=httpx.Request("POST", "https://x"), headers={"retry-after": "0.3"})
        limite = anthropic.RateLimitError("rl", response=resp, body=None)
        ev = Registro()
        cola = Cola(cfg(), ev, fabrica=fabrica_con([limite, FakeResponse("tras la espera"), FakeResponse()]))
        cola.agregar("a")
        cola.agregar("b")
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)
        esperas = [e for e in ev.estados if e.tipo == "waiting"]
        assert esperas and esperas[0].motivo == "rate_limit" and esperas[0].segundos == pytest.approx(0.3)
        assert ev.cuentas and ev.cuentas[0] <= 0.3        # hay cuenta atrás para enseñar
        assert all(m.status == STATUS_COMPLETED for m in cola.preguntas())
        assert cola.preguntas()[0].errores == 0           # esperar no es fallar

    def test_servidor_saturado_reintenta_solo(self):
        ev = Registro()
        cola = Cola(cfg(), ev, fabrica=fabrica_con([status_error(529), FakeResponse()]))
        cola.agregar("a")
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)
        assert any(e.tipo == "waiting" and e.motivo == "server" for e in ev.estados)

    def test_sin_conexion_espera_a_que_vuelva(self):
        sin_red = anthropic.APIConnectionError(message="x", request=httpx.Request("POST", "https://x"))
        sin_red.__cause__ = httpx.ConnectError("sin ruta")
        ev = Registro()
        fab = fabrica_con([sin_red, FakeResponse()])
        cola = Cola(cfg(), ev, fabrica=lambda c, j: _ligero(fab(c, j)))
        cola.agregar("a")
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa, timeout=15)
        assert any(e.tipo == "waiting" and e.motivo == "sin_conexion" for e in ev.estados)


def _ligero(motor):
    """Espera real de 0.05 s en vez de los 5 s mínimos de 'sin conexión'."""
    original = motor.esperar
    motor.esperar = lambda s, cb: original(min(s, 0.05), cb)
    return motor


class TestParadasQueNoSeReintentan:
    def test_tope_de_gasto_para_la_cola_y_se_puede_seguir_subiendolo(self):
        ev = Registro()
        fab = fabrica_con([FakeResponse()] * 10)
        cola = Cola(cfg(tope_gasto_usd=D("0.005")), ev, fabrica=fab)
        for t in "abcd":
            cola.agregar(t)
        assert hasta(lambda: not cola.activa)
        assert cola.estado.tipo == "stopped" and cola.estado.razon is StopReason.BUDGET
        hechas = sum(m.status == STATUS_COMPLETED for m in cola.preguntas())
        assert 0 < hechas < 4 and cola.pendientes() == 4 - hechas
        # La persona sube el tope en Opciones y pulsa Reanudar.
        cola.actualizar_config(cfg(tope_gasto_usd=D("5")))
        cola.reanudar()
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)

    def test_tope_mensual_de_la_cuenta_no_se_queda_reintentando(self):
        ev = Registro()
        fab = fabrica_con([error(429, interno(TOPE_MENSUAL))])
        cola = Cola(cfg(), ev, fabrica=fab)
        cola.agregar("a")
        assert hasta(lambda: not cola.activa)
        assert cola.estado.razon is StopReason.ACCOUNT_LIMIT
        assert fab.cliente.messages.llamadas == 1
        assert "2026-09-01" in cola.ultimo_error and cola.preguntas()[0].status == STATUS_PENDING

    def test_sin_saldo_deja_la_pregunta_pendiente_y_se_reanuda_al_recargar(self):
        guion = [status_error(402), FakeResponse("ya hay saldo")]
        fab = fabrica_con(guion)
        cola = Cola(cfg(), fabrica=fab)
        cola.agregar("a")
        assert hasta(lambda: not cola.activa)
        assert cola.estado.razon is StopReason.NO_CREDIT and cola.pendientes() == 1
        cola.reanudar()
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)

    def test_clave_invalida_marca_la_pregunta_fallida_y_reanudar_la_reintenta(self):
        fab = fabrica_con([status_error(401), FakeResponse()])
        cola = Cola(cfg(), fabrica=fab)
        cola.agregar("a")
        assert hasta(lambda: not cola.activa)
        assert cola.estado.razon is StopReason.FATAL and cola.preguntas()[0].error_type == "auth"
        cola.reanudar()  # tras cambiar la clave
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)
        assert cola.preguntas()[0].status == STATUS_COMPLETED

    def test_un_fallo_inesperado_al_crear_el_motor_no_cuelga_la_cola(self):
        def rota(config, job):
            raise RuntimeError("no se pudo crear el motor")
        ev = Registro()
        cola = Cola(cfg(), ev, fabrica=rota)
        cola.agregar("a")
        assert hasta(lambda: not cola.activa)
        # La ventana se entera: hay mensaje y estado, y la pregunta sigue pendiente.
        assert cola.estado.tipo == "stopped" and cola.estado.razon is StopReason.FATAL
        assert "no se pudo crear el motor" in ev.errores[0] and cola.pendientes() == 1


class TestPausa:
    def test_pausar_detiene_el_envio_y_reanudar_lo_retoma(self):
        ev = Registro()
        cola = Cola(cfg(), ev, fabrica=fabrica_con([FakeResponse()] * 20))
        cola.pausar()                     # pausada de antemano: las preguntas esperan
        cola.agregar("a")
        cola.agregar("b")
        time.sleep(0.2)
        assert cola.pendientes() == 2 and not cola.activa and cola.estado.tipo == "paused"
        cola.reanudar()
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)

    def test_pausar_durante_una_espera_la_interrumpe_enseguida(self, margen_corto):
        resp = httpx.Response(429, request=httpx.Request("POST", "https://x"), headers={"retry-after": "30"})
        limite = anthropic.RateLimitError("rl", response=resp, body=None)
        ev = Registro()
        cola = Cola(cfg(), ev, fabrica=fabrica_con([limite] * 5 + [FakeResponse()]))
        cola.agregar("a")
        assert hasta(lambda: cola.estado.tipo == "waiting")
        t0 = time.monotonic()
        cola.pausar()
        assert hasta(lambda: not cola.activa, timeout=3) and time.monotonic() - t0 < 3
        assert cola.estado.tipo == "paused" and cola.pendientes() == 1


class TestPersistencia:
    def test_lo_pendiente_sobrevive_a_cerrar_el_programa(self):
        c1 = Cola(cfg(), fabrica=fabrica_con([]))
        c1.pausar()
        c1.agregar("quedó sin enviar")
        c1.cerrar()

        c2 = Cola(cfg(), fabrica=fabrica_con([FakeResponse("ahora sí")]))
        assert c2.cargar_ultima() == 1
        assert c2.preguntas()[0].text == "quedó sin enviar"
        c2.reanudar()
        assert hasta(lambda: c2.pendientes() == 0 and not c2.activa)
        assert "ahora sí" in c2.job.response_path(1, 1).read_text(encoding="utf-8")

    def test_la_cola_se_recupera_por_su_etiqueta_y_no_confunde_lotes_normales(self):
        lote = Job.crear(["a", "b"], cfg())
        lote.save()
        assert Cola(cfg()).cargar_ultima() == 0
        c = Cola(cfg(), fabrica=fabrica_con([]))
        c.pausar()
        c.agregar("x")
        assert Job.load(c.job.job_id).etiqueta == ETIQUETA

    def test_cerrar_con_envio_en_curso_no_pierde_nada(self):
        suelta = threading.Event()
        cliente = FakeClient([FakeResponse()])
        original = cliente.messages._siguiente
        en_vuelo = threading.Event()
        def bloqueante():
            en_vuelo.set()
            suelta.wait(5)
            return original()
        cliente.messages._siguiente = bloqueante
        def fab(config, job):
            m = SequentialEngine(config, job)
            m.notificaciones = False
            m._client = cliente
            m.inicializar_cliente = lambda *a, **k: None
            return m
        cola = Cola(cfg(), fabrica=fab)
        cola.agregar("a")
        cola.agregar("b")
        assert en_vuelo.wait(5)             # la primera petición YA salió
        threading.Timer(0.2, suelta.set).start()
        cola.cerrar(timeout=5)
        assert not cola.activa
        assert cola.preguntas()[0].status == STATUS_COMPLETED       # la que iba en vuelo se guardó
        assert Job.load(cola.job.job_id).pendientes()                # y la otra seguirá mañana


class TestGestionDeLaLista:
    def test_quitar_una_pendiente(self):
        cola = Cola(cfg(), fabrica=fabrica_con([FakeResponse()] * 3))
        cola.pausar()
        a = cola.agregar("a")
        b = cola.agregar("b")
        assert cola.quitar(a.id) is True and a.status == STATUS_SKIPPED
        cola.reanudar()
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)
        assert b.status == STATUS_COMPLETED and a.status == STATUS_SKIPPED

    def test_no_se_puede_quitar_la_que_ya_se_envio(self):
        cola = Cola(cfg(), fabrica=fabrica_con([FakeResponse()]))
        m = cola.agregar("a")
        assert hasta(lambda: m.status == STATUS_COMPLETED)
        assert cola.quitar(m.id) is False and cola.quitar(999) is False

    def test_nueva_cola_solo_en_reposo(self):
        cola = Cola(cfg(), fabrica=fabrica_con([FakeResponse()] * 3))
        cola.agregar("a")
        assert hasta(lambda: not cola.activa)
        viejo = cola.job.job_id
        assert cola.nueva() is True and cola.job is None and cola.gastado() == 0
        cola.agregar("b")
        assert cola.job.job_id != viejo and Job.load(viejo) is not None  # la anterior sigue en disco

    def test_un_callback_de_la_interfaz_que_falla_no_rompe_la_cola(self):
        class Rota(EventosCola):
            def orden_cambiada(self, m): raise RuntimeError("la ventana explotó")
            def estado(self, e): raise RuntimeError("otra vez")
        cola = Cola(cfg(), Rota(), fabrica=fabrica_con([FakeResponse()]))
        m = cola.agregar("a")
        assert hasta(lambda: m.status == STATUS_COMPLETED and not cola.activa)

    def test_los_eventos_pasan_por_el_publicador(self):
        # En la ventana, `publicar` es el puente que lleva el evento al hilo de la interfaz.
        hilos = set()
        def publicar(f):
            hilos.add(threading.current_thread().name)
            f()
        cola = Cola(cfg(), Registro(), publicar=publicar, fabrica=fabrica_con([FakeResponse()]))
        cola.agregar("a")
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)
        assert "cola-de-preguntas" in hilos     # los del motor salen del hilo de la cola


def test_el_estado_es_inmutable():
    with pytest.raises(FrozenInstanceError):
        EstadoCola().tipo = "otro"  # type: ignore[misc]


class TestSuspensionDelEquipo:
    def test_el_equipo_no_se_duerme_mientras_la_cola_trabaja(self, monkeypatch):
        from claude_batch_monitor import cola as modulo
        eventos = []
        monkeypatch.setattr(modulo.KeepAwake, "enable", lambda self: eventos.append(("on", threading.current_thread().name)))
        monkeypatch.setattr(modulo.KeepAwake, "disable", lambda self: eventos.append(("off", threading.current_thread().name)))
        cola = Cola(cfg(), fabrica=fabrica_con([FakeResponse()] * 3))
        cola.agregar("una")
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)
        # Activado y soltado en el hilo de la cola (en Windows el estado de ejecución es por hilo).
        assert eventos == [("on", "cola-de-preguntas"), ("off", "cola-de-preguntas")]

    def test_se_suelta_aunque_el_motor_reviente(self, monkeypatch):
        from claude_batch_monitor import cola as modulo
        eventos = []
        monkeypatch.setattr(modulo.KeepAwake, "enable", lambda self: eventos.append("on"))
        monkeypatch.setattr(modulo.KeepAwake, "disable", lambda self: eventos.append("off"))

        class Roto:
            notificaciones = True

            def ejecutar(self, cb):
                raise RuntimeError("explotó")

            def detener(self):
                pass

        cola = Cola(cfg(), fabrica=lambda c, j: Roto())
        cola.agregar("una")
        assert hasta(lambda: not cola.activa)
        assert eventos == ["on", "off"]
