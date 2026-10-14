"""El modo simple, con la ventana de verdad.

Necesitan tkinter y una pantalla: en Linux sin escritorio, `xvfb-run -a pytest`.
Si no hay pantalla, se saltan solos (el resto de la suite no los necesita).
"""

import threading
import time
from decimal import Decimal as D

import anthropic
import pytest

from claude_batch_monitor.claves import ErrorDeClave
from claude_batch_monitor.cola import ETIQUETA
from claude_batch_monitor.config import ConfigManager, Configuracion
from claude_batch_monitor.core import base
from claude_batch_monitor.job import STATUS_SKIPPED, Job

from ._http import httpx
from .gui_comun import bombear, registrar
from .test_cola import fabrica_con
from .test_engine import FakeResponse
from .test_errors import TOPE_MENSUAL, error, interno
from .test_integration import status_error

tk = pytest.importorskip("tkinter")
pytestmark = pytest.mark.gui

from claude_batch_monitor.gui import simple  # noqa: E402
from claude_batch_monitor.gui.i18n import fijar_idioma, t  # noqa: E402


@pytest.fixture(autouse=True)
def sin_efectos(monkeypatch):
    fijar_idioma("es")
    monkeypatch.setattr(base, "notificar", lambda *a, **k: None)
    yield
    fijar_idioma("es")


