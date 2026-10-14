"""Instalar la librería que falta: qué se ejecuta, cómo se explica un fallo y cómo se vuelve a abrir el programa.

Nada de esto instala nada de verdad: `subprocess.run`, `os.execv` y las ventanas se sustituyen por falsos.
"""

import os
import subprocess
import sys
import types

import pytest

from claude_batch_monitor import instalacion as ins
from claude_batch_monitor.instalacion import Resultado


class Ejecutor:
    """Un `subprocess.run` falso con un guion: cada llamada devuelve (código, salida) y se anota."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def __call__(self, orden, **kw):
        self.llamadas.append(list(orden))
        r = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(r, Exception):
            raise r
        codigo, salida = r
        return types.SimpleNamespace(returncode=codigo, stdout=salida, stderr="")


PEP668 = "error: externally-managed-environment\n× This environment is externally managed"


def _usuario_de_la_api():
    """Alguien que ya usa la API (hay configuración y no es el plan): su librería hace falta."""
    from claude_batch_monitor.config import ConfigManager, Configuracion

    ConfigManager.guardar(Configuracion())


class TestDeteccion:
    def test_falta_cuando_no_se_puede_importar(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "anthropic", None)
        assert ins.falta_anthropic() is True

    def test_no_falta_cuando_esta(self):
        assert ins.falta_anthropic() is False

    @pytest.mark.parametrize("argv, terminal, esperado", [
        ([], False, True),                                   # la ventana siempre la necesita
        (["--cli", "--mensaje", "x"], True, True),
        (["--cli", "--reanudar", "abc"], True, True),
        (["--version"], True, False),
        (["--help"], True, False), (["-h"], True, False),
        (["--cli", "--mensaje", "x", "--dry-run"], True, False),   # estimar no habla con la API
        (["--cli", "--listar"], True, False),
    ])
    def test_cuando_se_necesita(self, argv, terminal, esperado):
        _usuario_de_la_api()
        assert ins.necesita_la_libreria(argv, terminal) is esperado


class TestComandos:
    def test_el_comando_instala_solo_anthropic(self):
        orden = ins.comando_pip("/usr/bin/python3", usuario=True)
        assert orden[:4] == ["/usr/bin/python3", "-m", "pip", "install"] and orden[-1] == "anthropic"
        assert "--user" in orden and orden.count("anthropic") == 1
        assert "--user" not in ins.comando_pip("/usr/bin/python3", usuario=False)

    def test_lo_que_se_le_ensena_a_la_persona_es_el_comando_de_siempre(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert ins.comando_visible() == "python3 -m pip install anthropic"
        monkeypatch.setattr(sys, "platform", "win32")
        assert ins.comando_visible() == "py -m pip install anthropic"

    def test_dentro_de_un_entorno_virtual_no_se_usa_user(self, monkeypatch):
        monkeypatch.setattr(sys, "prefix", "/venv")
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        assert ins.en_entorno_virtual() is True
        monkeypatch.setattr(sys, "prefix", "/usr")
        assert ins.en_entorno_virtual() is False

    def test_reiniciar_repite_la_linea_de_ordenes_original(self, monkeypatch):
        monkeypatch.setattr(sys, "orig_argv", ["python", "-X", "utf8", "programa.py", "--avanzado"], raising=False)
        assert ins.comando_para_reiniciar("/otro/python") == ["/otro/python", "-X", "utf8", "programa.py", "--avanzado"]

    def test_reiniciar_sin_orig_argv_usa_dash_m_si_se_lanzo_como_modulo(self, monkeypatch):
        monkeypatch.delattr(sys, "orig_argv", raising=False)
        falso = types.SimpleNamespace(__spec__=types.SimpleNamespace(name="claude_batch_monitor.__main__"))
        monkeypatch.setitem(sys.modules, "__main__", falso)
        monkeypatch.setattr(sys, "argv", ["x", "--simple"])
        assert ins.comando_para_reiniciar("py") == ["py", "-m", "claude_batch_monitor", "--simple"]

    def test_reiniciar_sin_orig_argv_ni_modulo_usa_el_script(self, monkeypatch):
        monkeypatch.delattr(sys, "orig_argv", raising=False)
        monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__spec__=None))
        monkeypatch.setattr(sys, "argv", ["/ruta/claude_batch_monitor.py", "--cli"])
        assert ins.comando_para_reiniciar("py") == ["py", "/ruta/claude_batch_monitor.py", "--cli"]

    def test_por_defecto_se_reinicia_con_el_mismo_python(self):
        assert ins.comando_para_reiniciar()[0] == sys.executable


class TestInstalar:
    def test_todo_bien(self):
        ejecutar = Ejecutor((0, "Successfully installed anthropic"))
        r = ins.instalar(ejecutar)
        assert r.ok and r.python == sys.executable and not r.entorno_propio
        assert len(ejecutar.llamadas) == 1 and ejecutar.llamadas[0][-1] == "anthropic"

    def test_fuera_de_un_entorno_virtual_se_instala_para_el_usuario(self, monkeypatch):
        monkeypatch.setattr(sys, "prefix", "/usr")
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        ejecutar = Ejecutor((0, ""))
        ins.instalar(ejecutar)
        assert "--user" in ejecutar.llamadas[0]

    def test_si_el_sistema_no_deja_se_usa_una_carpeta_propia(self):
        ejecutar = Ejecutor((1, PEP668), (0, ""), (0, "Successfully installed anthropic"))
        r = ins.instalar(ejecutar)
        assert r.ok and r.entorno_propio and r.python == str(ins.python_del_entorno_propio())
        crear, instalar = ejecutar.llamadas[1], ejecutar.llamadas[2]
        assert crear[1:3] == ["-m", "venv"] and crear[3] == str(ins.carpeta_entorno_propio())
        assert instalar[0] == r.python and instalar[-1] == "anthropic" and "--user" not in instalar

    def test_sin_pip_tambien_se_usa_la_carpeta_propia(self):
        ejecutar = Ejecutor((1, "/usr/bin/python3: No module named pip"), (0, ""), (0, ""))
        assert ins.instalar(ejecutar).entorno_propio is True

    def test_si_tampoco_se_puede_crear_la_carpeta_se_dice_que_paquete_falta(self):
        r = ins.instalar(Ejecutor((1, PEP668), (1, "ensurepip is not available")))
        assert not r.ok and "python3-venv" in r.detalle

    def test_si_la_instalacion_en_la_carpeta_propia_falla(self):
        r = ins.instalar(Ejecutor((1, PEP668), (0, ""), (1, "ERROR: boom")))
        assert not r.ok and "boom" in r.detalle

    def test_sin_internet_se_explica(self):
        r = ins.instalar(Ejecutor((1, "WARNING: Retrying ... Temporary failure in name resolution\nERROR: No matching distribution")))
        assert not r.ok and "conexión a internet" in r.detalle

    def test_otro_fallo_enseña_las_ultimas_lineas(self):
        salida = "\n".join(f"linea {i}" for i in range(40)) + "\nERROR: algo raro"
        r = ins.instalar(Ejecutor((1, salida)))
        assert not r.ok and "algo raro" in r.detalle and "linea 0" not in r.detalle

    def test_tiempo_agotado(self):
        r = ins.instalar(Ejecutor(subprocess.TimeoutExpired("pip", 900)))
        assert not r.ok and "Tiempo agotado" in r.detalle

    def test_no_se_puede_ejecutar_python(self):
        r = ins.instalar(Ejecutor(FileNotFoundError("no hay python")))
        assert not r.ok and "no hay python" in r.detalle


class TestEntornoPropio:
    def test_sin_carpeta_no_hay_entorno(self):
        assert ins.entorno_propio_con_anthropic(Ejecutor((0, ""))) is None

    def test_con_la_libreria_dentro_se_usa(self):
        python = ins.python_del_entorno_propio()
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        ejecutar = Ejecutor((0, ""))
        assert ins.entorno_propio_con_anthropic(ejecutar) == str(python)
        assert ejecutar.llamadas[0][1:] == ["-c", "import anthropic"]

    def test_si_la_carpeta_existe_pero_sin_la_libreria(self):
        python = ins.python_del_entorno_propio()
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        assert ins.entorno_propio_con_anthropic(Ejecutor((1, "ModuleNotFoundError"))) is None

    def test_la_ruta_cambia_segun_el_sistema(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert ins.python_del_entorno_propio().parts[-2:] == ("Scripts", "python.exe")
        monkeypatch.setattr(sys, "platform", "linux")
        assert ins.python_del_entorno_propio().parts[-2:] == ("bin", "python")

    def test_vive_dentro_de_la_carpeta_de_datos_del_programa(self):
        from claude_batch_monitor import paths
        assert ins.carpeta_entorno_propio() == paths.DATA_DIR / "venv"


class TestReiniciar:
    def test_en_posix_sustituye_el_proceso_y_deja_la_marca_anti_bucle(self, monkeypatch):
        llamadas = []
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "execv", lambda programa, orden: llamadas.append((programa, orden)))
        monkeypatch.setenv(ins.VARIABLE_REINTENTO, "")     # (al terminar se restaura a «no está»; con delenv se volvería a poner)
        ins.reiniciar_con("/otro/python")
        assert llamadas[0][0] == "/otro/python" and llamadas[0][1][0] == "/otro/python"
        assert os.environ[ins.VARIABLE_REINTENTO] == "1"

    def test_en_windows_lanza_otro_proceso(self, monkeypatch):
        lanzados = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(ins.subprocess, "Popen", lambda orden: lanzados.append(orden))
        monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("execv no sustituye el proceso en Windows"))
        monkeypatch.setenv(ins.VARIABLE_REINTENTO, "")
        assert ins.reiniciar_con("py.exe") == 0 and lanzados[0][0] == "py.exe"


class TestTerminal:
    def _ofrecer(self, respuesta=None, interactivo=True, resultado=None):
        salida, reinicios = [], []

        def entrada(pregunta):
            salida.append(pregunta)
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        codigo = ins.ofrecer_en_terminal(
            entrada=entrada, salida=salida.append, interactivo=interactivo,
            instalar_fn=lambda: resultado or Resultado(True, python="/py"),
            reiniciar_fn=lambda python: reinicios.append(python) or 0,
        )
        return codigo, "\n".join(salida), reinicios

    def test_sin_nadie_al_teclado_solo_explica(self):
        codigo, texto, reinicios = self._ofrecer(interactivo=False)
        assert codigo == 1 and ins.comando_visible() in texto and not reinicios
        assert "[s/N]" not in texto          # no pregunta: no hay a quién

    def test_si_dices_que_si_instala_y_reinicia(self):
        codigo, texto, reinicios = self._ofrecer("s")
        assert codigo == 0 and reinicios == ["/py"] and "[s/N]" in texto

    @pytest.mark.parametrize("respuesta", ["", "n", "no", "quizá"])
    def test_cualquier_otra_cosa_no_instala(self, respuesta):
        codigo, _, reinicios = self._ofrecer(respuesta)
        assert codigo == 1 and not reinicios

    def test_se_acepta_si_con_o_sin_tilde(self):
        assert self._ofrecer("  SÍ ")[0] == 0 and self._ofrecer("si")[0] == 0

    def test_si_se_cierra_la_entrada_no_instala(self):
        assert self._ofrecer(EOFError())[0] == 1

    def test_si_la_instalacion_falla_lo_cuenta_y_sale_con_error(self):
        codigo, texto, reinicios = self._ofrecer("s", resultado=Resultado(False, "sin internet"))
        assert codigo == 1 and "sin internet" in texto and not reinicios

    def test_los_textos_salen_en_el_idioma_del_sistema(self, monkeypatch):
        from claude_batch_monitor.gui.i18n import fijar_idioma
        fijar_idioma("en")
        try:
            _, texto, _ = self._ofrecer(interactivo=False)
            assert "Anthropic's official library" in texto
        finally:
            fijar_idioma("es")


class TestResolver:
    def test_si_ya_se_intento_no_se_entra_en_un_bucle(self, monkeypatch, capsys):
        monkeypatch.setenv(ins.VARIABLE_REINTENTO, "1")
        monkeypatch.setattr(ins, "reiniciar_con", lambda p=None: pytest.fail("no debe reiniciar otra vez"))
        assert ins.resolver_falta_de_la_libreria(terminal=True) == 1
        assert ins.comando_visible() in capsys.readouterr().err

    def test_si_hay_carpeta_propia_con_la_libreria_se_abre_con_ella(self, monkeypatch):
        monkeypatch.delenv(ins.VARIABLE_REINTENTO, raising=False)
        monkeypatch.setattr(ins, "entorno_propio_con_anthropic", lambda: "/propio/python")
        monkeypatch.setattr(ins, "reiniciar_con", lambda p=None: 42 if p == "/propio/python" else 0)
        assert ins.resolver_falta_de_la_libreria(terminal=False) == 42

    def test_en_la_terminal_se_pregunta_ahi(self, monkeypatch):
        monkeypatch.delenv(ins.VARIABLE_REINTENTO, raising=False)
        monkeypatch.setattr(ins, "entorno_propio_con_anthropic", lambda: None)
        monkeypatch.setattr(ins, "ofrecer_en_terminal", lambda: 11)
        monkeypatch.setattr(ins, "ofrecer_en_ventana", lambda: pytest.fail("no hay ventana en la terminal"))
        assert ins.resolver_falta_de_la_libreria(terminal=True) == 11

    def test_con_ventana_se_ofrece_en_la_ventana(self, monkeypatch):
        monkeypatch.delenv(ins.VARIABLE_REINTENTO, raising=False)
        monkeypatch.setattr(ins, "entorno_propio_con_anthropic", lambda: None)
        monkeypatch.setattr(ins, "ofrecer_en_ventana", lambda: 5)
        assert ins.resolver_falta_de_la_libreria(terminal=False) == 5

    def test_sin_pantalla_se_cae_a_la_terminal(self, monkeypatch):
        class TclError(Exception):
            pass

        def sin_pantalla():
            raise TclError("no display name and no $DISPLAY environment variable")

        monkeypatch.delenv(ins.VARIABLE_REINTENTO, raising=False)
        monkeypatch.setattr(ins, "entorno_propio_con_anthropic", lambda: None)
        monkeypatch.setattr(ins, "ofrecer_en_ventana", sin_pantalla)
        monkeypatch.setattr(ins, "ofrecer_en_terminal", lambda: 3)
        assert ins.resolver_falta_de_la_libreria(terminal=False) == 3

    def test_sin_tkinter_tambien(self, monkeypatch):
        def sin_tk():
            raise ImportError("No module named 'tkinter'")

        monkeypatch.delenv(ins.VARIABLE_REINTENTO, raising=False)
        monkeypatch.setattr(ins, "entorno_propio_con_anthropic", lambda: None)
        monkeypatch.setattr(ins, "ofrecer_en_ventana", sin_tk)
        monkeypatch.setattr(ins, "ofrecer_en_terminal", lambda: 4)
        assert ins.resolver_falta_de_la_libreria(terminal=False) == 4

    def test_un_error_que_no_es_de_pantalla_no_se_esconde(self, monkeypatch):
        def roto():
            raise ZeroDivisionError("bug")

        monkeypatch.delenv(ins.VARIABLE_REINTENTO, raising=False)
        monkeypatch.setattr(ins, "entorno_propio_con_anthropic", lambda: None)
        monkeypatch.setattr(ins, "ofrecer_en_ventana", roto)
        with pytest.raises(ZeroDivisionError):
            ins.resolver_falta_de_la_libreria(terminal=False)
