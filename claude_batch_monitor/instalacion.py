"""Instalar la única librería externa que necesita el programa si falta: `anthropic`.

Lo que se ejecuta es exactamente `python -m pip install anthropic` (con `--user` fuera de un entorno
virtual). No se instala ni se descarga nada más, y siempre se pregunta antes. Si el sistema no deja
instalar paquetes en su Python (Debian, Ubuntu, Fedora…), se crea una carpeta propia del programa
(`venv`, dentro de su carpeta de datos) y se instala ahí.
"""

from __future__ import annotations

import importlib.util
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import paths
from .gui.i18n import fijar_idioma, t
from .platform_utils import comando_para_instalar

LIBRERIA_NECESARIA = "anthropic"
# Evita un bucle si la instalación «acaba bien» pero Python sigue sin encontrar la librería.
VARIABLE_REINTENTO = "CLAUDE_MONITOR_YA_INSTALADO"
LIMITE_SEGUNDOS = 900
# Opciones que no necesitan hablar con la API: funcionan aunque falte la librería.
_SIN_API = {"--version", "--help", "-h", "--dry-run", "--listar"}


@dataclass
class Resultado:
    ok: bool
    detalle: str = ""
    python: Optional[str] = None  # con qué intérprete volver a abrir el programa
    entorno_propio: bool = False


# ----------------------------------------------------------------------
# Qué falta y cómo se instala
# ----------------------------------------------------------------------


def falta_anthropic() -> bool:
    try:
        return importlib.util.find_spec(LIBRERIA_NECESARIA) is None
    except (ImportError, ValueError):
        return True


def necesita_la_libreria(argv: list[str], terminal: bool) -> bool:
    """La ventana siempre; en la terminal, salvo estimar, listar o pedir ayuda/versión."""
    return not terminal or not any(a.split("=", 1)[0] in _SIN_API for a in argv)


