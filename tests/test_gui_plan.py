"""El modo simple con el plan de Claude (Claude Code), con la ventana de verdad.

Necesitan tkinter y una pantalla: en Linux sin escritorio, `xvfb-run -a pytest`. Para lo que habla con Claude Code se
usa el `claude` de mentira (ver claude_falso.py); para lo que solo mira la ventana, una comprobación inyectada.
"""

from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from claude_batch_monitor.claves import ErrorDeClave
from claude_batch_monitor.config import ConfigManager, Configuracion
from claude_batch_monitor.core import base
from claude_batch_monitor.core.claude_code import EstadoClaude, EstadoPlan, VentanaPlan

from .gui_comun import bombear, registrar

tk = pytest.importorskip("tkinter")
pytestmark = pytest.mark.gui

from claude_batch_monitor.gui import comun, simple  # noqa: E402
from claude_batch_monitor.gui.i18n import fijar_idioma, t  # noqa: E402


@pytest.fixture(autouse=True)
def sin_efectos(monkeypatch):
    fijar_idioma("es")
    monkeypatch.setattr(base, "notificar", lambda *a, **k: None)
    yield
    fijar_idioma("es")


def cfg(**kw):
    base_ = dict(modelo="claude-sonnet-5-5", max_tokens=512, tope_gasto_usd=D("5"),
                 precio_input_mtok=D("1"), precio_output_mtok=D("5"))
    base_.update(kw)
    return Configuracion(**base_)


def con_plan(**kw):
    return cfg(modo="plan", **kw)


def estado(codigo="ok", **kw):
    return EstadoClaude(codigo, "/usr/local/bin/claude", "2.1.296", **kw)


class Comprobador:
    """Una comprobación de Claude Code que devuelve, una tras otra, los estados dados (el último se repite)."""

    def __init__(self, *estados):
        self.estados = list(estados) or [estado()]
        self.llamadas = []
        self.rutas = []

    def __call__(self, ruta="", usar_cache=True):
        self.llamadas.append(usar_cache)
        self.rutas.append(ruta)
        return self.estados[min(len(self.llamadas), len(self.estados)) - 1]


def crear(raiz, config=None, comprobar=None, **kw):
    kw.setdefault("probar", lambda k: ["claude-sonnet-5-5", "claude-haiku-4-5"])
    kw.setdefault("listar", lambda k: ["claude-sonnet-5-5"])
    kw.setdefault("guardar", lambda k: "llavero")
    kw.setdefault("abrir_url", lambda u: None)
    kw["comprobar_plan"] = comprobar or Comprobador()
    app = simple.SimpleApp(raiz, config=config or cfg(), usar_bandeja=False, **kw)
    return registrar(app)


def filas(app):
    return [app.tabla.item(i, "values") for i in app.tabla.get_children()]


def escribir(app, texto):
    app._quitar_placeholder()
    app.entrada.insert("1.0", texto)


def banner_visible(app):
    return app.banner.winfo_manager() == "grid"


def activado(boton):
    return "disabled" not in boton.state()


# ----------------------------------------------------------------------


