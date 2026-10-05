"""El generador del archivo único (`tools/build_single_file.py`): une bien y, si algo no se puede unir con seguridad, se niega."""

from __future__ import annotations

import ast
import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

GENERADOR = Path(__file__).resolve().parent.parent / "tools" / "build_single_file.py"
pytestmark = pytest.mark.skipif(not GENERADOR.exists(), reason="solo en el repositorio completo")


@pytest.fixture(scope="module")
def g():
    spec = importlib.util.spec_from_file_location("build_single_file_pruebas", GENERADOR)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def modulo(g, nombre: str, fuente: str, es_paquete: bool = False):
    fuente = textwrap.dedent(fuente).lstrip("\n")
    base = Path("/falso") / Path(*nombre.split("."))
    ruta = base / "__init__.py" if es_paquete else base.with_suffix(".py")
    m = g.Modulo(nombre, nombre, ruta, fuente, ast.parse(fuente))
    m.definidos = g._nombres_de_nivel_superior(m.arbol)
    return m


def unir(g, *modulos):
    por_nombre = {m.nombre: m for m in modulos}
    importaciones = g.Importaciones()
    return [g.transformar(m, por_nombre, importaciones) for m in modulos], importaciones


class TestTransformar:
    def test_quita_las_importaciones_del_paquete_y_el_prefijo_del_modulo(self, g):
        rutas = modulo(g, "paths", "DATA_DIR = 1\n")
        usa = modulo(g, "usa", """
            from . import paths

            def f():
                return paths.DATA_DIR
        """)
        (_, t), _ = unir(g, rutas, usa)
        assert "import" not in t.cuerpo and "return DATA_DIR" in t.cuerpo and "paths." not in t.cuerpo

    def test_los_nombres_importados_de_otro_modulo_ya_estan_a_la_vista(self, g):
        a = modulo(g, "a", "def util(): ...\n")
        b = modulo(g, "b", """
            from .a import util

            def f():
                return util()
        """)
        (_, t), _ = unir(g, a, b)
        assert "import" not in t.cuerpo and "return util()" in t.cuerpo

    def test_un_alias_de_importacion_pasa_a_ser_una_asignacion(self, g):
        a = modulo(g, "a", "def util(): ...\n")
        b = modulo(g, "b", """
            from .a import util as ayuda

            def f():
                return ayuda()
        """)
        (_, t), _ = unir(g, a, b)
        assert "ayuda = util" in t.cuerpo and "from" not in t.cuerpo

    def test_un_alias_dentro_de_una_funcion_se_queda_dentro(self, g):
        a = modulo(g, "a", "def util(): ...\n")
        b = modulo(g, "b", """
            def f():
                from .a import util as ayuda
                return ayuda()
        """)
        (_, t), _ = unir(g, a, b)
        assert "    ayuda = util\n    return ayuda()" in t.cuerpo

    def test_los_modulos_de_subpaquetes_se_resuelven(self, g):
        base = modulo(g, "core.base", "class Motor: ...\n")
        seq = modulo(g, "core.seq", """
            from .base import Motor

            class Otro(Motor): ...
        """)
        cli = modulo(g, "cli", """
            from .core.base import Motor
            from .core.seq import Otro
        """)
        (_, _, t), _ = unir(g, base, seq, cli)
        assert "import" not in t.cuerpo

    def test_el_bloque_type_checking_con_solo_importaciones_del_paquete_desaparece_entero(self, g):
        a = modulo(g, "a", "class Config: ...\n")
        b = modulo(g, "b", """
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                from .a import Config

            def f(c: "Config"): ...
        """)
        (_, t), imp = unir(g, a, b)
        assert "TYPE_CHECKING" not in t.cuerpo and "def f" in t.cuerpo
        assert "typing" in imp.desde  # se reunió arriba (luego se descarta porque ya no se usa)

    def test_una_funcion_que_solo_importaba_se_queda_con_pass(self, g):
        a = modulo(g, "a", "X = 1\n")
        b = modulo(g, "b", """
            def f():
                from .a import X

            def g():
                return 1
        """)
        (_, t), _ = unir(g, a, b)
        ast.parse(t.cuerpo)  # sigue siendo Python válido
        assert "def f():\n    pass" in t.cuerpo

    def test_la_guarda_de_ejecucion_y_el_docstring_se_quitan(self, g):
        a = modulo(g, "a", '''
            """Explica el módulo."""

            def main(): ...

            if __name__ == "__main__":
                main()
        ''')
        (t,), _ = unir(g, a)
        assert "__main__" not in t.cuerpo and "Explica" not in t.cuerpo and t.docstring == "Explica el módulo."

    def test_las_importaciones_externas_se_reunen_y_se_quitan_del_modulo(self, g):
        a = modulo(g, "a", """
            import os
            from pathlib import Path

            def f(): return os.sep, Path
        """)
        (t,), imp = unir(g, a)
        assert "import" not in t.cuerpo
        assert imp.simples == {"os": None} and imp.desde == {"pathlib": {("Path", None)}}

    def test_las_importaciones_dentro_de_try_se_quedan_donde_estan(self, g):
        a = modulo(g, "a", """
            try:
                import keyring
            except ImportError:
                keyring = None
        """)
        (t,), imp = unir(g, a)
        assert "import keyring" in t.cuerpo and imp.simples == {}

    def test_lo_que_no_se_puede_unir_con_seguridad_se_rechaza(self, g):
        a = modulo(g, "a", "X = 1\n")
        with pytest.raises(g.ErrorDeConstruccion, match="no define"):
            unir(g, a, modulo(g, "b", "from .a import Y\n"))
        with pytest.raises(g.ErrorDeConstruccion, match="import \\*"):
            unir(g, a, modulo(g, "b", "from .a import *\n"))
        with pytest.raises(g.ErrorDeConstruccion, match="relativas"):
            unir(g, a, modulo(g, "b", "import claude_batch_monitor.a\n"))

    def test_un_modulo_importado_que_se_usa_sin_punto_se_rechaza(self, g):
        a = modulo(g, "a", "X = 1\n")
        b = modulo(g, "b", """
            from . import a

            def f():
                return a
        """)
        with pytest.raises(g.ErrorDeConstruccion, match="sin `.nombre`"):
            unir(g, a, b)

    def test_si_el_nombre_de_un_modulo_se_reutiliza_como_variable_se_rechaza(self, g):
        a = modulo(g, "a", "X = 1\n")
        b = modulo(g, "b", """
            from . import a

            def f():
                a = 3
                return a
        """)
        with pytest.raises(g.ErrorDeConstruccion, match="reutiliza como variable local"):
            unir(g, a, b)

    def test_si_el_nombre_al_quitar_el_prefijo_choca_con_una_variable_local_se_rechaza(self, g):
        a = modulo(g, "a", "LIMITE = 1\n")
        b = modulo(g, "b", """
            from . import a

            def f(LIMITE):
                return a.LIMITE + LIMITE
        """)
        with pytest.raises(g.ErrorDeConstruccion, match="variable local"):
            unir(g, a, b)


