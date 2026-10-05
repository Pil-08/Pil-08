"""El idioma de la interfaz: cómo se averigua el del sistema y que dé lo mismo en cualquier versión de Python."""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

from claude_batch_monitor.gui import i18n

pytestmark = pytest.mark.idioma_real  # aquí SÍ se mira el sistema (el resto de las pruebas lo fijan en español)

VARIABLES = ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE")


@pytest.fixture(autouse=True)
def entorno_limpio(monkeypatch):
    for v in VARIABLES:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    yield
    i18n.fijar_idioma("es")


class TestCodigoDeIdioma:
    @pytest.mark.parametrize("codigo", ["es", "ES", "es_ES.UTF-8", "es-MX", "es_AR@euro", "Spanish_Spain", "spanish"])
    def test_espanol(self, codigo):
        assert i18n._idioma_de(codigo) == "es"

    @pytest.mark.parametrize("codigo", ["en", "en_US.UTF-8", "en-GB", "English_United States", "english"])
    def test_ingles(self, codigo):
        assert i18n._idioma_de(codigo) == "en"

    @pytest.mark.parametrize("codigo", ["", None, "C", "C.UTF-8", "POSIX", "fr_FR.UTF-8", "est_EE", "esperanto", "ensenada", "de_DE"])
    def test_lo_demas_no_dice_ni_espanol_ni_ingles(self, codigo):
        assert i18n._idioma_de(codigo) == ""


class TestIdiomaDelSistema:
    def test_sin_ninguna_pista_es_espanol(self):
        assert i18n.idioma_del_sistema() == "es"

    @pytest.mark.parametrize("variable", VARIABLES)
    def test_cualquiera_de_las_variables_vale(self, monkeypatch, variable):
        monkeypatch.setenv(variable, "en_US.UTF-8")
        assert i18n.idioma_del_sistema() == "en"

    def test_manda_la_primera_que_diga_un_idioma(self, monkeypatch):
        monkeypatch.setenv("LC_ALL", "en_GB.UTF-8")
        monkeypatch.setenv("LANG", "es_ES.UTF-8")
        assert i18n.idioma_del_sistema() == "en"

    def test_c_y_posix_no_cuentan_y_se_mira_la_siguiente(self, monkeypatch):
        monkeypatch.setenv("LC_ALL", "C.UTF-8")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        assert i18n.idioma_del_sistema() == "en"

    def test_un_sistema_en_c_no_se_toma_por_ingles(self, monkeypatch):
        """Python 3.9 llama `en_US` a la configuración «C»; con 3.12 no. Aquí da igual la versión: es el idioma por omisión."""
        monkeypatch.setenv("LANG", "C.UTF-8")
        monkeypatch.setattr(i18n.locale, "getlocale", lambda *a: ("en_US", "UTF-8"))
        assert i18n.idioma_del_sistema() == "es"

    def test_otro_idioma_cae_al_espanol(self, monkeypatch):
        monkeypatch.setenv("LANG", "fr_FR.UTF-8")
        assert i18n.idioma_del_sistema() == "es"

    def test_windows_usa_el_idioma_de_la_interfaz(self, monkeypatch):
        for lcid, esperado in ((1033, "en"), (3082, "es"), (1036, "es")):  # inglés, español, francés (cae al español)
            kernel32 = types.SimpleNamespace(GetUserDefaultUILanguage=lambda lcid=lcid: lcid)
            monkeypatch.setattr("ctypes.windll", types.SimpleNamespace(kernel32=kernel32), raising=False)
            monkeypatch.setattr(sys, "platform", "win32")
            assert i18n.idioma_del_sistema() == esperado

    def test_macos_lee_los_ajustes_del_sistema_porque_un_doble_clic_no_trae_lang(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        pedidos = []

        def defaults(orden, **kw):
            pedidos.append(orden)
            return types.SimpleNamespace(stdout='(\n    "en-GB",\n    "es-ES"\n)\n', stderr="", returncode=0)

        monkeypatch.setattr(i18n.subprocess, "run", defaults)
        assert i18n.idioma_del_sistema() == "en"
        assert pedidos == [["defaults", "read", "-g", "AppleLanguages"]]

    def test_macos_con_un_solo_idioma_sin_comillas(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(i18n.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="(\n    en,\n    fr\n)\n"))
        assert i18n.idioma_del_sistema() == "en"

    def test_macos_si_defaults_falla_se_mira_el_entorno_y_luego_el_espanol(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")

        def roto(*a, **k):
            raise subprocess.TimeoutExpired("defaults", 3)

        monkeypatch.setattr(i18n.subprocess, "run", roto)
        assert i18n.idioma_del_sistema() == "es"
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        assert i18n.idioma_del_sistema() == "en"


class TestFijarIdioma:
    def test_un_codigo_valido_se_usa_tal_cual(self):
        assert i18n.fijar_idioma("en") == "en" and i18n.idioma() == "en"
        assert i18n.fijar_idioma(" ES ") == "es"

    def test_vacio_o_desconocido_es_el_del_sistema(self, monkeypatch):
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        assert i18n.fijar_idioma("") == "en"
        assert i18n.fijar_idioma("klingon") == "en"
        assert i18n.fijar_idioma("en_US") == "en"

    def test_los_textos_siguen_al_idioma(self):
        i18n.fijar_idioma("en")
        assert i18n.t("send") == "Send"
        i18n.fijar_idioma("es")
        assert i18n.t("send") == "Enviar"

    def test_una_clave_que_falta_en_el_idioma_cae_al_espanol_y_si_no_a_la_clave(self, monkeypatch):
        monkeypatch.setitem(i18n.TEXTOS["es"], "solo_es", "Solo en español")
        i18n.fijar_idioma("en")
        assert i18n.t("solo_es") == "Solo en español"
        assert i18n.t("no_existe_en_ningun_idioma") == "no_existe_en_ningun_idioma"

    def test_los_valores_se_insertan_y_un_marcador_que_sobra_no_rompe(self):
        i18n.fijar_idioma("es")
        assert "7" in i18n.t("opt_about", v="7")
        assert i18n.t("opt_about") == i18n.TEXTOS["es"]["opt_about"]       # sin valores: el texto sin tocar
        assert i18n.t("opt_about", otro="x") == i18n.TEXTOS["es"]["opt_about"]  # marcador que falta: no lanza
