"""Mostrar respuestas con formato: el análisis (sin ventanas) y su aplicación a un cuadro de texto."""

import random

from claude_batch_monitor.gui.formato import analizar_formato as analizar


def texto(segmentos):
    return "".join(t for t, _ in segmentos)


def etiquetas_de(segmentos, buscado):
    """Las etiquetas del primer trozo cuyo texto es `buscado`."""
    for t, e in segmentos:
        if t == buscado:
            return e
    raise AssertionError(f"no hay un trozo {buscado!r} en {segmentos}")


class TestTextoSinFormato:
    def test_el_texto_normal_no_cambia(self):
        original = "Hola.\nSegunda línea, con ñ y tildes.\n\nTercera."
        assert texto(analizar(original)) == original
        assert all(e == () for _, e in analizar(original))

    def test_vacio(self):
        assert analizar("") == []

    def test_los_saltos_de_linea_de_windows_se_normalizan(self):
        assert texto(analizar("a\r\nb")) == "a\nb"

    def test_asteriscos_de_multiplicar_y_nombres_con_guion_bajo_no_son_formato(self):
        original = "2 * 3 * 4 = 24 y mi_variable_larga y una_otra_cosa"
        assert texto(analizar(original)) == original
        assert all(e == () for _, e in analizar(original))

    def test_un_asterisco_suelto_se_queda_como_esta(self):
        assert texto(analizar("precio* sujeto a cambios")) == "precio* sujeto a cambios"


class TestEnLinea:
    def test_negrita_cursiva_y_ambas(self):
        t = analizar("**a** *b* ***c***")
        assert etiquetas_de(t, "a") == ("negrita",)
        assert etiquetas_de(t, "b") == ("cursiva",)
        assert etiquetas_de(t, "c") == ("negrita_cursiva",)
        assert texto(t) == "a b c"

    def test_con_guiones_bajos(self):
        t = analizar("__fuerte__ y _suave_")
        assert etiquetas_de(t, "fuerte") == ("negrita",) and etiquetas_de(t, "suave") == ("cursiva",)

    def test_codigo_en_linea_no_interpreta_su_contenido(self):
        t = analizar("usa `**no** *formato*` aquí")
        assert etiquetas_de(t, "**no** *formato*") == ("codigo",)
        assert texto(t) == "usa **no** *formato* aquí"

    def test_codigo_con_dobles_comillas_invertidas_admite_una_dentro(self):
        t = analizar("mira ``a ` b`` listo")
        assert etiquetas_de(t, "a ` b") == ("codigo",)

    def test_negrita_que_contiene_codigo(self):
        t = analizar("**mira `x`**")
        assert etiquetas_de(t, "mira ") == ("negrita",) and etiquetas_de(t, "x") == ("negrita", "codigo")

    def test_enlaces_muestran_el_destino(self):
        t = analizar("ver [la guía](https://ejemplo.org/guia) ya")
        assert etiquetas_de(t, "la guía") == ("enlace",)
        assert etiquetas_de(t, " (https://ejemplo.org/guia)") == ("apagado",)
        assert texto(t) == "ver la guía (https://ejemplo.org/guia) ya"

    def test_un_enlace_cuyo_texto_es_la_url_no_la_repite(self):
        assert texto(analizar("[https://a.org](https://a.org)")) == "https://a.org"

    def test_los_caracteres_escapados_se_quedan_literales(self):
        t = analizar(r"\*no cursiva\* y \`no código\` y \# no título")
        assert texto(t) == "*no cursiva* y `no código` y # no título"
        assert all(e == () for _, e in t)

    def test_la_barra_invertida_normal_se_conserva(self):
        assert texto(analizar(r"ruta C:\carpeta\archivo")) == r"ruta C:\carpeta\archivo"

    def test_cursiva_dentro_de_una_palabra_con_asteriscos_no_se_toca(self):
        assert all(e == () for _, e in analizar("a*b*c"))