def en_entorno_virtual() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def comando_pip(python: str, usuario: bool) -> list[str]:
    orden = [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input"]
    if usuario:
        orden.append("--user")
    return [*orden, LIBRERIA_NECESARIA]


def comando_visible() -> str:
    """El comando que se le enseña a la persona (el real, sin opciones técnicas)."""
    return comando_para_instalar(LIBRERIA_NECESARIA)


def _ultimas_lineas(texto: str, cuantas: int = 10) -> str:
    return "\n".join([linea for linea in texto.strip().splitlines() if linea.strip()][-cuantas:])


def _ejecutar(orden: list[str], ejecutar: Callable[..., Any]) -> tuple[int, str]:
    try:
        r = ejecutar(orden, capture_output=True, text=True, timeout=LIMITE_SEGUNDOS)
    except subprocess.TimeoutExpired:
        return 1, "Tiempo agotado."
    except OSError as error:
        return 1, str(error)
    return r.returncode, f"{r.stdout or ''}\n{r.stderr or ''}"


_SIN_RED = ("Temporary failure in name resolution", "getaddrinfo failed", "Network is unreachable",
            "Max retries exceeded", "Connection refused", "ConnectionError", "No matching distribution")


def _explicar(salida: str) -> str:
    if any(marca in salida for marca in _SIN_RED):
        return "No se pudo descargar. Comprueba tu conexión a internet.\n\n" + _ultimas_lineas(salida, 4)
    return _ultimas_lineas(salida)


# ----------------------------------------------------------------------
# Carpeta propia (cuando el sistema no deja instalar en su Python)
# ----------------------------------------------------------------------


def carpeta_entorno_propio() -> Path:
    return paths.DATA_DIR / "venv"


def python_del_entorno_propio() -> Path:
    base = carpeta_entorno_propio()
    if sys.platform == "win32":
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def entorno_propio_con_anthropic(ejecutar: Callable[..., Any] = subprocess.run) -> Optional[str]:
    """El Python de la carpeta propia, si existe y ya tiene la librería."""
    python = python_del_entorno_propio()
    if not python.is_file():
        return None
    codigo, _ = _ejecutar([str(python), "-c", f"import {LIBRERIA_NECESARIA}"], ejecutar)
    return str(python) if codigo == 0 else None


def _instalar_en_entorno_propio(ejecutar: Callable[..., Any]) -> Resultado:
    carpeta = carpeta_entorno_propio()
    codigo, salida = _ejecutar([sys.executable, "-m", "venv", str(carpeta)], ejecutar)
    if codigo != 0:
        return Resultado(False, "No se pudo crear una carpeta propia para instalarla "
                                "(¿falta el paquete «python3-venv»?).\n\n" + _ultimas_lineas(salida, 4))
    python = str(python_del_entorno_propio())
    codigo, salida = _ejecutar(comando_pip(python, usuario=False), ejecutar)
    if codigo != 0:
        return Resultado(False, _explicar(salida))
    return Resultado(True, python=python, entorno_propio=True)


def instalar(ejecutar: Callable[..., Any] = subprocess.run) -> Resultado:
    codigo, salida = _ejecutar(comando_pip(sys.executable, usuario=not en_entorno_virtual()), ejecutar)
    if codigo == 0:
        return Resultado(True, python=sys.executable)
    if "externally-managed-environment" in salida or "No module named pip" in salida:
        return _instalar_en_entorno_propio(ejecutar)
    return Resultado(False, _explicar(salida))


# ----------------------------------------------------------------------
# Volver a abrir el programa
# ----------------------------------------------------------------------


def comando_para_reiniciar(python: Optional[str] = None) -> list[str]:
    ejecutable = python or sys.executable
    original = getattr(sys, "orig_argv", None)
    if original:  # Python 3.10+: la línea de órdenes tal cual se escribió (con `-m paquete` si fue así)
        return [ejecutable, *original[1:]]
    principal = sys.modules.get("__main__")
    spec = getattr(principal, "__spec__", None)
    if spec is not None and spec.name:
        modulo = spec.name[: -len(".__main__")] if spec.name.endswith(".__main__") else spec.name
        return [ejecutable, "-m", modulo, *sys.argv[1:]]
    return [ejecutable, *sys.argv]


def reiniciar_con(python: Optional[str] = None) -> int:
    """Vuelve a lanzar el mismo programa y termina este proceso. En POSIX no vuelve."""
    orden = comando_para_reiniciar(python)
    os.environ[VARIABLE_REINTENTO] = "1"
    if sys.platform == "win32":  # `os.execv` no sustituye el proceso en Windows
        subprocess.Popen(orden)
        return 0
    os.execv(orden[0], orden)
    return 0  # pragma: no cover - execv no vuelve


# ----------------------------------------------------------------------
# Preguntar, instalar, reiniciar
# ----------------------------------------------------------------------


def ofrecer_en_terminal(
    entrada: Callable[[str], str] = input,
    salida: Callable[[str], None] = print,
    interactivo: Optional[bool] = None,
    instalar_fn: Callable[[], Resultado] = instalar,
    reiniciar_fn: Callable[[Optional[str]], int] = reiniciar_con,
) -> int:
    """Explica qué falta y, si hay alguien al teclado y dice que sí, lo instala. Devuelve el código de salida."""
    if interactivo is None:
        interactivo = bool(sys.stdin is not None and sys.stdin.isatty())
    salida(t("install_text"))
    salida(f"\n    {comando_visible()}\n")
    if not interactivo:
        salida(t("install_manual"))
        return 1
    try:
        respuesta = entrada(t("install_ask")).strip().lower()
    except EOFError:
        respuesta = ""
    if respuesta not in ("s", "si", "sí", "y", "yes"):
        salida(t("install_manual"))
        return 1
    salida(t("install_working"))
    resultado = instalar_fn()
    if not resultado.ok:
        salida(f"{t('install_failed')}\n{resultado.detalle}\n\n{t('install_manual')}")
        return 1
    salida(t("install_done"))
    return reiniciar_fn(resultado.python)


class DialogoInstalacion:
    """Una ventana pequeña: explica qué falta, enseña el comando y lo instala con un botón."""

    def __init__(self, root: Any = None, instalar_fn: Callable[[], Resultado] = instalar,
                 reiniciar_fn: Callable[[Optional[str]], int] = reiniciar_con):
        import tkinter as tk
        from tkinter import ttk

        from .gui.comun import poner_icono
        from .gui.estilo import aplicar_estilo

        self.root = root or tk.Tk()
        self.instalar_fn, self.reiniciar_fn = instalar_fn, reiniciar_fn
        self.resultado: Optional[Resultado] = None
        self.codigo = 1
        self._respuestas: queue.SimpleQueue = queue.SimpleQueue()
        self.p, self.f = aplicar_estilo(self.root)
        self.root.title(t("install_title"))
        self.root.resizable(False, False)
        poner_icono(self.root)

        cuerpo = ttk.Frame(self.root, padding=24)
        cuerpo.grid(row=0, column=0)
        ttk.Label(cuerpo, text=t("install_title"), style="Titulo.TLabel").grid(row=0, column=0, sticky="w")
        tk.Label(cuerpo, text=t("install_text"), justify="left", anchor="w", wraplength=480,
                 background=self.p.fondo, foreground=self.p.texto, font=self.f.base).grid(
            row=1, column=0, sticky="w", pady=(10, 10))
        self.comando = tk.StringVar(value=comando_visible())
        ttk.Entry(cuerpo, textvariable=self.comando, state="readonly", width=52, font=self.f.mono).grid(
            row=2, column=0, sticky="ew")
        self.estado = tk.StringVar(value="")
        self.etiqueta_estado = tk.Label(cuerpo, textvariable=self.estado, justify="left", anchor="w", wraplength=480,
                                        background=self.p.fondo, foreground=self.p.apagado, font=self.f.base)
        self.etiqueta_estado.grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.etiqueta_estado.grid_remove()  # hasta que haya algo que contar
        self.barra = ttk.Progressbar(cuerpo, mode="indeterminate", length=480)
        botones = ttk.Frame(cuerpo)
        botones.grid(row=5, column=0, sticky="e", pady=(16, 0))
        self.boton_salir = ttk.Button(botones, text=t("install_quit"), command=self.salir)
        self.boton_salir.pack(side="left")
        self.boton_copiar = ttk.Button(botones, text=t("install_copy"), command=self.copiar)
        self.boton_instalar = ttk.Button(botones, text=t("install_button"), style="Primary.TButton", command=self.instalar)
        self.boton_instalar.pack(side="left", padx=(8, 0))
        self.root.protocol("WM_DELETE_WINDOW", self.salir)
        self._barra_visible = False

    # -- acciones -------------------------------------------------------

    def instalar(self) -> None:
        self.boton_instalar.state(["disabled"])
        self.boton_salir.state(["disabled"])
        self.estado.set(t("install_working"))
        self.etiqueta_estado.grid()
        if not self._barra_visible:
            self.barra.grid(row=4, column=0, sticky="ew", pady=(10, 0))
            self._barra_visible = True
        self.barra.start(12)

        def trabajo() -> None:
            try:
                resultado = self.instalar_fn()
            except Exception as error:  # pragma: no cover - defensa
                resultado = Resultado(False, str(error))
            self._respuestas.put(resultado)

        threading.Thread(target=trabajo, daemon=True, name="instalacion").start()
        self.root.after(150, self._esperar)

    def _esperar(self) -> None:
        try:
            resultado = self._respuestas.get_nowait()
        except queue.Empty:
            self.root.after(150, self._esperar)
            return
        self.barra.stop()
        self.resultado = resultado
        if resultado.ok:
            self.estado.set(t("install_done"))
            self.codigo = 0
            self.root.after(900, self.root.destroy)
            return
        self.etiqueta_estado.configure(foreground=self.p.error)
        self.estado.set(f"{t('install_failed')}\n{resultado.detalle}\n\n{t('install_manual')}")
        self.boton_salir.state(["!disabled"])
        self.boton_instalar.configure(text=t("install_retry"))
        self.boton_instalar.state(["!disabled"])
        self.boton_copiar.pack(side="left", padx=(8, 0), before=self.boton_instalar)

    def copiar(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(comando_visible())

    def salir(self) -> None:
        self.codigo = 1
        self.root.destroy()

    def ejecutar(self) -> int:
        self.root.mainloop()
        if self.codigo == 0 and self.resultado and self.resultado.ok:
            return self.reiniciar_fn(self.resultado.python)
        return self.codigo


def ofrecer_en_ventana() -> int:
    return DialogoInstalacion().ejecutar()


def resolver_falta_de_la_libreria(terminal: bool) -> int:
    """Qué hacer cuando falta `anthropic` y se la necesita. Devuelve el código de salida (o reinicia el programa)."""
    fijar_idioma("")
    if os.environ.get(VARIABLE_REINTENTO):
        print(t("install_loop", comando=comando_visible()), file=sys.stderr)
        return 1
    propio = entorno_propio_con_anthropic()
    if propio:
        return reiniciar_con(propio)
    if not terminal:
        try:
            return ofrecer_en_ventana()
        except ImportError:  # este Python no trae tkinter
            pass
        except Exception as error:  # noqa: BLE001 - sin pantalla (un servidor, por SSH): se pregunta en la terminal
            if type(error).__name__ != "TclError":
                raise
    return ofrecer_en_terminal()
