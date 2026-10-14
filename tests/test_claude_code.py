"""Hablar con Claude Code: localizarlo, comprobar la sesión, la línea de órdenes y entender lo que imprime.

Nada de esto lanza el `claude` de verdad. Las líneas de salida son las que imprime el `claude` de mentira
(`claude_falso.py`), que copia las de Claude Code 2.1.296.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from claude_batch_monitor.core import claude_code as cc
from claude_batch_monitor.core.claude_code import (
    FIN,
    Ejecucion,
    EstadoClaude,
    EstadoPlan,
    LectorDeSalida,
    argumentos_de_claude,
    buscar_claude,
    comprobar_claude,
    entorno_para_el_plan,
    opcion_rechazada,
    reinicio_desde_texto,
    texto_del_sistema,
    tipo_de_limite,
)

from . import claude_falso as falso

UTC = timezone.utc
AHORA = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


def ejecutable(carpeta: Path, nombre: str = "claude") -> Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / nombre
    ruta.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ruta.chmod(0o755)
    return ruta


# ----------------------------------------------------------------------
# Dónde está
# ----------------------------------------------------------------------


class TestBuscarClaude:
    @pytest.fixture(autouse=True)
    def aislado(self, monkeypatch):
        monkeypatch.delenv(cc.VARIABLE_RUTA, raising=False)
        monkeypatch.setattr(cc.shutil, "which", lambda nombre: None)
        monkeypatch.setattr(cc, "_rutas_habituales", lambda: [])

    def test_la_variable_manda_sobre_todo_lo_demas(self, tmp_path, monkeypatch):
        forzado, otro = ejecutable(tmp_path, "forzado"), ejecutable(tmp_path, "otro")
        monkeypatch.setattr(cc.shutil, "which", lambda nombre: str(otro))
        monkeypatch.setenv(cc.VARIABLE_RUTA, str(forzado))
        assert buscar_claude(str(otro)) == str(forzado)

    def test_si_la_variable_apunta_a_la_nada_no_se_busca_en_otro_sitio(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc.shutil, "which", lambda nombre: str(ejecutable(tmp_path)))
        monkeypatch.setenv(cc.VARIABLE_RUTA, str(tmp_path / "no-existe"))
        assert buscar_claude() is None

    def test_la_ruta_de_la_configuracion(self, tmp_path):
        assert buscar_claude(str(ejecutable(tmp_path, "mio"))) == str(tmp_path / "mio")

    def test_el_path_del_sistema(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc.shutil, "which", lambda nombre: str(ejecutable(tmp_path)))
        assert buscar_claude() == str(tmp_path / "claude")

    def test_los_sitios_donde_lo_deja_el_instalador_si_el_path_no_lo_tiene(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc, "_rutas_habituales", lambda: [str(tmp_path / "nada"), str(ejecutable(tmp_path))])
        assert buscar_claude() == str(tmp_path / "claude")

    def test_un_archivo_que_no_se_puede_ejecutar_no_vale(self, tmp_path):
        malo = tmp_path / "claude"
        malo.write_text("no soy un programa", encoding="utf-8")
        malo.chmod(0o644)
        assert buscar_claude(str(malo)) is None

    def test_una_carpeta_no_vale(self, tmp_path):
        assert buscar_claude(str(tmp_path)) is None

    def test_sin_nada_no_lo_encuentra(self):
        assert buscar_claude() is None
        assert buscar_claude("") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="las rutas de Node con gestor de versiones son de Linux y macOS")
    def test_node_con_un_gestor_de_versiones_se_encuentra_aunque_el_path_no_lo_tenga(self, tmp_path, monkeypatch):
        """Un programa abierto con doble clic no hereda el PATH de la terminal, donde nvm y fnm añaden su carpeta."""
        monkeypatch.undo()  # la autouse de arriba vacía las rutas habituales: aquí se prueban de verdad
        monkeypatch.delenv(cc.VARIABLE_RUTA, raising=False)
        monkeypatch.setattr(cc.shutil, "which", lambda nombre: None)
        monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: tmp_path))
        antigua = ejecutable(tmp_path / ".nvm/versions/node/v18.19.0/bin")
        nueva = ejecutable(tmp_path / ".nvm/versions/node/v22.11.0/bin")
        os.utime(antigua, (1_000_000, 1_000_000))
        os.utime(nueva, (2_000_000, 2_000_000))
        assert buscar_claude() == str(nueva)                      # la versión de Node más reciente
        fnm = ejecutable(tmp_path / ".local/share/fnm/node-versions/v20.0.0/installation/bin")
        nueva.unlink()
        antigua.unlink()
        assert buscar_claude() == str(fnm)

    def test_las_rutas_habituales_no_dependen_del_sistema_para_no_fallar(self, monkeypatch):
        monkeypatch.undo()  # la autouse de arriba las vacía: aquí se prueban de verdad
        rutas = cc._rutas_habituales()
        assert rutas and all(isinstance(r, str) and r for r in rutas)


class TestEntorno:
    def test_se_quita_todo_lo_que_haria_pagar_por_token(self):
        base = {v: "x" for v in cc.VARIABLES_DE_PAGO_POR_USO}
        base.update({"PATH": "/usr/bin", "HOME": "/h", "ANTHROPIC_MODEL": "m"})
        limpio = entorno_para_el_plan(base)
        assert not set(cc.VARIABLES_DE_PAGO_POR_USO) & set(limpio)
        assert limpio == {"PATH": "/usr/bin", "HOME": "/h", "ANTHROPIC_MODEL": "m"}
        assert len(base) == len(cc.VARIABLES_DE_PAGO_POR_USO) + 3          # el original no se toca

    def test_por_omision_parte_del_entorno_del_programa(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secreta")
        monkeypatch.setenv("UNA_VARIABLE_CUALQUIERA", "valor")
        limpio = entorno_para_el_plan()
        assert "ANTHROPIC_API_KEY" not in limpio and limpio["UNA_VARIABLE_CUALQUIERA"] == "valor"
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-secreta"          # y el del programa tampoco

    def test_la_lista_incluye_las_variables_que_importan(self):
        assert {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY"} <= set(cc.VARIABLES_DE_PAGO_POR_USO)


# ----------------------------------------------------------------------
# La línea de órdenes
# ----------------------------------------------------------------------


class TestArgumentos:
    def test_lo_basico(self):
        orden = argumentos_de_claude("/x/claude")
        assert orden[:5] == ["/x/claude", "-p", "--output-format", "stream-json", "--verbose"]
        assert "--include-partial-messages" in orden and "--no-session-persistence" in orden
        assert orden[orden.index("--tools") + 1] == ""                       # sin herramientas
        assert "--disable-slash-commands" in orden and "--safe-mode" in orden
        assert "--model" not in orden and "--system-prompt-file" not in orden

    def test_con_modelo_y_con_archivo_de_instrucciones(self, tmp_path):
        orden = argumentos_de_claude("c", "opus", tmp_path / "i.txt")
        assert orden[orden.index("--model") + 1] == "opus"
        assert orden[orden.index("--system-prompt-file") + 1] == str(tmp_path / "i.txt")

    @pytest.mark.parametrize("modelo", ["claude-opus-4-1", "sonnet", "claude-sonnet-4-5[1m]", "a.b_c:d-e"])
    def test_modelos_con_pinta_de_modelo(self, modelo):
        assert "--model" in argumentos_de_claude("c", modelo)

    @pytest.mark.parametrize("modelo", ["sonnet; rm -rf /", "$(reboot)", "a b", "--help", "-x", "ñ", "a" * 200, "\nopus"])
    def test_lo_que_no_tiene_pinta_de_modelo_no_llega_a_la_linea(self, modelo):
        assert "--model" not in argumentos_de_claude("c", modelo)

    def test_se_pueden_quitar_las_opciones_que_una_version_no_conoce(self):
        sin = {"--safe-mode", "--tools", "--include-partial-messages"}
        orden = argumentos_de_claude("c", sin=sin)
        assert not sin & set(orden)
        assert "" not in orden                                                # el valor vacío de --tools se va con ella
        assert "--disable-slash-commands" in orden

    def test_la_pregunta_nunca_va_en_la_linea(self):
        assert all("pregunta" not in a for a in argumentos_de_claude("c", "opus", Path("/t/i.txt")))

    @pytest.mark.parametrize("texto, opcion", [
        ("error: unknown option '--safe-mode'\n(Did you mean --mode?)", "--safe-mode"),
        ("error: unknown option '-p'", "-p"),
        ("error: option '--tools <tools...>' argument missing", "--tools"),
        ("error: option '--system-prompt-file <path>' argument missing", "--system-prompt-file"),
        ("Todo bien", ""), ("", ""), (None, ""),
    ])
    def test_que_opcion_no_entiende(self, texto, opcion):
        assert opcion_rechazada(texto) == opcion

    def test_las_instrucciones_van_detras_de_la_base(self):
        assert texto_del_sistema("") == cc.SISTEMA_BASE == texto_del_sistema("   \n")
        t = texto_del_sistema("  Responde como un pirata  ")
        assert t.startswith(cc.SISTEMA_BASE) and t.endswith("Responde como un pirata")


# ----------------------------------------------------------------------
# ¿Está listo?
# ----------------------------------------------------------------------


def respuesta(codigo=0, salida="", error=""):
    return SimpleNamespace(returncode=codigo, stdout=salida.encode("utf-8"), stderr=error.encode("utf-8"))


class Ejecutor:
    """Un `subprocess.run` de mentira: contesta según la orden y anota lo que le piden."""

    def __init__(self, version="2.1.296 (Claude Code)", auth=None, falla=None):
        self.version, self.falla = version, falla
        self.auth = auth if auth is not None else respuesta(0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        self.llamadas = []

    def __call__(self, orden, **kw):
        self.llamadas.append((orden, kw))
        if self.falla:
            raise self.falla
        if orden[1:] == ["--version"]:
            return respuesta(0, self.version + "\n")
        if orden[1:] == ["auth", "status"]:
            return self.auth
        raise AssertionError(f"orden inesperada: {orden}")


@pytest.fixture
def instalado(tmp_path, monkeypatch):
    ruta = ejecutable(tmp_path)
    monkeypatch.setenv(cc.VARIABLE_RUTA, str(ruta))
    cc.olvidar_comprobaciones()
    yield str(ruta)
    cc.olvidar_comprobaciones()


class TestComprobarClaude:
    def test_sin_instalar(self, tmp_path, monkeypatch):
        monkeypatch.setenv(cc.VARIABLE_RUTA, str(tmp_path / "no"))
        ej = Ejecutor()
        assert comprobar_claude(ejecutar=ej).estado == "no_instalado" and ej.llamadas == []

    def test_listo_con_el_plan(self, instalado):
        e = comprobar_claude(ejecutar=Ejecutor())
        assert e.listo and e.estado == "ok" and e.ruta == instalado and e.version == "2.1.296" and e.metodo == "claude.ai"

    @pytest.mark.parametrize("metodo", ["claude.ai", "oauth_token"])
    def test_las_sesiones_de_plan_valen(self, instalado, metodo):
        auth = respuesta(0, json.dumps({"loggedIn": True, "authMethod": metodo}))
        assert comprobar_claude(ejecutar=Ejecutor(auth=auth), usar_cache=False).listo

    @pytest.mark.parametrize("metodo", ["api_key", "api_key_helper", "third_party"])
    def test_las_sesiones_de_pago_por_uso_no_valen(self, instalado, metodo):
        auth = respuesta(0, json.dumps({"loggedIn": True, "authMethod": metodo}))
        e = comprobar_claude(ejecutar=Ejecutor(auth=auth), usar_cache=False)
        assert e.estado == "no_es_plan" and e.metodo == metodo and not e.listo
        assert "claude auth logout" in e.explicacion() and metodo in e.explicacion()

    @pytest.mark.parametrize("auth", [
        respuesta(1, json.dumps({"loggedIn": False, "authMethod": "none"})),
        respuesta(0, json.dumps({"loggedIn": False})),
        respuesta(1, json.dumps({"authMethod": "none"})),
        respuesta(1, "Not logged in. Run `claude auth login`."),
        respuesta(1, "", "Not logged in"),
    ])
    def test_sin_sesion(self, instalado, auth):
        e = comprobar_claude(ejecutar=Ejecutor(auth=auth), usar_cache=False)
        assert e.estado == "sin_sesion" and "claude auth login" in e.explicacion()

    @pytest.mark.parametrize("auth", [
        respuesta(1, "", "error: unknown command 'auth'"),
        respuesta(1, "", "error: too many arguments. Expected 0 arguments but got 2.\n(Did you mean ...)"),
    ])
    def test_una_version_que_no_sabe_contar_su_sesion_se_da_por_buena_y_se_comprueba_al_usarla(self, instalado, auth):
        assert comprobar_claude(ejecutar=Ejecutor(auth=auth), usar_cache=False).listo

    def test_la_salida_con_avisos_alrededor_se_lee_igual(self, instalado):
        auth = respuesta(0, 'Aviso: hay una versión nueva\n{"loggedIn": true, "authMethod": "api_key"}\nOtro aviso\n')
        assert comprobar_claude(ejecutar=Ejecutor(auth=auth), usar_cache=False).estado == "no_es_plan"

    def test_texto_que_no_es_json_con_salida_0_se_da_por_bueno(self, instalado):
        assert comprobar_claude(ejecutar=Ejecutor(auth=respuesta(0, "Logged in as x@y.z")), usar_cache=False).listo

    def test_una_version_antigua_se_dice(self, instalado):
        e = comprobar_claude(ejecutar=Ejecutor(version="1.0.88 (Claude Code)"), usar_cache=False)
        assert e.estado == "antiguo" and e.version == "1.0.88" and "claude update" in e.explicacion()

    def test_si_no_dice_su_version_se_prueba_igualmente(self, instalado):
        assert comprobar_claude(ejecutar=Ejecutor(version="Claude Code"), usar_cache=False).listo

    @pytest.mark.parametrize("falla", [subprocess.TimeoutExpired("claude", 30), OSError("no se puede ejecutar")])
    def test_si_no_se_puede_ejecutar_o_no_responde_es_un_error_no_una_sesion_caducada(self, instalado, falla):
        e = comprobar_claude(ejecutar=Ejecutor(falla=falla), usar_cache=False)
        assert e.estado == "error" and not e.listo and e.detalle

    def test_no_pasa_la_clave_de_api_ni_usa_la_terminal(self, instalado, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secreta")
        ej = Ejecutor()
        comprobar_claude(ejecutar=ej, usar_cache=False)
        assert ej.llamadas
        for _, kw in ej.llamadas:
            assert "ANTHROPIC_API_KEY" not in kw["env"] and kw["stdin"] == subprocess.DEVNULL and kw["timeout"] > 0

    def test_lo_que_esta_bien_se_recuerda_un_rato(self, instalado):
        ej = Ejecutor()
        assert comprobar_claude(ejecutar=ej).listo
        n = len(ej.llamadas)
        assert comprobar_claude(ejecutar=ej).listo and len(ej.llamadas) == n         # no vuelve a lanzar nada
        assert comprobar_claude(ejecutar=ej, usar_cache=False).listo and len(ej.llamadas) > n

    def test_lo_que_esta_mal_no_se_recuerda_para_que_arreglarlo_valga_al_momento(self, instalado):
        sin_sesion = Ejecutor(auth=respuesta(1, json.dumps({"loggedIn": False, "authMethod": "none"})))
        assert comprobar_claude(ejecutar=sin_sesion).estado == "sin_sesion"
        assert comprobar_claude(ejecutar=Ejecutor()).listo                           # ya inició sesión: sin esperar 90 s

    def test_lo_recordado_caduca(self, instalado, monkeypatch):
        ej = Ejecutor()
        comprobar_claude(ejecutar=ej)
        n = len(ej.llamadas)
        monkeypatch.setattr(cc, "SEGUNDOS_DE_CACHE", 0.0)
        comprobar_claude(ejecutar=ej)
        assert len(ej.llamadas) > n

    def test_cada_programa_se_recuerda_por_separado(self, tmp_path, monkeypatch):
        a, b = ejecutable(tmp_path, "a"), ejecutable(tmp_path, "b")
        cc.olvidar_comprobaciones()
        ej = Ejecutor()
        monkeypatch.setenv(cc.VARIABLE_RUTA, str(a))
        comprobar_claude(ejecutar=ej)
        monkeypatch.setenv(cc.VARIABLE_RUTA, str(b))
        n = len(ej.llamadas)
        comprobar_claude(ejecutar=ej)
        assert len(ej.llamadas) > n
        cc.olvidar_comprobaciones()

    @pytest.mark.parametrize("estado", ["ok", "no_instalado", "antiguo", "sin_sesion", "no_es_plan", "error", "otro"])
    def test_toda_situacion_tiene_una_explicacion_en_cristiano(self, estado):
        texto = EstadoClaude(estado, "/x", "2.0.0", "api_key", "algo").explicacion()
        assert isinstance(texto, str) and len(texto) > 15


# ----------------------------------------------------------------------
# El texto de un límite
# ----------------------------------------------------------------------


class TestTipoDeLimite:
    @pytest.mark.parametrize("texto, tipo", [
        ("You've hit your session limit · resets 3:45pm", "sesion"),
        ("5-hour limit reached ∙ resets 3pm", "sesion"),
        ("Claude usage limit reached. Your limit will reset at 3pm", "sesion"),
        ("You've hit your weekly limit · resets Mon 12:00am", "semana"),
        ("You've hit your 7-day limit", "semana"),
        ("You've hit your Opus limit · resets Oct 20, 9am", "opus"),
        ("You've hit your Sonnet limit", "sonnet"),
        ("You've hit your monthly spend limit · raise it at https://claude.ai/settings/usage", "gasto"),
        ("Your organization's monthly spend limit was reached", "gasto"),
        ("You've reached your usage limit", "otro"),
        ("API Error: Overloaded", ""), ("", ""), (None, ""), ("Respuesta normal sobre límites de velocidad", ""),
    ])
    def test_que_limite_es(self, texto, tipo):
        assert tipo_de_limite(texto) == tipo


def a_las(h, m=0, dia=13, mes=10, zona=UTC):
    return datetime(2026, mes, dia, h, m, tzinfo=zona)


class TestReinicioDesdeElTexto:
    @pytest.mark.parametrize("texto, esperado", [
        ("You've hit your session limit · resets 3:45pm", a_las(15, 45)),
        ("resets 3pm", a_las(15)),
        ("resets 3 pm", a_las(15)),
        ("resets 12am", a_las(0, dia=14)),                       # medianoche: la de mañana
        ("resets 12:30pm", a_las(12, 30, dia=14)),               # las 12:30 de hoy ya pasaron (son las 14:00)
        ("resets 1pm", a_las(13, dia=14)),                       # ya pasó hoy: mañana
        ("resets 15:45", a_las(15, 45)),
        ("resets 9:05 am", a_las(9, 5, dia=14)),
        ("limit reached ∙ resets 11:00 PM", a_las(23)),
        ("resets Mon 12:00am", a_las(0, dia=19)),                # el 13/10/2026 es martes: el lunes siguiente es el 19
        ("resets Tuesday 3pm", a_las(15)),                       # hoy es martes y las 15 aún no han llegado
        ("resets Tue 1pm", a_las(13, dia=20)),                   # hoy ya pasó: el martes que viene
        ("resets Oct 20, 9am", a_las(9, dia=20)),
        ("resets 20 Oct, 9am", a_las(9, dia=20)),
        ("resets Nov 2 at 3:30pm", a_las(15, 30, dia=2, mes=11)),
    ])
    def test_formas_de_decir_la_hora(self, texto, esperado):
        assert reinicio_desde_texto(texto, AHORA) == esperado

    def test_con_zona_horaria_entre_parentesis(self):
        zoneinfo = pytest.importorskip("zoneinfo")
        try:
            madrid = zoneinfo.ZoneInfo("Europe/Madrid")
        except Exception:
            pytest.skip("este equipo no tiene la base de zonas horarias")
        # 13/10/2026 14:00 UTC son las 16:00 en Madrid (CEST, +2).
        assert reinicio_desde_texto("resets 7:30pm (Europe/Madrid)", AHORA) == datetime(2026, 10, 13, 19, 30, tzinfo=madrid)
        un_dia_mas = reinicio_desde_texto("resets 3pm (Europe/Madrid)", AHORA)       # las 15:00 de Madrid ya pasaron
        assert un_dia_mas == datetime(2026, 10, 14, 15, 0, tzinfo=madrid)

    def test_una_zona_que_no_existe_se_ignora_y_vale_la_del_equipo(self):
        assert reinicio_desde_texto("resets 3pm (Mars/Olympus)", AHORA) == a_las(15)

    def test_el_formato_antiguo_con_la_marca_de_tiempo(self):
        instante = AHORA + timedelta(hours=2)
        assert reinicio_desde_texto(f"Claude AI usage limit reached|{int(instante.timestamp())}", AHORA) == instante

    @pytest.mark.parametrize("texto", [
        "", None, "You've hit your limit", "resets", "resets soon", "resets 25:00", "resets 0pm", "resets 13pm",
        "resets Feb 30, 9am", "resets Dec 31, 9am",                               # (la última queda a más de 40 días)
        "usage limit reached|12", "usage limit reached|99999999999999999",
        "no se reinicia nunca", "RESETS",
    ])
    def test_lo_que_no_se_entiende_es_none_y_no_una_hora_inventada(self, texto):
        assert reinicio_desde_texto(texto, AHORA) is None

    def test_sin_hora_de_referencia_usa_el_reloj(self):
        r = reinicio_desde_texto("resets 3:45pm")
        assert r is not None and r > datetime.now(UTC) - timedelta(minutes=1) and r - datetime.now(UTC) <= timedelta(days=1, minutes=1)


@given(texto=st.text(max_size=300))
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_ningun_texto_hace_fallar_al_lector_de_limites(texto):
    reinicio_desde_texto(texto, AHORA)
    reinicio_desde_texto("resets " + texto, AHORA)
    tipo_de_limite(texto)
    opcion_rechazada(texto)


# ----------------------------------------------------------------------
# La salida de `claude -p`
# ----------------------------------------------------------------------


def lineas_de(funcion, *args, **kw) -> list[str]:
    """Lo que imprime el `claude` de mentira para una respuesta o un fallo, línea a línea."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        funcion(*args, **kw)
    return [x for x in buffer.getvalue().splitlines() if x.strip()]


