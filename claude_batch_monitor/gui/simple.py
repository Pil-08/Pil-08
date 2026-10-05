"""Modo simple: escribir una pregunta, pulsar Enviar y ver cómo la cola trabaja sola.

Pensado para alguien que no quiere saber qué es un lote ni un token. Todo lo
técnico (modelo, tope de gasto, clave) está en «Opciones»; el resto del tiempo
solo hay una caja de texto, un botón y una lista que se va llenando.
"""

from __future__ import annotations

import gc
import threading
import tkinter as tk
import webbrowser
from decimal import Decimal
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Optional

from .. import paths
from ..claves import (
    ErrorDeClave,
    elegir_modelo_por_defecto,
    guardar_clave,
    listar_modelos,
    probar_clave,
)
from ..cola import Cola, EstadoCola, EventosCola
from ..config import TEMAS, ConfigManager, Configuracion, a_decimal
from ..core.base import StopReason
from ..core.sequential import SequentialEngine
from ..export import ErrorDeExportacion, exportar, leer_respuesta
from ..job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REFUSED,
    STATUS_SKIPPED,
    JobMessage,
)
from ..paths import logger
from ..platform_utils import abrir_ruta
from ..pricing import (
    MODELOS_SUGERIDOS,
    descripcion_modelo,
    formatear_usd,
    soporta_esfuerzo,
)
from ..state import obtener_api_key, origen_api_key
from . import formato, tray
from .bridge import UiBridge
from .comun import URL_CONSOLA, URL_FACTURACION, Modal, avisar_de_errores_inesperados, poner_icono
from .estilo import aplicar_estilo, tarjeta
from .i18n import fijar_idioma, t

TOPE_INICIAL = Decimal("2")
MAX_TOKENS_INICIAL = 8000


def formatear_tiempo(segundos: float) -> str:
    """`75.2` -> `1:15`."""
    s = max(0, int(round(segundos)))
    return f"{s // 60}:{s % 60:02d}"


def config_inicial() -> Configuracion:
    """Configuración guardada o, la primera vez, una prudente para quien no sabe de qué va esto."""
    primera_vez = not paths.CONFIG_FILE.exists()
    config = ConfigManager.cargar()
    if primera_vez:
        config.tope_gasto_usd = TOPE_INICIAL
        config.max_tokens = MAX_TOKENS_INICIAL
        config.interfaz = "simple"
        if soporta_esfuerzo(config.modelo, "medium"):
            config.esfuerzo = "medium"  # respuestas buenas sin pagar razonamiento de más
        ConfigManager.guardar(config)
    return config


class _EventosDeLaCola(EventosCola):
    """Reenvía los eventos de la cola a la ventana (ya están en el hilo de la ventana)."""

    def __init__(self, app: SimpleApp):
        self.app = app

    def orden_cambiada(self, msg: JobMessage) -> None: self.app.on_orden(msg)
    def estado(self, estado: EstadoCola) -> None: self.app.on_estado(estado)
    def cuenta_atras(self, restante: float) -> None: self.app.on_cuenta_atras(restante)
    def delta(self, msg_id: int, texto: str) -> None: self.app.on_delta(msg_id, texto)
    def gasto(self, usd: Decimal) -> None: self.app.on_gasto(usd)
    def error(self, texto: str) -> None: self.app.on_error(texto)


# ----------------------------------------------------------------------
# Piezas reutilizables
# ----------------------------------------------------------------------


class PanelClave(ttk.Frame):
    """Campo para pegar la clave de API, probarla y guardarla. Lo usan la bienvenida y «Cambiar clave»."""

    def __init__(self, master: tk.Misc, app: SimpleApp, al_terminar: Callable[[list[str]], None]):
        super().__init__(master)
        self.app, self.al_terminar = app, al_terminar
        self.var = tk.StringVar()
        fila = ttk.Frame(self)
        fila.pack(fill="x")
        self.entrada = ttk.Entry(fila, textvariable=self.var, show="•", width=48)
        self.entrada.pack(side="left", fill="x", expand=True)
        ttk.Button(fila, text=t("welcome_paste"), command=self._pegar).pack(side="left", padx=(8, 0))

        fila2 = ttk.Frame(self)
        fila2.pack(fill="x", pady=(8, 0))
        self.mostrar = tk.BooleanVar(value=False)
        ttk.Checkbutton(fila2, text=t("welcome_show"), variable=self.mostrar, command=self._alternar).pack(side="left")
        self.boton = ttk.Button(fila2, text=t("welcome_test"), style="Primary.TButton", command=self.probar)
        self.boton.pack(side="right")

        self.mensaje = tk.Label(self, text="", anchor="w", justify="left", wraplength=480,
                                background=app.p.fondo, foreground=app.p.apagado, font=app.f.base)
        self.mensaje.pack(fill="x", pady=(10, 0))
        self.entrada.bind("<Return>", lambda e: self.probar())

    def _alternar(self) -> None:
        self.entrada.configure(show="" if self.mostrar.get() else "•")

    def _pegar(self) -> None:
        try:
            self.var.set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    def _decir(self, texto: str, color: str) -> None:
        self.mensaje.configure(text=texto, foreground=color)

    def probar(self) -> None:
        clave = self.var.get().strip()
        if not clave:
            self._decir(t("key_empty"), self.app.p.error)
            return
        self.boton.state(["disabled"])
        self._decir(t("key_testing"), self.app.p.apagado)

        def trabajo() -> None:
            try:
                modelos = self.app.probar(clave)
            except ErrorDeClave as e:
                self.app.bridge.post(self._fallo, e)
            except Exception as e:  # pragma: no cover - defensa
                self.app.bridge.post(self._fallo, ErrorDeClave("otro", str(e)))
            else:
                self.app.bridge.post(self._exito, clave, modelos)

        threading.Thread(target=trabajo, daemon=True, name="probar-clave").start()

    def _fallo(self, e: ErrorDeClave) -> None:
        self.boton.state(["!disabled"])
        texto = {
            "invalida": t("key_bad"),
            "sin_conexion": t("key_offline"),
            "vacia": t("key_empty"),
        }.get(e.tipo) or t("key_other", texto=e.detalle)
        self._decir(texto, self.app.p.error)

    def _exito(self, clave: str, modelos: list[str]) -> None:
        self.boton.state(["!disabled"])
        donde = self.app.guardar(clave)
        self.var.set("")  # la clave no se queda en un campo de texto
        extra = t("key_saved_keyring") if donde == "llavero" else t("key_saved_memory")
        self._decir(f"{t('key_ok')} {extra}", self.app.p.ok if donde == "llavero" else self.app.p.aviso)
        self.al_terminar(modelos)


