"""El modo avanzado con el plan de Claude (Claude Code), con la ventana de verdad y un `claude` de mentira.

Necesitan tkinter y una pantalla: en Linux sin escritorio, `xvfb-run -a pytest`. Igual que en test_gui_avanzada.py, los
diálogos del sistema se sustituyen por registros.
"""

from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core import base
from claude_batch_monitor.core.claude_code import EstadoClaude
from claude_batch_monitor.estimator import estimar
from claude_batch_monitor.job import Job

from .gui_comun import bombear, registrar

tk = pytest.importorskip("tkinter")
pytestmark = pytest.mark.gui

from claude_batch_monitor.gui import app as modulo  # noqa: E402
from claude_batch_monitor.gui.app import AppAvanzada, ErrorDeAjustes  # noqa: E402
from claude_batch_monitor.gui.dialogos import DialogoEstimacion  # noqa: E402


@pytest.fixture(autouse=True)
def sin_efectos(monkeypatch):
    monkeypatch.setattr(base, "notificar", lambda *a, **k: None)


def cfg(**kw):
    base_ = dict(modelo="claude-sonnet-5-5", max_tokens=512, tope_gasto_usd=D("5"),
                 precio_input_mtok=D("1"), precio_output_mtok=D("5"), modo="plan")
    base_.update(kw)
    return Configuracion(**base_)


def crear(raiz, config=None, comprobar=None, **kw):
    kw.setdefault("abrir_url", lambda u: None)
    kw.setdefault("abrir", lambda p: True)
    if comprobar is not None:
        kw["comprobar_plan"] = comprobar
    app = AppAvanzada(raiz, config=config or cfg(), usar_bandeja=False, arrancar=False, **kw)
    app.dialogos = []
    app.responde_si = True
    app._info = lambda t, x: app.dialogos.append(("info", t, x))
    app._aviso = lambda t, x: app.dialogos.append(("aviso", t, x))
    app._error = lambda t, x: app.dialogos.append(("error", t, x))
    app._confirmar = lambda t, x: (app.dialogos.append(("confirmar", t, x)), app.responde_si)[1]
    return registrar(app)


def escribir(app, *ordenes):
    app.txt_ordenes.delete("1.0", "end")
    app.txt_ordenes.insert("1.0", "\n\n---\n\n".join(ordenes))
    app._al_cambiar()


def dialogos(app, tipo):
    return [d for d in app.dialogos if d[0] == tipo]


def filas(app):
    return [app.tabla.item(i, "values") for i in app.tabla.get_children()]


def terminado(app):
    return not app._ocupado and not app.ejecutor.activo


def activado(w):
    return "disabled" not in w.state()


# ----------------------------------------------------------------------


class TestAjustesDelPlan:
    def test_las_casillas_del_plan_se_vuelcan_y_se_leen(self, raiz):
        app = crear(raiz, config=cfg(modelo_plan="opus", franja="22:00-07:00"))
        assert app.v["modo"].get() == "plan" and app.v["modelo_plan"].get() == "opus"
        assert app.v["franja"].get() == "22:00-07:00"
        c = app.leer_ajustes()
        assert c.es_plan and c.modelo_plan == "opus" and c.franja == "22:00-07:00"

    def test_la_franja_se_normaliza_y_vacia_es_a_cualquier_hora(self, raiz):
        app = crear(raiz)
        app.v["franja"].set(" 7:00 - 22:30 ")
        assert app.leer_ajustes().franja == "07:00-22:30"
        app.v["franja"].set("")
        assert app.leer_ajustes().franja == ""

    @pytest.mark.parametrize("franja", ["25:00-07:00", "22:00-22:00", "de noche", "22:00"])
    def test_una_franja_que_no_vale_se_avisa(self, raiz, franja):
        app = crear(raiz)
        app.v["franja"].set(franja)
        with pytest.raises(ErrorDeAjustes, match="Franja horaria"):
            app.leer_ajustes()

    def test_un_modelo_con_caracteres_raros_se_avisa(self, raiz):
        app = crear(raiz)
        app.v["modelo_plan"].set("sonnet; echo hola")
        with pytest.raises(ErrorDeAjustes, match="Modelo con el plan"):
            app.leer_ajustes()

    def test_los_ajustes_del_plan_se_guardan_en_el_archivo(self, raiz):
        from claude_batch_monitor.config import ConfigManager

        app = crear(raiz)
        app.v["modelo_plan"].set("haiku")
        app.v["franja"].set("23:00-06:00")
        assert app.guardar_ajustes() is not None
        guardada = ConfigManager.cargar()
        assert guardada.modo == "plan" and guardada.modelo_plan == "haiku" and guardada.franja == "23:00-06:00"

    def test_con_el_plan_no_sale_el_aviso_de_la_clave(self, raiz):
        app = crear(raiz, config=cfg(modo="inmediato"))
        assert app.aviso_clave.winfo_manager() == "grid"            # sin clave y con la API: hace falta
        app.v["modo"].set("plan")
        assert app.aviso_clave.winfo_manager() == ""                # con el plan: no
        app.v["modo"].set("lote")
        assert app.aviso_clave.winfo_manager() == "grid"

    def test_las_casillas_que_no_van_con_el_plan_se_apagan(self, raiz):
        app = crear(raiz, config=cfg(modo="inmediato"))
        sin_bucle = (app.spin_concurrencia, app.check_bucle, app.spin_pausa_ciclos)
        assert all(activado(w) for w in (*sin_bucle, app.spin_pausa_msgs))
        app.v["modo"].set("plan")
        assert not any(activado(w) for w in sin_bucle) and activado(app.spin_pausa_msgs)
        app.v["modo"].set("lote")
        assert not any(activado(w) for w in (*sin_bucle, app.spin_pausa_msgs))
        app.v["modo"].set("inmediato")
        assert all(activado(w) for w in (*sin_bucle, app.spin_pausa_msgs))

    def test_comprobar_claude_code_cuenta_que_pasa(self, raiz):
        estados = [EstadoClaude("sin_sesion", "/x/claude", "2.1.296"), EstadoClaude("ok", "/x/claude", "2.1.296")]
        llamadas = []

        def comprobar(ruta="", usar_cache=True):
            llamadas.append(usar_cache)
            return estados[len(llamadas) - 1]

        app = crear(raiz, comprobar=comprobar)
        app.comprobar_claude()
        assert app.var_claude.get() == "Comprobando…" and not activado(app.boton_claude)
        assert bombear(raiz, lambda: "no has iniciado sesión" in app.var_claude.get())
        assert activado(app.boton_claude)
        app.comprobar_claude()
        assert bombear(raiz, lambda: "listo y usa tu plan" in app.var_claude.get())
        assert llamadas == [False, False]                           # a propósito: lo que se mira es lo de ahora