def leer(lineas, codigo=0, stderr="", ahora=AHORA):
    lector = LectorDeSalida()
    nuevo = "".join(lector.alimentar(x, ahora) for x in lineas)
    return lector, nuevo, lector.veredicto(codigo, stderr, ahora)


class TestUnaRespuesta:
    def test_ok(self):
        lector, escrito, v = leer(lineas_de(falso._responder, "¿Qué es un token?", "s1", 77))
        assert v.tipo == "ok" and v.texto == "Respuesta a: ¿Qué es un token?" and v.stop_reason == "end_turn"
        assert escrito == v.texto == lector.parcial                        # lo que fue llegando es lo que quedó
        assert v.uso["output_tokens"] == 77 and v.uso["input_tokens"] > 0 and v.session_id == "s1"
        assert not v.usando_extra and not lector.usa_clave_de_api()

    def test_el_uso_del_plan_sale_del_evento(self):
        lector, _, _ = leer(lineas_de(falso._responder, "p", "s1", 5))
        assert round(lector.plan.sesion.uso) == 13 and round(lector.plan.semana.uso) == 2
        assert lector.plan.sesion.reinicia is not None and lector.plan.situacion == "allowed"
        assert lector.plan.conocido() and "sesión 13 %" in lector.plan.resumen()

    def test_gastar_creditos_extra_se_nota(self):
        lector, _, v = leer(lineas_de(falso._responder, "p", "s1", 5, usando_extra=True))
        assert v.tipo == "ok" and v.usando_extra and lector.plan.usando_extra

    def test_la_respuesta_vacia_sigue_siendo_ok_y_texto_vacio(self):
        _, _, v = leer(lineas_de(falso._responder, "p", "s1", 1, texto=""))
        assert v.tipo == "ok" and v.texto == ""

    def test_si_el_resultado_no_trae_el_texto_se_usa_el_del_mensaje(self):
        lineas = [x for x in lineas_de(falso._responder, "pregunta", "s1", 5)]
        resultado = json.loads(lineas[-1])
        resultado["result"] = ""
        lineas[-1] = json.dumps(resultado)
        _, _, v = leer(lineas)
        assert v.tipo == "ok" and v.texto == "Respuesta a: pregunta"

    def test_truncada_y_rechazada_conservan_su_motivo(self):
        assert leer(lineas_de(falso._responder, "p", "s", 5, stop="max_tokens"))[2].stop_reason == "max_tokens"
        assert leer(lineas_de(falso._responder, "p", "s", 5, stop="refusal"))[2].stop_reason == "refusal"

    def test_sin_la_linea_final_y_con_codigo_0_no_se_da_por_buena(self):
        _, _, v = leer(lineas_de(falso._responder, "p", "s", 5, con_resultado=False), codigo=0)
        assert v.tipo == "otro" and "formato" in v.texto

    def test_sin_la_linea_final_y_con_error_de_salida_se_cuenta_lo_que_dijo_stderr(self):
        _, _, v = leer(lineas_de(falso._responder, "p", "s", 5, con_resultado=False), codigo=3, stderr="Error: se vino abajo")
        assert v.tipo == "otro" and "se vino abajo" in v.texto

    def test_no_dice_nada_y_se_cae(self):
        _, _, v = leer([], codigo=137, stderr="Killed")
        assert v.tipo == "otro" and v.texto == "Killed"

    def test_nada_de_nada_con_codigo_0(self):
        _, _, v = leer([], codigo=0)
        assert v.tipo == "otro"

    @pytest.mark.parametrize("origen, usa_clave", [
        ("none", False), ("", False), ("oauth", False), ("OAUTH", False),
        ("ANTHROPIC_API_KEY", True), ("apiKeyHelper", True), ("/login managed key", True), ("project", True),
    ])
    def test_de_donde_salen_las_credenciales(self, origen, usa_clave):
        lector = LectorDeSalida()
        lector.alimentar(json.dumps(falso._inicio("s", origen)))
        assert lector.usa_clave_de_api() is usa_clave

    def test_si_no_dice_de_donde_salen_no_se_acusa_a_nadie(self):
        lector = LectorDeSalida()
        lector.alimentar(json.dumps({"type": "system", "subtype": "init", "session_id": "s"}))
        assert not lector.usa_clave_de_api()
        assert not LectorDeSalida().usa_clave_de_api()


