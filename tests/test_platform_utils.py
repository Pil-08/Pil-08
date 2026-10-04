"""Anti-suspensión y abrir carpetas, sin tocar el sistema de verdad (todo se sustituye por falsos)."""

import os
import sys
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