class DialogoOpciones(Modal):
    def __init__(self, app: SimpleApp):
        super().__init__(app, t("opt_title"), 560)
        p, f, c = app.p, app.f, app.config
        cuerpo = ttk.Frame(self, padding=20)
        cuerpo.grid(row=0, column=0, sticky="nsew")
        cuerpo.columnconfigure(0, weight=1)

        # Clave
        ttk.Label(cuerpo, text=t("opt_key"), style="Seccion.TLabel").grid(row=0, column=0, sticky="w")
        origen = origen_api_key()
        estado_clave = {"entorno": t("opt_key_env"), "llavero": t("opt_key_keyring")}.get(origen) or t("opt_key_none")
        ttk.Label(cuerpo, text=estado_clave, style="Sub.TLabel", wraplength=500, justify="left").grid(
            row=1, column=0, sticky="w", pady=(2, 4))
        self.boton_clave = ttk.Button(cuerpo, text=t("btn_change_key"), command=self._cambiar_clave)
        self.boton_clave.grid(row=2, column=0, sticky="w")
        if origen == "entorno":
            self.boton_clave.state(["disabled"])

        # Modelo
        ttk.Label(cuerpo, text=t("opt_model"), style="Seccion.TLabel").grid(row=3, column=0, sticky="w", pady=(16, 0))
        fila = ttk.Frame(cuerpo)
        fila.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        fila.columnconfigure(0, weight=1)
        self.modelo = tk.StringVar(value=c.modelo)
        valores = list(dict.fromkeys([c.modelo, *MODELOS_SUGERIDOS]))
        self.combo = ttk.Combobox(fila, textvariable=self.modelo, values=valores, state="readonly")
        self.combo.grid(row=0, column=0, sticky="ew")
        self.boton_modelos = ttk.Button(fila, text=t("opt_load_models"), command=self._cargar_modelos)
        self.boton_modelos.grid(row=0, column=1, padx=(8, 0))
        self.precio = ttk.Label(cuerpo, text=descripcion_modelo(c.modelo), style="Sub.TLabel")
        self.precio.grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.combo.bind("<<ComboboxSelected>>", lambda e: self.precio.configure(text=descripcion_modelo(self.modelo.get())))

        # Instrucciones comunes
        ttk.Label(cuerpo, text=t("opt_instructions"), style="Seccion.TLabel").grid(row=6, column=0, sticky="w", pady=(16, 0))
        caja_i = tarjeta(cuerpo, p)
        caja_i.grid(row=7, column=0, sticky="ew", pady=(4, 0))
        caja_i.columnconfigure(0, weight=1)
        self.instrucciones = tk.Text(caja_i, height=3, wrap="word", relief="flat", font=f.texto, padx=10, pady=8,
                                     background=p.superficie, foreground=p.texto, insertbackground=p.texto,
                                     borderwidth=0, highlightthickness=0, undo=True)
        self.instrucciones.grid(row=0, column=0, sticky="ew")
        self.instrucciones.insert("1.0", c.system_prompt)
        ttk.Label(cuerpo, text=t("opt_instructions_help"), style="Sub.TLabel", wraplength=500, justify="left").grid(
            row=8, column=0, sticky="w", pady=(4, 0))

        # Tope
        ttk.Label(cuerpo, text=t("opt_cap"), style="Seccion.TLabel").grid(row=9, column=0, sticky="w", pady=(16, 0))
        self.tope = tk.StringVar(value=f"{c.tope_gasto_usd:f}".rstrip("0").rstrip(".") or "0")
        ttk.Entry(cuerpo, textvariable=self.tope, width=12).grid(row=10, column=0, sticky="w", pady=(4, 0))
        ttk.Label(cuerpo, text=t("opt_cap_help"), style="Sub.TLabel", wraplength=500, justify="left").grid(
            row=11, column=0, sticky="w", pady=(4, 0))

        # Resto
        self.auto = tk.BooleanVar(value=c.auto_arranque)
        ttk.Checkbutton(cuerpo, text=t("opt_autostart"), variable=self.auto).grid(row=12, column=0, sticky="w", pady=(14, 0))
        fila_i = ttk.Frame(cuerpo)
        fila_i.grid(row=13, column=0, sticky="w", pady=(10, 0))
        ttk.Label(fila_i, text=t("opt_language")).pack(side="left")
        self._idiomas = {"Auto": "", "Español": "es", "English": "en"}
        actual = next((k for k, v in self._idiomas.items() if v == c.idioma), "Auto")
        self.idioma = tk.StringVar(value=actual)
        ttk.Combobox(fila_i, textvariable=self.idioma, values=list(self._idiomas), state="readonly", width=10).pack(
            side="left", padx=(8, 0))
        ttk.Label(fila_i, text=t("opt_theme")).pack(side="left", padx=(18, 0))
        self._temas = {t("theme_auto"): "auto", t("theme_light"): "claro", t("theme_dark"): "oscuro"}
        self.tema = tk.StringVar(value=next((k for k, v in self._temas.items() if v == c.tema), t("theme_auto")))
        ttk.Combobox(fila_i, textvariable=self.tema, values=list(self._temas), state="readonly", width=10).pack(
            side="left", padx=(8, 0))

        self.aviso = tk.Label(cuerpo, text="", anchor="w", justify="left", wraplength=500,
                              background=p.fondo, foreground=p.error, font=f.base)
        self.aviso.grid(row=14, column=0, sticky="w", pady=(8, 0))

        # Enlaces y botones
        enlaces = ttk.Frame(cuerpo)
        enlaces.grid(row=15, column=0, sticky="ew", pady=(14, 0))
        ttk.Button(enlaces, text=t("opt_advanced"), style="Enlace.TButton", command=self._avanzado).pack(side="left")
        ttk.Button(enlaces, text=t("opt_folder"), style="Enlace.TButton",
                   command=lambda: abrir_ruta(paths.RESPONSES_DIR)).pack(side="left")
        ttk.Label(enlaces, text=t("opt_about", v=paths.APP_VERSION), style="Pequeno.TLabel").pack(side="right")

        botones = ttk.Frame(cuerpo)
        botones.grid(row=16, column=0, sticky="e", pady=(16, 0))
        ttk.Button(botones, text=t("opt_cancel"), command=self.destroy).pack(side="left")
        ttk.Button(botones, text=t("opt_save"), style="Primary.TButton", command=self.guardar).pack(side="left", padx=(8, 0))
        self.mostrar()

    def _cambiar_clave(self) -> None:
        d = Modal(self.app, t("opt_key"), 520)
        cuerpo = ttk.Frame(d, padding=20)
        cuerpo.grid(row=0, column=0)
        PanelClave(cuerpo, self.app, lambda modelos: self.app._luego(900, d.destroy)).pack()
        d.mostrar()

    def _cargar_modelos(self) -> None:
        clave = obtener_api_key()
        if not clave:
            self.aviso.configure(text=t("opt_key_none"))
            return
        self.boton_modelos.state(["disabled"])

        def trabajo() -> None:
            try:
                ids = self.app.listar(clave)
            except ErrorDeClave as e:
                self.app.bridge.post(self._modelos_fallo, e)
            else:
                self.app.bridge.post(self._modelos_listos, ids)

        threading.Thread(target=trabajo, daemon=True, name="cargar-modelos").start()

    def _modelos_listos(self, ids: list[str]) -> None:
        self.boton_modelos.state(["!disabled"])
        if ids:
            self.combo.configure(values=ids)
            self.aviso.configure(text=t("opt_models_loaded", n=len(ids)), foreground=self.app.p.ok)

    def _modelos_fallo(self, e: ErrorDeClave) -> None:
        self.boton_modelos.state(["!disabled"])
        self.aviso.configure(text=t("key_offline") if e.tipo == "sin_conexion" else t("key_bad"),
                             foreground=self.app.p.error)

    def _avanzado(self) -> None:
        self.destroy()
        self.app.cambiar_a_avanzado()

    def guardar(self) -> None:
        tope = a_decimal(self.tope.get(), Decimal(-1))
        if tope < 0:
            self.aviso.configure(text=t("opt_bad_cap"), foreground=self.app.p.error)
            return
        self.app.aplicar_opciones(
            modelo=self.modelo.get().strip(), tope=tope, auto=self.auto.get(),
            idioma=self._idiomas.get(self.idioma.get(), ""),
            instrucciones=self.instrucciones.get("1.0", "end-1c").strip(),
            tema=self._temas.get(self.tema.get(), "auto"),
        )
        self.destroy()


