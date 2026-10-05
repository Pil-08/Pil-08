"""Anti-suspensión y abrir carpetas, sin tocar el sistema de verdad (todo se sustituye por falsos)."""

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from claude_batch_monitor import platform_utils as pu
from claude_batch_monitor.platform_utils import KeepAwake, abrir_ruta

# Estas pruebas SÍ ejercitan `KeepAwake` real: el resto de la suite lo anula (ver conftest).
pytestmark = pytest.mark.keepawake_real

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


class ProcesoFalso:
    def __init__(self, args, **kw):
        self.args, self.kw, self.terminado = args, kw, False

    def terminate(self):
        self.terminado = True


@pytest.fixture
def procesos(monkeypatch):
    creados = []

    def popen(args, **kw):
        p = ProcesoFalso(args, **kw)
        creados.append(p)
        return p

    monkeypatch.setattr(pu.subprocess, "Popen", popen)
    return creados


def sistema(monkeypatch, nombre):
    monkeypatch.setattr(pu.platform, "system", lambda: nombre)


class TestKeepAwake:
    def test_en_windows_activa_y_suelta_el_estado_de_ejecucion(self, monkeypatch):
        import ctypes
        llamadas = []

        class Kernel32:
            @staticmethod
            def SetThreadExecutionState(flags):
                llamadas.append(flags)

        class Falso:
            kernel32 = Kernel32()

        monkeypatch.setattr(ctypes, "windll", Falso(), raising=False)
        sistema(monkeypatch, "Windows")
        k = KeepAwake()
        k.enable()
        k.disable()
        assert llamadas == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]

    def test_soltar_sin_haber_activado_no_toca_nada(self, monkeypatch, procesos):
        sistema(monkeypatch, "Linux")
        KeepAwake().disable()
        assert procesos == []

    def test_en_macos_usa_caffeinate_atado_al_proceso(self, monkeypatch, procesos):
        sistema(monkeypatch, "Darwin")
        k = KeepAwake()
        k.enable()
        assert procesos[0].args == ["caffeinate", "-i", "-w", str(os.getpid())]
        k.disable()
        assert procesos[0].terminado

    def test_en_linux_usa_systemd_inhibit(self, monkeypatch, procesos):
        sistema(monkeypatch, "Linux")
        monkeypatch.setattr(pu.shutil, "which", lambda n: "/usr/bin/systemd-inhibit")
        k = KeepAwake()
        k.enable()
        args = procesos[0].args
        assert args[0] == "systemd-inhibit" and "--what=idle:sleep" in args and args[-2:] == ["sleep", "infinity"]
        k.disable()
        assert procesos[0].terminado

    def test_en_linux_sin_systemd_no_hace_nada(self, monkeypatch, procesos):
        sistema(monkeypatch, "Linux")
        monkeypatch.setattr(pu.shutil, "which", lambda n: None)
        k = KeepAwake()
        k.enable()
        k.disable()
        assert procesos == []

    def test_si_falla_no_se_propaga(self, monkeypatch):
        sistema(monkeypatch, "Darwin")

        def roto(*a, **kw):
            raise OSError("caffeinate no existe")

        monkeypatch.setattr(pu.subprocess, "Popen", roto)
        k = KeepAwake()
        k.enable()           # un lote nunca debe pararse porque no se pueda evitar la suspensión
        k.disable()

    def test_soltar_dos_veces_es_inofensivo(self, monkeypatch, procesos):
        sistema(monkeypatch, "Darwin")
        k = KeepAwake()
        k.enable()
        k.disable()
        k.disable()
        assert len(procesos) == 1

    def test_como_contexto(self, monkeypatch, procesos):
        sistema(monkeypatch, "Darwin")
        with KeepAwake():
            assert procesos and not procesos[0].terminado
        assert procesos[0].terminado

    def test_se_suelta_aunque_el_bloque_falle(self, monkeypatch, procesos):
        sistema(monkeypatch, "Darwin")
        with pytest.raises(RuntimeError), KeepAwake():
            raise RuntimeError("el lote reventó")
        assert procesos[0].terminado