class TestComprobaciones:
    def test_dos_modulos_con_el_mismo_nombre_no_se_unen(self, g):
        a = modulo(g, "a", "def util(): ...\n")
        b = modulo(g, "b", "def util(): ...\n")
        with pytest.raises(g.ErrorDeConstruccion, match="'util' está definido en a y en b"):
            g._comprobar_colisiones([a, b], g.Importaciones())

    def test_un_nombre_importado_no_puede_ser_tambien_una_definicion(self, g):
        a = modulo(g, "a", "def time(): ...\n")
        imp = g.Importaciones()
        imp.simples["time"] = None
        with pytest.raises(g.ErrorDeConstruccion, match="'time' es a la vez"):
            g._comprobar_colisiones([a], imp)

    def test_el_orden_tiene_que_respetar_lo_que_se_usa_al_cargar(self, g):
        a = modulo(g, "a", "X = 1\n")
        b = modulo(g, "b", "from .a import X\nY = X\n")
        por_nombre = {"a": a, "b": b}
        for m in (a, b):
            m.importaciones_al_cargar = g._dependencias_al_cargar(m, por_nombre)
        g._comprobar_orden([a, b])
        with pytest.raises(g.ErrorDeConstruccion, match="va después"):
            g._comprobar_orden([b, a])

    def test_lo_que_solo_se_importa_dentro_de_una_funcion_no_obliga_a_ningun_orden(self, g):
        a = modulo(g, "a", "X = 1\n")
        b = modulo(g, "b", "def f():\n    from .a import X\n    return X\n")
        assert g._dependencias_al_cargar(b, {"a": a, "b": b}) == set()

    def test_cada_modulo_del_paquete_tiene_su_seccion(self, g):
        modulos = g.cargar_modulos()  # lanza si aparece un módulo nuevo sin sitio en SECCIONES
        assert [m.nombre for m in modulos][0] == "paths" and [m.nombre for m in modulos][-1] == "__main__"

    def test_el_generador_dice_que_no_esta_al_dia_si_el_archivo_difiere(self, g, tmp_path, capsys):
        archivo = tmp_path / "otro.py"
        archivo.write_text("# viejo\n", encoding="utf-8")
        assert g.main(["--check", "--salida", str(archivo)]) == 1
        assert "no está al día" in capsys.readouterr().err
        assert archivo.read_text(encoding="utf-8") == "# viejo\n"  # --check nunca escribe


class TestLimpieza:
    def test_no_deja_mas_de_dos_lineas_en_blanco_ni_espacios_al_final(self, g):
        assert g.compactar_lineas_en_blanco("a = 1   \n\n\n\n\n\nb = 2\n") == "a = 1\n\n\nb = 2\n"

    def test_no_deja_lineas_en_blanco_justo_despues_de_abrir_un_bloque(self, g):
        assert g.compactar_lineas_en_blanco("def f():\n\n    return 1\n") == "def f():\n    return 1\n"

    def test_no_toca_el_interior_de_las_cadenas_de_varias_lineas(self, g):
        texto = 'x = """uno\n\n\n\n\ncuatro   \n"""\n'
        assert g.compactar_lineas_en_blanco(texto) == texto
