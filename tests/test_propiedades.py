"""Pruebas de propiedades: en vez de unos pocos ejemplos, cientos de entradas al azar (y raras) por cada regla.

Una propiedad es algo que debe cumplirse SIEMPRE: «partir y volver a unir no pierde nada», «el coste nunca es negativo»,
«ninguna entrada hace reventar el lector de configuración». Si una falla, hypothesis enseña el caso mínimo que la rompe.
"""

from __future__ import annotations

import csv
import json
import math
import re
import tempfile
from decimal import Decimal as D
from pathlib import Path

import pytest

pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, assume, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from claude_batch_monitor import paths  # noqa: E402
from claude_batch_monitor.config import ConfigManager, Configuracion, a_decimal  # noqa: E402
from claude_batch_monitor.core.errors import (  # noqa: E402
    Categoria,
    analizar_error,
    es_corte_de_conexion,
    es_respuesta_ilegible,
    es_timeout,
    pudo_haberse_procesado,
)
from claude_batch_monitor.export import exportar, neutralizar_formula  # noqa: E402
from claude_batch_monitor.gui.formato import analizar_formato  # noqa: E402
from claude_batch_monitor.inputs import aplicar_plantilla, leer_ordenes  # noqa: E402
from claude_batch_monitor.job import STATUS_COMPLETED, Job  # noqa: E402
from claude_batch_monitor.parsing import limpiar_sustitutos, split_messages  # noqa: E402
from claude_batch_monitor.pricing import (  # noqa: E402
    Consumo,
    Precios,
    coste_consumo,
    coste_maximo,
    estimar_tokens,
    formatear_precio,
    formatear_usd,
    leer_consumo,
)
from claude_batch_monitor.ratelimit import parse_reset  # noqa: E402

from .test_errors import error as error_http  # noqa: E402

AJUSTES = settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
settings.register_profile("cbm", AJUSTES)
settings.load_profile("cbm")

# Texto de todo tipo (sin sustitutos sueltos, que no son texto válido); aparte se prueban los sustitutos.
TEXTO = st.text(max_size=200)
TEXTO_NO_VACIO = TEXTO.filter(lambda t: t.strip() != "")
CON_SUSTITUTOS = st.text(
    alphabet=st.one_of(st.characters(), st.sampled_from(["\ud800", "\ud83d", "\udc00", "\udfff"])), min_size=1, max_size=60,
)
LINEA_SEPARADORA = re.compile(r"(?m)^[ \t]*---[ \t]*$")


# ----------------------------------------------------------------------
# Trocear y unir
# ----------------------------------------------------------------------


class TestParsing:
    @given(st.lists(TEXTO_NO_VACIO.filter(lambda t: not LINEA_SEPARADORA.search(t)), min_size=1, max_size=8))
    def test_unir_con_el_separador_y_volver_a_partir_no_pierde_nada(self, bloques):
        texto = "\n---\n".join(bloques)
        esperado = [b.replace("\r\n", "\n").replace("\r", "\n").strip() for b in bloques]   # (los saltos se unifican)
        assert split_messages(texto) == esperado

    @given(TEXTO)
    def test_ningun_trozo_esta_vacio_ni_lleva_espacios_en_los_bordes(self, texto):
        for trozo in split_messages(texto):
            assert trozo and trozo == trozo.strip()

    @given(TEXTO, st.text(max_size=5))
    def test_cualquier_separador_funciona_o_se_usa_el_de_siempre(self, texto, separador):
        resultado = split_messages(texto, separador)
        assert all(isinstance(t, str) and t for t in resultado)

    @given(TEXTO)
    def test_no_hay_ninguna_linea_separadora_dentro_de_un_trozo(self, texto):
        for trozo in split_messages(texto):
            assert not LINEA_SEPARADORA.search(trozo)

    def test_los_saltos_de_linea_de_windows_tambien_parten(self):
        assert split_messages("uno\r\n---\r\ndos") == ["uno", "dos"]
        assert split_messages("uno\r---\rdos") == ["uno", "dos"]


# ----------------------------------------------------------------------
# Dinero
# ----------------------------------------------------------------------