class TestBienvenida:
    def test_ofrece_primero_el_plan_y_luego_la_clave_de_api(self, raiz):
        app = crear(raiz)
        assert app.pantalla == "bienvenida"
        assert [app.pestanas.tab(i, "text") for i in (0, 1)] == [t("welcome_tab_plan"), t("welcome_tab_api")]
        assert app.pestanas.index(app.pestanas.select()) == 0

    def test_con_claude_code_listo_se_usa_el_plan_y_se_recuerda(self, raiz):
        app = crear(raiz, comprobar=Comprobador(estado("ok", metodo="claude.ai")))
        assert bombear(raiz, lambda: app.bienvenida_estado.cget("text") == t("plan_ok"))
        assert activado(app.boton_usar_plan)
        app.usar_plan()
        assert app.pantalla == "principal" and app.config.es_plan
        assert ConfigManager.cargar().modo == "plan"               # la próxima vez entra directamente

    @pytest.mark.parametrize("codigo, clave", [
        ("no_instalado", "plan_no_instalado"), ("antiguo", "plan_antiguo"),
        ("sin_sesion", "plan_sin_sesion"), ("no_es_plan", "plan_no_es_plan"),
    ])
    def test_si_algo_falta_se_explica_y_no_se_deja_continuar(self, raiz, codigo, clave):
        app = crear(raiz, comprobar=Comprobador(estado(codigo)))
        assert bombear(raiz, lambda: app.bienvenida_estado.cget("text") == t(clave))
        assert not activado(app.boton_usar_plan) and app.pantalla == "bienvenida"

    def test_comprobar_de_nuevo_vuelve_a_mirar_sin_usar_lo_recordado(self, raiz):
        comprobar = Comprobador(estado("sin_sesion"), estado("ok"))
        app = crear(raiz, comprobar=comprobar)
        assert bombear(raiz, lambda: app.bienvenida_estado.cget("text") == t("plan_sin_sesion"))
        app.boton_comprobar.invoke()
        assert bombear(raiz, lambda: app.bienvenida_estado.cget("text") == t("plan_ok"))
        assert activado(app.boton_usar_plan)
        assert comprobar.llamadas[-1] is False                      # a propósito: la persona acaba de arreglarlo

    def test_mientras_comprueba_no_se_puede_pulsar_nada_que_dependa_del_resultado(self, raiz):
        app = crear(raiz)
        assert app.bienvenida_estado.cget("text") == t("plan_checking")
        assert not activado(app.boton_usar_plan)

    def test_si_no_se_pudo_comprobar_se_deja_intentar(self, raiz):
        app = crear(raiz, comprobar=Comprobador(estado("error", detalle="no respondió a tiempo")))
        assert bombear(raiz, lambda: "no respondió a tiempo" in app.bienvenida_estado.cget("text"))
        assert activado(app.boton_usar_plan)

    def test_pegar_una_clave_elige_la_api(self, raiz):
        app = crear(raiz)
        app.config.modo = "plan"
        app.panel_clave.var.set("sk-ant-buena")
        app.panel_clave.probar()
        assert bombear(raiz, lambda: app.pantalla == "principal")
        assert not app.config.es_plan and ConfigManager.cargar().modo == "inmediato"

    def test_sin_clave_y_sin_plan_el_envio_lleva_a_la_pestana_de_la_clave(self, raiz):
        app = crear(raiz, config=cfg(modo="inmediato"))
        app.pantalla = "principal"
        escribir(app, "hola")
        app.enviar()
        assert app.pantalla == "bienvenida" and app.pestanas.index(app.pestanas.select()) == 1

    def test_con_la_libreria_de_la_api_no_hay_aviso(self, raiz):
        assert crear(raiz).aviso_libreria is None

    def test_si_falta_la_libreria_de_la_api_se_dice_antes_de_pegar_la_clave(self, raiz, monkeypatch):
        from claude_batch_monitor import instalacion

        monkeypatch.setattr(instalacion, "falta_anthropic", lambda: True)
        app = crear(raiz)
        assert app.aviso_libreria is not None
        texto = app.aviso_libreria.cget("text")
        assert "anthropic" in texto and instalacion.comando_visible() in texto and "plan de Claude no hace falta" in texto

    def test_si_al_probar_la_clave_falta_la_libreria_se_explica_y_no_se_guarda_nada(self, raiz):
        guardadas = []

        def sin_libreria(clave):
            raise ErrorDeClave("libreria")

        app = crear(raiz, probar=sin_libreria, guardar=lambda k: (guardadas.append(k), "llavero")[1])
        app.panel_clave.var.set("sk-ant-x")
        app.panel_clave.probar()
        assert bombear(raiz, lambda: "anthropic" in app.panel_clave.mensaje.cget("text"))
        assert guardadas == [] and app.pantalla == "bienvenida" and activado(app.panel_clave.boton)

    def test_el_enlace_para_instalar_claude_code(self, raiz):
        abiertas = []
        app = crear(raiz, abrir_url=abiertas.append)
        for hijo in app.boton_comprobar.master.winfo_children():
            if hijo.cget("text") == t("plan_install"):
                hijo.invoke()
        assert abiertas == [comun.URL_CLAUDE_CODE]