class DialogoCierre(Modal):
    """«Quedan preguntas»: salir guardando, seguir en segundo plano o volver."""

    def __init__(self, app: SimpleApp, pendientes: int, con_bandeja: bool):
        super().__init__(app, t("close_title"), 480)
        self.eleccion = "volver"
        cuerpo = ttk.Frame(self, padding=20)
        cuerpo.grid(row=0, column=0)
        ttk.Label(cuerpo, text=t("close_text", n=pendientes), wraplength=420, justify="left").pack(anchor="w")
        fila = ttk.Frame(cuerpo)
        fila.pack(anchor="e", pady=(18, 0))
        ttk.Button(fila, text=t("close_cancel"), command=lambda: self._elegir("volver")).pack(side="left")
        if con_bandeja:
            ttk.Button(fila, text=t("close_background"), command=lambda: self._elegir("fondo")).pack(side="left", padx=(8, 0))
        ttk.Button(fila, text=t("close_quit"), style="Primary.TButton", command=lambda: self._elegir("salir")).pack(
            side="left", padx=(8, 0))
        self.protocol("WM_DELETE_WINDOW", lambda: self._elegir("volver"))
        self.mostrar()

    def _elegir(self, eleccion: str) -> None:
        self.eleccion = eleccion
        self.destroy()


# ----------------------------------------------------------------------
# La ventana
# ----------------------------------------------------------------------