TOKENS = st.integers(min_value=0, max_value=10**11)
PRECIO = st.decimals(min_value=D("0.001"), max_value=D("1000"), places=3, allow_nan=False, allow_infinity=False)


@st.composite
def precios_al_azar(draw):
    return Precios(entrada=draw(PRECIO), salida=draw(PRECIO), lectura_cache=draw(st.sampled_from([D('0.1'), D('0.5')])),
                   origen='manual')


@st.composite
def consumos(draw):
    return Consumo(entrada=draw(TOKENS), salida=draw(TOKENS), escritura_5m=draw(TOKENS), escritura_1h=draw(TOKENS),
                   lectura_cache=draw(TOKENS))


def sumar(a: Consumo, b: Consumo) -> Consumo:
    return Consumo(a.entrada + b.entrada, a.salida + b.salida, a.escritura_5m + b.escritura_5m,
                   a.escritura_1h + b.escritura_1h, a.lectura_cache + b.lectura_cache)


class TestDinero:
    @given(consumos(), precios_al_azar())
    def test_el_coste_nunca_es_negativo(self, consumo, precios):
        assert coste_consumo(consumo, precios) >= 0

    @given(consumos(), consumos(), precios_al_azar())
    def test_el_coste_es_aditivo_y_exacto(self, a, b, precios):
        assert coste_consumo(sumar(a, b), precios) == coste_consumo(a, precios) + coste_consumo(b, precios)

    @given(consumos(), precios_al_azar())
    def test_el_lote_cuesta_exactamente_la_mitad(self, consumo, precios):
        assert coste_consumo(consumo, precios, lote=True) * 2 == coste_consumo(consumo, precios)

    @given(consumos(), TOKENS, precios_al_azar())
    def test_mas_tokens_nunca_cuestan_menos(self, consumo, extra, precios):
        mas = Consumo(consumo.entrada + extra, consumo.salida + extra, consumo.escritura_5m, consumo.escritura_1h,
                      consumo.lectura_cache)
        assert coste_consumo(mas, precios) >= coste_consumo(consumo, precios)

    @given(st.integers(0, 10**7), st.integers(1, 10**6), st.data(), precios_al_azar(), st.booleans())
    def test_el_peor_caso_nunca_es_menor_que_lo_que_se_puede_gastar(self, entrada, max_tokens, datos, precios, lote):
        """Si la respuesta usa como mucho `max_tokens` y la entrada es la estimada, el coste real cabe en el peor caso."""
        real = Consumo(entrada=datos.draw(st.integers(0, entrada)), salida=datos.draw(st.integers(0, max_tokens)))
        assert coste_consumo(real, precios, lote) <= coste_maximo(entrada, max_tokens, precios, lote)

    @given(st.one_of(st.none(), st.integers(), st.floats(), st.text(max_size=10), st.lists(st.integers(), max_size=3),
                     st.decimals()))
    def test_un_contador_raro_se_lee_sin_reventar_y_nunca_es_negativo(self, valor):
        class Uso:
            input_tokens = valor
            output_tokens = valor
            cache_read_input_tokens = valor

        consumo = leer_consumo(Uso())
        assert consumo.entrada >= 0 and consumo.salida >= 0 and consumo.lectura_cache >= 0

    @given(TEXTO)
    def test_estimar_tokens_es_una_cota_superior_de_lo_razonable(self, texto):
        n = estimar_tokens(texto)
        assert n >= 0
        if texto:
            assert n >= len(texto) // 4        # nunca por debajo de la regla clásica de 4 caracteres por token

    @given(st.decimals(min_value=D("-1e12"), max_value=D("1e12"), allow_nan=False, allow_infinity=False),
           st.integers(0, 8))
    def test_dar_formato_a_un_importe_siempre_funciona(self, valor, decimales):
        assert re.fullmatch(r"\$-?\d+(\.\d+)?", formatear_usd(valor, decimales))
        assert re.fullmatch(r"\$-?\d+(\.\d+)?", formatear_precio(valor))

    @given(st.one_of(st.none(), st.booleans(), st.integers(), st.floats(), st.text(max_size=20), st.just("1,5")))
    def test_a_decimal_nunca_revienta_y_siempre_es_finito(self, valor):
        resultado = a_decimal(valor, D(7))
        assert isinstance(resultado, D) and resultado.is_finite()


