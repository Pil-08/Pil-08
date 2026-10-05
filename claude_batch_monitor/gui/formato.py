"""Mostrar las respuestas con formato: títulos, negrita, cursiva, código, listas, citas y tablas.

Claude contesta en Markdown; leerlo con los asteriscos y las almohadillas a la vista cansa. `analizar` convierte
el texto en trozos con estilos (no necesita ventanas, así que se prueba sola) y `mostrar` los escribe en un
`Text` de tkinter. Es un Markdown sencillo, pensado para respuestas de chat: si algo no se entiende se deja tal
cual, y si algo falla se muestra el texto original. Nunca se pierde ni se cambia una respuesta: copiar y exportar
siempre usan el texto original.
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import font as tkfont
from typing import Any

Segmento = tuple[str, tuple[str, ...]]

_VALLA = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)\s*$")
_TITULO = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_REGLA = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_CITA = re.compile(r"^\s{0,3}>\s?(.*)$")
_LISTA = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_FILA_DE_TABLA = re.compile(r"^\s*\|.*\|\s*$")
_SEPARADOR_DE_TABLA = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

_INLINE = re.compile(
    r"(?P<cod>(?P<tic>`+)(?P<codigo>.+?)(?<!`)(?P=tic)(?!`))"
    r"|\[(?P<enlace>[^\]\n]+)\]\((?P<url>[^)\s]+)\)"
    r"|(?P<marca3>\*\*\*|___)(?=\S)(?P<ambas>.+?)(?<=\S)(?P=marca3)"
    r"|(?P<marca>\*\*|__)(?=\S)(?P<negrita>.+?)(?<=\S)(?P=marca)"
    r"|(?<![\w*])\*(?![\s*])(?P<cursiva>[^*\n]+?)(?<!\s)\*(?![\w*])"
    r"|(?<![\w_])_(?![\s_])(?P<cursiva2>[^_\n]+?)(?<!\s)_(?![\w_])"
)

# Un carácter escapado (\*) no se interpreta: se guarda con un sustituto privado y se devuelve al final.
_ESCAPABLES = "\\`*_{}[]()#+-.!|>~"
_ESCAPE = re.compile(r"\\([" + re.escape(_ESCAPABLES) + r"])")
_SUSTITUTO = {c: chr(0xE000 + i) for i, c in enumerate(_ESCAPABLES)}
_RESTAURAR = {v: k for k, v in _SUSTITUTO.items()}
_RESTAURAR_RE = re.compile("[" + "".join(_RESTAURAR) + "]")

ETIQUETAS_DE_TITULO = {1: "h1", 2: "h2"}  # del 3 al 6 comparten tamaño
ANCHO_REGLA = 40
ANCHO_MAXIMO_COLUMNA = 48


def _restaurar(texto: str) -> str:
    return _RESTAURAR_RE.sub(lambda m: _RESTAURAR[m.group(0)], texto)


def _juntar(etiquetas: tuple[str, ...]) -> tuple[str, ...]:
    """Tk no mezcla fuentes de etiquetas distintas: negrita + cursiva necesita su propia etiqueta."""
    if "negrita" in etiquetas and "cursiva" in etiquetas:
        return tuple(e for e in etiquetas if e not in ("negrita", "cursiva")) + ("negrita_cursiva",)
    return etiquetas


def _en_linea(texto: str, base: tuple[str, ...] = ()) -> list[Segmento]:
    """El formato dentro de una línea: `código`, **negrita**, *cursiva*, [enlaces](url)."""
    salida: list[Segmento] = []
    pos = 0
    for m in _INLINE.finditer(texto):
        if m.start() > pos:
            salida.append((texto[pos:m.start()], base))
        if m.group("cod"):
            salida.append((m.group("codigo"), base + ("codigo",)))
        elif m.group("enlace") is not None:
            visible, url = m.group("enlace"), m.group("url")
            salida.append((visible, base + ("enlace",)))
            if visible.strip() != url:
                salida.append((f" ({url})", base + ("apagado",)))
        elif m.group("ambas") is not None:
            salida += _en_linea(m.group("ambas"), _juntar(base + ("negrita", "cursiva")))
        elif m.group("negrita") is not None:
            salida += _en_linea(m.group("negrita"), _juntar(base + ("negrita",)))
        else:
            salida += _en_linea(m.group("cursiva") or m.group("cursiva2"), _juntar(base + ("cursiva",)))
        pos = m.end()
    if pos < len(texto):
        salida.append((texto[pos:], base))
    return salida


def _celdas(linea: str) -> list[str]:
    return [c.strip() for c in linea.strip().strip("|").split("|")]


def _tabla(filas: list[str]) -> list[Segmento]:
    """Una tabla Markdown como columnas de texto alineadas, con la cabecera en negrita."""
    datos = [_celdas(f) for f in filas if not _SEPARADOR_DE_TABLA.match(f)]
    if not datos:
        return []
    columnas = max(len(f) for f in datos)
    datos = [f + [""] * (columnas - len(f)) for f in datos]
    anchos = [min(ANCHO_MAXIMO_COLUMNA, max(len(f[c]) for f in datos)) for c in range(columnas)]
    salida: list[Segmento] = []
    for n, fila in enumerate(datos):
        texto = "  │  ".join(celda[: anchos[c]].ljust(anchos[c]) for c, celda in enumerate(fila)).rstrip()
        salida.append((texto + "\n", ("tabla_cabecera",) if n == 0 else ("tabla",)))
    return salida


def _analizar(texto: str) -> list[Segmento]:
    texto = _ESCAPE.sub(lambda m: _SUSTITUTO[m.group(1)], texto.replace("\r\n", "\n"))
    lineas = texto.split("\n")
    salida: list[Segmento] = []
    valla: str = ""
    i = 0
    while i < len(lineas):
        linea = lineas[i]
        fin_de_linea = "\n" if i < len(lineas) - 1 else ""

        if valla:  # dentro de un bloque de código: se copia sin interpretar nada
            cierre = _VALLA.match(linea)
            if cierre and cierre.group(1)[0] == valla[0] and len(cierre.group(1)) >= len(valla) and not cierre.group(2):
                valla = ""
            else:
                salida.append((linea + fin_de_linea, ("bloque",)))
            i += 1
            continue

        apertura = _VALLA.match(linea)
        if apertura:
            valla = apertura.group(1)
            i += 1
            continue

        if _FILA_DE_TABLA.match(linea) and i + 1 < len(lineas) and _SEPARADOR_DE_TABLA.match(lineas[i + 1]) and "|" in lineas[i + 1]:
            fin = i
            while fin < len(lineas) and _FILA_DE_TABLA.match(lineas[fin]):
                fin += 1
            tabla = _tabla(lineas[i:fin])
            if fin >= len(lineas) and tabla:  # la última fila no lleva salto de línea si es el final del texto
                texto_final, etiquetas = tabla[-1]
                tabla[-1] = (texto_final.rstrip("\n"), etiquetas)
            salida += tabla
            i = fin
            continue

        titulo = _TITULO.match(linea)
        if titulo:
            etiqueta = ETIQUETAS_DE_TITULO.get(len(titulo.group(1)), "h3")
            salida += _en_linea(titulo.group(2), (etiqueta,))
            salida.append((fin_de_linea, (etiqueta,)))
            i += 1
            continue

        if _REGLA.match(linea):
            salida.append(("─" * ANCHO_REGLA + fin_de_linea, ("regla",)))
            i += 1
            continue

        cita = _CITA.match(linea)
        if cita:
            salida.append(("│ ", ("cita_marca",)))
            salida += _en_linea(cita.group(1), ("cita",))
            salida.append((fin_de_linea, ("cita",)))
            i += 1
            continue

        item = _LISTA.match(linea)
        if item:
            nivel = min(3, len(item.group(1).replace("\t", "    ")) // 2)
            marca = "•" if item.group(2) in "-*+" else item.group(2)
            etiqueta = f"lista{nivel}"
            salida.append((f"{marca}\t", (etiqueta,)))
            salida += _en_linea(item.group(3), (etiqueta,))
            salida.append((fin_de_linea, (etiqueta,)))
            i += 1
            continue

        salida += _en_linea(linea)
        salida.append((fin_de_linea, ()))
        i += 1

    return [(_restaurar(t), etiquetas) for t, etiquetas in salida if t]


def analizar_formato(texto: str) -> list[Segmento]:
    """El texto como una lista de (trozo, etiquetas). Si algo falla, el texto entero sin formato."""
    try:
        return _analizar(texto)
    except Exception:
        return [(texto, ())] if texto else []


# ----------------------------------------------------------------------
# tkinter
# ----------------------------------------------------------------------


def preparar_etiquetas_de_formato(caja: tk.Text, p: Any, fuentes: Any) -> None:
    """Crea las etiquetas de formato en `caja` (una sola vez) a partir de las fuentes de la ventana."""
    if getattr(caja, "_formato_listo", False):
        return
    base: tkfont.Font = fuentes.texto

    def derivada(**cambios: Any) -> tkfont.Font:
        f = base.copy()
        f.configure(**cambios)
        return f

    tamano = abs(int(base.actual("size"))) or 11
    mono = tkfont.Font(root=caja, font=fuentes.mono)
    mono.configure(size=max(8, tamano - 1))
    mono_negrita = mono.copy()
    mono_negrita.configure(weight="bold")
    negrita = derivada(weight="bold")
    cursiva = derivada(slant="italic")
    negrita_cursiva = derivada(weight="bold", slant="italic")
    h1 = derivada(weight="bold", size=tamano + 6)
    h2 = derivada(weight="bold", size=tamano + 3)
    h3 = derivada(weight="bold", size=tamano + 1)
    # tkinter suelta las fuentes si nadie las sujeta: se guardan en la propia caja
    caja._fuentes_del_formato = [mono, mono_negrita, negrita, cursiva, negrita_cursiva, h1, h2, h3]  # type: ignore[attr-defined]
    caja.tag_configure("negrita", font=negrita)
    caja.tag_configure("cursiva", font=cursiva)
    caja.tag_configure("negrita_cursiva", font=negrita_cursiva)
    caja.tag_configure("h1", font=h1, spacing1=10, spacing3=6)
    caja.tag_configure("h2", font=h2, spacing1=8, spacing3=4)
    caja.tag_configure("h3", font=h3, spacing1=6, spacing3=2)
    caja.tag_configure("enlace", foreground=p.acento_texto, underline=True)
    caja.tag_configure("apagado", foreground=p.apagado)
    caja.tag_configure("cita", foreground=p.apagado, lmargin1=18, lmargin2=18, font=cursiva)
    caja.tag_configure("cita_marca", foreground=p.borde, lmargin1=6, lmargin2=18)
    caja.tag_configure("regla", foreground=p.borde, spacing1=4, spacing3=4)
    for nivel in range(4):
        sangria = 14 + nivel * 20
        caja.tag_configure(f"lista{nivel}", lmargin1=sangria, lmargin2=sangria + 16, tabs=(sangria + 16,))
    caja.tag_configure("tabla", font=mono, lmargin1=6, lmargin2=6)
    caja.tag_configure("tabla_cabecera", font=mono_negrita, lmargin1=6, lmargin2=6)
    fondo_codigo = p.codigo_fondo
    caja.tag_configure("codigo", font=mono, background=fondo_codigo)
    caja.tag_configure("bloque", font=mono, background=fondo_codigo, lmargin1=10, lmargin2=10, rmargin=8,
                       spacing1=1, spacing3=1)
    caja._formato_listo = True  # type: ignore[attr-defined]


def mostrar_texto_con_formato(caja: tk.Text, texto: str, p: Any, fuentes: Any, con_formato: bool = True) -> None:
    """Escribe `texto` en `caja` (que queda de solo lectura), con formato o tal cual."""
    caja.configure(state="normal")
    caja.delete("1.0", "end")
    try:
        if not con_formato:
            raise ValueError("sin formato")
        preparar_etiquetas_de_formato(caja, p, fuentes)
        for trozo, etiquetas in analizar_formato(texto):
            caja.insert("end", trozo, etiquetas)
    except Exception:
        caja.delete("1.0", "end")
        caja.insert("1.0", texto)
    caja.configure(state="disabled")
