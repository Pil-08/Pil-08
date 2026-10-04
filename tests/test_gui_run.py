"""El lanzador de las ventanas: qué modo se abre y cómo se alterna entre ellos (sin abrir ventanas de verdad)."""

import sys

import pytest

from claude_batch_monitor import gui
from claude_batch_monitor.config import ConfigManager, Configuracion

tk = pytest.importorskip("tkinter")

from claude_batch_monitor.gui import app as modulo_app  # noqa: E402
from claude_batch_monitor.gui import simple as modulo_simple  # noqa: E402


@pytest.fixture
def ventanas(monkeypatch):
    """Sustituye las dos ventanas por funciones que devuelven lo que diga el guion."""
    registro = {"llamadas": [], "simple": [], "avanzada": []}

    def falso(nombre):
        def abrir(config=None):
            registro["llamadas"].append(nombre)
            guion = registro[nombre]
            return guion.pop(0) if guion else None
        return abrir

    monkeypatch.setattr(modulo_simple, "ejecutar_simple", falso("simple"))
    monkeypatch.setattr(modulo_app, "ejecutar_avanzada", falso("avanzada"))
    monkeypatch.setattr(gui, "_activar_dpi_alto", lambda: None)
    return registro


class TestModoPedido:
    @pytest.mark.parametrize("argv, esperado", [
        ([], None), (["--simple"], "simple"), (["--avanzado"], "avanzada"), (["--avanzada"], "avanzada"),
        (["--otra-cosa"], None), (["--simple", "--avanzado"], "avanzada"),
    ])
    def test_opciones(self, argv, esperado):
        assert gui.modo_pedido(argv) == esperado


class TestRun:
    def test_la_primera_vez_se_abre_el_modo_simple(self, ventanas):
        assert gui.run([]) == 0
        assert ventanas["llamadas"] == ["simple"]

    def test_se_abre_el_modo_de_la_ultima_vez(self, ventanas):
        ConfigManager.guardar(Configuracion(interfaz="avanzada"))
        gui.run([])
        assert ventanas["llamadas"] == ["avanzada"]

    def test_la_opcion_de_la_linea_de_ordenes_manda_sobre_lo_guardado(self, ventanas):
        ConfigManager.guardar(Configuracion(interfaz="avanzada"))
        gui.run(["--simple"])
        assert ventanas["llamadas"] == ["simple"]
        ventanas["llamadas"].clear()
        ConfigManager.guardar(Configuracion(interfaz="simple"))
        gui.run(["--avanzado"])
        assert ventanas["llamadas"] == ["avanzada"]

    def test_pedir_otro_modo_desde_la_ventana_lo_abre(self, ventanas):
        ventanas["simple"].append("avanzada")      # en el simple se pulsó «Modo avanzado»
        ventanas["avanzada"].append("simple")      # y luego, en el avanzado, «Modo simple»
        assert gui.run([]) == 0
        assert ventanas["llamadas"] == ["simple", "avanzada", "simple"]

    def test_cerrar_la_ventana_termina_sin_volver_a_abrir(self, ventanas):
        ventanas["simple"].append(None)
        gui.run([])
        assert ventanas["llamadas"] == ["simple"]

    def test_pedir_el_mismo_modo_no_entra_en_un_bucle(self, ventanas):
        ventanas["simple"].append("simple")
        assert gui.run([]) == 0 and ventanas["llamadas"] == ["simple"]

    def test_un_valor_desconocido_tampoco(self, ventanas):
        ventanas["simple"].append("otra-cosa")
        assert gui.run([]) == 0 and ventanas["llamadas"] == ["simple"]

    def test_sin_pantalla_se_dice_y_se_sale_con_error(self, ventanas, monkeypatch, capsys):
        def sin_pantalla(config=None):
            raise tk.TclError("no display name and no $DISPLAY environment variable")

        monkeypatch.setattr(modulo_simple, "ejecutar_simple", sin_pantalla)
        assert gui.run([]) == 1
        err = capsys.readouterr().err
        assert "No se pudo abrir la ventana" in err and "--cli" in err

    def test_un_config_json_corrupto_abre_el_modo_simple(self, ventanas):
        from claude_batch_monitor import paths
        paths.CONFIG_FILE.write_text("{no es json", encoding="utf-8")
        gui.run([])
        assert ventanas["llamadas"] == ["simple"]


class TestDpi:
    def test_fuera_de_windows_no_hace_nada(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        gui._activar_dpi_alto()

    def test_en_windows_pide_conciencia_de_dpi(self, monkeypatch):
        import ctypes
        llamadas = []

        class Shcore:
            @staticmethod
            def SetProcessDpiAwareness(n):
                llamadas.append(n)

        class Falso:
            shcore = Shcore()

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(ctypes, "windll", Falso(), raising=False)
        gui._activar_dpi_alto()
        assert llamadas == [1]

    def test_en_windows_antiguo_usa_la_funcion_clasica(self, monkeypatch):
        import ctypes
        llamadas = []

        class Shcore:
            @staticmethod
            def SetProcessDpiAwareness(n):
                raise OSError("no disponible")

        class User32:
            @staticmethod
            def SetProcessDPIAware():
                llamadas.append("clasica")

        class Falso:
            shcore = Shcore()
            user32 = User32()

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(ctypes, "windll", Falso(), raising=False)
        gui._activar_dpi_alto()
        assert llamadas == ["clasica"]

    def test_si_todo_falla_no_impide_abrir(self, monkeypatch):
        import ctypes

        class Roto:
            @property
            def shcore(self):
                raise RuntimeError("rotísimo")

            user32 = shcore

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(ctypes, "windll", Roto(), raising=False)
        gui._activar_dpi_alto()      # no debe lanzar