class TestEstimarYConfirmar:
    def test_la_estimacion_con_el_plan_no_habla_de_dinero(self, raiz):
        app = crear(raiz, config=cfg(tope_gasto_usd=D("3")))
        c = app.leer_ajustes()
        d = DialogoEstimacion(app, estimar(c, ["una orden", "otra"]), c.tope_gasto_usd)
        assert any("incluido en tu plan" in linea for linea in d.lineas)
        assert not any("Tope" in linea or "máximo" in linea.lower() for linea in d.lineas)
        d.destroy()

    def test_la_confirmacion_habla_del_cupo_y_de_la_franja(self, raiz):
        app = crear(raiz, config=cfg(modelo_plan="opus", franja="22:00-07:00"))
        texto = app._texto_confirmacion(app.leer_ajustes(), ["una", "otra"])
        assert "Órdenes: 2" in texto and "Modelo: opus" in texto and "plan de Claude" in texto
        assert "Franja horaria: 22:00-07:00" in texto
        assert "Coste máximo" not in texto and "Tope de gasto" not in texto and texto.endswith("¿Iniciar?")

    def test_sin_modelo_elegido_se_dice_el_del_plan(self, raiz):
        app = crear(raiz)
        assert "Modelo: plan de Claude" in app._texto_confirmacion(app.leer_ajustes(), ["una"])

    def test_la_confirmacion_de_la_api_no_cambia_salvo_la_franja(self, raiz):
        app = crear(raiz, config=cfg(modo="inmediato", franja="08:00-20:00"))
        texto = app._texto_confirmacion(app.leer_ajustes(), ["una"])
        assert "Coste máximo estimado" in texto and "Tope de gasto" in texto and "Franja horaria: 08:00-20:00" in texto