@pytest.fixture
def con_clave(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-de-prueba")


def cfg(**kw):
    base_ = dict(modelo="claude-sonnet-5-5", max_tokens=512, tope_gasto_usd=D("5"),
                 precio_input_mtok=D("1"), precio_output_mtok=D("5"))
    base_.update(kw)
    return Configuracion(**base_)


def crear(raiz, guion=None, config=None, **kw):
    fab = fabrica_con(guion if guion is not None else [FakeResponse("respuesta de prueba")] * 20)
    kw.setdefault("probar", lambda k: ["claude-sonnet-5-5", "claude-haiku-4-5"])
    kw.setdefault("listar", lambda k: ["claude-opus-5-5", "claude-sonnet-5-5"])
    kw.setdefault("guardar", lambda k: "llavero")
    kw.setdefault("abrir_url", lambda u: None)
    app = simple.SimpleApp(raiz, config=config or cfg(), fabrica=fab, usar_bandeja=False, **kw)
    app.fab = fab
    return registrar(app)


def filas(app):
    return [app.tabla.item(i, "values") for i in app.tabla.get_children()]


def escribir(app, texto):
    app._quitar_placeholder()
    app.entrada.insert("1.0", texto)


# ----------------------------------------------------------------------


class TestBienvenida:
    def test_sin_clave_arranca_en_la_bienvenida(self, raiz):
        assert crear(raiz).pantalla == "bienvenida"

    def test_con_clave_arranca_en_la_pantalla_principal(self, raiz, con_clave):
        assert crear(raiz).pantalla == "principal"

    def test_una_clave_valida_pasa_a_la_pantalla_principal_y_elige_modelo(self, raiz):
        guardadas = []
        app = crear(raiz, config=cfg(modelo="modelo-retirado"), guardar=lambda k: (guardadas.append(k), "llavero")[1],
                    probar=lambda k: ["claude-haiku-4-5", "claude-sonnet-5-5"])
        app.panel_clave.var.set("  sk-ant-buena  ")
        app.panel_clave.probar()
        assert bombear(raiz, lambda: app.pantalla == "principal")
        assert guardadas == ["sk-ant-buena"]                         # sin los espacios de copiar y pegar
        assert app.config.modelo == "claude-sonnet-5-5"              # el modelo retirado se sustituyó
        assert app.panel_clave.var.get() == ""                       # la clave no se queda en un campo

    def test_una_clave_invalida_se_explica_y_se_puede_reintentar(self, raiz):
        def malo(k):
            raise ErrorDeClave("invalida")
        app = crear(raiz, probar=malo)
        app.panel_clave.var.set("sk-ant-mala")
        app.panel_clave.probar()
        assert bombear(raiz, lambda: "no reconoce" in app.panel_clave.mensaje.cget("text"))
        assert app.pantalla == "bienvenida"
        assert "disabled" not in app.panel_clave.boton.state()

    def test_sin_internet(self, raiz):
        def caido(k):
            raise ErrorDeClave("sin_conexion")
        app = crear(raiz, probar=caido)
        app.panel_clave.var.set("sk-ant-x")
        app.panel_clave.probar()
        assert bombear(raiz, lambda: "internet" in app.panel_clave.mensaje.cget("text"))

    def test_clave_vacia(self, raiz):
        app = crear(raiz)
        app.panel_clave.probar()
        assert "Pega primero" in app.panel_clave.mensaje.cget("text")

    def test_la_clave_se_oculta_salvo_que_se_pida(self, raiz):
        app = crear(raiz)
        assert app.panel_clave.entrada.cget("show") == "•"
        app.panel_clave.mostrar.set(True)
        app.panel_clave._alternar()
        assert app.panel_clave.entrada.cget("show") == ""

    def test_si_no_hay_llavero_avisa_de_que_hay_que_pegarla_cada_vez(self, raiz):
        app = crear(raiz, guardar=lambda k: "memoria")
        app.panel_clave.var.set("sk-ant-x")
        app.panel_clave.probar()
        assert bombear(raiz, lambda: "cada vez" in app.panel_clave.mensaje.cget("text"))


class TestEnviar:
    def test_la_pregunta_se_envia_sola_y_se_responde(self, raiz, con_clave):
        app = crear(raiz)
        escribir(app, "¿Qué es un token?")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista")
        assert filas(app)[0][:2] == ("1", "¿Qué es un token?")
        assert "respuesta de prueba" in app.respuesta.get("1.0", "end")
        assert app.entrada.get("1.0", "end-1c") == ""

    def test_el_gasto_se_actualiza(self, raiz, con_clave):
        app = crear(raiz)
        escribir(app, "a")
        app.enviar()
        assert bombear(raiz, lambda: "$0.0035" in app.var_gasto.get())
        assert "de $5.00" in app.var_gasto.get()

    def test_sin_tope_lo_dice(self, raiz, con_clave):
        app = crear(raiz, config=cfg(tope_gasto_usd=D(0)))
        assert "sin tope" in app.var_gasto.get()

    def test_el_texto_vacio_o_el_placeholder_no_se_envian(self, raiz, con_clave):
        app = crear(raiz)
        app.enviar()
        escribir(app, "   \n ")
        app.enviar()
        app.entrada.delete("1.0", "end")
        app._poner_placeholder()
        app.enviar()
        assert filas(app) == [] and app.cola.job is None

    def test_el_placeholder_aparece_y_desaparece(self, raiz, con_clave):
        app = crear(raiz)
        assert app.entrada.get("1.0", "end-1c") == t("placeholder") and app.texto_de_entrada() == ""
        app._quitar_placeholder()
        assert app.entrada.get("1.0", "end-1c") == ""
        app._poner_placeholder()
        assert app.entrada.get("1.0", "end-1c") == t("placeholder")

    def test_enter_envia_y_mayus_enter_añade_una_linea(self, raiz, con_clave):
        app = crear(raiz)
        raiz.deiconify()
        raiz.update()
        app.entrada.focus_force()
        raiz.update()
        escribir(app, "primera")
        app.entrada.event_generate("<Shift-Return>")
        raiz.update()
        assert filas(app) == []                              # Mayús+Enter no envía
        assert "\n" in app.entrada.get("1.0", "end-1c")
        app.entrada.event_generate("<Return>")
        assert bombear(raiz, lambda: len(filas(app)) == 1)    # Enter sí

    def test_varias_preguntas_se_responden_en_orden(self, raiz, con_clave):
        app = crear(raiz)
        for q in ("uno", "dos", "tres"):
            escribir(app, q)
            app.enviar()
        assert bombear(raiz, lambda: [f[2] for f in filas(app)] == ["Lista"] * 3)
        assert [f[1] for f in filas(app)] == ["uno", "dos", "tres"]

    def test_las_preguntas_largas_se_resumen_en_la_lista(self, raiz, con_clave):
        app = crear(raiz)
        escribir(app, "palabra " * 60)
        app.enviar()
        bombear(raiz, lambda: filas(app))
        assert filas(app)[0][1].endswith("…") and len(filas(app)[0][1]) <= 90

    def test_el_texto_de_la_respuesta_llega_por_trozos(self, raiz, con_clave):
        app = crear(raiz)
        app.cola.pausar()
        app.cola.agregar("a")  # en la cola pero sin enviar (`enviar()` reanudaría): la respuesta de verdad no se mezcla con los trozos
        assert bombear(raiz, lambda: filas(app))
        app._seleccionar(1)
        app.on_delta(1, "Hola ")
        app.on_delta(1, "mundo")
        assert bombear(raiz, lambda: app.respuesta.get("1.0", "end").strip() == "Hola mundo")

    def test_los_trozos_que_llegan_juntos_se_pintan_de_una_vez(self, raiz, con_clave):
        """Repintar con cada trozo dejaba la ventana sin responder con una respuesta larga sin saltos de línea."""
        app = crear(raiz)
        app.cola.pausar()
        app.cola.agregar("a")
        assert bombear(raiz, lambda: filas(app))
        app._seleccionar(1)
        pintados = []
        original = app._escribir_respuesta
        app._escribir_respuesta = lambda texto, **k: (pintados.append(texto), original(texto, **k))[1]
        for i in range(500):
            app.on_delta(1, f"trozo{i} ")
        assert bombear(raiz, lambda: pintados and pintados[-1].endswith("trozo499 "))
        assert len(pintados) <= 5                      # y no 500
        assert app.respuesta.get("1.0", "end").split() == [f"trozo{i}" for i in range(500)]

    def test_si_la_respuesta_termina_antes_del_repintado_no_se_pisa_con_el_texto_parcial(self, raiz, con_clave):
        app = crear(raiz, guion=[FakeResponse("La respuesta definitiva")])
        escribir(app, "q")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == t("st_done"))
        assert "La respuesta definitiva" in app.respuesta.get("1.0", "end")
        app.on_delta(1, "trozo tardío")                 # un repintado queda programado…
        app.on_orden(app.cola.job.mensaje(1))            # …y la respuesta definitiva llega antes de que se ejecute
        bombear(raiz, timeout=0.4)
        texto = app.respuesta.get("1.0", "end")
        assert "La respuesta definitiva" in texto and "trozo tardío" not in texto


