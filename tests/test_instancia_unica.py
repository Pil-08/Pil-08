"""Una sola copia del programa gastando a la vez: el bloqueo, la terminal y la ventana."""

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from claude_batch_monitor import cli, instancia_unica, paths
from claude_batch_monitor.instancia_unica import BloqueoDeInstancia, aviso_de_otra_copia

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture
def bloqueo(tmp_path):
    return BloqueoDeInstancia(tmp_path / "x.lock")


class TestElBloqueo:
    def test_la_primera_copia_entra_y_la_segunda_no(self, tmp_path):
        a, b = BloqueoDeInstancia(tmp_path / "x.lock"), BloqueoDeInstancia(tmp_path / "x.lock")
        assert a.adquirir() is True
        assert b.adquirir() is False
        a.liberar()

    def test_al_liberar_puede_entrar_otra(self, tmp_path):
        a, b = BloqueoDeInstancia(tmp_path / "x.lock"), BloqueoDeInstancia(tmp_path / "x.lock")
        assert a.adquirir()
        a.liberar()
        assert b.adquirir() is True
        b.liberar()

    def test_quien_se_encuentra_el_bloqueo_ocupado_sabe_de_quien_es(self, tmp_path):
        a, b = BloqueoDeInstancia(tmp_path / "x.lock"), BloqueoDeInstancia(tmp_path / "x.lock")
        a.adquirir()
        b.adquirir()
        assert b.dueno == str(os.getpid())
        a.liberar()

    def test_adquirir_dos_veces_la_misma_no_se_bloquea_a_si_misma(self, bloqueo):
        assert bloqueo.adquirir() and bloqueo.adquirir()
        bloqueo.liberar()
        bloqueo.liberar()  # soltar de más tampoco pasa nada

    def test_como_gestor_de_contexto(self, tmp_path):
        with BloqueoDeInstancia(tmp_path / "x.lock"):
            assert BloqueoDeInstancia(tmp_path / "x.lock").adquirir() is False
        otra = BloqueoDeInstancia(tmp_path / "x.lock")
        assert otra.adquirir() is True
        otra.liberar()

    def test_por_omision_usa_la_carpeta_de_datos_actual(self):
        assert BloqueoDeInstancia().ruta == paths.DATA_DIR / instancia_unica.ARCHIVO_DE_BLOQUEO

    def test_el_fichero_no_tiene_nada_secreto(self, bloqueo):
        bloqueo.adquirir()
        contenido = bloqueo.ruta.read_bytes()[instancia_unica._RESERVADO:]
        bloqueo.liberar()
        assert contenido.decode().isdigit()  # solo el número de proceso


class TestElSeguroNuncaEstorba:
    def test_si_no_se_puede_crear_el_fichero_se_deja_pasar(self, tmp_path):
        assert BloqueoDeInstancia(tmp_path / "no_existe" / "x.lock").adquirir() is True

    def test_si_la_ruta_es_una_carpeta_se_deja_pasar(self, tmp_path):
        assert BloqueoDeInstancia(tmp_path).adquirir() is True

    def test_si_el_sistema_falla_por_dentro_se_deja_pasar(self, bloqueo, monkeypatch):
        def roto(archivo):
            raise RuntimeError("el sistema se ha vuelto loco")

        monkeypatch.setattr(BloqueoDeInstancia, "_bloquear", staticmethod(roto))
        assert bloqueo.adquirir() is True

    def test_sin_ningun_mecanismo_para_bloquear_se_deja_pasar(self, bloqueo, monkeypatch):
        monkeypatch.setattr(instancia_unica, "fcntl", None)
        monkeypatch.setattr(instancia_unica, "msvcrt", None)
        assert bloqueo.adquirir() is True and BloqueoDeInstancia(bloqueo.ruta).adquirir() is True


