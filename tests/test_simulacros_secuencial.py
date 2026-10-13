"""Simulacros del motor inmediato: el programa de verdad contra un servidor falso, con el SDK real y sockets reales.

Cada prueba cuenta una historia («el servidor se satura», «se corta la conexión a mitad de la respuesta»…) y,
además de lo que le es propio, pasa por `comprobar_invariantes`: el tope no se supera, nada se factura dos veces,
la contabilidad cuadra con la del servidor y lo pagado está guardado.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal as D

import pytest

from claude_batch_monitor.core import base
from claude_batch_monitor.core.base import StopReason
from claude_batch_monitor.job import STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING, STATUS_REFUSED

from .simulacros_comun import comprobar_invariantes, configuracion, correr

pytestmark = pytest.mark.simulacro

NUEVE = [f"Pregunta número {i}" for i in range(1, 10)]


class TestCaminoFeliz:
    def test_todo_se_responde_se_guarda_y_se_factura_una_vez(self, api, esperas):
        c = correr(NUEVE)
        assert c.razon is StopReason.COMPLETED and c.completadas == 9
        assert len(api.cobros) == 9 and esperas == []
        assert c.motor.gasto_actual() == c.job.coste_total() > 0
        comprobar_invariantes(api, c)

    def test_en_streaming_igual(self, api, esperas):
        c = correr(NUEVE[:4], configuracion(streaming=True))
        assert c.razon is StopReason.COMPLETED and c.completadas == 4
        comprobar_invariantes(api, c)

    def test_con_concurrencia(self, api, esperas):
        c = correr([f"Pregunta {i}" for i in range(24)], configuracion(concurrencia=6))
        assert c.razon is StopReason.COMPLETED and c.completadas == 24
        comprobar_invariantes(api, c)

    def test_las_instrucciones_comunes_viajan_con_cada_pregunta(self, api, esperas):
        c = correr(NUEVE[:2], configuracion(system_prompt="Responde en francés."))
        assert c.completadas == 2
        enviados = [p["cuerpo"] for p in api.peticiones_a("/v1/messages")]
        assert all("Responde en francés." in str(b.get("system")) for b in enviados)

    def test_la_clave_viaja_en_la_cabecera_y_en_ningun_otro_sitio(self, api, esperas):
        c = correr(NUEVE[:2])
        for p in api.peticiones:
            assert p["cabeceras"].get("x-api-key") == api.clave
            assert api.clave not in str(p["cuerpo"])
        assert c.completadas == 2


class TestLimitesDeLaApi:
    def test_429_con_retry_after_espera_lo_que_dice_y_no_cuenta_como_fallo(self, api, esperas):
        c = correr(["uno", "dos [[plan:429(3),ok]]", "tres"])
        assert c.razon is StopReason.COMPLETED and c.completadas == 3
        assert esperas == [pytest.approx(5.0)]          # 3 s de la API + 2 s de margen
        assert c.job.mensaje(2).errores == 0
        assert api.veces_facturada("dos") == 1
        comprobar_invariantes(api, c)

    def test_429_sin_ninguna_indicacion_para_pasadas_unas_cuantas_veces(self, api, esperas):
        c = correr(["uno", "dos [[plan:429sin]]", "tres"])
        assert c.razon is StopReason.ACCOUNT_LIMIT
        assert c.estado_de(1) == STATUS_COMPLETED and c.estado_de(2) == STATUS_PENDING and c.estado_de(3) == STATUS_PENDING
        assert len(esperas) == base.MAX_429_SIN_INFO + 0 or len(esperas) >= base.MAX_429_SIN_INFO
        assert api.veces_facturada("dos") == 0
        comprobar_invariantes(api, c)

    def test_tope_mensual_del_nivel_de_uso_para_a_la_primera_y_dice_cuando_vuelve(self, api, esperas):
        c = correr(["uno", "dos [[plan:429tope]]"])
        assert c.razon is StopReason.ACCOUNT_LIMIT and esperas == []
        assert any("2026-11-01" in e for e in c.errores)
        assert c.estado_de(2) == STATUS_PENDING
        comprobar_invariantes(api, c)

    def test_sin_saldo_para_y_lo_pagado_queda_guardado(self, api, esperas):
        c = correr(["uno", "dos", "tres [[plan:400saldo]]", "cuatro"])
        assert c.razon is StopReason.NO_CREDIT
        assert c.completadas == 2 and c.estado_de(3) == STATUS_PENDING and c.estado_de(4) == STATUS_PENDING
        comprobar_invariantes(api, c)

    def test_limite_de_gasto_puesto_en_la_consola_para_sin_marcar_la_orden_como_fallida(self, api, esperas):
        c = correr(["uno [[plan:400limite]]", "dos"])
        assert c.razon is StopReason.ACCOUNT_LIMIT and c.estado_de(1) == STATUS_PENDING

    @pytest.mark.parametrize("valor", ["nan", "inf", "-5", "-1000000", "abc", "1e999", "", "0"])
    def test_un_retry_after_raro_no_cuelga_el_programa_ni_lo_pone_a_martillear(self, api, esperas, valor):
        """Una cabecera absurda no debe dar una espera infinita, NaN ni cero (un bucle de peticiones sin descanso)."""
        c = correr([f"uno [[plan:retry_after({valor}),ok]]"], limite_s=15)
        assert c.razon is StopReason.COMPLETED
        assert esperas, "debió esperar algo antes de reintentar"
        assert all(0 < e < 86_400 + 5 and e == e for e in esperas), esperas
        assert api.veces_facturada("uno") == 1


class TestErroresDeServidor:
    def test_saturado_dos_veces_reintenta_y_cuenta_los_fallos(self, api, esperas):
        c = correr(["uno [[plan:529,529,ok]]", "dos"])
        assert c.razon is StopReason.COMPLETED and c.completadas == 2
        assert c.job.mensaje(1).errores == 2 and len(esperas) == 2
        comprobar_invariantes(api, c)

    def test_saturado_siempre_pausa_el_lote_al_llegar_al_maximo(self, api, esperas):
        c = correr(["uno [[plan:529]]", "dos"], configuracion(max_error_attempts=3))
        assert c.razon is StopReason.FATAL
        assert c.estado_de(1) == STATUS_FAILED and c.estado_de(2) == STATUS_PENDING
        assert len(api.peticiones_a("/v1/messages")) == 3
        assert any("Máximo de errores" in e for e in c.errores)

    @pytest.mark.parametrize("plan", ["500", "502", "503", "504", "408", "409"])
    def test_los_errores_pasajeros_se_reintentan(self, api, esperas, plan):
        c = correr([f"uno [[plan:{plan},ok]]", "dos"])
        assert c.razon is StopReason.COMPLETED and c.completadas == 2, (plan, c.errores)
        assert api.veces_facturada("uno") == 1

    def test_413_falla_solo_esa_orden_y_el_resto_sigue(self, api, esperas):
        c = correr(["uno", "dos [[plan:413]]", "tres"])
        assert c.razon is StopReason.COMPLETED
        assert c.estado_de(2) == STATUS_FAILED and c.completadas == 2
        comprobar_invariantes(api, c)

    @pytest.mark.parametrize("plan, esperado", [("401", "autenticación"), ("403", "autenticación"), ("404modelo", "modelo")])
    def test_clave_o_modelo_malos_paran_todo_con_un_mensaje_claro(self, api, esperas, plan, esperado):
        c = correr([f"uno [[plan:{plan}]]", "dos"])
        assert c.razon is StopReason.FATAL and esperado in " ".join(c.errores).lower()
        assert c.estado_de(2) == STATUS_PENDING

    def test_una_clave_equivocada_se_rechaza_en_el_primer_intento(self, api, esperas, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-otra")
        c = correr(["uno", "dos"])
        assert c.razon is StopReason.FATAL and c.completadas == 0 and len(api.peticiones_a("/v1/messages")) == 1


class TestCortesDeConexion:
    @pytest.mark.parametrize("plan", ["corte", "perdida"])
    def test_cortada_a_mitad_de_la_respuesta_se_para_sin_reenviar(self, api, esperas, plan):
        """El servidor pudo cobrarla: reenviar duplicaría el cobro. Se para y se dice."""
        c = correr(["uno", f"dos [[plan:{plan}]]", "tres"])
        assert c.razon is StopReason.NETWORK
        assert api.veces_facturada("dos") == 1               # una sola vez: no se reenvió
        assert c.estado_de(3) == STATUS_PENDING
        assert any("cobros duplicados" in e for e in c.errores)
        comprobar_invariantes(api, c)

    def test_cortada_en_streaming_tambien_es_un_corte_de_red(self, api, esperas):
        c = correr(["uno", "dos [[plan:corte_stream]]", "tres"], configuracion(streaming=True))
        assert c.razon is StopReason.NETWORK, (c.razon, c.errores)
        assert api.veces_facturada("dos") == 1 and c.estado_de(3) == STATUS_PENDING
        assert any("conexión" in e.lower() for e in c.errores)

    @pytest.mark.parametrize("plan", ["cierre", "reset"])
    def test_el_servidor_cierra_o_reinicia_la_conexion_se_para(self, api, esperas, plan):
        c = correr(["uno", f"dos [[plan:{plan}]]", "tres"])
        assert c.razon is StopReason.NETWORK and c.estado_de(3) == STATUS_PENDING
        comprobar_invariantes(api, c)

    def test_servidor_caido_un_rato_y_de_vuelta_el_lote_sigue_solo(self, api, esperas):
        api.caer()
        hilo = threading.Timer(0.1, api.levantar)   # las esperas van comprimidas: 0,1 s reales son varios minutos virtuales
        hilo.start()
        c = correr(NUEVE[:3], limite_s=30)
        hilo.join()
        assert c.razon is StopReason.COMPLETED and c.completadas == 3
        assert esperas and esperas[0] == 5.0                  # esperó (con la compresión de tiempo) lo que debía
        comprobar_invariantes(api, c)

    def test_servidor_caido_mas_de_diez_minutos_pausa_el_lote(self, api, esperas, monkeypatch):
        monkeypatch.setattr(base, "MAX_SIN_CONEXION_SEC", 12)
        api.caer()
        c = correr(NUEVE[:3], limite_s=30)
        assert c.razon is StopReason.NETWORK and c.completadas == 0 and c.pendientes == 3
        assert any("10 minutos" in e for e in c.errores)


class TestRespuestasRaras:
    def test_el_modelo_se_niega_se_marca_rechazada_y_se_cobra(self, api, esperas):
        c = correr(["uno [[plan:refusal]]", "dos"])
        assert c.estado_de(1) == STATUS_REFUSED and c.completadas == 2
        assert c.job.mensaje(1).coste_usd > 0
        comprobar_invariantes(api, c)

    def test_truncada_por_max_tokens_se_guarda_con_aviso(self, api, esperas):
        c = correr(["uno [[plan:maxtokens]]"])
        assert c.job.mensaje(1).truncated
        assert base.AVISO_TRUNCADA in c.job.response_path(1, 1).read_text(encoding="utf-8")

    def test_respuesta_vacia_se_dice(self, api, esperas):
        c = correr(["uno [[plan:vacio]]"])
        assert base.RESPUESTA_VACIA in c.job.response_path(1, 1).read_text(encoding="utf-8")

    def test_un_200_que_no_es_json_no_hunde_el_lote_entero(self, api, esperas):
        """Una página de un proxy o de un portal cautivo con código 200: se reintenta, no se abandona todo."""
        c = correr(["uno [[plan:basura,ok]]", "dos"])
        assert c.razon is StopReason.COMPLETED and c.completadas == 2, (c.razon, c.errores)

    def test_usage_incompleto_o_con_tipos_raros_no_rompe_la_contabilidad(self, api, esperas):
        c = correr(["uno [[plan:uso_raro]]", "dos"])
        assert c.razon is StopReason.COMPLETED and c.completadas == 2
        assert c.motor.gasto_actual() >= 0
        comprobar_invariantes(api, c)          # lo que se puede leer del usage raro (7 tokens de salida) se cuenta

    def test_en_streaming_un_200_que_no_es_un_flujo_se_reintenta(self, api, esperas):
        """Antes salía un `AssertionError` del SDK, se tomaba por un fallo inesperado y se abandonaba todo."""
        c = correr(["uno [[plan:basura,ok]]", "dos"], configuracion(streaming=True))
        assert c.razon is StopReason.COMPLETED and c.completadas == 2, (c.razon, c.errores)
        assert api.veces_facturada("uno") == 1
        comprobar_invariantes(api, c)

    def test_en_streaming_un_flujo_que_termina_sin_mensaje_se_trata_como_un_corte(self, api, esperas):
        c = correr(["uno", "dos [[plan:flujo_vacio]]", "tres"], configuracion(streaming=True))
        assert c.razon is StopReason.NETWORK, (c.razon, c.errores)
        assert c.estado_de(2) != STATUS_COMPLETED and c.estado_de(3) == STATUS_PENDING

    def test_en_streaming_un_mensaje_a_medias_no_se_da_por_bueno(self, api, esperas):
        """Si el flujo se cierra «bien» pero sin el final del mensaje, no se guarda como respuesta completa."""
        c = correr(["uno", "dos [[plan:flujo_trunco]]", "tres"], configuracion(streaming=True))
        assert c.razon is StopReason.NETWORK, (c.razon, c.errores)
        assert c.estado_de(2) != STATUS_COMPLETED and c.estado_de(3) == STATUS_PENDING
        assert api.veces_facturada("dos") == 1               # no se reenvió: pudo cobrarse
        assert not c.job.response_path(2, 1).exists()

    def test_un_usage_descomunal_para_por_el_tope_sin_desbordar_nada(self, api, esperas):
        c = correr(["uno [[plan:uso_enorme]]", "dos"])
        assert c.razon is StopReason.BUDGET and c.estado_de(1) == STATUS_COMPLETED and c.estado_de(2) == STATUS_PENDING
        assert c.motor.gasto_actual() > D("5")


class TestElTopeDeGasto:
    def test_el_tope_se_respeta_aunque_cada_respuesta_use_todo_su_max_tokens(self, api, esperas):
        preguntas = [f"Pregunta larga {i} [[plan:ok(200)]]" for i in range(40)]
        c = correr(preguntas, configuracion(tope_gasto_usd=D("0.05"), max_tokens=200))
        assert c.razon is StopReason.BUDGET
        assert 0 < c.completadas < 40
        comprobar_invariantes(api, c)

    @pytest.mark.parametrize("concurrencia", [1, 4, 8])
    def test_con_concurrencia_el_tope_tampoco_se_supera(self, api, esperas, concurrencia):
        preguntas = [f"Pregunta {i} [[plan:ok(180)]]" for i in range(60)]
        c = correr(preguntas, configuracion(tope_gasto_usd=D("0.04"), max_tokens=200, concurrencia=concurrencia))
        assert c.razon is StopReason.BUDGET and c.completadas > 0
        comprobar_invariantes(api, c)

    def test_una_respuesta_que_cruza_el_tope_se_guarda_igualmente(self, api, esperas):
        c = correr(["uno [[plan:ok(190)]]", "dos"], configuracion(tope_gasto_usd=D("0.0035"), max_tokens=200))
        assert c.estado_de(1) == STATUS_COMPLETED and c.job.response_path(1, 1).exists()


class TestTimeouts:
    def test_un_timeout_se_reintenta_y_el_posible_cobro_cuenta_para_el_tope(self, api, esperas):
        """Si el cliente se rinde pero el servidor ya procesaba, el cobro existe aunque el programa no lo vea.

        La contabilidad prudente trata cada intento abandonado como gastado en el peor caso: el tope debe aguantar
        aunque todas las peticiones acaben en timeout.
        """
        preguntas = [f"uno {i} [[plan:lento(1.4)]]" for i in range(8)]
        c = correr(
            preguntas,
            configuracion(tope_gasto_usd=D("0.02"), max_tokens=500, timeout_api_sec=1, max_error_attempts=50),
            limite_s=60,
        )
        comprobar_invariantes(api, c)


def test_ningun_hilo_del_motor_se_queda_vivo(api, esperas):
    antes = {h.ident for h in threading.enumerate()}
    c = correr(NUEVE[:3], configuracion(concurrencia=3))
    time.sleep(0.2)
    vivos = [h.name for h in threading.enumerate() if h.ident not in antes and not h.daemon]
    assert c.razon is StopReason.COMPLETED and vivos == []
