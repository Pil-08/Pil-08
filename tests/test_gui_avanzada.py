"""El modo avanzado, con la ventana de verdad.

Necesitan tkinter y una pantalla: en Linux sin escritorio, `xvfb-run -a pytest`.
Los diálogos del sistema (mensajes, confirmaciones, selectores de archivo) se sustituyen por
registros, para poder afirmar qué se le habría dicho a la persona sin que nada se abra.
"""

import json
import threading
import time
from decimal import Decimal as D

import pytest

from claude_batch_monitor.claves import ErrorDeClave
from claude_batch_monitor.cola import ETIQUETA
from claude_batch_monitor.config import ConfigManager, Configuracion
from claude_batch_monitor.core import base
from claude_batch_monitor.job import STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING, Job
from claude_batch_monitor.state import EstadoMonitor

from .gui_comun import bombear, registrar
from .test_cola import fabrica_con
from .test_engine import FakeResponse
from .test_errors import TOPE_MENSUAL, error, interno
from .test_integration import status_error

tk = pytest.importorskip("tkinter")
pytestmark = pytest.mark.gui

from claude_batch_monitor.gui import app as modulo  # noqa: E402
from claude_batch_monitor.gui.app import AppAvanzada, ErrorDeAjustes  # noqa: E402


@pytest.fixture(autouse=True)
def sin_efectos(monkeypatch):
    monkeypatch.setattr(base, "notificar", lambda *a, **k: None)