class TestEnWindows:
    """Windows no se puede probar aquí: se comprueba que se le pide al sistema lo que hay que pedirle."""

    class FalsoMsvcrt:
        LK_NBLCK, LK_UNLCK = 2, 0
        ocupado = False

        def __init__(self):
            self.llamadas = []

        def locking(self, fd, modo, n):
            self.llamadas.append((modo, n))
            if modo == self.LK_NBLCK and self.ocupado:
                raise PermissionError(13, "Permission denied")

    def test_se_bloquea_un_byte_y_se_suelta_al_terminar(self, bloqueo, monkeypatch):
        falso = self.FalsoMsvcrt()
        monkeypatch.setattr(instancia_unica, "fcntl", None)
        monkeypatch.setattr(instancia_unica, "msvcrt", falso)
        assert bloqueo.adquirir() is True
        bloqueo.liberar()
        assert falso.llamadas == [(falso.LK_NBLCK, 1), (falso.LK_UNLCK, 1)]
        assert bloqueo.ruta.stat().st_size >= instancia_unica._RESERVADO

    def test_si_esta_ocupado_dice_que_no(self, bloqueo, monkeypatch):
        falso = self.FalsoMsvcrt()
        falso.ocupado = True
        monkeypatch.setattr(instancia_unica, "fcntl", None)
        monkeypatch.setattr(instancia_unica, "msvcrt", falso)
        assert bloqueo.adquirir() is False


@pytest.mark.skipif(sys.platform == "win32", reason="el bloqueo entre procesos se prueba con señales de POSIX")
class TestEntreProcesos:
    def _copia_que_bloquea(self, ruta, tmp_path):
        codigo = textwrap.dedent(
            """
            import sys, time
            from pathlib import Path
            from claude_batch_monitor.instancia_unica import BloqueoDeInstancia
            b = BloqueoDeInstancia(Path(sys.argv[1]))
            print("listo" if b.adquirir() else "ocupado", flush=True)
            time.sleep(120)
            """
        )
        entorno = {**os.environ, "CLAUDE_MONITOR_HOME": str(tmp_path / "hijo"), "PYTHONPATH": str(RAIZ)}
        entorno.pop("CBM_ARCHIVO_UNICO", None)
        hijo = subprocess.Popen([sys.executable, "-c", codigo, str(ruta)], stdout=subprocess.PIPE, env=entorno, text=True)
        assert hijo.stdout.readline().strip() == "listo"
        return hijo

    def test_otro_proceso_con_el_bloqueo_impide_entrar_y_si_muere_de_golpe_se_libera(self, tmp_path):
        ruta = tmp_path / "x.lock"
        hijo = self._copia_que_bloquea(ruta, tmp_path)
        try:
            yo = BloqueoDeInstancia(ruta)
            assert yo.adquirir() is False and yo.dueno == str(hijo.pid)
            hijo.send_signal(signal.SIGKILL)          # sin avisar: no hay ocasión de soltar nada
            hijo.wait(timeout=10)
            assert yo.adquirir() is True              # lo suelta el sistema operativo
            yo.liberar()
        finally:
            if hijo.poll() is None:
                hijo.kill()
                hijo.wait()
            hijo.stdout.close()

    def test_un_proceso_que_lanza_a_otro_no_le_pasa_el_bloqueo(self, tmp_path):
        """Si el bloqueo se heredara, `pip install` o `systemd-inhibit` lo mantendrían vivo tras morir el programa."""
        ruta = tmp_path / "x.lock"
        codigo = textwrap.dedent(
            """
            import subprocess, sys, time
            from pathlib import Path
            from claude_batch_monitor.instancia_unica import BloqueoDeInstancia
            b = BloqueoDeInstancia(Path(sys.argv[1]))
            assert b.adquirir()
            nieto = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            print(nieto.pid, flush=True)
            time.sleep(120)
            """
        )
        entorno = {**os.environ, "CLAUDE_MONITOR_HOME": str(tmp_path / "hijo"), "PYTHONPATH": str(RAIZ)}
        hijo = subprocess.Popen([sys.executable, "-c", codigo, str(ruta)], stdout=subprocess.PIPE, env=entorno, text=True)
        nieto = int(hijo.stdout.readline())
        try:
            hijo.send_signal(signal.SIGKILL)
            hijo.wait(timeout=10)
            os.kill(nieto, 0)                          # el nieto sigue vivo…
            yo = BloqueoDeInstancia(ruta)
            assert yo.adquirir() is True               # …y aun así el bloqueo está libre
            yo.liberar()
        finally:
            try:
                os.kill(nieto, signal.SIGKILL)
            except OSError:
                pass
            if hijo.poll() is None:
                hijo.kill()
                hijo.wait()
            hijo.stdout.close()


