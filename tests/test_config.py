import json
from decimal import Decimal as D

import pytest

from claude_batch_monitor import paths
from claude_batch_monitor.config import TEMAS, ConfigManager, Configuracion, a_decimal


class TestDecimal:
    def test_float_se_convierte_sin_ruido_binario(self):
        assert a_decimal(0.1, D(0)) == D("0.1")
        assert a_decimal("2,5", D(0)) == D("2.5")  # coma decimal española

    def test_basura_devuelve_el_defecto(self):
        assert a_decimal("abc", D(7)) == D(7)
        assert a_decimal(None, D(7)) == D(7)
        assert a_decimal(True, D(7)) == D(7)
        assert a_decimal("NaN", D(7)) == D(7)
        assert a_decimal("Infinity", D(7)) == D(7)


class TestValidacion:
    def test_configuracion_por_defecto_es_valida(self):
        v = Configuracion().validar()
        assert v.ok, v.errores

    def test_max_tokens_por_encima_del_maximo_del_modelo(self):
        c = Configuracion(modelo="claude-haiku-4-5", max_tokens=100_000)
        v = c.validar()
        assert not v.ok
        assert "64,000" in v.errores[0]

    def test_esfuerzo_desconocido(self):
        assert not Configuracion(esfuerzo="ultra").validar().ok

    def test_esfuerzo_no_soportado_solo_avisa(self):
        v = Configuracion(modelo="claude-haiku-4-5", esfuerzo="high").validar()
        assert v.ok
        assert any("no admite" in a for a in v.avisos)

    def test_modelo_desconocido_avisa_del_precio(self):
        v = Configuracion(modelo="modelo-inventado").validar()
        assert any("modelo-inventado" in a for a in v.avisos)

    def test_sin_tope_avisa(self):
        v = Configuracion(tope_gasto_usd=D(0)).validar()
        assert any("Sin tope" in a for a in v.avisos)

    def test_modo_desconocido(self):
        assert not Configuracion(modo="turbo").validar().ok

    def test_tope_negativo(self):
        assert not Configuracion(tope_gasto_usd=D(-1)).validar().ok

    def test_modelo_retirado_avisa(self):
        v = Configuracion(modelo="claude-opus-4-1").validar()
        assert any("retirad" in a for a in v.avisos)

    def test_bucle_avisa_de_que_gasta(self):
        v = Configuracion(modo_continuo=True).validar()
        assert any("bucle" in a.lower() for a in v.avisos)


class TestPrecios:
    def test_sin_precios_manuales_usa_el_catalogo(self):
        p = Configuracion(modelo="claude-sonnet-5-5").precios()
        assert (p.entrada, p.salida) == (D(2), D(10))

    def test_precios_manuales_mandan(self):
        c = Configuracion(modelo="claude-sonnet-5-5", precio_input_mtok=D(9), precio_output_mtok=D(9))
        assert c.precios().origen == "manual"


class TestStreaming:
    def test_por_encima_de_16000_tokens_se_usa_streaming(self):
        assert not Configuracion(max_tokens=16_000).usa_streaming
        assert Configuracion(max_tokens=16_001).usa_streaming

    def test_se_puede_forzar(self):
        assert Configuracion(streaming=True).usa_streaming


