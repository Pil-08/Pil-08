import json
from decimal import Decimal as D

from claude_batch_monitor import paths, state
from claude_batch_monitor.state import (
    KEY_API,
    EstadoMonitor,
    SecureStorage,
    obtener_api_key,
    origen_api_key,
)


class TestEstado:
    def test_ida_y_vuelta(self):
        e = EstadoMonitor(exitos=3, current_job_id="ab12", gasto_acumulado_usd=D("1.2345"),
                          tokens_input_total=10, tokens_output_total=20)
        e.guardar()
        assert EstadoMonitor.cargar() == e

    def test_sin_fichero(self):
        assert EstadoMonitor.cargar() == EstadoMonitor()

    def test_estado_de_la_v11_con_floats(self):
        paths.STATE_FILE.write_text(
            json.dumps({"exitos": 2, "gasto_acumulado_usd": 0.1234, "current_job_id": "x"}),
            encoding="utf-8",
        )
        e = EstadoMonitor.cargar()
        assert e.gasto_acumulado_usd == D("0.1234")
        assert e.current_job_id == "x"

    def test_corrupto_se_aparta(self):
        paths.STATE_FILE.write_text("{{{", encoding="utf-8")
        assert EstadoMonitor.cargar() == EstadoMonitor()
        assert paths.STATE_FILE.with_suffix(".corrupt.json").exists()

    def test_valores_absurdos_se_sanean(self):
        paths.STATE_FILE.write_text(
            json.dumps({"exitos": -5, "gasto_acumulado_usd": "-3", "tokens_input_total": -1}),
            encoding="utf-8",
        )
        e = EstadoMonitor.cargar()
        assert e.exitos == 0 and e.gasto_acumulado_usd == 0 and e.tokens_input_total == 0


class TestClaveApi:
    def test_sin_nada(self):
        assert obtener_api_key() is None
        assert origen_api_key() == ""

    def test_desde_el_entorno(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-ant-entorno  ")
        assert obtener_api_key() == "sk-ant-entorno"
        assert origen_api_key() == "entorno"

    def test_desde_el_llavero(self, llavero):
        assert SecureStorage.set(KEY_API, "sk-ant-llavero")
        assert obtener_api_key() == "sk-ant-llavero"
        assert origen_api_key() == "llavero"

    def test_el_entorno_gana_al_llavero(self, llavero, monkeypatch):
        SecureStorage.set(KEY_API, "del-llavero")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "del-entorno")
        assert obtener_api_key() == "del-entorno"

    def test_sin_keyring_no_se_puede_guardar(self):
        assert not state.HAS_KEYRING
        assert SecureStorage.set(KEY_API, "x") is False
        assert SecureStorage.get(KEY_API) is None
        assert not SecureStorage.disponible()

    def test_borrar(self, llavero):
        SecureStorage.set(KEY_API, "x")
        assert SecureStorage.delete(KEY_API)
        assert SecureStorage.get(KEY_API) is None
        assert SecureStorage.delete(KEY_API) is False  # ya no existe: no revienta

    def test_valor_vacio_no_se_guarda(self, llavero):
        assert SecureStorage.set(KEY_API, "") is False
