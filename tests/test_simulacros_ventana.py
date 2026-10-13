"""La ventana del modo simple como la usa una persona: del primer arranque a la respuesta, con el SDK real y el servidor falso.

Los otros tests de la ventana usan un motor falso; aquí todo es de verdad salvo el servidor y el llavero.
Necesitan tkinter y una pantalla (en Linux sin escritorio: `xvfb-run -a pytest`).
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from claude_batch_monitor.core.base import StopReason
from claude_batch_monitor.job import STATUS_COMPLETED

from .gui_comun import bombear, registrar
from .simulacros_comun import configuracion

tk = pytest.importorskip("tkinter")
pytestmark = [pytest.mark.gui, pytest.mark.simulacro]

from claude_batch_monitor.gui import simple  # noqa: E402
from claude_batch_monitor.gui.i18n import fijar_idioma, t  # noqa: E402


@pytest.fixture(autouse=True)
def idioma():
    fijar_idioma("es")
    yield
    fijar_idioma("es")


def abrir(raiz, **config):
    app = simple.SimpleApp(raiz, config=configuracion(**config), usar_bandeja=False, abrir_url=lambda u: None,
                           guardar=lambda k: "llavero")
    return registrar(app)


def escribir(app, texto):
    app._quitar_placeholder()
    app.entrada.insert("1.0", texto)


def filas(app):
    return [app.tabla.item(i, "values") for i in app.tabla.get_children()]


def respuesta_en_pantalla(app):
    return app.respuesta.get("1.0", "end").strip()


class TestPrimerUso:
    def test_pegar_la_clave_preguntar_y_leer_la_respuesta(self, raiz, api, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        guardadas = []

        def guardar(clave):                       # el llavero del sistema, simulado con el entorno
            guardadas.append(clave)
            monkeypatch.setenv("ANTHROPIC_API_KEY", clave)
            return "llavero"

        app = registrar(simple.SimpleApp(
            raiz, config=configuracion(modelo="modelo-retirado"), usar_bandeja=False,
            abrir_url=lambda u: None, guardar=guardar))
        assert app.pantalla == "bienvenida"

        app.panel_clave.var.set(f"  {api.clave}  ")           # con los espacios de copiar y pegar
        app.panel_clave.probar()
        assert bombear(raiz, lambda: app.pantalla == "principal", timeout=15), app.panel_clave.mensaje.cget("text")
        assert guardadas == [api.clave]
        assert app.config.modelo in ("claude-sonnet-5-5", "claude-haiku-4-5", "claude-opus-5-5")   # el retirado se cambió

        escribir(app, "¿Qué es un token?")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == t("st_done"), timeout=20)
        assert "Respuesta a: ¿Qué es un token?" in respuesta_en_pantalla(app)
        assert t("status_idle") in app.var_estado.get()
        assert app.var_gasto.get().startswith("Gastado") and "$0.0000" not in app.var_gasto.get()
        assert app.cola.gastado() > 0
        assert [p["cabeceras"]["x-api-key"] for p in api.peticiones_a("/v1/messages")] == [api.clave]

    def test_una_clave_equivocada_se_explica_y_no_se_guarda(self, raiz, api, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        app = abrir(raiz)
        app.panel_clave.var.set("sk-ant-equivocada")
        app.panel_clave.probar()
        assert bombear(raiz, lambda: "no reconoce" in app.panel_clave.mensaje.cget("text"), timeout=15)
        assert app.pantalla == "bienvenida"


class TestUsoNormal:
    def test_varias_preguntas_seguidas_se_contestan_en_orden_y_cada_fila_enseña_su_respuesta(self, raiz, api):
        app = abrir(raiz)
        for i in range(1, 4):
            escribir(app, f"Pregunta número {i}")
            app.enviar()
        assert bombear(raiz, lambda: len(filas(app)) == 3 and all(f[2] == t("st_done") for f in filas(app)), timeout=30)
        for i in range(1, 4):
            app.tabla.selection_set(app.tabla.get_children()[i - 1])
            app._al_seleccionar()
            assert f"Respuesta a: Pregunta número {i}" in respuesta_en_pantalla(app)

    def test_un_limite_de_la_api_se_ve_en_pantalla_y_se_resuelve_solo(self, raiz, api):
        app = abrir(raiz)
        escribir(app, "Hola [[plan:429(2),ok]]")
        app.enviar()
        assert bombear(raiz, lambda: "límite" in app.var_estado.get(), timeout=15), app.var_estado.get()
        assert bombear(raiz, lambda: filas(app)[0][2] == t("st_done"), timeout=30)
        assert api.veces_facturada("Hola") == 1

    def test_sin_conexion_se_avisa_y_al_volver_sigue_sola(self, raiz, api):
        app = abrir(raiz)
        api.caer()
        escribir(app, "Hola")
        app.enviar()
        assert bombear(raiz, lambda: "Sin conexión" in app.var_estado.get(), timeout=20), app.var_estado.get()
        api.levantar()
        assert bombear(raiz, lambda: filas(app)[0][2] == t("st_done"), timeout=30)

    def test_el_tope_de_gasto_para_la_cola_y_la_pantalla_lo_dice(self, raiz, api):
        app = abrir(raiz, tope_gasto_usd=D("0.0015"), max_tokens=50)
        for i in range(1, 6):
            escribir(app, f"Pregunta {i}")
            app.enviar()
        assert bombear(raiz, lambda: t("status_stopped") in app.var_estado.get(), timeout=30), app.var_estado.get()
        assert app.cola.estado.razon is StopReason.BUDGET
        hechas = sum(1 for m in app.cola.preguntas() if m.status == STATUS_COMPLETED)
        assert 0 < hechas < 5

    def test_cerrar_con_preguntas_pendientes_y_volver_a_abrir_las_encuentra_sin_enviarlas_solas(self, raiz, api):
        app = abrir(raiz)
        app.cola.pausar()
        for texto in ("Uno", "Dos", "Tres"):
            escribir(app, texto)
            app.cola.agregar(texto)
        bombear(raiz, timeout=0.3)
        app._salir()                                           # se cierra el programa
        assert api.peticiones_a("/v1/messages") == []

        raiz2 = tk.Tk()                                        # …y se vuelve a abrir
        try:
            otra = simple.SimpleApp(raiz2, config=configuracion(), usar_bandeja=False, abrir_url=lambda u: None,
                                    guardar=lambda k: "llavero")
            registrar(otra)
            assert bombear(raiz2, lambda: len(filas(otra)) == 3, timeout=10)
            assert [f[2] for f in filas(otra)] == [t("st_pending")] * 3
            assert api.peticiones_a("/v1/messages") == []      # no gasta sin que se lo pidan
            otra.reanudar_pendientes()
            assert bombear(raiz2, lambda: all(f[2] == t("st_done") for f in filas(otra)), timeout=30)
            assert sorted(c.pregunta for c in api.cobros) == ["Dos", "Tres", "Uno"]
        finally:
            raiz2.destroy()
