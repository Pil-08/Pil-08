"""Catálogo de precios y cálculo de coste.

Las cifras de estos tests salen de la tabla oficial de precios (oct. 2026); si
Anthropic cambia un precio, el test que falla te dice cuál actualizar en el catálogo.
"""

from decimal import Decimal as D

import pytest

from claude_batch_monitor.pricing import (
    CATALOGO,
    Consumo,
    buscar_modelo,
    coste_maximo,
    coste_uso,
    estimar_tokens,
    formatear_precio,
    formatear_usd,
    leer_consumo,
    normalizar_id,
    resolver_precios,
    soporta_esfuerzo,
)


class Uso:
    def __init__(self, entrada=0, salida=0, escritura=0, lectura=0, w5=None, w1=None):
        self.input_tokens = entrada
        self.output_tokens = salida
        self.cache_creation_input_tokens = escritura
        self.cache_read_input_tokens = lectura
        if w5 is not None or w1 is not None:
            class Desglose:
                ephemeral_5m_input_tokens = w5 or 0
                ephemeral_1h_input_tokens = w1 or 0
            self.cache_creation = Desglose()


class TestNormalizacion:
    @pytest.mark.parametrize(
        "entrada,esperado",
        [
            ("claude-opus-4-5-20251101", "claude-opus-4-5"),
            ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
            ("claude-sonnet-4-20250514", "claude-sonnet-4"),
            ("claude-3-5-haiku-20241022", "claude-haiku-3-5"),
            ("CLAUDE-SONNET-5-5", "claude-sonnet-5-5"),
            ("claude-opus-4-0", "claude-opus-4"),
            ("claude-sonnet-5-5-latest", "claude-sonnet-5-5"),
            ("  claude-haiku-4-5  ", "claude-haiku-4-5"),
        ],
    )
    def test_ids_con_fecha_y_alias(self, entrada, esperado):
        assert normalizar_id(entrada) == esperado

    def test_los_ids_con_fecha_encuentran_su_modelo(self):
        assert buscar_modelo("claude-haiku-4-5-20251001").familia == "haiku"


class TestPreciosDelCatalogo:
    # Tabla oficial: (id, entrada, salida) en USD por millón de tokens.
    @pytest.mark.parametrize(
        "modelo,entrada,salida",
        [
            ("claude-fable-5-1", "10", "50"),
            ("claude-opus-5-5", "4", "20"),
            ("claude-opus-5", "5", "25"),
            ("claude-opus-4-8", "5", "25"),
            ("claude-opus-4-1", "15", "75"),
            ("claude-sonnet-5-5", "2", "10"),
            ("claude-sonnet-5", "2", "10"),
            ("claude-sonnet-4-6", "3", "15"),
            ("claude-haiku-4-5", "1", "5"),
            ("claude-haiku-3-5", "0.8", "4"),
        ],
    )
    def test_precios_oficiales(self, modelo, entrada, salida):
        p = resolver_precios(modelo)
        assert (p.entrada, p.salida) == (D(entrada), D(salida))
        assert p.origen == "catalogo" and p.fiable

    def test_multiplicador_de_lectura_de_cache_por_modelo(self):
        assert resolver_precios("claude-sonnet-5-5").lectura_cache == D("0.1")
        assert resolver_precios("claude-opus-5-5").lectura_cache == D("0.05")
        assert resolver_precios("claude-fable-5-1").lectura_cache == D("0.025")

    def test_todos_los_modelos_tienen_precio_positivo(self):
        for m in CATALOGO.values():
            assert m.entrada > 0 and m.salida > m.entrada


class TestModelosDesconocidos:
    def test_familia_conocida_usa_el_precio_de_su_familia_y_avisa(self):
        p = resolver_precios("claude-opus-9")
        assert p.origen == "familia" and not p.fiable
        assert (p.entrada, p.salida) == (D(5), D(25))
        assert "claude-opus-9" in p.aviso()

    def test_modelo_totalmente_desconocido_asume_el_precio_mas_caro(self):
        # Un tope calculado con precios demasiado bajos protege menos de lo que promete.
        p = resolver_precios("gpt-4")
        assert p.origen == "desconocido"
        assert (p.entrada, p.salida) == (D(10), D(50))
        assert p.aviso()

    def test_precio_manual_manda_sobre_el_catalogo(self):
        p = resolver_precios("claude-haiku-4-5", D("7"), D("9"))
        assert (p.entrada, p.salida, p.origen) == (D(7), D(9), "manual")

    def test_precio_manual_parcial_completa_con_el_catalogo(self):
        p = resolver_precios("claude-haiku-4-5", entrada=D("3"))
        assert (p.entrada, p.salida) == (D(3), D(5))


