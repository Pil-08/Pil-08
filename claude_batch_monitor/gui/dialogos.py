"""Ventanas secundarias del modo avanzado (el modo simple tiene las suyas en `simple.py`)."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Optional

from ..estimator import Estimacion
from ..pricing import formatear_usd
from . import formato
from .comun import Modal


def _texto_solo_lectura(master: tk.Misc, app: Any, alto: int, mono: bool = False) -> tuple[ttk.Frame, tk.Text]:
    """Un `Text` de solo lectura con su barra de desplazamiento, con el aspecto de las demás tarjetas."""
    p = app.p
    marco = ttk.Frame(master)
    marco.columnconfigure(0, weight=1)
    marco.rowconfigure(0, weight=1)
    texto = tk.Text(
        marco, height=alto, wrap="word", relief="flat", borderwidth=0, padx=10, pady=8,
        background=p.superficie, foreground=p.texto, highlightthickness=1,
        highlightbackground=p.borde, highlightcolor=p.borde,
        font=app.f.mono if mono else app.f.texto, insertbackground=p.texto,
    )
    barra = ttk.Scrollbar(marco, orient="vertical", command=texto.yview)
    texto.configure(yscrollcommand=barra.set)
    texto.grid(row=0, column=0, sticky="nsew")
    barra.grid(row=0, column=1, sticky="ns")
    return marco, texto


def _escribir_en(texto: tk.Text, contenido: str) -> None:
    texto.configure(state="normal")
    texto.delete("1.0", "end")
    texto.insert("1.0", contenido)
    texto.configure(state="disabled")


class DialogoColumna(Modal):
    """Un CSV/JSONL tiene varias columnas: se pregunta cuál lleva el texto que se enviará a Claude."""

    def __init__(self, app: Any, archivo: str, columnas: list[str], sugerida: str):
        super().__init__(app, "Elegir columna")
        self.eleccion: Optional[str] = None
        cuerpo = ttk.Frame(self, padding=20)
        cuerpo.grid(row=0, column=0, sticky="nsew")
        cuerpo.columnconfigure(0, weight=1)
        ttk.Label(cuerpo, text=archivo, style="Seccion.TLabel").grid(row=0, column=0, sticky="w")
        tk.Label(
            cuerpo, justify="left", anchor="w", wraplength=440, background=app.p.fondo,
            foreground=app.p.texto, font=app.f.base,
            text="Este archivo tiene varias columnas. ¿Cuál contiene el texto que quieres enviar a Claude? "
                 "Cada fila será una orden.",
        ).grid(row=1, column=0, sticky="w", pady=(6, 12))
        self.var = tk.StringVar(value=sugerida if sugerida in columnas else columnas[0])
        self.combo = ttk.Combobox(cuerpo, textvariable=self.var, values=columnas, state="readonly", width=40)
        self.combo.grid(row=2, column=0, sticky="ew")
        botones = ttk.Frame(cuerpo)
        botones.grid(row=3, column=0, sticky="e", pady=(18, 0))
        ttk.Button(botones, text="Cancelar", command=self.destroy).pack(side="left")
        ttk.Button(botones, text="Usar esta columna", style="Primary.TButton", command=self.aceptar).pack(
            side="left", padx=(8, 0))
        self.bind("<Return>", lambda e: self.aceptar())
        self.mostrar()

    def aceptar(self) -> None:
        self.eleccion = self.var.get()
        self.destroy()


class VisorRespuesta(Modal):
    """La orden y su respuesta completas, con un botón para copiarla."""

    def __init__(self, app: Any, titulo: str, estado: str, solicitud: str, respuesta: str,
                 al_copiar: Callable[[str], None]):
        super().__init__(app, titulo, redimensionable=True)
        self.minsize(560, 420)
        self.geometry("720x560")
        self.rowconfigure(0, weight=1)
        cuerpo = ttk.Frame(self, padding=16)
        cuerpo.grid(row=0, column=0, sticky="nsew")
        cuerpo.columnconfigure(0, weight=1)
        cuerpo.rowconfigure(2, weight=1)
        cuerpo.rowconfigure(4, weight=3)
        self.respuesta = respuesta
        ttk.Label(cuerpo, text=estado, style="Sub.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(cuerpo, text="Orden", style="Seccion.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 4))
        marco_a, self.texto_orden = _texto_solo_lectura(cuerpo, app, 5)
        marco_a.grid(row=2, column=0, sticky="nsew")
        ttk.Label(cuerpo, text="Respuesta", style="Seccion.TLabel").grid(row=3, column=0, sticky="w", pady=(12, 4))
        marco_b, self.texto_respuesta = _texto_solo_lectura(cuerpo, app, 10)
        marco_b.grid(row=4, column=0, sticky="nsew")
        _escribir_en(self.texto_orden, solicitud)
        self.var_formato = tk.BooleanVar(value=bool(getattr(app.config, "mostrar_formato", True)))
        self._pintar_respuesta()
        botones = ttk.Frame(cuerpo)
        botones.grid(row=5, column=0, sticky="e", pady=(14, 0))
        self.check_formato = ttk.Checkbutton(botones, text="Con formato", variable=self.var_formato,
                                             command=self._pintar_respuesta)
        self.check_formato.pack(side="left", padx=(0, 14))
        self.boton_copiar = ttk.Button(botones, text="Copiar respuesta", command=lambda: al_copiar(self.respuesta))
        self.boton_copiar.pack(side="left")
        if not respuesta:
            self.boton_copiar.state(["disabled"])
        ttk.Button(botones, text="Cerrar", style="Primary.TButton", command=self.destroy).pack(
            side="left", padx=(8, 0))
        self.mostrar()

    def _pintar_respuesta(self) -> None:
        if self.respuesta:
            formato.mostrar_texto_con_formato(self.texto_respuesta, self.respuesta, self.app.p, self.app.f, bool(self.var_formato.get()))
        else:
            _escribir_en(self.texto_respuesta, "(sin respuesta guardada)")


class DialogoEstimacion(Modal):
    """El peor caso de coste de lo que hay escrito, con los avisos que correspondan."""

    def __init__(self, app: Any, est: Estimacion, tope: Any):
        super().__init__(app, "Estimación de coste")
        cuerpo = ttk.Frame(self, padding=20)
        cuerpo.grid(row=0, column=0, sticky="nsew")
        cuerpo.columnconfigure(0, weight=1)
        lineas = est.resumen_lineas()
        if not est.plan:
            lineas.append(f"Tope configurado   : {'sin tope' if tope == 0 else formatear_usd(tope, 2)}")
        self.lineas = lineas
        marco, texto = _texto_solo_lectura(cuerpo, app, len(lineas) + 1, mono=True)
        marco.grid(row=0, column=0, sticky="ew")
        texto.configure(width=88)
        _escribir_en(texto, "\n".join(lineas))
        fila = 1
        explicacion = (
            "Con tu plan de Claude no pagas por token: cada pregunta gasta parte del cupo de tu plan (que se renueva "
            "cada 5 horas y cada semana). Si se agota, el programa espera a que vuelva y sigue solo."
            if est.plan else
            "El coste máximo supone que cada respuesta llena max_tokens. Lo normal es gastar bastante "
            "menos, pero el tope se calcula con el peor caso para que nunca te lleves una sorpresa."
        )
        tk.Label(cuerpo, text=explicacion, justify="left", anchor="w", wraplength=620,
                 background=app.p.fondo, foreground=app.p.apagado, font=app.f.pequena).grid(
            row=fila, column=0, sticky="w", pady=(10, 0))
        for aviso in est.avisos:
            fila += 1
            tk.Label(cuerpo, text=aviso, justify="left", anchor="w", wraplength=620, background=app.p.fondo,
                     foreground=app.p.aviso, font=app.f.base).grid(row=fila, column=0, sticky="w", pady=(8, 0))
        ttk.Button(cuerpo, text="Cerrar", style="Primary.TButton", command=self.destroy).grid(
            row=fila + 1, column=0, sticky="e", pady=(16, 0))
        self.mostrar()


class DialogoLotes(Modal):
    """Los lotes guardados en disco: abrir uno para ver sus resultados, reintentarlo o exportarlo."""

    def __init__(self, app: Any, lotes: list[dict], al_abrir: Callable[[str], None]):
        super().__init__(app, "Lotes anteriores", redimensionable=True)
        self.minsize(760, 360)
        self.geometry("860x420")
        self.rowconfigure(0, weight=1)
        self.al_abrir = al_abrir
        cuerpo = ttk.Frame(self, padding=16)
        cuerpo.grid(row=0, column=0, sticky="nsew")
        cuerpo.columnconfigure(0, weight=1)
        cuerpo.rowconfigure(1, weight=1)
        ttk.Label(
            cuerpo, style="Sub.TLabel", wraplength=740, justify="left",
            text="Se conservan en disco (por defecto 30 días). Abre uno para ver sus respuestas, "
                 "reintentar lo que falló o exportarlo.",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        columnas = ("id", "fecha", "modelo", "modo", "progreso", "coste")
        marco = ttk.Frame(cuerpo)
        marco.grid(row=1, column=0, sticky="nsew")
        marco.columnconfigure(0, weight=1)
        marco.rowconfigure(0, weight=1)
        self.tabla = ttk.Treeview(marco, columns=columnas, show="headings", selectmode="browse", height=9)
        for col, titulo, ancho, ancla in (
            ("id", "Lote", 90, "w"), ("fecha", "Creado", 140, "w"), ("modelo", "Modelo", 190, "w"),
            ("modo", "Modo", 80, "w"), ("progreso", "Progreso", 160, "center"), ("coste", "Coste", 90, "e"),
        ):
            self.tabla.heading(col, text=titulo)
            self.tabla.column(col, width=ancho, anchor=ancla, stretch=col == "modelo")
        barra = ttk.Scrollbar(marco, orient="vertical", command=self.tabla.yview)
        self.tabla.configure(yscrollcommand=barra.set)
        self.tabla.grid(row=0, column=0, sticky="nsew")
        barra.grid(row=0, column=1, sticky="ns")
        self.tabla.tag_configure("incompleto", foreground=app.p.aviso)
        for d in lotes:
            self.tabla.insert(
                "", "end", iid=d["job_id"], tags=("incompleto",) if d["incompleto"] else (),
                values=(
                    d["job_id"], d["created_at"][:16].replace("T", " "), d["modelo"], d["modo"],
                    f"{d['completadas']}/{d['total']}" + ("  (sin terminar)" if d["incompleto"] else ""),
                    formatear_usd(d["coste_usd"]),
                ),
            )
        if not lotes:
            ttk.Label(cuerpo, text="Todavía no hay lotes guardados.", style="Sub.TLabel").grid(
                row=2, column=0, sticky="w", pady=(8, 0))
        botones = ttk.Frame(cuerpo)
        botones.grid(row=3, column=0, sticky="e", pady=(14, 0))
        ttk.Button(botones, text="Cerrar", command=self.destroy).pack(side="left")
        self.boton_abrir = ttk.Button(botones, text="Abrir en el panel", style="Primary.TButton", command=self.abrir)
        self.boton_abrir.pack(side="left", padx=(8, 0))
        self.tabla.bind("<Double-1>", lambda e: self.abrir())
        self.tabla.bind("<Return>", lambda e: self.abrir())
        hijos = self.tabla.get_children()
        if hijos:
            self.tabla.selection_set(hijos[0])
            self.tabla.focus(hijos[0])
        else:
            self.boton_abrir.state(["disabled"])
        self.mostrar()

    def seleccionado(self) -> Optional[str]:
        sel = self.tabla.selection()
        return sel[0] if sel else None

    def abrir(self) -> None:
        job_id = self.seleccionado()
        if job_id:
            self.destroy()
            self.al_abrir(job_id)
