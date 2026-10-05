"""Icono de la aplicación y bandeja del sistema (con un `pystray` falso: aquí no hay bandeja de verdad)."""

import sys
import types

import pytest

from claude_batch_monitor.gui import tray
from claude_batch_monitor.gui.tray import BandejaDelSistema


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


def _leer_png(datos: bytes):
    """Decodificador mínimo (solo RGBA de 8 bits sin filtro): así la prueba no depende de Pillow."""
    import struct
    import zlib

    assert datos[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ancho, alto = 8, b"", 0, 0
    while pos < len(datos):
        largo, tipo = struct.unpack(">I4s", datos[pos:pos + 8])
        cuerpo = datos[pos + 8:pos + 8 + largo]
        crc = struct.unpack(">I", datos[pos + 8 + largo:pos + 12 + largo])[0]
        assert crc == zlib.crc32(tipo + cuerpo) & 0xFFFFFFFF, f"CRC mal en {tipo}"
        if tipo == b"IHDR":
            ancho, alto, profundidad, color = struct.unpack(">IIBB", cuerpo[:10])
            assert (profundidad, color) == (8, 6)
        elif tipo == b"IDAT":
            idat += cuerpo
        pos += 12 + largo
    crudo = zlib.decompress(idat)
    paso = 1 + ancho * 4
    assert len(crudo) == alto * paso
    pixel = lambda x, y: tuple(crudo[y * paso + 1 + x * 4:y * paso + 5 + x * 4])  # noqa: E731
    return ancho, alto, pixel


class TestIcono:
    def test_es_un_png_valido_y_cuadrado_del_tamano_pedido(self):
        from claude_batch_monitor.gui.icono import png_del_icono
        for tamano in (16, 64):
            ancho, alto, _ = _leer_png(png_del_icono(tamano))
            assert (ancho, alto) == (tamano, tamano)

    def test_las_esquinas_son_transparentes_el_cuerpo_azul_y_las_barras_blancas(self):
        from claude_batch_monitor.gui.icono import png_del_icono
        _, _, pixel = _leer_png(png_del_icono(64))
        assert pixel(0, 0)[3] == 0                                     # esquina fuera del cuadrado redondeado
        r, g, b, a = pixel(32, 56)
        assert a == 255 and b > r and b > 200                           # cuerpo azul y opaco
        assert pixel(20, 21) == (255, 255, 255, 255)                    # primera barra, blanca

    def test_es_determinista_y_no_toca_el_disco(self):
        from claude_batch_monitor.gui.icono import png_del_icono
        assert png_del_icono(32) == png_del_icono(32)

    def test_pillow_lo_abre_para_la_bandeja(self):
        pytest.importorskip("PIL")
        from claude_batch_monitor.gui.icono import imagen_pil
        img = imagen_pil(64)
        assert img.size == (64, 64) and img.mode == "RGBA" and img.getpixel((0, 0))[3] == 0

    def test_la_bandeja_reutiliza_el_mismo_dibujo(self):
        pytest.importorskip("PIL")
        assert tray._imagen_de_bandeja().size == (64, 64)


class TestDisponible:
    def test_con_las_librerias_instaladas(self, pystray_falso):
        assert tray.bandeja_disponible() is True

    def test_sin_pystray(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pystray", None)       # None en sys.modules = ImportError al importar
        assert tray.bandeja_disponible() is False

    def test_sin_pillow(self, pystray_falso, monkeypatch):
        monkeypatch.setitem(sys.modules, "PIL.Image", None)
        assert tray.bandeja_disponible() is False


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