class TestSeleccion:
    def test_la_respuesta_nueva_se_muestra_sola(self, raiz, con_clave):
        app = crear(raiz, guion=[FakeResponse("primera"), FakeResponse("segunda")])
        for q in ("a", "b"):
            escribir(app, q)
            app.enviar()
        assert bombear(raiz, lambda: [f[2] for f in filas(app)] == ["Lista", "Lista"])
        bombear(raiz, lambda: "segunda" in app.respuesta.get("1.0", "end"), timeout=2)
        assert "segunda" in app.respuesta.get("1.0", "end")
        assert app._seleccionada() == 2

    def test_si_la_persona_elige_otra_no_se_le_quita(self, raiz, con_clave):
        app = crear(raiz, guion=[FakeResponse("primera"), FakeResponse("segunda")])
        escribir(app, "a")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista")
        app.cola.pausar()
        escribir(app, "b")
        app.cola.agregar(app.texto_de_entrada())
        assert bombear(raiz, lambda: len(filas(app)) == 2)         # la fila 2 ya existe en la lista
        app.tabla.selection_set(app.tabla.get_children()[0])      # elige la 1 a mano
        app._fijar_modo_seleccion()
        assert app._seleccion_auto is False
        app.cola.reanudar()
        assert bombear(raiz, lambda: len(filas(app)) >= 2 and filas(app)[1][2] == "Lista")
        bombear(raiz, timeout=0.3)
        assert app._seleccionada() == 1 and "primera" in app.respuesta.get("1.0", "end")

    def test_elegir_la_ultima_vuelve_a_activar_el_seguimiento(self, raiz, con_clave):
        app = crear(raiz)
        for q in ("a", "b"):
            escribir(app, q)
            app.enviar()
        bombear(raiz, lambda: [f[2] for f in filas(app)] == ["Lista", "Lista"])
        app.tabla.selection_set(app.tabla.get_children()[0])
        app._fijar_modo_seleccion()
        assert not app._seleccion_auto
        app.tabla.selection_set(app.tabla.get_children()[-1])
        app._fijar_modo_seleccion()
        assert app._seleccion_auto


class TestEstadosYEsperas:
    def test_formato_de_la_cuenta_atras(self):
        assert [simple.formatear_tiempo(s) for s in (0, 5, 59.6, 60, 75.2)] == ["0:00", "0:05", "1:00", "1:00", "1:15"]

    def test_las_esperas_largas_del_plan_se_cuentan_en_horas_y_dias(self):
        assert [simple.formatear_tiempo(s) for s in (3599.4, 3600, 3725, 5 * 3600 + 7, 86400, 93600 + 61)] == [
            "59:59", "1:00:00", "1:02:05", "5:00:07", "1 d 0:00:00", "1 d 2:01:01"]

    def test_la_espera_por_limite_se_cuenta_en_pantalla_y_continua_sola(self, raiz, con_clave, monkeypatch):
        monkeypatch.setattr(base, "MARGEN_RATE_LIMIT_SEC", 0.0)
        resp = httpx.Response(429, request=httpx.Request("POST", "https://x"), headers={"retry-after": "1.5"})
        limite = anthropic.RateLimitError("rl", response=resp, body=None)
        app = crear(raiz, guion=[limite, FakeResponse("tras esperar")])
        escribir(app, "a")
        app.enviar()
        assert bombear(raiz, lambda: "Sigo sola en" in app.var_estado.get())
        assert filas(app)[0][2] == "Esperando…"
        assert bombear(raiz, lambda: filas(app)[0][2] == "Lista", timeout=8)

    def test_pausar_y_reanudar(self, raiz, con_clave):
        app = crear(raiz)
        app.alternar_pausa()
        bombear(raiz, timeout=0.2)
        assert app.boton_pausa.cget("text") == "Reanudar" and "En pausa" in app.var_estado.get()
        escribir(app, "a")
        app.enviar()                                   # enviar = querer que salga
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista")
        assert app.boton_pausa.cget("text") == "Pausar"

    def test_la_pregunta_en_marcha_se_distingue_de_las_que_esperan(self, raiz, con_clave):
        app = crear(raiz)
        app.cola.pausar()
        for q in ("a", "b"):
            escribir(app, q)
            app.cola.agregar(q)
        bombear(raiz, timeout=0.2)
        assert [f[2] for f in filas(app)] == ["En cola", "En cola"]


MARKDOWN = "# Título\n\nTexto con **fuerte** y `código`.\n\n- uno\n- dos"