class TestLanzarConElPlan:
    def test_se_inicia_sin_ninguna_clave_y_se_responde(self, raiz, claude):
        app = crear(raiz)
        escribir(app, "Una pregunta", "Otra pregunta")
        assert app.iniciar(confirmar=False)
        assert bombear(raiz, lambda: terminado(app), timeout=30)
        assert [f[2] for f in filas(app)] == ["Lista", "Lista"]
        assert "Respuesta a: Una pregunta" in app.texto_de_respuesta(1)
        assert app.var_gasto.get() == "Incluido en tu plan"
        assert app.etiqueta_plan.winfo_manager() == "grid" and app.var_plan.get()
        assert [d for d in dialogos(app, "error")] == []            # nadie pidió una clave

    def test_pide_confirmacion_con_el_texto_del_plan(self, raiz, claude):
        app = crear(raiz)
        escribir(app, "Una")
        assert app.iniciar(confirmar=True)
        assert "plan de Claude" in dialogos(app, "confirmar")[0][2]
        assert bombear(raiz, lambda: terminado(app), timeout=30)

    def test_el_bucle_no_existe_con_el_plan(self, raiz, claude):
        app = crear(raiz, config=cfg(modo_continuo=True))
        escribir(app, "Una")
        assert not app.iniciar(confirmar=False)
        assert "bucle" in dialogos(app, "error")[0][2].lower() and claude.preguntas() == []

    def test_el_lote_guarda_el_modo_y_el_modelo_elegidos(self, raiz, claude):
        app = crear(raiz, config=cfg(modelo_plan="sonnet"))
        escribir(app, "Una")
        assert app.iniciar(confirmar=False)
        assert bombear(raiz, lambda: terminado(app), timeout=30)
        assert (app.job.modo, app.job.modelo) == ("plan", "sonnet")
        assert claude.preguntas()[0]["valores"]["--model"] == "sonnet"

    def test_el_uso_extra_avisa_sin_hablar_de_saldo(self, raiz, claude):
        app = crear(raiz)
        escribir(app, "Una [[plan:extra]]", "Otra")
        assert app.iniciar(confirmar=False)
        assert bombear(raiz, lambda: terminado(app), timeout=30)
        _, titulo, texto = dialogos(app, "aviso")[0]
        assert titulo == "Lote detenido" and "créditos extra" in texto and "saldo" not in texto
        assert "incluido en tu plan" in texto                       # y el resumen no habla de dólares
        assert filas(app)[0][2] == "Lista" and filas(app)[1][2] == "En cola"

    def test_un_limite_del_plan_espera_y_sigue_en_el_panel(self, raiz, claude, esperas, sin_margen):
        app = crear(raiz)
        escribir(app, "Una [[plan:limite(3),ok]]")
        assert app.iniciar(confirmar=False)
        assert bombear(raiz, lambda: "límite de tu plan" in app.var_estado.get(), timeout=20)
        assert bombear(raiz, lambda: terminado(app), timeout=40)
        assert filas(app)[0][2] == "Lista" and claude.veces("Una") == 2

    def test_fuera_de_la_franja_espera_y_se_puede_detener(self, raiz, claude):
        ahora = datetime.now()
        fuera = f"{(ahora + timedelta(hours=2)):%H:%M}-{(ahora + timedelta(hours=3)):%H:%M}"
        app = crear(raiz, config=cfg(franja=fuera))
        escribir(app, "Una")
        assert app.iniciar(confirmar=False)
        assert bombear(raiz, lambda: "Fuera de la franja horaria" in app.var_estado.get(), timeout=20)
        assert claude.preguntas() == []
        app.detener()
        assert bombear(raiz, lambda: terminado(app), timeout=20)
        assert filas(app)[0][2] == "En cola"

    def test_con_la_api_la_franja_tambien_vale(self, raiz):
        """La franja no es solo del plan: el motor de la API la respeta igual."""
        from .test_cola import fabrica_con
        from .test_engine import FakeResponse

        ahora = datetime.now()
        fuera = f"{(ahora + timedelta(hours=2)):%H:%M}-{(ahora + timedelta(hours=3)):%H:%M}"
        fab = fabrica_con([FakeResponse("respuesta")] * 3)
        app = crear(raiz, config=cfg(modo="inmediato", franja=fuera), fabrica=fab)
        escribir(app, "Una")
        app._asegurar_clave = lambda plan=False: True
        assert app.iniciar(confirmar=False)
        assert bombear(raiz, lambda: "Fuera de la franja horaria" in app.var_estado.get(), timeout=20)
        assert fab.cliente.messages.guion                            # no se llamó a la API: la respuesta sigue sin gastar
        app.detener()
        assert bombear(raiz, lambda: terminado(app), timeout=20)


class TestReanudarYAbrirLotes:
    def lote_del_plan(self, modelo, franja=""):
        config = cfg(modelo_plan="" if modelo == modulo.MODELO_DEL_PLAN else modelo, franja=franja)
        job = Job.crear(["Una pregunta", "Otra pregunta"], config)
        job.save()
        return job

    @pytest.mark.parametrize("modelo, esperado", [("opus", "opus"), (modulo.MODELO_DEL_PLAN, None)])
    def test_reanudar_un_lote_del_plan_hereda_su_modelo(self, raiz, claude, modelo, esperado):
        job = self.lote_del_plan(modelo)
        app = crear(raiz, config=cfg(modo="inmediato", modelo_plan="haiku"))   # la ventana está en otra cosa
        app.job = job
        assert app.reanudar()
        assert bombear(raiz, lambda: terminado(app), timeout=30)
        assert app.v["modo"].get() == "plan"
        assert all(f[2] == "Lista" for f in filas(app))
        assert claude.preguntas()[0]["valores"].get("--model") == esperado

    def test_abrir_un_lote_del_plan_antiguo_no_enseña_dolares(self, raiz):
        job = self.lote_del_plan("sonnet")
        app = crear(raiz, config=cfg(modo="inmediato"))
        assert app.abrir_lote(job.job_id)
        assert app.var_gasto.get() == "Incluido en tu plan"
        assert app.etiqueta_plan.winfo_manager() == "grid"

    def test_con_la_api_el_gasto_sigue_en_dolares(self, raiz):
        app = crear(raiz, config=cfg(modo="inmediato", tope_gasto_usd=D("5")))
        app._pintar_gasto(D("1.5"))
        assert app.var_gasto.get().startswith("Gasto $1.5") and app.etiqueta_plan.winfo_manager() == ""
