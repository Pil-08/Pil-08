"""La cola del modo simple, con el SDK real contra el servidor falso (sin ventanas).

Es lo que hay debajo de la ventana sencilla: una persona va escribiendo preguntas y la cola las envía sola, espera si
hay que esperar y sigue donde se quedó. Recuerda que el modo simple envía SIEMPRE en streaming.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from decimal import Decimal as D

import pytest

from claude_batch_monitor.cola import Cola, EstadoCola, EventosCola
from claude_batch_monitor.core.base import StopReason
from claude_batch_monitor.export import leer_respuesta
from claude_batch_monitor.job import STATUS_COMPLETED, STATUS_PENDING, STATUS_SKIPPED

from .servidor_falso import sin_marca
from .simulacros_comun import configuracion

pytestmark = pytest.mark.simulacro


class Registro(EventosCola):
    """Apunta todo lo que la cola le cuenta a la interfaz."""

    def __init__(self):
        self.estados: list[EstadoCola] = []
        self.ordenes: dict[int, list[str]] = {}
        self.deltas: dict[int, str] = {}
        self.errores: list[str] = []
        self.cuentas_atras: list[float] = []
        self.gastos: list[D] = []

    def orden_cambiada(self, msg):
        self.ordenes.setdefault(msg.id, []).append(msg.status)

    def estado(self, estado):
        self.estados.append(estado)

    def cuenta_atras(self, restante):
        self.cuentas_atras.append(restante)

    def delta(self, msg_id, texto):
        self.deltas[msg_id] = self.deltas.get(msg_id, "") + texto

    def gasto(self, usd):
        self.gastos.append(usd)

    def error(self, texto):
        self.errores.append(texto)

    def tipos(self) -> list[str]:
        return [e.tipo for e in self.estados]


@pytest.fixture
def hacer_cola(api, esperas):
    colas: list[Cola] = []

    def hacer(**config):
        registro = Registro()
        cola = Cola(configuracion(**config), registro)
        cola.notificaciones = False
        colas.append(cola)
        return cola, registro

    yield hacer
    for cola in colas:
        cola.cerrar(timeout=10)


def hasta(condicion, segundos=30, mensaje="no ocurrió a tiempo"):
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        if condicion():
            return
        time.sleep(0.02)
    raise AssertionError(mensaje)


def en_reposo(cola):
    return not cola.activa


def terminadas(cola, n):
    return lambda: en_reposo(cola) and sum(m.status == STATUS_COMPLETED for m in cola.preguntas()) >= n


class TestFlujoNormal:
    def test_las_preguntas_se_envian_una_a_una_y_se_contestan_en_directo(self, api, hacer_cola):
        cola, ev = hacer_cola()
        for i in range(1, 5):
            cola.agregar(f"Pregunta de la cola {i}")
        hasta(terminadas(cola, 4))
        assert [m.status for m in cola.preguntas()] == [STATUS_COMPLETED] * 4
        for m in cola.preguntas():
            assert f"Respuesta a: Pregunta de la cola {m.id}" in leer_respuesta(cola.job, m.id, 1)
            assert "Respuesta a:" in ev.deltas[m.id]                  # el texto fue llegando por trozos
        assert ev.tipos()[-1] == "idle" and "sending" in ev.tipos()
        assert len(api.cobros) == 4 and cola.gastado() > 0 and ev.gastos
        assert all(p["cuerpo"].get("stream") is True for p in api.peticiones_a("/v1/messages"))

    def test_una_pregunta_vacia_no_se_encola(self, hacer_cola):
        cola, _ = hacer_cola()
        assert cola.agregar("   ") is None and cola.agregar("") is None and cola.preguntas() == []

    def test_se_pueden_anadir_preguntas_mientras_se_envia(self, api, hacer_cola):
        cola, _ = hacer_cola()
        cola.agregar("Primera [[plan:lento(0.6)]]")
        hasta(lambda: len(api.peticiones_a("/v1/messages")) >= 1)
        cola.agregar("Segunda")
        cola.agregar("Tercera")
        hasta(terminadas(cola, 3))
        assert [sin_marca(c.pregunta) for c in api.cobros] == ["Primera", "Segunda", "Tercera"]   # en orden

    def test_las_instrucciones_comunes_viajan_con_cada_pregunta(self, api, hacer_cola):
        cola, _ = hacer_cola(system_prompt="Responde siempre en francés.")
        cola.agregar("Hola")
        hasta(terminadas(cola, 1))
        assert "Responde siempre en francés." in str(api.peticiones_a("/v1/messages")[0]["cuerpo"].get("system"))


class TestEsperas:
    def test_un_limite_por_minuto_se_espera_y_se_sigue_sin_contar_como_fallo(self, api, hacer_cola, esperas):
        cola, ev = hacer_cola()
        cola.agregar("Uno")
        cola.agregar("Dos [[plan:429(2),ok]]")
        cola.agregar("Tres")
        hasta(terminadas(cola, 3))
        espera = [e for e in ev.estados if e.tipo == "waiting"]
        assert espera and espera[0].motivo == "rate_limit" and espera[0].segundos == pytest.approx(4.0)
        assert cola.job.mensaje(2).errores == 0 and api.veces_facturada("Dos") == 1
        assert ev.cuentas_atras

    def test_servidor_saturado(self, api, hacer_cola):
        cola, ev = hacer_cola()
        cola.agregar("Uno [[plan:529,529,ok]]")
        hasta(terminadas(cola, 1))
        assert any(e.tipo == "waiting" and e.motivo == "server" for e in ev.estados)

    def test_sin_conexion_espera_y_cuando_vuelve_continua(self, api, hacer_cola):
        api.caer()
        cola, ev = hacer_cola()
        cola.agregar("Uno")
        cola.agregar("Dos")
        hasta(lambda: any(e.tipo == "waiting" and e.motivo == "sin_conexion" for e in ev.estados), mensaje="no esperó")
        api.levantar()
        hasta(terminadas(cola, 2))
        assert len(api.cobros) == 2


class TestPausaYCierre:
    def test_pausar_deja_terminar_la_pregunta_en_curso_y_reanudar_sigue(self, api, hacer_cola):
        cola, ev = hacer_cola()
        cola.agregar("Uno [[plan:lento(0.5)]]")
        hasta(lambda: len(api.peticiones_a("/v1/messages")) >= 1)
        cola.agregar("Dos")
        cola.agregar("Tres")
        cola.pausar()
        hasta(lambda: en_reposo(cola) and ev.tipos()[-1] == "paused")
        hechas = sum(m.status == STATUS_COMPLETED for m in cola.preguntas())
        assert hechas == 1 and cola.pendientes() == 2
        cola.reanudar()
        hasta(terminadas(cola, 3))
        assert sorted(sin_marca(c.pregunta) for c in api.cobros) == ["Dos", "Tres", "Uno"]     # ninguna dos veces

    def test_con_la_cola_pausada_lo_nuevo_se_guarda_pero_no_se_envia(self, api, hacer_cola):
        cola, _ = hacer_cola()
        cola.pausar()
        cola.agregar("Uno")
        time.sleep(0.3)
        assert cola.pendientes() == 1 and api.peticiones_a("/v1/messages") == []
        cola.reanudar()
        hasta(terminadas(cola, 1))

    def test_quitar_una_pregunta_pendiente(self, api, hacer_cola):
        cola, _ = hacer_cola()
        cola.pausar()
        for texto in ("Uno", "Dos", "Tres"):
            cola.agregar(texto)
        assert cola.quitar(2) is True and cola.quitar(2) is False
        cola.reanudar()
        hasta(terminadas(cola, 2))
        assert cola.job.mensaje(2).status == STATUS_SKIPPED
        assert sorted(sin_marca(c.pregunta) for c in api.cobros) == ["Tres", "Uno"]

    def test_cerrar_deja_lo_pendiente_y_al_volver_a_abrir_se_retoma_sin_pagar_dos_veces(self, api, hacer_cola):
        cola, _ = hacer_cola()
        cola.agregar("Uno [[plan:lento(0.4)]]")
        for texto in ("Dos", "Tres", "Cuatro"):
            cola.agregar(texto)
        hasta(lambda: len(api.peticiones_a("/v1/messages")) >= 1)
        cola.cerrar(timeout=10)
        hechas = [m.id for m in cola.preguntas() if m.status == STATUS_COMPLETED]
        assert hechas == [1] and cola.pendientes() == 3

        reabierta, _ = hacer_cola()                     # «al abrir el programa otra vez»
        assert reabierta.cargar_ultima() == 3
        reabierta.reanudar()
        hasta(terminadas(reabierta, 4))
        assert sorted(sin_marca(c.pregunta) for c in api.cobros) == ["Cuatro", "Dos", "Tres", "Uno"]

    def test_una_cola_nueva_empieza_en_blanco_pero_la_anterior_sigue_en_disco(self, api, hacer_cola):
        cola, _ = hacer_cola()
        cola.agregar("Uno")
        hasta(terminadas(cola, 1))
        anterior = cola.job.job_id
        assert cola.nueva() is True and cola.preguntas() == []
        cola.agregar("Otra")
        hasta(terminadas(cola, 1))
        assert cola.job.job_id != anterior


class TestCuandoNoSePuedeSeguir:
    def test_el_tope_detiene_la_cola_y_al_subirlo_continua(self, api, hacer_cola):
        cola, ev = hacer_cola(tope_gasto_usd=D("0.002"), max_tokens=50)
        for i in range(1, 7):
            cola.agregar(f"Pregunta {i}")
        hasta(lambda: en_reposo(cola) and cola.estado.tipo == "stopped")
        assert cola.estado.razon is StopReason.BUDGET and 0 < len(api.cobros) < 6
        antes = len(api.cobros)
        cola.actualizar_config(replace(cola.config, tope_gasto_usd=D("5")))
        cola.reanudar()
        hasta(terminadas(cola, 6))
        assert len(api.cobros) == 6 and len(api.cobros) > antes
        assert sorted(sin_marca(c.pregunta) for c in api.cobros) == sorted(f"Pregunta {i}" for i in range(1, 7))

    def test_una_clave_rechazada_para_con_el_motivo_y_arreglada_continua(self, api, hacer_cola, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-equivocada")
        cola, ev = hacer_cola()
        cola.agregar("Uno")
        hasta(lambda: en_reposo(cola) and cola.estado.tipo == "stopped")
        assert cola.estado.razon is StopReason.FATAL and any("API key" in e for e in ev.errores)
        assert cola.job.mensaje(1).status != STATUS_COMPLETED
        monkeypatch.setenv("ANTHROPIC_API_KEY", api.clave)
        cola.reanudar()
        hasta(terminadas(cola, 1))

    def test_sin_saldo_para_y_lo_dice(self, api, hacer_cola):
        cola, ev = hacer_cola()
        cola.agregar("Uno [[plan:400saldo]]")
        hasta(lambda: en_reposo(cola) and cola.estado.tipo == "stopped")
        assert cola.estado.razon is StopReason.NO_CREDIT and cola.pendientes() == 1

    def test_un_modelo_que_no_existe(self, api, hacer_cola):
        cola, ev = hacer_cola()
        cola.agregar("Uno [[plan:404modelo]]")
        hasta(lambda: en_reposo(cola) and cola.estado.tipo == "stopped")
        assert cola.estado.razon is StopReason.FATAL and ev.errores

    def test_un_corte_en_pleno_streaming_para_y_al_reanudar_se_reenvia_esa_pregunta(self, api, hacer_cola):
        cola, ev = hacer_cola()
        cola.agregar("Uno")
        cola.agregar("Dos [[plan:corte_stream,ok]]")
        cola.agregar("Tres")
        hasta(lambda: en_reposo(cola) and cola.estado.tipo == "stopped")
        assert cola.estado.razon is StopReason.NETWORK and cola.pendientes() >= 1
        assert not cola.job.response_path(2, 1).exists()
        cola.reanudar()
        hasta(terminadas(cola, 3))
        assert api.veces_facturada("Dos") == 2          # el servidor cobró la cortada: es lo que avisa el programa

    def test_un_200_que_no_es_un_flujo_no_para_la_cola(self, api, hacer_cola):
        cola, _ = hacer_cola()
        cola.agregar("Uno [[plan:basura,ok]]")
        cola.agregar("Dos")
        hasta(terminadas(cola, 2))


class TestSinHilosColgados:
    def test_tras_cerrar_no_queda_ningun_hilo_de_la_cola(self, api, hacer_cola):
        cola, _ = hacer_cola()
        cola.agregar("Uno [[plan:lento(0.3)]]")
        cola.agregar("Dos")
        hasta(lambda: len(api.peticiones_a("/v1/messages")) >= 1)
        cola.cerrar(timeout=10)
        assert not [t for t in threading.enumerate() if t.name == "cola-de-preguntas" and t.is_alive()]
        assert cola.job.mensaje(2).status == STATUS_PENDING