class TestRespuestasConFormato:
    def _responder(self, raiz, config=None):
        app = crear(raiz, guion=[FakeResponse(MARKDOWN)] * 3, config=config)
        escribir(app, "pregunta")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista")
        bombear(raiz, lambda: "Título" in app.respuesta.get("1.0", "end"), timeout=2)
        return app

    def test_la_respuesta_terminada_se_ve_con_formato(self, raiz, con_clave):
        app = self._responder(raiz)
        texto = app.respuesta.get("1.0", "end")
        assert "**" not in texto and "`" not in texto and "# " not in texto      # sin los signos de Markdown
        assert "Título" in texto and "fuerte" in texto and "•\tuno" in texto
        assert app.respuesta.tag_ranges("h1") and app.respuesta.tag_ranges("negrita")
        assert app.respuesta.tag_ranges("codigo") and app.respuesta.tag_ranges("lista0")

    def test_se_lee_desde_el_principio(self, raiz, con_clave):
        app = self._responder(raiz)
        assert app.respuesta.yview()[0] == 0.0

    def test_mientras_llega_se_ve_el_texto_tal_cual(self, raiz, con_clave):
        app = crear(raiz)
        app.cola.pausar()
        app.cola.agregar("a")  # en la cola pero sin enviar (`enviar()` reanudaría y la respuesta de verdad se mezclaría)
        assert bombear(raiz, lambda: filas(app), timeout=2)
        app._seleccionar(1)
        app.on_delta(1, "**a medias")
        assert bombear(raiz, lambda: app.respuesta.get("1.0", "end").strip() == "**a medias")
        assert not app.respuesta.tag_ranges("negrita")

    def test_copiar_da_el_texto_original_no_el_formateado(self, raiz, con_clave):
        app = self._responder(raiz)
        app.copiar_respuesta()
        assert raiz.clipboard_get() == MARKDOWN

    def test_la_casilla_muestra_el_texto_original_y_se_recuerda(self, raiz, con_clave):
        app = self._responder(raiz)
        app.var_formato.set(False)
        app._cambiar_formato()
        assert "**fuerte**" in app.respuesta.get("1.0", "end") and not app.respuesta.tag_ranges("negrita")
        assert ConfigManager.cargar().mostrar_formato is False
        app.var_formato.set(True)
        app._cambiar_formato()
        assert app.respuesta.tag_ranges("negrita") and ConfigManager.cargar().mostrar_formato is True

    def test_si_ya_estaba_desactivado_arranca_sin_formato(self, raiz, con_clave):
        app = self._responder(raiz, config=cfg(mostrar_formato=False))
        assert app.var_formato.get() is False and "**fuerte**" in app.respuesta.get("1.0", "end")

    def test_un_error_no_se_formatea(self, raiz, con_clave):
        app = crear(raiz, guion=[status_error(400)] * 3)
        escribir(app, "a")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Error")
        assert not app.respuesta.tag_ranges("negrita")


class TestInstruccionesComunes:
    def test_las_instrucciones_guardadas_aparecen_en_las_opciones(self, raiz, con_clave):
        d = simple.DialogoOpciones(crear(raiz, config=cfg(system_prompt="Responde en francés.")))
        assert d.instrucciones.get("1.0", "end-1c") == "Responde en francés."
        d.destroy()

    def test_guardar_las_opciones_las_conserva_y_se_envian_con_cada_pregunta(self, raiz, con_clave):
        app = crear(raiz)
        d = simple.DialogoOpciones(app)
        d.instrucciones.insert("1.0", "  Responde siempre en español y con frases cortas.  ")
        d.guardar()
        assert app.config.system_prompt == "Responde siempre en español y con frases cortas."
        assert ConfigManager.cargar().system_prompt == "Responde siempre en español y con frases cortas."
        escribir(app, "¿Qué es un token?")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista")
        peticion = app.fab.cliente.messages.peticiones[0]
        assert peticion["system"][0]["text"] == "Responde siempre en español y con frases cortas."

    def test_vaciar_el_cuadro_quita_las_instrucciones(self, raiz, con_clave):
        app = crear(raiz, config=cfg(system_prompt="algo"))
        d = simple.DialogoOpciones(app)
        d.instrucciones.delete("1.0", "end")
        d.guardar()
        assert app.config.system_prompt == ""
        escribir(app, "a")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista")
        assert "system" not in app.fab.cliente.messages.peticiones[0]

    def test_aplicar_opciones_sin_instrucciones_no_las_toca(self, raiz, con_clave):
        app = crear(raiz, config=cfg(system_prompt="no me borres"))
        app.aplicar_opciones(modelo="claude-sonnet-5-5", tope=D("5"), auto=False, idioma="")
        assert app.config.system_prompt == "no me borres"


class TestColaVacia:
    def test_con_la_cola_vacia_se_explica_donde_iran_las_preguntas(self, raiz, con_clave):
        app = crear(raiz)
        assert app.vacio.winfo_manager() == "place" and app.vacio.cget("text") == t("queue_empty")

    def test_el_panel_de_la_respuesta_tambien_dice_que_pasara(self, raiz, con_clave):
        app = crear(raiz)
        assert app.respuesta.get("1.0", "end").strip() == t("answer_placeholder")

    def test_desaparece_con_la_primera_pregunta_y_vuelve_con_una_cola_nueva(self, raiz, con_clave, monkeypatch):
        app = crear(raiz)
        escribir(app, "a")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista")
        assert app.vacio.winfo_manager() == ""
        monkeypatch.setattr(app, "_confirmar", lambda titulo, texto: True)
        app.nueva_cola()
        assert app.vacio.winfo_manager() == "place"

    def test_si_se_reabre_con_preguntas_pendientes_no_se_ve(self, raiz, con_clave):
        app = TestCierreYReanudacion()._app_con_pendientes(raiz)
        app._salir()
        raiz2 = tk.Tk()
        try:
            app2 = crear(raiz2, guion=[FakeResponse()] * 5)
            bombear(raiz2, timeout=0.3)
            assert app2.vacio.winfo_manager() == ""
        finally:
            raiz2.destroy()