class TestLosFallos:
    def fallo(self, texto, estado, error, evento=None, subtipo="success"):
        return lineas_de(falso._fallo, "s1", texto, estado, error, evento, subtipo)

    def limite(self, texto, segundos=3600, tipo="five_hour", **kw):
        ahora = time.time()
        return self.fallo(texto, 429, "rate_limit", falso._evento_de_limite("rejected", ahora + segundos, tipo, **kw))

    def test_limite_de_la_sesion_con_su_hora(self):
        lector, _, v = leer(self.limite("You've hit your session limit · resets 3:45pm", 5400), codigo=1, ahora=datetime.now(UTC))
        assert v.tipo == "limite" and v.ventana == "sesion"
        assert abs((v.reinicia - datetime.now(UTC)).total_seconds() - 5400) < 5
        assert lector.plan.bloqueado_hasta == v.reinicia and lector.plan.situacion == "rejected"

    def test_un_limite_de_verdad_tal_como_lo_registro_claude_code(self):
        """Los valores son los de un registro real: la sesión de Claude Code que escribió este programa agotó sus 5 horas
        el 13/10/2026 a las 20:24 UTC (mensaje del asistente con `error: rate_limit`, `usage_limit_reached` y HTTP 429)."""
        ahora = datetime(2026, 10, 13, 20, 24, 36, tzinfo=UTC)
        texto = "You've hit your session limit · resets 12:10am (UTC)"
        evento = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": 1791936600, "rateLimitType": "five_hour", "overageStatus": "rejected",
            "overageDisabledReason": "out_of_credits", "isUsingOverage": False}}
        inicio = {"type": "system", "subtype": "init", "apiKeySource": "none", "session_id": "s"}
        asistente = {"type": "assistant", "error": "rate_limit", "session_id": "s", "message": {
            "model": "<synthetic>", "role": "assistant", "type": "message", "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": texto}]}}
        resultado = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429, "result": texto,
                     "session_id": "s"}
        con_evento = [json.dumps(x) for x in (inicio, asistente, evento, resultado)]
        sin_evento = [json.dumps(x) for x in (inicio, asistente, resultado)]
        for lineas in (con_evento, sin_evento):                  # con el evento y solo con el texto: la misma hora
            lector, _, v = leer(lineas, codigo=1, ahora=ahora)
            assert v.tipo == "limite" and v.ventana == "sesion" and v.texto == texto
            assert v.reinicia == datetime(2026, 10, 14, 0, 10, tzinfo=UTC)
            assert not lector.usa_clave_de_api() and not lector.plan.usando_extra
        assert leer(con_evento, codigo=1, ahora=ahora)[0].plan.extra_activado is False

    def test_limite_semanal(self):
        _, _, v = leer(self.limite("You've hit your weekly limit · resets Mon 12:00am", 3 * 86400, "seven_day"), codigo=1,
                       ahora=datetime.now(UTC))
        assert v.tipo == "limite" and v.ventana == "semana" and v.reinicia is not None

    def test_limite_solo_con_el_texto(self):
        ahora = datetime.now(UTC).astimezone()
        hora = (ahora + timedelta(minutes=95)).strftime("%I:%M%p").lstrip("0").lower()
        _, _, v = leer(self.fallo(f"You've hit your session limit · resets {hora}", 429, "rate_limit"), codigo=1, ahora=ahora)
        assert v.tipo == "limite" and v.ventana == "sesion"
        assert abs((v.reinicia - ahora).total_seconds() - 95 * 60) <= 60                       # de minuto en minuto

    def test_limite_sin_ninguna_hora(self):
        _, _, v = leer(lineas_de(falso._fallo, "s", "You've hit your session limit", 429, "rate_limit",
                                 falso._evento_de_limite("rejected", None, ventanas=False)), codigo=1)
        assert v.tipo == "limite" and v.reinicia is None

    def test_pedir_creditos_no_es_esperar(self):
        _, _, v = leer(lineas_de(falso._fallo, "s", "Usage credits required to continue", 429, "rate_limit",
                                 falso._evento_de_limite("rejected", time.time() + 3600,
                                                         extra={"errorCode": "credits_required"})), codigo=1)
        assert v.tipo == "creditos"

    @pytest.mark.parametrize("texto, tipo", [
        ("You've hit your monthly spend limit · raise it", "gasto"),
        ("Your organization's spend limit has been reached", "gasto"),
        ("Credit balance is too low", "creditos"),
        ("API Error: billing_error", "creditos"),
    ])
    def test_los_limites_de_dinero_no_se_esperan(self, texto, tipo):
        assert leer(self.fallo(texto, 429, "rate_limit"), codigo=1)[2].tipo == tipo

    @pytest.mark.parametrize("texto, estado, error, tipo", [
        ("Not logged in · Please run /login", 401, "authentication_failed", "auth"),
        ("OAuth token has expired", 401, "authentication_failed", "auth"),
        ("Invalid API key · Fix external API key", 401, "authentication_failed", "auth"),
        ("Your account is on hold. Contact support.", 403, "account_on_hold", "cuenta"),
        ("Your organization is not allowed to use Claude Code", 403, "oauth_org_not_allowed", "cuenta"),
        ("There's an issue with the selected model (x). It may not exist or you may not have access to it.", 404,
         "model_not_found", "modelo"),
        ("API Error: 400 invalid_request_error: messages: text content blocks must be non-empty", 400, "invalid_request",
         "invalido"),
        ("Prompt is too long", 400, "invalid_request", "invalido"),
        ("API Error: Overloaded (529)", 529, "overloaded", "saturado"),
        ("API Error: 500 Internal server error", 500, "server_error", "saturado"),
        ("Unable to connect to API (ECONNRESET)", None, "unknown", "saturado"),
        ("Algo que nadie había visto nunca", None, "algo_nuevo", "otro"),
    ])
    def test_cada_error_se_clasifica(self, texto, estado, error, tipo):
        _, _, v = leer(self.fallo(texto, estado, error), codigo=1)
        assert v.tipo == tipo and v.texto

    def test_un_fallo_que_dice_success_sigue_siendo_un_fallo(self):
        """Claude Code puede contar un error de la API con `subtype: "success"` y código 0 (anthropics/claude-code#79500)."""
        _, _, v = leer(self.fallo("API Error: Overloaded (529)", 529, "overloaded", subtipo="success"), codigo=0)
        assert v.tipo == "saturado"

    def test_el_rechazo_del_evento_manda_aunque_el_texto_no_diga_nada_claro(self):
        _, _, v = leer(self.fallo("Request rejected", 429, "rate_limit",
                                  falso._evento_de_limite("rejected", time.time() + 600)), codigo=1, ahora=datetime.now(UTC))
        assert v.tipo == "limite" and v.reinicia is not None

    def test_una_opcion_que_no_conoce(self):
        _, _, v = leer([], codigo=1, stderr="error: unknown option '--safe-mode'")
        assert v.tipo == "otro" or v.tipo == "opcion"
        lector = LectorDeSalida()
        lector.alimentar(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                                     "result": "error: unknown option '--safe-mode'", "session_id": "s"}))
        assert lector.veredicto(1, "", AHORA).tipo == "opcion" and lector.veredicto(1, "", AHORA).opcion == "--safe-mode"


