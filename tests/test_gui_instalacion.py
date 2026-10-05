"""La ventanita que ofrece instalar la librería que falta (con una instalación simulada)."""

import threading

import pytest

from claude_batch_monitor.gui.i18n import fijar_idioma, t
from claude_batch_monitor.instalacion import DialogoInstalacion, Resultado, comando_visible

from .gui_comun import bombear

tk = pytest.importorskip("tkinter")
pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def en_espanol():
    fijar_idioma("es")
    yield
    fijar_idioma("es")


def existe(raiz):
    try:
        return bool(raiz.winfo_exists())
    except tk.TclError:
        return False


def dialogo(raiz, resultado=None, reinicios=None, espera=None):
    def instalar():
        if espera is not None:
            espera.wait(5)
        return resultado or Resultado(True, python="/py")

    return DialogoInstalacion(raiz, instalar_fn=instalar,
                              reiniciar_fn=lambda python: (reinicios.append(python) if reinicios is not None else None) or 77)


class TestDialogoInstalacion:
    def test_explica_que_falta_y_enseña_el_comando_exacto(self, raiz):
        d = dialogo(raiz)
        assert raiz.title() == "Falta una pieza"
        assert d.comando.get() == comando_visible() and comando_visible().endswith("-m pip install anthropic")
        assert d.boton_instalar.cget("text") == "Instalar ahora" and d.boton_salir.cget("text") == "Salir"

    def test_los_botones_se_bloquean_mientras_instala(self, raiz):
        espera = threading.Event()
        d = dialogo(raiz, espera=espera)
        d.instalar()
        raiz.update()
        assert "disabled" in d.boton_instalar.state() and "disabled" in d.boton_salir.state()
        assert d.estado.get() == t("install_working")
        espera.set()
        assert bombear(raiz, lambda: d.resultado is not None)

    def test_al_terminar_bien_lo_dice_y_se_cierra_sola(self, raiz):
        d = dialogo(raiz)
        d.instalar()
        assert bombear(raiz, lambda: d.resultado is not None)
        assert d.estado.get() == "Listo. Abriendo el programa…" and d.codigo == 0
        assert bombear(raiz, lambda: not existe(raiz), timeout=4)

    def test_si_falla_se_explica_y_se_puede_reintentar_o_copiar(self, raiz):
        d = dialogo(raiz, resultado=Resultado(False, "No se pudo descargar."))
        d.instalar()
        assert bombear(raiz, lambda: d.resultado is not None)
        assert "No se pudo instalar." in d.estado.get() and "No se pudo descargar." in d.estado.get()
        assert d.boton_instalar.cget("text") == "Reintentar" and "disabled" not in d.boton_instalar.state()
        assert "disabled" not in d.boton_salir.state() and d.boton_copiar.winfo_manager() == "pack"
        d.copiar()
        assert raiz.clipboard_get() == comando_visible()
        assert d.codigo == 1

    def test_ejecutar_devuelve_lo_que_diga_el_reinicio(self, raiz):
        reinicios = []
        d = dialogo(raiz, reinicios=reinicios)
        raiz.after(100, d.instalar)
        raiz.after(15000, raiz.destroy)           # red de seguridad: la prueba nunca se queda colgada
        assert d.ejecutar() == 77 and reinicios == ["/py"]

    def test_cerrar_la_ventana_cancela(self, raiz):
        reinicios = []
        d = dialogo(raiz, reinicios=reinicios)
        raiz.after(100, d.salir)
        raiz.after(15000, raiz.destroy)
        assert d.ejecutar() == 1 and reinicios == []

    def test_en_ingles_con_el_idioma_del_sistema(self, raiz):
        fijar_idioma("en")
        d = dialogo(raiz)
        assert raiz.title() == "One piece is missing" and d.boton_instalar.cget("text") == "Install now"
