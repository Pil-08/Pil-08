"""Piezas comunes de las ventanas: el icono, el panel con desplazamiento y las ventanas secundarias."""

import types

import pytest

from claude_batch_monitor.gui import comun

tk = pytest.importorskip("tkinter")
pytestmark = pytest.mark.gui

from tkinter import ttk  # noqa: E402

from claude_batch_monitor.gui.estilo import aplicar_estilo  # noqa: E402

from .gui_comun import bombear  # noqa: E402


class TestIcono:
    def test_se_pone_el_icono_y_la_ventana_lo_conserva(self, raiz):
        assert comun.poner_icono(raiz) is True
        assert raiz._icono_app.width() == 64           # sujeto: si no, tkinter lo suelta y la ventana queda sin icono

    def test_si_algo_falla_no_impide_abrir_la_ventana(self, raiz, monkeypatch):
        def roto(tamano):
            raise RuntimeError("sin imagen")
        monkeypatch.setattr(comun, "png_del_icono", roto)
        assert comun.poner_icono(raiz) is False


def _panel(raiz, filas=60, alto=200):
    p, _ = aplicar_estilo(raiz)
    raiz.geometry("500x400+0+0")
    d = comun.Desplazable(raiz, p)
    d.place(x=0, y=0, width=480, height=alto)
    for i in range(filas):
        ttk.Label(d.interior, text=f"Fila {i}").pack(anchor="w")
    raiz.update()
    return d


def evento(d, **kw):
    base = dict(num=0, delta=0, x_root=d.winfo_rootx() + 20, y_root=d.winfo_rooty() + 20)
    base.update(kw)
    return types.SimpleNamespace(**base)


class TestDesplazable:
    def test_la_region_se_ajusta_al_contenido(self, raiz):
        d = _panel(raiz)
        region = [int(x) for x in str(d._lienzo.cget("scrollregion")).split()]
        assert region[3] > 400                                       # 60 filas son mucho más altas que 200 px

    def test_el_interior_ocupa_todo_el_ancho(self, raiz):
        d = _panel(raiz)
        assert abs(d.interior.winfo_width() - d._lienzo.winfo_width()) <= 2

    @pytest.mark.parametrize("campos, sentido", [
        (dict(num=5), +1), (dict(num=4), -1), (dict(delta=-120), +1), (dict(delta=120), -1),
    ])
    def test_la_rueda_desplaza_en_el_sentido_correcto(self, raiz, campos, sentido):
        d = _panel(raiz)
        d._lienzo.yview_moveto(0.5)
        raiz.update()
        antes = d._lienzo.yview()[0]
        d._rueda(evento(d, **campos))
        raiz.update()
        despues = d._lienzo.yview()[0]
        assert (despues - antes) * sentido > 0

    def test_en_macos_los_valores_son_pequenos(self, raiz, monkeypatch):
        monkeypatch.setattr(comun.sys, "platform", "darwin")
        d = _panel(raiz)
        d._lienzo.yview_moveto(0.5)
        raiz.update()
        antes = d._lienzo.yview()[0]
        d._rueda(evento(d, delta=-2))
        raiz.update()
        assert d._lienzo.yview()[0] > antes

    def test_un_delta_de_rueda_fina_tambien_mueve(self, raiz):
        d = _panel(raiz)
        d._lienzo.yview_moveto(0.5)
        raiz.update()
        antes = d._lienzo.yview()[0]
        d._rueda(evento(d, delta=-30))            # ratones de alta resolución: menos de 120
        raiz.update()
        assert d._lienzo.yview()[0] > antes

    def test_si_el_puntero_esta_fuera_no_se_mueve(self, raiz):
        d = _panel(raiz)
        antes = d._lienzo.yview()[0]
        d._rueda(evento(d, num=5, x_root=d.winfo_rootx() + 495, y_root=d.winfo_rooty() + 380))
        raiz.update()
        assert d._lienzo.yview()[0] == antes

    def test_sobre_un_cuadro_de_texto_la_rueda_es_del_cuadro(self, raiz):
        d = _panel(raiz, filas=5)
        caja = tk.Text(d.interior, height=3)
        caja.pack()
        for _ in range(60):
            ttk.Label(d.interior, text="más").pack()
        raiz.update()
        antes = d._lienzo.yview()[0]
        d._rueda(evento(d, num=5, x_root=caja.winfo_rootx() + 5, y_root=caja.winfo_rooty() + 5))
        raiz.update()
        assert d._lienzo.yview()[0] == antes

    def test_si_todo_cabe_no_se_desplaza(self, raiz):
        d = _panel(raiz, filas=2, alto=300)
        d._rueda(evento(d, num=5))
        raiz.update()
        assert d._lienzo.yview() == (0.0, 1.0)

    def test_oculto_no_reacciona(self, raiz):
        d = _panel(raiz)
        d.place_forget()
        raiz.update()
        d._rueda(evento(d, num=5))               # no debe lanzar ni moverse

    def test_destruido_no_lanza(self, raiz):
        d = _panel(raiz)
        e = evento(d, num=5)
        d.destroy()
        d._rueda(e)                              # la ventana sigue escuchando la rueda: no debe fallar

    def test_la_rueda_llega_desde_un_evento_real(self, raiz):
        d = _panel(raiz)
        raiz.update()
        hijo = d.interior.winfo_children()[3]
        antes = d._lienzo.yview()[0]
        hijo.event_generate("<Button-5>", x=3, y=3, rootx=hijo.winfo_rootx() + 3, rooty=hijo.winfo_rooty() + 3)
        bombear(raiz, timeout=0.2)
        assert d._lienzo.yview()[0] >= antes


class TestModal:
    def test_se_crea_encima_de_la_principal_y_se_cierra_con_escape(self, raiz):
        p, _ = aplicar_estilo(raiz)
        app = types.SimpleNamespace(root=raiz, p=p)
        m = comun.Modal(app, "Título", redimensionable=False)
        m.mostrar()
        assert m.title() == "Título" and m.winfo_exists()
        m.focus_force()                          # los eventos de teclado solo llegan a la ventana con el foco
        raiz.update()
        m.event_generate("<Escape>")
        bombear(raiz, timeout=0.2)
        assert not m.winfo_exists()
