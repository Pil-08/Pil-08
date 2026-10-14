"""`claude_batch_monitor.py`, el programa en un solo archivo: está al día, se ejecuta solo y no esconde nada."""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
ARCHIVO = RAIZ / "claude_batch_monitor.py"
GENERADOR = RAIZ / "tools" / "build_single_file.py"

pytestmark = pytest.mark.skipif(
    not (ARCHIVO.exists() and GENERADOR.exists()), reason="solo en el repositorio completo"
)


def _cargar_generador():
    spec = importlib.util.spec_from_file_location("build_single_file", GENERADOR)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def _carpeta_de_descargas(tmp_path: Path) -> Path:
    """Una carpeta vacía con una COPIA del archivo: ahí no existe el paquete, solo el archivo."""
    carpeta = tmp_path / "descargas"
    carpeta.mkdir(exist_ok=True)
    shutil.copy(ARCHIVO, carpeta / "claude_batch_monitor.py")
    return carpeta


def _entorno(tmp_path: Path) -> dict[str, str]:
    entorno = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "CBM_ARCHIVO_UNICO")}
    entorno.update({"CLAUDE_MONITOR_HOME": str(tmp_path / "datos"), "PYTHONIOENCODING": "utf-8"})
    return entorno


def _ejecutar_solo(tmp_path: Path, *args: str):
    carpeta = _carpeta_de_descargas(tmp_path)
    return subprocess.run(
        [sys.executable, str(carpeta / "claude_batch_monitor.py"), *args],
        cwd=carpeta, env=_entorno(tmp_path), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )


def _ejecutar_guion(tmp_path: Path, nombre: str, codigo: str):
    """Ejecuta `codigo` (un guion de prueba) junto a la copia del archivo."""
    carpeta = _carpeta_de_descargas(tmp_path)
    (carpeta / nombre).write_text(codigo, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(carpeta / nombre)],
        cwd=carpeta, env=_entorno(tmp_path), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )


class TestAlDia:
    def test_es_lo_que_genera_el_generador(self):
        generado = _cargar_generador().construir()
        actual = ARCHIVO.read_text(encoding="utf-8")
        assert actual == generado, "claude_batch_monitor.py está desfasado: ejecuta  python tools/build_single_file.py"

    def test_la_cabecera_dice_la_version_del_programa(self):
        from claude_batch_monitor import __version__

        assert f"Claude Batch Monitor {__version__}" in ARCHIVO.read_text(encoding="utf-8").split("\n", 3)[1]

    def test_todas_las_secciones_estan_en_el_indice(self):
        texto = ARCHIVO.read_text(encoding="utf-8")
        titulos = re.findall(r"^# (\d+)\. (.+)$", texto, re.M)
        assert len(titulos) >= 30
        for numero, titulo in titulos:
            assert re.search(rf"^#\s+{numero}\. {re.escape(titulo)}\s+línea\s+\d+$", texto, re.M), titulo

    def test_el_indice_apunta_a_la_linea_de_cada_seccion(self):
        lineas = ARCHIVO.read_text(encoding="utf-8").split("\n")
        for linea in lineas:
            fila = re.match(r"^#\s+(\d+)\. (.+?)\s+línea\s+(\d+)$", linea)
            if fila:
                numero, titulo, destino = fila.group(1), fila.group(2), int(fila.group(3))
                assert lineas[destino - 1] == f"# {numero}. {titulo}"


