"""Lectura de órdenes desde CSV, JSONL y texto."""

import json

import pytest

from claude_batch_monitor.inputs import (
    ErrorDeEntrada,
    aplicar_plantilla,
    leer_ordenes,
    listar_campos,
)


@pytest.fixture
def csv_simple(tmp_path):
    ruta = tmp_path / "datos.csv"
    ruta.write_text(
        "id,input,notas\n1,primera fila,x\n2,segunda fila,y\n3,tercera fila,z\n",
        encoding="utf-8",
    )
    return ruta


@pytest.fixture
def jsonl_simple(tmp_path):
    ruta = tmp_path / "datos.jsonl"
    ruta.write_text(
        "\n".join(
            json.dumps({"id": i, "input": f"fila {i}"}, ensure_ascii=False)
            for i in (1, 2, 3)
        ),
        encoding="utf-8",
    )
    return ruta


class TestCsv:
    def test_lee_la_columna_por_defecto(self, csv_simple):
        assert leer_ordenes(csv_simple) == ["primera fila", "segunda fila", "tercera fila"]

    def test_lee_una_columna_concreta(self, csv_simple):
        assert leer_ordenes(csv_simple, campo="notas") == ["x", "y", "z"]

    def test_columna_inexistente_falla_pronto_y_dice_cuáles_hay(self, csv_simple):
        # Procesar filas vacías y darse cuenta después de pagar es el peor
        # resultado posible: mejor reventar aquí con un mensaje útil.
        with pytest.raises(ErrorDeEntrada) as excinfo:
            leer_ordenes(csv_simple, campo="texto")
        mensaje = str(excinfo.value)
        assert "texto" in mensaje
        assert "id, input, notas" in mensaje

    def test_ignora_filas_vacias(self, tmp_path):
        ruta = tmp_path / "huecos.csv"
        ruta.write_text("input\nuno\n\n  \ndos\n", encoding="utf-8")
        assert leer_ordenes(ruta) == ["uno", "dos"]

    def test_tsv(self, tmp_path):
        ruta = tmp_path / "datos.tsv"
        ruta.write_text("input\tnotas\nuno\tx\ndos\ty\n", encoding="utf-8")
        assert leer_ordenes(ruta) == ["uno", "dos"]

    def test_celdas_con_comas_y_saltos_de_linea(self, tmp_path):
        ruta = tmp_path / "complejo.csv"
        ruta.write_text('input\n"uno, con coma"\n"dos\ncon salto"\n', encoding="utf-8")
        ordenes = leer_ordenes(ruta)
        assert ordenes == ["uno, con coma", "dos\ncon salto"]

    def test_excel_en_espanol_usa_punto_y_coma(self, tmp_path):
        # Excel con configuración regional española guarda los "CSV" con `;`.
        ruta = tmp_path / "excel.csv"
        ruta.write_text("id;comentario;fecha\n1;me encantó, repetiré;2026-01-01\n", encoding="utf-8")
        assert leer_ordenes(ruta, campo="comentario") == ["me encantó, repetiré"]

    def test_bom_de_excel(self, tmp_path):
        ruta = tmp_path / "bom.csv"
        ruta.write_bytes(b"\xef\xbb\xbfinput\nhola\n")
        assert leer_ordenes(ruta) == ["hola"]

    def test_una_sola_columna_con_otro_nombre_se_usa_sin_preguntar(self, tmp_path):
        ruta = tmp_path / "una.csv"
        ruta.write_text("pregunta\n¿Qué es un token?\n", encoding="utf-8")
        assert leer_ordenes(ruta) == ["¿Qué es un token?"]

    def test_varias_columnas_sin_input_exige_elegir(self, tmp_path):
        ruta = tmp_path / "dos.csv"
        ruta.write_text("a,b\n1,2\n", encoding="utf-8")
        with pytest.raises(ErrorDeEntrada) as excinfo:
            leer_ordenes(ruta)
        assert "a, b" in str(excinfo.value)

    def test_nombre_de_columna_sin_importar_mayusculas(self, tmp_path):
        ruta = tmp_path / "may.csv"
        ruta.write_text("Input\nhola\n", encoding="utf-8")
        assert leer_ordenes(ruta, campo="input") == ["hola"]