class TestErroresInesperados:
    @pytest.fixture
    def avisos(self, monkeypatch):
        vistos = []
        monkeypatch.setattr(simple.messagebox, "showerror", lambda titulo, texto, **kw: vistos.append((titulo, texto)))
        from claude_batch_monitor.gui import comun
        monkeypatch.setattr(comun.messagebox, "showerror", lambda titulo, texto, **kw: vistos.append((titulo, texto)))
        return vistos

    def test_un_error_en_un_boton_se_avisa_con_el_detalle_y_la_ruta_del_registro(self, raiz, con_clave, avisos):
        from claude_batch_monitor import paths
        crear(raiz)
        raiz.report_callback_exception(ValueError, ValueError("boom"), None)
        titulo, texto = avisos[0]
        assert titulo == "Algo ha fallado" and "ValueError: boom" in texto and str(paths.LOG_FILE) in texto
        assert "no se ha perdido" in texto

    def test_se_guarda_en_el_registro(self, raiz, con_clave, avisos, caplog):
        crear(raiz)
        with caplog.at_level("ERROR"):
            try:
                raise RuntimeError("fallo registrado")
            except RuntimeError:
                import sys
                raiz.report_callback_exception(*sys.exc_info())
        assert "Error inesperado en la ventana" in caplog.text and "fallo registrado" in caplog.text

    def test_el_mismo_error_no_se_repite_en_bucle_pero_otro_distinto_si_se_avisa(self, raiz, con_clave, avisos):
        crear(raiz)
        for _ in range(5):
            raiz.report_callback_exception(ValueError, ValueError("igual"), None)
        raiz.report_callback_exception(KeyError, KeyError("otro"), None)
        assert len(avisos) == 2

    def test_un_error_real_dentro_de_un_callback_de_tk_llega_hasta_el_aviso(self, raiz, con_clave, avisos):
        crear(raiz)
        raiz.after(0, lambda: 1 / 0)
        assert bombear(raiz, lambda: avisos, timeout=3)
        assert "ZeroDivisionError" in avisos[0][1]

    def test_si_avisar_falla_no_se_encadena_otro_error(self, raiz, con_clave, monkeypatch):
        from claude_batch_monitor.gui import comun

        def roto(*a, **k):
            raise RuntimeError("sin ventanas")

        monkeypatch.setattr(comun.messagebox, "showerror", roto)
        crear(raiz)
        raiz.report_callback_exception(ValueError, ValueError("x"), None)    # no debe lanzar


class TestAvisos:
    def _espera_banner(self, raiz, app):
        assert bombear(raiz, lambda: app.banner.winfo_ismapped() and app.banner_texto.cget("text"))
        return app.banner_texto.cget("text"), [b.cget("text") for b in app.banner_botones.winfo_children()]

    def test_tope_de_gasto(self, raiz, con_clave):
        app = crear(raiz, config=cfg(tope_gasto_usd=D("0.004")), guion=[FakeResponse()] * 5)
        for q in "abc":
            escribir(app, q)
            app.enviar()
        texto, botones = self._espera_banner(raiz, app)
        assert "tope de gasto" in texto and botones == ["Subir el tope…"]
        assert app.var_estado.get() == "Detenida."

    def test_subir_el_tope_abre_las_opciones(self, raiz, con_clave, monkeypatch):
        abiertas = []
        app = crear(raiz, config=cfg(tope_gasto_usd=D("0.004")), guion=[FakeResponse()] * 5)
        monkeypatch.setattr(app, "abrir_opciones", lambda: abiertas.append(True))
        for q in "abc":
            escribir(app, q)
            app.enviar()
        self._espera_banner(raiz, app)
        app.banner_botones.winfo_children()[0].invoke()
        assert abiertas == [True]

    def test_sin_saldo_ofrece_la_consola_y_reanudar(self, raiz, con_clave):
        urls = []
        app = crear(raiz, guion=[status_error(402), FakeResponse("ya hay saldo")], abrir_url=urls.append)
        escribir(app, "a")
        app.enviar()
        texto, botones = self._espera_banner(raiz, app)
        assert "sin saldo" in texto and botones == ["Abrir la consola de Anthropic", "Reanudar"]
        app.banner_botones.winfo_children()[0].invoke()
        assert urls == [simple.URL_FACTURACION]
        app.banner_botones.winfo_children()[1].invoke()
        assert bombear(raiz, lambda: filas(app)[0][2] == "Lista")
        assert not app.banner.winfo_ismapped()          # el aviso desaparece al seguir

    def test_limite_de_cuenta_muestra_cuando_vuelve_el_acceso(self, raiz, con_clave):
        app = crear(raiz, guion=[error(429, interno(TOPE_MENSUAL))])
        escribir(app, "a")
        app.enviar()
        bombear(raiz, lambda: app.banner.winfo_ismapped())
        assert "límite de gasto" in app.banner_texto.cget("text")
        assert "2026-09-01 00:00 UTC" in app.banner_detalle.cget("text")

    def test_clave_invalida_en_pleno_uso(self, raiz, con_clave):
        app = crear(raiz, guion=[status_error(401)])
        escribir(app, "a")
        app.enviar()
        texto, botones = self._espera_banner(raiz, app)
        assert "clave de API no es válida" in texto and botones == ["Cambiar la clave…"]

    def test_modelo_que_ya_no_existe(self, raiz, con_clave):
        cuerpo = {"type": "not_found_error", "message": "model: claude-viejo"}
        app = crear(raiz, guion=[error(404, cuerpo)])
        escribir(app, "a")
        app.enviar()
        texto, botones = self._espera_banner(raiz, app)
        assert "modelo" in texto and botones == ["Elegir modelo…"]

    def test_corte_de_red(self, raiz, con_clave):
        corte = anthropic.APIConnectionError(message="reset", request=httpx.Request("POST", "https://x"))
        corte.__cause__ = httpx.ReadError("reiniciada")
        app = crear(raiz, guion=[corte])
        escribir(app, "a")
        app.enviar()
        texto, botones = self._espera_banner(raiz, app)
        assert "conexión" in texto and botones == ["Reanudar"]