class TestElegirDondeEstaClaudeCode:
    """Claude Code en un sitio raro (nvm, un disco aparte…): si no se encuentra solo, se puede señalar a mano."""

    @staticmethod
    def _programa(tmp_path):
        elegido = tmp_path / "mi-claude"
        elegido.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        elegido.chmod(0o755)
        return str(elegido)

    @staticmethod
    def _botones(frame):
        return {hijo.cget("text"): hijo for hijo in frame.winfo_children() if hijo.winfo_class() == "TButton"}

    def test_desde_la_bienvenida_se_guarda_la_ruta_y_se_vuelve_a_comprobar(self, raiz, monkeypatch, tmp_path):
        elegido = self._programa(tmp_path)
        monkeypatch.setattr(simple.filedialog, "askopenfilename", lambda **kw: elegido)
        comprobar = Comprobador(estado("no_instalado"), estado("ok", metodo="claude.ai"))
        app = crear(raiz, comprobar=comprobar)
        assert bombear(raiz, lambda: app.bienvenida_estado.cget("text") == t("plan_no_instalado"))
        assert not activado(app.boton_usar_plan)
        self._botones(app.boton_comprobar.master)[t("plan_pick")].invoke()
        assert app.config.ruta_claude == elegido and ConfigManager.cargar().ruta_claude == elegido
        assert bombear(raiz, lambda: app.bienvenida_estado.cget("text") == t("plan_ok"))
        assert activado(app.boton_usar_plan) and comprobar.rutas[-1] == elegido and comprobar.llamadas[-1] is False

    def test_si_se_cierra_el_selector_no_cambia_nada(self, raiz, monkeypatch):
        monkeypatch.setattr(simple.filedialog, "askopenfilename", lambda **kw: "")
        comprobar = Comprobador(estado("no_instalado"))
        app = crear(raiz, comprobar=comprobar)
        assert bombear(raiz, lambda: app.bienvenida_estado.cget("text") == t("plan_no_instalado"))
        antes = len(comprobar.llamadas)
        app.elegir_ruta_de_claude()
        assert app.config.ruta_claude == "" and len(comprobar.llamadas) == antes

    def test_el_aviso_de_que_no_esta_instalado_trae_el_boton_y_los_otros_avisos_no(self, raiz):
        app = crear(raiz, config=con_plan(), comprobar=Comprobador(estado("no_instalado")))
        assert bombear(raiz, lambda: banner_visible(app))
        assert t("plan_pick") in self._botones(app.banner_botones)
        app2 = crear(raiz, config=con_plan(), comprobar=Comprobador(estado("sin_sesion")))
        assert bombear(raiz, lambda: banner_visible(app2))
        assert t("plan_pick") not in self._botones(app2.banner_botones)

    def test_desde_el_aviso_la_cola_sigue_sola_en_cuanto_se_encuentra(self, raiz, monkeypatch, tmp_path):
        elegido = self._programa(tmp_path)
        monkeypatch.setattr(simple.filedialog, "askopenfilename", lambda **kw: elegido)
        app = crear(raiz, config=con_plan(), comprobar=Comprobador(estado("no_instalado"), estado("ok")))
        assert bombear(raiz, lambda: banner_visible(app))
        self._botones(app.banner_botones)[t("plan_pick")].invoke()
        assert bombear(raiz, lambda: not banner_visible(app))
        assert app.config.ruta_claude == elegido


