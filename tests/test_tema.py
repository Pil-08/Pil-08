"""Las paletas clara y oscura: que se elijan bien y que todo se lea en las dos.

(Elegir el tema en la configuración y detectar el del sistema se prueban en test_config.py y
test_platform_utils.py; las ventanas con cada tema, en test_gui_simple.py y test_gui_avanzada.py.)
"""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")  # el módulo de estilo importa tkinter, aunque aquí no se abra ninguna ventana

from claude_batch_monitor.gui import estilo  # noqa: E402


def luminancia(color: str) -> float:
    r, g, b = (int(color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    canal = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * canal(r) + 0.7152 * canal(g) + 0.0722 * canal(b)


def contraste(a: str, b: str) -> float:
    la, lb = sorted((luminancia(a), luminancia(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


PALETAS = [pytest.param(estilo.PALETA_CLARA, id="claro"), pytest.param(estilo.PALETA_OSCURA, id="oscuro")]


class TestPaletas:
    @pytest.mark.parametrize("p", PALETAS)
    def test_el_texto_se_lee_sobre_cada_fondo(self, p):
        """4,5:1 es el mínimo de las pautas de accesibilidad (WCAG AA) para texto normal."""
        sobre_fondo = {"texto": p.texto, "apagado": p.apagado, "acento_texto": p.acento_texto,
                       "ok": p.ok, "aviso": p.aviso, "error": p.error}
        for nombre, color in sobre_fondo.items():
            assert contraste(color, p.fondo) >= 4.5, f"{nombre} sobre el fondo"
            assert contraste(color, p.superficie) >= 4.5, f"{nombre} sobre las tarjetas"
        assert contraste(p.gris, p.superficie) >= 4.5
        assert contraste(p.texto, p.boton) >= 4.5 and contraste(p.texto, p.boton_hover) >= 4.5
        assert contraste(p.apagado, p.boton) >= 4.5, "cabeceras de las tablas"
        assert contraste(p.texto, p.acento_suave) >= 4.5, "texto seleccionado"
        assert contraste(p.texto, p.codigo_fondo) >= 4.5 and contraste(p.acento_texto, p.codigo_fondo) >= 4.5

    @pytest.mark.parametrize("p", PALETAS)
    def test_los_avisos_se_leen_sobre_su_propio_fondo(self, p):
        assert contraste(p.aviso, p.banner_aviso_fondo) >= 4.5
        assert contraste(p.error, p.banner_error_fondo) >= 4.5
        assert contraste(p.acento_texto, p.banner_info_fondo) >= 4.5
        for fondo in (p.banner_aviso_fondo, p.banner_error_fondo, p.banner_info_fondo):
            assert contraste(p.apagado, fondo) >= 4.5, "el detalle en gris dentro de los avisos"

    @pytest.mark.parametrize("p", PALETAS)
    def test_el_boton_principal_se_lee_en_reposo_y_al_pasar_el_raton(self, p):
        assert contraste("#ffffff", p.acento) >= 4.5
        assert contraste("#ffffff", p.acento_hover) >= 4.5

    def test_solo_la_oscura_dice_que_lo_es(self):
        assert estilo.PALETA_OSCURA.oscura and not estilo.PALETA_CLARA.oscura
        assert estilo.PALETA is estilo.PALETA_CLARA

    def test_las_dos_tienen_los_mismos_campos_con_un_color_valido(self):
        for p in (estilo.PALETA_CLARA, estilo.PALETA_OSCURA):
            for nombre, valor in vars(p).items():
                if nombre != "oscura":
                    assert isinstance(valor, str) and len(valor) == 7 and valor.startswith("#"), nombre
                    int(valor[1:], 16)


class TestElegirPaleta:
    def test_claro_y_oscuro_se_eligen_tal_cual(self, monkeypatch):
        monkeypatch.setattr(estilo, "tema_del_sistema", lambda: pytest.fail("no debía mirarse el sistema"))
        assert estilo.elegir_paleta("claro") is estilo.PALETA_CLARA
        assert estilo.elegir_paleta("oscuro") is estilo.PALETA_OSCURA

    def test_auto_sigue_al_sistema(self, monkeypatch):
        monkeypatch.setattr(estilo, "tema_del_sistema", lambda: "oscuro")
        assert estilo.elegir_paleta("auto") is estilo.elegir_paleta() is estilo.PALETA_OSCURA
        monkeypatch.setattr(estilo, "tema_del_sistema", lambda: "claro")
        assert estilo.elegir_paleta("auto") is estilo.PALETA_CLARA

    def test_un_valor_raro_no_rompe_nada(self):
        assert estilo.elegir_paleta("rosa") is estilo.PALETA_CLARA