class TestOpciones:
    def test_guardar_persiste_modelo_tope_y_arranque(self, raiz, con_clave):
        app = crear(raiz)
        app.aplicar_opciones(modelo="claude-opus-5-5", tope=D("7.5"), auto=True, idioma="en")
        guardada = ConfigManager.cargar()
        assert (guardada.modelo, guardada.tope_gasto_usd, guardada.auto_arranque, guardada.idioma) == (
            "claude-opus-5-5", D("7.5"), True, "en")
        assert "de $7.50" in app.var_gasto.get()

    def test_un_tope_invalido_no_se_guarda(self, raiz, con_clave):
        app = crear(raiz)
        d = simple.DialogoOpciones(app)
        d.tope.set("mucho")
        d.guardar()
        assert d.winfo_exists() and "número" in d.aviso.cget("text")
        assert ConfigManager.cargar().tope_gasto_usd != D("0")
        d.destroy()

    def test_la_coma_decimal_se_acepta(self, raiz, con_clave):
        app = crear(raiz)
        d = simple.DialogoOpciones(app)
        d.tope.set("3,5")
        d.guardar()
        assert app.config.tope_gasto_usd == D("3.5")

    def test_subir_el_tope_tras_pararse_por_el_tope_reanuda_la_cola(self, raiz, con_clave):
        app = crear(raiz, config=cfg(tope_gasto_usd=D("0.004")), guion=[FakeResponse()] * 10)
        for q in "abcd":
            escribir(app, q)
            app.enviar()
        bombear(raiz, lambda: app.cola.estado.tipo == "stopped")
        pendientes = app.cola.pendientes()
        assert pendientes > 0
        app.aplicar_opciones(modelo="claude-sonnet-5-5", tope=D("10"), auto=False, idioma="")
        assert bombear(raiz, lambda: app.cola.pendientes() == 0 and not app.cola.activa)

    def test_si_la_clave_viene_del_entorno_no_se_puede_cambiar_desde_aqui(self, raiz, con_clave):
        d = simple.DialogoOpciones(crear(raiz))
        assert "disabled" in d.boton_clave.state()
        d.destroy()

    def test_cargar_modelos_rellena_la_lista(self, raiz, con_clave):
        d = simple.DialogoOpciones(crear(raiz))
        d._cargar_modelos()
        assert bombear(raiz, lambda: tuple(d.combo.cget("values")) == ("claude-opus-5-5", "claude-sonnet-5-5"))
        assert "2 modelos" in d.aviso.cget("text")
        d.destroy()

    def test_el_precio_del_modelo_se_muestra(self, raiz, con_clave):
        d = simple.DialogoOpciones(crear(raiz))
        assert "$2 entrada / $10 salida" in d.precio.cget("text")
        d.destroy()

    def test_pedir_el_modo_avanzado(self, raiz, con_clave):
        app = crear(raiz)
        app.cambiar_a_avanzado()
        assert app.resultado == "avanzada" and ConfigManager.cargar().interfaz == "avanzada"


class TestPrimeraVez:
    def test_la_primera_vez_se_crea_una_configuracion_prudente(self, raiz):
        from claude_batch_monitor import paths
        assert not paths.CONFIG_FILE.exists()
        c = simple.config_inicial()
        assert c.tope_gasto_usd == D("2") and c.max_tokens == 8000 and c.esfuerzo == "medium"
        assert paths.CONFIG_FILE.exists()

    def test_no_pisa_lo_que_la_persona_ya_eligio(self, raiz):
        ConfigManager.guardar(cfg(tope_gasto_usd=D("50"), esfuerzo=""))
        c = simple.config_inicial()
        assert c.tope_gasto_usd == D("50") and c.esfuerzo == ""