class TestLoQueNoEsDelFormato:
    def test_lineas_que_no_son_json_se_guardan_acotadas(self):
        lector = LectorDeSalida()
        for i in range(50):
            assert lector.alimentar(f"basura {i} " + "x" * 1000) == ""
        assert len(lector.ajenas) == cc.MAX_LINEAS_AJENAS and all(len(x) <= 300 for x in lector.ajenas)

    @pytest.mark.parametrize("linea", ["", "   ", "[]", "[1, 2]", '"texto"', "42", "null", "true", "{}", '{"type": 5}',
                                       '{"type": "stream_event"}', '{"type": "stream_event", "event": "no"}',
                                       '{"type": "stream_event", "event": {"type": "content_block_delta", "delta": 3}}',
                                       '{"type": "assistant", "message": 5}', '{"type": "assistant", "message": {"content": 7}}',
                                       '{"type": "rate_limit_event", "rate_limit_info": 7}',
                                       '{"type": "system", "subtype": "init", "apiKeySource": 5}',
                                       '{"type": "result"}', '{"type": "result", "result": 5, "is_error": "si"}'])
    def test_lineas_raras_no_rompen_nada(self, linea):
        lector = LectorDeSalida()
        assert isinstance(lector.alimentar(linea), str)
        lector.veredicto(0, "", AHORA)
        lector.veredicto(1, "x", AHORA)

    @pytest.mark.parametrize("errores", [10, 1.5, True, None, "texto suelto", {"a": 1}, [], ["uno", 2, None], [["anidado"]]])
    def test_un_campo_de_errores_que_no_es_una_lista_de_textos_no_rompe_el_veredicto(self, errores):
        """Lo encontró el test de propiedades: `"errors": 10` hacía fallar el lector (no se puede recorrer un número)."""
        lector = LectorDeSalida()
        lector.alimentar(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                                     "result": "", "errors": errores, "session_id": "s"}))
        assert lector.veredicto(1, "", AHORA).tipo in cc.TIPOS_DE_VEREDICTO

    def test_los_errores_de_la_linea_final_se_leen_tanto_en_lista_como_sueltos(self):
        frase = "You've hit your session limit · resets 3:45pm (UTC)"
        for errores in ([frase], frase):
            lector = LectorDeSalida()
            lector.alimentar(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                                         "result": "", "errors": errores, "session_id": "s"}))
            assert lector.veredicto(1, "", AHORA).tipo == "limite", errores

    def test_los_mensajes_de_subagentes_no_se_mezclan_con_la_respuesta(self):
        lector = LectorDeSalida()
        delta = {"type": "stream_event", "parent_tool_use_id": "toolu_1",
                 "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "NO"}}}
        assert lector.alimentar(json.dumps(delta)) == "" and lector.parcial == ""

    @pytest.mark.parametrize("utilizacion", [True, False, "0.5", None, -1, float("nan"), float("inf"), 1e9, [], {}])
    def test_un_uso_que_no_es_creible_se_ignora(self, utilizacion):
        lector = LectorDeSalida()
        evento = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": utilizacion}}}}
        lector.alimentar(json.dumps(evento))
        assert lector.plan.sesion.uso is None

    @pytest.mark.parametrize("reinicio", [0, -5, 1, "mañana", None, True, 10**30, 4_102_444_800])  # (el último: el año 2100)
    def test_una_hora_de_reinicio_absurda_se_ignora(self, reinicio):
        lector = LectorDeSalida()
        lector.alimentar(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "five_hour", "resetsAt": reinicio}}), AHORA)
        assert lector.plan.sesion.reinicia is None and lector.plan.bloqueado_hasta is None

    def test_la_utilizacion_puede_venir_como_fraccion_o_como_porcentaje(self):
        lector = LectorDeSalida()
        for valor, esperado in ((0.25, 25.0), (0.0, 0.0), (42.0, 42.0), (100.0, 100.0)):
            lector.alimentar(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
                "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": valor}}}}))
            assert lector.plan.sesion.uso == pytest.approx(esperado)

    def test_un_evento_posterior_que_no_trae_datos_no_borra_lo_ya_sabido(self):
        lector = LectorDeSalida()
        lector.alimentar(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.4}}}}))
        lector.alimentar(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}))
        assert lector.plan.sesion.uso == pytest.approx(40.0)

    def test_aceptado_de_nuevo_levanta_el_bloqueo(self):
        lector = LectorDeSalida()
        ahora = datetime.now(UTC)
        lector.alimentar(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "five_hour", "resetsAt": int(ahora.timestamp()) + 600}}), ahora)
        assert lector.plan.bloqueado_hasta is not None
        lector.alimentar(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}), ahora)
        assert lector.plan.bloqueado_hasta is None