class TestJsonl:
    def test_lee_el_campo_por_defecto(self, jsonl_simple):
        assert leer_ordenes(jsonl_simple) == ["fila 1", "fila 2", "fila 3"]

    def test_campo_inexistente_falla_con_la_linea_exacta(self, jsonl_simple):
        with pytest.raises(ErrorDeEntrada) as excinfo:
            leer_ordenes(jsonl_simple, campo="texto")
        assert "línea 1" in str(excinfo.value)

    def test_json_invalido_dice_la_linea(self, tmp_path):
        ruta = tmp_path / "roto.jsonl"
        ruta.write_text('{"input": "ok"}\nesto no es json\n', encoding="utf-8")
        with pytest.raises(ErrorDeEntrada) as excinfo:
            leer_ordenes(ruta)
        assert "línea 2" in str(excinfo.value)

    def test_una_linea_que_no_es_objeto(self, tmp_path):
        ruta = tmp_path / "lista.jsonl"
        ruta.write_text("[1, 2, 3]\n", encoding="utf-8")
        with pytest.raises(ErrorDeEntrada):
            leer_ordenes(ruta)

    def test_valores_no_texto_se_convierten(self, tmp_path):
        ruta = tmp_path / "numeros.jsonl"
        ruta.write_text('{"input": 42}\n{"input": true}\n', encoding="utf-8")
        assert leer_ordenes(ruta) == ["42", "True"]


class TestTextoPlano:
    def test_usa_el_separador(self, tmp_path):
        ruta = tmp_path / "ordenes.txt"
        ruta.write_text("uno\n---\ndos\n---\ntres", encoding="utf-8")
        assert leer_ordenes(ruta) == ["uno", "dos", "tres"]

    def test_separador_personalizado(self, tmp_path):
        ruta = tmp_path / "ordenes.md"
        ruta.write_text("uno\n===\ndos", encoding="utf-8")
        assert leer_ordenes(ruta, separador="===") == ["uno", "dos"]


class TestPlantilla:
    def test_sustituye_el_marcador(self, csv_simple):
        ordenes = leer_ordenes(csv_simple, plantilla="Traduce:\n{input}\nFin.")
        assert ordenes[0] == "Traduce:\nprimera fila\nFin."

    def test_sin_marcador_concatena(self):
        assert aplicar_plantilla("Resume esto:", "texto") == "Resume esto:\n\ntexto"

    def test_plantilla_vacia_no_toca_nada(self):
        assert aplicar_plantilla("", "texto") == "texto"

    def test_el_texto_con_llaves_no_se_interpreta(self):
        # str.replace, no str.format: una fila con "{x}" no debe reventar.
        assert aplicar_plantilla("A {input} B", "{x} {0}") == "A {x} {0} B"


class TestLimite:
    def test_corta_a_las_primeras_n(self, csv_simple):
        # El "smoke test": probar el prompt con 2 filas antes de lanzar 5000.
        assert leer_ordenes(csv_simple, limite=2) == ["primera fila", "segunda fila"]

    def test_limite_mayor_que_el_total_no_molesta(self, csv_simple):
        assert len(leer_ordenes(csv_simple, limite=999)) == 3

    def test_limite_cero_se_ignora(self, csv_simple):
        assert len(leer_ordenes(csv_simple, limite=0)) == 3


class TestListarCampos:
    def test_csv(self, csv_simple):
        assert listar_campos(csv_simple) == ["id", "input", "notas"]

    def test_jsonl(self, jsonl_simple):
        assert listar_campos(jsonl_simple) == ["id", "input"]

    def test_texto_plano_no_tiene_campos(self, tmp_path):
        ruta = tmp_path / "a.txt"
        ruta.write_text("hola", encoding="utf-8")
        assert listar_campos(ruta) is None


class TestErrores:
    def test_fichero_inexistente(self, tmp_path):
        with pytest.raises(ErrorDeEntrada):
            leer_ordenes(tmp_path / "no-existe.csv")

    def test_fichero_vacio(self, tmp_path):
        ruta = tmp_path / "vacio.csv"
        ruta.write_text("input\n", encoding="utf-8")
        with pytest.raises(ErrorDeEntrada):
            leer_ordenes(ruta)

    def test_codificacion_no_utf8_da_un_mensaje_util(self, tmp_path):
        ruta = tmp_path / "latin1.csv"
        ruta.write_bytes("input\ncanción\n".encode("latin-1"))
        with pytest.raises(ErrorDeEntrada) as excinfo:
            leer_ordenes(ruta)
        assert "UTF-8" in str(excinfo.value)