# ----------------------------------------------------------------------
# Leer órdenes
# ----------------------------------------------------------------------


class TestEntradas:
    @given(st.lists(TEXTO_NO_VACIO.filter(lambda t: "\x00" not in t and "\r" not in t), min_size=1, max_size=10),
           st.sampled_from([",", ";", "\t"]))
    def test_un_csv_escrito_por_el_modulo_csv_se_lee_igual(self, celdas, delimitador):
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "datos.csv"
            with open(ruta, "w", encoding="utf-8", newline="") as f:
                escritor = csv.writer(f, delimiter=delimitador)
                escritor.writerow(["id", "texto"])
                for i, celda in enumerate(celdas):
                    escritor.writerow([i, celda])
            assert leer_ordenes(ruta, campo="texto") == [c.strip() for c in celdas]

    @given(st.lists(TEXTO_NO_VACIO.filter(lambda t: "\x00" not in t), min_size=1, max_size=10))
    def test_un_jsonl_se_lee_igual(self, textos):
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "datos.jsonl"
            ruta.write_text("\n".join(json.dumps({"input": t}, ensure_ascii=False) for t in textos), encoding="utf-8")
            assert leer_ordenes(ruta) == [t.strip() for t in textos]

    @given(TEXTO, TEXTO)
    def test_la_plantilla_siempre_contiene_la_orden(self, plantilla, orden):
        assert orden in aplicar_plantilla(plantilla, orden)

    @given(st.lists(TEXTO_NO_VACIO, min_size=1, max_size=6), st.integers(1, 10))
    def test_el_limite_corta_sin_reordenar(self, textos, limite):
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "datos.jsonl"
            ruta.write_text("\n".join(json.dumps({"input": t}) for t in textos), encoding="utf-8")
            completo = leer_ordenes(ruta)
            assert leer_ordenes(ruta, limite=limite) == completo[:limite]


# ----------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------

JSON_RARO = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(), st.text(max_size=12)),
    lambda hijos: st.one_of(st.lists(hijos, max_size=3), st.dictionaries(st.text(max_size=8), hijos, max_size=3)),
    max_leaves=6,
)
CLAVES_DE_CONFIG = [
    "mensaje", "plantilla", "modelo", "separador", "modo", "system_prompt", "usar_cache", "esfuerzo", "max_tokens",
    "concurrencia", "streaming", "tope_gasto_usd", "precio_input_mtok", "precio_output_mtok", "reset_gasto_por_lote",
    "estimar_antes_de_enviar", "modo_continuo", "hora_limite", "pausa_ciclos_sec", "pausa_entre_mensajes_sec",
    "timeout_api_sec", "max_error_attempts", "telegram_chat_id", "auto_arranque", "iniciar_minimizado",
    "retencion_dias", "interfaz", "mostrar_formato", "idioma", "tema",
]


class TestConfiguracion:
    @given(st.dictionaries(st.sampled_from(CLAVES_DE_CONFIG), JSON_RARO, max_size=12))
    def test_un_config_json_con_valores_raros_nunca_revienta_y_queda_valido(self, datos):
        config = Configuracion.desde_dict(datos)
        assert config.tope_gasto_usd >= 0 and config.tope_gasto_usd.is_finite()
        assert 1 <= config.max_tokens <= 1_000_000 and 1 <= config.concurrencia <= 32
        assert 15 <= config.timeout_api_sec <= 900 and 1 <= config.max_error_attempts <= 100

    @given(st.dictionaries(st.sampled_from(CLAVES_DE_CONFIG), JSON_RARO, max_size=12))
    def test_guardar_y_volver_a_cargar_deja_la_misma_configuracion(self, datos):
        config = Configuracion.desde_dict(datos)
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "config.json"
            ConfigManager.guardar(config, ruta)
            assert ConfigManager.cargar(ruta) == config
            assert "api" not in ruta.read_text(encoding="utf-8").lower() or "sk-ant" not in ruta.read_text(encoding="utf-8")

    @given(st.binary(max_size=200))
    def test_un_fichero_de_configuracion_estropeado_no_impide_arrancar(self, basura):
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "config.json"
            ruta.write_bytes(basura)
            assert isinstance(ConfigManager.cargar(ruta), Configuracion)

    @given(st.floats(allow_nan=True, allow_infinity=True))
    def test_un_numero_infinito_o_nan_en_el_json_no_estropea_la_configuracion_entera(self, numero):
        config = Configuracion.desde_dict({"modelo": "claude-haiku-4-5", "max_tokens": numero, "tope_gasto_usd": numero,
                                           "concurrencia": numero})
        assert config.modelo == "claude-haiku-4-5"          # lo demás se conserva