class TestLaTerminal:
    ARGS = ["--mensaje", "hola", "--modelo", "claude-haiku-4-5"]

    def test_con_otra_copia_en_marcha_no_envia_nada_y_sale_con_5(self, api, esperas, capsys):
        with BloqueoDeInstancia():
            codigo = cli.main_cli(self.ARGS)
        cap = capsys.readouterr()
        assert codigo == cli.EXIT_USO == 5
        assert "otra copia" in cap.err and str(os.getpid()) in cap.err
        assert api.peticiones == []

    def test_sin_otra_copia_funciona_y_suelta_el_bloqueo(self, api, esperas, capsys):
        codigo = cli.main_cli(self.ARGS)
        assert codigo == 0 and len(api.cobros) == 1
        otra = BloqueoDeInstancia()
        assert otra.adquirir() is True                # la terminal lo soltó al acabar
        otra.liberar()

    def test_lo_que_no_gasta_no_necesita_el_bloqueo(self, api, esperas, capsys):
        with BloqueoDeInstancia():
            assert cli.main_cli(["--version"]) == 0
            assert cli.main_cli(["--listar"]) == 0
            assert cli.main_cli([*self.ARGS, "--dry-run"]) == 0
        assert api.peticiones_a("/v1/messages") == []        # (el --dry-run solo cuenta tokens)

    def test_el_aviso_dice_que_hacer(self):
        texto = aviso_de_otra_copia("1234")
        assert "1234" in texto and "CLAUDE_MONITOR_HOME" in texto and "bandeja" in texto
        assert "proceso" not in aviso_de_otra_copia("")

    def test_aunque_falle_la_ejecucion_se_suelta(self, api, esperas, monkeypatch):
        def revienta(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(cli, "_ejecutar_lote", revienta)
        with pytest.raises(RuntimeError):
            cli.main_cli(self.ARGS)
        otra = BloqueoDeInstancia()
        assert otra.adquirir() is True
        otra.liberar()


class TestLaVentana:
    def test_una_segunda_ventana_avisa_y_no_se_abre(self, raiz, monkeypatch):
        from tkinter import messagebox

        from claude_batch_monitor import gui

        avisos = []
        monkeypatch.setattr(messagebox, "showwarning", lambda titulo, texto, **k: avisos.append((titulo, texto)))

        def no_debe_abrirse(*a, **k):
            raise AssertionError("se abrió una segunda ventana")

        import claude_batch_monitor.gui.simple as simple

        monkeypatch.setattr(simple, "ejecutar_simple", no_debe_abrirse)
        with BloqueoDeInstancia():
            assert gui.abrir_interfaz([]) == gui.EXIT_YA_ABIERTO == 5
        assert len(avisos) == 1 and str(os.getpid()) in avisos[0][1]

    def test_la_ventana_suelta_el_bloqueo_al_cerrarse(self, raiz, monkeypatch):
        import claude_batch_monitor.gui.simple as simple
        from claude_batch_monitor import gui

        durante = []

        def ventana():
            durante.append(BloqueoDeInstancia().adquirir())   # desde dentro, el bloqueo está tomado
            return None

        monkeypatch.setattr(simple, "ejecutar_simple", ventana)
        assert gui.abrir_interfaz([]) == 0
        assert durante == [False]
        despues = BloqueoDeInstancia()
        assert despues.adquirir() is True
        despues.liberar()

    def test_cambiar_de_modo_no_suelta_el_bloqueo(self, raiz, monkeypatch):
        import claude_batch_monitor.gui.app as app
        import claude_batch_monitor.gui.simple as simple
        from claude_batch_monitor import gui

        vistos = []

        def simple_a_avanzada():
            vistos.append(("simple", BloqueoDeInstancia().adquirir()))
            return "avanzada"

        def avanzada():
            vistos.append(("avanzada", BloqueoDeInstancia().adquirir()))
            return None

        monkeypatch.setattr(simple, "ejecutar_simple", simple_a_avanzada)
        monkeypatch.setattr(app, "ejecutar_avanzada", avanzada)
        assert gui.abrir_interfaz([]) == 0
        assert vistos == [("simple", False), ("avanzada", False)]
