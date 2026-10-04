"""Icono de la aplicación y bandeja del sistema (con un `pystray` falso: aquí no hay bandeja de verdad)."""

import sys
import types

import pytest

from claude_batch_monitor.gui import tray
from claude_batch_monitor.gui.tray import BandejaDelSistema

PIL = pytest.importorskip("PIL")


class IconoFalso:
    creados = []

    def __init__(self, nombre, imagen, titulo, menu):
        self.nombre, self.imagen, self.titulo, self.menu = nombre, imagen, titulo, menu
        self.ejecutado = self.parado = False
        IconoFalso.creados.append(self)

    def run(self):
        self.ejecutado = True

    def stop(self):
        self.parado = True


@pytest.fixture
def pystray_falso(monkeypatch):
    IconoFalso.creados = []
    modulo = types.ModuleType("pystray")
    modulo.Icon = IconoFalso
    modulo.Menu = lambda *items: list(items)
    modulo.MenuItem = lambda texto, accion, default=False: (texto, accion, default)
    monkeypatch.setitem(sys.modules, "pystray", modulo)
    return modulo


class TestIcono:
    def test_el_dibujo_es_cuadrado_y_con_transparencia(self):
        from claude_batch_monitor.gui.icono import dibujar
        img = dibujar(64)
        assert img.size == (64, 64) and img.mode == "RGBA"
        assert img.getpixel((0, 0))[3] == 0                       # las esquinas son transparentes
        r, g, b, a = img.getpixel((32, 12))
        assert a == 255 and b > r                                  # el cuerpo es azul

    def test_el_png_de_la_ventana_existe_y_es_valido(self):
        from PIL import Image

        from claude_batch_monitor.gui.icono import RUTA_PNG
        assert RUTA_PNG.is_file()
        with Image.open(RUTA_PNG) as img:
            assert img.size == (256, 256)

    def test_la_bandeja_reutiliza_el_mismo_dibujo(self):
        assert tray._imagen().size == (64, 64)


class TestDisponible:
    def test_con_las_librerias_instaladas(self, pystray_falso):
        assert tray.disponible() is True

    def test_sin_pystray(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pystray", None)       # None en sys.modules = ImportError al importar
        assert tray.disponible() is False

    def test_sin_pillow(self, pystray_falso, monkeypatch):
        monkeypatch.setitem(sys.modules, "PIL.Image", None)
        assert tray.disponible() is False


class TestBandeja:
    def _bandeja(self, abrir=None, salir=None):
        return BandejaDelSistema("Título", "Abrir", "Salir", abrir or (lambda: None), salir or (lambda: None))

    def test_mostrar_crea_el_icono_y_lo_ejecuta_en_otro_hilo(self, pystray_falso):
        b = self._bandeja()
        assert b.mostrar() is True
        icono = IconoFalso.creados[0]
        assert icono.titulo == "Título" and icono.imagen.size == (64, 64)
        import time
        for _ in range(100):
            if icono.ejecutado:
                break
            time.sleep(0.01)
        assert icono.ejecutado

    def test_mostrar_dos_veces_no_crea_dos_iconos(self, pystray_falso):
        b = self._bandeja()
        assert b.mostrar() and b.mostrar()
        assert len(IconoFalso.creados) == 1

    def test_el_menu_llama_a_abrir_y_a_salir(self, pystray_falso):
        llamadas = []
        b = self._bandeja(abrir=lambda: llamadas.append("abrir"), salir=lambda: llamadas.append("salir"))
        b.mostrar()
        (texto_a, accion_a, por_defecto), (texto_s, accion_s, _) = IconoFalso.creados[0].menu
        assert (texto_a, por_defecto, texto_s) == ("Abrir", True, "Salir")      # el doble clic abre
        accion_a(None, None)
        accion_s(None, None)
        assert llamadas == ["abrir", "salir"]

    def test_ocultar_para_el_icono_y_es_seguro_repetirlo(self, pystray_falso):
        b = self._bandeja()
        b.mostrar()
        b.ocultar()
        b.ocultar()
        assert IconoFalso.creados[0].parado

    def test_ocultar_sin_haber_mostrado(self, pystray_falso):
        self._bandeja().ocultar()

    def test_si_pystray_falla_se_devuelve_false(self, pystray_falso, monkeypatch):
        def roto(*a, **k):
            raise RuntimeError("sin servidor de ventanas")
        monkeypatch.setattr(pystray_falso, "Icon", roto)
        assert self._bandeja().mostrar() is False

    def test_si_el_icono_falla_al_pararse_no_se_propaga(self, pystray_falso):
        b = self._bandeja()
        b.mostrar()

        def roto():
            raise RuntimeError("ya estaba parado")
        IconoFalso.creados[0].stop = roto
        b.ocultar()