class TestPersistencia:
    def test_ida_y_vuelta_conserva_todo(self):
        original = Configuracion(
            mensaje="a\n---\nb",
            modelo="claude-opus-5-5",
            modo="lote",
            system_prompt="Eres breve.",
            esfuerzo="high",
            max_tokens=8000,
            concurrencia=4,
            tope_gasto_usd=D("2.50"),
            precio_input_mtok=D("4"),
            hora_limite="22:30",
            interfaz="avanzada",
        )
        ConfigManager.guardar(original)
        assert ConfigManager.cargar() == original

    def test_el_dinero_se_guarda_como_texto(self):
        ConfigManager.guardar(Configuracion(tope_gasto_usd=D("0.1")))
        datos = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
        assert datos["tope_gasto_usd"] == "0.1"

    def test_la_api_key_nunca_se_escribe(self):
        ConfigManager.guardar(Configuracion())
        texto = paths.CONFIG_FILE.read_text(encoding="utf-8").lower()
        assert "api_key" not in texto and "sk-ant" not in texto

    def test_sin_fichero_da_los_valores_por_defecto(self):
        assert ConfigManager.cargar() == Configuracion()

    def test_json_corrupto_se_aparta_y_no_revienta(self):
        paths.CONFIG_FILE.write_text("{esto no es json", encoding="utf-8")
        assert ConfigManager.cargar() == Configuracion()
        assert paths.CONFIG_FILE.with_suffix(".corrupt.json").exists()

    def test_json_que_no_es_un_objeto(self):
        paths.CONFIG_FILE.write_text("[1, 2]", encoding="utf-8")
        assert ConfigManager.cargar() == Configuracion()

    def test_carga_el_config_json_de_la_v11(self):
        # Formato exacto que escribía la v11: floats, sin modo, sin system prompt.
        v11 = {
            "mensaje": "hola\n---\nadiós",
            "modelo": "claude-haiku-4-5-20251001",
            "separador": "---",
            "telegram_chat_id": "123",
            "modo_continuo": False,
            "hora_limite": "23:59",
            "pausa_ciclos_sec": 10,
            "pausa_entre_mensajes_sec": 3,
            "auto_arranque": False,
            "timeout_api_sec": 120,
            "max_error_attempts": 8,
            "tope_gasto_usd": 5.0,
            "precio_input_mtok": 1.0,
            "precio_output_mtok": 5.0,
            "reset_gasto_por_lote": True,
        }
        paths.CONFIG_FILE.write_text(json.dumps(v11), encoding="utf-8")
        c = ConfigManager.cargar()
        assert c.tope_gasto_usd == D("5.0")
        assert c.precio_input_mtok == D("1.0")
        assert c.modo == "inmediato" and c.system_prompt == ""
        assert c.hora_limite == "23:59"
        assert c.telegram_chat_id == "123"

    def test_el_formato_de_las_respuestas_esta_activado_salvo_que_se_diga_otra_cosa(self):
        assert Configuracion().mostrar_formato is True
        assert Configuracion.desde_dict({}).mostrar_formato is True
        assert Configuracion.desde_dict({"mostrar_formato": False}).mostrar_formato is False

    def test_los_valores_fuera_de_rango_se_recortan(self):
        paths.CONFIG_FILE.write_text(
            json.dumps(
                {
                    "pausa_ciclos_sec": 999999,
                    "timeout_api_sec": 1,
                    "max_error_attempts": 0,
                    "concurrencia": 500,
                    "tope_gasto_usd": -3,
                    "modo": "turbo",
                    "interfaz": "rara",
                    "esfuerzo": "ultra",
                    "precio_input_mtok": "abc",
                }
            ),
            encoding="utf-8",
        )
        c = ConfigManager.cargar()
        assert c.pausa_ciclos_sec == 3600
        assert c.timeout_api_sec == 15
        assert c.max_error_attempts == 1
        assert c.concurrencia == 32
        assert c.tope_gasto_usd == 0
        assert c.modo == "inmediato"
        assert c.interfaz == "simple"
        assert c.esfuerzo == ""
        assert c.precio_input_mtok is None


class TestTema:
    def test_por_omision_sigue_al_sistema(self):
        assert Configuracion().tema == "auto" and TEMAS == ("auto", "claro", "oscuro")

    def test_se_guarda_y_se_lee(self):
        assert Configuracion.desde_dict(Configuracion(tema="oscuro").a_dict()).tema == "oscuro"

    @pytest.mark.parametrize("valor", ["OSCURO", " Oscuro "])
    def test_mayusculas_y_espacios_no_importan(self, valor):
        assert Configuracion.desde_dict({"tema": valor}).tema == "oscuro"

    @pytest.mark.parametrize("valor", ["rosa", "", None, 3, ["oscuro"]])
    def test_un_valor_que_no_existe_vuelve_a_auto(self, valor):
        assert Configuracion.desde_dict({"tema": valor}).tema == "auto"

    def test_una_configuracion_antigua_sin_tema_sigue_funcionando(self):
        assert Configuracion.desde_dict({"modelo": "claude-sonnet-5-5"}).tema == "auto"