# ----------------------------------------------------------------------
# Errores de la API y cabeceras
# ----------------------------------------------------------------------


class TestErrores:
    @given(st.integers(100, 599), JSON_RARO)
    def test_clasificar_cualquier_error_http_siempre_da_una_categoria(self, estado, cuerpo):
        info = analizar_error(error_http(estado, cuerpo))
        assert isinstance(info.categoria, Categoria)
        assert isinstance(pudo_haberse_procesado(error_http(estado, cuerpo)), bool)

    @given(st.text(max_size=40))
    def test_leer_un_reset_cualquiera_no_revienta_y_es_un_numero_finito_o_nada(self, texto):
        resultado = parse_reset(texto)
        assert resultado is None or math.isfinite(resultado)

    @given(st.from_regex(r"[0-9]{1,6}(\.[0-9]{1,3})?[smhd]", fullmatch=True))
    def test_las_duraciones_con_unidad_se_entienden(self, texto):
        assert parse_reset(texto) is not None

    @given(st.recursive(st.builds(Exception, st.text(max_size=5)), lambda e: st.builds(
        lambda a, b: _con_causa(a, b), e, st.builds(Exception, st.text(max_size=5))), max_leaves=4))
    def test_reconocer_la_causa_de_un_fallo_funciona_con_cualquier_cadena(self, error):
        assert isinstance(es_corte_de_conexion(error), bool)
        assert isinstance(es_timeout(error), bool)
        assert isinstance(es_respuesta_ilegible(error), bool)
        assert isinstance(pudo_haberse_procesado(error), bool)


def _con_causa(a: Exception, b: Exception) -> Exception:
    a.__cause__ = b
    return a


# ----------------------------------------------------------------------
# Lo que se guarda y lo que se exporta
# ----------------------------------------------------------------------