class SimpleApp:
    def __init__(
        self,
        root: tk.Tk,
        config: Optional[Configuracion] = None,
        fabrica: Callable[..., Any] = SequentialEngine,
        probar: Callable[[str], list[str]] = probar_clave,
        listar: Callable[[str], list[str]] = listar_modelos,
        guardar: Callable[[str], str] = guardar_clave,
        abrir_url: Callable[[str], Any] = webbrowser.open,
        usar_bandeja: bool = True,
    ):
        self.root = root
        self.config = config or config_inicial()
        fijar_idioma(self.config.idioma)
        self.probar, self.listar, self.guardar, self.abrir_url = probar, listar, guardar, abrir_url
        self.resultado: Optional[str] = None  # None = salir; "avanzada" = cambiar de modo

        root.title(t("app_title"))
        poner_icono(root)
        avisar_de_errores_inesperados(root)
        root.geometry("1020x660")
        root.minsize(820, 560)
        self.p, self.f = aplicar_estilo(root, self.config.tema)
        self.bridge = UiBridge(root)
        self.cola = Cola(self.config, _EventosDeLaCola(self), publicar=self.bridge.publicar, fabrica=fabrica)

        self._filas: dict[int, str] = {}
        self._parcial: dict[int, str] = {}
        self._seleccion_auto = True
        self._placeholder = False
        self._banner_fijo = False
        self._ultimo_restante: Optional[float] = None
        self._dibujando_estado = EstadoCola()
        self.bandeja: Optional[tray.BandejaDelSistema] = None
        self._usar_bandeja = usar_bandeja and tray.bandeja_disponible()
        self._temporizadores: set[str] = set()

        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.f_bienvenida = ttk.Frame(root, padding=30)
        self.f_principal = ttk.Frame(root, padding=(20, 16, 20, 8))
        for fr in (self.f_bienvenida, self.f_principal):
            fr.grid(row=0, column=0, sticky="nsew")
        self._construir_bienvenida()
        self._construir_principal()
        root.protocol("WM_DELETE_WINDOW", self.cerrar)

        self.pantalla = "bienvenida"
        if obtener_api_key():
            self._entrar()
        else:
            self.f_bienvenida.tkraise()

    # ------------------------------------------------------------------
    # Construcción
    # ------------------------------------------------------------------

    def _construir_bienvenida(self) -> None:
        f = self.f_bienvenida
        f.columnconfigure(0, weight=1)
        caja = ttk.Frame(f)
        caja.grid(row=0, column=0)
        ttk.Label(caja, text=t("welcome_title"), style="Titulo.TLabel").pack(anchor="w")
        ttk.Label(caja, text=t("welcome_text"), wraplength=560, justify="left").pack(anchor="w", pady=(10, 14))
        for clave in ("welcome_step1", "welcome_step2", "welcome_step3"):
            ttk.Label(caja, text=t(clave), wraplength=560, justify="left").pack(anchor="w", pady=2)
        ttk.Button(caja, text=t("welcome_open_console"), command=lambda: self.abrir_url(URL_CONSOLA)).pack(
            anchor="w", pady=(12, 14))
        self.panel_clave = PanelClave(caja, self, lambda modelos: self._clave_lista(modelos))
        self.panel_clave.pack(fill="x")
        ttk.Label(caja, text=t("welcome_privacy"), style="Pequeno.TLabel", wraplength=560, justify="left").pack(
            anchor="w", pady=(18, 0))

    def _construir_principal(self) -> None:
        p, fu = self.p, self.f
        f = self.f_principal
        f.columnconfigure(0, weight=1)

        # Cabecera
        cab = ttk.Frame(f)
        cab.grid(row=0, column=0, sticky="ew")
        cab.columnconfigure(0, weight=1)
        ttk.Label(cab, text=t("app_title"), style="Titulo.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(cab, text=t("tagline"), style="Sub.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Button(cab, text=t("options"), command=self.abrir_opciones).grid(row=0, column=1, rowspan=2, sticky="e")

        # Banner de avisos (oculto hasta que haga falta)
        self.banner = tk.Frame(f, highlightthickness=1)
        self.banner_texto = tk.Label(self.banner, anchor="w", justify="left", wraplength=860, font=fu.base)
        self.banner_texto.pack(fill="x", padx=14, pady=(10, 2))
        self.banner_detalle = tk.Label(self.banner, anchor="w", justify="left", wraplength=860, font=fu.pequena)
        self.banner_detalle.pack(fill="x", padx=14)
        self.banner_botones = tk.Frame(self.banner)
        self.banner_botones.pack(anchor="w", padx=10, pady=(4, 10))

        # Pregunta: una caja compacta arriba, como en un chat
        f.rowconfigure(2, weight=0)
        f.rowconfigure(4, weight=1)
        ttk.Label(f, text=t("ask"), style="Seccion.TLabel").grid(row=2, column=0, sticky="w", pady=(14, 6))
        entrada_fila = ttk.Frame(f)
        entrada_fila.grid(row=3, column=0, sticky="ew")
        entrada_fila.columnconfigure(0, weight=1)
        caja = tarjeta(entrada_fila, p)
        caja.grid(row=0, column=0, sticky="ew")
        caja.columnconfigure(0, weight=1)
        self.entrada = tk.Text(caja, wrap="word", height=4, relief="flat", font=fu.texto, padx=12, pady=10,
                               background=p.superficie, foreground=p.texto, insertbackground=p.texto,
                               undo=True, borderwidth=0, highlightthickness=0)
        self.entrada.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(caja, orient="vertical", command=self.entrada.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.entrada.configure(yscrollcommand=sb.set)
        self.entrada.bind("<Return>", self._al_pulsar_enter)
        self.entrada.bind("<FocusIn>", self._quitar_placeholder)
        self.entrada.bind("<FocusOut>", lambda e: self._poner_placeholder())
        self._poner_placeholder()

        botones = ttk.Frame(entrada_fila)
        botones.grid(row=0, column=1, sticky="ns", padx=(10, 0))
        self.boton_enviar = ttk.Button(botones, text=t("send"), style="Primary.TButton", command=self.enviar)
        self.boton_enviar.pack(fill="x")
        self.boton_pausa = ttk.Button(botones, text=t("pause"), command=self.alternar_pausa)
        self.boton_pausa.pack(fill="x", pady=(8, 0))

        ttk.Label(f, text=t("hint_keys") + "   ·   " + t("hint_saved"), style="Pequeno.TLabel").grid(
            row=5, column=0, sticky="w", pady=(6, 0))

        # Cola y respuesta: ocupan el resto de la ventana
        cuerpo = ttk.PanedWindow(f, orient="horizontal")
        self.cuerpo = cuerpo
        cuerpo.grid(row=4, column=0, sticky="nsew", pady=(14, 0))
        izq = ttk.Frame(cuerpo, padding=(0, 0, 8, 0))
        der = ttk.Frame(cuerpo, padding=(8, 0, 0, 0))
        cuerpo.add(izq, weight=2)
        cuerpo.add(der, weight=3)

        izq.columnconfigure(0, weight=1)
        izq.rowconfigure(1, weight=1)
        ttk.Label(izq, text=t("queue"), style="Seccion.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        tabla = tarjeta(izq, p)
        tabla.grid(row=1, column=0, sticky="nsew")
        tabla.columnconfigure(0, weight=1)
        tabla.rowconfigure(0, weight=1)
        self.tabla = ttk.Treeview(tabla, columns=("n", "q", "s"), show="headings", selectmode="browse", height=6)
        self.tabla.heading("n", text="#")
        self.tabla.heading("q", text=t("col_q"), anchor="w")
        self.tabla.heading("s", text=t("col_s"), anchor="w")
        self.tabla.column("n", width=36, minwidth=36, anchor="center", stretch=False)
        self.tabla.column("q", width=200, minwidth=90, anchor="w")
        self.tabla.column("s", width=112, minwidth=100, anchor="w", stretch=False)
        self.tabla.grid(row=0, column=0, sticky="nsew")
        self.vacio = tk.Label(tabla, text=t("queue_empty"), background=p.superficie, foreground=p.apagado,
                              font=fu.base, justify="center", wraplength=260)
        sb2 = ttk.Scrollbar(tabla, orient="vertical", command=self.tabla.yview)
        sb2.grid(row=0, column=1, sticky="ns")
        self.tabla.configure(yscrollcommand=sb2.set)
        for tag, color in (("lista", p.ok), ("error", p.error), ("enviando", p.acento_texto), ("espera", p.aviso),
                           ("cola", p.gris), ("rechazada", p.aviso), ("quitada", p.gris)):
            self.tabla.tag_configure(tag, foreground=color)
        self.tabla.bind("<<TreeviewSelect>>", self._al_seleccionar)
        # La intención de la persona se lee del ratón/teclado, no del evento de selección: Tk lo
        # entrega DESPUÉS de cualquier bandera mía y no distingue un clic de una selección hecha por código.
        self.tabla.bind("<ButtonRelease-1>", lambda e: self.root.after_idle(self._fijar_modo_seleccion))
        for tecla in ("<KeyRelease-Up>", "<KeyRelease-Down>", "<KeyRelease-Prior>", "<KeyRelease-Next>"):
            self.tabla.bind(tecla, lambda e: self.root.after_idle(self._fijar_modo_seleccion))
        self.tabla.bind("<Button-3>", self._menu_contextual)
        self.tabla.bind("<Button-2>", self._menu_contextual)
        self.tabla.bind("<Control-Button-1>", self._menu_contextual)
        ttk.Button(izq, text=t("new_queue"), command=self.nueva_cola).grid(row=2, column=0, sticky="w", pady=(10, 0))

        der.columnconfigure(0, weight=1)
        der.rowconfigure(1, weight=1)
        ttk.Label(der, text=t("answer"), style="Seccion.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        resp = tarjeta(der, p)
        resp.grid(row=1, column=0, sticky="nsew")
        resp.columnconfigure(0, weight=1)
        resp.rowconfigure(0, weight=1)
        self.respuesta = tk.Text(resp, wrap="word", height=8, relief="flat", font=fu.texto, padx=14, pady=12,
                                 background=p.superficie, foreground=p.texto, borderwidth=0,
                                 highlightthickness=0, state="disabled", cursor="arrow", spacing3=4)
        self.respuesta.grid(row=0, column=0, sticky="nsew")
        sb3 = ttk.Scrollbar(resp, orient="vertical", command=self.respuesta.yview)
        sb3.grid(row=0, column=1, sticky="ns")
        self.respuesta.configure(yscrollcommand=sb3.set)
        self.respuesta.tag_configure("apagado", foreground=p.apagado)

        acciones = ttk.Frame(der)
        acciones.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(acciones, text=t("copy"), command=self.copiar_respuesta).pack(side="left")
        ttk.Button(acciones, text=t("save_all"), command=self.guardar_todo).pack(side="left", padx=(8, 0))
        self.var_formato = tk.BooleanVar(value=self.config.mostrar_formato)
        self.check_formato = ttk.Checkbutton(acciones, text=t("format"), variable=self.var_formato,
                                             command=self._cambiar_formato)
        self.check_formato.pack(side="right")

        # Barra de estado
        barra = ttk.Frame(f)
        barra.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        barra.columnconfigure(1, weight=1)
        self.punto = tk.Label(barra, text="●", background=p.fondo, foreground=p.ok, font=fu.base)
        self.punto.grid(row=0, column=0, padx=(0, 6))
        self.var_estado = tk.StringVar(value=t("status_idle"))
        ttk.Label(barra, textvariable=self.var_estado).grid(row=0, column=1, sticky="w")
        self.var_gasto = tk.StringVar()
        ttk.Label(barra, textvariable=self.var_gasto, style="Sub.TLabel").grid(row=0, column=2, sticky="e")
        self.menu = tk.Menu(self.root, tearoff=0)
        self._actualizar_vacio()
        # El reparto inicial lo decide el contenido; se fija a mano para que la cola se lea entera.
        self._luego(60, self._ajustar_reparto)

    def _luego(self, ms: int, fn: Callable[[], Any]) -> None:
        """`after` que se anula solo al cerrar: un temporizador vivo sobre una ventana destruida da errores."""
        def ejecutar() -> None:
            self._temporizadores.discard(ident)
            fn()

        ident = self.root.after(ms, ejecutar)
        self._temporizadores.add(ident)

    def _ajustar_reparto(self) -> None:
        try:
            ancho = self.cuerpo.winfo_width()
            if ancho > 200:
                self.cuerpo.sashpos(0, max(330, int(ancho * 0.38)))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Arranque
    # ------------------------------------------------------------------

    def _clave_lista(self, modelos: list[str]) -> None:
        elegido = elegir_modelo_por_defecto(modelos, self.config.modelo)
        if elegido != self.config.modelo:
            self.config.modelo = elegido
            ConfigManager.guardar(self.config)
        self._luego(700, self._entrar)

    def _entrar(self) -> None:
        self.pantalla = "principal"
        self.f_principal.tkraise()
        n = self.cola.cargar_ultima()
        for m in self.cola.preguntas():
            self.on_orden(m)
        self._refrescar_gasto()
        self.entrada.focus_set()
        if n:
            if self.config.auto_arranque:
                self.cola.reanudar()
            else:
                self.cola.pausar()
                self._mostrar_banner(t("resumed_found", n=n), "", [(t("btn_retry"), self.reanudar_pendientes)],
                                     "info", fijo=True)
        else:
            self._pintar_estado(self.cola.estado)

    def reanudar_pendientes(self) -> None:
        self._banner_fijo = False
        self._ocultar_banner()
        self.cola.reanudar()

    # ------------------------------------------------------------------
    # Entrada
    # ------------------------------------------------------------------

    def _poner_placeholder(self) -> None:
        if self.entrada.get("1.0", "end-1c").strip():
            return
        self._placeholder = True
        self.entrada.delete("1.0", "end")
        self.entrada.insert("1.0", t("placeholder"))
        self.entrada.configure(foreground=self.p.apagado)

    def _quitar_placeholder(self, _e: Any = None) -> None:
        if self._placeholder:
            self.entrada.delete("1.0", "end")
            self.entrada.configure(foreground=self.p.texto)
            self._placeholder = False

    def _al_pulsar_enter(self, evento: tk.Event) -> Optional[str]:
        if evento.state & 0x1:  # Mayús+Enter: salto de línea normal
            return None
        self.enviar()
        return "break"

    def texto_de_entrada(self) -> str:
        return "" if self._placeholder else self.entrada.get("1.0", "end-1c").strip()

    def enviar(self) -> None:
        texto = self.texto_de_entrada()
        if not texto:
            return
        if not obtener_api_key():
            self.pantalla = "bienvenida"
            self.f_bienvenida.tkraise()
            return
        self._quitar_placeholder()
        self.entrada.delete("1.0", "end")
        self._seleccion_auto = True
        self._banner_fijo = False
        self.cola.agregar(texto)
        if self.cola.pausada:
            self.cola.reanudar()  # pulsar Enviar es querer que salga
        self.entrada.focus_set()

    def alternar_pausa(self) -> None:
        if self.cola.pausada:
            self.reanudar_pendientes()
        else:
            self.cola.pausar()

    # ------------------------------------------------------------------
    # Eventos de la cola
    # ------------------------------------------------------------------

    def _etiqueta_de(self, msg: JobMessage) -> tuple[str, str]:
        """(texto de estado, etiqueta de color) de una fila."""
        e = self._dibujando_estado
        en_marcha = e.msg_id == msg.id and e.tipo in ("sending", "waiting")
        if msg.status == STATUS_PENDING:
            if en_marcha:
                return (t("st_waiting"), "espera") if e.tipo == "waiting" else (t("st_sending"), "enviando")
            return t("st_pending"), "cola"
        return {
            STATUS_COMPLETED: (t("st_done"), "lista"),
            STATUS_REFUSED: (t("st_refused"), "rechazada"),
            STATUS_FAILED: (t("st_failed"), "error"),
            STATUS_SKIPPED: (t("st_skipped"), "quitada"),
        }.get(msg.status, (msg.status, "cola"))

    def on_orden(self, msg: JobMessage) -> None:
        estado, etiqueta = self._etiqueta_de(msg)
        resumen = " ".join(msg.text.split())
        resumen = resumen if len(resumen) <= 90 else resumen[:89] + "…"
        valores = (msg.id, resumen, estado)
        iid = self._filas.get(msg.id)
        if iid is None:
            iid = self.tabla.insert("", "end", values=valores, tags=(etiqueta,))
            self._filas[msg.id] = iid
            self._actualizar_vacio()
            self.tabla.see(iid)
        else:
            self.tabla.item(iid, values=valores, tags=(etiqueta,))
        if msg.status == STATUS_COMPLETED or msg.status == STATUS_REFUSED or msg.status == STATUS_FAILED:
            self._parcial.pop(msg.id, None)
            if self._seleccion_auto:
                self._seleccionar(msg.id)
            elif self._seleccionada() == msg.id:
                self._mostrar_respuesta(msg.id)
        self._refrescar_gasto()
        if self.cola.estado.tipo == "idle":
            self._pintar_estado(self.cola.estado)

    def on_estado(self, e: EstadoCola) -> None:
        self._dibujando_estado = e
        self._ultimo_restante = e.segundos if e.tipo == "waiting" else None
        self._pintar_estado(e)
        if e.tipo == "stopped":
            self._banner_de_parada(e)
        elif not self._banner_fijo:
            self._ocultar_banner()
        if e.msg_id is not None and e.tipo in ("sending", "waiting"):
            msg = self.cola.job.mensaje(e.msg_id) if self.cola.job else None
            if msg is not None:
                self.on_orden(msg)
            if e.tipo == "sending" and self._seleccion_auto:
                self._seleccionar(e.msg_id)

    def on_cuenta_atras(self, restante: float) -> None:
        if self._dibujando_estado.tipo == "waiting":
            self._ultimo_restante = restante
            self._pintar_estado(self._dibujando_estado)

    def on_delta(self, msg_id: int, texto: str) -> None:
        self._parcial[msg_id] = self._parcial.get(msg_id, "") + texto
        if self._seleccionada() == msg_id:
            self._escribir_respuesta(self._parcial[msg_id])

    def on_gasto(self, usd: Decimal) -> None:
        self._refrescar_gasto()

    def on_error(self, texto: str) -> None:
        logger.info("La cola informa: %s", texto)

    # ------------------------------------------------------------------
    # Pintar
    # ------------------------------------------------------------------

    def _pintar_estado(self, e: EstadoCola) -> None:
        n = self.cola.pendientes()
        color = self.p.ok
        if e.tipo == "sending":
            texto, color = t("status_sending", n=e.msg_id or "?"), self.p.acento_texto
        elif e.tipo == "waiting":
            restante = self._ultimo_restante if self._ultimo_restante is not None else e.segundos
            texto = t(f"status_wait_{e.motivo}", t=formatear_tiempo(restante)) if e.motivo else t("status_stopped")
            color = self.p.aviso
        elif e.tipo == "paused":
            texto = t("status_paused_n", n=n) if n else t("status_paused")
            color = self.p.gris
        elif e.tipo == "stopped":
            texto, color = t("status_stopped"), self.p.error
        else:
            texto = t("status_idle_n", n=n) if n else t("status_idle")
        self.var_estado.set(texto)
        self.punto.configure(foreground=color)
        self.boton_pausa.configure(text=t("resume") if self.cola.pausada else t("pause"))

    def _refrescar_gasto(self) -> None:
        gastado = formatear_usd(self.cola.gastado())
        tope = self.config.tope_gasto_usd
        if tope > 0:
            self.var_gasto.set(t("spend", gastado=gastado, tope=formatear_usd(tope, 2)))
        else:
            self.var_gasto.set(t("spend_unlimited", gastado=gastado))

    def _seleccionada(self) -> Optional[int]:
        sel = self.tabla.selection()
        if not sel:
            return None
        valores = self.tabla.item(sel[0], "values")
        return int(valores[0]) if valores else None

    def _seleccionar(self, msg_id: int) -> None:
        iid = self._filas.get(msg_id)
        if iid is None:
            return
        self.tabla.selection_set(iid)
        self.tabla.see(iid)
        self._mostrar_respuesta(msg_id)

    def _al_seleccionar(self, _e: Any = None) -> None:
        sel = self._seleccionada()
        if sel is not None:
            self._mostrar_respuesta(sel)

    def _fijar_modo_seleccion(self) -> None:
        """Si la persona elige una fila que no es la última, deja de saltar sola a la más reciente."""
        sel = self._seleccionada()
        self._seleccion_auto = sel is None or sel == max(self._filas, default=sel)

    def _escribir_respuesta(self, texto: str, apagado: bool = False, con_formato: bool = False) -> None:
        """`con_formato` es para la respuesta ya terminada; mientras llega (streaming) se ve el texto tal cual."""
        if con_formato:
            formato.mostrar_texto_con_formato(self.respuesta, texto, self.p, self.f, self.config.mostrar_formato)
            self.respuesta.yview_moveto(0.0)  # una respuesta terminada se lee desde el principio
            return
        self.respuesta.configure(state="normal")
        self.respuesta.delete("1.0", "end")
        self.respuesta.insert("1.0", texto, ("apagado",) if apagado else ())
        self.respuesta.configure(state="disabled")
        if not apagado:
            self.respuesta.see("end")

    def _cambiar_formato(self) -> None:
        self.config.mostrar_formato = bool(self.var_formato.get())
        ConfigManager.guardar(self.config)
        sel = self._seleccionada()
        if sel is not None:
            self._mostrar_respuesta(sel)

    def texto_de_respuesta(self, msg_id: int) -> str:
        job = self.cola.job
        msg = job.mensaje(msg_id) if job else None
        if msg is None:
            return ""
        if msg.status in (STATUS_COMPLETED, STATUS_REFUSED):
            return leer_respuesta(job, msg_id, job.ciclos_completados + 1)
        if msg.status == STATUS_FAILED:
            return msg.detalle
        return self._parcial.get(msg_id, "")

    def _mostrar_respuesta(self, msg_id: int) -> None:
        msg = self.cola.job.mensaje(msg_id) if self.cola.job else None
        texto = self.texto_de_respuesta(msg_id)
        if texto:
            terminada = msg is not None and msg.status in (STATUS_COMPLETED, STATUS_REFUSED)
            self._escribir_respuesta(texto, con_formato=terminada)
        elif msg is not None and msg.status == STATUS_PENDING:
            self._escribir_respuesta(t("answer_waiting"), apagado=True)
        else:
            self._escribir_respuesta(t("answer_empty"), apagado=True)

    # ------------------------------------------------------------------
    # Avisos
    # ------------------------------------------------------------------

    def _mostrar_banner(self, texto: str, detalle: str, botones: list[tuple[str, Callable[[], None]]],
                        tono: str, fijo: bool = False) -> None:
        p = self.p
        fondo, borde, color = {
            "error": (p.banner_error_fondo, p.banner_error_borde, p.error),
            "info": (p.banner_info_fondo, p.banner_info_borde, p.acento_texto),
        }.get(tono, (p.banner_aviso_fondo, p.banner_aviso_borde, p.aviso))
        self.banner.configure(background=fondo, highlightbackground=borde)
        self.banner_botones.configure(background=fondo)
        self.banner_texto.configure(text=texto, background=fondo, foreground=color)
        self.banner_detalle.configure(text=detalle, background=fondo, foreground=p.apagado)
        if not detalle:
            self.banner_detalle.pack_forget()
        else:
            self.banner_detalle.pack(fill="x", padx=14, after=self.banner_texto)
        for w in self.banner_botones.winfo_children():
            w.destroy()
        for etiqueta, accion in botones:
            ttk.Button(self.banner_botones, text=etiqueta, command=accion).pack(side="left", padx=4)
        self._banner_fijo = fijo
        self.banner.grid(row=1, column=0, sticky="ew", pady=(12, 0))

    def _ocultar_banner(self) -> None:
        self.banner.grid_remove()

    def _banner_de_parada(self, e: EstadoCola) -> None:
        razon = e.razon
        detalle = e.texto or self.cola.ultimo_error
        reintentar = (t("btn_retry"), self.reanudar_pendientes)
        consola = (t("btn_open_console"), lambda: self.abrir_url(URL_FACTURACION))
        if razon is StopReason.BUDGET:
            texto = t("banner_budget", gastado=formatear_usd(self.cola.gastado()),
                      tope=formatear_usd(self.config.tope_gasto_usd, 2))
            self._mostrar_banner(texto, "", [(t("btn_raise_cap"), self.abrir_opciones)], "aviso")
        elif razon is StopReason.NO_CREDIT:
            self._mostrar_banner(t("banner_no_credit"), "", [consola, reintentar], "aviso")
        elif razon is StopReason.ACCOUNT_LIMIT:
            self._mostrar_banner(t("banner_account_limit"), t("banner_detail", texto=detalle), [consola, reintentar], "aviso")
        elif razon is StopReason.NETWORK:
            self._mostrar_banner(t("banner_network"), "", [reintentar], "aviso")
        else:
            fallida = next((m for m in reversed(self.cola.preguntas()) if m.status == STATUS_FAILED), None)
            tipo = fallida.error_type if fallida else ""
            if tipo == "auth":
                self._mostrar_banner(t("banner_auth"), "", [(t("btn_change_key"), self.abrir_opciones)], "error")
            elif tipo == "model_not_found":
                self._mostrar_banner(t("banner_model"), "", [(t("btn_choose_model"), self.abrir_opciones)], "error")
            else:
                self._mostrar_banner(t("banner_fatal"), t("banner_detail", texto=detalle) if detalle else "",
                                     [reintentar], "error")

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------

    def _confirmar(self, titulo: str, texto: str) -> bool:
        return messagebox.askyesno(titulo, texto, parent=self.root)

    def _info(self, titulo: str, texto: str) -> None:
        messagebox.showinfo(titulo, texto, parent=self.root)

    def _error(self, titulo: str, texto: str) -> None:
        messagebox.showerror(titulo, texto, parent=self.root)

    def _decir_en_barra(self, texto: str, ms: int = 2500) -> None:
        """Mensaje pasajero en la barra de estado; después vuelve a mostrar el estado real."""
        self.var_estado.set(texto)
        self._luego(ms, lambda: self.var_estado.get() == texto and self._pintar_estado(self._dibujando_estado))

    def copiar_respuesta(self) -> None:
        sel = self._seleccionada()
        texto = self.texto_de_respuesta(sel) if sel is not None else ""
        if texto:
            self._copiar(texto)

    def _copiar(self, texto: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(texto)
        self._decir_en_barra(t("copied"))

    def guardar_todo(self) -> None:
        job = self.cola.job
        if job is None or not job.messages:
            self._info(t("save_title"), t("save_empty"))
            return
        ruta = filedialog.asksaveasfilename(
            parent=self.root, title=t("save_title"), defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("CSV (Excel)", "*.csv"), ("Texto", "*.txt"), ("JSON Lines", "*.jsonl")],
        )
        if ruta:
            self.exportar_a(ruta)

    def exportar_a(self, ruta: str) -> bool:
        try:
            destino = exportar(self.cola.job, Path(ruta))
        except (ErrorDeExportacion, OSError) as e:
            self._error(t("save_title"), t("save_error", texto=str(e)))
            return False
        self._decir_en_barra(t("save_done", ruta=destino), 4000)
        return True

    def _actualizar_vacio(self) -> None:
        """El mensaje de «aquí irán tus preguntas» solo se ve mientras la cola está vacía."""
        if self._filas:
            self.vacio.place_forget()
        else:
            self.vacio.place(relx=0.5, rely=0.42, anchor="center")
            self.vacio.lift()
            self._escribir_respuesta(t("answer_placeholder"), apagado=True)

    def nueva_cola(self) -> None:
        if self.cola.activa and not self.cola.pausada:
            self._info(t("new_queue"), t("new_blocked"))
            return
        if self.cola.preguntas() and not self._confirmar(t("new_queue"), t("new_confirm")):
            return
        if self.cola.nueva():
            self.tabla.delete(*self.tabla.get_children())
            self._filas.clear()
            self._parcial.clear()
            self._escribir_respuesta(t("answer_empty"), apagado=True)
            self._ocultar_banner()
            self._banner_fijo = False
            self._seleccion_auto = True
            self._refrescar_gasto()
            self._actualizar_vacio()

    def volver_a_preguntar(self, msg_id: int) -> None:
        msg = self.cola.job.mensaje(msg_id) if self.cola.job else None
        if msg is not None:
            self.cola.agregar(msg.text)
            if self.cola.pausada:
                self.cola.reanudar()

    def quitar_de_la_cola(self, msg_id: int) -> None:
        self.cola.quitar(msg_id)

    def _menu_contextual(self, evento: tk.Event) -> None:
        iid = self.tabla.identify_row(evento.y)
        if not iid:
            return
        self.tabla.selection_set(iid)
        msg_id = int(self.tabla.item(iid, "values")[0])
        msg = self.cola.job.mensaje(msg_id)
        self.menu.delete(0, "end")
        self.menu.add_command(label=t("menu_copy_answer"), command=self.copiar_respuesta,
                              state="normal" if msg.status in (STATUS_COMPLETED, STATUS_REFUSED) else "disabled")
        self.menu.add_command(label=t("menu_copy_question"), command=lambda: self._copiar(msg.text))
        self.menu.add_command(label=t("menu_ask_again"), command=lambda: self.volver_a_preguntar(msg_id))
        self.menu.add_command(label=t("menu_remove"), command=lambda: self.quitar_de_la_cola(msg_id),
                              state="normal" if msg.status == STATUS_PENDING else "disabled")
        try:
            self.menu.tk_popup(evento.x_root, evento.y_root)
        finally:
            self.menu.grab_release()

    # ------------------------------------------------------------------
    # Opciones
    # ------------------------------------------------------------------

    def abrir_opciones(self) -> None:
        DialogoOpciones(self)

    def aplicar_opciones(self, modelo: str, tope: Decimal, auto: bool, idioma: str,
                         instrucciones: Optional[str] = None, tema: Optional[str] = None) -> None:
        sube_el_tope = tope > self.config.tope_gasto_usd or tope == 0
        self.config.modelo = modelo or self.config.modelo
        if instrucciones is not None:
            self.config.system_prompt = instrucciones
        self.config.tope_gasto_usd = tope
        self.config.auto_arranque = auto
        self.config.idioma = idioma
        if tema in TEMAS:
            self.config.tema = tema
        ConfigManager.guardar(self.config)
        self.cola.actualizar_config(self.config)
        self._refrescar_gasto()
        # Si la cola se paró por el tope y la persona lo ha subido, tiene sentido seguir.
        if sube_el_tope and self.cola.estado.razon is StopReason.BUDGET:
            self.reanudar_pendientes()
        elif self.cola.estado.razon is StopReason.FATAL and self.cola.pendientes():
            self.reanudar_pendientes()  # cambió la clave o el modelo: se reintenta

    def cambiar_a_avanzado(self) -> None:
        self.config.interfaz = "avanzada"
        ConfigManager.guardar(self.config)
        self.resultado = "avanzada"
        self._salir()

    # ------------------------------------------------------------------
    # Cierre
    # ------------------------------------------------------------------

    def cerrar(self) -> None:
        pendientes = self.cola.pendientes()
        if pendientes and not self.cola.pausada:
            d = DialogoCierre(self, pendientes, con_bandeja=self._usar_bandeja)
            self.root.wait_window(d)
            if d.eleccion == "volver":
                return
            if d.eleccion == "fondo" and self._segundo_plano():
                return
        self._salir()

    def _segundo_plano(self) -> bool:
        self.bandeja = tray.BandejaDelSistema(
            t("app_title"), t("tray_open"), t("tray_quit"),
            al_abrir=lambda: self.bridge.post(self._restaurar),
            al_salir=lambda: self.bridge.post(self._salir),
        )
        if not self.bandeja.mostrar():
            return False
        self.root.withdraw()
        return True

    def _restaurar(self) -> None:
        if self.bandeja:
            self.bandeja.ocultar()
            self.bandeja = None
        self.root.deiconify()
        self.root.lift()

    def _salir(self) -> None:
        self.cola.cerrar()
        if self.bandeja:
            self.bandeja.ocultar()
        self.bridge.drenar()
        self.bridge.cerrar()
        for ident in list(self._temporizadores):
            try:
                self.root.after_cancel(ident)
            except tk.TclError:
                pass
        self._temporizadores.clear()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> Optional[str]:
        self.root.mainloop()
        return self.resultado


def ejecutar_simple(config: Optional[Configuracion] = None) -> Optional[str]:
    """Abre el modo simple. Devuelve 'avanzada' si la persona pidió cambiar de modo."""
    root = tk.Tk()
    app = SimpleApp(root, config=config)
    resultado = app.run()
    # Las variables de tkinter se liberan aquí, en el hilo de la ventana, y no en un hilo de trabajo
    # cuando el recolector pase por casualidad (importa al cambiar de modo y abrir otra ventana).
    del app, root
    gc.collect()
    return resultado