class TestSeEjecutaSolo:
    def test_dice_su_version(self, tmp_path):
        r = _ejecutar_solo(tmp_path, "--version")
        from claude_batch_monitor import __version__

        assert r.returncode == 0 and r.stdout.strip() == f"Claude Batch Monitor {__version__}", r.stderr

    def test_la_ayuda_dice_como_se_ejecuta_este_archivo(self, tmp_path):
        r = _ejecutar_solo(tmp_path, "--cli", "--help")
        assert r.returncode == 0
        assert "claude_batch_monitor.py --cli" in r.stdout

    def test_estima_sin_enviar_nada(self, tmp_path):
        r = _ejecutar_solo(tmp_path, "--cli", "--mensaje", "Hola mundo", "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "Órdenes detectadas : 1" in r.stdout and "No se ha enviado nada" in r.stdout

    def test_los_datos_van_a_su_carpeta_y_no_junto_al_archivo(self, tmp_path):
        _ejecutar_solo(tmp_path, "--cli", "--listar")
        assert (tmp_path / "datos" / "responses").is_dir()
        assert [p.name for p in (tmp_path / "descargas").iterdir()] == ["claude_batch_monitor.py"]

    def test_sin_tkinter_la_terminal_funciona_y_la_ventana_explica_que_falta(self, tmp_path):
        r = _ejecutar_guion(
            tmp_path,
            "sin_tk.py",
            "import runpy, sys\n"
            "sys.modules['tkinter'] = None\n"  # `import tkinter` falla, como en un Python sin tcl/tk
            "ns = runpy.run_path('claude_batch_monitor.py', run_name='prueba')\n"
            "print('base:', ns['Modal'].__mro__[1].__name__)\n"
            "print('terminal:', ns['main'](['--version']))\n"
            "print('ventana:', ns['main']([]))\n",
        )
        assert r.returncode == 0, r.stderr
        assert "base: _VentanaSinTk" in r.stdout and "terminal: 0" in r.stdout and "ventana: 1" in r.stdout
        assert "tkinter" in r.stderr and "python3-tk" in r.stderr

    def test_sin_la_libreria_de_anthropic_se_ofrece_instalarla_en_vez_de_morir(self, tmp_path):
        r = _ejecutar_guion(
            tmp_path,
            "sin_anthropic.py",
            "import runpy, sys\n"
            "sys.modules['anthropic'] = None\n"
            "ns = runpy.run_path('claude_batch_monitor.py', run_name='prueba')\n"
            "print('falta:', ns['falta_anthropic']())\n"
            "print('codigo:', ns['main'](['--cli', '--mensaje', 'x']))\n",
        )
        assert "falta: True" in r.stdout and "codigo: 1" in r.stdout, r.stderr + r.stdout
        assert "-m pip install anthropic" in r.stdout  # explica qué falta y cómo instalarlo; sin nadie al teclado no instala nada


def _arbol() -> ast.Module:
    return ast.parse(ARCHIVO.read_text(encoding="utf-8"))


class TestNoEsconde:
    """Es un archivo para leer: nada se descomprime, descifra ni ejecuta desde un texto, y se habla solo con lo que dice la cabecera."""

    def test_sin_exec_eval_ni_importaciones_dinamicas(self):
        prohibidas = {"exec", "eval", "compile", "__import__"}
        encontradas = [
            (n.func.id, n.lineno) for n in ast.walk(_arbol())
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in prohibidas
        ]
        assert encontradas == []

    def test_sin_descodificar_ni_descomprimir_ni_deserializar_codigo(self):
        sospechosas = {"b64decode", "decodebytes", "a2b_base64", "unhexlify", "decompress", "decompressobj"}
        prohibidos = {"marshal", "pickle", "shelve", "importlib.machinery", "zipimport"}
        arbol = _arbol()
        llamadas = [n.func.attr for n in ast.walk(arbol) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        assert sospechosas.isdisjoint(llamadas)
        importados = set()
        for n in ast.walk(arbol):
            if isinstance(n, ast.Import):
                importados.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                importados.add(n.module)
        assert prohibidos.isdisjoint(importados)

    def test_sin_bloques_de_texto_ilegibles(self):
        for n in ast.walk(_arbol()):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                assert not re.search(r"[A-Za-z0-9+/=]{120,}", n.value), f"línea {n.lineno}: texto sin espacios muy largo"
                assert len(n.value) < 2500 or n.lineno <= 3, f"línea {n.lineno}: cadena demasiado larga"

    def test_las_direcciones_web_son_solo_las_conocidas(self):
        # (claude.ai: la página de uso del plan; code.claude.com: cómo instalar Claude Code. Solo se abren si la persona pulsa.)
        permitidas = {"api.telegram.org", "platform.claude.com", "www.python.org", "claude.ai", "code.claude.com"}
        texto = ARCHIVO.read_text(encoding="utf-8")
        servidores = set(re.findall(r"https?://([A-Za-z0-9.-]+)", texto))
        assert servidores <= permitidas, servidores - permitidas

    def test_la_cabecera_explica_a_donde_se_conecta(self):
        cabecera = ast.get_docstring(_arbol()) or ""
        assert "api.anthropic.com" in cabecera and "api.telegram.org" in cabecera and "PyPI" in cabecera

    def test_las_unicas_librerias_externas_son_las_que_dice_la_cabecera(self):
        declaradas = {"anthropic", "keyring", "pystray", "PIL", "plyer"}
        cabecera = ast.get_docstring(_arbol()) or ""
        for nombre in ("anthropic", "keyring", "pystray", "Pillow", "plyer"):
            assert nombre in cabecera, nombre

        importados = set()
        for n in ast.walk(_arbol()):
            if isinstance(n, ast.Import):
                importados.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                importados.add(n.module.split(".")[0])

        def es_estandar(nombre: str) -> bool:
            if hasattr(sys, "stdlib_module_names"):
                return nombre in sys.stdlib_module_names
            spec = importlib.util.find_spec(nombre)
            origen = getattr(spec, "origin", None) or ""
            return spec is not None and "site-packages" not in origen and "dist-packages" not in origen

        # `tkinter` (opcional en algunos Python) y `winreg` / `msvcrt` (solo Windows) también son de la biblioteca estándar
        externas = {n for n in importados if not es_estandar(n) and n not in ("tkinter", "winreg", "msvcrt")}
        assert externas == declaradas, externas ^ declaradas