class TestGuardado:
    @given(st.lists(TEXTO_NO_VACIO, min_size=1, max_size=5))
    def test_un_lote_guardado_y_cargado_es_el_mismo(self, textos):
        with tempfile.TemporaryDirectory() as carpeta:
            respuestas = Path(carpeta)
            original = paths.RESPONSES_DIR
            paths.RESPONSES_DIR = respuestas
            try:
                job = Job.crear(textos, Configuracion())
                job.messages[0].status = STATUS_COMPLETED
                assert job.save() is True
                cargado = Job.load(job.job_id)
            finally:
                paths.RESPONSES_DIR = original
        assert cargado is not None
        assert [m.text for m in cargado.messages] == textos
        assert cargado.messages[0].status == STATUS_COMPLETED

    @given(TEXTO, st.sampled_from(["\n", "\r\n"]))
    def test_la_escritura_atomica_es_exacta_en_utf8(self, texto, salto):
        contenido = texto.replace("\n", salto)
        with tempfile.TemporaryDirectory() as carpeta:
            destino = Path(carpeta) / "x.txt"
            paths.write_atomic(destino, contenido)
            assert destino.read_bytes() == contenido.encode("utf-8")
            assert [p.name for p in Path(carpeta).iterdir()] == ["x.txt"]

    @given(CON_SUSTITUTOS)
    def test_un_texto_con_sustitutos_sueltos_se_escribe_igualmente(self, texto):
        """Un emoji partido por la mitad (de un pegado raro) no debe tirar abajo el guardado."""
        with tempfile.TemporaryDirectory() as carpeta:
            destino = Path(carpeta) / "x.txt"
            paths.write_atomic(destino, texto)
            assert destino.exists()
            destino.read_text(encoding="utf-8")                # y se puede volver a leer

    @given(CON_SUSTITUTOS)
    def test_limpiar_sustitutos_deja_un_texto_que_se_puede_escribir(self, texto):
        limpio = limpiar_sustitutos(texto)
        limpio.encode("utf-8")                                 # no lanza
        assert len(limpio) == len(texto)
        assert limpiar_sustitutos(limpio) == limpio

    @given(CON_SUSTITUTOS)
    def test_una_pregunta_con_sustitutos_se_guarda_y_se_vuelve_a_cargar(self, texto):
        with tempfile.TemporaryDirectory() as carpeta:
            original = paths.RESPONSES_DIR
            paths.RESPONSES_DIR = Path(carpeta)
            try:
                job = Job.crear(["pregunta normal"], Configuracion())
                job.agregar_orden(texto)
                cargado = Job.load(job.job_id)
            finally:
                paths.RESPONSES_DIR = original
        assert cargado is not None and [m.text for m in cargado.messages][0] == "pregunta normal"
        assert cargado.messages[1].text == limpiar_sustitutos(texto)

    def test_un_sustituto_suelto_concreto(self, tmp_path):
        paths.write_atomic(tmp_path / "x.txt", "hola \ud83d mundo")
        assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hola \ufffd mundo"   # el mismo criterio en los dos sitios
        assert limpiar_sustitutos("hola \ud83d mundo") == "hola \ufffd mundo"

    @given(st.text(max_size=40))
    def test_neutralizar_nunca_deja_una_celda_que_empiece_como_formula(self, texto):
        resultado = neutralizar_formula(texto)
        assert resultado in (texto, "'" + texto)
        if resultado != texto:
            assert texto.startswith(("=", "+", "-", "@", "\t", "\r"))
        elif texto.startswith(("=", "@", "\t", "\r")):
            raise AssertionError("una fórmula sin neutralizar")
        assert neutralizar_formula(resultado) == resultado       # y no se aplica dos veces

    @given(TEXTO, TEXTO)
    def test_el_csv_exportado_se_lee_con_el_modulo_csv_y_las_formulas_van_neutralizadas(self, pregunta, respuesta):
        assume("\x00" not in pregunta + respuesta)
        with tempfile.TemporaryDirectory() as carpeta:
            paths_antes = paths.RESPONSES_DIR
            paths.RESPONSES_DIR = Path(carpeta)
            try:
                job = Job.crear([pregunta.strip() or "x"], Configuracion())
                job.messages[0].status = STATUS_COMPLETED
                job.response_path(1, 1).write_bytes(
                    f"====== SOLICITUD ======\n{job.messages[0].text}\n\n====== RESPUESTA ======\n{respuesta}\n".encode())
                destino = exportar(job, Path(carpeta) / "r.csv")
                with open(destino, encoding="utf-8-sig", newline="") as f:
                    filas = list(csv.DictReader(f))
            finally:
                paths.RESPONSES_DIR = paths_antes
        assert len(filas) == 1
        assert filas[0]["solicitud"] == neutralizar_formula(job.messages[0].text)
        celda = filas[0]["respuesta"]
        assert not (celda.startswith(("=", "@", "\t", "\r")))


# ----------------------------------------------------------------------
# Lo que se dibuja
# ----------------------------------------------------------------------


class TestFormato:
    @given(st.text(alphabet=st.sampled_from(list("#*_`>-|[]()\n 0123456789.abc!~$\\")), max_size=300))
    def test_dar_formato_a_cualquier_markdown_no_revienta_y_conserva_el_texto(self, texto):
        segmentos = analizar_formato(texto)
        unido = "".join(t for t, _ in segmentos)
        assert all(isinstance(t, str) and isinstance(e, tuple) for t, e in segmentos)
        # Las letras y los dígitos nunca desaparecen al formatear.
        solo = lambda s: re.sub(r"[^0-9a-c]", "", s)  # noqa: E731
        assert solo(unido) == solo(texto)

    @given(TEXTO)
    def test_dar_formato_a_texto_cualquiera_no_revienta(self, texto):
        analizar_formato(texto)