class TestMenuYAcciones:
    def _con_dos(self, raiz):
        app = crear(raiz, guion=[FakeResponse("la primera"), FakeResponse("la segunda")])
        for q in ("uno", "dos"):
            escribir(app, q)
            app.enviar()
        bombear(raiz, lambda: [f[2] for f in filas(app)] == ["Lista", "Lista"])
        return app

    def test_copiar_la_respuesta(self, raiz, con_clave):
        app = self._con_dos(raiz)
        app._seleccionar(1)
        app.copiar_respuesta()
        assert raiz.clipboard_get() == "la primera"

    def test_volver_a_preguntar(self, raiz, con_clave):
        app = self._con_dos(raiz)
        app.volver_a_preguntar(1)
        assert bombear(raiz, lambda: len(filas(app)) == 3)
        assert filas(app)[2][1] == "uno"

    def test_quitar_de_la_cola_solo_lo_pendiente(self, raiz, con_clave):
        app = crear(raiz)
        app.cola.pausar()
        m = app.cola.agregar("sin enviar")
        bombear(raiz, timeout=0.2)
        app.quitar_de_la_cola(m.id)
        bombear(raiz, lambda: filas(app)[0][2] == "Quitada")
        assert app.cola.job.mensaje(m.id).status == STATUS_SKIPPED

    def test_guardar_todo_en_csv(self, raiz, con_clave, tmp_path):
        app = self._con_dos(raiz)
        destino = tmp_path / "salida.csv"
        assert app.exportar_a(str(destino))
        texto = destino.read_text(encoding="utf-8-sig")
        assert "la primera" in texto and "la segunda" in texto

    def test_guardar_en_un_formato_raro_avisa(self, raiz, con_clave, tmp_path, monkeypatch):
        app = self._con_dos(raiz)
        avisos = []
        monkeypatch.setattr(app, "_error", lambda titulo, texto: avisos.append(texto))
        assert app.exportar_a(str(tmp_path / "salida.xlsx")) is False
        assert avisos and "xlsx" in avisos[0]

    def test_guardar_sin_nada_no_abre_un_dialogo(self, raiz, con_clave, monkeypatch):
        app = crear(raiz)
        avisos = []
        monkeypatch.setattr(app, "_info", lambda titulo, texto: avisos.append(texto))
        app.guardar_todo()
        assert avisos == ["Todavía no hay nada que guardar."]

    def test_nueva_cola_vacia_la_lista_pero_deja_la_anterior_en_disco(self, raiz, con_clave, monkeypatch):
        app = self._con_dos(raiz)
        anterior = app.cola.job.job_id
        monkeypatch.setattr(app, "_confirmar", lambda titulo, texto: True)
        app.nueva_cola()
        assert filas(app) == [] and app.cola.job is None
        assert Job.load(anterior) is not None

    def test_nueva_cola_pide_confirmacion(self, raiz, con_clave, monkeypatch):
        app = self._con_dos(raiz)
        monkeypatch.setattr(app, "_confirmar", lambda titulo, texto: False)
        app.nueva_cola()
        assert len(filas(app)) == 2


class TestCierreYReanudacion:
    def test_cerrar_sin_nada_pendiente_sale_directamente(self, raiz, con_clave):
        app = crear(raiz)
        app.cerrar()
        with pytest.raises(tk.TclError):
            raiz.winfo_exists()
            raiz.update()

    def _app_con_pendientes(self, raiz, **kw):
        app = crear(raiz, guion=[], **kw)
        app.cola.pausar()
        for q in ("pendiente 1", "pendiente 2"):
            app.cola.agregar(q)
        bombear(raiz, timeout=0.2)
        app.cola.cerrar()
        return app

    def test_lo_pendiente_se_ofrece_al_volver_a_abrir_y_no_se_envia_solo(self, raiz, con_clave):
        self._app_con_pendientes(raiz)._salir()
        raiz2 = tk.Tk()
        try:
            app2 = crear(raiz2, guion=[FakeResponse()] * 5)
            bombear(raiz2, timeout=0.3)
            assert [f[1] for f in filas(app2)] == ["pendiente 1", "pendiente 2"]
            assert app2.banner.winfo_ismapped() and "2 pregunta" in app2.banner_texto.cget("text")
            assert app2.fab.cliente.messages.llamadas == 0       # NO gasta sin que se lo pidan
            app2.banner_botones.winfo_children()[0].invoke()      # «Reanudar»
            assert bombear(raiz2, lambda: app2.cola.pendientes() == 0 and not app2.cola.activa)
        finally:
            raiz2.destroy()

    def test_con_continuar_sola_activado_se_envia_al_abrir(self, raiz, con_clave):
        self._app_con_pendientes(raiz)._salir()
        raiz2 = tk.Tk()
        try:
            app2 = crear(raiz2, guion=[FakeResponse()] * 5, config=cfg(auto_arranque=True))
            assert bombear(raiz2, lambda: app2.cola.pendientes() == 0 and not app2.cola.activa)
        finally:
            raiz2.destroy()

    def test_cerrar_con_preguntas_en_marcha_pregunta_y_guarda(self, raiz, con_clave, monkeypatch):
        app = crear(raiz, guion=[FakeResponse()] * 5)
        app.cola.agregar("a")
        app.cola.pausar()
        app.cola._pausada = False                       # simula una cola en marcha con algo pendiente
        app.cola.job.agregar_orden("b")

        class Falso:
            def __init__(self, a, n, con_bandeja):
                self.eleccion = "salir"
        monkeypatch.setattr(simple, "DialogoCierre", Falso)
        monkeypatch.setattr(raiz, "wait_window", lambda w: None)
        app.cerrar()
        assert Job.load(app.cola.job.job_id).etiqueta == ETIQUETA

    def test_volver_cancela_el_cierre(self, raiz, con_clave, monkeypatch):
        app = crear(raiz, guion=[FakeResponse()] * 5)
        app.cola.agregar("a")
        app.cola.job.agregar_orden("b")
        app.cola.cerrar()
        app.cola._pausada = False

        class Falso:
            def __init__(self, a, n, con_bandeja):
                self.eleccion = "volver"
        monkeypatch.setattr(simple, "DialogoCierre", Falso)
        monkeypatch.setattr(raiz, "wait_window", lambda w: None)
        app.cerrar()
        assert raiz.winfo_exists()


