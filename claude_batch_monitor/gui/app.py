"""Modo avanzado: todos los ajustes, lotes grandes (CSV/JSONL), resultados, exportación y reanudación.

Hace lo mismo que la terminal, con ventana. Para preguntar cosas sueltas está el modo simple.
Esta ventana solo dibuja y recoge ajustes; el trabajo lo hace `EjecutorLote` en otro hilo y
cada evento llega por el puente al hilo de la ventana, que es el único que toca tkinter.
"""

from __future__ import annotations

import gc
import os
import threading
import tkinter as tk
import webbrowser
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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
from ..cola import ETIQUETA as ETIQUETA_COLA
from ..config import ConfigManager, Configuracion
from ..core.base import FRASES_DE_PARADA, StopReason
from ..ejecutor import EjecutorLote, EventosLote, motor_para
from ..estimator import Estimacion, estimar
from ..export import FORMATOS, ErrorDeExportacion, exportar, leer_respuesta
from ..inputs import (
    CAMPO_POR_DEFECTO,
    ErrorDeEntrada,
    aplicar_plantilla,
    leer_ordenes,
    listar_campos,
)
from ..job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REFUSED,
    STATUS_SKIPPED,
    Job,
    JobMessage,
    listar_jobs,
    purgar_jobs_antiguos,
)
from ..notify import ErrorDeTelegram, credenciales_telegram
from ..notify import probar_telegram as enviar_prueba_telegram
from ..parsing import DEFAULT_SEPARATOR, split_messages
from ..paths import APP_VERSION, logger
from ..platform_utils import abrir_ruta
from ..pricing import (
    DEFAULT_MODEL,
    MODELOS_SUGERIDOS,
    NIVELES_ESFUERZO,
    Consumo,
    descripcion_modelo,
    formatear_precio,
    formatear_usd,
    resolver_precios,
)
from ..state import (
    KEY_TELEGRAM_TOKEN,
    EstadoMonitor,
    SecureStorage,
    obtener_api_key,
    origen_api_key,
)
from . import tray
from .bridge import UiBridge
from .comun import URL_FACTURACION, Desplazable, avisar_de_errores_inesperados, poner_icono
from .dialogos import DialogoColumna, DialogoEstimacion, DialogoLotes, VisorRespuesta
from .estilo import aplicar_estilo

SIN_ESFUERZO = "(no enviar)"
TEMAS_VISIBLES = {"Automático": "auto", "Claro": "claro", "Oscuro": "oscuro"}  # lo que se lee -> lo que se guarda
MAX_LINEAS_LOG = 300
MAX_FILAS = 20_000          # más filas que esto no hacen la tabla útil y sí lenta
MAX_BORRADOR = 2_000_000    # el texto de la caja se guarda como borrador; un CSV enorme no se copia a config.json

NOMBRES_ESTADO = {
    STATUS_PENDING: "En cola",
    STATUS_COMPLETED: "Lista",
    STATUS_REFUSED: "Rechazada",
    STATUS_FAILED: "Error",
    STATUS_SKIPPED: "Omitida",
}

AYUDA_FORMATOS = [
    ("Órdenes (.txt .md .csv .tsv .jsonl)", "*.txt *.md *.csv *.tsv *.jsonl *.ndjson"),
    ("Texto", "*.txt *.md"),
    ("Hoja de cálculo", "*.csv *.tsv"),
    ("JSON Lines", "*.jsonl *.ndjson"),
    ("Todos los archivos", "*.*"),
]


class ErrorDeAjustes(ValueError):
    """Una casilla de los ajustes tiene un valor que no se puede usar (el texto dice cuál y por qué)."""


def _entero(texto: str, nombre: str, minimo: int, maximo: int) -> int:
    try:
        n = int(str(texto).strip())
    except ValueError:
        raise ErrorDeAjustes(f"{nombre}: «{str(texto).strip()}» no es un número entero.") from None
    if not minimo <= n <= maximo:
        raise ErrorDeAjustes(f"{nombre}: tiene que estar entre {minimo} y {maximo}.")
    return n


def _decimal_de_casilla(texto: str, nombre: str, opcional: bool = False) -> Optional[Decimal]:
    limpio = str(texto).strip().replace(",", ".")
    if not limpio:
        if opcional:
            return None
        raise ErrorDeAjustes(f"{nombre}: escribe un número (0 = sin tope).")
    try:
        d = Decimal(limpio)
    except InvalidOperation:
        raise ErrorDeAjustes(f"{nombre}: «{str(texto).strip()}» no es un número.") from None
    if not d.is_finite() or d < 0:
        raise ErrorDeAjustes(f"{nombre}: tiene que ser un número igual o mayor que 0.")
    return d


def _hora(texto: str) -> str:
    limpio = texto.strip()
    if not limpio:
        return ""
    try:
        datetime.strptime(limpio, "%H:%M")
    except ValueError:
        raise ErrorDeAjustes(f"Hora límite: «{limpio}» no es una hora válida (usa HH:MM, por ejemplo 23:30).") from None
    return limpio


def _texto_decimal(d: Optional[Decimal]) -> str:
    if d is None:
        return ""
    return format(d.normalize(), "f") if d else "0"


def _acortar(texto: str, largo: int = 100) -> str:
    uno = " ".join(texto.split())
    return uno if len(uno) <= largo else uno[: largo - 1] + "…"


def _miles(n: int) -> str:
    return f"{n:,}".replace(",", ".")


class _EventosDelLote(EventosLote):
    """Reenvía lo que cuenta el ejecutor a la ventana (ya llega en el hilo de la ventana)."""

    def __init__(self, app: AppAvanzada):
        self.app = app

    def estado(self, texto: str, activo: bool) -> None: self.app.on_estado(texto, activo)
    def inicio_orden(self, msg: JobMessage) -> None: self.app.on_inicio_orden(msg)
    def resultado(self, msg: JobMessage) -> None: self.app.on_resultado(msg)
    def fin_ciclo(self, completadas: int, total: int, ciclo: int) -> None: self.app.on_fin_ciclo(completadas, total, ciclo)
    def error(self, texto: str) -> None: self.app.on_error(texto)
    def tick(self, total: float, restante: float) -> None: self.app.on_tick(total, restante)
    def gasto(self, usd: Decimal, consumo: Consumo) -> None: self.app.on_gasto(usd)
    def terminado(self, razon: StopReason, job: Job) -> None: self.app.on_terminado(razon, job)