@pytest.fixture
def con_clave(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-de-prueba")


def cfg(**kw):
    base_ = dict(modelo="claude-sonnet-5-5", max_tokens=512, tope_gasto_usd=D("5"),
                 precio_input_mtok=D("1"), precio_output_mtok=D("5"))
    base_.update(kw)
    return Configuracion(**base_)


def crear(raiz, guion=None, config=None, arrancar=False, **kw):
    fab = fabrica_con(guion if guion is not None else [FakeResponse("respuesta de prueba")] * 40)
    kw.setdefault("probar", lambda k: ["claude-sonnet-5-5", "claude-haiku-4-5"])
    kw.setdefault("listar", lambda k: ["claude-opus-5-5", "claude-sonnet-5-5"])
    kw.setdefault("guardar", lambda k: "llavero")
    kw.setdefault("abrir_url", lambda u: None)
    kw.setdefault("abrir", lambda p: True)
    app = AppAvanzada(raiz, config=config or cfg(), fabrica=fab, usar_bandeja=False, arrancar=arrancar, **kw)
    app.fab = fab
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


def lanzar(raiz, app, *ordenes):
    escribir(app, *ordenes)
    assert app.iniciar()
    assert bombear(raiz, lambda: terminado(app), timeout=10)


# ----------------------------------------------------------------------


class TestAjustes:
    def test_la_configuracion_se_vuelca_en_las_casillas(self, raiz):
        c = cfg(modelo="claude-haiku-4-5", esfuerzo="high", max_tokens=1234, concurrencia=3, tope_gasto_usd=D("7.5"),
                precio_input_mtok=D("1.2"), hora_limite="23:30", telegram_chat_id="42", modo_continuo=True,
                system_prompt="Sé breve.", mensaje="uno\n---\ndos", plantilla="Traduce: {input}", retencion_dias=9)
        app = crear(raiz, config=c)
        v = app.v
        assert (v["modelo"].get(), v["esfuerzo"].get(), v["max_tokens"].get(), v["concurrencia"].get()) == (
            "claude-haiku-4-5", "high", "1234", "3")
        assert (v["tope_gasto_usd"].get(), v["precio_input_mtok"].get(), v["precio_output_mtok"].get()) == (
            "7.5", "1.2", "5")
        assert v["hora_limite"].get() == "23:30" and v["telegram_chat_id"].get() == "42" and v["modo_continuo"].get()
        assert app.txt_sistema.get("1.0", "end-1c") == "Sé breve."
        assert app.v["plantilla"].get() == "Traduce: {input}" and v["retencion_dias"].get() == "9"
        assert app.var_cuenta.get() == "2 órdenes detectadas"

    def test_leer_devuelve_lo_que_dicen_las_casillas(self, raiz):
        app = crear(raiz)
        app.v["tope_gasto_usd"].set(" 12,50 ")
        app.v["esfuerzo"].set("medium")
        app.v["precio_input_mtok"].set("")
        app.v["concurrencia"].set("4")
        app.txt_sistema.insert("1.0", "  Eres un traductor.  ")
        c = app.leer_ajustes()
        assert c.tope_gasto_usd == D("12.50") and c.esfuerzo == "medium" and c.concurrencia == 4
        assert c.precio_input_mtok is None            # vacío = el del catálogo
        assert c.system_prompt == "Eres un traductor."

    def test_el_tema_se_elige_con_nombres_legibles_y_se_guarda(self, raiz):
        from claude_batch_monitor.gui.estilo import PALETA_OSCURA

        app = crear(raiz, config=cfg(tema="oscuro"))
        assert app.p is PALETA_OSCURA and app.v["tema"].get() == "Oscuro"
        assert list(app.combo_tema.cget("values")) == ["Automático", "Claro", "Oscuro"]
        app.v["tema"].set("Claro")
        assert app.leer_ajustes().tema == "claro"
        app.v["tema"].set("Automático")
        assert app.leer_ajustes().tema == "auto"
        app.v["tema"].set("algo raro")                      # un valor que no existe no cambia la elección anterior
        assert app.leer_ajustes().tema == "oscuro"

    def test_sin_esfuerzo_no_se_envia(self, raiz):
        app = crear(raiz, config=cfg(esfuerzo="high"))
        app.v["esfuerzo"].set(modulo.SIN_ESFUERZO)
        assert app.leer_ajustes().esfuerzo == ""

    @pytest.mark.parametrize("campo, valor, texto", [
        ("tope_gasto_usd", "mucho", "Tope de gasto"),
        ("tope_gasto_usd", "", "Tope de gasto"),
        ("tope_gasto_usd", "-3", "igual o mayor que 0"),
        ("precio_output_mtok", "caro", "Precio de salida"),
        ("max_tokens", "mil", "Máximo de tokens"),
        ("concurrencia", "99", "entre 1 y 32"),
        ("timeout_api_sec", "3", "entre 15 y 900"),
        ("hora_limite", "25:99", "HH:MM"),
        ("hora_limite", "mañana", "HH:MM"),
    ])
    def test_un_valor_invalido_se_explica(self, raiz, campo, valor, texto):
        app = crear(raiz)
        app.v[campo].set(valor)
        with pytest.raises(ErrorDeAjustes, match=texto):
            app.leer_ajustes()

    def test_un_valor_invalido_no_se_guarda_y_se_avisa(self, raiz):
        app = crear(raiz)
        ConfigManager.guardar(app.config)
        app.v["tope_gasto_usd"].set("abc")
        assert app.guardar_ajustes() is None
        assert "Tope de gasto" in app.var_aviso_ajustes.get()
        assert ConfigManager.cargar().tope_gasto_usd == D("5")     # lo bueno anterior sigue ahí
        app.v["tope_gasto_usd"].set("8")
        assert app.guardar_ajustes() is not None and app.var_aviso_ajustes.get() == ""
        assert ConfigManager.cargar().tope_gasto_usd == D("8")

    def test_los_cambios_se_guardan_solos_al_dejar_de_teclear(self, raiz):
        app = crear(raiz)
        app.v["tope_gasto_usd"].set("9")
        assert ConfigManager.cargar().tope_gasto_usd != D("9")     # todavía no: se espera a que pare
        assert bombear(raiz, lambda: ConfigManager.cargar().tope_gasto_usd == D("9"), timeout=3)

    def test_muchos_cambios_seguidos_escriben_una_sola_vez(self, raiz, monkeypatch):
        app = crear(raiz)
        escrituras = []
        monkeypatch.setattr(modulo.ConfigManager, "guardar", staticmethod(lambda c, path=None: escrituras.append(c)))
        for i in range(10):
            app.v["max_tokens"].set(str(100 + i))
        bombear(raiz, timeout=1.2)
        assert len(escrituras) == 1 and escrituras[0].max_tokens == 109

    def test_el_borrador_de_las_ordenes_se_guarda_y_se_recupera(self, raiz):
        app = crear(raiz)
        escribir(app, "primera", "segunda")
        app.guardar_ajustes()
        assert "segunda" in ConfigManager.cargar().mensaje

    def test_un_texto_enorme_no_se_copia_a_config_json(self, raiz, monkeypatch):
        monkeypatch.setattr(modulo, "MAX_BORRADOR", 50)
        app = crear(raiz)
        escribir(app, "x" * 200)
        app.guardar_ajustes()
        assert ConfigManager.cargar().mensaje == ""

    def test_el_modo_lote_apaga_lo_que_no_aplica(self, raiz):
        app = crear(raiz)
        assert "disabled" not in app.spin_concurrencia.state() and "disabled" not in app.check_bucle.state()
        app.v["modo"].set("lote")
        assert "disabled" in app.spin_concurrencia.state() and "disabled" in app.check_bucle.state()
        app.v["modo"].set("inmediato")
        assert "disabled" not in app.spin_concurrencia.state()

    def test_la_pista_de_precios_sigue_al_modelo(self, raiz):
        app = crear(raiz)
        assert "$2 entrada / $10 salida" in app.var_modelo_info.get()
        app.v["modelo"].set("claude-haiku-4-5")
        assert "$1 entrada / $5 salida" in app.var_modelo_info.get()
        assert "catálogo" in app.var_precio_info.get() and "$1 entrada / $5 salida" in app.var_precio_info.get()
        app.v["modelo"].set("modelo-nuevo-desconocido")
        assert "no está en el catálogo" in app.var_precio_info.get()

    def test_los_ajustes_viejos_de_la_v11_se_leen(self, raiz):
        ConfigManager.guardar(Configuracion.desde_dict({"modelo": "claude-sonnet-4-5", "tope_gasto_usd": 3.5,
                                                        "precio_input_mtok": 3.0, "mensaje": "hola"}))
        app = crear(raiz, config=ConfigManager.cargar())
        assert app.v["tope_gasto_usd"].get() == "3.5" and app.v["precio_input_mtok"].get() == "3"
        assert app.txt_ordenes.get("1.0", "end-1c") == "hola"


class TestClaveYModelos:
    def test_sin_clave_se_avisa_en_el_panel(self, raiz):
        app = crear(raiz)
        assert app.aviso_clave.winfo_manager() == "grid" and "Todavía no hay clave" in app.var_auth.get()

    def test_con_clave_no_hay_aviso(self, raiz, con_clave):
        app = crear(raiz)
        assert app.aviso_clave.winfo_manager() == ""
        assert "variable de entorno" in app.var_auth.get()
        assert "disabled" in app.entrada_clave.state() and "disabled" in app.boton_clave.state()

    def test_probar_y_guardar_una_clave_valida(self, raiz):
        guardadas = []
        app = crear(raiz, guardar=lambda k: (guardadas.append(k), "llavero")[1],
                    probar=lambda k: ["claude-haiku-4-5", "claude-opus-5-5"])
        app.var_clave.set("  sk-ant-buena ")
        app.probar_y_guardar_clave()
        assert bombear(raiz, lambda: guardadas)
        assert guardadas == ["sk-ant-buena"] and app.var_clave.get() == ""
        assert bombear(raiz, lambda: "Clave válida" in app.var_auth.get())
        assert "claude-haiku-4-5" in app.combo_modelo.cget("values")

    def test_una_clave_sin_llavero_avisa_de_que_hay_que_pegarla_cada_vez(self, raiz):
        app = crear(raiz, guardar=lambda k: "memoria")
        app.var_clave.set("sk-ant-x")
        app.probar_y_guardar_clave()
        assert bombear(raiz, lambda: "cada vez" in app.var_auth.get())

    @pytest.mark.parametrize("tipo, texto", [("invalida", "no reconoce"), ("sin_conexion", "conexión")])
    def test_una_clave_mala_se_explica_y_se_puede_reintentar(self, raiz, tipo, texto):
        def malo(k):
            raise ErrorDeClave(tipo)
        app = crear(raiz, probar=malo)
        app.var_clave.set("sk-ant-mala")
        app.probar_y_guardar_clave()
        assert bombear(raiz, lambda: texto in app.var_auth.get())
        assert "disabled" not in app.boton_clave.state() and app.var_clave.get() == "sk-ant-mala"

    def test_clave_vacia(self, raiz):
        app = crear(raiz)
        app.probar_y_guardar_clave()
        assert "Pega primero" in app.var_auth.get()

    def test_cargar_modelos_rellena_la_lista(self, raiz, con_clave):
        app = crear(raiz)
        app.cargar_modelos()
        assert bombear(raiz, lambda: dialogos(app, "info"))
        assert dialogos(app, "info")[0][2] == "Se cargaron 2 modelos."
        assert set(app.combo_modelo.cget("values")) == {"claude-opus-5-5", "claude-sonnet-5-5"}

    def test_si_el_modelo_elegido_ya_no_existe_se_cambia_por_uno_valido(self, raiz, con_clave):
        app = crear(raiz, config=cfg(modelo="modelo-retirado"), listar=lambda k: ["claude-haiku-4-5", "claude-sonnet-5-5"])
        app.cargar_modelos()
        assert bombear(raiz, lambda: dialogos(app, "info"))
        assert app.v["modelo"].get() == "claude-sonnet-5-5"

    def test_cargar_modelos_con_error(self, raiz, con_clave):
        def malo(k):
            raise ErrorDeClave("sin_conexion")
        app = crear(raiz, listar=malo)
        app.cargar_modelos()
        assert bombear(raiz, lambda: dialogos(app, "error"))
        assert "conexión" in dialogos(app, "error")[0][2] and "disabled" not in app.boton_modelos.state()

    def test_cargar_modelos_sin_clave_avisa(self, raiz):
        app = crear(raiz)
        app.cargar_modelos()
        assert dialogos(app, "aviso") and "clave" in dialogos(app, "aviso")[0][2]


class TestTelegram:
    def test_sin_datos_avisa(self, raiz):
        app = crear(raiz)
        app.probar_telegram()
        assert dialogos(app, "aviso")

    def test_la_prueba_correcta_guarda_el_token_y_no_lo_deja_en_el_campo(self, raiz, monkeypatch):
        enviados = []
        monkeypatch.setattr(modulo, "enviar_prueba_telegram", lambda token, chat: enviados.append((token, chat)))
        app = crear(raiz)
        app.var_token_tg.set("123:ABC")
        app.v["telegram_chat_id"].set("42")
        app.probar_telegram()
        assert bombear(raiz, lambda: dialogos(app, "info"))
        assert enviados == [("123:ABC", "42")] and app.var_token_tg.get() == ""
        import os
        assert os.environ.get("TELEGRAM_BOT_TOKEN") == "123:ABC"      # sin llavero, vale para esta sesión
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")

    def test_un_fallo_se_explica_y_no_guarda_nada(self, raiz, monkeypatch):
        from claude_batch_monitor.notify import ErrorDeTelegram

        def falla(token, chat):
            raise ErrorDeTelegram("Unauthorized")

        monkeypatch.setattr(modulo, "enviar_prueba_telegram", falla)
        app = crear(raiz)
        app.var_token_tg.set("123:ABC")
        app.v["telegram_chat_id"].set("42")
        app.probar_telegram()
        assert bombear(raiz, lambda: dialogos(app, "error"))
        assert dialogos(app, "error")[0][2] == "Unauthorized" and app.var_token_tg.get() == "123:ABC"
        assert "disabled" not in app.boton_tg.state()


class TestOrdenes:
    def test_el_contador(self, raiz):
        app = crear(raiz)
        assert app.var_cuenta.get() == "0 órdenes detectadas"
        escribir(app, "una")
        assert app.var_cuenta.get() == "1 orden detectada"
        escribir(app, "a", "b", "c")
        assert app.var_cuenta.get() == "3 órdenes detectadas"

    def test_el_contador_respeta_el_separador_elegido(self, raiz):
        app = crear(raiz)
        app.txt_ordenes.insert("1.0", "uno\n###\ndos\n---\ntres")
        app.v["separador"].set("###")
        assert app.var_cuenta.get() == "2 órdenes detectadas"

    def test_un_txt_se_carga_tal_cual(self, raiz, tmp_path):
        f = tmp_path / "preguntas.txt"
        f.write_text("primera\n\n---\n\nsegunda  con   espacios\n", encoding="utf-8")
        app = crear(raiz)
        assert app.cargar_archivo(str(f))
        assert app.txt_ordenes.get("1.0", "end-1c") == "primera\n\n---\n\nsegunda  con   espacios\n"
        assert app.var_cuenta.get() == "2 órdenes detectadas" and app.var_archivo.get() == "preguntas.txt"

    def test_un_csv_pregunta_la_columna_y_une_las_filas(self, raiz, tmp_path):
        f = tmp_path / "datos.csv"
        f.write_text("id;texto;otro\n1;Hola;x\n2;Adiós;y\n", encoding="utf-8")
        app = crear(raiz)
        vistas = []
        app._elegir_columna = lambda archivo, cols: (vistas.append((archivo, cols)), "texto")[1]
        assert app.cargar_archivo(str(f))
        assert vistas == [("datos.csv", ["id", "texto", "otro"])]
        assert app.txt_ordenes.get("1.0", "end-1c") == "Hola\n\n---\n\nAdiós"
        assert app.var_archivo.get() == "datos.csv · columna «texto»"

    def test_un_csv_con_una_sola_columna_no_pregunta(self, raiz, tmp_path):
        f = tmp_path / "una.csv"
        f.write_text("pregunta\nA\nB\n", encoding="utf-8")
        app = crear(raiz)
        assert app.cargar_archivo(str(f))
        assert app.var_cuenta.get() == "2 órdenes detectadas"

    def test_cancelar_la_eleccion_de_columna_no_cambia_nada(self, raiz, tmp_path):
        f = tmp_path / "d.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        app = crear(raiz)
        escribir(app, "lo que había")
        app._elegir_columna = lambda archivo, cols: None
        assert app.cargar_archivo(str(f)) is False
        assert "lo que había" in app.txt_ordenes.get("1.0", "end-1c")

    def test_un_jsonl_usa_el_campo_elegido(self, raiz, tmp_path):
        f = tmp_path / "d.jsonl"
        f.write_text('{"id": 1, "prompt": "uno"}\n{"id": 2, "prompt": "dos"}\n', encoding="utf-8")
        app = crear(raiz)
        assert app.cargar_archivo(str(f), campo="prompt")
        assert app.txt_ordenes.get("1.0", "end-1c") == "uno\n\n---\n\ndos"

    def test_una_celda_que_contiene_el_separador_se_avisa_antes_de_partirla(self, raiz, tmp_path):
        f = tmp_path / "d.csv"
        f.write_text('t\n"hola\n---\nadiós"\n', encoding="utf-8")
        app = crear(raiz)
        escribir(app, "intacto")
        assert app.cargar_archivo(str(f)) is False
        assert "separador" in dialogos(app, "error")[0][2] and "intacto" in app.txt_ordenes.get("1.0", "end-1c")

    def test_un_archivo_que_no_es_utf8(self, raiz, tmp_path):
        f = tmp_path / "raro.txt"
        f.write_bytes("canción".encode("latin-1"))
        app = crear(raiz)
        assert app.cargar_archivo(str(f)) is False
        assert "UTF-8" in dialogos(app, "error")[0][2]

    def test_un_archivo_que_no_existe(self, raiz, tmp_path):
        app = crear(raiz)
        assert app.cargar_archivo(str(tmp_path / "no-esta.csv")) is False
        assert dialogos(app, "error")

    def test_sin_elegir_archivo_no_pasa_nada(self, raiz):
        app = crear(raiz)
        app._pedir_archivo = lambda: ""
        assert app.cargar_archivo() is False and not app.dialogos

    def test_la_plantilla_y_el_limite_se_aplican_al_lanzar(self, raiz, con_clave):
        app = crear(raiz)
        app.v["plantilla"].set("Traduce: {input}")
        app.var_limite.set("2")
        lanzar(raiz, app, "uno", "dos", "tres")
        assert [f[1] for f in filas(app)] == ["Traduce: uno", "Traduce: dos"]

    def test_una_plantilla_sin_marcador_se_añade_al_final(self, raiz):
        app = crear(raiz)
        app.v["plantilla"].set("Responde en francés.")
        escribir(app, "hola")
        assert app._ordenes(app.leer_ajustes()) == ["Responde en francés.\n\nhola"]


class TestEjecucion:
    def test_un_lote_se_ejecuta_de_principio_a_fin(self, raiz, con_clave):
        app = crear(raiz)
        lanzar(raiz, app, "uno", "dos", "tres")
        assert [f[2] for f in filas(app)] == ["Lista"] * 3
        assert app.var_progreso.get() == "Progreso 3/3" and float(app.progreso.cget("value")) == 3
        assert app.var_estado.get() == "Estado: Completado." and "$0.0" in app.var_gasto.get()
        assert "de $5.00" in app.var_gasto.get() or "/ $5.00" in app.var_gasto.get()
        info = dialogos(app, "info")
        assert info and "Completadas 3/3" in info[0][2]
        assert app.job.completed_count() == 3 and EstadoMonitor.cargar().exitos == 3
        assert app.var_exitos.get() == "Éxitos totales 3"

    def test_los_botones_cambian_mientras_trabaja(self, raiz, con_clave):
        suelta = threading.Event()
        app = crear(raiz)
        original = app.fab.cliente.messages._siguiente

        def lenta():
            suelta.wait(5)
            return original()

        app.fab.cliente.messages._siguiente = lenta
        escribir(app, "a", "b")
        app.iniciar()
        bombear(raiz, timeout=0.3)
        assert "disabled" in app.boton_iniciar.state() and "disabled" not in app.boton_detener.state()
        assert "disabled" in app.txt_ordenes.cget("state") and "disabled" in app.combo_modelo.state()
        assert "disabled" in app.boton_lotes.state() and "disabled" in app.boton_reanudar.state()
        assert app.iniciar() is False               # no se pueden lanzar dos a la vez
        suelta.set()
        assert bombear(raiz, lambda: terminado(app), timeout=10)
        assert "disabled" not in app.boton_iniciar.state() and "disabled" in app.boton_detener.state()
        assert app.txt_ordenes.cget("state") == "normal"

    def test_antes_de_gastar_pide_confirmacion_con_el_peor_caso(self, raiz, con_clave):
        app = crear(raiz, config=cfg(tope_gasto_usd=D("3")))
        escribir(app, "a", "b")
        app.responde_si = False
        assert app.iniciar() is False
        _, _, texto = dialogos(app, "confirmar")[0]
        assert "Órdenes: 2" in texto and "Modelo: claude-sonnet-5-5" in texto
        assert "Coste máximo estimado: $" in texto and "Tope de gasto: $3.00" in texto
        assert app.job is None and app.fab.cliente.messages.llamadas == 0     # no se envió nada

    def test_sin_tope_la_confirmacion_lo_grita(self, raiz, con_clave):
        app = crear(raiz, config=cfg(tope_gasto_usd=D(0)))
        escribir(app, "a")
        app.responde_si = False
        app.iniciar()
        texto = dialogos(app, "confirmar")[0][2]
        assert "SIN TOPE" in texto and "Sin tope de gasto" in texto

    def test_sin_clave_no_arranca_y_lleva_a_ajustes(self, raiz):
        app = crear(raiz)
        escribir(app, "a")
        assert app.iniciar() is False
        assert "clave" in dialogos(app, "error")[0][2]
        assert app.cuaderno.index(app.cuaderno.select()) == 1
        assert app.fab.cliente.messages.llamadas == 0

    def test_una_clave_escrita_se_guarda_al_arrancar(self, raiz, monkeypatch):
        guardadas = []
        app = crear(raiz, guardar=lambda k: (guardadas.append(k), monkeypatch.setenv("ANTHROPIC_API_KEY", k), "memoria")[2])
        app.var_clave.set("sk-ant-escrita")
        lanzar(raiz, app, "a")
        assert guardadas == ["sk-ant-escrita"] and app.job.completed_count() == 1

    def test_sin_ordenes_avisa(self, raiz, con_clave):
        app = crear(raiz)
        assert app.iniciar() is False
        assert dialogos(app, "aviso")[0][1] == "Sin órdenes"

    def test_un_ajuste_invalido_impide_arrancar(self, raiz, con_clave):
        app = crear(raiz)
        escribir(app, "a")
        app.v["tope_gasto_usd"].set("mucho")
        assert app.iniciar() is False
        assert "Tope de gasto" in dialogos(app, "error")[0][2] and app.cuaderno.index(app.cuaderno.select()) == 1

    def test_un_modelo_que_no_admite_ese_maximo_se_rechaza(self, raiz, con_clave):
        app = crear(raiz)
        escribir(app, "a")
        app.v["modelo"].set("claude-haiku-4-5")
        app.v["max_tokens"].set("100000")
        assert app.iniciar() is False
        assert "64,000" in dialogos(app, "error")[0][2] or "64.000" in dialogos(app, "error")[0][2]

    def test_el_bucle_no_existe_en_modo_lote(self, raiz, con_clave):
        app = crear(raiz)
        escribir(app, "a")
        app.v["modo"].set("lote")
        app.v["modo_continuo"].set(True)
        assert app.iniciar() is False
        assert "bucle" in dialogos(app, "error")[0][2].lower()

    def test_el_modo_de_la_pantalla_decide_el_motor(self, raiz, con_clave):
        modos = []

        def fabrica(config, job):
            modos.append(config.modo)
            raise RuntimeError("no hace falta ejecutar")

        app = crear(raiz)
        app.ejecutor._fabrica = fabrica
        escribir(app, "a")
        app.v["modo"].set("lote")
        app.iniciar()
        bombear(raiz, lambda: terminado(app))
        assert modos == ["lote"]

    def test_detener_conserva_lo_hecho_y_permite_reanudar(self, raiz, con_clave):
        entrada, suelta = threading.Event(), threading.Event()
        app = crear(raiz)
        original = app.fab.cliente.messages._siguiente

        def lenta():
            entrada.set()
            suelta.wait(5)
            return original()

        app.fab.cliente.messages._siguiente = lenta
        escribir(app, "a", "b", "c")
        app.iniciar()
        assert bombear(raiz, entrada.is_set)
        app.detener()
        assert "cancelando" in app.var_estado.get()
        suelta.set()
        assert bombear(raiz, lambda: terminado(app), timeout=10)
        assert app.var_estado.get().startswith("Estado: Detenido")
        assert app.job.completed_count() == 1 and len(app.job.pendientes()) == 2
        assert "disabled" not in app.boton_reanudar.state() and app.boton_reanudar.cget("text") == "Reanudar"
        app.fab.cliente.messages._siguiente = original
        assert app.reanudar()
        assert bombear(raiz, lambda: terminado(app), timeout=10)
        assert app.job.completed_count() == 3 and "disabled" in app.boton_reanudar.state()

    def test_al_reanudar_manda_el_lote_y_no_la_pantalla(self, raiz, con_clave):
        app = crear(raiz)
        job = Job.crear(["a", "b"], cfg(modelo="claude-haiku-4-5", modo="inmediato"))
        job.save()
        app.job = job
        app.v["modelo"].set("claude-opus-5-5")        # la pantalla dice otra cosa
        assert app.reanudar()
        assert bombear(raiz, lambda: terminado(app), timeout=10)
        assert app.fab.motores[0].config.modelo == "claude-haiku-4-5"
        assert app.v["modelo"].get() == "claude-haiku-4-5"       # y la pantalla lo refleja

    def test_un_error_fatal_dice_que_hacer_despues(self, raiz, con_clave):
        app = crear(raiz, guion=[status_error(400)] * 3)
        lanzar(raiz, app, "una")
        aviso = dialogos(app, "aviso")[0]
        assert aviso[1] == "Lote detenido" and "Detalle: La API rechazó la petición" in aviso[2]
        assert "pulsa «Reanudar»" in aviso[2]
        assert app.boton_reanudar.cget("text") == "Reintentar fallidas"

    def test_las_fallidas_se_reintentan_con_un_boton_claro(self, raiz, con_clave):
        # 413: la orden es demasiado grande; falla ella sola y el lote termina (a diferencia de un 400).
        app = crear(raiz, guion=[status_error(413), FakeResponse("ya sí")] * 3)
        lanzar(raiz, app, "una")
        assert filas(app)[0][2] == "Error"
        assert dialogos(app, "aviso") and "Reintentar fallidas" in dialogos(app, "aviso")[0][2]
        assert app.boton_reanudar.cget("text") == "Reintentar fallidas" and "disabled" not in app.boton_reanudar.state()
        assert app.reanudar()
        assert bombear(raiz, lambda: terminado(app), timeout=10)
        assert filas(app)[0][2] == "Lista" and app.boton_reanudar.cget("text") == "Reanudar"

    def test_reanudar_sin_nada_pendiente_no_hace_nada(self, raiz, con_clave):
        app = crear(raiz)
        lanzar(raiz, app, "a")
        assert app.reanudar() is False

    def test_el_tope_de_gasto_se_explica(self, raiz, con_clave):
        app = crear(raiz, config=cfg(tope_gasto_usd=D("0.004")), guion=[FakeResponse()] * 10)
        escribir(app, *"abcd")
        app.iniciar()
        assert bombear(raiz, lambda: terminado(app), timeout=10)
        aviso = dialogos(app, "aviso")[0]
        assert aviso[1] == "Lote detenido" and "tope de gasto" in aviso[2] and "Reanudar" in aviso[2]

    def test_sin_saldo_da_el_enlace_para_recargar(self, raiz, con_clave):
        app = crear(raiz, guion=[status_error(402)])
        lanzar(raiz, app, "a")
        texto = dialogos(app, "aviso")[0][2]
        assert "sin saldo" in texto and "platform.claude.com/settings/billing" in texto
        assert "Último error:" in app.var_error.get() and app.etiqueta_error.winfo_manager() == "grid"

    def test_limite_de_la_cuenta_se_distingue_del_de_este_programa(self, raiz, con_clave):
        app = crear(raiz, guion=[error(429, interno(TOPE_MENSUAL))])
        lanzar(raiz, app, "a")
        texto = dialogos(app, "aviso")[0][2]
        assert "límite de tu cuenta" in texto and "2026-09-01" in texto

    def test_si_la_ventana_esta_oculta_no_salen_mensajes(self, raiz, con_clave):
        app = crear(raiz)
        app._visible = lambda: False
        lanzar(raiz, app, "a")
        assert not dialogos(app, "info") and not dialogos(app, "aviso")

    def test_un_lote_nuevo_limpia_el_error_anterior(self, raiz, con_clave):
        app = crear(raiz, guion=[status_error(402), FakeResponse("bien")] * 2)
        lanzar(raiz, app, "a")
        assert app.etiqueta_error.winfo_manager() == "grid"
        lanzar(raiz, app, "b")
        assert app.etiqueta_error.winfo_manager() == ""

    def test_la_tabla_muestra_los_tokens_y_el_coste(self, raiz, con_clave):
        app = crear(raiz)
        lanzar(raiz, app, "a")
        id_, orden, estado, tokens, coste = filas(app)[0]
        assert (id_, orden, estado) == ("1", "a", "Lista") and tokens and coste.startswith("$0.")


class TestResultados:
    def test_ver_la_respuesta_completa(self, raiz, con_clave, monkeypatch):
        vistos = []
        monkeypatch.setattr(modulo, "VisorRespuesta", lambda *a: vistos.append(a))
        app = crear(raiz, guion=[FakeResponse("la respuesta larga")] * 3)
        lanzar(raiz, app, "una pregunta")
        app.tabla.selection_set(app.tabla.get_children()[0])
        app.ver_respuesta()
        _, titulo, estado, solicitud, respuesta, _copiar = vistos[0]
        assert titulo == "Orden 1" and solicitud == "una pregunta" and respuesta == "la respuesta larga"
        assert "Lista" in estado and "tokens de entrada" in estado

    def test_el_visor_real_muestra_y_copia(self, raiz, con_clave):
        from claude_batch_monitor.gui.dialogos import VisorRespuesta
        app = crear(raiz)
        copiado = []
        v = VisorRespuesta(app, "Orden 1", "Lista", "pregunta", "respuesta", copiado.append)
        assert v.texto_orden.get("1.0", "end-1c") == "pregunta" and v.texto_respuesta.get("1.0", "end-1c") == "respuesta"
        v.boton_copiar.invoke()
        assert copiado == ["respuesta"]
        v.destroy()

    def test_el_visor_muestra_la_respuesta_con_formato_y_se_puede_ver_sin_el(self, raiz, con_clave):
        from claude_batch_monitor.gui.dialogos import VisorRespuesta
        app = crear(raiz)
        copiado = []
        v = VisorRespuesta(app, "Orden 1", "Lista", "pregunta", "# Título\n\nCon **fuerza**.", copiado.append)
        assert "**" not in v.texto_respuesta.get("1.0", "end") and v.texto_respuesta.tag_ranges("negrita")
        v.var_formato.set(False)
        v._pintar_respuesta()
        assert "**fuerza**" in v.texto_respuesta.get("1.0", "end") and not v.texto_respuesta.tag_ranges("negrita")
        v.boton_copiar.invoke()
        assert copiado == ["# Título\n\nCon **fuerza**."]               # copiar siempre da el original
        v.destroy()

    def test_el_visor_sin_respuesta_no_deja_copiar(self, raiz, con_clave):
        from claude_batch_monitor.gui.dialogos import VisorRespuesta
        v = VisorRespuesta(crear(raiz), "Orden 2", "Error", "p", "", lambda t: None)
        assert "disabled" in v.boton_copiar.state()
        v.destroy()

    def test_copiar_deja_el_texto_en_el_portapapeles(self, raiz, con_clave):
        app = crear(raiz)
        app._copiar("hola portapapeles")
        assert raiz.clipboard_get() == "hola portapapeles"

    def test_ver_sin_seleccion_no_hace_nada(self, raiz, con_clave):
        app = crear(raiz)
        app.ver_respuesta()

    def test_la_respuesta_de_una_orden_fallida_es_su_motivo(self, raiz, con_clave):
        app = crear(raiz, guion=[status_error(400)] * 3)
        lanzar(raiz, app, "a")
        assert app.job.mensaje(1).status == STATUS_FAILED
        assert app.texto_de_respuesta(1) == app.job.mensaje(1).detalle != ""

    def test_exportar_a_csv(self, raiz, con_clave, tmp_path):
        app = crear(raiz, guion=[FakeResponse("la primera"), FakeResponse("la segunda")])
        lanzar(raiz, app, "uno", "dos")
        destino = tmp_path / "salida.csv"
        assert app.exportar_a(str(destino))
        texto = destino.read_text(encoding="utf-8-sig")
        assert "la primera" in texto and "la segunda" in texto

    def test_exportar_en_un_formato_raro_avisa(self, raiz, con_clave, tmp_path):
        app = crear(raiz)
        lanzar(raiz, app, "a")
        assert app.exportar_a(str(tmp_path / "salida.xlsx")) is False
        assert "xlsx" in dialogos(app, "error")[0][2]

    def test_exportar_sin_nada(self, raiz):
        app = crear(raiz)
        app.exportar_resultados()
        assert dialogos(app, "info")[0][2] == "Todavía no hay nada que exportar."

    def test_exportar_cancelando_el_selector(self, raiz, con_clave):
        app = crear(raiz)
        lanzar(raiz, app, "a")
        app._pedir_destino = lambda: ""
        app.exportar_resultados()
        assert not dialogos(app, "error")

    def test_abrir_carpetas(self, raiz):
        abiertas = []
        app = crear(raiz, abrir=lambda p: (abiertas.append(p.name), True)[1])
        app.abrir_respuestas()
        app.abrir_logs()
        app.abrir_datos()
        assert abiertas[0] == "responses" and abiertas[1] == "claude_monitor.log" and len(abiertas) == 3

    def test_si_no_se_puede_abrir_se_dice(self, raiz):
        app = crear(raiz, abrir=lambda p: False)
        app.abrir_respuestas()
        assert dialogos(app, "error")

    def test_estimar_el_coste_muestra_el_peor_caso(self, raiz, con_clave, monkeypatch):
        vistas = []
        monkeypatch.setattr(modulo, "DialogoEstimacion", lambda app, est, tope: vistas.append((est, tope)))
        app = crear(raiz, config=cfg(max_tokens=1000, tope_gasto_usd=D("3")))
        escribir(app, "una", "otra")
        app.estimar_coste()
        assert bombear(raiz, lambda: vistas)
        est, tope = vistas[0]
        assert est.ordenes == 2 and est.max_tokens == 1000 and tope == D("3")
        assert est.coste_maximo > 0 and "disabled" not in app.boton_estimar.state()

    def test_estimar_sin_ordenes_avisa(self, raiz):
        app = crear(raiz)
        app.estimar_coste()
        assert dialogos(app, "aviso")[0][1] == "Sin órdenes"

    def test_el_dialogo_de_estimacion_real_se_dibuja(self, raiz, con_clave):
        from claude_batch_monitor.estimator import estimar
        from claude_batch_monitor.gui.dialogos import DialogoEstimacion
        app = crear(raiz)
        c = cfg(tope_gasto_usd=D("0.000001"))     # el peor caso supera el tope: sale un aviso
        d = DialogoEstimacion(app, estimar(c, ["una orden"]), c.tope_gasto_usd)
        assert any("Coste MÁXIMO" in linea for linea in d.lineas)
        assert any("Tope configurado" in linea for linea in d.lineas)
        d.destroy()


class TestLotesAnteriores:
    def _guardar(self, n_ok, n_pend, **kw):
        job = Job.crear([f"o{i}" for i in range(n_ok + n_pend)], cfg(**kw))
        for m in job.messages[:n_ok]:
            m.status = STATUS_COMPLETED
        job.save()
        return job

    def test_el_dialogo_lista_los_lotes_y_no_la_cola_del_modo_simple(self, raiz, monkeypatch):
        a = self._guardar(2, 0)
        b = self._guardar(1, 2)
        cola = Job.crear(["pregunta suelta"], cfg(), etiqueta=ETIQUETA)
        cola.save()
        vistos = []
        monkeypatch.setattr(modulo, "DialogoLotes", lambda app, lotes, al_abrir: vistos.append(lotes))
        crear(raiz).lotes_anteriores()
        ids = {d["job_id"] for d in vistos[0]}
        assert ids == {a.job_id, b.job_id}

    def test_el_dialogo_real_se_dibuja_y_abre_el_elegido(self, raiz):
        from claude_batch_monitor.gui.dialogos import DialogoLotes
        from claude_batch_monitor.job import listar_jobs
        a = self._guardar(1, 1)
        app = crear(raiz)
        abiertos = []
        d = DialogoLotes(app, listar_jobs(), abiertos.append)
        assert d.seleccionado() == a.job_id
        assert "sin terminar" in d.tabla.item(a.job_id, "values")[4]
        d.abrir()
        assert abiertos == [a.job_id]

    def test_el_dialogo_vacio_no_se_puede_abrir(self, raiz):
        from claude_batch_monitor.gui.dialogos import DialogoLotes
        d = DialogoLotes(crear(raiz), [], lambda j: None)
        assert d.seleccionado() is None and "disabled" in d.boton_abrir.state()
        d.destroy()

    def test_abrir_un_lote_lo_muestra_y_deja_exportar_y_reanudar(self, raiz, con_clave):
        job = self._guardar(1, 2)
        app = crear(raiz)
        assert app.abrir_lote(job.job_id)
        assert [f[2] for f in filas(app)] == ["Lista", "En cola", "En cola"]
        assert app.var_progreso.get() == "Progreso 1/3"
        assert "disabled" not in app.boton_exportar.state() and "disabled" not in app.boton_reanudar.state()
        assert app.reanudar()
        assert bombear(raiz, lambda: terminado(app), timeout=10)
        assert [f[2] for f in filas(app)] == ["Lista"] * 3

    def test_abrir_un_lote_inexistente(self, raiz):
        app = crear(raiz)
        assert app.abrir_lote("zzzzzzzz") is False and dialogos(app, "error")

    def test_no_se_abre_otro_lote_mientras_se_trabaja(self, raiz, con_clave):
        job = self._guardar(1, 0)
        app = crear(raiz)
        app._ocupado = True
        assert app.abrir_lote(job.job_id) is False
        app._ocupado = False

    def test_el_dialogo_de_columnas_devuelve_la_elegida(self, raiz):
        from claude_batch_monitor.gui.dialogos import DialogoColumna
        d = DialogoColumna(crear(raiz), "datos.csv", ["id", "input", "otro"], "input")
        assert d.var.get() == "input"
        d.var.set("otro")
        d.aceptar()
        assert d.eleccion == "otro"

    def test_el_dialogo_de_columnas_sin_sugerida_usa_la_primera(self, raiz):
        from claude_batch_monitor.gui.dialogos import DialogoColumna
        d = DialogoColumna(crear(raiz), "datos.csv", ["a", "b"], "input")
        assert d.var.get() == "a"
        d.destroy()
        assert d.eleccion is None


class TestArranque:
    def _lote_a_medias(self, **kw):
        job = Job.crear(["a", "b", "c"], cfg(**kw))
        job.messages[0].status = STATUS_COMPLETED
        job.save()
        EstadoMonitor(current_job_id=job.job_id).guardar()
        return job

    def test_ofrece_reanudar_un_lote_sin_terminar(self, raiz, con_clave):
        job = self._lote_a_medias()
        app = crear(raiz, arrancar=True)
        assert bombear(raiz, lambda: dialogos(app, "confirmar"))
        titulo, texto = dialogos(app, "confirmar")[0][1:]
        assert titulo == "Lote sin terminar" and job.job_id in texto and "1/3" in texto
        assert bombear(raiz, lambda: terminado(app) and app.job and app.job.completed_count() == 3, timeout=10)

    def test_rechazar_olvida_el_lote_pero_se_puede_seguir_viendo(self, raiz, con_clave):
        job = self._lote_a_medias()
        app = crear(raiz, arrancar=False)
        app.responde_si = False
        app._arrancar()
        assert EstadoMonitor.cargar().current_job_id is None
        assert app.job.job_id == job.job_id and len(filas(app)) == 3     # sigue visible, por si quieres exportarlo
        assert not app.ejecutor.activo

    def test_con_autoarranque_reanuda_sin_preguntar(self, raiz, con_clave):
        self._lote_a_medias()
        app = crear(raiz, config=cfg(auto_arranque=True))
        app._arrancar()
        assert not dialogos(app, "confirmar")
        assert bombear(raiz, lambda: terminado(app) and app.job.completed_count() == 3, timeout=10)

    def test_con_autoarranque_inicia_lo_escrito_sin_confirmar(self, raiz, con_clave):
        app = crear(raiz, config=cfg(auto_arranque=True, mensaje="uno\n---\ndos"))
        app._arrancar()
        assert not dialogos(app, "confirmar")
        assert bombear(raiz, lambda: terminado(app) and app.job and app.job.completed_count() == 2, timeout=10)

    def test_sin_autoarranque_no_envia_nada_solo(self, raiz, con_clave):
        app = crear(raiz, config=cfg(mensaje="uno"))
        app._arrancar()
        bombear(raiz, timeout=0.3)
        assert app.job is None and app.fab.cliente.messages.llamadas == 0

    def test_la_cola_del_modo_simple_no_se_ofrece_aqui(self, raiz, con_clave):
        job = Job.crear(["pregunta suelta"], cfg(), etiqueta=ETIQUETA)
        job.save()
        EstadoMonitor(current_job_id=job.job_id).guardar()
        app = crear(raiz)
        app._arrancar()
        assert not dialogos(app, "confirmar") and app.job is None

    def test_un_lote_terminado_no_se_ofrece(self, raiz, con_clave):
        job = Job.crear(["a"], cfg())
        job.messages[0].status = STATUS_COMPLETED
        job.save()
        EstadoMonitor(current_job_id=job.job_id).guardar()
        app = crear(raiz)
        app._arrancar()
        assert not dialogos(app, "confirmar")

    def test_un_estado_corrupto_no_impide_abrir(self, raiz, con_clave):
        from claude_batch_monitor import paths
        paths.STATE_FILE.write_text("{esto no es json", encoding="utf-8")
        app = crear(raiz)
        app._arrancar()          # no debe lanzar

    def test_los_lotes_viejos_se_purgan_pero_nunca_los_pendientes(self, raiz, con_clave):
        import os

        from claude_batch_monitor import paths
        viejo_hecho = self._guardar_viejo(os, paths, pendiente=False)
        viejo_pendiente = self._guardar_viejo(os, paths, pendiente=True)
        app = crear(raiz, config=cfg(retencion_dias=30))
        lanzar(raiz, app, "nuevo")
        assert not viejo_hecho.file_path.exists()
        assert viejo_pendiente.file_path.exists()

    def _guardar_viejo(self, os, paths, pendiente):
        job = Job.crear(["a", "b"], cfg())
        if not pendiente:
            for m in job.messages:
                m.status = STATUS_COMPLETED
        job.save()
        hace_un_ano = time.time() - 365 * 86400
        for f in paths.RESPONSES_DIR.glob(f"job_{job.job_id}*"):
            os.utime(f, (hace_un_ano, hace_un_ano))
        return job


class TestModoYCierre:
    def test_cambiar_a_simple_guarda_la_eleccion_y_lo_dice(self, raiz, con_clave):
        app = crear(raiz, config=cfg(interfaz="avanzada"))
        app.cambiar_a_simple()
        assert app.resultado == "simple" and ConfigManager.cargar().interfaz == "simple"

    def test_cambiar_de_modo_con_un_lote_en_marcha_pide_confirmacion(self, raiz, con_clave):
        suelta = threading.Event()
        app = crear(raiz)
        original = app.fab.cliente.messages._siguiente
        app.fab.cliente.messages._siguiente = lambda: (suelta.wait(5), original())[1]
        escribir(app, "a", "b")
        app.iniciar()
        bombear(raiz, timeout=0.2)
        app.responde_si = False
        app.cambiar_a_simple()
        assert app.resultado is None and dialogos(app, "confirmar")[-1][1] == "Cambiar de modo"
        suelta.set()
        bombear(raiz, lambda: terminado(app), timeout=10)

    def test_cerrar_sin_lote_sale_directamente_y_guarda(self, raiz, con_clave):
        app = crear(raiz)
        app.v["tope_gasto_usd"].set("11")
        app.cerrar()
        with pytest.raises(tk.TclError):
            raiz.winfo_exists()
            raiz.update()
        assert ConfigManager.cargar().tope_gasto_usd == D("11")

    def test_cerrar_con_un_lote_en_marcha_pregunta(self, raiz, con_clave):
        suelta = threading.Event()
        app = crear(raiz)
        original = app.fab.cliente.messages._siguiente
        app.fab.cliente.messages._siguiente = lambda: (suelta.wait(5), original())[1]
        escribir(app, "a", "b", "c")
        app.iniciar()
        bombear(raiz, timeout=0.2)
        app._preguntar_cierre = lambda: "volver"
        app.cerrar()
        assert raiz.winfo_exists() and app.ejecutor.activo               # no se cerró ni se paró
        app._preguntar_cierre = lambda: "salir"
        suelta.set()
        app.cerrar()
        assert not app.ejecutor.activo
        assert Job.load(app.job.job_id) is not None                     # lo hecho queda en disco

    def test_sin_bandeja_la_pregunta_de_cierre_es_si_o_no(self, raiz, con_clave):
        app = crear(raiz)
        assert app._usar_bandeja is False
        app.responde_si = False
        assert app._preguntar_cierre() == "volver"
        app.responde_si = True
        assert app._preguntar_cierre() == "salir"

    def test_cerrar_la_ventana_no_deja_hilos_vivos(self, raiz, con_clave):
        antes = set(threading.enumerate())
        app = crear(raiz)
        lanzar(raiz, app, "a")
        app._salir()
        time.sleep(0.2)
        vivos = [h.name for h in threading.enumerate() if h not in antes and h.name in ("lote", "estimar", "probar-clave")]
        assert vivos == []

    def test_no_se_pierden_temporizadores_al_cerrar(self, raiz, con_clave):
        app = crear(raiz)
        app.v["tope_gasto_usd"].set("6")             # programa un guardado
        assert app._temporizadores
        app._salir()
        assert not app._temporizadores

    def test_las_respuestas_viejas_de_la_v11_no_rompen_nada(self, raiz, con_clave):
        # Un config.json de la v11 trae `precio_input_mtok` como número y sin las claves nuevas.
        from claude_batch_monitor import paths
        paths.CONFIG_FILE.write_text(json.dumps({"modelo": "claude-haiku-4-5", "mensaje": "x\n---\ny"}), encoding="utf-8")
        app = crear(raiz, config=ConfigManager.cargar())
        assert app.var_cuenta.get() == "2 órdenes detectadas" and app.v["modelo"].get() == "claude-haiku-4-5"


def test_los_lotes_pendientes_cuentan_como_trabajo_para_el_boton():
    # `hay_trabajo` incluye fallidas y pendientes; es lo que decide si «Reanudar» está activo.
    job = Job.crear(["a", "b"], cfg())
    job.messages[0].status = STATUS_COMPLETED
    assert job.hay_trabajo() and job.messages[1].status == STATUS_PENDING