class TestTema:
    def test_en_oscuro_la_ventana_usa_la_paleta_oscura(self, raiz, con_clave):
        from claude_batch_monitor.gui.estilo import PALETA_OSCURA

        app = crear(raiz, config=cfg(tema="oscuro"))
        assert app.p is PALETA_OSCURA
        assert app.root.cget("background") == PALETA_OSCURA.fondo
        assert app.entrada.cget("background") == PALETA_OSCURA.superficie
        assert app.entrada.cget("insertbackground") == PALETA_OSCURA.texto  # el cursor se ve sobre el fondo oscuro

    def test_en_claro_sigue_igual_que_siempre(self, raiz, con_clave):
        from claude_batch_monitor.gui.estilo import PALETA_CLARA

        app = crear(raiz, config=cfg(tema="claro"))
        assert app.p is PALETA_CLARA and app.root.cget("background") == PALETA_CLARA.fondo

    def test_auto_sigue_al_sistema(self, raiz, con_clave, monkeypatch):
        from claude_batch_monitor.gui import estilo

        monkeypatch.setattr(estilo, "tema_del_sistema", lambda: "oscuro")
        assert crear(raiz, config=cfg(tema="auto")).p.oscura

    def test_las_opciones_ofrecen_los_tres_temas_y_guardan_el_elegido(self, raiz, con_clave):
        app = crear(raiz, config=cfg(tema="auto"))
        d = simple.DialogoOpciones(app)
        assert list(d._temas) == ["Automático", "Claro", "Oscuro"] and d.tema.get() == "Automático"
        d.tema.set("Oscuro")
        d.guardar()
        assert app.config.tema == "oscuro" and ConfigManager.cargar().tema == "oscuro"

    def test_las_opciones_muestran_el_tema_guardado(self, raiz, con_clave):
        d = simple.DialogoOpciones(crear(raiz, config=cfg(tema="claro")))
        assert d.tema.get() == "Claro"
        d.destroy()

    def test_aplicar_opciones_sin_tema_no_lo_toca_y_uno_raro_se_ignora(self, raiz, con_clave):
        app = crear(raiz, config=cfg(tema="oscuro"))
        app.aplicar_opciones(modelo="claude-sonnet-5-5", tope=D("5"), auto=False, idioma="")
        assert app.config.tema == "oscuro"
        app.aplicar_opciones(modelo="claude-sonnet-5-5", tope=D("5"), auto=False, idioma="", tema="rosa")
        assert app.config.tema == "oscuro"

    def test_los_textos_del_tema_existen_en_los_dos_idiomas(self):
        for clave in ("opt_theme", "theme_auto", "theme_light", "theme_dark"):
            fijar_idioma("es")
            es = t(clave)
            fijar_idioma("en")
            en = t(clave)
            assert es != clave and en != clave and es != en

    def test_la_respuesta_con_formato_usa_los_colores_del_tema(self, raiz, con_clave):
        from claude_batch_monitor.gui.estilo import PALETA_OSCURA

        app = crear(raiz, config=cfg(tema="oscuro"), guion=[FakeResponse("Mira `x = 1` y [aquí](https://a.org)")])
        escribir(app, "pregunta")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista")
        app.tabla.selection_set(app.tabla.get_children()[0])
        raiz.update()
        assert app.respuesta.tag_cget("codigo", "background") == PALETA_OSCURA.codigo_fondo
        assert app.respuesta.tag_cget("enlace", "foreground") == PALETA_OSCURA.acento_texto


class TestIdioma:
    def test_la_interfaz_en_ingles(self, raiz, con_clave):
        app = crear(raiz, config=cfg(idioma="en"))
        assert app.root.title() == "Claude · Question queue"
        assert app.boton_enviar.cget("text") == "Send"
        assert app.var_estado.get() == "Ready"

    def test_idioma_desconocido_cae_al_del_sistema(self):
        assert fijar_idioma("klingon") in ("es", "en")

    def test_todas_las_claves_existen_en_los_dos_idiomas(self):
        from claude_batch_monitor.gui.i18n import TEXTOS
        assert set(TEXTOS["es"]) == set(TEXTOS["en"])

    def test_los_marcadores_de_formato_coinciden_entre_idiomas(self):
        import re

        from claude_batch_monitor.gui.i18n import TEXTOS
        for clave, es in TEXTOS["es"].items():
            marcadores = lambda s: sorted(re.findall(r"\{(\w+)\}", s))  # noqa: E731
            assert marcadores(es) == marcadores(TEXTOS["en"][clave]), clave


def test_cerrar_la_ventana_no_deja_hilos_vivos(raiz, con_clave):
    antes = set(threading.enumerate())
    app = crear(raiz)
    escribir(app, "a")
    app.enviar()
    bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista")
    app._salir()
    time.sleep(0.2)
    vivos = [h.name for h in threading.enumerate() if h not in antes and h.name.startswith("cola-")]
    assert vivos == []
