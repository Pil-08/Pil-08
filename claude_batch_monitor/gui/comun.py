"""Piezas de interfaz que usan los dos modos: ventanas secundarias y paneles con desplazamiento."""

from __future__ import annotations

import base64
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .. import paths
from ..paths import logger
from .estilo import Paleta
from .i18n import t
from .icono import png_del_icono

URL_CONSOLA = "https://platform.claude.com"
URL_FACTURACION = "https://platform.claude.com/settings/billing"


class Modal(tk.Toplevel):
    """Ventana secundaria centrada sobre la principal y bloqueante.

    `app` es cualquier objeto con `.root` (la ventana principal) y `.p` (la paleta).
    """

    def __init__(self, app: Any, titulo: str, ancho: int = 520, redimensionable: bool = False):
        super().__init__(app.root)
        self.app = app
        self.title(titulo)
        self.configure(background=app.p.fondo)
        self.transient(app.root)
        self.resizable(redimensionable, redimensionable)
        self.columnconfigure(0, weight=1)
        self.bind("<Escape>", lambda e: self.destroy())
        self._ancho = ancho

    def mostrar(self) -> None:
        self.update_idletasks()
        r = self.app.root
        x = r.winfo_rootx() + max(0, (r.winfo_width() - self.winfo_reqwidth()) // 2)
        y = r.winfo_rooty() + max(0, (r.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{x}+{y}")
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass


class Desplazable(ttk.Frame):
    """Un contenedor con barra de desplazamiento vertical: lo que se añade a `.interior` se puede recorrer.

    Los ajustes del modo avanzado son más altos que una pantalla pequeña; sin esto, lo de abajo no se ve.
    """

    def __init__(self, master: tk.Misc, p: Paleta):
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._lienzo = tk.Canvas(self, background=p.fondo, highlightthickness=0, borderwidth=0)
        self._barra = ttk.Scrollbar(self, orient="vertical", command=self._lienzo.yview)
        self._lienzo.configure(yscrollcommand=self._barra.set)
        self._lienzo.grid(row=0, column=0, sticky="nsew")
        self._barra.grid(row=0, column=1, sticky="ns")
        self.interior = ttk.Frame(self._lienzo)
        self._ventana = self._lienzo.create_window((0, 0), window=self.interior, anchor="nw")
        self.interior.bind("<Configure>", self._al_cambiar_interior)
        self._lienzo.bind("<Configure>", self._al_cambiar_lienzo)
        # La rueda se escucha en la ventana entera (los hijos heredan ese enlace) y solo desplaza
        # si el puntero está encima de este panel y no encima de algo que ya usa la rueda.
        self._toplevel = self.winfo_toplevel()
        for evento in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._toplevel.bind(evento, self._rueda, add="+")

    def _al_cambiar_interior(self, _e: Any = None) -> None:
        self._lienzo.configure(scrollregion=self._lienzo.bbox("all"))

    def _al_cambiar_lienzo(self, e: tk.Event) -> None:
        self._lienzo.itemconfigure(self._ventana, width=e.width)

    # Estos ya reaccionan a la rueda por su cuenta; moverlos Y desplazar la página sería un salto doble.
    _PROPIOS = {"Text", "Listbox", "Treeview", "TCombobox", "TSpinbox", "Spinbox"}

    def _rueda(self, e: tk.Event) -> None:
        try:
            if not self.winfo_viewable() or self.interior.winfo_height() <= self._lienzo.winfo_height():
                return  # oculto (otra pestaña) o cabe entero: nada que desplazar
            encima = self.winfo_containing(e.x_root, e.y_root)
            if encima is None or not str(encima).startswith(str(self)):
                return
            if encima.winfo_class() in self._PROPIOS:
                return
            if getattr(e, "num", 0) == 4:
                paso = -3
            elif getattr(e, "num", 0) == 5:
                paso = 3
            else:  # Windows: múltiplos de 120; macOS: valores pequeños
                bruto = e.delta if sys.platform == "darwin" else e.delta // 120
                paso = -3 * bruto if bruto else (-3 if e.delta > 0 else 3)
            self._lienzo.yview_scroll(paso, "units")
        except tk.TclError:
            pass  # el panel ya no existe


def poner_icono(root: tk.Misc) -> bool:
    """Pone el icono de la aplicación en la ventana (y en la barra de tareas). Nunca impide abrirla."""
    try:
        imagen = tk.PhotoImage(master=root, data=base64.b64encode(png_del_icono(64)))
        root.iconphoto(True, imagen)  # type: ignore[attr-defined]
        root._icono_app = imagen  # type: ignore[attr-defined]  # tkinter suelta la imagen si nadie la sujeta
        return True
    except Exception:
        return False


def avisar_de_errores_inesperados(root: tk.Misc, segundos_sin_repetir: float = 30.0) -> None:
    """Tk cuenta los errores de los botones solo en la consola, que quien abre el programa con doble clic no ve.

    Aquí se guardan en el registro y se avisa con una ventana (sin repetir el mismo aviso en bucle).
    """
    ultimo = {"firma": "", "hora": -1e9}

    def al_fallar(tipo: Any, valor: Any, traza: Any) -> None:
        logger.error("Error inesperado en la ventana", exc_info=(tipo, valor, traza))
        firma = f"{getattr(tipo, '__name__', tipo)}: {valor}"
        ahora = time.monotonic()
        if firma == ultimo["firma"] and ahora - ultimo["hora"] < segundos_sin_repetir:
            return
        ultimo["firma"], ultimo["hora"] = firma, ahora
        try:
            messagebox.showerror(t("unexpected_title"), t("unexpected_text", error=firma, log=paths.LOG_FILE), parent=root)
        except Exception:
            pass  # un fallo al avisar no puede causar otro

    root.report_callback_exception = al_fallar  # type: ignore[method-assign]