class AppAvanzada:
    def __init__(
        self,
        root: tk.Tk,
        config: Optional[Configuracion] = None,
        fabrica: Callable[[Configuracion, Job], Any] = motor_para,
        usar_bandeja: bool = True,
        probar: Callable[[str], list[str]] = probar_clave,
        listar: Callable[[str], list[str]] = listar_modelos,
        guardar: Callable[[str], str] = guardar_clave,
        abrir_url: Callable[[str], Any] = webbrowser.open,
        abrir: Callable[[Path], bool] = abrir_ruta,
        cliente_conteo: Callable[[], Any] = lambda: None,
        arrancar: bool = True,
    ):
        self.root = root
        self.config = config or ConfigManager.cargar()
        self.probar, self.listar, self.guardar = probar, listar, guardar
        self.abrir_url, self.abrir, self.cliente_conteo = abrir_url, abrir, cliente_conteo
        self.resultado: Optional[str] = None  # None = salir; "simple" = cambiar de modo

        root.title(f"Claude Batch Monitor v{APP_VERSION}")
        poner_icono(root)
        avisar_de_errores_inesperados(root)
        ancho, alto = 900, min(860, max(660, root.winfo_screenheight() - 90))
        root.geometry(f"{ancho}x{alto}")
        root.minsize(780, 660)
        self.p, self.f = aplicar_estilo(root, self.config.tema)
        self.bridge = UiBridge(root)
        self.ejecutor = EjecutorLote(publicar=self.bridge.publicar, fabrica=fabrica)

        self.job: Optional[Job] = None
        self._config_en_marcha: Optional[Configuracion] = None
        self._filas: dict[int, str] = {}
        self._hechas: set[int] = set()
        self._enviando: Optional[int] = None
        self._ocupado = False
        self._ultimo_error = ""
        self._temporizadores: set[str] = set()
        self._guardado: Optional[str] = None
        self._cargando = True
        self._resumen_final_visible = False
        self.bandeja: Optional[tray.BandejaDelSistema] = None
        self._usar_bandeja = usar_bandeja and tray.bandeja_disponible()
        self._archivo_cargado = ""

        self._crear_variables()
        self._construir()
        self._escribir_ajustes(self.config)
        self._cargando = False
        self._enlazar_cambios()
        self._actualizar_pistas()
        self._recontar()
        self._refrescar_clave()
        self._refrescar_botones()
        root.protocol("WM_DELETE_WINDOW", self.cerrar)
        if arrancar:
            self._luego(400, self._arrancar)

    # ------------------------------------------------------------------
    # Temporizadores
    # ------------------------------------------------------------------

    def _luego(self, ms: int, fn: Callable[[], Any]) -> str:
        """`after` que se anula solo al cerrar: un temporizador vivo sobre una ventana destruida da errores."""
        def ejecutar() -> None:
            self._temporizadores.discard(ident)
            fn()

        ident = self.root.after(ms, ejecutar)
        self._temporizadores.add(ident)
        return ident

    def _anular(self, ident: Optional[str]) -> None:
        if ident is None:
            return
        self._temporizadores.discard(ident)
        try:
            self.root.after_cancel(ident)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Construcción
    # ------------------------------------------------------------------

    def _crear_variables(self) -> None:
        S, B = tk.StringVar, tk.BooleanVar
        self.v: dict[str, Any] = {
            "modelo": S(), "esfuerzo": S(), "modo": S(value="inmediato"), "separador": S(), "plantilla": S(),
            "max_tokens": S(), "concurrencia": S(), "max_error_attempts": S(),
            "pausa_entre_mensajes_sec": S(), "timeout_api_sec": S(),
            "tope_gasto_usd": S(), "precio_input_mtok": S(), "precio_output_mtok": S(),
            "hora_limite": S(), "pausa_ciclos_sec": S(), "telegram_chat_id": S(), "retencion_dias": S(), "tema": S(),
            "usar_cache": B(), "reset_gasto_por_lote": B(), "estimar_antes_de_enviar": B(),
            "modo_continuo": B(), "auto_arranque": B(), "iniciar_minimizado": B(),
        }
        self.var_cuenta = tk.StringVar(value="0 órdenes detectadas")
        self.var_archivo = tk.StringVar()
        self.var_limite = tk.StringVar(value="0")
        self.var_estado = tk.StringVar(value="Estado: en espera")
        self.var_progreso = tk.StringVar(value="Progreso 0/0")
        self.var_proximo = tk.StringVar(value="Próximo intento --:--")
        self.var_gasto = tk.StringVar()
        self.var_exitos = tk.StringVar(value="Éxitos totales 0")
        self.var_error = tk.StringVar()
        self.var_auth = tk.StringVar()
        self.var_clave = tk.StringVar()
        self.var_token_tg = tk.StringVar()
        self.var_estado_tg = tk.StringVar()
        self.var_modelo_info = tk.StringVar()
        self.var_precio_info = tk.StringVar()
        self.var_aviso_ajustes = tk.StringVar()

    def _construir(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        cabecera = ttk.Frame(self.root, padding=(18, 12, 18, 0))
        cabecera.grid(row=0, column=0, sticky="ew")
        cabecera.columnconfigure(1, weight=1)
        ttk.Label(cabecera, text="Claude Batch Monitor", style="Titulo.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(cabecera, text=f"v{APP_VERSION} · modo avanzado", style="Sub.TLabel").grid(
            row=0, column=1, sticky="w", padx=(12, 0), pady=(7, 0))
        self.boton_simple = ttk.Button(cabecera, text="Modo simple", command=self.cambiar_a_simple)
        self.boton_simple.grid(row=0, column=2, sticky="e")

        self.cuaderno = ttk.Notebook(self.root)
        self.cuaderno.grid(row=1, column=0, sticky="nsew", padx=14, pady=(8, 14))
        self.tab_panel = ttk.Frame(self.cuaderno, padding=10)
        self.tab_ajustes = ttk.Frame(self.cuaderno)
        self.cuaderno.add(self.tab_panel, text="Panel")
        self.cuaderno.add(self.tab_ajustes, text="Ajustes")
        self._bloqueables: list[Any] = []
        self._construir_panel(self.tab_panel)
        self._construir_ajustes(self.tab_ajustes)

    def _texto(self, master: tk.Misc, alto: int, **kw: Any) -> tuple[ttk.Frame, tk.Text]:
        """Un `Text` con barra de desplazamiento y el aspecto de las tarjetas."""
        p = self.p
        marco = ttk.Frame(master)
        marco.columnconfigure(0, weight=1)
        marco.rowconfigure(0, weight=1)
        opciones: dict[str, Any] = dict(
            height=alto, wrap="word", relief="flat", borderwidth=0, padx=8, pady=6,
            background=p.superficie, foreground=p.texto, highlightthickness=1,
            highlightbackground=p.borde, highlightcolor=p.acento, font=self.f.texto,
            insertbackground=p.texto, undo=True,
        )
        opciones.update(kw)
        texto = tk.Text(marco, **opciones)
        barra = ttk.Scrollbar(marco, orient="vertical", command=texto.yview)
        texto.configure(yscrollcommand=barra.set)
        texto.grid(row=0, column=0, sticky="nsew")
        barra.grid(row=0, column=1, sticky="ns")
        return marco, texto

    # -- Panel ----------------------------------------------------------

    def _construir_panel(self, tab: ttk.Frame) -> None:
        p, f = self.p, self.f
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=4, minsize=230)  # órdenes: la caja de texto siempre cabe
        tab.rowconfigure(2, weight=2, minsize=120)  # resultados y actividad

        self.aviso_clave = tk.Frame(tab, background=p.banner_aviso_fondo, highlightthickness=1,
                                    highlightbackground=p.banner_aviso_borde)
        tk.Label(self.aviso_clave, text="Falta la clave de API de Anthropic.", background=p.banner_aviso_fondo,
                 foreground=p.aviso, font=f.negrita).pack(side="left", padx=12, pady=8)
        ttk.Button(self.aviso_clave, text="Ir a Ajustes", command=self._ir_a_ajustes).pack(side="right", padx=8, pady=4)
        self.aviso_clave.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        # --- Órdenes
        f_ord = ttk.LabelFrame(tab, text=" Órdenes (una línea con solo --- separa una de otra) ", padding=8)
        f_ord.grid(row=1, column=0, sticky="nsew")
        f_ord.columnconfigure(0, weight=1)
        f_ord.rowconfigure(1, weight=1)
        cab = ttk.Frame(f_ord)
        cab.grid(row=0, column=0, sticky="ew")
        cab.columnconfigure(1, weight=1)
        ttk.Label(cab, textvariable=self.var_cuenta, style="Seccion.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(cab, textvariable=self.var_archivo, style="Sub.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.boton_cargar = ttk.Button(cab, text="Cargar archivo…", command=self.cargar_archivo)
        self.boton_cargar.grid(row=0, column=2, sticky="e")
        marco, self.txt_ordenes = self._texto(f_ord, 6, font=f.mono)
        marco.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        fila_pl = ttk.Frame(f_ord)
        fila_pl.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        fila_pl.columnconfigure(1, weight=1)
        ttk.Label(fila_pl, text="Plantilla", style="Sub.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.entrada_plantilla = ttk.Entry(fila_pl, textvariable=self.v["plantilla"])
        self.entrada_plantilla.grid(row=0, column=1, sticky="ew")
        ttk.Label(fila_pl, style="Pequeno.TLabel",
                  text="Opcional. Envuelve cada orden: «Traduce al inglés: {input}». Sin {input}, se añade al final."
                  ).grid(row=1, column=1, sticky="w", pady=(2, 0))
        fila_modo = ttk.Frame(f_ord)
        fila_modo.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.radio_inmediato = ttk.Radiobutton(fila_modo, text="Inmediato", value="inmediato", variable=self.v["modo"])
        self.radio_lote = ttk.Radiobutton(fila_modo, text="Lote (-50 %, hasta 24 h)", value="lote",
                                          variable=self.v["modo"])
        self.radio_inmediato.pack(side="left")
        self.radio_lote.pack(side="left", padx=(14, 0))
        self.boton_estimar = ttk.Button(fila_modo, text="Estimar coste", command=self.estimar_coste)
        self.boton_estimar.pack(side="right")
        self.spin_limite = ttk.Spinbox(fila_modo, from_=0, to=1_000_000, width=6, textvariable=self.var_limite)
        ttk.Label(fila_modo, text="(0 = todas)", style="Sub.TLabel").pack(side="right", padx=(6, 14))
        self.spin_limite.pack(side="right")
        ttk.Label(fila_modo, text="Solo las primeras", style="Sub.TLabel").pack(side="right", padx=(0, 6))

        # --- Resultados y actividad, en pestañas
        f_baja = ttk.Frame(tab)
        f_baja.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        f_baja.columnconfigure(0, weight=1)
        f_baja.rowconfigure(0, weight=1)
        self.cuaderno_bajo = ttk.Notebook(f_baja, style="Interior.TNotebook")
        self.cuaderno_bajo.grid(row=0, column=0, sticky="nsew")
        tab_res = ttk.Frame(self.cuaderno_bajo, padding=8)
        tab_act = ttk.Frame(self.cuaderno_bajo, padding=8)
        self.cuaderno_bajo.add(tab_res, text="Resultados")
        self.cuaderno_bajo.add(tab_act, text="Actividad")
        tab_res.columnconfigure(0, weight=1)
        tab_res.rowconfigure(0, weight=1)
        columnas = ("id", "orden", "estado", "tokens", "coste")
        self.tabla = ttk.Treeview(tab_res, columns=columnas, show="headings", selectmode="browse", height=5)
        for col, titulo, ancho, ancla, estirar in (
            ("id", "#", 54, "e", False), ("orden", "Orden", 360, "w", True), ("estado", "Estado", 110, "w", False),
            ("tokens", "Tokens", 90, "e", False), ("coste", "Coste", 90, "e", False),
        ):
            self.tabla.heading(col, text=titulo)
            self.tabla.column(col, width=ancho, anchor=ancla, stretch=estirar, minwidth=40)
        barra = ttk.Scrollbar(tab_res, orient="vertical", command=self.tabla.yview)
        self.tabla.configure(yscrollcommand=barra.set)
        self.tabla.grid(row=0, column=0, sticky="nsew")
        barra.grid(row=0, column=1, sticky="ns")
        ttk.Label(tab_res, style="Pequeno.TLabel", text="Doble clic en una fila para ver la respuesta completa.").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.tabla.tag_configure("lista", foreground=p.ok)
        self.tabla.tag_configure("error", foreground=p.error)
        self.tabla.tag_configure("rechazada", foreground=p.aviso)
        self.tabla.tag_configure("envio", foreground=p.acento_texto)
        self.tabla.tag_configure("cola", foreground=p.apagado)
        self.tabla.bind("<Double-1>", lambda e: self.ver_respuesta())
        self.tabla.bind("<Return>", lambda e: self.ver_respuesta())
        tab_act.columnconfigure(0, weight=1)
        tab_act.rowconfigure(0, weight=1)
        marco_log, self.txt_log = self._texto(
            tab_act, 4, state="disabled", background="#1e1e1e", foreground="#d4d4d4", font=f.mono,
            insertbackground="#d4d4d4", highlightbackground="#1e1e1e", undo=False)
        marco_log.grid(row=0, column=0, sticky="nsew")

        # --- Estado
        monitor = ttk.LabelFrame(tab, text=" Estado ", padding=(12, 8))
        monitor.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        for c in range(4):
            monitor.columnconfigure(c, weight=1, uniform="estado")
        ttk.Label(monitor, textvariable=self.var_estado, style="Seccion.TLabel", wraplength=800).grid(
            row=0, column=0, columnspan=4, sticky="w")
        self.progreso = ttk.Progressbar(monitor, mode="determinate", maximum=1, value=0)
        self.progreso.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 6))
        ttk.Label(monitor, textvariable=self.var_progreso).grid(row=2, column=0, sticky="w")
        tk.Label(monitor, textvariable=self.var_gasto, background=p.fondo, foreground=p.ok, font=f.base).grid(
            row=2, column=1, sticky="w")
        ttk.Label(monitor, textvariable=self.var_proximo).grid(row=2, column=2, sticky="w")
        ttk.Label(monitor, textvariable=self.var_exitos).grid(row=2, column=3, sticky="w")
        self.etiqueta_error = tk.Label(monitor, textvariable=self.var_error, background=p.fondo, foreground=p.error,
                                       font=f.base, anchor="w", justify="left", wraplength=800)
        self.etiqueta_error.grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.etiqueta_error.grid_remove()  # solo se ve cuando hay un error que contar

        # --- Botones
        botones = ttk.Frame(tab)
        botones.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.boton_iniciar = ttk.Button(botones, text="Iniciar lote", style="Primary.TButton", command=self.iniciar)
        self.boton_iniciar.pack(side="left")
        self.boton_reanudar = ttk.Button(botones, text="Reanudar", command=self.reanudar)
        self.boton_reanudar.pack(side="left", padx=(8, 0))
        self.boton_detener = ttk.Button(botones, text="Detener", command=self.detener)
        self.boton_detener.pack(side="left", padx=(8, 0))
        self.menu_mas = tk.Menu(self.root, tearoff=0)
        self.menu_mas.add_command(label="Carpeta de respuestas", command=self.abrir_respuestas)
        self.menu_mas.add_command(label="Registro de actividad (log)", command=self.abrir_logs)
        self.menu_mas.add_command(label="Carpeta de datos", command=self.abrir_datos)
        self.boton_mas = ttk.Menubutton(botones, text="Más", menu=self.menu_mas)
        self.boton_mas.pack(side="right")
        self.boton_lotes = ttk.Button(botones, text="Lotes anteriores…", command=self.lotes_anteriores)
        self.boton_lotes.pack(side="right", padx=(0, 8))
        self.boton_exportar = ttk.Button(botones, text="Exportar…", command=self.exportar_resultados)
        self.boton_exportar.pack(side="right", padx=(0, 8))

        self._bloqueables += [self.txt_ordenes, self.entrada_plantilla, self.boton_cargar, self.radio_inmediato,
                              self.radio_lote, self.spin_limite]

    # -- Ajustes --------------------------------------------------------

    def _seccion(self, interior: ttk.Frame, titulo: str) -> ttk.LabelFrame:
        marco = ttk.LabelFrame(interior, text=f" {titulo} ", padding=(14, 10))
        marco.pack(fill="x", padx=10, pady=(8, 2))
        marco.columnconfigure(0, minsize=200)
        marco.columnconfigure(1, weight=1)
        return marco

    def _fila(self, marco: ttk.LabelFrame, fila: int, etiqueta: str, widget: tk.Misc, ayuda: str = "",
              estirar: bool = False) -> None:
        ttk.Label(marco, text=etiqueta).grid(row=fila, column=0, sticky="nw", padx=(0, 14), pady=5)
        widget.grid(row=fila, column=1, sticky="ew" if estirar else "w", pady=5)
        if ayuda:
            ttk.Label(marco, text=ayuda, style="Pequeno.TLabel", wraplength=560, justify="left").grid(
                row=fila + 1, column=1, sticky="w", pady=(0, 4))

    def _construir_ajustes(self, tab: ttk.Frame) -> None:
        p, f, v = self.p, self.f, self.v
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        desplazable = Desplazable(tab, p)
        desplazable.grid(row=0, column=0, sticky="nsew")
        inter = desplazable.interior
        tk.Label(inter, textvariable=self.var_aviso_ajustes, background=p.fondo, foreground=p.error, font=f.negrita,
                 anchor="w", justify="left", wraplength=760).pack(fill="x", padx=14, pady=(8, 0))

        # Autenticación y modelo
        a = self._seccion(inter, "Autenticación y modelo")
        ttk.Label(a, textvariable=self.var_auth, wraplength=600, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        fila_clave = ttk.Frame(a)
        fila_clave.columnconfigure(0, weight=1)
        self.entrada_clave = ttk.Entry(fila_clave, textvariable=self.var_clave, show="*", width=48)
        self.entrada_clave.grid(row=0, column=0, sticky="ew")
        self.boton_clave = ttk.Button(fila_clave, text="Probar y guardar", command=self.probar_y_guardar_clave)
        self.boton_clave.grid(row=0, column=1, padx=(8, 0))
        self._fila(a, 1, "Clave de API", fila_clave, estirar=True)
        self.entrada_clave.bind("<Return>", lambda e: self.probar_y_guardar_clave())
        fila_modelo = ttk.Frame(a)
        fila_modelo.columnconfigure(0, weight=1)
        self.combo_modelo = ttk.Combobox(fila_modelo, textvariable=v["modelo"], values=list(MODELOS_SUGERIDOS),
                                         state="readonly", width=40)
        self.combo_modelo.grid(row=0, column=0, sticky="ew")
        self.boton_modelos = ttk.Button(fila_modelo, text="Cargar modelos", command=self.cargar_modelos)
        self.boton_modelos.grid(row=0, column=1, padx=(8, 0))
        self._fila(a, 2, "Modelo", fila_modelo, estirar=True)
        ttk.Label(a, textvariable=self.var_modelo_info, style="Pequeno.TLabel", wraplength=560).grid(
            row=3, column=1, sticky="w", pady=(0, 4))

        # Respuesta
        r = self._seccion(inter, "Respuesta")
        self.combo_esfuerzo = ttk.Combobox(r, textvariable=v["esfuerzo"], values=[SIN_ESFUERZO, *NIVELES_ESFUERZO],
                                           state="readonly", width=14)
        self._fila(r, 0, "Esfuerzo de razonamiento", self.combo_esfuerzo,
                   "Más esfuerzo = mejores respuestas en tareas difíciles, pero más tokens. Solo algunos modelos "
                   "lo admiten; si no, se ignora.")
        self.spin_max_tokens = ttk.Spinbox(r, from_=1, to=128000, increment=1000, width=9, textvariable=v["max_tokens"])
        self._fila(r, 2, "Máximo de tokens por respuesta", self.spin_max_tokens,
                   "Tope de la longitud de cada respuesta. También fija el peor caso de coste.")
        marco_sis, self.txt_sistema = self._texto(r, 4)
        self._fila(r, 4, "Prompt de sistema", marco_sis, "Instrucciones comunes a todas las órdenes (opcional).",
                   estirar=True)
        self.check_cache = ttk.Checkbutton(r, text="Usar caché de prompt (ahorra si el prompt de sistema es largo)",
                                           variable=v["usar_cache"])
        self.check_cache.grid(row=6, column=1, sticky="w", pady=(2, 0))

        # Ejecución
        e = self._seccion(inter, "Ejecución")
        self.spin_concurrencia = ttk.Spinbox(e, from_=1, to=32, width=6, textvariable=v["concurrencia"])
        self._fila(e, 0, "Peticiones en paralelo", self.spin_concurrencia,
                   "Solo en modo inmediato. Más de 1 acelera, pero consume el límite por minuto más deprisa.")
        self.spin_errores = ttk.Spinbox(e, from_=1, to=100, width=6, textvariable=v["max_error_attempts"])
        self._fila(e, 2, "Reintentos por orden", self.spin_errores, "Errores de servidor o red antes de dar la orden por fallida.")
        self.spin_pausa_msgs = ttk.Spinbox(e, from_=0, to=60, width=6, textvariable=v["pausa_entre_mensajes_sec"])
        self._fila(e, 4, "Pausa entre órdenes (s)", self.spin_pausa_msgs)
        self.spin_timeout = ttk.Spinbox(e, from_=15, to=900, width=6, textvariable=v["timeout_api_sec"])
        self._fila(e, 5, "Tiempo máximo por petición (s)", self.spin_timeout)

        # Gasto
        g = self._seccion(inter, "Blindaje de gasto")
        self.entrada_tope = ttk.Entry(g, textvariable=v["tope_gasto_usd"], width=10)
        self._fila(g, 0, "Tope de gasto (USD)", self.entrada_tope,
                   "El lote se detiene antes de pasarlo. 0 = sin tope (no recomendado).")
        self.check_reset = ttk.Checkbutton(g, text="Empezar el contador de gasto en cada lote nuevo",
                                           variable=v["reset_gasto_por_lote"])
        self.check_reset.grid(row=2, column=1, sticky="w")
        self.check_estimar = ttk.Checkbutton(g, text="Calcular el peor caso antes de cada petición y no enviarla si no cabe",
                                             variable=v["estimar_antes_de_enviar"])
        self.check_estimar.grid(row=3, column=1, sticky="w", pady=(2, 6))
        fila_precios = ttk.Frame(g)
        self.entrada_precio_in = ttk.Entry(fila_precios, textvariable=v["precio_input_mtok"], width=8)
        self.entrada_precio_out = ttk.Entry(fila_precios, textvariable=v["precio_output_mtok"], width=8)
        ttk.Label(fila_precios, text="entrada").pack(side="left", padx=(0, 4))
        self.entrada_precio_in.pack(side="left")
        ttk.Label(fila_precios, text="salida").pack(side="left", padx=(14, 4))
        self.entrada_precio_out.pack(side="left")
        self._fila(g, 4, "Precios USD / millón", fila_precios)
        tk.Label(g, textvariable=self.var_precio_info, background=p.fondo, foreground=p.apagado, font=f.pequena,
                 wraplength=560, justify="left", anchor="w").grid(row=5, column=1, sticky="w")

        # Tiempos
        t_ = self._seccion(inter, "Tiempos y bucle")
        self.entrada_hora = ttk.Entry(t_, textvariable=v["hora_limite"], width=8)
        self._fila(t_, 0, "Hora límite (HH:MM)", self.entrada_hora,
                   "Vacío = sin hora límite. En modo bucle, si lo dejas vacío se aplica 23:59.")
        self.entrada_separador = ttk.Entry(t_, textvariable=v["separador"], width=10)
        self._fila(t_, 2, "Separador de órdenes", self.entrada_separador)
        self.check_bucle = ttk.Checkbutton(
            t_, text="Repetir el lote en bucle hasta la hora límite o el tope (¡gasta!)", variable=v["modo_continuo"])
        self.check_bucle.grid(row=3, column=1, sticky="w", pady=(4, 0))
        self.spin_pausa_ciclos = ttk.Spinbox(t_, from_=1, to=3600, width=6, textvariable=v["pausa_ciclos_sec"])
        self._fila(t_, 4, "Pausa entre repeticiones (s)", self.spin_pausa_ciclos)

        # Telegram
        tg = self._seccion(inter, "Telegram (opcional)")
        self.entrada_token_tg = ttk.Entry(tg, textvariable=self.var_token_tg, show="*", width=44)
        self._fila(tg, 0, "Token del bot", self.entrada_token_tg, estirar=True)
        self.entrada_chat = ttk.Entry(tg, textvariable=v["telegram_chat_id"], width=24)
        self._fila(tg, 1, "Chat ID", self.entrada_chat)
        fila_tg = ttk.Frame(tg)
        self.boton_tg = ttk.Button(fila_tg, text="Probar Telegram", command=self.probar_telegram)
        self.boton_tg.pack(side="left")
        ttk.Label(fila_tg, textvariable=self.var_estado_tg, style="Sub.TLabel").pack(side="left", padx=(12, 0))
        self._fila(tg, 2, "", fila_tg)

        # Automatización
        au = self._seccion(inter, "Automatización y mantenimiento")
        ttk.Checkbutton(au, text="Al abrir el programa, iniciar o reanudar el lote sin preguntar",
                        variable=v["auto_arranque"]).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(au, text="Al abrir, empezar minimizado en la bandeja del sistema",
                        variable=v["iniciar_minimizado"]).grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.spin_retencion = ttk.Spinbox(au, from_=0, to=3650, width=6, textvariable=v["retencion_dias"])
        self._fila(au, 2, "Conservar lotes (días)", self.spin_retencion,
                   "Los lotes más viejos se borran al abrir el programa. 0 = no borrar nunca. "
                   "Los que tienen trabajo pendiente nunca se borran.")
        self.combo_tema = ttk.Combobox(au, textvariable=v["tema"], values=list(TEMAS_VISIBLES), state="readonly", width=14)
        self._fila(au, 3, "Tema (al reiniciar)", self.combo_tema)
        fila_datos = ttk.Frame(au)
        ttk.Button(fila_datos, text="Abrir carpeta de datos", command=self.abrir_datos).pack(side="left")
        self._fila(au, 4, "", fila_datos)
        ttk.Frame(inter, height=10).pack()

        self._bloqueables += [
            self.combo_modelo, self.boton_modelos, self.combo_esfuerzo, self.spin_max_tokens, self.txt_sistema,
            self.check_cache, self.spin_concurrencia, self.spin_errores, self.spin_pausa_msgs, self.spin_timeout,
            self.entrada_tope, self.check_reset, self.check_estimar, self.entrada_precio_in, self.entrada_precio_out,
            self.entrada_hora, self.entrada_separador, self.check_bucle, self.spin_pausa_ciclos,
        ]

    # ------------------------------------------------------------------
    # Ajustes <-> configuración
    # ------------------------------------------------------------------

    _ENTEROS = {
        "max_tokens": ("Máximo de tokens", 1, 1_000_000),
        "concurrencia": ("Peticiones en paralelo", 1, 32),
        "max_error_attempts": ("Reintentos por orden", 1, 100),
        "pausa_entre_mensajes_sec": ("Pausa entre órdenes", 0, 60),
        "timeout_api_sec": ("Tiempo máximo por petición", 15, 900),
        "pausa_ciclos_sec": ("Pausa entre repeticiones", 1, 3600),
        "retencion_dias": ("Conservar lotes", 0, 3650),
    }
    _BOOLEANOS = ("usar_cache", "reset_gasto_por_lote", "estimar_antes_de_enviar", "modo_continuo",
                  "auto_arranque", "iniciar_minimizado")

    def _escribir_ajustes(self, c: Configuracion) -> None:
        v = self.v
        v["modelo"].set(c.modelo)
        v["esfuerzo"].set(c.esfuerzo or SIN_ESFUERZO)
        v["modo"].set(c.modo)
        v["separador"].set(c.separador)
        for campo in self._ENTEROS:
            v[campo].set(str(getattr(c, campo)))
        for campo in self._BOOLEANOS:
            v[campo].set(bool(getattr(c, campo)))
        v["tope_gasto_usd"].set(_texto_decimal(c.tope_gasto_usd))
        v["precio_input_mtok"].set(_texto_decimal(c.precio_input_mtok))
        v["precio_output_mtok"].set(_texto_decimal(c.precio_output_mtok))
        v["hora_limite"].set(c.hora_limite)
        v["telegram_chat_id"].set(c.telegram_chat_id)
        v["tema"].set(next((nombre for nombre, valor in TEMAS_VISIBLES.items() if valor == c.tema), "Automático"))
        v["plantilla"].set(c.plantilla)
        for caja, valor in ((self.txt_ordenes, c.mensaje), (self.txt_sistema, c.system_prompt)):
            caja.delete("1.0", "end")
            caja.insert("1.0", valor)
            caja.edit_reset()

    def leer_ajustes(self) -> Configuracion:
        """Lo que dicen ahora mismo las casillas, ya validado. Lanza `ErrorDeAjustes` si algo no sirve."""
        v = self.v
        esfuerzo = v["esfuerzo"].get()
        enteros = {campo: _entero(v[campo].get(), nombre, mn, mx) for campo, (nombre, mn, mx) in self._ENTEROS.items()}
        return replace(
            self.config,
            mensaje=self.txt_ordenes.get("1.0", "end-1c"),
            plantilla=v["plantilla"].get().strip(),
            system_prompt=self.txt_sistema.get("1.0", "end-1c").strip(),
            modelo=v["modelo"].get().strip() or DEFAULT_MODEL,
            esfuerzo="" if esfuerzo == SIN_ESFUERZO else esfuerzo,
            modo=v["modo"].get(),
            separador=v["separador"].get().strip() or DEFAULT_SEPARATOR,
            tope_gasto_usd=_decimal_de_casilla(v["tope_gasto_usd"].get(), "Tope de gasto") or Decimal(0),
            precio_input_mtok=_decimal_de_casilla(v["precio_input_mtok"].get(), "Precio de entrada", opcional=True),
            precio_output_mtok=_decimal_de_casilla(v["precio_output_mtok"].get(), "Precio de salida", opcional=True),
            hora_limite=_hora(v["hora_limite"].get()),
            telegram_chat_id=v["telegram_chat_id"].get().strip(),
            tema=TEMAS_VISIBLES.get(v["tema"].get(), self.config.tema),
            **enteros,
            **{campo: bool(v[campo].get()) for campo in self._BOOLEANOS},
        )

    def guardar_ajustes(self) -> Optional[Configuracion]:
        """Guarda las casillas en config.json. Si alguna no es válida, lo dice y no guarda nada."""
        self._anular(self._guardado)
        self._guardado = None
        try:
            c = self.leer_ajustes()
        except ErrorDeAjustes as error:
            self.var_aviso_ajustes.set(f"Revisa los ajustes — {error}")
            return None
        self.var_aviso_ajustes.set("")
        self.config = c
        ConfigManager.guardar(replace(c, mensaje=c.mensaje if len(c.mensaje) <= MAX_BORRADOR else ""))
        return c

    def _enlazar_cambios(self) -> None:
        for variable in self.v.values():
            variable.trace_add("write", self._al_cambiar)
        for caja in (self.txt_ordenes, self.txt_sistema):
            caja.bind("<KeyRelease>", self._al_cambiar, add="+")
            caja.bind("<<Paste>>", lambda e: self._luego(10, self._al_cambiar), add="+")
            caja.bind("<<Cut>>", lambda e: self._luego(10, self._al_cambiar), add="+")

    def _al_cambiar(self, *_: Any) -> None:
        if self._cargando:
            return
        self._recontar()
        self._actualizar_pistas()
        # Con un temporizador la escritura a disco no ocurre en cada tecla, sino al dejar de teclear.
        self._anular(self._guardado)
        self._guardado = self._luego(600, self.guardar_ajustes)

    def _actualizar_pistas(self) -> None:
        modelo = self.v["modelo"].get().strip() or DEFAULT_MODEL
        self.var_modelo_info.set(descripcion_modelo(modelo))
        precios = resolver_precios(modelo)
        self.var_precio_info.set(
            "Déjalos vacíos para usar los del catálogo "
            f"({formatear_precio(precios.entrada)} entrada / {formatear_precio(precios.salida)} salida) "
            "o escribe los tuyos si Anthropic los ha cambiado."
            + (" Este modelo no está en el catálogo." if precios.origen != "catalogo" else "")
        )
        lote = self.v["modo"].get() == "lote"
        # En modo lote, la API ya procesa en paralelo y el bucle no existe: se apagan esas casillas.
        for w in (self.spin_concurrencia, self.check_bucle, self.spin_pausa_ciclos, self.spin_pausa_msgs):
            if not self._ocupado:
                self._habilitar(w, not lote)

    def _recontar(self) -> None:
        texto = self.txt_ordenes.get("1.0", "end-1c")
        n = len(split_messages(texto, self.v["separador"].get().strip() or DEFAULT_SEPARATOR))
        self.var_cuenta.set(f"{_miles(n)} {'orden detectada' if n == 1 else 'órdenes detectadas'}")

    @staticmethod
    def _habilitar(w: Any, si: bool) -> None:
        try:
            if isinstance(w, ttk.Widget):
                w.state(["!disabled" if si else "disabled"])
            else:
                w.configure(state="normal" if si else "disabled")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Diálogos del sistema (aislados para poder probarlos sin ventanas emergentes)
    # ------------------------------------------------------------------

    def _confirmar(self, titulo: str, texto: str) -> bool:
        return messagebox.askyesno(titulo, texto, parent=self.root)

    def _info(self, titulo: str, texto: str) -> None:
        messagebox.showinfo(titulo, texto, parent=self.root)

    def _aviso(self, titulo: str, texto: str) -> None:
        messagebox.showwarning(titulo, texto, parent=self.root)

    def _error(self, titulo: str, texto: str) -> None:
        messagebox.showerror(titulo, texto, parent=self.root)

    def _pedir_archivo(self) -> str:
        return filedialog.askopenfilename(parent=self.root, filetypes=AYUDA_FORMATOS) or ""

    def _pedir_destino(self) -> str:
        return filedialog.asksaveasfilename(
            parent=self.root, defaultextension=".csv",
            filetypes=[(f"{f.upper()} (.{f})", f"*.{f}") for f in FORMATOS],
        ) or ""

    def _visible(self) -> bool:
        try:
            return self.root.state() not in ("iconic", "withdrawn")
        except tk.TclError:
            return False

    def _ir_a_ajustes(self) -> None:
        self.cuaderno.select(self.tab_ajustes)

    # ------------------------------------------------------------------
    # Clave de API, modelos y Telegram
    # ------------------------------------------------------------------

    def _refrescar_clave(self) -> None:
        origen = origen_api_key()
        self.var_auth.set({
            "entorno": "La clave viene de la variable de entorno ANTHROPIC_API_KEY (cámbiala allí).",
            "llavero": "La clave está guardada en el llavero de tu sistema. Escribe otra para cambiarla.",
        }.get(origen, "Todavía no hay clave. Pégala aquí abajo y pulsa «Probar y guardar»."))
        self._habilitar(self.entrada_clave, origen != "entorno")
        self._habilitar(self.boton_clave, origen != "entorno")
        if origen:
            self.aviso_clave.grid_remove()
        else:
            self.aviso_clave.grid()
        if SecureStorage.get(KEY_TELEGRAM_TOKEN) or os.environ.get("TELEGRAM_BOT_TOKEN"):
            self.var_estado_tg.set("Token guardado.")
        else:
            self.var_estado_tg.set("")

    def probar_y_guardar_clave(self) -> None:
        clave = self.var_clave.get().strip()
        if not clave:
            self.var_auth.set("Pega primero tu clave de API.")
            return
        self._habilitar(self.boton_clave, False)
        self.var_auth.set("Probando la clave…")

        def trabajo() -> None:
            try:
                modelos = self.probar(clave)
            except ErrorDeClave as e:
                self.bridge.post(self._clave_fallo, e)
            except Exception as e:  # pragma: no cover - defensa
                self.bridge.post(self._clave_fallo, ErrorDeClave("otro", str(e)))
            else:
                self.bridge.post(self._clave_valida, clave, modelos)

        threading.Thread(target=trabajo, daemon=True, name="probar-clave").start()

    def _clave_fallo(self, e: ErrorDeClave) -> None:
        self._habilitar(self.boton_clave, True)
        self.var_auth.set({
            "invalida": "Anthropic no reconoce esa clave. Comprueba que la copiaste entera.",
            "sin_conexion": "No hay conexión con Anthropic. Comprueba tu internet.",
            "vacia": "Pega primero tu clave de API.",
        }.get(e.tipo) or f"No se pudo comprobar la clave: {e.detalle}")

    def _clave_valida(self, clave: str, modelos: list[str]) -> None:
        donde = self.guardar(clave)
        self.var_clave.set("")  # la clave no se queda en un campo de texto
        self._habilitar(self.boton_clave, True)
        self._refrescar_clave()
        extra = ("" if donde == "llavero" else
                 " Ojo: no pude guardarla de forma segura, así que tendrás que pegarla cada vez que abras el programa.")
        self.var_auth.set(f"Clave válida.{extra}")
        self._ofrecer_modelos(modelos)

    def _ofrecer_modelos(self, modelos: list[str]) -> None:
        if not modelos:
            return
        actual = self.v["modelo"].get()
        self.combo_modelo.configure(values=sorted(set(modelos) | {actual}, reverse=True))
        elegido = elegir_modelo_por_defecto(modelos, actual)
        if elegido != actual:
            self.v["modelo"].set(elegido)

    def cargar_modelos(self) -> None:
        clave = obtener_api_key() or self.var_clave.get().strip()
        if not clave:
            self._aviso("Falta la clave", "Necesitas una clave de API para consultar los modelos disponibles.")
            return
        self._habilitar(self.boton_modelos, False)

        def trabajo() -> None:
            try:
                modelos = self.listar(clave)
            except ErrorDeClave as e:
                self.bridge.post(self._modelos_fallo, e)
            except Exception as e:  # pragma: no cover - defensa
                self.bridge.post(self._modelos_fallo, ErrorDeClave("otro", str(e)))
            else:
                self.bridge.post(self._modelos_listos, modelos)

        threading.Thread(target=trabajo, daemon=True, name="cargar-modelos").start()

    def _modelos_listos(self, modelos: list[str]) -> None:
        self._habilitar(self.boton_modelos, True)
        if not modelos:
            self._aviso("Modelos", "La API no devolvió ningún modelo.")
            return
        actual = self.v["modelo"].get()
        self.combo_modelo.configure(values=sorted(set(modelos) | {actual}, reverse=True))
        if actual not in modelos:
            self.v["modelo"].set(elegir_modelo_por_defecto(modelos, actual))
        self._info("Modelos", f"Se cargaron {len(modelos)} modelos.")

    def _modelos_fallo(self, e: ErrorDeClave) -> None:
        self._habilitar(self.boton_modelos, True)
        self._error("No se pudieron cargar los modelos", {
            "invalida": "Anthropic no reconoce la clave.",
            "sin_conexion": "No hay conexión con Anthropic.",
        }.get(e.tipo) or e.detalle or "Error desconocido.")

    def _guardar_token_telegram(self) -> None:
        token = self.var_token_tg.get().strip()
        if not token:
            return
        if not SecureStorage.set(KEY_TELEGRAM_TOKEN, token):
            os.environ["TELEGRAM_BOT_TOKEN"] = token  # sin llavero: solo vale en esta sesión
        self.var_token_tg.set("")
        self._refrescar_clave()

    def probar_telegram(self) -> None:
        token = self.var_token_tg.get().strip() or credenciales_telegram()[0]
        chat = self.v["telegram_chat_id"].get().strip()
        if not token or not chat:
            self._aviso("Datos incompletos", "Escribe el token del bot y el Chat ID.")
            return
        self._habilitar(self.boton_tg, False)
        self.var_estado_tg.set("Enviando mensaje de prueba…")

        def trabajo() -> None:
            try:
                enviar_prueba_telegram(token, chat)
            except ErrorDeTelegram as e:
                self.bridge.post(self._telegram_listo, str(e))
            else:
                self.bridge.post(self._telegram_listo, "")

        threading.Thread(target=trabajo, daemon=True, name="probar-telegram").start()

    def _telegram_listo(self, error: str) -> None:
        self._habilitar(self.boton_tg, True)
        if error:
            self.var_estado_tg.set("")
            self._error("Telegram", error)
            return
        self._guardar_token_telegram()
        self.var_estado_tg.set("Mensaje de prueba enviado. Token guardado.")
        self._info("Telegram", "Mensaje de prueba enviado.")

    # ------------------------------------------------------------------
    # Órdenes
    # ------------------------------------------------------------------

    def cargar_archivo(self, ruta: Optional[str] = None, campo: Optional[str] = None) -> bool:
        ruta = ruta or self._pedir_archivo()
        if not ruta:
            return False
        path = Path(ruta)
        sep = self.v["separador"].get().strip() or DEFAULT_SEPARATOR
        try:
            campos = listar_campos(path)
            if campos is None:  # texto plano: se muestra tal cual, para poder editarlo
                contenido = path.read_text(encoding="utf-8-sig")
                etiqueta = path.name
            else:
                if not campos:
                    raise ErrorDeEntrada(f"{path.name} no tiene columnas ni campos que leer.")
                if campo is None:
                    campo = self._elegir_columna(path.name, campos)
                    if campo is None:
                        return False
                ordenes = leer_ordenes(path, campo=campo, separador=sep)
                # Si una orden contiene una línea igual al separador, al juntarlas se partiría en dos.
                partidas = sum(1 for o in ordenes if len(split_messages(o, sep)) != 1)
                if partidas:
                    raise ErrorDeEntrada(
                        f"{partidas} orden(es) de {path.name} contienen una línea que solo dice «{sep}». "
                        "Cambia el separador en Ajustes por algo que no aparezca en tus datos y vuelve a cargarlo.")
                contenido = f"\n\n{sep}\n\n".join(ordenes)
                etiqueta = f"{path.name} · columna «{campo}»"
        except UnicodeDecodeError:
            self._error("No se pudo leer el archivo", f"{path.name} no está en UTF-8. Vuelve a guardarlo con esa codificación.")
            return False
        except (ErrorDeEntrada, OSError) as e:
            self._error("No se pudo leer el archivo", str(e))
            return False
        self.txt_ordenes.delete("1.0", "end")
        self.txt_ordenes.insert("1.0", contenido)
        self.var_archivo.set(etiqueta)
        self._archivo_cargado = ruta
        self._al_cambiar()
        return True

    def _elegir_columna(self, archivo: str, columnas: list[str]) -> Optional[str]:
        if len(columnas) == 1:
            return columnas[0]
        d = DialogoColumna(self, archivo, columnas, CAMPO_POR_DEFECTO)
        self.root.wait_window(d)
        return d.eleccion

    def _ordenes(self, config: Configuracion) -> list[str]:
        ordenes = split_messages(config.mensaje, config.separador)
        ordenes = [aplicar_plantilla(config.plantilla, o) for o in ordenes]
        try:
            limite = int(self.var_limite.get().strip() or 0)
        except ValueError:
            limite = 0
        return ordenes[:limite] if limite > 0 else ordenes

    # ------------------------------------------------------------------
    # Estimar y arrancar
    # ------------------------------------------------------------------

    def _ajustes_o_aviso(self) -> Optional[Configuracion]:
        try:
            c = self.leer_ajustes()
        except ErrorDeAjustes as e:
            self._error("Revisa los ajustes", str(e))
            self._ir_a_ajustes()
            return None
        v = c.validar()
        if not v.ok:
            self._error("Revisa los ajustes", "\n".join(v.errores))
            self._ir_a_ajustes()
            return None
        self.config = c
        return c

    def estimar_coste(self) -> None:
        c = self._ajustes_o_aviso()
        if c is None:
            return
        ordenes = self._ordenes(c)
        if not ordenes:
            self._aviso("Sin órdenes", "Escribe o carga primero las órdenes que quieres enviar.")
            return
        self._habilitar(self.boton_estimar, False)
        self.var_estado.set("Estado: calculando la estimación…")

        def trabajo() -> None:
            try:
                est = estimar(c, ordenes, client=self.cliente_conteo())
            except Exception as e:  # pragma: no cover - defensa
                logger.exception("Falló la estimación")
                self.bridge.post(self._estimacion_fallo, str(e))
            else:
                self.bridge.post(self._estimacion_lista, est, c)

        threading.Thread(target=trabajo, daemon=True, name="estimar").start()

    def _estimacion_lista(self, est: Estimacion, c: Configuracion) -> None:
        self._habilitar(self.boton_estimar, True)
        self.var_estado.set("Estado: en espera")
        DialogoEstimacion(self, est, c.tope_gasto_usd)

    def _estimacion_fallo(self, texto: str) -> None:
        self._habilitar(self.boton_estimar, True)
        self.var_estado.set("Estado: en espera")
        self._error("No se pudo estimar", texto)

    def _asegurar_clave(self) -> bool:
        escrita = self.var_clave.get().strip()
        if escrita:
            self.guardar(escrita)
            self.var_clave.set("")
            self._refrescar_clave()
        if obtener_api_key():
            return True
        self._error("Falta la clave de API", "No hay ninguna clave de Anthropic. Pégala en Ajustes y pulsa «Probar y guardar».")
        self._ir_a_ajustes()
        return False

    def iniciar(self, confirmar: bool = True) -> bool:
        """Crea un lote nuevo con lo que hay en la caja y lo lanza. `confirmar=False` en el arranque automático."""
        if self._ocupado or self.ejecutor.activo:
            return False
        c = self._ajustes_o_aviso()
        if c is None:
            return False
        if c.modo_continuo and c.es_lote:
            self._error("Bucle no disponible", "El bucle solo existe en modo inmediato: repetir un lote entero sería muy caro.")
            return False
        ordenes = self._ordenes(c)
        if not ordenes:
            self._aviso("Sin órdenes", "Escribe o carga primero las órdenes que quieres enviar.")
            return False
        if not self._asegurar_clave():
            return False
        if confirmar and not self._confirmar("Confirmar lote", self._texto_confirmacion(c, ordenes)):
            return False
        self._guardar_token_telegram()
        job = Job.crear(ordenes, c)
        job.save()
        self.config = c
        ConfigManager.guardar(replace(c, mensaje=c.mensaje if len(c.mensaje) <= MAX_BORRADOR else ""))
        self._lanzar(c, job)
        return True

    def _texto_confirmacion(self, c: Configuracion, ordenes: list[str]) -> str:
        est = estimar(c, ordenes)
        tope = "SIN TOPE" if c.tope_gasto_usd == 0 else formatear_usd(c.tope_gasto_usd, 2)
        lineas = [
            f"Órdenes: {_miles(len(ordenes))}",
            f"Modelo: {c.modelo}",
            f"Modo: {est.modo}",
            f"Coste máximo estimado: {formatear_usd(est.coste_maximo, 2)}",
            f"Tope de gasto: {tope}",
        ]
        if c.modo_continuo:
            lineas.append("Bucle: SÍ (se repetirá hasta la hora límite o el tope)")
        avisos = list(dict.fromkeys([*c.validar().avisos, *est.avisos]))
        if avisos:
            lineas += ["", "Avisos:", *[f"• {a}" for a in avisos]]
        return "\n".join(lineas) + "\n\n¿Iniciar?"

    def reanudar(self, confirmar: bool = False) -> bool:
        """Continúa el lote abierto con el modelo y el modo con que se creó."""
        job = self.job
        if job is None or self._ocupado or self.ejecutor.activo or not job.hay_trabajo():
            return False
        c = self._ajustes_o_aviso()
        if c is None:
            return False
        # El lote manda: mezclar modelos o reenviar por la API normal un lote pagado a mitad de precio sería un accidente.
        c = replace(c, modelo=job.modelo, modo=job.modo, system_prompt=job.system_prompt)
        if not self._asegurar_clave():
            return False
        if confirmar and not self._confirmar(
                "Reanudar lote",
                f"Lote {job.job_id}: quedan {len(job.pendientes()) + len(job.fallidas())} órdenes por enviar "
                f"con {job.modelo} en modo {job.modo}.\n\n¿Reanudar?"):
            return False
        self._guardar_token_telegram()
        job.reintentar_fallidas()
        self._cargando = True
        self.v["modelo"].set(job.modelo)
        self.v["modo"].set(job.modo)
        self._cargando = False
        self._actualizar_pistas()
        self._log(f"Reanudando el lote {job.job_id} con {job.modelo} (modo {job.modo}).")
        self._lanzar(c, job)
        return True

    def _lanzar(self, c: Configuracion, job: Job) -> None:
        self.job = job
        self._config_en_marcha = c
        self._ultimo_error = ""
        self._enviando = None
        self._poblar_resultados(job)
        self._pintar_error("")
        self.var_proximo.set("Próximo intento --:--")
        self.var_estado.set("Estado: iniciando…")
        self._pintar_gasto(job.coste_total())
        self.var_exitos.set(f"Éxitos totales {EstadoMonitor.cargar().exitos}")
        self._ocupar(True)
        self._log(f"Lote {job.job_id}: {_miles(job.total_count())} órdenes · {c.modelo} · modo {c.modo}.")
        self._purgar(c)
        self.ejecutor.iniciar(c, job, _EventosDelLote(self))

    def _purgar(self, c: Configuracion) -> None:
        try:
            # Nunca se borra un lote con órdenes sin enviar (lo garantiza la propia función), ni el que va a correr.
            borrados = purgar_jobs_antiguos(c.retencion_dias, proteger=self.job.job_id if self.job else None)
            if borrados:
                self._log(f"Se borraron {borrados} archivos de lotes antiguos (más de {c.retencion_dias} días).")
        except Exception:
            logger.exception("No se pudieron purgar los lotes antiguos")

    def detener(self) -> None:
        if not (self._ocupado or self.ejecutor.activo):
            return
        job = self.job
        if job is not None and job.modo == "lote" and job.batch_id and self._confirmar(
                "Detener el lote",
                "Este lote ya está en los servidores de Anthropic y sigue procesándose aunque dejes de esperar "
                "o cierres el programa; se cobrará al terminar.\n\n"
                "¿Quieres cancelarlo también allí? Lo que ya se haya procesado se cobra; el resto, no.\n\n"
                "Sí = cancelar el lote\n"
                "No = solo dejar de esperar: podrás reanudar, y si luego quieres cancelarlo, pulsa Detener otra vez"):
            if self.ejecutor.cancelar_lote(self._lote_cancelado):
                self.var_estado.set("Estado: cancelando el lote en el servidor…")
                self._log("Cancelación pedida al servidor. Lo ya procesado se cobra; el resto quedará como «cancelada».")
                self._habilitar(self.boton_detener, False)
                return
        self.ejecutor.detener()
        self.var_estado.set("Estado: cancelando (termino la petición en curso)…")
        self._log("Parada pedida. Se termina la petición en curso y no se envía ninguna más.")
        self._habilitar(self.boton_detener, False)

    def _lote_cancelado(self, ok: bool) -> None:
        if ok:
            self._log("El servidor aceptó la cancelación. Pulsa «Reanudar» para recoger lo que se llegó a procesar.")
        else:
            self._log("No se pudo cancelar el lote en el servidor. Compruébalo en la consola de Anthropic.")
            self._aviso("Cancelar el lote", "No se pudo cancelar el lote en el servidor.\n\n"
                        "Compruébalo en la consola de Anthropic (Batches); puede seguir procesándose y cobrándose.")

    # ------------------------------------------------------------------
    # Pantalla durante el lote
    # ------------------------------------------------------------------

    def _ocupar(self, ocupado: bool) -> None:
        self._ocupado = ocupado
        for w in self._bloqueables:
            self._habilitar(w, not ocupado)
        if not ocupado:
            self._actualizar_pistas()
        self._refrescar_botones()

    def _refrescar_botones(self) -> None:
        ocupado = self._ocupado
        job = self.job
        hay_trabajo = bool(job and job.hay_trabajo())
        self._habilitar(self.boton_iniciar, not ocupado)
        self._habilitar(self.boton_detener, ocupado)
        self._habilitar(self.boton_reanudar, not ocupado and hay_trabajo)
        # Con órdenes sin enviar se «reanuda»; si solo quedan las que fallaron, se «reintentan».
        solo_fallidas = bool(job and job.fallidas() and not job.pendientes())
        self.boton_reanudar.configure(text="Reintentar fallidas" if solo_fallidas else "Reanudar")
        self._habilitar(self.boton_exportar, bool(job and job.messages))
        self._habilitar(self.boton_estimar, not ocupado)
        self._habilitar(self.boton_lotes, not ocupado)

    def _texto_estado(self, msg: JobMessage) -> tuple[str, str]:
        if msg.status == STATUS_PENDING:
            if msg.id == self._enviando:
                return "Enviando…", "envio"
            return NOMBRES_ESTADO[STATUS_PENDING], "cola"
        etiqueta = {STATUS_COMPLETED: "lista", STATUS_FAILED: "error", STATUS_REFUSED: "rechazada"}.get(msg.status, "cola")
        return NOMBRES_ESTADO.get(msg.status, msg.status), etiqueta

    def _valores_fila(self, msg: JobMessage) -> tuple[tuple, str]:
        estado, etiqueta = self._texto_estado(msg)
        tokens = _miles(msg.tokens_input + msg.tokens_output) if (msg.tokens_input or msg.tokens_output) else ""
        coste = formatear_usd(msg.coste_usd) if msg.coste_usd else ""
        return (msg.id, _acortar(msg.text), estado, tokens, coste), etiqueta

    def _poblar_resultados(self, job: Job) -> None:
        self.tabla.delete(*self.tabla.get_children())
        self._filas.clear()
        self._hechas = {m.id for m in job.messages if m.status != STATUS_PENDING}
        for m in job.messages[:MAX_FILAS]:
            valores, etiqueta = self._valores_fila(m)
            self._filas[m.id] = self.tabla.insert("", "end", values=valores, tags=(etiqueta,))
        if job.total_count() > MAX_FILAS:
            self._log(f"La tabla muestra las primeras {_miles(MAX_FILAS)} órdenes; el lote completo se procesa y se exporta.")
        self._refrescar_progreso()
        self._refrescar_botones()

    def _actualizar_fila(self, msg: JobMessage) -> None:
        iid = self._filas.get(msg.id)
        if iid is None:
            return
        valores, etiqueta = self._valores_fila(msg)
        try:
            self.tabla.item(iid, values=valores, tags=(etiqueta,))
        except tk.TclError:
            pass

    def _refrescar_progreso(self) -> None:
        total = self.job.total_count() if self.job else 0
        hechas = len(self._hechas)
        self.progreso.configure(maximum=max(total, 1), value=hechas)
        self.var_progreso.set(f"Progreso {_miles(hechas)}/{_miles(total)}")

    def _pintar_gasto(self, usd: Decimal) -> None:
        tope = (self._config_en_marcha or self.config).tope_gasto_usd
        self.var_gasto.set(f"Gasto {formatear_usd(usd)} / {formatear_usd(tope, 2) if tope > 0 else 'sin tope'}")

    def _pintar_error(self, texto: str) -> None:
        self.var_error.set(f"Último error: {texto}" if texto else "")
        if texto:
            self.etiqueta_error.grid()
        else:
            self.etiqueta_error.grid_remove()

    def _log(self, texto: str) -> None:
        try:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", f"[{datetime.now():%H:%M:%S}] {texto}\n")
            lineas = int(self.txt_log.index("end-1c").split(".")[0])
            if lineas > MAX_LINEAS_LOG:
                self.txt_log.delete("1.0", f"{lineas - MAX_LINEAS_LOG}.0")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        except tk.TclError:
            pass

    # -- Eventos del ejecutor ------------------------------------------

    def on_estado(self, texto: str, activo: bool) -> None:
        self.var_estado.set(f"Estado: {texto}")
        self._log(texto)

    def on_inicio_orden(self, msg: JobMessage) -> None:
        self._enviando = msg.id
        self._actualizar_fila(msg)
        iid = self._filas.get(msg.id)
        if iid:
            self.tabla.see(iid)

    def on_resultado(self, msg: JobMessage) -> None:
        if self._enviando == msg.id and msg.status != STATUS_PENDING:
            self._enviando = None
        if msg.status != STATUS_PENDING:
            self._hechas.add(msg.id)
        else:
            self._hechas.discard(msg.id)
        self._actualizar_fila(msg)
        self._refrescar_progreso()
        self.var_exitos.set(f"Éxitos totales {EstadoMonitor.cargar().exitos}")
        marca = {STATUS_COMPLETED: "OK", STATUS_REFUSED: "rechazada", STATUS_FAILED: "FALLÓ"}.get(msg.status, msg.status)
        extra = f" — {msg.detalle}" if msg.detalle else ""
        self._log(f"Orden {msg.id}: {marca} · {_miles(msg.tokens_input + msg.tokens_output)} tokens · "
                  f"{formatear_usd(msg.coste_usd)}{extra}")

    def on_fin_ciclo(self, completadas: int, total: int, ciclo: int) -> None:
        self.var_progreso.set(f"Ciclo {ciclo} terminado: {_miles(completadas)}/{_miles(total)}")

    def on_error(self, texto: str) -> None:
        self._ultimo_error = texto
        self._pintar_error(texto)
        self._log(f"ERROR: {texto}")

    def on_tick(self, total: float, restante: float) -> None:
        siguiente = datetime.now() + timedelta(seconds=int(restante))
        minutos, segundos = divmod(int(restante), 60)
        self.var_proximo.set(f"Próximo intento {siguiente:%H:%M:%S} ({minutos:02d}:{segundos:02d})")

    def on_gasto(self, usd: Decimal) -> None:
        self._pintar_gasto(usd)

    def on_terminado(self, razon: StopReason, job: Job) -> None:
        previa, self._enviando = self._enviando, None
        self.job = job
        self._ocupar(False)
        if previa is not None and job.mensaje(previa) is not None:
            self._actualizar_fila(job.mensaje(previa))  # dejó de estar «Enviando…»
        self._refrescar_progreso()
        self.var_proximo.set("Próximo intento --:--")
        self._pintar_gasto(job.coste_total())
        frase = FRASES_DE_PARADA.get(razon, razon.value)
        self.var_estado.set(f"Estado: {frase}")
        self._log(frase)
        self._avisar_fin(razon, job, frase)

    def _avisar_fin(self, razon: StopReason, job: Job, frase: str) -> None:
        if not self._visible():
            return  # en la bandeja ya avisó el escritorio / Telegram
        e = job.estadisticas()
        resumen = (f"Completadas {_miles(e['completadas'])}/{_miles(e['total'])} · fallidas {e['fallidas']} · "
                   f"rechazadas {e['rechazadas']} · pendientes {e['pendientes']}\n"
                   f"Coste de este lote: {formatear_usd(e['coste_usd'])}")
        if razon is StopReason.COMPLETED:
            if e["fallidas"]:
                self._aviso("Lote terminado con fallos", f"{frase}\n\n{resumen}\n\nPuedes pulsar «Reintentar fallidas».")
            else:
                self._info("Lote terminado", f"{frase}\n\n{resumen}")
            return
        pista = {
            StopReason.NO_CREDIT: f"\n\nAñade saldo en {URL_FACTURACION} y pulsa «Reanudar».",
            StopReason.ACCOUNT_LIMIT: "\n\nEsto es un límite de tu cuenta de Anthropic, no de este programa. Cuando "
                                      "vuelva el acceso, pulsa «Reanudar».",
            StopReason.BUDGET: "\n\nSube el tope en Ajustes y pulsa «Reanudar» para seguir.",
            StopReason.NETWORK: "\n\nCuando vuelva la conexión, pulsa «Reanudar».",
            StopReason.FATAL: "\n\nCuando hayas corregido la causa, pulsa «Reanudar»: la orden que falló se reintentará.",
            StopReason.STOPPED: "\n\nPulsa «Reanudar» para continuar donde se quedó.",
            StopReason.DEADLINE: "\n\nPulsa «Reanudar» para continuar donde se quedó.",
        }.get(razon, "")
        detalle = f"\n\nDetalle: {self._ultimo_error}" if self._ultimo_error else ""
        self._aviso("Lote detenido", f"{frase}\n\n{resumen}{pista}{detalle}")

    # ------------------------------------------------------------------
    # Resultados: ver, exportar, abrir carpetas, lotes anteriores
    # ------------------------------------------------------------------

    def _seleccionada(self) -> Optional[int]:
        sel = self.tabla.selection()
        if not sel:
            return None
        valores = self.tabla.item(sel[0], "values")
        return int(valores[0]) if valores else None

    def texto_de_respuesta(self, msg_id: int) -> str:
        job = self.job
        if job is None:
            return ""
        msg = job.mensaje(msg_id)
        if msg is None:
            return ""
        if msg.status == STATUS_FAILED:
            return msg.detalle
        return leer_respuesta(job, msg_id, job.ciclos_completados + 1)

    def ver_respuesta(self, msg_id: Optional[int] = None) -> None:
        msg_id = msg_id if msg_id is not None else self._seleccionada()
        job = self.job
        msg = job.mensaje(msg_id) if (job and msg_id is not None) else None
        if msg is None:
            return
        estado = f"Orden {msg.id} · {NOMBRES_ESTADO.get(msg.status, msg.status)}"
        if msg.tokens_input or msg.tokens_output:
            estado += f" · {_miles(msg.tokens_input)} tokens de entrada, {_miles(msg.tokens_output)} de salida"
            estado += f" · {formatear_usd(msg.coste_usd)}"
        if msg.truncated:
            estado += " · respuesta truncada por max_tokens"
        VisorRespuesta(self, f"Orden {msg.id}", estado, msg.text, self.texto_de_respuesta(msg.id), self._copiar)

    def _copiar(self, texto: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(texto)

    def exportar_resultados(self) -> None:
        if self.job is None or not self.job.messages:
            self._info("Exportar", "Todavía no hay nada que exportar.")
            return
        ruta = self._pedir_destino()
        if ruta:
            self.exportar_a(ruta)

    def exportar_a(self, ruta: str) -> bool:
        if self.job is None:
            return False
        try:
            destino = exportar(self.job, Path(ruta))
        except (ErrorDeExportacion, OSError) as e:
            self._error("No se pudo exportar", str(e))
            return False
        self._log(f"Resultados exportados a {destino}")
        return True

    def abrir_respuestas(self) -> None:
        paths.RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
        self._abrir(paths.RESPONSES_DIR)

    def abrir_logs(self) -> None:
        try:
            paths.LOG_FILE.touch(exist_ok=True)
        except OSError:
            pass
        self._abrir(paths.LOG_FILE)

    def abrir_datos(self) -> None:
        self._abrir(paths.DATA_DIR)

    def _abrir(self, ruta: Path) -> None:
        if not self.abrir(ruta):
            self._error("No se pudo abrir", f"No pude abrir {ruta}.")

    def lotes_anteriores(self) -> None:
        DialogoLotes(self, [d for d in listar_jobs() if d["etiqueta"] != ETIQUETA_COLA], self.abrir_lote)

    def abrir_lote(self, job_id: str) -> bool:
        """Muestra un lote guardado en el panel (para ver, exportar, reintentar o reanudar)."""
        if self._ocupado or self.ejecutor.activo:
            return False
        job = Job.load(job_id)
        if job is None:
            self._error("Lotes anteriores", f"No se pudo abrir el lote {job_id}.")
            return False
        self.job = job
        self._config_en_marcha = None
        self._ultimo_error = ""
        self._pintar_error("")
        self._poblar_resultados(job)
        self._pintar_gasto(job.coste_total())
        self.var_estado.set(f"Estado: lote {job.job_id} abierto")
        self._log(f"Lote {job.job_id} abierto: {job.completed_count()}/{job.total_count()} completadas.")
        self.cuaderno.select(self.tab_panel)
        return True

    # ------------------------------------------------------------------
    # Arranque, cambio de modo y cierre
    # ------------------------------------------------------------------

    def _arrancar(self) -> None:
        c = self.config
        retomado = False
        try:
            estado = EstadoMonitor.cargar()
            job = Job.load(estado.current_job_id) if estado.current_job_id else None
            if job is not None and job.etiqueta == ETIQUETA_COLA:
                job = None  # la cola del modo simple se retoma en el modo simple
            if job is not None and job.hay_trabajo():
                self.job = job
                self._poblar_resultados(job)
                self._pintar_gasto(job.coste_total())
                if c.auto_arranque or self._confirmar(
                        "Lote sin terminar",
                        f"Se encontró un lote sin terminar (ID {job.job_id}).\n"
                        f"Completadas: {job.completed_count()}/{job.total_count()}\n\n¿Quieres reanudarlo ahora?"):
                    retomado = self.reanudar()
                elif estado.current_job_id:
                    estado.current_job_id = None
                    estado.guardar()
        except Exception:
            logger.exception("No se pudo comprobar si había un lote sin terminar")
        if not retomado and c.auto_arranque and self._ordenes(c):
            self.iniciar(confirmar=False)
        if c.iniciar_minimizado and self._usar_bandeja:
            self._segundo_plano()

    def cambiar_a_simple(self) -> None:
        if (self._ocupado or self.ejecutor.activo) and not self._confirmar(
                "Cambiar de modo",
                "Hay un lote en marcha. Si cambias de modo se detendrá (queda guardado y se puede reanudar).\n\n¿Continuar?"):
            return
        self.guardar_ajustes()
        self.config = replace(self.config, interfaz="simple")
        ConfigManager.guardar(replace(self.config, mensaje=self.config.mensaje if len(self.config.mensaje) <= MAX_BORRADOR else ""))
        self.resultado = "simple"
        self._salir()

    def _preguntar_cierre(self) -> str:
        """'fondo' (seguir en la bandeja), 'salir' (parar y cerrar) o 'volver'."""
        if self._usar_bandeja:
            r = messagebox.askyesnocancel(
                "Hay un lote en marcha",
                "¿Qué quieres hacer?\n\nSí = minimizar a la bandeja y seguir\n"
                "No = detener el lote y cerrar (queda guardado)\nCancelar = volver",
                parent=self.root)
            return "volver" if r is None else ("fondo" if r else "salir")
        return "salir" if self._confirmar(
            "Hay un lote en marcha",
            "Si cierras ahora el lote se detiene (queda guardado y se puede reanudar).\n\n¿Cerrar?") else "volver"

    def cerrar(self) -> None:
        if self._ocupado or self.ejecutor.activo:
            eleccion = self._preguntar_cierre()
            if eleccion == "volver":
                return
            if eleccion == "fondo" and self._segundo_plano():
                return
        self.guardar_ajustes()
        self._salir()

    def _segundo_plano(self) -> bool:
        if self.bandeja is None:
            self.bandeja = tray.BandejaDelSistema(
                "Claude Batch Monitor", "Abrir", "Salir",
                al_abrir=lambda: self.bridge.post(self._restaurar),
                al_salir=lambda: self.bridge.post(self._salir),
            )
        if not self.bandeja.mostrar():
            self.bandeja = None
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
        self._anular(self._guardado)
        self.ejecutor.detener()
        self.ejecutor.esperar(5)
        if self.bandeja:
            self.bandeja.ocultar()
        self.bridge.drenar()
        self.bridge.cerrar()
        for ident in list(self._temporizadores):
            self._anular(ident)
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> Optional[str]:
        self.root.mainloop()
        return self.resultado


def ejecutar_avanzada(config: Optional[Configuracion] = None) -> Optional[str]:
    """Abre el modo avanzado. Devuelve 'simple' si la persona pidió cambiar de modo."""
    root = tk.Tk()
    app = AppAvanzada(root, config=config)
    resultado = app.run()
    del app, root
    gc.collect()  # las variables de tkinter se liberan aquí, en el hilo de la ventana
    return resultado
