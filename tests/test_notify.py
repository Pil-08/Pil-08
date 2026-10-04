"""Avisos por escritorio y Telegram. Lo crítico: el token nunca sale en un log ni en un mensaje de error."""

import io
import json
import logging
import urllib.error

import pytest

from claude_batch_monitor import notify
from claude_batch_monitor.notify import (
    ErrorDeTelegram,
    credenciales_telegram,
    enviar_telegram,
    notificar,
    notificar_escritorio,
    probar_telegram,
)
from claude_batch_monitor.state import KEY_TELEGRAM_CHAT_ID, KEY_TELEGRAM_TOKEN, SecureStorage

TOKEN = "123456:SECRETO-del-bot"


class Respuesta:
    def __init__(self, cuerpo: dict):
        self._cuerpo = json.dumps(cuerpo).encode("utf-8")

    def read(self):
        return self._cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FalsoUrlopen:
    """Sustituye a `urllib.request.urlopen`: guarda las peticiones y responde según un guion."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.peticiones = []

    def __call__(self, peticion, timeout=None):
        self.peticiones.append(peticion)
        r = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch):
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)


def instalar(monkeypatch, *respuestas):
    falso = FalsoUrlopen(*respuestas)
    monkeypatch.setattr(notify.urllib.request, "urlopen", falso)
    return falso


class TestCredenciales:
    def test_sin_nada_no_hay_credenciales(self):
        assert credenciales_telegram() == ("", "")

    def test_el_entorno_manda_sobre_el_llavero(self, monkeypatch, llavero):
        SecureStorage.set(KEY_TELEGRAM_TOKEN, "del-llavero")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "del-entorno")
        assert credenciales_telegram()[0] == "del-entorno"

    def test_el_chat_id_sale_de_entorno_luego_config_luego_llavero(self, monkeypatch, llavero):
        SecureStorage.set(KEY_TELEGRAM_CHAT_ID, "llavero")
        assert credenciales_telegram()[1] == "llavero"
        assert credenciales_telegram("config")[1] == "config"
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "entorno")
        assert credenciales_telegram("config")[1] == "entorno"

    def test_se_quitan_los_espacios(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f"  {TOKEN}\n")
        assert credenciales_telegram(" 42 ") == (TOKEN, "42")


class TestEnviar:
    def test_sin_credenciales_no_toca_la_red(self, monkeypatch):
        falso = instalar(monkeypatch, Respuesta({"ok": True}))
        assert enviar_telegram("t", "m") is False
        assert falso.peticiones == []

    def test_envia_con_html_escapado(self, monkeypatch):
        falso = instalar(monkeypatch, Respuesta({"ok": True}))
        assert enviar_telegram("a<b>", "x & y", token=TOKEN, chat_id="42") is True
        carga = json.loads(falso.peticiones[0].data)
        assert carga["chat_id"] == "42" and carga["parse_mode"] == "HTML"
        assert "a&lt;b&gt;" in carga["text"] and "x &amp; y" in carga["text"]
        assert falso.peticiones[0].full_url.endswith("/sendMessage")

    def test_reintenta_y_acaba_devolviendo_false(self, monkeypatch):
        falso = instalar(monkeypatch, OSError("sin red"))
        assert enviar_telegram("t", "m", token=TOKEN, chat_id="1", reintentos=3) is False
        assert len(falso.peticiones) == 3

    def test_un_reintento_puede_salvar_el_envio(self, monkeypatch):
        falso = instalar(monkeypatch, OSError("fallo"), Respuesta({"ok": True}))
        assert enviar_telegram("t", "m", token=TOKEN, chat_id="1") is True
        assert len(falso.peticiones) == 2

    def test_si_telegram_dice_ok_false_cuenta_como_fallo(self, monkeypatch):
        instalar(monkeypatch, Respuesta({"ok": False, "description": "chat not found"}))
        assert enviar_telegram("t", "m", token=TOKEN, chat_id="1", reintentos=1) is False

    def test_el_token_nunca_aparece_en_el_log(self, monkeypatch, caplog):
        # El texto de un error de urllib puede llevar la URL completa, que contiene el token.
        instalar(monkeypatch, urllib.error.URLError(f"https://api.telegram.org/bot{TOKEN}/sendMessage"))
        with caplog.at_level(logging.DEBUG):
            enviar_telegram("t", "m", token=TOKEN, chat_id="1", reintentos=2)
        assert caplog.records and "SECRETO" not in caplog.text and TOKEN not in caplog.text


class TestEscritorio:
    def test_sin_plyer_devuelve_false(self, monkeypatch):
        monkeypatch.setattr(notify, "HAS_PLYER", False)
        assert notificar_escritorio("t", "m") is False

    def test_con_plyer_recorta_el_mensaje(self, monkeypatch):
        llamadas = []

        class Falso:
            @staticmethod
            def notify(**kw):
                llamadas.append(kw)

        monkeypatch.setattr(notify, "HAS_PLYER", True)
        monkeypatch.setattr(notify, "_plyer_notification", Falso)
        assert notificar_escritorio("Título", "x" * 400) is True
        assert len(llamadas[0]["message"]) == 250 and llamadas[0]["title"] == "Título"

    def test_si_plyer_revienta_no_se_propaga(self, monkeypatch):
        class Roto:
            @staticmethod
            def notify(**kw):
                raise RuntimeError("sin servicio de notificaciones")

        monkeypatch.setattr(notify, "HAS_PLYER", True)
        monkeypatch.setattr(notify, "_plyer_notification", Roto)
        assert notificar_escritorio("t", "m") is False

    def test_notificar_avisa_por_los_dos_canales(self, monkeypatch):
        vistos = []
        monkeypatch.setattr(notify, "notificar_escritorio", lambda t, m: vistos.append(("escritorio", t)))
        hecho = []

        class HiloSincrono:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)
                hecho.append(True)

        monkeypatch.setattr(notify.threading, "Thread", HiloSincrono)
        monkeypatch.setattr(notify, "enviar_telegram", lambda t, m, c: vistos.append(("telegram", t)))
        notificar("Fin", "ok", "42")
        assert vistos == [("escritorio", "Fin"), ("telegram", "Fin")] and hecho


class TestProbarTelegram:
    def test_faltan_datos(self):
        with pytest.raises(ErrorDeTelegram, match="Faltan"):
            probar_telegram("", "42")
        with pytest.raises(ErrorDeTelegram, match="Faltan"):
            probar_telegram(TOKEN, "  ")

    def test_funciona(self, monkeypatch):
        falso = instalar(monkeypatch, Respuesta({"ok": True}))
        probar_telegram(f" {TOKEN} ", " 42 ")
        assert json.loads(falso.peticiones[0].data)["chat_id"] == "42"
        assert TOKEN in falso.peticiones[0].full_url      # el token SÍ va en la URL de la API…

    def test_el_motivo_de_telegram_se_explica(self, monkeypatch):
        cuerpo = io.BytesIO(json.dumps({"ok": False, "description": "Unauthorized"}).encode())
        instalar(monkeypatch, urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, cuerpo))
        with pytest.raises(ErrorDeTelegram) as e:
            probar_telegram(TOKEN, "42")
        assert str(e.value) == "Unauthorized"

    def test_http_sin_cuerpo_util(self, monkeypatch):
        instalar(monkeypatch, urllib.error.HTTPError("https://x", 502, "Bad Gateway", {}, io.BytesIO(b"<html>")))
        with pytest.raises(ErrorDeTelegram, match="502"):
            probar_telegram(TOKEN, "42")

    def test_sin_red_no_filtra_el_token(self, monkeypatch):
        instalar(monkeypatch, urllib.error.URLError(f"fallo con https://api.telegram.org/bot{TOKEN}/sendMessage"))
        with pytest.raises(ErrorDeTelegram) as e:
            probar_telegram(TOKEN, "42")
        assert "SECRETO" not in str(e.value) and "URLError" in str(e.value)

    def test_ok_false(self, monkeypatch):
        instalar(monkeypatch, Respuesta({"ok": False, "description": "chat not found"}))
        with pytest.raises(ErrorDeTelegram, match="chat not found"):
            probar_telegram(TOKEN, "42")

    def test_ok_false_sin_descripcion(self, monkeypatch):
        instalar(monkeypatch, Respuesta({"ok": False}))
        with pytest.raises(ErrorDeTelegram, match="rechazó"):
            probar_telegram(TOKEN, "42")