JSON_CUALQUIERA = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=20),
    lambda hijos: st.lists(hijos, max_size=4) | st.dictionaries(st.text(max_size=12), hijos, max_size=5), max_leaves=12)


@given(lineas=st.lists(
    st.one_of(
        st.text(max_size=120),
        JSON_CUALQUIERA.map(lambda x: json.dumps(x, allow_nan=True)),
        st.fixed_dictionaries({"type": st.sampled_from(
            ["system", "assistant", "stream_event", "rate_limit_event", "result", "user", "x"])},
            optional={"subtype": st.sampled_from(["init", "api_retry", "status", "success"]),
                      "event": JSON_CUALQUIERA, "message": JSON_CUALQUIERA, "rate_limit_info": JSON_CUALQUIERA,
                      "result": JSON_CUALQUIERA, "is_error": JSON_CUALQUIERA, "error": JSON_CUALQUIERA,
                      "apiKeySource": JSON_CUALQUIERA, "usage": JSON_CUALQUIERA, "api_error_status": JSON_CUALQUIERA,
                      "errors": JSON_CUALQUIERA}).map(lambda d: json.dumps(d, allow_nan=True))),
    max_size=12),
    codigo=st.one_of(st.none(), st.integers(-300, 300)), stderr=st.text(max_size=80))
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_ninguna_salida_hace_fallar_al_lector(lineas, codigo, stderr):
    lector = LectorDeSalida()
    for linea in lineas:
        assert isinstance(lector.alimentar(linea, AHORA), str)
    v = lector.veredicto(codigo, stderr, AHORA)
    assert v.tipo in cc.TIPOS_DE_VEREDICTO
    lector.usa_clave_de_api()
    lector.plan.resumen()


