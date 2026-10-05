"""Aspecto común de las ventanas: colores, tipografías y estilos de ttk."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

from ..platform_utils import tema_del_sistema


@dataclass(frozen=True)
class Paleta:
    oscura: bool = False
    fondo: str = "#f4f6f9"
    superficie: str = "#ffffff"
    borde: str = "#d5dae1"
    texto: str = "#1f2933"
    apagado: str = "#5d6778"
    acento: str = "#2f6fed"
    acento_hover: str = "#2459c9"
    acento_suave: str = "#dce8ff"
    acento_desactivado: str = "#9db8f5"
    boton: str = "#e9edf3"
    boton_hover: str = "#dde3ec"
    ok: str = "#15803d"
    aviso: str = "#b45309"
    error: str = "#b91c1c"
    gris: str = "#6b7280"
    banner_aviso_fondo: str = "#fff4e0"
    banner_aviso_borde: str = "#f3c37a"
    banner_error_fondo: str = "#fdecec"
    banner_error_borde: str = "#f1a7a7"
    banner_info_fondo: str = "#e8f1ff"
    banner_info_borde: str = "#a9c6f7"
    acento_texto: str = "#2459c9"  # el azul cuando es el color de un TEXTO (enlaces, «enviando…», pestaña activa)
    deshabilitado: str = "#a3abb8"  # texto de lo que está desactivado
    codigo_fondo: str = "#eef1f6"   # fondo del código en las respuestas


PALETA_CLARA = Paleta()
PALETA_OSCURA = Paleta(
    oscura=True, fondo="#161a21", superficie="#1f242d", borde="#323a48", texto="#e5e9f0", apagado="#9aa4b5",
    acento="#3566d6", acento_hover="#3f6fe3", acento_suave="#2a3c60", acento_desactivado="#2c4070",
    boton="#2a313d", boton_hover="#363f4f", ok="#4ade80", aviso="#fbbf24", error="#f87171", gris="#8b95a7",
    banner_aviso_fondo="#3a2e16", banner_aviso_borde="#7a5b1c",
    banner_error_fondo="#3c1f22", banner_error_borde="#843c40",
    banner_info_fondo="#1d2b47", banner_info_borde="#38558f",
    acento_texto="#7aa2ff", deshabilitado="#6b7587", codigo_fondo="#2a3140",
)
PALETA = PALETA_CLARA  # la de siempre: lo que usan las pruebas y los valores por omisión


def elegir_paleta(tema: str = "auto") -> Paleta:
    """La paleta de un tema: `auto` sigue al sistema (y si no se puede saber, claro)."""
    if tema == "auto":
        tema = tema_del_sistema()
    return PALETA_OSCURA if tema == "oscuro" else PALETA_CLARA


@dataclass
class Fuentes:
    base: tkfont.Font
    texto: tkfont.Font
    titulo: tkfont.Font
    seccion: tkfont.Font
    pequena: tkfont.Font
    negrita: tkfont.Font
    mono: tkfont.Font


def aplicar_estilo(root: tk.Misc, tema: str = "auto") -> tuple[Paleta, Fuentes]:
    """Aplica el tema a la ventana y devuelve la paleta y las fuentes para usarlas en los widgets."""
    p = elegir_paleta(tema)
    estilo = ttk.Style(root)
    if "clam" in estilo.theme_names():
        estilo.theme_use("clam")  # el único tema de ttk que deja recolorear todo igual en cada SO

    base = tkfont.nametofont("TkDefaultFont")
    base.configure(size=10)
    texto = tkfont.nametofont("TkTextFont")
    texto.configure(size=11)
    familia = base.actual("family")
    fuentes = Fuentes(
        base=base,
        texto=texto,
        titulo=tkfont.Font(root=root, family=familia, size=17, weight="bold"),
        seccion=tkfont.Font(root=root, family=familia, size=11, weight="bold"),
        pequena=tkfont.Font(root=root, family=familia, size=9),
        negrita=tkfont.Font(root=root, family=familia, size=10, weight="bold"),
        mono=tkfont.nametofont("TkFixedFont"),
    )

    root.configure(background=p.fondo)
    estilo.configure(".", background=p.fondo, foreground=p.texto, font=base, borderwidth=0,
                     focuscolor=p.acento, troughcolor=p.fondo)
    estilo.configure("TFrame", background=p.fondo)
    estilo.configure("TLabel", background=p.fondo, foreground=p.texto)
    estilo.configure("Titulo.TLabel", font=fuentes.titulo)
    estilo.configure("Seccion.TLabel", font=fuentes.seccion)
    estilo.configure("Sub.TLabel", foreground=p.apagado)
    estilo.configure("Pequeno.TLabel", foreground=p.apagado, font=fuentes.pequena)

    estilo.configure("TButton", padding=(14, 7), background=p.boton, foreground=p.texto,
                     bordercolor=p.borde, lightcolor=p.boton, darkcolor=p.boton, relief="flat")
    estilo.map("TButton",
               background=[("disabled", p.boton), ("active", p.boton_hover), ("pressed", p.boton_hover)],
               foreground=[("disabled", p.deshabilitado)],
               lightcolor=[("active", p.boton_hover)], darkcolor=[("active", p.boton_hover)])
    estilo.configure("TMenubutton", padding=(14, 7), background=p.boton, foreground=p.texto,
                     arrowcolor=p.apagado, bordercolor=p.borde, relief="flat")
    estilo.map("TMenubutton", background=[("disabled", p.boton), ("active", p.boton_hover)],
               foreground=[("disabled", p.deshabilitado)])
    estilo.configure("Primary.TButton", background=p.acento, foreground="#ffffff", font=fuentes.negrita,
                     bordercolor=p.acento, lightcolor=p.acento, darkcolor=p.acento)
    estilo.map("Primary.TButton",
               background=[("disabled", p.acento_desactivado), ("active", p.acento_hover),
                           ("pressed", p.acento_hover)],
               foreground=[("disabled", "#ffffff")],
               lightcolor=[("active", p.acento_hover)], darkcolor=[("active", p.acento_hover)],
               bordercolor=[("active", p.acento_hover)])
    estilo.configure("Enlace.TButton", background=p.fondo, foreground=p.acento_texto, padding=(6, 3),
                     lightcolor=p.fondo, darkcolor=p.fondo, bordercolor=p.fondo)
    estilo.map("Enlace.TButton", foreground=[("active", p.acento_hover)],
               background=[("active", p.fondo)])

    estilo.configure("Treeview", background=p.superficie, fieldbackground=p.superficie,
                     foreground=p.texto, rowheight=30, bordercolor=p.superficie, lightcolor=p.superficie,
                     darkcolor=p.superficie, font=base)
    estilo.map("Treeview", background=[("selected", p.acento_suave)], foreground=[("selected", p.texto)])
    estilo.configure("Treeview.Heading", background=p.boton, foreground=p.apagado, font=fuentes.negrita,
                     padding=(8, 6), relief="flat", bordercolor=p.borde)
    estilo.map("Treeview.Heading", background=[("active", p.boton_hover)])

    for nombre in ("TEntry", "TCombobox", "TSpinbox"):
        estilo.configure(nombre, fieldbackground=p.superficie, foreground=p.texto, bordercolor=p.borde,
                         lightcolor=p.borde, darkcolor=p.borde, padding=6, insertcolor=p.texto,
                         selectbackground=p.acento_suave, selectforeground=p.texto, arrowcolor=p.apagado)
    estilo.map("TCombobox", fieldbackground=[("readonly", p.superficie)],
               selectbackground=[("readonly", p.superficie)], selectforeground=[("readonly", p.texto)])
    estilo.configure("TCheckbutton", background=p.fondo, foreground=p.texto)
    estilo.configure("TRadiobutton", background=p.fondo, foreground=p.texto)
    estilo.configure("TLabelframe", background=p.fondo, bordercolor=p.borde, lightcolor=p.borde,
                     darkcolor=p.borde, relief="solid", borderwidth=1)
    estilo.configure("TLabelframe.Label", background=p.fondo, foreground=p.apagado, font=fuentes.negrita)
    estilo.configure("TNotebook", background=p.fondo, borderwidth=0, tabmargins=(0, 4, 0, 0),
                     bordercolor=p.borde, lightcolor=p.borde, darkcolor=p.borde)
    estilo.configure("TNotebook.Tab", background=p.boton, foreground=p.apagado, padding=(20, 8),
                     font=fuentes.negrita, borderwidth=0, bordercolor=p.borde, lightcolor=p.boton, darkcolor=p.boton)
    estilo.map("TNotebook.Tab", background=[("selected", p.superficie), ("active", p.boton_hover)],
               foreground=[("selected", p.acento_texto)])
    estilo.configure("Interior.TNotebook", background=p.fondo, borderwidth=0, tabmargins=(0, 2, 0, 0),
                     bordercolor=p.borde, lightcolor=p.borde, darkcolor=p.borde)
    estilo.configure("Interior.TNotebook.Tab", background=p.boton, foreground=p.apagado, padding=(14, 5),
                     font=fuentes.base, borderwidth=0, bordercolor=p.borde, lightcolor=p.boton, darkcolor=p.boton)
    estilo.map("Interior.TNotebook.Tab", background=[("selected", p.superficie), ("active", p.boton_hover)],
               foreground=[("selected", p.acento_texto)])
    estilo.configure("TPanedwindow", background=p.fondo)
    estilo.configure("Sash", sashthickness=10, handlesize=0)
    estilo.configure("TScrollbar", background=p.boton, bordercolor=p.fondo, arrowcolor=p.apagado,
                     troughcolor=p.fondo, lightcolor=p.boton, darkcolor=p.boton)
    estilo.configure("Horizontal.TProgressbar", background=p.acento, troughcolor=p.boton,
                     bordercolor=p.fondo, lightcolor=p.acento, darkcolor=p.acento)

    if p.oscura:
        # Con el fondo oscuro, las casillas y los botones de opción por defecto (cuadro blanco, marca negra) desentonan.
        for nombre in ("TCheckbutton", "TRadiobutton"):
            estilo.configure(nombre, indicatorbackground=p.superficie, indicatorforeground=p.texto,
                             upperbordercolor=p.borde, lowerbordercolor=p.borde)
            estilo.map(nombre, background=[("active", p.fondo)],
                       indicatorbackground=[("selected", p.acento), ("pressed", p.boton_hover)],
                       indicatorforeground=[("selected", "#ffffff"), ("disabled", p.deshabilitado)],
                       foreground=[("disabled", p.deshabilitado)])
        estilo.configure("TSeparator", background=p.borde)

    # Selectores y menús desplegables de los combobox y cuadros de texto (son widgets Tk, no ttk).
    root.option_add("*TCombobox*Listbox.background", p.superficie)
    root.option_add("*TCombobox*Listbox.foreground", p.texto)
    root.option_add("*TCombobox*Listbox.selectBackground", p.acento_suave)
    root.option_add("*TCombobox*Listbox.selectForeground", p.texto)
    for clase in ("Text", "Entry", "Listbox"):
        root.option_add(f"*{clase}.selectBackground", p.acento_suave)
        root.option_add(f"*{clase}.selectForeground", p.texto)
        root.option_add(f"*{clase}.inactiveSelectBackground", p.acento_suave)
    root.option_add("*Menu.background", p.superficie)
    root.option_add("*Menu.foreground", p.texto)
    root.option_add("*Menu.activeBackground", p.acento_suave)
    root.option_add("*Menu.activeForeground", p.texto)
    root.option_add("*Menu.disabledForeground", p.deshabilitado)
    return p, fuentes


def tarjeta(master: tk.Misc, p: Paleta = PALETA, **kw: object) -> tk.Frame:
    """Un recuadro blanco con borde fino donde poner un campo de texto o una tabla."""
    return tk.Frame(master, background=p.superficie, highlightbackground=p.borde,
                    highlightcolor=p.acento, highlightthickness=1, **kw)
