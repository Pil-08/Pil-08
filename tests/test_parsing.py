from claude_batch_monitor.parsing import split_messages


class TestSplitMessages:
    def test_parte_por_linea_separadora(self):
        assert split_messages("uno\n---\ndos\n---\ntres", "---") == ["uno", "dos", "tres"]

    def test_cuatro_guiones_no_parten(self):
        # Regresión: re.split(r"-{3,}") rompía reglas horizontales de Markdown.
        assert split_messages("uno\n----\ndos", "---") == ["uno\n----\ndos"]

    def test_guiones_dentro_de_una_linea_no_parten(self):
        assert split_messages("a --- b\nc", "---") == ["a --- b\nc"]

    def test_espacios_alrededor_del_separador(self):
        assert split_messages("uno\n   ---   \ndos", "---") == ["uno", "dos"]

    def test_bloques_vacios_se_descartan(self):
        assert split_messages("---\nuno\n---\n---\n  \n---\ndos\n---", "---") == ["uno", "dos"]

    def test_separador_personalizado_con_caracteres_especiales(self):
        assert split_messages("a\n***\nb\n***\nc", "***") == ["a", "b", "c"]
        assert split_messages("a\n[[fin]]\nb", "[[fin]]") == ["a", "b"]

    def test_separador_vacio_usa_el_de_por_defecto(self):
        assert split_messages("a\n---\nb", "") == ["a", "b"]
        assert split_messages("a\n---\nb", "   ") == ["a", "b"]

    def test_texto_vacio(self):
        assert split_messages("", "---") == []
        assert split_messages("  \n\n ", "---") == []

    def test_conserva_saltos_de_linea_internos(self):
        assert split_messages("línea 1\nlínea 2\n---\notra", "---") == ["línea 1\nlínea 2", "otra"]