class TestBloques:
    def test_titulos_por_niveles(self):
        t = analizar("# Uno\n## Dos\n### Tres\n###### Seis")
        assert etiquetas_de(t, "Uno") == ("h1",) and etiquetas_de(t, "Dos") == ("h2",)
        assert etiquetas_de(t, "Tres") == ("h3",) and etiquetas_de(t, "Seis") == ("h3",)

    def test_un_titulo_necesita_espacio_tras_la_almohadilla(self):
        assert texto(analizar("#sinespacio")) == "#sinespacio"

    def test_titulo_con_almohadillas_de_cierre_y_formato_dentro(self):
        t = analizar("## Hola **mundo** ##")
        assert texto(t) == "Hola mundo"                       # las almohadillas de cierre no se muestran
        assert etiquetas_de(t, "mundo") == ("h2", "negrita")

    def test_lista_con_viñetas_y_numeros(self):
        t = analizar("- uno\n* dos\n+ tres\n1. cuatro\n2) cinco")
        assert texto(t).splitlines() == ["•\tuno", "•\tdos", "•\ttres", "1.\tcuatro", "2)\tcinco"]

    def test_lista_anidada_por_sangria(self):
        t = analizar("- a\n  - b\n    - c\n      - d\n        - e")
        niveles = [e[0] for tx, e in t if tx == "•\t"]
        assert niveles == ["lista0", "lista1", "lista2", "lista3", "lista3"]   # el nivel máximo es 3

    def test_el_texto_de_la_lista_admite_formato(self):
        t = analizar("- punto con **fuerza**")
        assert etiquetas_de(t, "fuerza") == ("lista0", "negrita")

    def test_cita(self):
        t = analizar("> dicho *famoso*\n> segunda")
        assert texto(t) == "│ dicho famoso\n│ segunda"
        assert etiquetas_de(t, "│ ") == ("cita_marca",) and "cursiva" in etiquetas_de(t, "famoso")

    def test_regla_horizontal(self):
        for marca in ("---", "***", "___", "- - -"):
            assert [e for _, e in analizar(f"a\n{marca}\nb") if e == ("regla",)]

    def test_dos_guiones_no_son_regla(self):
        assert texto(analizar("a\n--\nb")) == "a\n--\nb"

    def test_bloque_de_codigo_no_interpreta_nada(self):
        t = analizar("antes\n```python\nx = **2**  # *no*\n# titulo\n```\ndespués")
        assert texto(t) == "antes\nx = **2**  # *no*\n# titulo\ndespués"
        assert [e for tx, e in t if tx.startswith("x = ")] == [("bloque",)]
        assert [e for tx, e in t if tx.startswith("# titulo")] == [("bloque",)]

    def test_bloque_sin_cerrar_llega_hasta_el_final(self):
        t = analizar("```\nabierto\n**sigue**")
        assert texto(t) == "abierto\n**sigue**" and all(e == ("bloque",) for _, e in t)

    def test_un_bloque_se_cierra_con_su_marca_no_con_otra(self):
        t = analizar("~~~\na\n```\nb\n~~~\nfuera **negrita**")
        assert [e for tx, e in t if tx in ("a\n", "```\n", "b\n")] == [("bloque",)] * 3
        assert etiquetas_de(t, "negrita") == ("negrita",)

    def test_la_valla_larga_solo_la_cierra_otra_igual_o_mas_larga(self):
        t = analizar("````\n```\ndentro\n````\nfuera")
        assert texto(t) == "```\ndentro\nfuera"

    def test_tabla(self):
        t = analizar("| Modelo | Precio |\n|---|---|\n| Sonnet | 2 $ |\n| Opus largo | 4 $ |\n\nfin")
        lineas = texto(t).splitlines()
        assert lineas[0].startswith("Modelo") and "│" in lineas[0]
        assert [e for tx, e in t if tx.startswith("Modelo")] == [("tabla_cabecera",)]
        assert [e for tx, e in t if tx.startswith("Sonnet")] == [("tabla",)]
        # Las columnas quedan alineadas: la barra está en el mismo sitio en todas las filas.
        assert len({linea.index("│") for linea in lineas[:3]}) == 1

    def test_tabla_con_filas_irregulares(self):
        t = analizar("| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |")
        assert texto(t)          # no se rompe

    def test_tabla_al_final_del_texto_sin_salto(self):
        t = analizar("| a | b |\n|---|---|\n| 1 | 2 |")
        assert not texto(t).endswith("\n")

    def test_una_linea_con_barras_pero_sin_separador_no_es_tabla(self):
        assert texto(analizar("| solo | una |")) == "| solo | una |"


class TestRobustez:
    def test_nunca_falla_ni_deja_escapar_sustitutos_privados(self):
        azar = random.Random(20261004)
        alfabeto = list("*_`#>-|[]() \n\\abc123~.+:")
        for _ in range(1500):
            original = "".join(azar.choice(alfabeto) for _ in range(azar.randint(0, 60)))
            resultado = texto(analizar(original))
            assert not any(0xE000 <= ord(c) <= 0xF8FF for c in resultado), repr(original)

    def test_las_letras_y_numeros_no_se_pierden(self):
        # Sin comillas invertidas ni virgulillas no hay bloques de código (cuyo nombre de lenguaje se quita),
        # así que todo lo alfanumérico del original debe seguir en el mismo orden.
        azar = random.Random(7)
        alfabeto = list("*_#>-|[]() \n\\abc123.+:")
        for _ in range(1500):
            original = "".join(azar.choice(alfabeto) for _ in range(azar.randint(0, 60)))
            solo = lambda s: "".join(c for c in s if c.isalnum())  # noqa: E731
            assert solo(texto(analizar(original))) == solo(original), repr(original)

    def test_texto_enorme(self):
        grande = ("# titulo\n- **punto** con `codigo`\n\n" * 3000)
        assert len(analizar(grande)) > 10000

    def test_si_el_analisis_falla_se_devuelve_el_texto_tal_cual(self, monkeypatch):
        from claude_batch_monitor.gui import formato

        def roto(texto):
            raise RuntimeError("bug")

        monkeypatch.setattr(formato, "_analizar", roto)
        assert analizar("**x**") == [("**x**", ())]