class TestArranqueConElPlan:
    def test_con_el_plan_elegido_se_entra_directamente_sin_clave(self, raiz):
        app = crear(raiz, config=con_plan())
        assert app.pantalla == "principal"

    def test_si_claude_code_no_esta_listo_un_aviso_lo_explica_y_se_quita_al_arreglarlo(self, raiz):
        comprobar = Comprobador(estado("sin_sesion"), estado("ok"))
        app = crear(raiz, config=con_plan(), comprobar=comprobar)
        assert bombear(raiz, lambda: banner_visible(app))
        assert app.banner_texto.cget("text") == t("plan_sin_sesion")
        app.comprobar_plan(usar_cache=False)
        assert bombear(raiz, lambda: not banner_visible(app))

    def test_el_aviso_sobrevive_a_los_cambios_de_estado_de_la_cola(self, raiz):
        app = crear(raiz, config=con_plan(), comprobar=Comprobador(estado("no_instalado")))
        assert bombear(raiz, lambda: banner_visible(app))
        app.on_estado(app.cola.estado)                              # la cola sigue en reposo, pero avisa
        assert banner_visible(app)

    def test_si_no_se_pudo_comprobar_no_se_alarma(self, raiz):
        app = crear(raiz, config=con_plan(), comprobar=Comprobador(estado("error", detalle="lento")))
        assert bombear(raiz, lambda: app.estado_claude is not None)
        assert not banner_visible(app)

    def test_con_la_clave_de_api_no_se_comprueba_claude_code(self, raiz, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-de-prueba")
        comprobar = Comprobador()
        app = crear(raiz, comprobar=comprobar)
        assert app.pantalla == "principal" and comprobar.llamadas == []


class TestPreguntarConElPlan:
    def test_se_responde_con_el_plan_sin_clave_y_sin_dinero(self, raiz, claude):
        app = crear(raiz, config=con_plan())
        escribir(app, "¿Qué es un token?")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista", timeout=20)
        assert "Respuesta a: ¿Qué es un token?" in app.respuesta.get("1.0", "end")
        assert "$" not in app.var_gasto.get()                       # nada de dinero: es del plan
        assert app.var_gasto.get().startswith(("Plan:", t("plan_included")))
        assert not [x for x in claude.llamadas() if x["env"].get("ANTHROPIC_API_KEY")]

    def test_el_modelo_elegido_llega_a_claude_code(self, raiz, claude):
        app = crear(raiz, config=con_plan(modelo_plan="opus"))
        escribir(app, "Una pregunta")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista", timeout=20)
        assert claude.preguntas()[0]["valores"]["--model"] == "opus"

    def test_el_uso_del_plan_se_ve_abajo_y_en_el_idioma_de_la_ventana(self, raiz):
        app = crear(raiz, config=con_plan())
        manana = datetime.now().astimezone() + timedelta(days=1)
        app.on_plan(EstadoPlan(sesion=VentanaPlan(13.0, datetime.now().astimezone() + timedelta(hours=2)),
                               semana=VentanaPlan(41.6, manana)))
        texto = app.var_gasto.get()
        assert texto.startswith("Plan: sesión 13 % (se renueva a las ") and "semana 42 % (se renueva el " in texto
        fijar_idioma("en")
        assert comun.texto_del_plan(app._estado_plan).startswith("Plan: session 13 % (renews at ")
        assert "week 42 % (renews on " in comun.texto_del_plan(app._estado_plan)

    def test_un_limite_del_plan_se_cuenta_en_pantalla_y_la_cola_sigue_sola(self, raiz, claude, esperas, sin_margen):
        app = crear(raiz, config=con_plan())
        escribir(app, "Una pregunta [[plan:limite(3),ok]]")
        app.enviar()
        assert bombear(raiz, lambda: "límite de tu plan" in app.var_estado.get(), timeout=20)
        texto = app.var_estado.get()
        assert "Sigo sola a las " in texto and "(faltan " in texto
        assert filas(app)[0][2] == "Esperando…"
        assert bombear(raiz, lambda: filas(app)[0][2] == "Lista", timeout=30)
        assert claude.veces("Una pregunta") == 2

    def test_el_uso_extra_para_la_cola_guardando_la_respuesta(self, raiz, claude):
        app = crear(raiz, config=con_plan())
        escribir(app, "Una [[plan:extra]]")
        app.enviar()
        assert bombear(raiz, lambda: banner_visible(app), timeout=20)
        assert app.banner_texto.cget("text") == t("banner_plan_extra")
        assert filas(app)[0][2] == "Lista"                          # lo respondido no se pierde

    @pytest.mark.parametrize("marca, clave", [
        ("auth", "banner_plan_auth"), ("creditos", "banner_plan_credits"), ("gasto", "banner_plan_spend"),
        ("cuenta", "banner_plan_account"), ("modelo", "banner_plan_model"), ("limite_sin_hora", "banner_plan_nohour"),
    ])
    def test_cada_problema_tiene_su_aviso(self, raiz, claude, monkeypatch, marca, clave):
        monkeypatch.setattr("claude_batch_monitor.core.plan.MAX_ESPERAS_SIN_REINICIO", 1)
        monkeypatch.setattr("claude_batch_monitor.core.plan.ESPERAS_SIN_REINICIO_SEC", (0.05,))
        app = crear(raiz, config=con_plan())
        escribir(app, f"Una [[plan:{marca}]]")
        app.enviar()
        assert bombear(raiz, lambda: banner_visible(app), timeout=20)
        assert app.banner_texto.cget("text") == t(clave)

    def test_si_claude_code_no_esta_se_dice_al_enviar(self, raiz):
        app = crear(raiz, config=con_plan(), comprobar=Comprobador(estado("ok")))   # (la ventana cree que sí)
        escribir(app, "Una")
        app.enviar()                                                # pero el programa de verdad no está
        assert bombear(raiz, lambda: banner_visible(app) and app.cola.estado.tipo == "stopped", timeout=20)
        assert app.banner_texto.cget("text") == t("plan_no_instalado")
        assert app.cola.pendientes() == 1                           # la pregunta no se pierde

    def test_una_clave_de_api_en_el_entorno_no_cambia_el_modo(self, raiz, claude, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-secreta")
        app = crear(raiz, config=con_plan())
        escribir(app, "Una")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista", timeout=20)
        assert all(not x["env"].get("ANTHROPIC_API_KEY") for x in claude.llamadas())


class TestOpciones:
    def abrir(self, app):
        d = simple.DialogoOpciones(app)
        return d

    def test_elegir_el_plan_se_guarda(self, raiz, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-de-prueba")
        app = crear(raiz, config=cfg())
        d = self.abrir(app)
        assert d.acceso.get() == "api" and d.zona_api.winfo_manager() == "grid"
        d.acceso.set("plan")
        d._cambiar_acceso()
        assert d.zona_api.winfo_manager() == "" and d.zona_plan.winfo_manager() == "grid"
        d.modelo_plan.set("sonnet")
        d.guardar()
        assert app.config.es_plan and app.config.modelo_plan == "sonnet"
        guardado = ConfigManager.cargar()
        assert guardado.modo == "plan" and guardado.modelo_plan == "sonnet"
        assert "$" not in app.var_gasto.get()

    def test_el_modelo_del_plan_por_defecto_se_guarda_en_blanco(self, raiz):
        app = crear(raiz, config=con_plan(modelo_plan="opus"))
        d = self.abrir(app)
        d.modelo_plan.set(t("opt_plan_model_default"))
        d.guardar()
        assert app.config.modelo_plan == ""

    def test_un_modelo_con_caracteres_raros_se_rechaza(self, raiz):
        app = crear(raiz, config=con_plan())
        d = self.abrir(app)
        d.modelo_plan.set("sonnet; rm -rf /")
        d.guardar()
        assert d.aviso.cget("text") == t("opt_bad_plan_model") and app.config.modelo_plan == ""

    def test_con_el_plan_no_se_valida_el_tope_de_gasto(self, raiz):
        app = crear(raiz, config=con_plan())
        d = self.abrir(app)
        d.tope.set("esto no es un número")
        d.guardar()
        assert app.config.tope_gasto_usd == D("5")                  # no se tocó
        assert not d.winfo_exists()                                 # y se guardó (el diálogo se cerró) sin quejarse

    def test_con_la_clave_si_se_valida_el_tope(self, raiz, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-de-prueba")
        app = crear(raiz, config=cfg())
        d = self.abrir(app)
        d.tope.set("esto no es un número")
        d.guardar()
        assert d.aviso.cget("text") == t("opt_bad_cap")

    def test_volver_a_la_clave_deja_el_modo_normal(self, raiz, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-de-prueba")
        app = crear(raiz, config=con_plan())
        d = self.abrir(app)
        d.acceso.set("api")
        d._cambiar_acceso()
        d.guardar()
        assert app.config.modo == "inmediato"

    def test_cambiar_entre_clave_y_clave_no_pisa_el_modo_lote_de_la_ventana_avanzada(self, raiz, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-de-prueba")
        app = crear(raiz, config=cfg(modo="lote"))
        d = self.abrir(app)
        d.guardar()
        assert app.config.modo == "lote"

    def test_la_franja_se_guarda_normalizada(self, raiz):
        app = crear(raiz, config=con_plan())
        d = self.abrir(app)
        assert not d.usar_franja.get() and not activado(d.entrada_desde)
        d.usar_franja.set(True)
        d._cambiar_franja()
        assert activado(d.entrada_desde)
        d.franja_desde.set("7:05")
        d.franja_hasta.set("22:00")
        d.guardar()
        assert app.config.franja == "07:05-22:00" and ConfigManager.cargar().franja == "07:05-22:00"

    @pytest.mark.parametrize("desde, hasta", [("25:00", "07:00"), ("22:00", "22:00"), ("", ""), ("abc", "07:00")])
    def test_una_franja_que_no_vale_se_rechaza(self, raiz, desde, hasta):
        app = crear(raiz, config=con_plan())
        d = self.abrir(app)
        d.usar_franja.set(True)
        d.franja_desde.set(desde)
        d.franja_hasta.set(hasta)
        d.guardar()
        assert d.aviso.cget("text") == t("opt_bad_window") and app.config.franja == ""

    def test_quitar_la_franja(self, raiz):
        app = crear(raiz, config=con_plan(franja="22:00-07:00"))
        d = self.abrir(app)
        assert d.usar_franja.get() and d.franja_desde.get() == "22:00" and d.franja_hasta.get() == "07:00"
        d.usar_franja.set(False)
        d.guardar()
        assert app.config.franja == ""

    def test_las_opciones_enseñan_el_estado_de_claude_code(self, raiz):
        app = crear(raiz, config=con_plan(), comprobar=Comprobador(estado("sin_sesion"), estado("ok")))
        assert bombear(raiz, lambda: app.estado_claude is not None)
        d = self.abrir(app)
        assert d.estado_claude.cget("text") == t("plan_sin_sesion")
        d._comprobar_claude()
        assert bombear(raiz, lambda: d.estado_claude.cget("text") == t("plan_ok"))

    def test_cambiar_a_plan_comprueba_claude_code_y_avisa_si_no_esta_listo(self, raiz, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-de-prueba")
        app = crear(raiz, config=cfg(), comprobar=Comprobador(estado("no_instalado")))
        d = self.abrir(app)
        d.acceso.set("plan")
        d._cambiar_acceso()
        d.guardar()
        assert bombear(raiz, lambda: banner_visible(app))
        assert app.banner_texto.cget("text") == t("plan_no_instalado")

    def test_arreglar_el_modelo_en_opciones_reintenta_la_cola_parada(self, raiz, claude):
        app = crear(raiz, config=con_plan(modelo_plan="modelo-que-no-existe"))
        escribir(app, "Una [[plan:modelo,ok]]")
        app.enviar()
        assert bombear(raiz, lambda: banner_visible(app) and app.cola.estado.tipo == "stopped", timeout=20)
        d = self.abrir(app)
        d.modelo_plan.set("sonnet")
        d.guardar()
        assert bombear(raiz, lambda: filas(app)[0][2] == "Lista", timeout=30)


class TestFranja:
    def test_fuera_de_la_franja_se_dice_cuando_sigue_y_se_puede_pausar(self, raiz, claude):
        ahora = datetime.now()
        fuera = f"{(ahora + timedelta(hours=2)):%H:%M}-{(ahora + timedelta(hours=3)):%H:%M}"
        app = crear(raiz, config=con_plan(franja=fuera))
        escribir(app, "Una pregunta")
        app.enviar()
        assert bombear(raiz, lambda: "franja horaria" in app.var_estado.get(), timeout=20)
        texto = app.var_estado.get()
        assert texto.startswith("Fuera de tu franja horaria. Sigo sola a las ") and "(faltan 1:" in texto
        assert claude.preguntas() == []                             # no se preguntó nada fuera de la franja
        app.alternar_pausa()
        assert bombear(raiz, lambda: app.cola.estado.tipo == "paused", timeout=20)
        assert filas(app)[0][2] == "En cola"

    def test_dentro_de_la_franja_trabaja_con_normalidad(self, raiz, claude):
        ahora = datetime.now()
        dentro = f"{(ahora - timedelta(hours=1)):%H:%M}-{(ahora + timedelta(hours=1)):%H:%M}"
        app = crear(raiz, config=con_plan(franja=dentro))
        escribir(app, "Una pregunta")
        app.enviar()
        assert bombear(raiz, lambda: filas(app) and filas(app)[0][2] == "Lista", timeout=20)