class TestAbrirRuta:
    def test_linux(self, monkeypatch, procesos):
        monkeypatch.setattr(sys, "platform", "linux")
        assert abrir_ruta(Path("/tmp/x")) is True
        assert procesos[0].args == ["xdg-open", "/tmp/x"]

    def test_macos(self, monkeypatch, procesos):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert abrir_ruta(Path("/tmp/x")) is True
        assert procesos[0].args == ["open", "/tmp/x"]

    def test_windows(self, monkeypatch):
        abiertas = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(os, "startfile", abiertas.append, raising=False)
        assert abrir_ruta(Path("C:/datos")) is True
        assert abiertas == [Path("C:/datos")]

    def test_si_no_se_puede_abrir_devuelve_false(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

        def roto(*a, **kw):
            raise FileNotFoundError("xdg-open")

        monkeypatch.setattr(pu.subprocess, "Popen", roto)
        assert abrir_ruta(Path("/tmp/x")) is False


class TestComandosParaMostrar:
    """Lo que se le enseña a la persona para copiar en una terminal debe funcionar en su sistema."""

    def test_python_se_llama_py_en_windows_y_python3_en_el_resto(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert pu.nombre_de_python() == "py"
        for sistema in ("linux", "darwin"):
            monkeypatch.setattr(sys, "platform", sistema)
            assert pu.nombre_de_python() == "python3"

    def test_instalar_una_libreria(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert pu.comando_para_instalar("anthropic") == "python3 -m pip install anthropic"

    def test_ejecutar_el_paquete(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(pu, "__package__", "claude_batch_monitor")
        assert pu.comando_para_ejecutar() == "python3 -m claude_batch_monitor"

    def test_ejecutar_el_archivo_unico_usa_su_nombre(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(pu, "__package__", "")
        monkeypatch.setattr(pu, "__file__", "C:/Users/ana/Descargas/claude_batch_monitor.py")
        assert pu.comando_para_ejecutar() == "py claude_batch_monitor.py"

    def test_ejecutar_el_ejecutable_descargado(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", "/opt/cbm/claude-batch-monitor")
        monkeypatch.setattr(sys, "platform", "linux")
        assert pu.comando_para_ejecutar() == "./claude-batch-monitor"
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "executable", "C:/cbm/claude-batch-monitor.exe")
        assert pu.comando_para_ejecutar() == "claude-batch-monitor.exe"


def _salida(texto: str, codigo: int = 0):
    return types.SimpleNamespace(stdout=texto, stderr="", returncode=codigo)


class TestTemaDelSistema:
    def test_windows_claro_y_oscuro(self, monkeypatch):
        valores = {}

        class Clave:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        falso = types.SimpleNamespace(
            HKEY_CURRENT_USER="HKCU",
            OpenKey=lambda raiz, ruta: (valores.update(ruta=ruta, raiz=raiz), Clave())[1],
            QueryValueEx=lambda clave, nombre: (valores["usa_claro"], 1),
        )
        monkeypatch.setitem(sys.modules, "winreg", falso)
        monkeypatch.setattr(sys, "platform", "win32")
        valores["usa_claro"] = 0
        assert pu.tema_del_sistema() == "oscuro"
        assert valores["ruta"].endswith(r"Themes\Personalize") and valores["raiz"] == "HKCU"
        valores["usa_claro"] = 1
        assert pu.tema_del_sistema() == "claro"

    def test_macos_oscuro_si_el_sistema_lo_dice_y_claro_si_el_ajuste_no_existe(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(pu.subprocess, "run", lambda *a, **k: _salida("Dark\n"))
        assert pu.tema_del_sistema() == "oscuro"
        # en modo claro, `defaults` falla con «The domain/default pair ... does not exist»
        monkeypatch.setattr(pu.subprocess, "run", lambda *a, **k: _salida("", 1))
        assert pu.tema_del_sistema() == "claro"

    def test_linux_gnome(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(pu.shutil, "which", lambda nombre: "/usr/bin/gsettings")
        monkeypatch.delenv("GTK_THEME", raising=False)
        pedidos = []
        monkeypatch.setattr(pu.subprocess, "run", lambda orden, **k: pedidos.append(orden) or _salida("'prefer-dark'\n"))
        assert pu.tema_del_sistema() == "oscuro"
        assert pedidos == [["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]]
        monkeypatch.setattr(pu.subprocess, "run", lambda *a, **k: _salida("'default'\n"))
        assert pu.tema_del_sistema() == "claro"

    def test_linux_sin_gsettings_mira_gtk_theme(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(pu.shutil, "which", lambda nombre: None)
        monkeypatch.setenv("GTK_THEME", "Adwaita:dark")
        assert pu.tema_del_sistema() == "oscuro"
        monkeypatch.setenv("GTK_THEME", "Adwaita")
        assert pu.tema_del_sistema() == "claro"
        monkeypatch.delenv("GTK_THEME")
        assert pu.tema_del_sistema() == "claro"

    @pytest.mark.parametrize("sistema", ["win32", "darwin", "linux"])
    def test_si_algo_falla_se_queda_en_claro(self, monkeypatch, sistema):
        def roto(*a, **k):
            raise subprocess.TimeoutExpired("x", 3)

        monkeypatch.setattr(sys, "platform", sistema)
        monkeypatch.setattr(pu.subprocess, "run", roto)
        monkeypatch.setattr(pu.shutil, "which", lambda nombre: "/usr/bin/gsettings")
        monkeypatch.setitem(sys.modules, "winreg", None)  # `import winreg` falla
        monkeypatch.delenv("GTK_THEME", raising=False)
        assert pu.tema_del_sistema() == "claro"