class TestCoste:
    def test_coste_simple(self):
        p = resolver_precios("claude-sonnet-5-5")  # 2 / 10
        assert coste_uso(Uso(entrada=1000, salida=500), p) == D("0.007")

    def test_modo_lote_cuesta_la_mitad(self):
        p = resolver_precios("claude-sonnet-5-5")
        assert coste_uso(Uso(entrada=1000, salida=500), p, lote=True) == D("0.0035")

    def test_cache_se_acumula_con_el_descuento_de_lote(self):
        p = resolver_precios("claude-haiku-4-5")
        u = Uso(entrada=100, salida=50, escritura=1000, lectura=2000)
        assert coste_uso(u, p) == D("0.0018")
        assert coste_uso(u, p, lote=True) == D("0.0009")

    def test_escritura_de_cache_de_1_hora_cuesta_el_doble_de_la_entrada(self):
        p = resolver_precios("claude-haiku-4-5")
        u = Uso(escritura=1000, w5=800, w1=200)
        # 800 x 1.25 + 200 x 2 = 1400 tokens-equivalentes a $1/M
        assert coste_uso(u, p) == D("0.0014")

    def test_escritura_sin_desglose_se_asume_de_5_minutos(self):
        p = resolver_precios("claude-haiku-4-5")
        assert coste_uso(Uso(escritura=1000), p) == D("0.00125")

    def test_lectura_de_cache_en_opus_5_5_es_un_5_por_ciento(self):
        p = resolver_precios("claude-opus-5-5")
        assert coste_uso(Uso(lectura=1_000_000), p) == D("0.2")

    def test_lectura_de_cache_en_fable_5_1_es_un_2_5_por_ciento(self):
        p = resolver_precios("claude-fable-5-1")
        assert coste_uso(Uso(lectura=1_000_000), p) == D("0.25")

    def test_decimal_no_acumula_ruido_binario(self):
        # Con float, 10_000 sumas de 0.0007 se desvían; con Decimal no.
        p = resolver_precios("claude-haiku-4-5")
        un = coste_uso(Uso(entrada=100, salida=120), p)  # 0.0007
        assert sum([un] * 10_000, D(0)) == D("7")

    def test_usage_incompleto_o_basura_no_revienta(self):
        p = resolver_precios("claude-haiku-4-5")

        class Raro:
            input_tokens = "mucho"
            output_tokens = None

        assert coste_uso(Raro(), p) == 0
        assert coste_uso(None, p) == 0
        assert leer_consumo(object()) == Consumo()

    @pytest.mark.parametrize("valor,esperado", [
        ("7", 7), (" 12 ", 12), ("007", 7), ("abc", 0), ("-3", 0), ("1.5", 0), ("", 0), (None, 0), (True, 0), ([], 0),
        ("²", 0), ("٣", 3),          # «²» pasa por isdigit() pero no es un número; «٣» sí (un dígito arábigo-índico)
        (-5, 0), (3.9, 3), (float("nan"), 0), (float("inf"), 0), (float("-inf"), 0),
        (D("8"), 8), (D("NaN"), 0), (D("Infinity"), 0), (10**15, 10**15),
    ])
    def test_los_contadores_raros_se_leen_sin_reventar(self, valor, esperado):
        class Uso:
            output_tokens = valor

        assert leer_consumo(Uso()).salida == esperado

    def test_coste_maximo_es_entrada_mas_max_tokens_de_salida(self):
        p = resolver_precios("claude-haiku-4-5")  # 1 / 5
        assert coste_maximo(1000, 16_000, p) == D("0.081")
        assert coste_maximo(1000, 16_000, p, lote=True) == D("0.0405")

    def test_coste_maximo_con_escritura_de_cache(self):
        p = resolver_precios("claude-haiku-4-5")
        assert coste_maximo(1000, 0, p, mult_entrada=D("1.25")) == D("0.00125")


class TestEstimacionLocal:
    def test_es_cota_superior_del_ingles(self):
        texto = "The quick brown fox jumps over the lazy dog. " * 40
        # Regla clásica: ~4 caracteres/token. Nuestra cota debe ser mayor.
        assert estimar_tokens(texto) > len(texto) / 4

    def test_texto_no_ascii_cuenta_un_token_por_caracter(self):
        assert estimar_tokens("日本語のテキスト") >= 8

    def test_vacio(self):
        assert estimar_tokens("") == 0


class TestEsfuerzo:
    def test_haiku_45_no_admite_esfuerzo(self):
        assert soporta_esfuerzo("claude-haiku-4-5") is False

    def test_sonnet_55_admite_los_cinco_niveles(self):
        for nivel in ("low", "medium", "high", "xhigh", "max"):
            assert soporta_esfuerzo("claude-sonnet-5-5", nivel) is True

    def test_xhigh_no_existe_en_sonnet_46_ni_opus_46(self):
        assert soporta_esfuerzo("claude-sonnet-4-6", "xhigh") is False
        assert soporta_esfuerzo("claude-opus-4-6", "xhigh") is False
        assert soporta_esfuerzo("claude-sonnet-4-6", "max") is True

    def test_modelo_desconocido_devuelve_none(self):
        assert soporta_esfuerzo("modelo-raro") is None


class TestFormato:
    def test_usd(self):
        assert formatear_usd(D("0.0035")) == "$0.0035"
        assert formatear_usd(D("12.5"), 2) == "$12.50"

    def test_precio(self):
        assert formatear_precio(D("2")) == "$2"
        assert formatear_precio(D("0.8")) == "$0.80"
        assert formatear_precio(D("12.5")) == "$12.50"