class TestEstadoPlan:
    def test_sin_datos_no_hay_resumen(self):
        assert not EstadoPlan().conocido() and EstadoPlan().resumen() == ""

    def test_con_datos(self):
        p = EstadoPlan(sesion=cc.VentanaPlan(13.4, datetime.now(UTC) + timedelta(hours=2)),
                       semana=cc.VentanaPlan(2.0, datetime.now(UTC) + timedelta(days=5)))
        assert p.conocido()
        assert p.resumen().startswith("sesión 13 % (se renueva a las ") and "semana 2 % (se renueva " in p.resumen()


# ----------------------------------------------------------------------
# Un proceso de verdad
# ----------------------------------------------------------------------

pytestmark_proceso = pytest.mark.skipif(sys.platform == "win32", reason="los grupos de procesos son de Unix")


def python(codigo: str) -> list[str]:
    return [sys.executable, "-u", "-c", codigo]


def todas(ej: Ejecucion, espera=10.0) -> list:
    salida = []
    fin = time.monotonic() + espera
    while time.monotonic() < fin:
        x = ej.siguiente(0.2)
        if x is FIN:
            return salida
        if x is not None:
            salida.append(x)
    raise AssertionError(f"no terminó: {salida}")


class TestEjecucion:
    def test_la_entrada_va_en_utf8_y_la_salida_llega_por_lineas(self, tmp_path):
        ej = Ejecucion(python("import sys; t=sys.stdin.buffer.read().decode('utf-8'); print(len(t)); print(t.upper()); print('fin')"),
                       "¿Qué canción es «Ñandú»?", dict(os.environ), tmp_path)
        try:
            assert todas(ej) == ["24\n", "¿QUÉ CANCIÓN ES «ÑANDÚ»?\n", "fin\n"]
            assert ej.esperar_codigo(5) == 0 and ej.codigo == 0
        finally:
            ej.terminar()
            ej.cerrar()

    def test_la_salida_se_decodifica_siempre_como_utf8(self, tmp_path):
        # (el código del hijo es solo ASCII: bajo `LC_ALL=C` un argumento con tildes no llegaría entero)
        ej = Ejecucion(python("import sys; sys.stdout.buffer.write('\\u00f1and\\u00fa \\u00abok\\u00bb\\n'.encode('utf-8')); "
                              "sys.stdout.buffer.write(b'\\xff\\xfe roto\\n')"),
                       "", {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0"}, tmp_path)
        try:
            lineas = todas(ej)
            assert lineas[0] == "ñandú «ok»\n" and lineas[1].endswith(" roto\n")          # lo ilegible no tumba nada
        finally:
            ej.cerrar()

    def test_una_entrada_enorme_no_bloquea_aunque_el_programa_conteste_sin_leerla(self, tmp_path):
        ej = Ejecucion(python("print('ya respondí')"), "x" * 5_000_000, dict(os.environ), tmp_path)
        try:
            assert todas(ej) == ["ya respondí\n"]
            assert ej.esperar_codigo(5) == 0
        finally:
            ej.terminar()
            ej.cerrar()

    def test_una_entrada_grande_que_el_programa_lee_entera(self, tmp_path):
        ej = Ejecucion(python("import sys; print(len(sys.stdin.read()))"), "ab" * 600_000, dict(os.environ), tmp_path)
        try:
            assert todas(ej, 20) == ["1200000\n"]
        finally:
            ej.cerrar()

    def test_el_stderr_se_guarda_acotado_por_el_final(self, tmp_path):
        ej = Ejecucion(python("import sys\nfor i in range(3000): sys.stderr.write('linea %05d de error\\n' % i)\nsys.exit(3)"),
                       "", dict(os.environ), tmp_path)
        try:
            todas(ej)
            assert ej.esperar_codigo(5) == 3
            e = ej.stderr
            assert len(e) <= cc.MAX_BYTES_STDERR and e.rstrip().endswith("linea 02999 de error")
        finally:
            ej.cerrar()

    def test_el_codigo_de_salida(self, tmp_path):
        ej = Ejecucion(python("import sys; sys.exit(7)"), "", dict(os.environ), tmp_path)
        try:
            todas(ej)
            assert ej.esperar_codigo(5) == 7
        finally:
            ej.cerrar()

    def test_si_no_hay_nada_que_leer_devuelve_none_sin_bloquear_para_siempre(self, tmp_path):
        ej = Ejecucion(python("import time; time.sleep(30)"), "", dict(os.environ), tmp_path)
        try:
            inicio = time.monotonic()
            assert ej.siguiente(0.3) is None and time.monotonic() - inicio < 2
            assert ej.codigo is None
        finally:
            ej.terminar()
            ej.cerrar()

    @pytest.mark.skipif(sys.platform == "win32", reason="los grupos de procesos son de Unix")
    def test_terminar_mata_tambien_lo_que_el_proceso_lanzo(self, tmp_path):
        pid_nieto = tmp_path / "nieto.pid"
        codigo = ("import subprocess, sys, time\n"
                  f"n = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                  f"open({str(pid_nieto)!r}, 'w').write(str(n.pid))\n"
                  "print('listo', flush=True)\n"
                  "time.sleep(60)\n")
        ej = Ejecucion(python(codigo), "", dict(os.environ), tmp_path)
        try:
            assert ej.siguiente(10) == "listo\n"
            nieto = int(pid_nieto.read_text())
            padre = ej.proc.pid
            ej.terminar()
            assert ej.codigo is not None
            from .test_simulacros_plan import vivo

            fin = time.monotonic() + 5
            while (vivo(nieto) or vivo(padre)) and time.monotonic() < fin:
                time.sleep(0.05)
            assert not vivo(padre) and not vivo(nieto), "quedó algún proceso vivo"
        finally:
            ej.cerrar()

    @pytest.mark.skipif(sys.platform == "win32", reason="las señales son de Unix")
    def test_un_proceso_que_ignora_la_orden_de_terminar_se_mata_igual(self, tmp_path):
        codigo = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('listo', flush=True)\ntime.sleep(60)"
        ej = Ejecucion(python(codigo), "", dict(os.environ), tmp_path)
        try:
            assert ej.siguiente(10) == "listo\n"
            inicio = time.monotonic()
            ej.terminar(gracia=0.5)
            assert ej.codigo is not None and time.monotonic() - inicio < 5
        finally:
            ej.cerrar()

    def test_terminar_dos_veces_o_tras_acabar_no_hace_nada_raro(self, tmp_path):
        ej = Ejecucion(python("print(1)"), "", dict(os.environ), tmp_path)
        try:
            todas(ej)
            ej.esperar_codigo(5)
            ej.terminar()
            ej.terminar()
            ej.pedir_que_termine()
        finally:
            ej.cerrar()
            ej.cerrar()

    def test_un_programa_que_no_existe_lanza_oserror_para_que_el_motor_lo_cuente(self, tmp_path):
        with pytest.raises(OSError):
            Ejecucion([str(tmp_path / "no-existe")], "x", dict(os.environ), tmp_path)

    def test_el_entorno_que_se_le_da_es_el_unico_que_ve(self, tmp_path):
        ej = Ejecucion(python("import os; print(sorted(k for k in os.environ if k.startswith('PRUEBA_')))"), "",
                       {**os.environ, "PRUEBA_UNO": "1"}, tmp_path)
        try:
            assert todas(ej) == ["['PRUEBA_UNO']\n"]
        finally:
            ej.cerrar()
