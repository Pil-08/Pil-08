#!/usr/bin/env python3
"""Claude Batch Monitor 12.3.0: una cola de preguntas para Claude con tope de gasto.

ESTE ARCHIVO ES EL PROGRAMA COMPLETO. No hace falta nada más que Python.

  Cómo abrirlo
    1. Instala Python 3.9 o superior desde https://www.python.org/downloads/
       (Windows y macOS: deja marcada la casilla de tcl/tk. Debian/Ubuntu: sudo apt install python3-tk)
    2. Windows: doble clic en este archivo.   Otros sistemas, en una terminal:
           python3 claude_batch_monitor.py
    3. La primera vez ofrece instalar la única pieza externa que necesita, la librería oficial
       `anthropic` (un botón; en la terminal, responde "s"). No instala nada más.
    4. Pega tu clave de API, escribe tus preguntas y pulsa "Enviar". Si se acaba el saldo o se
       alcanza un límite, la cola espera y sigue sola; nunca gasta más que el tope que pongas.

  Terminal y servidores:   python3 claude_batch_monitor.py --cli --help

  In English
    One plain Python file: no packing, no obfuscation, no hidden steps. Install Python 3.9+, then
    double-click it on Windows or run `python3 claude_batch_monitor.py` elsewhere. On first run it
    offers to install the one library it needs (`anthropic`). Your questions go to the Claude API
    with a spending cap you choose; waiting and retrying is automatic.

  Librerías externas
    Obligatoria: `anthropic`, la oficial de Anthropic (si falta, el programa se ofrece a instalarla).
    Opcionales, el programa funciona sin ellas: `keyring` (la clave en el llavero del sistema),
    `pystray` y `Pillow` (icono en la bandeja), `plyer` (avisos de escritorio).
    Todo lo demás es la biblioteca estándar de Python.

  Qué hace en tu ordenador
    - Se conecta a api.anthropic.com con tu clave. Nada más, salvo avisos por Telegram si los activas
      (api.telegram.org) y la descarga de `anthropic` desde PyPI si aceptas instalarla.
    - Guarda su configuración, las respuestas y un registro en su propia carpeta de datos
      (subcarpeta ClaudeMonitor de %APPDATA% en Windows, ~/Library/Application Support en macOS
      y ~/.config en Linux). La clave va al llavero del sistema si hay `keyring`; nunca se
      escribe en archivos ni en el registro.
    - Evita que el equipo se suspenda mientras trabaja y abre carpetas o enlaces solo cuando se lo pides.

  Código fuente
    Este archivo se genera a partir de los módulos de `claude_batch_monitor/` con
    `python tools/build_single_file.py`. Para cambiar algo, edita los módulos y vuelve a generarlo.
    Licencia MIT.
"""

from __future__ import annotations

import argparse
import base64
import csv
import gc
import html
import importlib.util
import io
import json
import locale
import logging
import math
import os
import platform
import queue
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
import zlib
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import Decimal, InvalidOperation
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional, Union

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont
except ImportError:  # un Python sin tkinter: la terminal funciona y el arranque explica qué instalar

    class _VentanaSinTk:
        """Ocupa el sitio de una clase de tkinter cuando tkinter no está instalado."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError("Este Python no incluye tkinter: la interfaz gráfica no está disponible.")

    class _SinTk:
        """Ocupa el sitio del módulo tkinter: así el resto del archivo se puede cargar igualmente."""

        def __getattr__(self, nombre):
            return _VentanaSinTk

    tk = filedialog = tkfont = messagebox = ttk = _SinTk()

# ============================================================================
# ÍNDICE   (cada sección empieza con un título numerado: busca su número)
# ============================================================================
#    1. Carpetas de datos, registro de eventos y escritura atómica       línea   148
#    2. Trocear el texto del usuario en órdenes                          línea   280
#    3. Modelos, precios y coste                                         línea   315
#    4. Configuración                                                    línea   637
#    5. Estado guardado y almacén seguro de secretos                     línea   871
#    6. El lote: órdenes, estado de cada una y guardado en disco         línea   999
#    7. Leer órdenes desde texto, CSV, TSV y JSONL                       línea  1397
#    8. Respetar los límites de la API                                   línea  1593
#    9. Exportar resultados                                              línea  1755
#   10. Estimar el coste antes de gastar                                 línea  1930
#   11. Avisos: escritorio y Telegram                                    línea  2036
#   12. Sistema operativo: anti-suspensión y abrir carpetas              línea  2153
#   13. Una sola copia del programa gastando a la vez                    línea  2295
#   14. Motores: clasificar los errores de la API                        línea  2435
#   15. Motores: lo común (contabilidad, esperas, guardado y parada)     línea  2659
#   16. Motor inmediato (Messages API)                                   línea  3569
#   17. Motor de lote (Message Batches API, mitad de precio)             línea  3760
#   18. Clave de API y elección de modelo                                línea  4273
#   19. La cola del modo simple                                          línea  4388
#   20. Ejecutar un lote en segundo plano (modo avanzado)                línea  4687
#   21. Terminal (--cli)                                                 línea  4835
#   22. Interfaz: textos en español e inglés                             línea  5306
#   23. Interfaz: colores, tipografías y estilos                         línea  5697
#   24. Interfaz: el icono, dibujado con código                          línea  5892
#   25. Interfaz: ventanas y paneles comunes                             línea  5971
#   26. Interfaz: puente entre hilos                                     línea  6101
#   27. Interfaz: icono en la bandeja del sistema (opcional)             línea  6178
#   28. Interfaz: respuestas con formato                                 línea  6254
#   29. Interfaz avanzada: ventanas secundarias                          línea  6507
#   30. Modo simple                                                      línea  6723
#   31. Modo avanzado                                                    línea  7783
#   32. Abrir la interfaz                                                línea  9270
#   33. Instalar la librería de Anthropic si falta                       línea  9364
#   34. Arranque                                                         línea  9686
# ============================================================================
# Para leerlo en el orden en que se ejecuta: 34 (arranque), 30 (modo simple), 16 (motor inmediato).

# ============================================================================
# 1. Carpetas de datos, registro de eventos y escritura atómica
#    (claude_batch_monitor/paths.py)
# ============================================================================
#
# Rutas de datos, logging y escritura atómica.
#
# Todo el estado persistente del programa pasa por aquí. `write_atomic` es la
# única forma soportada de escribir a disco: escribe a un `.tmp` único y hace
# `replace()`, que es atómico en todos los SO soportados. Un corte a mitad de
# escritura deja el fichero anterior intacto en vez de uno truncado.
#
# Los demás módulos leen estas rutas (`RESPONSES_DIR`, `STATE_FILE`…) en el momento de usarlas, no al
# importar, de modo que los tests pueden redirigirlas todas con un único `monkeypatch`.


APP_NAME = "ClaudeMonitor"
APP_VERSION = "12.3.0"
__version__ = APP_VERSION

# Variable de entorno para guardar los datos en otro sitio (modo portátil, Docker,
# tests). Si no existe se usa el directorio estándar del SO.
ENV_HOME = "CLAUDE_MONITOR_HOME"


def _directorio_base() -> Path:
    override = os.getenv(ENV_HOME)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        # `or`, no el segundo argumento de getenv: una XDG_CONFIG_HOME vacía
        # daría Path("") y los datos acabarían en el directorio de trabajo.
        base = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_NAME


def get_data_dir() -> Path:
    """Directorio de datos por SO, creado si no existe.

    Si no se puede escribir (HOME de solo lectura en un contenedor, por ejemplo)
    cae a un directorio temporal en vez de impedir que el programa arranque.
    """
    path = _directorio_base()
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / "responses").mkdir(exist_ok=True)
    except OSError:
        path = Path(tempfile.gettempdir()) / APP_NAME
        path.mkdir(parents=True, exist_ok=True)
        (path / "responses").mkdir(exist_ok=True)
    return path


DATA_DIR = get_data_dir()
RESPONSES_DIR = DATA_DIR / "responses"
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "claude_monitor.log"


def _configurar_logger() -> logging.Logger:
    log = logging.getLogger(APP_NAME)
    log.setLevel(logging.INFO)
    if not log.handlers:
        try:
            handler: logging.Handler = RotatingFileHandler(
                LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
            )
        except OSError:
            handler = logging.NullHandler()
        log.addHandler(handler)
    return log


logger = _configurar_logger()


# Esperas entre intentos de `replace`: en Windows falla un instante si un antivirus o un indexador tiene abierto el destino.
_ESPERAS_AL_REEMPLAZAR = (0.05, 0.1, 0.2, 0.4)


def _reemplazar(origen: Path, destino: Path) -> None:
    for espera in (*_ESPERAS_AL_REEMPLAZAR, None):
        try:
            origen.replace(destino)
            return
        except PermissionError:
            if espera is None:
                raise
            time.sleep(espera)


_SUSTITUTOS_SUELTOS = re.compile("[\ud800-\udfff]")


def _a_utf8(contenido: str) -> bytes:
    """El texto como UTF-8 EXACTO (sin que Windows convierta `\\n` en `\\r\\n`) y sin fallar por un sustituto suelto."""
    try:
        return contenido.encode("utf-8")
    except UnicodeEncodeError:
        return _SUSTITUTOS_SUELTOS.sub("\ufffd", contenido).encode("utf-8")  # el mismo criterio que `limpiar_sustitutos`


def write_atomic(path: Path, content: str) -> None:
    """Escribe `content` en `path` sin dejar nunca un fichero a medias.

    El temporal lleva el PID y el hilo en el nombre: con concurrencia > 1, dos
    hilos guardando el mismo job a la vez usaban el MISMO `fichero.tmp`, uno
    hacía `replace()` del temporal que el otro aún estaba escribiendo y el
    segundo reventaba con `FileNotFoundError`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    marca = f"{os.getpid()}.{threading.get_ident()}"
    tmp = path.with_name(f"{path.name}.{marca}.tmp")
    try:
        tmp.write_bytes(_a_utf8(content))
        _reemplazar(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ============================================================================
# 2. Trocear el texto del usuario en órdenes
#    (claude_batch_monitor/parsing.py)
# ============================================================================
#
# Troceado del texto del usuario en órdenes independientes.


DEFAULT_SEPARATOR = "---"

_SUSTITUTOS = re.compile("[\ud800-\udfff]")


def limpiar_sustitutos(texto: str) -> str:
    """Cambia los «sustitutos» sueltos de UTF-16 por U+FFFD.

    Un emoji partido por la mitad (de un pegado raro, de un texto cortado en mal sitio) es una cadena que Python
    acepta pero que NO se puede escribir como UTF-8: sin esto, guardar la pregunta o la respuesta fallaba.
    """
    return _SUSTITUTOS.sub("\ufffd", texto) if texto else texto


def split_messages(text: str, separator: str = DEFAULT_SEPARATOR) -> list[str]:
    """Parte el texto en bloques por una LÍNEA que sea exactamente el separador.

    Predecible y sin colisión accidental: `----` (4 guiones) no parte si el
    separador es `---`. Un `re.split(r"-{3,}")` rompe el frontmatter y las
    reglas horizontales de Markdown dentro de una orden.
    """
    sep = (separator or "").strip() or DEFAULT_SEPARATOR
    pattern = rf"(?m)^[ \t]*{re.escape(sep)}[ \t]*$"
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # `$` solo reconoce `\n`: sin esto, un texto de Windows no se parte
    return [m.strip() for m in re.split(pattern, text) if m.strip()]


# ============================================================================
# 3. Modelos, precios y coste
#    (claude_batch_monitor/pricing.py)
# ============================================================================
#
# Catálogo de modelos, precios y cálculo de coste (siempre en `Decimal`).
#
# Los precios salen de https://platform.claude.com/docs/en/about-claude/pricing
# (revisado en octubre de 2026). CAMBIAN con el tiempo: por eso el usuario puede
# sobrescribirlos a mano, y por eso un modelo desconocido se valora con el precio
# CARO de su familia, nunca con el barato. Un tope de gasto calculado con precios
# demasiado bajos protege menos de lo que promete.
#
# Reglas de facturación que se modelan aquí:
# - Escritura de caché de 5 min: 1,25 x la entrada; de 1 h: 2 x.
# - Lectura de caché: 0,1 x la entrada (0,05 x en Opus 5.5; 0,025 x en Fable 5.1).
# - Message Batches API: 50 % de descuento en todo, y se acumula con la caché.


MILLON = Decimal(1_000_000)
_D = Decimal

ESCRITURA_CACHE_5M = _D("1.25")
ESCRITURA_CACHE_1H = _D("2")
DESCUENTO_LOTE = _D("0.5")

# Niveles de `output_config.effort` según el modelo.
_E5 = ("low", "medium", "high", "xhigh", "max")
_E4 = ("low", "medium", "high", "max")
_E3 = ("low", "medium", "high")
NIVELES_ESFUERZO = _E5


@dataclass(frozen=True)
class ModeloInfo:
    id: str
    nombre: str
    familia: str
    entrada: Decimal  # USD por millón de tokens
    salida: Decimal
    lectura_cache: Decimal = _D("0.1")  # multiplicador sobre `entrada`
    max_salida: Optional[int] = None
    contexto: Optional[int] = None
    min_cache: Optional[int] = None  # prefijo mínimo cacheable, en tokens
    esfuerzo: tuple = ()
    retirado: bool = False


def _modelo(id_, nombre, familia, e, s, **kw) -> ModeloInfo:
    return ModeloInfo(id_, nombre, familia, _D(str(e)), _D(str(s)), **kw)


_MODELOS = [
    _modelo("claude-fable-5-1", "Claude Fable 5.1", "fable", 10, 50, lectura_cache=_D("0.025"),
       max_salida=128_000, contexto=1_000_000, min_cache=512, esfuerzo=_E5),
    _modelo("claude-fable-5", "Claude Fable 5", "fable", 10, 50, esfuerzo=_E5),
    _modelo("claude-mythos-5-1", "Claude Mythos 5.1", "mythos", 10, 50, lectura_cache=_D("0.025"),
       min_cache=512, esfuerzo=_E5),
    _modelo("claude-mythos-5", "Claude Mythos 5", "mythos", 10, 50, esfuerzo=_E5),
    _modelo("claude-opus-5-5", "Claude Opus 5.5", "opus", 4, 20, lectura_cache=_D("0.05"),
       max_salida=128_000, contexto=1_000_000, min_cache=512, esfuerzo=_E5),
    _modelo("claude-opus-5", "Claude Opus 5", "opus", 5, 25, min_cache=512, esfuerzo=_E5),
    _modelo("claude-opus-4-8", "Claude Opus 4.8", "opus", 5, 25, min_cache=1024, esfuerzo=_E5),
    _modelo("claude-opus-4-7", "Claude Opus 4.7", "opus", 5, 25, min_cache=2048, esfuerzo=_E5),
    _modelo("claude-opus-4-6", "Claude Opus 4.6", "opus", 5, 25, min_cache=4096, esfuerzo=_E4),
    _modelo("claude-opus-4-5", "Claude Opus 4.5", "opus", 5, 25, min_cache=4096, esfuerzo=_E3),
    _modelo("claude-opus-4-1", "Claude Opus 4.1", "opus", 15, 75, retirado=True),
    _modelo("claude-opus-4", "Claude Opus 4", "opus", 15, 75, retirado=True),
    _modelo("claude-sonnet-5-5", "Claude Sonnet 5.5", "sonnet", 2, 10,
       max_salida=128_000, contexto=1_000_000, min_cache=512, esfuerzo=_E5),
    _modelo("claude-sonnet-5", "Claude Sonnet 5", "sonnet", 2, 10, min_cache=1024, esfuerzo=_E5),
    _modelo("claude-sonnet-4-6", "Claude Sonnet 4.6", "sonnet", 3, 15, min_cache=1024, esfuerzo=_E4),
    _modelo("claude-sonnet-4-5", "Claude Sonnet 4.5", "sonnet", 3, 15, min_cache=1024),
    _modelo("claude-sonnet-4", "Claude Sonnet 4", "sonnet", 3, 15, retirado=True),
    _modelo("claude-haiku-4-5", "Claude Haiku 4.5", "haiku", 1, 5,
       max_salida=64_000, contexto=200_000, min_cache=4096),
    _modelo("claude-haiku-3-5", "Claude Haiku 3.5", "haiku", "0.8", 4, retirado=True),
]

CATALOGO: dict[str, ModeloInfo] = {m.id: m for m in _MODELOS}

_ALIAS = {
    "claude-opus-4-0": "claude-opus-4",
    "claude-sonnet-4-0": "claude-sonnet-4",
    "claude-3-5-haiku": "claude-haiku-3-5",
}

# Precio por familia para modelos que aún no están en el catálogo: el del buque
# insignia actual de la familia, y para lo desconocido el más caro de todos.
_FAMILIA_CARA = {
    "fable": (_D(10), _D(50)),
    "mythos": (_D(10), _D(50)),
    "opus": (_D(5), _D(25)),
    "sonnet": (_D(3), _D(15)),
    "haiku": (_D(1), _D(5)),
}
_DESCONOCIDO = (_D(10), _D(50))

# Modelos que se ofrecen antes de consultar la API en vivo. El primero es el
# predeterminado: Haiku 4.5 es más barato, pero su retirada más temprana es el
# 15/10/2026 y Sonnet 5.5 tiene recorrido hasta, como mínimo, septiembre de 2027.
DEFAULT_MODEL = "claude-sonnet-5-5"
MODELOS_SUGERIDOS = [
    "claude-sonnet-5-5",
    "claude-haiku-4-5",
    "claude-opus-5-5",
    "claude-fable-5-1",
]

_FECHA_FINAL = re.compile(r"-\d{8}$")


def normalizar_id(modelo: str) -> str:
    """`claude-opus-4-5-20251101` -> `claude-opus-4-5`; quita fecha y `-latest`."""
    m = (modelo or "").strip().lower()
    if m.endswith("-latest"):
        m = m[: -len("-latest")]
    m = _FECHA_FINAL.sub("", m)
    return _ALIAS.get(m, m)


def buscar_modelo(modelo: str) -> Optional[ModeloInfo]:
    return CATALOGO.get(normalizar_id(modelo))


def familia_de(modelo: str) -> Optional[str]:
    info = buscar_modelo(modelo)
    if info:
        return info.familia
    nombre = (modelo or "").lower()
    for fam in _FAMILIA_CARA:
        if fam in nombre:
            return fam
    return None


@dataclass(frozen=True)
class Precios:
    entrada: Decimal
    salida: Decimal
    lectura_cache: Decimal
    origen: str  # catalogo | familia | desconocido | manual
    modelo: str = ""

    @property
    def fiable(self) -> bool:
        """True si el precio viene del catálogo o lo fijó el usuario."""
        return self.origen in ("catalogo", "manual")

    def aviso(self) -> str:
        if self.origen == "familia":
            return (f"'{self.modelo}' no está en el catálogo: se usa el precio de su familia "
                    f"({formatear_precio(self.entrada)} / {formatear_precio(self.salida)} por millón). "
                    "Compruébalo y ajústalo a mano si hace falta.")
        if self.origen == "desconocido":
            return (f"Modelo desconocido '{self.modelo}': se asume el precio más alto "
                    f"({formatear_precio(self.entrada)} / {formatear_precio(self.salida)} por millón) "
                    "para que el tope de gasto no se quede corto.")
        return ""


def resolver_precios(
    modelo: str,
    entrada: Optional[Decimal] = None,
    salida: Optional[Decimal] = None,
) -> Precios:
    """Precios efectivos: lo fijado a mano manda; si no, catálogo; si no, familia."""
    info = buscar_modelo(modelo)
    if info:
        base = Precios(info.entrada, info.salida, info.lectura_cache, "catalogo", modelo)
    else:
        fam = familia_de(modelo)
        if fam:
            e, s = _FAMILIA_CARA[fam]
            base = Precios(e, s, _D("0.1"), "familia", modelo)
        else:
            e, s = _DESCONOCIDO
            base = Precios(e, s, _D("0.1"), "desconocido", modelo)
    if entrada is None and salida is None:
        return base
    return Precios(
        _D(entrada) if entrada is not None else base.entrada,
        _D(salida) if salida is not None else base.salida,
        base.lectura_cache,
        "manual",
        modelo,
    )


def soporta_esfuerzo(modelo: str, nivel: Optional[str] = None) -> Optional[bool]:
    """True/False si se sabe; None si el modelo no está en el catálogo."""
    info = buscar_modelo(modelo)
    if info is None:
        return None
    if nivel is None:
        return bool(info.esfuerzo)
    return nivel in info.esfuerzo


# ----------------------------------------------------------------------
# Coste
# ----------------------------------------------------------------------


def _contador(obj: Any, nombre: str) -> int:
    """Lee un contador de tokens de un objeto `usage` sin fiarse de que exista."""
    valor = getattr(obj, nombre, 0)
    if isinstance(valor, str):
        try:
            valor = int(valor.strip())  # un intermediario que convierte los números en texto: se cuenta igual, no como cero
        except ValueError:
            return 0
    if isinstance(valor, bool) or not isinstance(valor, (int, float, Decimal)):
        return 0
    if (isinstance(valor, float) and not math.isfinite(valor)) or (isinstance(valor, Decimal) and not valor.is_finite()):
        return 0
    return max(0, int(valor))


@dataclass(frozen=True)
class Consumo:
    """Tokens de una respuesta, ya desglosados por tipo de facturación."""

    entrada: int = 0  # sin caché
    salida: int = 0
    escritura_5m: int = 0
    escritura_1h: int = 0
    lectura_cache: int = 0

    @property
    def entrada_total(self) -> int:
        return self.entrada + self.escritura_5m + self.escritura_1h + self.lectura_cache


def leer_consumo(uso: Any) -> Consumo:
    """Convierte el `usage` de la API (o un doble de test) en un `Consumo`."""
    if uso is None:
        return Consumo()
    escritura = _contador(uso, "cache_creation_input_tokens")
    desglose = getattr(uso, "cache_creation", None)
    w5 = _contador(desglose, "ephemeral_5m_input_tokens") if desglose is not None else 0
    w1 = _contador(desglose, "ephemeral_1h_input_tokens") if desglose is not None else 0
    if w5 + w1 < escritura:
        w5 += escritura - (w5 + w1)  # sin desglose: se asume TTL de 5 minutos
    return Consumo(
        entrada=_contador(uso, "input_tokens"),
        salida=_contador(uso, "output_tokens"),
        escritura_5m=w5,
        escritura_1h=w1,
        lectura_cache=_contador(uso, "cache_read_input_tokens"),
    )


def coste_consumo(consumo: Consumo, precios: Precios, lote: bool = False) -> Decimal:
    e, s = precios.entrada, precios.salida
    total = (
        _D(consumo.entrada) * e
        + _D(consumo.escritura_5m) * e * ESCRITURA_CACHE_5M
        + _D(consumo.escritura_1h) * e * ESCRITURA_CACHE_1H
        + _D(consumo.lectura_cache) * e * precios.lectura_cache
        + _D(consumo.salida) * s
    ) / MILLON
    return total * DESCUENTO_LOTE if lote else total


def coste_uso(uso: Any, precios: Precios, lote: bool = False) -> Decimal:
    return coste_consumo(leer_consumo(uso), precios, lote)


def coste_maximo(
    tokens_entrada: int,
    max_tokens: int,
    precios: Precios,
    lote: bool = False,
    mult_entrada: Decimal = _D(1),
) -> Decimal:
    """Peor caso de UNA petición: toda la entrada a precio completo y `max_tokens` de salida.

    `mult_entrada` > 1 cubre la primera escritura de caché (1,25 x), que es más
    cara que no cachear.
    """
    total = (
        _D(max(0, tokens_entrada)) * precios.entrada * mult_entrada
        + _D(max(0, max_tokens)) * precios.salida
    ) / MILLON
    return total * DESCUENTO_LOTE if lote else total


def estimar_tokens(texto: str) -> int:
    """Cota SUPERIOR barata de los tokens de un texto, sin llamar a la API.

    ~3 caracteres ASCII por token y 1 token por carácter no ASCII (CJK, emoji).
    Los modelos desde Opus 4.7 usan un tokenizador que produce ~30 % más tokens
    que el anterior, así que la regla clásica de 4 caracteres por token se queda
    corta. Para proteger un presupuesto es mejor pasarse que no llegar.
    """
    if not texto:
        return 0
    ascii_chars = sum(1 for c in texto if ord(c) < 128)
    otros = len(texto) - ascii_chars
    return int(ascii_chars / 3 + otros) + 8


def formatear_usd(valor: Decimal, decimales: int = 4) -> str:
    return f"${_D(valor):.{decimales}f}"


def formatear_precio(valor: Decimal) -> str:
    """`$2` o `$0.80` (hasta 3 decimales, sin ceros sobrantes)."""
    texto = f"{_D(valor):.3f}".rstrip("0").rstrip(".")
    if "." in texto and len(texto.split(".")[1]) < 2:
        texto += "0"
    return f"${texto}"


def descripcion_modelo(modelo: str) -> str:
    info = buscar_modelo(modelo)
    p = resolver_precios(modelo)
    nombre = info.nombre if info else modelo
    return f"{nombre}: {formatear_precio(p.entrada)} entrada / {formatear_precio(p.salida)} salida por millón"


# ============================================================================
# 4. Configuración
#    (claude_batch_monitor/config.py)
# ============================================================================
#
# Configuración del usuario: modelo, tope de gasto, tiempos… con validación central.
#
# Todo el dinero va en `Decimal`. Con `float`, 0.1 + 0.2 != 0.3 y un contador de
# gasto que se acumula miles de veces acaba con errores de céntimos justo en la
# cifra que decide si se para el lote.


MODOS = ("inmediato", "lote")
INTERFACES = ("simple", "avanzada")
TEMAS = ("auto", "claro", "oscuro")  # auto = el del sistema
MAX_TOKENS_STREAMING = 16_000  # por encima, el SDK exige streaming
MAX_ERROR_ATTEMPTS = 8
DELAY_BETWEEN_CYCLES_SEC = 10
DEFAULT_API_TIMEOUT_SEC = 300


def a_decimal(valor: Any, defecto: Decimal) -> Decimal:
    """Convierte lo que haya en el JSON (str, int, float) a Decimal sin arrastrar ruido binario."""
    if valor is None or isinstance(valor, bool):
        return defecto
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return defecto
    return d if d.is_finite() else defecto


def _int(valor: Any, defecto: int, minimo: int, maximo: int) -> int:
    try:
        return max(minimo, min(maximo, int(valor)))
    except (TypeError, ValueError, OverflowError):  # (OverflowError: `Infinity`, que Python sí lee en un JSON)
        return defecto


@dataclass
class Validacion:
    errores: list = field(default_factory=list)  # impiden ejecutar
    avisos: list = field(default_factory=list)   # se muestran pero no bloquean

    @property
    def ok(self) -> bool:
        return not self.errores


@dataclass
class Configuracion:
    mensaje: str = ""
    plantilla: str = ""  # envuelve cada orden; `{input}` marca el hueco (ver inputs.aplicar_plantilla)
    modelo: str = DEFAULT_MODEL
    separador: str = DEFAULT_SEPARATOR
    modo: str = "inmediato"  # inmediato | lote (Batches API, -50 %)
    system_prompt: str = ""
    usar_cache: bool = True
    esfuerzo: str = ""  # "" = no enviarlo; low | medium | high | xhigh | max
    max_tokens: int = 16_000
    concurrencia: int = 1
    streaming: bool = False  # mostrar la respuesta según se escribe

    # --- Blindaje de gasto ---
    tope_gasto_usd: Decimal = Decimal("5")  # 0 = sin tope
    # None = usar el catálogo. Un valor fijado a mano manda sobre el catálogo.
    precio_input_mtok: Optional[Decimal] = None
    precio_output_mtok: Optional[Decimal] = None
    reset_gasto_por_lote: bool = True
    estimar_antes_de_enviar: bool = True

    # --- Tiempos ---
    modo_continuo: bool = False  # repetir el lote en bucle (¡gasta!)
    hora_limite: str = ""        # "" = sin hora límite (en bucle se aplica 23:59)
    pausa_ciclos_sec: int = DELAY_BETWEEN_CYCLES_SEC
    pausa_entre_mensajes_sec: int = 0
    timeout_api_sec: int = DEFAULT_API_TIMEOUT_SEC
    max_error_attempts: int = MAX_ERROR_ATTEMPTS

    # --- Aplicación ---
    telegram_chat_id: str = ""
    auto_arranque: bool = False
    iniciar_minimizado: bool = False  # abrir directamente en la bandeja del sistema
    retencion_dias: int = 30
    interfaz: str = "simple"  # simple | avanzada
    mostrar_formato: bool = True  # respuestas con negrita, listas, código… (el texto original no cambia)
    idioma: str = ""          # "" = el del sistema
    tema: str = "auto"        # auto (el del sistema) | claro | oscuro

    # ------------------------------------------------------------------

    @property
    def usa_streaming(self) -> bool:
        return self.streaming or self.max_tokens > MAX_TOKENS_STREAMING

    @property
    def es_lote(self) -> bool:
        return self.modo == "lote"

    def precios(self) -> Precios:
        return resolver_precios(self.modelo, self.precio_input_mtok, self.precio_output_mtok)

    def validar(self) -> Validacion:
        v = Validacion()
        if not self.modelo.strip():
            v.errores.append("Falta el modelo.")
        if self.modo not in MODOS:
            v.errores.append(f"Modo desconocido '{self.modo}'. Usa: {', '.join(MODOS)}.")
        if self.max_tokens < 1:
            v.errores.append("max_tokens debe ser al menos 1.")
        if self.tope_gasto_usd < 0:
            v.errores.append("El tope de gasto no puede ser negativo.")
        if self.concurrencia < 1:
            v.errores.append("La concurrencia debe ser al menos 1.")
        if self.esfuerzo and self.esfuerzo not in NIVELES_ESFUERZO:
            v.errores.append(
                f"Nivel de esfuerzo desconocido '{self.esfuerzo}'. Usa: {', '.join(NIVELES_ESFUERZO)}."
            )

        info = buscar_modelo(self.modelo)
        if info and info.max_salida and self.max_tokens > info.max_salida:
            v.errores.append(
                f"{info.nombre} admite como máximo {info.max_salida:,} tokens de salida "
                f"(has pedido {self.max_tokens:,})."
            )
        if info and info.retirado:
            v.avisos.append(f"{info.nombre} está retirado o en retirada: puede dejar de funcionar.")
        if self.esfuerzo and soporta_esfuerzo(self.modelo, self.esfuerzo) is False:
            v.avisos.append(
                f"{self.modelo} no admite el esfuerzo '{self.esfuerzo}': se ignorará al enviar."
            )

        aviso_precio = self.precios().aviso()
        if aviso_precio:
            v.avisos.append(aviso_precio)
        if self.tope_gasto_usd == 0:
            v.avisos.append("Sin tope de gasto: nada impedirá que el lote consuma todo tu saldo.")
        if self.modo_continuo:
            v.avisos.append("Modo bucle: el lote se repetirá hasta la hora límite o el tope de gasto.")
        if self.es_lote and self.concurrencia > 1:
            v.avisos.append("La concurrencia no aplica al modo lote (la API ya lo procesa en paralelo).")
        return v

    # ------------------------------------------------------------------

    def a_dict(self) -> dict:
        d = asdict(self)
        for clave in ("tope_gasto_usd", "precio_input_mtok", "precio_output_mtok"):
            d[clave] = None if d[clave] is None else str(d[clave])
        return d

    @classmethod
    def desde_dict(cls, data: dict) -> Configuracion:
        """Construye una configuración válida a partir de cualquier JSON (incluido el de la v11)."""
        base = cls()

        def texto(clave: str, defecto: str) -> str:
            valor = data.get(clave, defecto)
            return defecto if valor is None else str(valor)

        def precio(clave: str) -> Optional[Decimal]:
            if data.get(clave) is None:
                return None
            d = a_decimal(data.get(clave), Decimal(-1))
            return d if d >= 0 else None

        modo = texto("modo", base.modo)
        interfaz = texto("interfaz", base.interfaz)
        tema = texto("tema", base.tema).strip().lower()
        esfuerzo = texto("esfuerzo", "")
        return cls(
            mensaje=texto("mensaje", ""),
            plantilla=texto("plantilla", ""),
            modelo=texto("modelo", base.modelo).strip() or base.modelo,
            separador=texto("separador", base.separador).strip() or base.separador,
            modo=modo if modo in MODOS else base.modo,
            system_prompt=texto("system_prompt", ""),
            usar_cache=bool(data.get("usar_cache", base.usar_cache)),
            esfuerzo=esfuerzo if esfuerzo in NIVELES_ESFUERZO else "",
            max_tokens=_int(data.get("max_tokens"), base.max_tokens, 1, 1_000_000),
            concurrencia=_int(data.get("concurrencia"), 1, 1, 32),
            streaming=bool(data.get("streaming", False)),
            tope_gasto_usd=max(Decimal(0), a_decimal(data.get("tope_gasto_usd"), base.tope_gasto_usd)),
            precio_input_mtok=precio("precio_input_mtok"),
            precio_output_mtok=precio("precio_output_mtok"),
            reset_gasto_por_lote=bool(data.get("reset_gasto_por_lote", True)),
            estimar_antes_de_enviar=bool(data.get("estimar_antes_de_enviar", True)),
            modo_continuo=bool(data.get("modo_continuo", False)),
            hora_limite=texto("hora_limite", "").strip(),
            pausa_ciclos_sec=_int(data.get("pausa_ciclos_sec"), DELAY_BETWEEN_CYCLES_SEC, 1, 3600),
            pausa_entre_mensajes_sec=_int(data.get("pausa_entre_mensajes_sec"), 0, 0, 60),
            timeout_api_sec=_int(data.get("timeout_api_sec"), DEFAULT_API_TIMEOUT_SEC, 15, 900),
            max_error_attempts=_int(data.get("max_error_attempts"), MAX_ERROR_ATTEMPTS, 1, 100),
            telegram_chat_id=texto("telegram_chat_id", ""),
            auto_arranque=bool(data.get("auto_arranque", False)),
            iniciar_minimizado=bool(data.get("iniciar_minimizado", False)),
            retencion_dias=_int(data.get("retencion_dias"), 30, 0, 3650),
            interfaz=interfaz if interfaz in INTERFACES else base.interfaz,
            mostrar_formato=bool(data.get("mostrar_formato", True)),
            idioma=texto("idioma", "").strip().lower()[:5].strip(),
            tema=tema if tema in TEMAS else base.tema,
        )


class ConfigManager:
    """Lectura y escritura de `config.json`. La clave de API NO se guarda aquí jamás."""

    @staticmethod
    def cargar(path: Optional[Path] = None) -> Configuracion:
        path = path or CONFIG_FILE
        if not path.exists():
            return Configuracion()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config.json no es un objeto")
            return Configuracion.desde_dict(data)
        except Exception:
            logger.exception("config.json ilegible; se aparta como .corrupt.json")
            try:
                path.replace(path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return Configuracion()

    @staticmethod
    def guardar(config: Configuracion, path: Optional[Path] = None) -> None:
        path = path or CONFIG_FILE
        try:
            write_atomic(path, json.dumps(config.a_dict(), indent=2, ensure_ascii=False))
        except Exception:
            logger.exception("No se pudo guardar la configuración")


# ============================================================================
# 5. Estado guardado y almacén seguro de secretos
#    (claude_batch_monitor/state.py)
# ============================================================================
#
# Estado persistente (gasto acumulado, lote en curso) y almacén seguro de secretos.


try:  # opcional: sin keyring la clave solo puede venir del entorno
    import keyring

    HAS_KEYRING = True
except ImportError:  # pragma: no cover - depende del entorno
    keyring = None  # type: ignore[assignment]
    HAS_KEYRING = False

SERVICE_NAME = "claude_monitor"
KEY_API = "api_key"
KEY_TELEGRAM_TOKEN = "telegram_token"
KEY_TELEGRAM_CHAT_ID = "telegram_chat_id"


@dataclass
class EstadoMonitor:
    exitos: int = 0
    ultimo_exito: str = ""
    current_job_id: Optional[str] = None
    gasto_acumulado_usd: Decimal = Decimal(0)
    tokens_input_total: int = 0
    tokens_output_total: int = 0

    def guardar(self, path: Optional[Path] = None) -> bool:
        """Escribe el estado. False si no se pudo (disco lleno, sin permisos…): quien gasta debe enterarse."""
        path = path or STATE_FILE
        datos = {
            "exitos": self.exitos,
            "ultimo_exito": self.ultimo_exito,
            "current_job_id": self.current_job_id,
            "gasto_acumulado_usd": str(self.gasto_acumulado_usd),
            "tokens_input_total": self.tokens_input_total,
            "tokens_output_total": self.tokens_output_total,
        }
        try:
            write_atomic(path, json.dumps(datos, indent=2, ensure_ascii=False))
        except Exception:
            logger.exception("No se pudo guardar el estado")
            return False
        return True

    @classmethod
    def cargar(cls, path: Optional[Path] = None) -> EstadoMonitor:
        path = path or STATE_FILE
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            exitos = data.get("exitos", 0)
            if not isinstance(exitos, int) or exitos < 0:
                exitos = 0
            job_id = data.get("current_job_id")
            return cls(
                exitos=exitos,
                ultimo_exito=str(data.get("ultimo_exito", "")),
                current_job_id=str(job_id) if job_id else None,
                gasto_acumulado_usd=max(Decimal(0), a_decimal(data.get("gasto_acumulado_usd"), Decimal(0))),
                tokens_input_total=max(0, int(data.get("tokens_input_total", 0))),
                tokens_output_total=max(0, int(data.get("tokens_output_total", 0))),
            )
        except Exception:
            logger.exception("state.json ilegible; se aparta como .corrupt.json")
            try:
                path.replace(path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return cls()


class SecureStorage:
    """Secretos en el llavero del sistema operativo (nunca en config.json)."""

    @staticmethod
    def disponible() -> bool:
        return HAS_KEYRING

    @staticmethod
    def get(key: str) -> Optional[str]:
        if not HAS_KEYRING:
            return None
        try:
            return keyring.get_password(SERVICE_NAME, key)
        except Exception:
            return None

    @staticmethod
    def set(key: str, value: str) -> bool:
        if not HAS_KEYRING or not value:
            return False
        try:
            keyring.set_password(SERVICE_NAME, key, value)
            return True
        except Exception:
            return False

    @staticmethod
    def delete(key: str) -> bool:
        if not HAS_KEYRING:
            return False
        try:
            keyring.delete_password(SERVICE_NAME, key)
            return True
        except Exception:
            return False


def obtener_api_key() -> Optional[str]:
    """Orden de prioridad: variable de entorno, luego llavero."""
    return (os.getenv("ANTHROPIC_API_KEY") or "").strip() or SecureStorage.get(KEY_API)


def origen_api_key() -> str:
    """'entorno' | 'llavero' | '' — para mostrar al usuario de dónde sale su clave."""
    if (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        return "entorno"
    if SecureStorage.get(KEY_API):
        return "llavero"
    return ""


# ============================================================================
# 6. El lote: órdenes, estado de cada una y guardado en disco
#    (claude_batch_monitor/job.py)
# ============================================================================
#
# El lote de trabajo: órdenes, estado por orden y persistencia en disco.
#
# Cada lote es un `job_<id>.json` en el directorio de respuestas. Es lo que hace
# que cerrar la aplicación a mitad no pierda nada: al volver a abrirla se reanuda
# solo lo pendiente, sin repetir (ni volver a pagar) lo ya hecho.


STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_REFUSED = "refused"  # el modelo se negó a responder (HTTP 200, pero se cobra)
STATUS_SKIPPED = "skipped"  # omitida a petición del usuario
TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_REFUSED, STATUS_SKIPPED}

_ID_JOB = re.compile(r"^job_([0-9a-f]+)(?:_|\.)")


@dataclass
class JobMessage:
    id: int
    text: str
    status: str = STATUS_PENDING
    errores: int = 0  # solo errores reales de servidor/red; los 429 NO cuentan
    truncated: bool = False
    tokens_input: int = 0  # entrada total, incluida la leída de caché
    tokens_output: int = 0
    tokens_cache_read: int = 0
    coste_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    detalle: str = ""
    error_type: str = ""  # categoría legible por máquina: refusal, auth, server…

    def __post_init__(self) -> None:
        self.text = limpiar_sustitutos(self.text)

    def a_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "status": self.status,
            "errores": self.errores,
            "truncated": self.truncated,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "tokens_cache_read": self.tokens_cache_read,
            "coste_usd": str(self.coste_usd),
            "latency_ms": self.latency_ms,
            "detalle": self.detalle,
            "error_type": self.error_type,
        }

    @classmethod
    def desde_dict(cls, d: dict) -> JobMessage:
        def entero(clave: str) -> int:
            try:
                return max(0, int(d.get(clave, 0)))
            except (TypeError, ValueError):
                return 0

        return cls(
            id=int(d["id"]),
            text=str(d["text"]),
            status=str(d.get("status", STATUS_PENDING)),
            # La v11 guardaba el contador como "attempts".
            errores=max(0, int(d.get("errores", d.get("attempts", 0)) or 0)),
            truncated=bool(d.get("truncated", False)),
            tokens_input=entero("tokens_input"),
            tokens_output=entero("tokens_output"),
            tokens_cache_read=entero("tokens_cache_read"),
            coste_usd=a_decimal(d.get("coste_usd"), Decimal(0)),
            latency_ms=entero("latency_ms"),
            detalle=str(d.get("detalle", "")),
            error_type=str(d.get("error_type", "")),
        )


class Job:
    def __init__(
        self,
        job_id: str,
        created_at: str,
        modelo: str,
        mensajes: Iterable[str],
        separador: str = DEFAULT_SEPARATOR,
        modo: str = "inmediato",
        system_prompt: str = "",
        batch_id: str = "",
        etiqueta: str = "",
    ):
        self.job_id = job_id
        self.created_at = created_at
        self.modelo = modelo
        self.separador = separador
        self.modo = modo
        self.system_prompt = system_prompt
        self.batch_id = batch_id  # id del lote en el servidor (modo lote)
        # Un envío de lote que pudo llegar al servidor sin que nos llegara su respuesta (tiempo agotado, corte, programa
        # cerrado a mitad): desde cuándo y cuántas órdenes. Mientras esté puesto, antes de enviar se busca ese lote.
        self.envio_incierto_desde = ""
        self.envio_incierto_ordenes = 0
        # El lote más reciente de la cuenta justo antes de intentarlo ("" = ninguno; None = no se pudo mirar): lo que
        # aparezca por delante en la lista de lotes lo creó el envío (o alguien más), no es anterior.
        self.envio_incierto_tras: Optional[str] = None
        # Respuestas ya pagadas que no se pudieron escribir en disco (disco lleno, sin permisos…): se guardan en
        # memoria para poder verlas, copiarlas o exportarlas mientras el programa siga abierto. No se persisten.
        self.respuestas_sin_guardar: dict[int, tuple[int, str]] = {}  # orden -> (ciclo, contenido del fichero)
        self.etiqueta = etiqueta  # "cola" = la cola del modo simple; "" = un lote normal
        self.ciclos_completados = 0
        self.messages: list[JobMessage] = [
            JobMessage(id=i + 1, text=m) for i, m in enumerate(mensajes)
        ]
        # Con concurrencia > 1 varios hilos guardan el mismo job; sin este cerrojo
        # dos `save()` podían mezclar dos instantes distintos en el mismo fichero.
        self._save_lock = threading.Lock()
        self._ids_lock = threading.Lock()

    @classmethod
    def crear(cls, mensajes: Iterable[str], config: Configuracion, etiqueta: str = "") -> Job:
        return cls(
            uuid.uuid4().hex[:8],
            datetime.now().isoformat(),
            config.modelo,
            mensajes,
            separador=config.separador,
            modo=config.modo,
            system_prompt=config.system_prompt,
            etiqueta=etiqueta,
        )

    # ---------- rutas ----------

    @property
    def file_path(self) -> Path:
        return RESPONSES_DIR / f"job_{self.job_id}.json"

    def response_path(self, msg_id: int, ciclo: int) -> Path:
        return RESPONSES_DIR / f"job_{self.job_id}_c{ciclo}_m{msg_id:03d}.txt"

    def resumen_path(self, ciclo: int) -> Path:
        return RESPONSES_DIR / f"job_{self.job_id}_ciclo_{ciclo}_resumen.md"

    # ---------- persistencia ----------

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "created_at": self.created_at,
            "modelo": self.modelo,
            "separador": self.separador,
            "modo": self.modo,
            "system_prompt": self.system_prompt,
            "batch_id": self.batch_id,
            "envio_incierto_desde": self.envio_incierto_desde,
            "envio_incierto_ordenes": self.envio_incierto_ordenes,
            "envio_incierto_tras": self.envio_incierto_tras,
            "etiqueta": self.etiqueta,
            "ciclos_completados": self.ciclos_completados,
            "messages": [m.a_dict() for m in list(self.messages)],
        }

    def save(self) -> bool:
        """Escribe el lote en disco. False si no se pudo: quien gasta debe enterarse."""
        with self._save_lock:
            try:
                write_atomic(
                    self.file_path, json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
                )
            except Exception:
                logger.exception("No se pudo guardar el Job %s", self.job_id)
                return False
            return True

    @classmethod
    def load(cls, job_id: str) -> Optional[Job]:
        path = RESPONSES_DIR / f"job_{job_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            job = cls(
                data["job_id"],
                data["created_at"],
                data["modelo"],
                [],
                separador=data.get("separador", DEFAULT_SEPARATOR),
                modo=data.get("modo", "inmediato"),
                system_prompt=data.get("system_prompt", ""),
                batch_id=data.get("batch_id", "") or "",
                etiqueta=data.get("etiqueta", "") or "",
            )
            job.ciclos_completados = int(data.get("ciclos_completados", 0))
            job.envio_incierto_desde = str(data.get("envio_incierto_desde", "") or "")
            job.envio_incierto_ordenes = max(0, int(data.get("envio_incierto_ordenes", 0) or 0))
            tras = data.get("envio_incierto_tras")
            job.envio_incierto_tras = None if tras is None else str(tras)
            job.messages = [JobMessage.desde_dict(m) for m in data["messages"]]
            return job
        except Exception:
            logger.exception("No se pudo cargar el Job %s", job_id)
            return None

    def agregar_orden(self, texto: str) -> JobMessage:
        """Añade una orden nueva al final (la cola del modo simple crece mientras se envía)."""
        with self._ids_lock:
            nuevo = JobMessage(id=max((m.id for m in self.messages), default=0) + 1, text=texto)
            self.messages.append(nuevo)
        self.save()
        return nuevo

    # ---------- consultas ----------

    def mensaje(self, msg_id: int) -> Optional[JobMessage]:
        for m in self.messages:
            if m.id == msg_id:
                return m
        return None

    def completed_count(self) -> int:
        return sum(1 for m in self.messages if m.status == STATUS_COMPLETED)

    def total_count(self) -> int:
        return len(self.messages)

    def pendientes(self) -> list[JobMessage]:
        """Órdenes que aún hay que enviar (las fallidas se reintentan a propósito)."""
        return [m for m in self.messages if m.status == STATUS_PENDING]

    def fallidas(self) -> list[JobMessage]:
        return [m for m in self.messages if m.status == STATUS_FAILED]

    def rechazadas(self) -> list[JobMessage]:
        return [m for m in self.messages if m.status == STATUS_REFUSED]

    def terminado(self) -> bool:
        return all(m.status in TERMINAL_STATUSES for m in self.messages)

    def hay_trabajo(self) -> bool:
        """True si queda algo por enviar o reintentar."""
        return any(m.status not in TERMINAL_STATUSES for m in self.messages)

    def coste_total(self) -> Decimal:
        return sum((m.coste_usd for m in self.messages), Decimal(0))

    def estadisticas(self) -> dict[str, Any]:
        return {
            "total": self.total_count(),
            "completadas": self.completed_count(),
            "pendientes": len(self.pendientes()),
            "fallidas": len(self.fallidas()),
            "rechazadas": len(self.rechazadas()),
            "coste_usd": self.coste_total(),
        }

    # ---------- transiciones ----------

    def reintentar_fallidas(self) -> int:
        """Devuelve a pendiente las órdenes fallidas. Devuelve cuántas eran."""
        n = 0
        for m in self.messages:
            if m.status == STATUS_FAILED:
                m.status = STATUS_PENDING
                m.errores = 0
                m.detalle = ""
                m.error_type = ""
                n += 1
        if n:
            self.save()
        return n

    def reset_for_new_cycle(self) -> None:
        for m in self.messages:
            m.status = STATUS_PENDING
            m.errores = 0
            m.truncated = False
            m.detalle = ""
            m.error_type = ""
        self.batch_id = ""
        self.envio_incierto_desde = ""
        self.envio_incierto_ordenes = 0
        self.envio_incierto_tras = None
        self.ciclos_completados += 1
        self.save()

    # ---------- reanudación segura ----------

    def incompatibilidades(self, config: Configuracion) -> list[str]:
        """Avisos si al reanudar la configuración actual difiere de la del lote.

        Mezclar en un mismo lote respuestas de dos modelos distintos, o reenviar
        por la API normal (al doble de precio) un lote creado en modo lote, suele
        ser un accidente. Se avisa; la decisión es del usuario.
        """
        avisos: list[str] = []
        if normalizar_id(config.modelo) != normalizar_id(self.modelo):
            avisos.append(
                f"El lote se creó con el modelo {self.modelo} y ahora está seleccionado "
                f"{config.modelo}: las órdenes pendientes se responderán con otro modelo."
            )
        if config.modo != self.modo:
            extra = ""
            if self.modo == "lote" and config.modo == "inmediato":
                extra = " Se reenviaría por la API normal, al doble de precio."
            avisos.append(
                f"El lote se creó en modo '{self.modo}' y ahora está en modo '{config.modo}'.{extra}"
            )
        if (config.system_prompt or "").strip() != (self.system_prompt or "").strip():
            avisos.append("El prompt de sistema ha cambiado desde que se creó el lote.")
        return avisos


# ----------------------------------------------------------------------
# Utilidades sobre el conjunto de lotes guardados
# ----------------------------------------------------------------------


def listar_jobs(etiqueta: Optional[str] = None) -> list[dict]:
    """Resumen de los lotes en disco, el más reciente primero.

    Con `etiqueta` solo los de esa etiqueta (`""` = los lotes normales, `"cola"` = la
    cola del modo simple); sin ella, todos.
    """
    salida: list[dict] = []
    for f in RESPONSES_DIR.glob("job_*.json"):
        job = Job.load(f.stem[len("job_"):])
        if job is None or (etiqueta is not None and job.etiqueta != etiqueta):
            continue
        e = job.estadisticas()
        salida.append(
            {
                "job_id": job.job_id,
                "created_at": job.created_at,
                "modelo": job.modelo,
                "modo": job.modo,
                "batch_id": job.batch_id,
                "etiqueta": job.etiqueta,
                "completadas": e["completadas"],
                "total": e["total"],
                "fallidas": e["fallidas"],
                "coste_usd": e["coste_usd"],
                "incompleto": job.hay_trabajo(),
            }
        )
    salida.sort(key=lambda d: d["created_at"], reverse=True)
    return salida


def purgar_jobs_antiguos(
    dias: int, proteger: Optional[Iterable[str] | str] = None, conservar_pendientes: bool = True
) -> int:
    """Borra los ficheros de lotes más viejos que `dias` días. Devuelve cuántos.

    Nunca se tocan:
    - los lotes de `proteger` (ids), por viejos que sean: sin esto la purga se llevaba también los
      ficheros del lote en curso y un lote dejado a medias más días que la retención se volvía irreanudable;
    - con `conservar_pendientes` (por defecto), cualquier lote que aún tenga órdenes sin enviar,
      incluida la cola del modo simple: perder preguntas que nadie ha visto responder es peor que ocupar disco.
    """
    if dias <= 0:
        return 0
    if proteger is None:
        protegidos: set[str] = set()
    elif isinstance(proteger, str):
        protegidos = {proteger}
    else:
        protegidos = {str(p) for p in proteger}

    limite = time.time() - dias * 86400
    viejos: dict[str, list[Path]] = {}
    for f in RESPONSES_DIR.glob("job_*"):
        m = _ID_JOB.match(f.name)
        if not m or m.group(1) in protegidos:
            continue
        try:
            if f.stat().st_mtime < limite:
                viejos.setdefault(m.group(1), []).append(f)
        except OSError:
            logger.warning("No se pudo examinar %s", f)

    borrados = 0
    for job_id, ficheros in viejos.items():
        if conservar_pendientes:
            job = Job.load(job_id)
            if job is not None and job.pendientes():
                continue
        for f in ficheros:
            try:
                f.unlink()
                borrados += 1
            except OSError:
                logger.warning("No se pudo borrar %s", f)
    return borrados


# ============================================================================
# 7. Leer órdenes desde texto, CSV, TSV y JSONL
#    (claude_batch_monitor/inputs.py)
# ============================================================================
#
# Lectura de órdenes desde texto, CSV, TSV y JSONL.
#
# Lo normal es tener los datos en una hoja de cálculo, no en un `.txt` con
# separadores. Si la columna pedida no existe se falla AQUÍ, antes de gastar nada,
# listando las que sí hay: procesar filas vacías y darse cuenta después de pagar
# es el peor resultado posible.


CAMPO_POR_DEFECTO = "input"
PLACEHOLDER = "{input}"
EXTENSIONES_TABLA = {".csv", ".tsv"}
EXTENSIONES_JSONL = {".jsonl", ".ndjson"}
EXTENSIONES_SOPORTADAS = EXTENSIONES_TABLA | EXTENSIONES_JSONL | {".txt", ".md"}

# Celdas largas (un artículo entero en una celda) superan el límite por defecto de 128 KB.
csv.field_size_limit(10_000_000)


class ErrorDeEntrada(ValueError):
    """El fichero de órdenes no se puede usar; el mensaje dice por qué y cómo arreglarlo."""


def aplicar_plantilla(plantilla: str, texto: str) -> str:
    """Envuelve cada orden en un prompt común. `{input}` marca el hueco."""
    if not plantilla:
        return texto
    if PLACEHOLDER in plantilla:
        return plantilla.replace(PLACEHOLDER, texto)
    return f"{plantilla}\n\n{texto}"


# ----------------------------------------------------------------------


def _leer_texto(path: Path) -> str:
    if not path.is_file():
        raise ErrorDeEntrada(f"No existe el fichero: {path}")
    try:
        # utf-8-sig: Excel antepone una marca BOM al guardar "CSV UTF-8".
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise ErrorDeEntrada(
            f"{path.name} no está en UTF-8. Vuelve a guardarlo con esa codificación."
        ) from e


def _delimitador(path: Path, contenido: str) -> str:
    """Coma, punto y coma, tabulador o barra, según la cabecera.

    Excel en español guarda los "CSV" con punto y coma; asumir siempre la coma
    daría una única columna enorme.
    """
    if path.suffix.lower() == ".tsv":
        return "\t"
    cabecera = contenido.split("\n", 1)[0]
    mejor, cuenta = ",", 0
    for candidato in (",", ";", "\t", "|"):
        n = cabecera.count(candidato)
        if n > cuenta:
            mejor, cuenta = candidato, n
    return mejor


def _elegir_columna(
    nombre: str, campo: Optional[str], disponibles: list[str], tipo: str = "columna"
) -> str:
    """Resuelve qué columna usar, o falla diciendo cuáles hay."""
    if campo:
        if campo in disponibles:
            return campo
        minusculas = {c.strip().lower(): c for c in disponibles}
        if campo.strip().lower() in minusculas:
            return minusculas[campo.strip().lower()]
    else:
        if CAMPO_POR_DEFECTO in disponibles:
            return CAMPO_POR_DEFECTO
        if len(disponibles) == 1:  # un solo campo: no hay nada que elegir
            return disponibles[0]
    pedido = campo or CAMPO_POR_DEFECTO
    plural = "columnas" if tipo == "columna" else "campos"
    raise ErrorDeEntrada(
        f"{nombre} no tiene la {tipo} '{pedido}'. "
        f"{plural.capitalize()} disponibles: {', '.join(disponibles) or '(ninguna)'}. "
        "Indica cuál usar con --campo."
    )


def _a_texto(valor: Any) -> str:
    if valor is None:
        return ""
    if isinstance(valor, str):
        return valor.strip()
    if isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False)
    return str(valor).strip()


def _leer_tabla(path: Path, contenido: str, campo: Optional[str]) -> list[str]:
    lector = csv.DictReader(io.StringIO(contenido, newline=""), delimiter=_delimitador(path, contenido))
    columnas = [c for c in (lector.fieldnames or []) if c is not None]
    if not columnas:
        return []
    col = _elegir_columna(path.name, campo, [c.strip() for c in columnas])
    # DictReader indexa por el nombre tal cual viene (con espacios, si los hay).
    real = next(c for c in columnas if c.strip() == col)
    ordenes = []
    for fila in lector:
        valor = _a_texto(fila.get(real))
        if valor:
            ordenes.append(valor)
    return ordenes


def _leer_jsonl(path: Path, contenido: str, campo: Optional[str]) -> list[str]:
    ordenes: list[str] = []
    # Un JSONL separa las líneas SOLO por `\n`: `splitlines()` también parte por U+2028, U+0085 o \x0b, que pueden ir
    # dentro de un texto JSON sin escapar y lo dejarían cortado por la mitad.
    for n, linea in enumerate(contenido.split("\n"), start=1):
        if not linea.strip():
            continue
        try:
            obj = json.loads(linea)
        except json.JSONDecodeError as e:
            raise ErrorDeEntrada(f"{path.name}: JSON inválido en la línea {n}: {e.msg}.") from e
        if not isinstance(obj, dict):
            raise ErrorDeEntrada(f"{path.name}: la línea {n} no es un objeto JSON.")
        try:
            clave = _elegir_columna(path.name, campo, list(obj.keys()), tipo="campo")
        except ErrorDeEntrada as e:
            raise ErrorDeEntrada(f"{e} (línea {n})") from e
        valor = _a_texto(obj.get(clave))
        if valor:
            ordenes.append(valor)
    return ordenes


def listar_campos(origen: Union[str, Path]) -> Optional[list[str]]:
    """Nombres de columna de un CSV/TSV o de campo del primer objeto de un JSONL.

    Devuelve None para texto plano. Sirve a la interfaz para preguntar qué
    columna usar sin tener que fallar primero.
    """
    path = Path(origen)
    sufijo = path.suffix.lower()
    if sufijo not in EXTENSIONES_TABLA | EXTENSIONES_JSONL:
        return None
    contenido = _leer_texto(path)
    if sufijo in EXTENSIONES_TABLA:
        lector = csv.reader(io.StringIO(contenido, newline=""), delimiter=_delimitador(path, contenido))
        cabecera = next(lector, [])
        return [c.strip() for c in cabecera if c.strip()]
    for linea in contenido.split("\n"):
        if linea.strip():
            try:
                obj = json.loads(linea)
            except json.JSONDecodeError:
                return []
            return list(obj.keys()) if isinstance(obj, dict) else []
    return []


def leer_ordenes(
    origen: Union[str, Path],
    *,
    campo: Optional[str] = None,
    separador: str = DEFAULT_SEPARATOR,
    plantilla: str = "",
    limite: Optional[int] = None,
) -> list[str]:
    """Lee las órdenes de un fichero según su extensión.

    `limite` corta a las N primeras: es la prueba de humo para validar un prompt
    con 2 filas antes de lanzar 5000.
    """
    path = Path(origen)
    contenido = _leer_texto(path)
    sufijo = path.suffix.lower()
    if sufijo in EXTENSIONES_TABLA:
        ordenes = _leer_tabla(path, contenido, campo)
    elif sufijo in EXTENSIONES_JSONL:
        ordenes = _leer_jsonl(path, contenido, campo)
    else:
        ordenes = split_messages(contenido, separador)

    if not ordenes:
        raise ErrorDeEntrada(f"{path.name} no contiene ninguna orden utilizable.")
    if limite and limite > 0:
        ordenes = ordenes[:limite]
    return [aplicar_plantilla(plantilla, o) for o in ordenes]


# ============================================================================
# 8. Respetar los límites de la API
#    (claude_batch_monitor/ratelimit.py)
# ============================================================================
#
# Throttling proactivo a partir de las cabeceras de rate limit.
#
# Lección tomada de la práctica documentada para la API de Anthropic: hay que
# **leer las cabeceras `anthropic-ratelimit-*` en TODAS las respuestas, no solo en
# los 429**. Anthropic aplica un token bucket que se rellena de forma continua
# (peticiones/min, tokens de entrada/min y tokens de salida/min) y las cabeceras
# dicen cuánto queda en cada cubo. Frenar un poco antes de agotarlos sale más
# barato que comerse el 429 y esperar el reset entero — y con concurrencia > 1
# evita que todos los hilos choquen a la vez contra el muro.


# Por debajo de este porcentaje de cuota restante se empieza a frenar.
UMBRAL_FRENADO = 0.15
# Nunca se espera más de esto de golpe por throttling preventivo.
ESPERA_MAXIMA_SEGUNDOS = 60.0

# Familias de cubos que publica la API. Se miran todas: basta con que una esté
# al límite para tener que frenar.
FAMILIAS = ("requests", "tokens", "input-tokens", "output-tokens")

_UNIDADES = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_FRACCION = re.compile(r"(\.\d{1,9})")


def parse_reset(valor: str) -> Optional[float]:
    """Segundos hasta el reset. Acepta duraciones (`45s`, `1h`) y fechas RFC 3339."""
    valor = str(valor).strip()
    if not valor:
        return None
    if len(valor) >= 2 and valor[-1].lower() in _UNIDADES:
        try:
            segundos = float(valor[:-1]) * _UNIDADES[valor[-1].lower()]
        except ValueError:
            segundos = None
        if segundos is not None:
            return segundos if math.isfinite(segundos) else None  # «nan s», «1e999h»…: no es un tiempo
    try:
        # Python < 3.11 solo admite 3 o 6 decimales de segundo y no entiende la Z.
        texto = _FRACCION.sub(lambda m: (m.group(1) + "000000")[:7], valor.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(texto)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, TypeError):
        return None


@dataclass
class Cubo:
    limite: int
    restante: int
    reset_en: float  # segundos desde que se observó

    @property
    def fraccion(self) -> float:
        if self.limite <= 0:
            return 1.0
        return max(0.0, self.restante / self.limite)


def _a_entero(valor: Optional[str]) -> Optional[int]:
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        return None


class RateLimitTracker:
    """Estado de los cubos de rate limit, alimentado por cada respuesta."""

    def __init__(self, umbral: float = UMBRAL_FRENADO):
        self.umbral = umbral
        self._lock = threading.Lock()
        self._cubos: dict[str, Cubo] = {}
        self._observado_en: float = 0.0

    # ------------------------------------------------------------------

    def observar(self, headers: Optional[Mapping[str, str]]) -> None:
        """Registra las cabeceras de una respuesta (de éxito o de error)."""
        if not headers:
            return
        try:
            normalizadas = {str(k).lower(): str(v) for k, v in dict(headers).items()}
        except (TypeError, ValueError):
            return

        nuevos: dict[str, Cubo] = {}
        for familia in FAMILIAS:
            limite = _a_entero(normalizadas.get(f"anthropic-ratelimit-{familia}-limit"))
            restante = _a_entero(normalizadas.get(f"anthropic-ratelimit-{familia}-remaining"))
            if limite is None or restante is None:
                continue
            reset = parse_reset(normalizadas.get(f"anthropic-ratelimit-{familia}-reset", ""))
            nuevos[familia] = Cubo(limite, restante, max(0.0, reset or 0.0))

        if nuevos:
            with self._lock:
                self._cubos = nuevos
                self._observado_en = time.monotonic()

    # ------------------------------------------------------------------

    def espera_recomendada(self) -> float:
        """Segundos a esperar antes de la siguiente petición. 0 si hay margen.

        - Cuota agotada  -> esperar al reset (acotado).
        - Cuota por debajo del umbral -> repartir el tiempo que queda hasta el
          reset entre las peticiones que aún caben, para llegar justo.
        """
        with self._lock:
            if not self._cubos:
                return 0.0
            transcurrido = time.monotonic() - self._observado_en
            cubos = list(self._cubos.values())

        espera = 0.0
        for cubo in cubos:
            restante_reset = max(0.0, cubo.reset_en - transcurrido)
            if cubo.restante <= 0:
                espera = max(espera, restante_reset)
            elif cubo.fraccion < self.umbral and restante_reset > 0:
                espera = max(espera, restante_reset / max(1, cubo.restante))
        return min(espera, ESPERA_MAXIMA_SEGUNDOS)

    # ------------------------------------------------------------------

    def resumen(self) -> str:
        """Texto corto para el log o la interfaz."""
        with self._lock:
            if not self._cubos:
                return "sin datos de cuota"
            partes = [
                f"{nombre} {c.restante}/{c.limite}" for nombre, c in sorted(self._cubos.items())
            ]
        return " · ".join(partes)

    @property
    def hay_datos(self) -> bool:
        with self._lock:
            return bool(self._cubos)


def observar_seguro(tracker: Optional[RateLimitTracker], objeto: object) -> None:
    """Extrae cabeceras de una respuesta cruda o de un error, sin lanzar."""
    if tracker is None:
        return
    headers = getattr(objeto, "headers", None)
    if headers is None:
        respuesta = getattr(objeto, "response", None)
        headers = getattr(respuesta, "headers", None)
    try:
        tracker.observar(headers)
    except Exception:
        logger.debug("No se pudieron leer las cabeceras de rate limit", exc_info=True)


# ============================================================================
# 9. Exportar resultados
#    (claude_batch_monitor/export.py)
# ============================================================================
#
# Exportación de resultados a CSV, JSONL, Markdown y texto.


FORMATOS = ("csv", "jsonl", "md", "txt")
MARCA_RESPUESTA = "====== RESPUESTA ======"
MARCA_SOLICITUD = "====== SOLICITUD ======"


class ErrorDeExportacion(ValueError):
    pass


def formato_por_extension(destino: Path) -> str:
    ext = destino.suffix.lower().lstrip(".")
    if ext == "markdown":
        ext = "md"
    if ext not in FORMATOS:
        raise ErrorDeExportacion(
            f"No sé exportar a '.{ext}'. Usa una de estas extensiones: "
            + ", ".join(f".{f}" for f in FORMATOS)
        )
    return ext


def leer_respuesta(job: Job, msg_id: int, ciclo: int) -> str:
    """Texto de la respuesta guardada de una orden ('' si no hay)."""
    ruta = job.response_path(msg_id, ciclo)
    try:
        contenido = ruta.read_text(encoding="utf-8")
    except OSError:
        # Una respuesta pagada que no se pudo escribir en disco se conserva en memoria.
        guardada = getattr(job, "respuestas_sin_guardar", {}).get(msg_id)
        if not guardada or guardada[0] != ciclo:
            return ""
        contenido = guardada[1]
    _, _, respuesta = contenido.partition(MARCA_RESPUESTA)
    return respuesta.strip()


def _texto_estado(m: JobMessage) -> str:
    return m.status if not m.detalle else f"{m.status}: {m.detalle}"


def exportar(
    job: Job,
    destino: Path,
    formato: Optional[str] = None,
    ciclo: Optional[int] = None,
) -> Path:
    """Escribe los resultados del lote en `destino` y devuelve su ruta."""
    destino = Path(destino)
    formato = (formato or formato_por_extension(destino)).lower()
    if formato not in FORMATOS:
        raise ErrorDeExportacion(f"Formato desconocido '{formato}'. Usa: {', '.join(FORMATOS)}.")
    ciclo = ciclo or (job.ciclos_completados + 1)
    destino.parent.mkdir(parents=True, exist_ok=True)

    if formato == "csv":
        _csv(job, destino, ciclo)
    elif formato == "jsonl":
        _jsonl(job, destino, ciclo)
    elif formato == "md":
        _md(job, destino, ciclo)
    else:
        _txt(job, destino, ciclo)
    return destino


_EMPIEZA_COMO_FORMULA = ("=", "+", "-", "@", "\t", "\r")
_ES_UN_NUMERO = re.compile(r"[+-]?(\d+[.,]?\d*|[.,]\d+)")


def neutralizar_formula(texto: str) -> str:
    """Evita que una hoja de cálculo ejecute como fórmula una celda de texto («inyección de CSV»).

    Excel, LibreOffice y Google Sheets interpretan como fórmula lo que empieza por `=`, `+`, `-` o `@`: una
    respuesta (o una pregunta sacada de un fichero ajeno) como `=HYPERLINK("http://…", "pulsa")` podría
    enviar datos de la hoja a un tercero. Se le antepone un apóstrofo, que la hoja trata como «esto es texto».
    Los números sueltos (`-5`, `+3,5`) no se tocan: no son fórmulas.
    """
    if texto.startswith(_EMPIEZA_COMO_FORMULA) and not _ES_UN_NUMERO.fullmatch(texto.strip()):
        return "'" + texto
    return texto


def _csv(job: Job, destino: Path, ciclo: int) -> None:
    # utf-8-sig para que Excel en Windows detecte el UTF-8 y no rompa las tildes.
    with open(destino, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "id",
                "estado",
                "tokens_entrada",
                "tokens_salida",
                "coste_usd",
                "latencia_ms",
                "truncada",
                "error_type",
                "detalle",
                "solicitud",
                "respuesta",
            ]
        )
        for msg in job.messages:
            writer.writerow(
                [
                    msg.id,
                    msg.status,
                    msg.tokens_input,
                    msg.tokens_output,
                    f"{msg.coste_usd:.6f}",
                    msg.latency_ms,
                    "sí" if msg.truncated else "no",
                    neutralizar_formula(msg.error_type),
                    neutralizar_formula(msg.detalle),
                    neutralizar_formula(msg.text),
                    neutralizar_formula(leer_respuesta(job, msg.id, ciclo)),
                ]
            )


def _jsonl(job: Job, destino: Path, ciclo: int) -> None:
    with open(destino, "w", encoding="utf-8", newline="\n") as handle:
        for msg in job.messages:
            registro = {
                "id": msg.id,
                "estado": msg.status,
                "modelo": job.modelo,
                "tokens_entrada": msg.tokens_input,
                "tokens_salida": msg.tokens_output,
                "coste_usd": str(msg.coste_usd),
                "latencia_ms": msg.latency_ms,
                "truncada": msg.truncated,
                "error_type": msg.error_type,
                "detalle": msg.detalle,
                "solicitud": msg.text,
                "respuesta": leer_respuesta(job, msg.id, ciclo),
            }
            handle.write(json.dumps(registro, ensure_ascii=False) + "\n")


def _md(job: Job, destino: Path, ciclo: int) -> None:
    partes = [
        f"# Resultados del lote {job.job_id}",
        "",
        f"- Modelo: `{job.modelo}`",
        f"- Modo: {job.modo}",
        f"- Completadas: {job.completed_count()}/{job.total_count()}",
        f"- Coste: ${job.coste_total():.4f}",
        "",
    ]
    for msg in job.messages:
        partes += [f"## Orden {msg.id} — {_texto_estado(msg)}", "", "**Solicitud**", ""]
        partes += ["> " + linea for linea in msg.text.splitlines() or [""]]
        respuesta = leer_respuesta(job, msg.id, ciclo)
        if respuesta:
            partes += ["", "**Respuesta**", "", respuesta]
        partes += ["", "---", ""]
    destino.write_text("\n".join(partes), encoding="utf-8")


def _txt(job: Job, destino: Path, ciclo: int) -> None:
    bloques = []
    for msg in job.messages:
        respuesta = leer_respuesta(job, msg.id, ciclo)
        bloques.append(respuesta if respuesta else f"[orden {msg.id}: {_texto_estado(msg)}]")
    destino.write_text("\n\n---\n\n".join(bloques) + "\n", encoding="utf-8")


# ============================================================================
# 10. Estimar el coste antes de gastar
#    (claude_batch_monitor/estimator.py)
# ============================================================================
#
# Estimación del coste ANTES de gastar nada.
#
# El número que importa es el peor caso: cada respuesta usando `max_tokens`
# completo. Es una cota, no una predicción; lo normal es que el gasto real sea
# bastante menor, pero un tope de gasto solo protege si se razona con el peor caso.


# Contar tokens de verdad cuesta una llamada (gratuita, pero con su propio rate
# limit) por orden. Por encima de este número se usa la estimación local.
MAX_ORDENES_CONTEO_EXACTO = 25


@dataclass
class Estimacion:
    ordenes: int
    tokens_entrada: int
    exacta: bool  # tokens contados por la API (True) o estimados en local (False)
    coste_maximo: Decimal
    coste_solo_entrada: Decimal
    precios: Precios
    modo: str
    max_tokens: int
    avisos: list = field(default_factory=list)

    def resumen_lineas(self) -> list[str]:
        origen = "contados por la API" if self.exacta else "estimación local"
        return [
            f"Órdenes detectadas : {self.ordenes}",
            f"Modo               : {self.modo}",
            f"Tokens entrada     : ~{self.tokens_entrada:,} ({origen})",
            f"max_tokens/orden   : {self.max_tokens:,}",
            f"Precio in/out      : {self.precios.entrada.normalize():f} / "
            f"{self.precios.salida.normalize():f} $/Mtok",
            f"Coste MÁXIMO       : {formatear_usd(self.coste_maximo)}  (si cada respuesta llenara max_tokens)",
        ]


def _tokens_orden(texto: str, config: Configuracion, client: Optional[Any], exactos: bool) -> int:
    local = estimar_tokens(texto) + estimar_tokens(config.system_prompt)
    if not (client is not None and exactos):
        return local
    try:
        kwargs: dict[str, Any] = {
            "model": config.modelo,
            "messages": [{"role": "user", "content": texto}],
        }
        if config.system_prompt.strip():
            kwargs["system"] = config.system_prompt
        return int(client.messages.count_tokens(**kwargs).input_tokens)
    except Exception:
        logger.info("count_tokens no disponible; se usa la estimación local", exc_info=True)
        return local


def estimar(
    config: Configuracion,
    ordenes: Sequence[str],
    client: Optional[Any] = None,
) -> Estimacion:
    """Peor caso de coste para estas órdenes con esta configuración."""
    precios = config.precios()
    lote = config.es_lote
    exactos = client is not None and len(ordenes) <= MAX_ORDENES_CONTEO_EXACTO

    tokens = [_tokens_orden(o, config, client, exactos) for o in ordenes]
    # La primera petición escribe la caché (1,25 x) si hay prompt de sistema.
    mult = ESCRITURA_CACHE_5M if (config.usar_cache and config.system_prompt.strip()) else Decimal(1)
    total = sum(
        (coste_maximo(t, config.max_tokens, precios, lote, mult) for t in tokens), Decimal(0)
    )
    solo_entrada = sum((coste_maximo(t, 0, precios, lote, mult) for t in tokens), Decimal(0))

    avisos: list[str] = []
    aviso_precio = precios.aviso()
    if aviso_precio:
        avisos.append(aviso_precio)
    if config.tope_gasto_usd > 0 and total > config.tope_gasto_usd:
        avisos.append(
            f"El peor caso ({formatear_usd(total, 2)}) supera tu tope de {formatear_usd(config.tope_gasto_usd, 2)}: "
            "el lote podría detenerse antes de terminar. Baja max_tokens, usa el modo lote o sube el tope."
        )
    info = buscar_modelo(config.modelo)
    if config.usar_cache and config.system_prompt.strip() and info and info.min_cache:
        if estimar_tokens(config.system_prompt) < info.min_cache:
            avisos.append(
                f"El prompt de sistema es más corto que el mínimo que {info.nombre} puede "
                f"cachear (~{info.min_cache} tokens): la caché no se activará, y es normal."
            )
    return Estimacion(
        ordenes=len(ordenes),
        tokens_entrada=sum(tokens),
        exacta=exactos,
        coste_maximo=total,
        coste_solo_entrada=solo_entrada,
        precios=precios,
        modo="lote (-50 %)" if lote else "inmediato",
        max_tokens=config.max_tokens,
        avisos=avisos,
    )


# ============================================================================
# 11. Avisos: escritorio y Telegram
#    (claude_batch_monitor/notify.py)
# ============================================================================
#
# Avisos al usuario: notificación de escritorio y Telegram.
#
# Nada de esto es imprescindible. Si falla, se registra y el lote sigue.


try:  # opcional
    from plyer import notification as _plyer_notification

    HAS_PLYER = True
except Exception:  # pragma: no cover - depende del entorno
    _plyer_notification = None
    HAS_PLYER = False


def credenciales_telegram(chat_id_config: str = "") -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or SecureStorage.get(KEY_TELEGRAM_TOKEN) or ""
    chat_id = (
        os.getenv("TELEGRAM_CHAT_ID")
        or chat_id_config
        or SecureStorage.get(KEY_TELEGRAM_CHAT_ID)
        or ""
    )
    return token.strip(), str(chat_id).strip()


def enviar_telegram(
    titulo: str,
    mensaje: str,
    chat_id_config: str = "",
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    reintentos: int = 3,
) -> bool:
    """Envía un mensaje por Telegram. Nunca lanza ni escribe el token en el log."""
    t, c = credenciales_telegram(chat_id_config)
    token, chat_id = token or t, chat_id or c
    if not token or not chat_id:
        return False
    texto = f"<b>{html.escape(titulo)}</b>\n\n{html.escape(mensaje)}"
    carga = json.dumps({"chat_id": chat_id, "text": texto, "parse_mode": "HTML"}).encode("utf-8")
    peticion = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=carga,
        headers={"Content-Type": "application/json"},
    )
    for intento in range(1, reintentos + 1):
        try:
            with urllib.request.urlopen(peticion, timeout=10) as r:
                datos = json.loads(r.read().decode("utf-8", errors="replace"))
            if not datos.get("ok", False):
                raise RuntimeError(datos.get("description", "Telegram rechazó el mensaje"))
            return True
        except Exception as e:
            # Se registra el TIPO de error, no su texto: el de urllib puede incluir la URL con el token.
            logger.warning("Telegram falló (intento %d/%d): %s", intento, reintentos, type(e).__name__)
            if intento < reintentos:
                time.sleep(2 ** (intento - 1))
    logger.error("Telegram falló definitivamente: %s", titulo)
    return False


class ErrorDeTelegram(Exception):
    """La prueba de Telegram falló; el mensaje explica por qué sin incluir el token."""


def probar_telegram(token: str, chat_id: str, timeout: float = 10.0) -> None:
    """Envía un mensaje de prueba. Lanza `ErrorDeTelegram` con la causa si no llega."""
    token, chat_id = (token or "").strip(), (chat_id or "").strip()
    if not token or not chat_id:
        raise ErrorDeTelegram("Faltan el token del bot y el Chat ID.")
    carga = json.dumps({"chat_id": chat_id, "text": "Telegram configurado correctamente."}).encode("utf-8")
    peticion = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=carga,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as r:
            datos = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        # Telegram explica el motivo en el cuerpo (token mal escrito, chat inexistente…).
        detalle = ""
        try:
            detalle = json.loads(e.read().decode("utf-8", errors="replace")).get("description", "")
        except Exception:
            pass
        raise ErrorDeTelegram(detalle or f"Telegram respondió con el error {e.code}.") from None
    except Exception as e:
        # No se incluye `str(e)`: el de urllib puede llevar la URL y, con ella, el token.
        raise ErrorDeTelegram(f"No se pudo conectar con Telegram ({type(e).__name__}).") from None
    if not datos.get("ok", False):
        raise ErrorDeTelegram(str(datos.get("description") or "Telegram rechazó el mensaje."))


def notificar_escritorio(titulo: str, mensaje: str) -> bool:
    if not HAS_PLYER:
        return False
    try:
        _plyer_notification.notify(title=titulo, message=mensaje[:250], app_name=APP_NAME, timeout=8)
        return True
    except Exception:
        return False


def notificar(titulo: str, mensaje: str, chat_id_config: str = "") -> None:
    """Escritorio + Telegram (este en un hilo aparte para no frenar el lote)."""
    notificar_escritorio(titulo, mensaje)
    threading.Thread(
        target=enviar_telegram, args=(titulo, mensaje, chat_id_config), daemon=True
    ).start()


# ============================================================================
# 12. Sistema operativo: anti-suspensión y abrir carpetas
#    (claude_batch_monitor/platform_utils.py)
# ============================================================================
#
# Utilidades dependientes del sistema operativo: anti-suspensión, abrir rutas, el tema y cómo se llama Python.


# Windows: ES_CONTINUOUS | ES_SYSTEM_REQUIRED. (La v11 añadía además
# ES_AWAYMODE_REQUIRED, pensado para grabadores multimedia, no para esto.)
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """Evita que el sistema se suspenda mientras corre un lote.

    En Windows `SetThreadExecutionState` es POR HILO: hay que activarlo y
    desactivarlo desde el mismo hilo (el que ejecuta el lote), no desde el de la
    interfaz. Por eso se usa como contexto dentro del hilo trabajador:

        with KeepAwake():
            engine.ejecutar(...)
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._windows = False

    def enable(self) -> None:
        sistema = platform.system()
        try:
            if sistema == "Windows":
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
                )
                self._windows = True
            elif sistema == "Darwin":
                self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            elif shutil.which("systemd-inhibit"):
                self._proc = subprocess.Popen(
                    [
                        "systemd-inhibit",
                        "--what=idle:sleep",
                        "--who=Claude Batch Monitor",
                        "--why=Procesando un lote",
                        "sleep",
                        "infinity",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception as e:
            logger.warning("No se pudo bloquear la suspensión: %s", e)

    def disable(self) -> None:
        try:
            if self._windows:
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)  # type: ignore[attr-defined]
                self._windows = False
            if self._proc is not None:
                self._proc.terminate()
                self._proc = None
        except Exception:
            pass

    def __enter__(self) -> KeepAwake:
        self.enable()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disable()


def abrir_ruta(path: Path) -> bool:
    """Abre un fichero o carpeta con la aplicación por defecto del sistema."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception as e:
        logger.warning("No se pudo abrir %s: %s", path, e)
        return False


def nombre_de_python() -> str:
    """Cómo se llama Python en una terminal de este sistema: `py` en Windows, `python3` en los demás."""
    return "py" if sys.platform == "win32" else "python3"


def comando_para_instalar(libreria: str) -> str:
    """El comando que se le enseña a la persona para instalar una librería a mano."""
    return f"{nombre_de_python()} -m pip install {libreria}"


def comando_para_ejecutar() -> str:
    """Lo que se escribe en una terminal para abrir este programa, tal como se abrió ahora.

    Sirve para los mensajes de ayuda: el archivo único (`python3 claude_batch_monitor.py`), el paquete
    (`python3 -m claude_batch_monitor`) o el ejecutable descargado.
    """
    if getattr(sys, "frozen", False):
        nombre = Path(sys.executable).name
        return nombre if sys.platform == "win32" else f"./{nombre}"
    if __package__:  # `python -m claude_batch_monitor`, o instalado con pip
        return f"{nombre_de_python()} -m {__package__.split('.')[0]}"
    return f"{nombre_de_python()} {Path(__file__).name}"  # el archivo único


def tema_del_sistema() -> str:
    """`oscuro` si el sistema usa el tema oscuro; `claro` en cualquier otro caso, también si no se puede saber."""
    try:
        if sys.platform == "win32":
            import winreg

            clave = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, clave) as abierta:
                return "claro" if winreg.QueryValueEx(abierta, "AppsUseLightTheme")[0] else "oscuro"
        if sys.platform == "darwin":
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                               capture_output=True, text=True, timeout=3)
            return "oscuro" if r.stdout.strip().lower() == "dark" else "claro"
        if shutil.which("gsettings"):  # GNOME y los que lo imitan (Ubuntu, Fedora, Cinnamon…)
            r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                               capture_output=True, text=True, timeout=3)
            if "dark" in r.stdout.lower():
                return "oscuro"
        if "dark" in os.environ.get("GTK_THEME", "").lower():
            return "oscuro"
    except Exception as e:  # solo es estética: ante la duda, claro
        logger.info("No se pudo saber el tema del sistema: %s", e)
    return "claro"


# ============================================================================
# 13. Una sola copia del programa gastando a la vez
#    (claude_batch_monitor/instancia_unica.py)
# ============================================================================
#
# Una sola copia del programa gastando a la vez sobre la misma carpeta de datos.
#
# El gasto acumulado vive en `state.json`: cada copia lo lee al empezar y lo reescribe entero con cada respuesta.
# Con dos copias abiertas a la vez, la que escribe última borra lo que sumó la otra y el tope de gasto deja de
# proteger; además las dos podrían enviar la misma pregunta. Para evitarlo, quien va a gastar toma un bloqueo del
# sistema operativo sobre un fichero de la carpeta de datos. Lo suelta el propio sistema cuando el programa
# termina —también si lo matan o se va la luz—, así que no puede quedarse «atascado».
#
# Este seguro nunca debe impedir el uso por culpa propia: si el sistema no permite bloquear (carpeta de solo
# lectura, un disco de red raro…), se deja pasar.


try:  # Linux y macOS
    import fcntl
except ImportError:  # pragma: no cover - depende del sistema
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - depende del sistema
    msvcrt = None  # type: ignore[assignment]

ARCHIVO_DE_BLOQUEO = "instancia.lock"
_RESERVADO = 16  # los primeros bytes son los que se bloquean; el número de proceso se escribe después


class BloqueoDeInstancia:
    """`adquirir()` devuelve False si OTRA copia ya tiene la carpeta de datos en uso."""

    def __init__(self, ruta: Optional[Path] = None):
        self._ruta = ruta
        self._archivo: Any = None
        self.dueno = ""  # número de proceso de la otra copia, si se pudo leer

    @property
    def ruta(self) -> Path:
        return self._ruta or (DATA_DIR / ARCHIVO_DE_BLOQUEO)

    def adquirir(self) -> bool:
        if self._archivo is not None:
            return True
        try:
            archivo = open(self.ruta, "a+b")
        except OSError:
            return True  # no se puede ni crear el fichero: no se impide el uso
        try:
            libre = self._bloquear(archivo)
        except Exception:  # noqa: BLE001 - el seguro no puede ser quien rompa el programa
            logger.exception("No se pudo comprobar si hay otra copia abierta")
            archivo.close()
            return True
        if not libre:
            self.dueno = self._leer_dueno(archivo)
            archivo.close()
            return False
        try:
            archivo.seek(_RESERVADO)
            archivo.truncate()
            archivo.write(str(os.getpid()).encode("ascii"))
            archivo.flush()
        except OSError:
            pass  # solo es información para quien se encuentre el bloqueo ocupado
        self._archivo = archivo
        return True

    def liberar(self) -> None:
        archivo, self._archivo = self._archivo, None
        if archivo is None:
            return
        try:
            self._desbloquear(archivo)
        except Exception:  # noqa: BLE001
            pass
        try:
            archivo.close()
        except OSError:
            pass

    def __enter__(self) -> BloqueoDeInstancia:
        self.adquirir()
        return self

    def __exit__(self, *exc: object) -> None:
        self.liberar()

    # ------------------------------------------------------------------

    @staticmethod
    def _bloquear(archivo: Any) -> bool:
        if fcntl is not None:
            try:
                fcntl.flock(archivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            return True
        if msvcrt is not None:
            archivo.seek(0)
            if os.fstat(archivo.fileno()).st_size < _RESERVADO:
                archivo.write(b"\0" * _RESERVADO)
                archivo.flush()
                archivo.seek(0)
            try:
                msvcrt.locking(archivo.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        return True  # ni uno ni otro: no hay con qué bloquear

    @staticmethod
    def _desbloquear(archivo: Any) -> None:
        if fcntl is not None:
            fcntl.flock(archivo.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            archivo.seek(0)
            msvcrt.locking(archivo.fileno(), msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _leer_dueno(archivo: Any) -> str:
        try:
            archivo.seek(_RESERVADO)
            texto = archivo.read(20).decode("ascii", "ignore").strip()
        except (OSError, ValueError):
            return ""
        return texto if texto.isdigit() else ""


def aviso_de_otra_copia(dueno: str = "") -> str:
    """Lo que se le dice a quien intenta abrir una segunda copia desde la terminal."""
    proceso = f" (proceso {dueno})" if dueno else ""
    return (
        f"Ya hay otra copia de Claude Batch Monitor en marcha{proceso}. Dos copias a la vez se pisarían el gasto "
        "acumulado y el tope de gasto dejaría de proteger. Ciérrala (si no ves su ventana, mira la barra de tareas "
        "o la bandeja del sistema) o usa otra carpeta de datos con la variable CLAUDE_MONITOR_HOME."
    )


# ============================================================================
# 14. Motores: clasificar los errores de la API
#    (claude_batch_monitor/core/errors.py)
# ============================================================================
#
# Clasificación de los errores de la API de Anthropic.
#
# Lo importante es distinguir cosas que el SDK trata igual y que exigen respuestas
# opuestas:
#
# - 429 *transitorio* (rate limit por minuto): esperar `retry-after` y seguir.
# - 429 *de tope de gasto* (`enforced_spend_limit_reached`): NO tiene `retry-after`
#   y no se levanta hasta el día 1 del mes siguiente. Reintentar es inútil: hay que
#   parar y decirlo. Tratarlo como un rate limit más deja el programa dando vueltas
#   durante semanas.
# - 400 por un límite de gasto que puso el propio usuario ("You have reached your
#   specified API usage limits"): la orden está bien; la petición no es "inválida".
# - 402 o "credit balance is too low": sin saldo.
#
# Referencia: https://platform.claude.com/docs/en/api/errors
# y https://platform.claude.com/docs/en/api/rate-limits#reaching-your-spend-cap


class Categoria(str, Enum):
    RATE_LIMIT = "rate_limit"      # esperar y reintentar; no cuenta como fallo
    LIMITE_CUENTA = "account_limit"  # tope de gasto (del tier o propio): parar
    SIN_SALDO = "no_credit"        # parar
    AUTH = "auth"                  # 401 / 403
    PETICION = "bad_request"       # 400 genuino
    MODELO = "model_not_found"     # 404 sobre el modelo
    NO_ENCONTRADO = "not_found"
    DEMASIADO_GRANDE = "too_large"  # 413
    SERVIDOR = "server"            # 5xx / 529 / 408 / 409 / error en mitad del stream: backoff
    OTRO = "other"


@dataclass
class ErrorApi:
    categoria: Categoria
    status: int = 0
    tipo: str = ""
    mensaje: str = ""
    codigo: str = ""

    @property
    def reanudacion(self) -> str:
        """'2026-09-01 00:00 UTC' si el mensaje de la API dice cuándo vuelve el acceso."""
        return fecha_de_reanudacion(self.mensaje)


_REANUDA = re.compile(
    r"regain access on\s+(\d{4}-\d{2}-\d{2})(?:\s+at\s+(\d{1,2}:\d{2})\s*(UTC)?)?", re.IGNORECASE
)


def fecha_de_reanudacion(mensaje: str) -> str:
    m = _REANUDA.search(mensaje or "")
    if not m:
        return ""
    hora = f" {m.group(2)} UTC" if m.group(2) else ""
    return f"{m.group(1)}{hora}"


def _cuerpo_interno(body: Any) -> dict:
    """El SDK entrega `body` como el objeto `error` interno, pero se tolera el envoltorio completo."""
    if not isinstance(body, dict):
        return {}
    interno = body.get("error")
    return interno if isinstance(interno, dict) else body


def analizar_error(error: BaseException) -> ErrorApi:
    status = int(getattr(error, "status_code", 0) or 0)
    cuerpo = _cuerpo_interno(getattr(error, "body", None))
    tipo = str(cuerpo.get("type") or "")
    mensaje = str(cuerpo.get("message") or "") or str(error)
    detalles = cuerpo.get("details")
    codigo = str(detalles.get("error_code") or "") if isinstance(detalles, dict) else ""
    texto = mensaje.lower()

    def devolver(c: Categoria) -> ErrorApi:
        return ErrorApi(c, status, tipo, mensaje, codigo)

    if status == 429 or tipo == "rate_limit_error":
        if (
            codigo == "enforced_spend_limit_reached"
            or "reached your api usage limits" in texto
            or "monthly api usage threshold" in texto
            or "regain access on" in texto
        ):
            return devolver(Categoria.LIMITE_CUENTA)
        return devolver(Categoria.RATE_LIMIT)
    if status == 402 or tipo == "billing_error":
        return devolver(Categoria.SIN_SALDO)
    if status == 400 and "credit balance" in texto and ("too low" in texto or "insufficient" in texto):
        return devolver(Categoria.SIN_SALDO)
    if status == 400 and "reached your specified" in texto and "usage limit" in texto:
        return devolver(Categoria.LIMITE_CUENTA)
    if status in (401, 403) or tipo in ("authentication_error", "permission_error"):
        return devolver(Categoria.AUTH)
    if status == 404 or tipo == "not_found_error":
        return devolver(Categoria.MODELO if "model" in texto else Categoria.NO_ENCONTRADO)
    if status == 413 or tipo == "request_too_large":
        return devolver(Categoria.DEMASIADO_GRANDE)
    if status in (400, 422) or tipo == "invalid_request_error":
        return devolver(Categoria.PETICION)
    if status >= 500 or status in (408, 409) or tipo in ("overloaded_error", "api_error", "timeout_error"):
        return devolver(Categoria.SERVIDOR)  # 408 y 409 los devuelven pasarelas y proxies: se reintentan como el SDK
    return devolver(Categoria.OTRO)


def cadena_de_causas(error: Optional[BaseException]) -> list[BaseException]:
    cadena: list[BaseException] = []
    visto: set[int] = set()
    while error is not None and id(error) not in visto:
        cadena.append(error)
        visto.add(id(error))
        error = error.__cause__ or error.__context__
    return cadena


def es_sin_conexion(error: BaseException) -> bool:
    """True si la conexión ni siquiera llegó a establecerse (DNS, red caída, rechazo).

    En ese caso la petición no salió, así que reintentar NO puede duplicar un
    cobro. Si la conexión se cortó a mitad de respuesta no se sabe si el servidor
    llegó a procesarla y es más prudente parar.

    Se detecta por NOMBRE de clase, no con `isinstance`: el SDK 0.x usa `httpx` y
    el 1.x usa `httpx2`, y un `import httpx` aquí dejaba el programa sin arrancar
    con el SDK actual.
    """
    for causa in cadena_de_causas(error):
        for clase in type(causa).__mro__:
            if clase.__name__ == "ConnectError" and clase.__module__.split(".")[0] in ("httpx", "httpx2"):
                return True
    return False


# ----------------------------------------------------------------------
# Fallos de la conexión que el SDK no siempre envuelve
# ----------------------------------------------------------------------
# Con la respuesta en streaming, un corte a mitad del cuerpo o un tiempo agotado entre trozos salen tal cual
# de la librería HTTP (no como `anthropic.APIConnectionError`). Se reconocen por NOMBRE de clase, como arriba.

_LIBRERIAS_HTTP = ("httpx", "httpx2", "httpcore", "h11", "ssl", "socket", "builtins", "http", "urllib3")
_CORTES = {
    "RemoteProtocolError", "ReadError", "WriteError", "CloseError", "ProtocolError", "IncompleteRead",
    "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError", "ChunkedEncodingError",
}
_TIMEOUTS = {"TimeoutException", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout", "TimeoutError", "timeout"}
_TIMEOUTS_SIN_ENVIO = {"ConnectTimeout", "PoolTimeout", "WriteTimeout"}  # la petición no llegó a salir entera


def _clases_de_la_cadena(error: BaseException) -> set[str]:
    nombres: set[str] = set()
    for causa in cadena_de_causas(error):
        for clase in type(causa).__mro__:
            if clase.__module__.split(".")[0] in _LIBRERIAS_HTTP:
                nombres.add(clase.__name__)
    return nombres


class FlujoIncompleto(Exception):
    """Una respuesta en streaming terminó sin entregar el mensaje entero.

    `era_un_flujo` es False si lo recibido ni siquiera era un flujo de la API (un proxy, un portal cautivo…
    contestó con otra cosa: el servidor no llegó a procesar nada) y True si empezó como tal y se cortó.
    """

    def __init__(self, era_un_flujo: bool, detalle: str = ""):
        super().__init__(detalle or (
            "El flujo de la respuesta se cortó antes de terminar." if era_un_flujo
            else "La respuesta no era un flujo de la API."))
        self.era_un_flujo = era_un_flujo


def es_corte_de_conexion(error: BaseException) -> bool:
    """La conexión se rompió a mitad de la petición o de la respuesta (el servidor pudo haberla procesado)."""
    if any(isinstance(c, FlujoIncompleto) and c.era_un_flujo for c in cadena_de_causas(error)):
        return True
    return bool(_clases_de_la_cadena(error) & _CORTES)


def es_timeout(error: BaseException) -> bool:
    return bool(_clases_de_la_cadena(error) & _TIMEOUTS)


def timeout_con_peticion_enviada(error: BaseException) -> bool:
    """True si el tiempo se agotó DESPUÉS de enviar la petición: el servidor pudo procesarla y cobrarla.

    Si no se puede saber, se supone que sí: para un tope de gasto, la duda cuenta como gasto.
    """
    return not (_clases_de_la_cadena(error) & _TIMEOUTS_SIN_ENVIO)


def pudo_haberse_procesado(error: BaseException) -> bool:
    """¿Pudo el servidor procesar la petición aunque no tengamos su respuesta?

    Un 4xx (salvo 408 y 409) lo rechazó sin ejecutarla, y si no había conexión la petición ni salió.
    Un 5xx, un tiempo agotado tras enviarla o un corte a mitad: no se sabe, y en un lote eso significa
    que pudo crearse (y cobrarse) sin que nos enteráramos.
    """
    estado = getattr(error, "status_code", None)
    if isinstance(estado, int):
        return estado >= 500 or estado in (408, 409)
    if es_sin_conexion(error) or es_respuesta_ilegible(error):
        return False
    if es_timeout(error):
        return timeout_con_peticion_enviada(error)
    return True


def es_respuesta_ilegible(error: BaseException) -> bool:
    """Llegó una respuesta completa pero no es lo que la API envía (una página de un proxy, un portal cautivo…)."""
    for causa in cadena_de_causas(error):
        nombre = type(causa).__name__
        if nombre in ("JSONDecodeError", "APIResponseValidationError", "UnicodeDecodeError"):
            return True
        if isinstance(causa, FlujoIncompleto) and not causa.era_un_flujo:
            return True
    return False


# ============================================================================
# 15. Motores: lo común (contabilidad, esperas, guardado y parada)
#    (claude_batch_monitor/core/base.py)
# ============================================================================
#
# Lo común a los dos motores: contabilidad, esperas, persistencia y parada.
#
# Las reglas que aquí se fijan son las que más dinero y datos protegen:
#
# - **Persistir ANTES de comprobar el tope** (`registrar_respuesta`). Una respuesta
#   que ya se pagó nunca se descarta porque el tope se haya alcanzado con ella.
# - **Estimar ANTES de enviar** (`_reservar`). El tope se compara con el peor caso
#   de la siguiente petición, no solo con lo ya gastado, y con concurrencia se
#   reserva el peor caso de cada petición en vuelo para que N hilos no se pasen N veces.
# - **Un solo motivo de parada, el más específico** (`marcar_parada`). Un hilo que
#   despierta de su espera no puede degradar un "sin saldo" a un "detenido".


try:
    import anthropic
except ImportError:  # el arranque lo explica y ofrece instalarla (ver instalacion.py)
    anthropic = None  # type: ignore[assignment]

MAX_BACKOFF_SECONDS = 1800
MARGEN_RATE_LIMIT_SEC = 2.0
# 429 seguidos SIN ninguna indicación de cuándo vuelve el cupo. Pasado esto no es
# un rate limit pasajero (probablemente un tope de gasto) y esperar más no sirve.
MAX_429_SIN_INFO = 10
# Tiempo máximo acumulado esperando a que vuelva la conexión antes de rendirse.
MAX_SIN_CONEXION_SEC = 600
# Lo más que se hace caso a un `retry-after`; más allá (o un valor que no es un número) se ignora.
MAX_ESPERA_API_SEC = 24 * 3600
# Veces que una misma orden puede agotar el tiempo DESPUÉS de enviarse: cada una pudo cobrarse.
MAX_TIMEOUTS_POR_ORDEN = 3
RESPUESTA_VACIA = "(Claude devolvió una respuesta vacía.)"
AVISO_TRUNCADA = "[ADVERTENCIA: respuesta truncada por el límite de tokens]"


class StopReason(str, Enum):
    COMPLETED = "completed"
    STOPPED = "stopped"            # el usuario pulsó Detener
    DEADLINE = "deadline"          # llegó la hora límite
    BUDGET = "budget"              # tope de gasto propio
    NO_CREDIT = "no_credit"        # sin saldo (402)
    ACCOUNT_LIMIT = "account_limit"  # tope mensual del tier o límite fijado en la consola
    NETWORK = "network"
    FATAL = "fatal"


# Lo que se le dice a la persona al terminar, en la terminal y en las ventanas.
FRASES_DE_PARADA = {
    StopReason.COMPLETED: "Completado.",
    StopReason.STOPPED: "Detenido: el lote queda guardado y se puede reanudar.",
    StopReason.DEADLINE: "Hora límite alcanzada: el lote queda guardado y se puede reanudar.",
    StopReason.BUDGET: "Detenido por el tope de gasto. Lo pagado está guardado.",
    StopReason.NO_CREDIT: "Detenido: sin saldo en la cuenta de Anthropic.",
    StopReason.ACCOUNT_LIMIT: "Detenido: límite de gasto de la cuenta de Anthropic.",
    StopReason.NETWORK: "Detenido por un fallo de red. El lote se puede reanudar.",
    StopReason.FATAL: "Detenido por un error. Revisa el mensaje y reanuda el lote.",
}

# Motivos que no explican por qué se paró; cualquier motivo concreto los sustituye.
_DEBILES = {StopReason.STOPPED, StopReason.DEADLINE}


class Decision(Enum):
    """Qué hacer tras un error de la API."""

    REINTENTAR = "reintentar"
    SIGUIENTE = "siguiente"  # esta orden falla, pero las demás pueden seguir
    PARAR = "parar"


@dataclass
class Contadores:
    """Reintentos de UNA operación. Los de rate limit y sin conexión no cuentan como fallo."""

    errores: int = 0  # solo se usa cuando no hay una orden concreta a la que anotarlos
    sin_info_429: int = 0
    intentos_sin_conexion: int = 0
    segundos_sin_conexion: float = 0.0
    timeouts_enviados: int = 0  # tiempos agotados con la petición ya enviada (pudo cobrarse)


@dataclass
class Callbacks:
    """Canales por los que el motor informa a la interfaz. Todos opcionales."""

    estado: Optional[Callable[[str, bool], None]] = None      # texto, ¿ocupado?
    resultado: Optional[Callable[[JobMessage], None]] = None  # una orden cambió de estado
    fin_ciclo: Optional[Callable[[int, int, int], None]] = None  # completadas, total, ciclo
    error: Optional[Callable[[str], None]] = None
    tick: Optional[Callable[[float, float], None]] = None     # espera total, restante
    gasto: Optional[Callable[[Decimal, Consumo], None]] = None
    # Para interfaces que quieren dibujar su propio texto en vez de usar el del motor:
    espera: Optional[Callable[[str, float], None]] = None     # motivo, segundos
    inicio_orden: Optional[Callable[[JobMessage], None]] = None
    delta: Optional[Callable[[int, str], None]] = None        # id de orden, texto nuevo (streaming)

    def emit(self, nombre: str, *args: Any) -> None:
        fn = getattr(self, nombre, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            # Un fallo en la interfaz nunca debe tumbar un lote que ya está cobrando.
            logger.exception("Fallo en el callback '%s'", nombre)


def build_request_params(config: Configuracion, texto: str) -> dict:
    """Parámetros de `messages.create` (o de una petición de lote) para una orden."""
    params: dict[str, Any] = {
        "model": config.modelo,
        "max_tokens": config.max_tokens,
        "messages": [{"role": "user", "content": texto}],
    }
    sistema = (config.system_prompt or "").strip()
    if sistema:
        bloque: dict[str, Any] = {"type": "text", "text": sistema}
        if config.usar_cache:
            bloque["cache_control"] = {"type": "ephemeral"}
        params["system"] = [bloque]
    if config.esfuerzo and soporta_esfuerzo(config.modelo, config.esfuerzo) is not False:
        params["output_config"] = {"effort": config.esfuerzo}
    return params


def _segundos_de_cabecera(valor: Any) -> Optional[float]:
    """Los segundos de un `retry-after`, o None si no es un número razonable (NaN, infinito, negativo, enorme)."""
    try:
        segundos = float(valor)
    except (TypeError, ValueError):
        return None
    return segundos if math.isfinite(segundos) and 0 <= segundos <= MAX_ESPERA_API_SEC else None


def _categoria_rechazo(response: Any) -> str:
    detalles = getattr(response, "stop_details", None)
    if detalles is None:
        return ""
    cat = detalles.get("category") if isinstance(detalles, dict) else getattr(detalles, "category", "")
    return str(cat or "")


class EngineBase:
    def __init__(self, config: Configuracion, job: Job):
        self.config = config
        self.job = job
        self.precios = config.precios()

        self.estado = EstadoMonitor.cargar()
        self.estado.current_job_id = job.job_id
        if config.reset_gasto_por_lote:
            # Contador por lote: parte de lo que ESTE lote ya lleva gastado (0 si es
            # nuevo). Así reanudar no reinicia el contador ni hereda el de otro lote.
            self.estado.gasto_acumulado_usd = job.coste_total()
        self.estado.guardar()

        self.evento_detener = threading.Event()
        self._client: Optional[Any] = None
        self.deadline: Optional[datetime] = None
        self.stop_reason: Optional[StopReason] = None
        self.rate_limits = RateLimitTracker()
        self.notificaciones = True

        self._lock_estado = threading.RLock()
        self._cond = threading.Condition(self._lock_estado)
        self._reservado = Decimal(0)
        # Peor caso de las peticiones que se abandonaron por tiempo sin saber si el servidor las cobró:
        # cuentan contra el tope (si no, un servidor lento podría cobrar muchas veces sin que se viera).
        self._incierto = Decimal(0)
        self._con_timeout: set[int] = set()  # órdenes que ya agotaron el tiempo: se reintentan en streaming

    # ------------------------------------------------------------------
    # Cliente
    # ------------------------------------------------------------------

    def _cliente_nuevo(self) -> Any:
        if anthropic is None:
            raise ValueError(f"Falta la librería 'anthropic'. Instálala con: {comando_para_instalar('anthropic')}")
        api_key = obtener_api_key()
        if not api_key:
            raise ValueError(
                "No hay API key de Anthropic. Pégala en la aplicación o define ANTHROPIC_API_KEY."
            )
        return anthropic.Anthropic(
            api_key=api_key, timeout=float(self.config.timeout_api_sec), max_retries=0
        )

    def inicializar_cliente(self) -> None:
        self._client = self._cliente_nuevo()

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("El cliente de la API no está inicializado.")
        return self._client

    def cerrar_cliente(self) -> None:
        """Libera el pool de conexiones (no se anula la referencia: puede consultarse después)."""
        cerrar = getattr(self._client, "close", None)
        if callable(cerrar):
            try:
                cerrar()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Tiempos
    # ------------------------------------------------------------------

    def calcular_deadline(self) -> Optional[datetime]:
        """Próxima ocurrencia de la hora límite, o None si no hay.

        Un lote único no necesita hora límite; un bucle SIEMPRE la necesita (si
        no, solo pararía el tope de gasto), así que se le aplica 23:59.
        """
        hora_txt = (self.config.hora_limite or "").strip()
        if not hora_txt and self.config.modo_continuo:
            hora_txt = "23:59"
        if not hora_txt:
            return None
        try:
            hora = datetime.strptime(hora_txt, "%H:%M").time()
        except ValueError:
            hora = dt_time(23, 59)
        ahora = datetime.now()
        limite = datetime.combine(ahora.date(), hora)
        if limite <= ahora:
            limite += timedelta(days=1)
        return limite

    @staticmethod
    def parse_reset(value: str) -> Optional[float]:
        return parse_reset(value)

    @staticmethod
    def _cabeceras(error: Any) -> dict:
        crudas = getattr(getattr(error, "response", None), "headers", None) or {}
        try:
            return {str(k).lower(): str(v) for k, v in dict(crudas).items()}
        except (TypeError, ValueError):
            return {}

    def espera_rate_limit_detalle(self, error: Any) -> tuple[float, bool]:
        """(segundos, ¿la API dijo cuánto?) para un 429.

        Orden de prioridad: `Retry-After` (la respuesta explícita del servidor) ->
        reset de los cubos agotados -> cualquier reset -> texto del mensaje ->
        un minuto. Sin backoff exponencial: cuando el servidor sabe cuándo vuelve
        el cupo, adivinarlo es peor.
        """
        h = self._cabeceras(error)
        retry_after = _segundos_de_cabecera(h.get("retry-after"))
        if retry_after is not None:
            return retry_after + MARGEN_RATE_LIMIT_SEC, True

        agotados = []
        for fam in ("requests", "tokens", "input-tokens", "output-tokens"):
            restante = h.get(f"anthropic-ratelimit-{fam}-remaining")
            reset = parse_reset(h.get(f"anthropic-ratelimit-{fam}-reset", ""))
            if restante is not None and reset is not None and restante.strip() == "0":
                agotados.append(reset)
        agotados = [s for s in agotados if s > 0]
        if agotados:
            return max(agotados) + MARGEN_RATE_LIMIT_SEC, True

        for fam in ("tokens", "requests", "input-tokens", "output-tokens"):
            segundos = parse_reset(h.get(f"anthropic-ratelimit-{fam}-reset", ""))
            if segundos and segundos > 0:
                return segundos + MARGEN_RATE_LIMIT_SEC, True

        m = re.search(r"try again in\s+([\d.]+)\s*s", str(error), re.IGNORECASE)
        indicado = _segundos_de_cabecera(m.group(1)) if m else None
        if indicado is not None:
            return indicado + MARGEN_RATE_LIMIT_SEC, True
        return 60.0, False

    def espera_rate_limit(self, error: Any) -> float:
        return self.espera_rate_limit_detalle(error)[0]

    def espera_error_servidor(self, error: Any, intentos: int) -> float:
        """Backoff para 5xx/timeouts: *full jitter* sobre un techo exponencial.

        Un jitter aditivo pequeño deja a todos los hilos reintentando casi a la
        vez y vuelven a tumbar al servidor. Sortear en todo el intervalo [0, techo]
        los desincroniza.
        """
        retry_after = _segundos_de_cabecera(self._cabeceras(error).get("retry-after"))
        if retry_after is not None:
            return retry_after + 5
        techo = min(30 * (2 ** min(max(intentos - 1, 0), 6)), MAX_BACKOFF_SECONDS)
        return max(1.0, random.uniform(0, techo))

    def esperar(self, segundos: float, cb: Callbacks) -> bool:
        """Espera cancelable. False si se interrumpió (parada o hora límite)."""
        if not math.isfinite(segundos):
            segundos = 60.0  # un valor que no es un tiempo no puede dejar el programa esperando para siempre
        segundos = max(0.0, segundos)
        fin = time.monotonic() + segundos
        cb.emit("tick", segundos, segundos)
        while True:
            restante = fin - time.monotonic()
            if restante <= 0:
                return True
            if self._debe_parar():
                return False
            if self.evento_detener.wait(min(0.5, restante)):
                return False
            cb.emit("tick", segundos, max(0.0, fin - time.monotonic()))

    # ------------------------------------------------------------------
    # Parada
    # ------------------------------------------------------------------

    def detener(self) -> None:
        self.marcar_parada(StopReason.STOPPED)

    def marcar_parada(self, razon: StopReason) -> None:
        """Fija el motivo de parada y despierta a todos los hilos.

        Solo escribe si no hay ya un motivo más específico (X5): un hilo que
        despierta de su backoff veía la parada y la degradaba a STOPPED.
        """
        with self._cond:
            actual = self.stop_reason
            if actual is None or (actual in _DEBILES and razon not in _DEBILES):
                self.stop_reason = razon
            self.evento_detener.set()
            self._cond.notify_all()

    def _debe_parar(self) -> bool:
        if self.deadline is not None and datetime.now() >= self.deadline:
            self.marcar_parada(StopReason.DEADLINE)
            return True
        if self.evento_detener.is_set():
            if self.stop_reason is None:
                self.marcar_parada(StopReason.STOPPED)
            return True
        return False

    # ------------------------------------------------------------------
    # Presupuesto
    # ------------------------------------------------------------------

    def tokens_entrada_estimados(self, texto: str) -> int:
        return estimar_tokens(texto) + estimar_tokens(self.config.system_prompt)

    def estimar_coste_maximo(self, msg: JobMessage, lote: bool = False) -> Decimal:
        mult = (
            ESCRITURA_CACHE_5M
            if (self.config.usar_cache and self.config.system_prompt.strip())
            else Decimal(1)
        )
        return coste_maximo(
            self.tokens_entrada_estimados(msg.text), self.config.max_tokens, self.precios, lote, mult
        )

    def gasto_actual(self) -> Decimal:
        with self._lock_estado:
            return self.estado.gasto_acumulado_usd

    def restante_del_tope(self) -> Optional[Decimal]:
        tope = self.config.tope_gasto_usd
        if tope <= 0:
            return None
        return max(Decimal(0), tope - self.gasto_actual() - self._incierto)

    def _reservar(self, coste_max: Decimal, cb: Callbacks) -> bool:
        """Reserva el peor caso de una petición. False si el tope impide enviarla.

        Si no cabe pero hay otras peticiones en vuelo, se espera a que terminen:
        casi siempre cuestan bastante menos que su peor caso y liberan margen.
        """
        tope = self.config.tope_gasto_usd
        if tope <= 0 or not self.config.estimar_antes_de_enviar:
            return True
        with self._cond:
            while True:
                if self.evento_detener.is_set():
                    return False
                gastado = self.estado.gasto_acumulado_usd + self._incierto
                if gastado + self._reservado + coste_max <= tope:
                    self._reservado += coste_max
                    return True
                if self._reservado == 0:
                    dudoso = (
                        f" (incluye hasta {formatear_usd(self._incierto)} de peticiones que agotaron el tiempo "
                        "y podrían haberse cobrado)" if self._incierto > 0 else ""
                    )
                    texto = (
                        f"TOPE DE GASTO: la siguiente orden podría costar hasta {formatear_usd(coste_max)} "
                        f"y solo quedan {formatear_usd(max(Decimal(0), tope - gastado))} de tu tope de "
                        f"{formatear_usd(tope, 2)}{dudoso}. Lote detenido sin enviarla. "
                        "Sube el tope o baja max_tokens."
                    )
                    logger.warning(texto)
                    cb.emit("error", texto)
                    self._avisar("Tope de gasto", texto)
                    self.marcar_parada(StopReason.BUDGET)
                    return False
                self._cond.wait(timeout=1.0)

    def _liberar(self, coste_max: Decimal) -> None:
        with self._cond:
            self._reservado = max(Decimal(0), self._reservado - coste_max)
            self._cond.notify_all()

    def _comprobar_tope(self, cb: Callbacks) -> bool:
        """True si se puede seguir. Se llama DESPUÉS de persistir la respuesta."""
        tope = self.config.tope_gasto_usd
        if tope <= 0:
            return True
        gasto = self.gasto_actual()
        if gasto < tope:
            return True
        texto = (
            f"TOPE DE GASTO alcanzado: {formatear_usd(gasto)} / {formatear_usd(tope, 2)}. "
            "Lote detenido para proteger tu saldo. Todo lo ya pagado está guardado."
        )
        logger.critical(texto)
        cb.emit("error", texto)
        self._avisar("Tope de gasto", texto)
        self.marcar_parada(StopReason.BUDGET)
        return False

    # ------------------------------------------------------------------
    # Registro de resultados
    # ------------------------------------------------------------------

    def registrar_respuesta(
        self,
        msg: JobMessage,
        response: Any,
        ciclo: int,
        cb: Callbacks,
        latency_ms: int = 0,
        lote: bool = False,
        reserva: Decimal = Decimal(0),
        comprobar_tope: bool = True,
        guardar_ya: bool = True,
    ) -> bool:
        """Guarda una respuesta ya recibida (y pagada). True si se puede seguir.

        El orden importa: primero se contabiliza y se escribe a disco, y solo
        después se mira el tope. Al revés, la respuesta que cruza el tope se
        perdía habiendo sido facturada.

        `reserva` es el peor caso que se reservó antes de enviar: se canjea por el
        coste real en el mismo instante en que se suma el gasto, sin dejar un hueco
        en el que otro hilo pueda reservar de más.

        `guardar_ya=False` (la recogida de un lote) deja para un punto de control el guardado del estado del lote y
        del gasto: reescribirlos enteros con cada uno de 100 000 resultados costaría más de lo que pesa el lote.
        """
        canjeada = [False]
        try:
            return self._registrar(
                msg, response, ciclo, cb, latency_ms, lote, reserva, canjeada, comprobar_tope, guardar_ya
            )
        finally:
            if reserva and not canjeada[0]:  # algo falló antes de canjearla: no dejarla colgada
                self._liberar(reserva)

    def _registrar(
        self,
        msg: JobMessage,
        response: Any,
        ciclo: int,
        cb: Callbacks,
        latency_ms: int,
        lote: bool,
        reserva: Decimal,
        canjeada: list,
        comprobar_tope: bool,
        guardar_ya: bool = True,
    ) -> bool:
        consumo = leer_consumo(getattr(response, "usage", None))
        coste = coste_consumo(consumo, self.precios, lote)
        stop = getattr(response, "stop_reason", None)
        rechazada = stop == "refusal"

        bloques = [
            b.text
            for b in (getattr(response, "content", None) or [])
            if isinstance(getattr(b, "text", None), str)
        ]
        texto = limpiar_sustitutos("\n".join(bloques).strip())
        truncada = stop in ("max_tokens", "model_context_window_exceeded")

        if rechazada:
            categoria = _categoria_rechazo(response)
            etiqueta = f" ({categoria})" if categoria else ""
            detalle = f"El modelo se negó a responder{etiqueta}."
            texto = (texto + "\n\n" if texto else "") + f"[{detalle}]"
        else:
            detalle = ""
            if not texto:
                texto = RESPUESTA_VACIA
            if truncada:
                texto += f"\n\n{AVISO_TRUNCADA}"

        with self._cond:
            self.estado.gasto_acumulado_usd += coste
            self.estado.tokens_input_total += consumo.entrada_total
            self.estado.tokens_output_total += consumo.salida
            if not rechazada:
                self.estado.exitos += 1
                self.estado.ultimo_exito = datetime.now(timezone.utc).isoformat()
            estado_guardado = self.estado.guardar() if guardar_ya else True
            gasto = self.estado.gasto_acumulado_usd
            if reserva:  # canje atómico: peor caso reservado -> coste real
                self._reservado = max(Decimal(0), self._reservado - reserva)
                canjeada[0] = True
                self._cond.notify_all()

        error_de_disco: Optional[BaseException] = None
        contenido = f"{MARCA_SOLICITUD}\n{msg.text}\n\n{MARCA_RESPUESTA}\n{texto}\n"
        try:
            write_atomic(self.job.response_path(msg.id, ciclo), contenido)
        except OSError as error:
            logger.exception("No se pudo guardar la respuesta de la orden %s", msg.id)
            error_de_disco = error
            self.job.respuestas_sin_guardar[msg.id] = (ciclo, contenido)  # pagada: que se pueda ver, copiar y volcar luego

        msg.status = STATUS_REFUSED if rechazada else STATUS_COMPLETED
        msg.truncated = truncada and not rechazada
        msg.tokens_input = consumo.entrada_total
        msg.tokens_output = consumo.salida
        msg.tokens_cache_read = consumo.lectura_cache
        msg.coste_usd += coste
        msg.latency_ms = latency_ms
        msg.detalle = detalle
        msg.error_type = "refusal" if rechazada else ""
        lote_guardado = self.job.save() if guardar_ya else True

        cb.emit("resultado", msg)
        cb.emit("gasto", gasto, consumo)
        sin_guardar = [
            nombre for nombre, bien in (
                ("la respuesta", error_de_disco is None),
                ("el estado del lote", lote_guardado),
                ("el gasto acumulado", estado_guardado),
            ) if not bien
        ]
        if sin_guardar:
            self._parar_por_disco(cb, msg, sin_guardar, error_de_disco)
            return False
        # En un lote ya está todo pagado en el servidor: el tope se comprueba una vez al final.
        return self._comprobar_tope(cb) if comprobar_tope else True

    def volcar_respuestas_sin_guardar(self) -> bool:
        """Escribe en disco las respuestas pagadas que antes no cupieron. True si no queda ninguna por escribir."""
        pendientes = self.job.respuestas_sin_guardar
        for msg_id, (ciclo, contenido) in list(pendientes.items()):
            try:
                write_atomic(self.job.response_path(msg_id, ciclo), contenido)
            except OSError:
                return False
            pendientes.pop(msg_id, None)
            logger.info("Respuesta de la orden %s guardada por fin en disco", msg_id)
        return True

    def _parar_por_disco(
        self, cb: Callbacks, msg: Optional[JobMessage], sin_guardar: list[str], error: Optional[BaseException]
    ) -> None:
        """Lo pagado no se puede escribir en disco: seguir sería pagar respuestas que se perderían."""
        with self._cond:  # mirar y marcar, de una vez: con varios hilos, solo uno debe ser «el primero»
            primera_vez = self.stop_reason is None or self.stop_reason in _DEBILES
            self.marcar_parada(StopReason.FATAL)
        if not primera_vez:
            return  # con varios hilos, los demás fallarán igual: se cuenta una sola vez
        causa = f" ({error})" if error is not None else ""
        de_la_orden = f" de la orden {msg.id}" if msg is not None else ""
        texto = (
            f"No se pudo guardar en el disco {' ni '.join(sin_guardar)}{de_la_orden}{causa}. "
            "Se detiene todo para no pagar respuestas que luego no se podrían conservar. La respuesta sigue "
            "disponible mientras el programa esté abierto: cópiala o expórtala. "
            f"Libera espacio o revisa los permisos de {DATA_DIR} y reanuda."
        )
        logger.critical(texto)
        cb.emit("error", texto)
        self._avisar("No se puede guardar", texto)

    def marcar_fallo(
        self, msg: JobMessage, detalle: str, cb: Callbacks, error_type: str = "other", guardar: bool = True
    ) -> None:
        msg.status = STATUS_FAILED
        msg.detalle = detalle
        msg.error_type = error_type
        if guardar:
            self.job.save()
        cb.emit("resultado", msg)

    def punto_de_control(self, cb: Callbacks) -> bool:
        """Escribe en disco el estado del lote y el gasto acumulado. False (y se para) si el disco no lo admite."""
        with self._cond:
            estado_ok = self.estado.guardar()
        lote_ok = self.job.save()
        if estado_ok and lote_ok:
            return True
        self._parar_por_disco(
            cb, None, [n for n, bien in (("el estado del lote", lote_ok), ("el gasto acumulado", estado_ok)) if not bien], None
        )
        return False

    # ------------------------------------------------------------------
    # Cierre
    # ------------------------------------------------------------------

    def cerrar_ciclo(self, ciclo: int, cb: Callbacks) -> None:
        """Escribe el resumen del ciclo. Se llama SIEMPRE, también al abortar:
        justo cuando algo falla es cuando más falta hace saber qué se hizo y qué se pagó."""
        total = self.job.total_count()
        completados = self.job.completed_count()
        self.volcar_respuestas_sin_guardar()  # si el disco ya admite escrituras, que no se pierda nada de lo pagado
        try:
            partes = [
                f"# Resumen del Ciclo {ciclo}",
                f"Fecha: {datetime.now().isoformat()}",
                f"Completados: {completados}/{total}",
                f"Gasto acumulado: {formatear_usd(self.gasto_actual())}",
                "",
                "---",
                "",
            ]
            for m in self.job.messages:
                partes.append(f"## Mensaje {m.id} — {m.status}")
                if m.status == STATUS_COMPLETED or m.status == STATUS_REFUSED:
                    ruta = self.job.response_path(m.id, ciclo)
                    if ruta.exists():
                        partes.append(ruta.read_text(encoding="utf-8"))
                elif m.detalle:
                    partes.append(f"Detalle: {m.detalle}")
                partes += ["", "---", ""]
            write_atomic(self.job.resumen_path(ciclo), "\n".join(partes))
        except Exception:
            logger.exception("Error creando el resumen del ciclo %s", ciclo)
        cb.emit("fin_ciclo", completados, total, ciclo)

    def _avisar(self, titulo: str, texto: str) -> None:
        if self.notificaciones:
            notificar(titulo, texto, self.config.telegram_chat_id)

    def _finalizar(self) -> StopReason:
        """Motivo final del `ejecutar`, y limpieza del estado persistente."""
        razon = self.stop_reason or StopReason.COMPLETED
        if razon in (StopReason.BUDGET, StopReason.STOPPED, StopReason.DEADLINE) and not self.job.hay_trabajo():
            razon = StopReason.COMPLETED  # se paró justo al terminar: no queda nada pendiente
        self.stop_reason = razon
        if razon == StopReason.COMPLETED and not self.config.modo_continuo and not self.job.hay_trabajo():
            with self._lock_estado:
                self.estado.current_job_id = None
                self.estado.guardar()
        return razon

    # ------------------------------------------------------------------
    # Errores de la API (comunes a los dos motores)
    # ------------------------------------------------------------------

    def _fallar(self, msg: Optional[JobMessage], detalle: str, cb: Callbacks, tipo: str) -> None:
        if msg is not None:
            self.marcar_fallo(msg, detalle, cb, tipo)

    def manejar_error(
        self,
        error: Exception,
        cb: Callbacks,
        cont: Contadores,
        msg: Optional[JobMessage] = None,
    ) -> Decision:
        """Decide qué hacer con una excepción de la API. `msg` es la orden afectada, si la hay."""
        observar_seguro(self.rate_limits, error)

        # OJO con el orden: APITimeoutError HEREDA de APIConnectionError. Con el orden
        # inverso un timeout normal se tomaba por "sin red" y abortaba el lote entero (C5).
        # (En streaming, un tiempo agotado entre trozos sale sin envolver: se reconoce por el nombre.)
        if isinstance(error, anthropic.APITimeoutError) or (
            not isinstance(error, anthropic.APIStatusError) and es_timeout(error)
        ):
            return self._manejar_timeout(msg, error, cb, cont)

        if isinstance(error, anthropic.APIConnectionError):
            if es_sin_conexion(error):
                return self._esperar_conexion(cb, cont)
            return self._parar_por_corte(msg, cb)

        if isinstance(error, anthropic.APIStatusError):
            return self._manejar_estado(analizar_error(error), error, cb, cont, msg)

        # Con la respuesta en streaming, el SDK deja pasar el corte de la conexión sin envolverlo.
        if es_corte_de_conexion(error):
            return self._parar_por_corte(msg, cb)

        # Una respuesta entera pero que no es de la API (portal cautivo, proxy…): no la procesó el servidor.
        if es_respuesta_ilegible(error):
            return self._reintentar_con_backoff(msg, error, cb, cont, "Respuesta ilegible de la API")

        logger.error("Error inesperado", exc_info=error)
        detalle = f"Error inesperado: {error}"
        self._fallar(msg, detalle, cb, "unexpected")
        cb.emit("error", detalle)
        self.marcar_parada(StopReason.FATAL)
        return Decision.PARAR

    def manejar_error_de_consulta(self, error: Exception, cb: Callbacks, cont: Contadores) -> Decision:
        """Como `manejar_error`, para una consulta que ni cuesta ni cambia nada en el servidor.

        Ver el estado de un lote, bajar sus resultados o listar los lotes recientes se puede repetir sin riesgo de
        cobrar dos veces. Por eso un corte de la conexión o un tiempo agotado se reintentan con espera (hasta el
        máximo de errores) en vez de parar: pararía justo la espera de hasta 24 h que más cuesta repetir a mano.
        Todo lo demás (429, saldo, clave, 5xx, sin conexión…) sigue la política de siempre.
        """
        if not isinstance(error, anthropic.APIStatusError):
            if isinstance(error, anthropic.APITimeoutError) or es_timeout(error):
                observar_seguro(self.rate_limits, error)
                return self._reintentar_con_backoff(None, error, cb, cont, "El servidor tardó demasiado en responder")
            if not es_sin_conexion(error) and (
                isinstance(error, anthropic.APIConnectionError) or es_corte_de_conexion(error)
            ):
                observar_seguro(self.rate_limits, error)
                return self._reintentar_con_backoff(None, error, cb, cont, "Se cortó la conexión con el servidor")
        return self.manejar_error(error, cb, cont)

    def _parar_por_corte(self, msg: Optional[JobMessage], cb: Callbacks) -> Decision:
        """La conexión se cortó a mitad: no sabemos si el servidor llegó a procesar la petición.

        Parar evita facturar un duplicado.
        """
        detalle = "Se cortó la conexión con la API. Lote pausado para evitar cobros duplicados."
        self._fallar(msg, detalle, cb, "connection")
        cb.emit("error", detalle)
        self.marcar_parada(StopReason.NETWORK)
        return Decision.PARAR

    def _manejar_timeout(self, msg: Optional[JobMessage], error: Exception, cb: Callbacks, cont: Contadores) -> Decision:
        """Se agotó el tiempo. Si la petición ya había salido, el servidor pudo procesarla y cobrarla."""
        if timeout_con_peticion_enviada(error):
            cont.timeouts_enviados += 1
            if cont.timeouts_enviados >= MAX_TIMEOUTS_POR_ORDEN:
                donde = f" en la orden {msg.id}" if msg is not None else ""
                detalle = (
                    f"La API no responde a tiempo{donde} (se agotó el tiempo {cont.timeouts_enviados} veces con la "
                    "petición ya enviada). Se para para no cobrar lo mismo varias veces: sube el tiempo máximo por "
                    "petición o baja max_tokens."
                )
                self._fallar(msg, detalle, cb, "timeout")
                cb.emit("error", detalle)
                self._avisar("Lote pausado", detalle)
                self.marcar_parada(StopReason.NETWORK)
                return Decision.PARAR
        return self._reintentar_con_backoff(msg, error, cb, cont, "Timeout de la API")

    def _anotar_incierto(self, coste_max: Decimal, msg: Optional[JobMessage]) -> None:
        """Una petición se abandonó por tiempo con el servidor posiblemente trabajando: su peor caso cuenta como gasto."""
        with self._cond:
            self._incierto += coste_max
            if msg is not None:
                self._con_timeout.add(msg.id)  # el reintento va en streaming: no tiene un tiempo total que agotar
        logger.warning("Petición abandonada por tiempo: se cuenta hasta %s como posible gasto", formatear_usd(coste_max))

    def _manejar_estado(
        self,
        info: ErrorApi,
        error: Exception,
        cb: Callbacks,
        cont: Contadores,
        msg: Optional[JobMessage],
    ) -> Decision:
        c = info.categoria
        etiqueta = f"[Orden {msg.id}] " if msg is not None else ""

        if c is Categoria.RATE_LIMIT:
            segundos, con_info = self.espera_rate_limit_detalle(error)
            cont.sin_info_429 = 0 if con_info else cont.sin_info_429 + 1
            if cont.sin_info_429 > MAX_429_SIN_INFO:
                self._parar_por_limite(info, cb, repetido=True)
                return Decision.PARAR
            cb.emit("espera", "rate_limit", segundos)
            cb.emit(
                "estado",
                f"{etiqueta}Límite de la API. Reintento en {int(segundos)}s (no cuenta como fallo)",
                False,
            )
            return Decision.REINTENTAR if self.esperar(segundos, cb) else Decision.PARAR

        if c is Categoria.LIMITE_CUENTA:
            self._parar_por_limite(info, cb)
            return Decision.PARAR

        if c is Categoria.SIN_SALDO:
            texto = (
                "Sin saldo en tu cuenta de Anthropic. Recarga créditos en la consola (Billing) y "
                "reanuda: la orden queda pendiente y lo ya pagado está guardado."
            )
            logger.critical(texto)
            cb.emit("error", texto)
            self._avisar("Saldo agotado", texto)
            self.marcar_parada(StopReason.NO_CREDIT)
            return Decision.PARAR

        if c is Categoria.SERVIDOR:
            que = f"Error de servidor {info.status or ''}".strip()
            return self._reintentar_con_backoff(msg, error, cb, cont, que)

        if c is Categoria.DEMASIADO_GRANDE and msg is not None:
            # Es culpa de esta orden, no del resto: se marca y se sigue con las demás.
            detalle = f"La orden es demasiado grande para la API (413). {info.mensaje}"
            self.marcar_fallo(msg, detalle, cb, c.value)
            cb.emit("error", detalle)
            return Decision.SIGUIENTE

        textos = {
            Categoria.AUTH: (
                f"Error de autenticación ({info.status}): la API key no es válida o no tiene permiso. "
                "Revísala en la consola de Anthropic."
            ),
            Categoria.MODELO: (
                f"El modelo '{self.config.modelo}' no existe o ya no está disponible. "
                "Elige otro (botón «Cargar modelos»)."
            ),
            Categoria.PETICION: f"La API rechazó la petición ({info.status}): {info.mensaje}",
        }
        detalle = textos.get(c, f"Error HTTP {info.status}: {info.mensaje}")
        self._fallar(msg, detalle, cb, c.value)
        cb.emit("error", detalle)
        self.marcar_parada(StopReason.FATAL)
        return Decision.PARAR

    def _parar_por_limite(self, info: ErrorApi, cb: Callbacks, repetido: bool = False) -> None:
        """Parada por un tope de gasto de la cuenta. La orden NO se marca como fallida."""
        if repetido:
            cuerpo = (
                "La API sigue rechazando las peticiones por límite sin indicar cuándo se levanta. "
                "Lo más probable es un tope de gasto: revisa Billing en la consola de Anthropic."
            )
        elif info.status == 429:
            cuando = (
                f"El acceso se restablece el {info.reanudacion}."
                if info.reanudacion
                else "El acceso vuelve a principios del mes siguiente."
            )
            cuerpo = (
                "Tu organización ha alcanzado el tope mensual de gasto de su nivel de uso. "
                f"{cuando} También puedes pedir un nivel superior en la consola."
            )
        else:
            cuerpo = (
                "Has alcanzado el límite de gasto que fijaste en la consola de Anthropic. "
                "Súbelo o quítalo en Billing → Spend limits para continuar."
            )
        texto = f"{cuerpo} Lo ya pagado está guardado y la orden queda pendiente. (API: «{info.mensaje}»)"
        logger.critical(texto)
        cb.emit("error", texto)
        self._avisar("Límite de gasto de la cuenta", cuerpo)
        self.marcar_parada(StopReason.ACCOUNT_LIMIT)

    def _reintentar_con_backoff(
        self,
        msg: Optional[JobMessage],
        error: Exception,
        cb: Callbacks,
        cont: Contadores,
        que: str,
    ) -> Decision:
        if msg is not None:
            msg.errores += 1
            self.job.save()
            errores = msg.errores
        else:
            cont.errores += 1
            errores = cont.errores
        if errores >= self.config.max_error_attempts:
            donde = f" en la orden {msg.id}" if msg is not None else ""
            detalle = f"{que}. Máximo de errores ({errores}){donde}: lote pausado."
            self._fallar(msg, detalle, cb, "max_retries")
            cb.emit("error", detalle)
            self._avisar("Lote pausado", detalle)
            self.marcar_parada(StopReason.FATAL)
            return Decision.PARAR
        segundos = self.espera_error_servidor(error, errores)
        cb.emit("espera", "server", segundos)
        etiqueta = f"[Orden {msg.id}] " if msg is not None else ""
        cb.emit(
            "estado",
            f"{etiqueta}{que}. Reintento en {int(segundos)}s (fallo {errores}/{self.config.max_error_attempts})",
            False,
        )
        return Decision.REINTENTAR if self.esperar(segundos, cb) else Decision.PARAR

    def _esperar_conexion(self, cb: Callbacks, cont: Contadores) -> Decision:
        """Sin conexión: la petición no salió, así que reintentar no puede duplicar un cobro."""
        paso = float(min(5 * (2 ** cont.intentos_sin_conexion), 60))
        cont.intentos_sin_conexion += 1
        cont.segundos_sin_conexion += paso
        if cont.segundos_sin_conexion > MAX_SIN_CONEXION_SEC:
            texto = (
                "Sin conexión con la API durante más de 10 minutos. Lote pausado; "
                "reanúdalo cuando vuelva internet."
            )
            cb.emit("error", texto)
            self._avisar("Sin conexión", texto)
            self.marcar_parada(StopReason.NETWORK)
            return Decision.PARAR
        cb.emit("espera", "sin_conexion", paso)
        cb.emit("estado", f"Sin conexión con la API. Reintento en {int(paso)}s…", False)
        return Decision.REINTENTAR if self.esperar(paso, cb) else Decision.PARAR


# ============================================================================
# 16. Motor inmediato (Messages API)
#    (claude_batch_monitor/core/sequential.py)
# ============================================================================
#
# Motor inmediato: una petición a la Messages API por orden, en secuencia o en paralelo.


class SequentialEngine(EngineBase):
    """Procesa el lote orden a orden contra la API de mensajes."""

    def __init__(self, config: Configuracion, job: Job):
        super().__init__(config, job)

    # ------------------------------------------------------------------
    # Bucle principal
    # ------------------------------------------------------------------

    def ejecutar(self, cb: Optional[Callbacks] = None) -> StopReason:
        cb = cb or Callbacks()
        try:
            self.inicializar_cliente()
        except Exception as error:
            cb.emit("error", str(error))
            self.marcar_parada(StopReason.FATAL)
            return self._finalizar()

        self.deadline = self.calcular_deadline()
        logger.info(
            "Lote %s iniciado: %d órdenes, modelo %s, tope %s",
            self.job.job_id, self.job.total_count(), self.config.modelo, self.config.tope_gasto_usd,
        )
        try:
            while True:
                ciclo = self.job.ciclos_completados + 1
                self._ejecutar_ciclo(ciclo, cb)
                # Siempre, también al abortar (M12): el resumen es justo lo que más
                # falta hace cuando algo ha salido mal.
                self.cerrar_ciclo(ciclo, cb)

                if self.stop_reason is not None or not self.config.modo_continuo:
                    break
                if self._debe_parar():
                    break
                self.job.reset_for_new_cycle()
                self._avisar("Ciclo completado", f"Ciclo {ciclo} terminado. Empieza el {ciclo + 1}.")
                cb.emit("estado", f"Ciclo {ciclo} completado. Pausa larga...", False)
                if not self.esperar(self.config.pausa_ciclos_sec, cb):
                    break
        finally:
            self.cerrar_cliente()
        return self._finalizar()

    def _ejecutar_ciclo(self, ciclo: int, cb: Callbacks) -> None:
        pendientes = self.job.pendientes()
        if not pendientes:
            return
        if self.config.concurrencia <= 1:
            for i, msg in enumerate(pendientes):
                if self._debe_parar():
                    return
                if not self._procesar_mensaje(msg, ciclo, cb):
                    return
                if i < len(pendientes) - 1 and not self._pausa_entre_ordenes(cb):
                    return
            return

        with ThreadPoolExecutor(max_workers=self.config.concurrencia) as pool:
            futuros = [pool.submit(self._tarea, msg, ciclo, cb) for msg in pendientes]
            for f in futuros:
                f.result()  # `_tarea` no lanza: ver más abajo

    def _tarea(self, msg: JobMessage, ciclo: int, cb: Callbacks) -> None:
        """Una orden dentro del pool. Nunca deja escapar una excepción (X6): relanzada
        al consumir el resultado del pool, mataba el hilo del motor con el lote a medias."""
        if self._debe_parar():
            return
        try:
            if not self._procesar_mensaje(msg, ciclo, cb) and self.stop_reason is None:
                self.marcar_parada(StopReason.STOPPED)
        except Exception as error:
            logger.exception("Error inesperado en la orden %s", msg.id)
            self.marcar_fallo(msg, f"Error inesperado: {error}", cb, "unexpected")
            cb.emit("error", f"Error inesperado: {error}")
            self.marcar_parada(StopReason.FATAL)

    def _pausa_entre_ordenes(self, cb: Callbacks) -> bool:
        d = self.config.pausa_entre_mensajes_sec
        if d <= 0:
            return True
        cb.emit("estado", f"Pausa de cortesía ({d}s)...", False)
        return self.esperar(d, cb)

    # ------------------------------------------------------------------
    # Una orden
    # ------------------------------------------------------------------

    def _procesar_mensaje(self, msg: JobMessage, ciclo: int, cb: Callbacks) -> bool:
        """Procesa UNA orden hasta terminarla. False si hay que abortar todo el lote.

        Los 429 transitorios reintentan sin límite (esperando lo que diga la API)
        y NO suman a `msg.errores`; solo los errores reales de servidor/red cuentan
        hacia `max_error_attempts`.
        """
        total = self.job.total_count()
        cont = Contadores()
        while True:
            if self._debe_parar() or not self._throttle_preventivo(cb):
                return False
            coste_max = self.estimar_coste_maximo(msg)
            if not self._reservar(coste_max, cb):
                return False

            cb.emit("inicio_orden", msg)
            cb.emit(
                "estado",
                f"[Ciclo {ciclo} | Orden {msg.id}/{total}] Enviando (fallos: {msg.errores})",
                True,
            )
            try:
                respuesta, ms = self._llamar_api(msg, cb)
            except Exception as error:
                self._liberar(coste_max)
                if es_timeout(error) and timeout_con_peticion_enviada(error):
                    self._anotar_incierto(coste_max, msg)
                decision = self.manejar_error(error, cb, cont, msg)
                if decision is Decision.PARAR:
                    return False
                if decision is Decision.SIGUIENTE:
                    return True
                continue
            return self.registrar_respuesta(msg, respuesta, ciclo, cb, ms, reserva=coste_max)

    @staticmethod
    def _mensaje_final(stream: Any) -> Any:
        """El mensaje entero de un streaming, o `FlujoIncompleto` si lo recibido no llegó a serlo.

        Sin esto, un flujo vacío (un proxy que contesta con una página, una conexión cerrada antes de empezar) salía
        como un `AssertionError` del SDK y se tomaba por un fallo inesperado que paraba todo; y uno cortado a mitad
        podía darse por bueno con la respuesta a medias.
        """
        try:
            respuesta = stream.get_final_message()
        except AssertionError as error:
            try:
                era_un_flujo = "text/event-stream" in str(stream.response.headers.get("content-type", ""))
            except Exception:
                era_un_flujo = True  # si no se puede saber, se supone lo peor: pudo procesarse
            raise FlujoIncompleto(era_un_flujo) from error
        if getattr(respuesta, "stop_reason", "") is None:
            raise FlujoIncompleto(True)  # empezó y no llegó el final del mensaje
        return respuesta

    def _llamar_api(self, msg: JobMessage, cb: Callbacks) -> tuple[Any, int]:
        """Una llamada a la API. Devuelve (respuesta, latencia en ms)."""
        params = build_request_params(self.config, msg.text)
        extra: dict[str, Any] = {}
        # `output_config` no existe como argumento con nombre en el SDK anterior a
        # 0.7x (daba TypeError); `extra_body` existe en todas y se fusiona en el JSON.
        if "output_config" in params:
            extra["extra_body"] = {"output_config": params.pop("output_config")}

        inicio = time.monotonic()
        if self.config.usa_streaming or msg.id in self._con_timeout:
            with self.client.messages.stream(**params, **extra) as stream:
                observar_seguro(self.rate_limits, stream)
                for trozo in stream.text_stream:
                    cb.emit("delta", msg.id, trozo)
                respuesta = self._mensaje_final(stream)
        else:
            # Respuesta cruda: las cabeceras de cuota se leen en TODAS las respuestas,
            # no solo al recibir un 429, para frenar antes de chocar con el límite.
            cruda = self.client.messages.with_raw_response.create(**params, **extra)
            observar_seguro(self.rate_limits, cruda)
            respuesta = cruda.parse()
        return respuesta, int((time.monotonic() - inicio) * 1000)

    def _throttle_preventivo(self, cb: Callbacks) -> bool:
        """Frena si la cuota va justa. False si hay que salir durante la espera."""
        espera = self.rate_limits.espera_recomendada()
        if espera <= 0:
            return True
        cb.emit("espera", "cuota", espera)
        cb.emit(
            "estado",
            f"Cuota casi agotada ({self.rate_limits.resumen()}). Frenando {espera:.1f}s antes de seguir.",
            False,
        )
        return self.esperar(espera, cb)


# ============================================================================
# 17. Motor de lote (Message Batches API, mitad de precio)
#    (claude_batch_monitor/core/batch.py)
# ============================================================================
#
# Motor de lote: Message Batches API.
#
# 50 % más barato en todos los tokens, hasta 100 000 órdenes (o 256 MB) por lote,
# resultados normalmente en menos de una hora y como máximo en 24. A cambio no se
# ve nada hasta que termina, y no hay forma de parar a mitad sin cancelar el lote.
#
# Dos consecuencias para el gasto:
# - El tope se comprueba ANTES de enviar contra el peor caso de todo el lote, porque
#   una vez enviado no se puede frenar. Por eso, aquí, bajar `max_tokens` es la
#   palanca que hace caber un lote en un tope.
# - Los resultados llegan SIN ORDEN: se emparejan siempre por `custom_id`.
#
# Referencia: https://platform.claude.com/docs/en/build-with-claude/batch-processing


LIMITE_ORDENES = 100_000
LIMITE_BYTES = 256 * 1024 * 1024
SOBRECARGA_POR_PETICION = 400  # bytes de JSON alrededor de cada orden
INTERVALO_MIN_SEC = 10.0
INTERVALO_MAX_SEC = 60.0
# Para reconocer un lote que el servidor creó sin que nos llegara su respuesta.
HOLGURA_RELOJES = timedelta(minutes=10)  # lo que pueden diferir el reloj de esta máquina y el del servidor
MAX_LOTES_A_MIRAR = 200
# Al recoger un lote, el estado y el gasto se escriben a intervalos, no con cada resultado: son del tamaño de TODO el lote.
RESULTADOS_ENTRE_PUNTOS_DE_CONTROL = 200
SEGUNDOS_ENTRE_PUNTOS_DE_CONTROL = 20.0


class _Busqueda(Enum):
    ENCONTRADO = "found"     # el servidor sí lo creó: se sigue con ese
    SIN_RASTRO = "none"      # no hay ningún lote así: se puede enviar
    PARAR = "stop"           # no se pudo comprobar, o hay dudas: no se envía


def custom_id(msg: JobMessage) -> str:
    """Id estable de una orden dentro del lote (`^[a-zA-Z0-9_-]{1,64}$`)."""
    return f"m{msg.id:06d}"


def _fecha(valor: Any) -> Optional[datetime]:
    """Una fecha de la API (datetime o texto ISO) como datetime con zona horaria; None si no se entiende."""
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(valor, datetime):
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)


def _ordenes_del_lote(lote: Any) -> int:
    contadores = getattr(lote, "request_counts", None)
    return sum(
        int(getattr(contadores, clave, 0) or 0)
        for clave in ("processing", "succeeded", "errored", "canceled", "expired")
    )


def _cerrado(cliente: Any) -> bool:
    try:
        return bool(cliente.is_closed())
    except Exception:
        return False  # un doble sin `is_closed`, o cualquier otra cosa: se usa tal cual


def _datos_del_error(resultado: Any) -> tuple[str, str]:
    """(tipo, mensaje) de un resultado `errored`; tolera las dos formas del objeto."""
    envoltorio = getattr(resultado, "error", None)
    interno = getattr(envoltorio, "error", envoltorio)
    return str(getattr(interno, "type", "") or ""), str(getattr(interno, "message", "") or "")


class BatchEngine(EngineBase):
    """Envía todas las órdenes pendientes como un único lote y recoge los resultados."""

    def __init__(self, config: Configuracion, job: Job):
        super().__init__(config, job)
        self.intervalo_min = INTERVALO_MIN_SEC
        self.intervalo_max = INTERVALO_MAX_SEC

    # ------------------------------------------------------------------

    def ejecutar(self, cb: Optional[Callbacks] = None) -> StopReason:
        cb = cb or Callbacks()
        try:
            self.inicializar_cliente()
        except Exception as error:
            cb.emit("error", str(error))
            self.marcar_parada(StopReason.FATAL)
            return self._finalizar()

        self.deadline = self.calcular_deadline()
        ciclo = self.job.ciclos_completados + 1
        try:
            if self.job.batch_id:
                cb.emit("estado", f"Reenganchando el lote {self.job.batch_id}, que sigue en el servidor…", True)
            elif self.job.pendientes():
                self._enviar(cb)
            if self.stop_reason is None and self.job.batch_id:
                self._seguir(cb, ciclo)
        finally:
            # Sin retornos tempranos: el resumen se escribe siempre (M12).
            self.cerrar_ciclo(ciclo, cb)
            self.cerrar_cliente()
        return self._finalizar()

    # ------------------------------------------------------------------
    # Llamadas con reintentos
    # ------------------------------------------------------------------

    def _llamar(
        self,
        fn: Callable[[], Any],
        cb: Callbacks,
        al_no_existir: Optional[Callable[[], None]] = None,
    ) -> Any:
        """Una consulta de solo lectura a la API (estado de un lote, lista de lotes), con reintentos.

        No cuesta nada ni cambia nada en el servidor, así que se puede repetir sin miedo a cobrar dos veces (véase
        `manejar_error_de_consulta`). Devuelve None si hay que abandonar (la razón ya está en `stop_reason`).
        """
        cont = Contadores()
        while True:
            if self._debe_parar():
                return None
            try:
                return fn()
            except anthropic.NotFoundError as error:
                if al_no_existir is not None:
                    al_no_existir()
                    return None
                if self.manejar_error(error, cb, cont) is Decision.PARAR:
                    return None
            except Exception as error:
                if self.manejar_error_de_consulta(error, cb, cont) is not Decision.REINTENTAR:
                    return None

    # ------------------------------------------------------------------
    # Envío
    # ------------------------------------------------------------------

    def _enviar(self, cb: Callbacks) -> None:
        pendientes = self.job.pendientes()
        if not pendientes:
            return

        if len(pendientes) > LIMITE_ORDENES:
            self._abortar(
                cb,
                f"Un lote admite como máximo {LIMITE_ORDENES:,} órdenes y hay {len(pendientes):,}. "
                "Divide el fichero en varios lotes.",
            )
            return
        sistema = len(self.config.system_prompt.encode("utf-8"))
        peso = sum(len(m.text.encode("utf-8")) + sistema + SOBRECARGA_POR_PETICION for m in pendientes)
        if peso > LIMITE_BYTES:
            self._abortar(
                cb,
                f"El lote pesaría ~{peso // (1024 * 1024)} MB y el máximo es 256 MB. Divídelo en varios lotes.",
            )
            return

        if not self._cabe_en_el_tope(pendientes, cb):
            return

        peticiones = [
            {"custom_id": custom_id(m), "params": build_request_params(self.config, m.text)}
            for m in pendientes
        ]
        cb.emit("estado", f"Enviando {len(pendientes):,} órdenes como un lote…", True)
        self._crear_lote(peticiones, cb)

    # ------------------------------------------------------------------
    # Crear el lote sin crear dos
    # ------------------------------------------------------------------

    def _crear_lote(self, peticiones: list[dict], cb: Callbacks) -> bool:
        """Crea el lote en el servidor y apunta su id. Nunca crea dos por un mismo envío.

        Si la petición pudo llegar pero no su respuesta (tiempo agotado, corte, un 5xx, o el programa se cerró en
        pleno envío), el servidor pudo crear —y cobrar— el lote sin que lo supiéramos, y repetir el envío
        crearía otro. Por eso el envío se apunta en disco ANTES de hacerlo, y antes de repetirlo (en esta
        ejecución o en la siguiente) se busca ese lote entre los recientes de la cuenta.
        """
        cont = Contadores()
        n = len(peticiones)
        if self.job.envio_incierto_desde:  # un envío anterior quedó sin respuesta
            busqueda = self._buscar_envio_anterior(cb)
            if busqueda is not _Busqueda.SIN_RASTRO:
                return busqueda is _Busqueda.ENCONTRADO
        while True:
            if self._debe_parar():
                return False
            if not self.job.envio_incierto_desde:
                # Antes de apuntarlo, se anota cuál es el último lote de la cuenta: lo que aparezca después de él
                # es lo único que puede ser el nuestro (no depende de que los relojes coincidan).
                self.job.envio_incierto_tras = self._ultimo_lote_de_la_cuenta()
                self.job.envio_incierto_desde = datetime.now(timezone.utc).isoformat()
                self.job.envio_incierto_ordenes = n
                self.job.save()
            try:
                lote = self.client.messages.batches.create(requests=peticiones)
            except Exception as error:
                if not pudo_haberse_procesado(error):
                    self._olvidar_envio()  # el servidor ni lo vio (o lo rechazó): no hay nada que buscar
                if self.manejar_error(error, cb, cont) is not Decision.REINTENTAR:
                    return False
                if self.job.envio_incierto_desde:  # ya esperó lo que tocaba: ¿llegó a crearse?
                    busqueda = self._buscar_envio_anterior(cb)
                    if busqueda is not _Busqueda.SIN_RASTRO:
                        return busqueda is _Busqueda.ENCONTRADO
                continue
            self._fijar_lote(lote, n, False, cb)
            return True

    def _ultimo_lote_de_la_cuenta(self) -> Optional[str]:
        """Id del lote más reciente de la cuenta ("" si no hay ninguno); None si no se pudo mirar."""
        try:
            for lote in self.client.messages.batches.list(limit=1):
                return str(lote.id)
            return ""
        except Exception:
            logger.warning("No se pudo mirar cuál es el último lote de la cuenta", exc_info=True)
            return None  # sin ese dato, la búsqueda se limita por la fecha (con holgura para los relojes)

    def _olvidar_envio(self) -> None:
        if self.job.envio_incierto_desde:
            self.job.envio_incierto_desde = ""
            self.job.envio_incierto_ordenes = 0
            self.job.envio_incierto_tras = None
            self.job.save()

    def _fijar_lote(self, lote: Any, n: int, adoptado: bool, cb: Callbacks) -> None:
        self.job.batch_id = str(lote.id)
        self.job.envio_incierto_desde = ""
        self.job.envio_incierto_ordenes = 0
        self.job.envio_incierto_tras = None
        self.job.save()
        if adoptado:
            texto = f"El envío anterior sí había creado el lote {lote.id} ({n:,} órdenes): sigo con ese en vez de enviar otro."
            logger.warning(texto)
        else:
            texto = f"Lote enviado ({n:,} órdenes). Id: {lote.id}"
            logger.info("Lote %s enviado: %d órdenes (id del servidor %s)", self.job.job_id, n, lote.id)
        cb.emit("estado", texto, True)

    def _buscar_envio_anterior(self, cb: Callbacks) -> _Busqueda:
        """¿Llegó a crear el servidor el lote de un envío que quedó sin respuesta?

        Se busca entre los lotes de la cuenta creados después del último que había al enviar (si se anotó cuál era;
        si no, entre los de los últimos minutos, con holgura para los relojes) uno con el mismo número de órdenes
        y que no sea ya de otra tarea nuestra. Si hay exactamente uno, es el nuestro.
        """
        desde = _fecha(self.job.envio_incierto_desde)
        n = self.job.envio_incierto_ordenes
        if desde is None or n <= 0:
            self._olvidar_envio()
            return _Busqueda.SIN_RASTRO
        desde -= HOLGURA_RELOJES
        tras = self.job.envio_incierto_tras
        cb.emit("estado", "Comprobando si el envío anterior llegó a crear el lote…", True)

        def recientes() -> list:
            salida: list = []
            for lote in self.client.messages.batches.list(limit=100):  # del más nuevo al más viejo
                if tras is not None:
                    if str(lote.id) == tras:
                        break  # de aquí hacia atrás ya existía antes de enviar: no puede ser el nuestro
                else:
                    creado = _fecha(getattr(lote, "created_at", None))
                    if creado is not None and creado < desde:
                        break
                salida.append(lote)
                if len(salida) >= MAX_LOTES_A_MIRAR:
                    break
            return salida

        vistos = self._llamar(recientes, cb)
        if vistos is None:
            return _Busqueda.PARAR  # no se pudo comprobar: sin saberlo, no se envía otro
        ajenos = {d["batch_id"] for d in listar_jobs() if d["batch_id"] and d["job_id"] != self.job.job_id}
        candidatos = [
            lote for lote in vistos
            if (tras is not None or _fecha(getattr(lote, "created_at", None)) is not None)  # sin línea base, hace falta fecha
            and _ordenes_del_lote(lote) == n and str(lote.id) not in ajenos
        ]
        if not candidatos:
            self._olvidar_envio()
            return _Busqueda.SIN_RASTRO
        if len(candidatos) == 1:
            self._fijar_lote(candidatos[0], n, True, cb)
            return _Busqueda.ENCONTRADO

        ids = ", ".join(str(lote.id) for lote in candidatos[:5])
        texto = (
            f"El envío anterior quedó sin respuesta y hay {len(candidatos)} lotes recientes de {n:,} órdenes en tu "
            f"cuenta ({ids}): no sé cuál es el tuyo y enviar otro podría cobrarte dos veces. Míralos en la consola "
            "de Anthropic (Batches). Si ninguno es el tuyo, pulsa Reanudar otra vez y se enviará uno nuevo."
        )
        self._olvidar_envio()  # el aviso se da una vez; la siguiente reanudación ya es decisión de la persona
        logger.warning(texto)
        cb.emit("error", texto)
        self.marcar_parada(StopReason.FATAL)
        return _Busqueda.PARAR

    def _cabe_en_el_tope(self, pendientes: list[JobMessage], cb: Callbacks) -> bool:
        """El peor caso del lote entero debe caber en lo que queda del tope."""
        restante = self.restante_del_tope()
        if restante is None or not self.config.estimar_antes_de_enviar:
            return True
        peor = sum((self.estimar_coste_maximo(m, lote=True) for m in pendientes), Decimal(0))
        if peor <= restante:
            return True

        # Cuántos max_tokens de salida cabrían, grosso modo, repartidos entre las órdenes.
        pista = ""
        n = Decimal(len(pendientes))
        entrada_total = peor - n * self._coste_salida_lote(self.config.max_tokens)
        por_token = n * self._coste_salida_lote(1)
        if por_token > 0 and restante > entrada_total:
            cabrian = int((restante - entrada_total) / por_token)
            if cabrian >= 1:
                pista = f" Con max_tokens ≈ {cabrian:,} sí cabría."
        texto = (
            f"TOPE DE GASTO: en el peor caso este lote costaría hasta {formatear_usd(peor, 2)} "
            f"y solo quedan {formatear_usd(restante, 2)} de tu tope. No se envía: en modo lote no se puede "
            f"frenar a mitad. Sube el tope, baja max_tokens o divide el lote.{pista}"
        )
        logger.warning(texto)
        cb.emit("error", texto)
        self._avisar("Tope de gasto", texto)
        self.marcar_parada(StopReason.BUDGET)
        return False

    def _coste_salida_lote(self, tokens: int) -> Decimal:
        return Decimal(tokens) * self.precios.salida / Decimal(1_000_000) / 2

    def _abortar(self, cb: Callbacks, texto: str) -> None:
        cb.emit("error", texto)
        self.marcar_parada(StopReason.FATAL)

    # ------------------------------------------------------------------
    # Espera y recogida
    # ------------------------------------------------------------------

    @staticmethod
    def _progreso(lote: Any) -> str:
        rc = getattr(lote, "request_counts", None)
        if rc is None:
            return "Lote en curso…"
        ok = int(getattr(rc, "succeeded", 0) or 0)
        mal = sum(int(getattr(rc, k, 0) or 0) for k in ("errored", "canceled", "expired"))
        en_curso = int(getattr(rc, "processing", 0) or 0)
        total = ok + mal + en_curso
        extra = f", {mal} con problema" if mal else ""
        return f"Lote en curso: {ok + mal:,}/{total:,} procesadas ({ok:,} correctas{extra})"

    def _seguir(self, cb: Callbacks, ciclo: int) -> None:
        intervalo = self.intervalo_min
        while True:
            lote = self._llamar(
                lambda: self.client.messages.batches.retrieve(self.job.batch_id),
                cb,
                al_no_existir=lambda: self._lote_perdido(cb),
            )
            if lote is None:
                return
            cb.emit("estado", self._progreso(lote), True)
            if getattr(lote, "processing_status", "") == "ended":
                break
            # Si el usuario cierra o detiene, el lote sigue en el servidor y se reengancha.
            if not self.esperar(intervalo, cb):
                return
            intervalo = min(self.intervalo_max, intervalo * 1.5)
        self._recoger(cb, ciclo)

    def _lote_perdido(self, cb: Callbacks) -> None:
        texto = (
            f"El lote {self.job.batch_id} ya no existe en el servidor (los resultados se conservan 29 días). "
            "Las órdenes siguen pendientes: si reanudas se enviarán en un lote nuevo."
        )
        self.job.batch_id = ""
        self.job.save()
        cb.emit("error", texto)
        self.marcar_parada(StopReason.FATAL)

    def _recoger(self, cb: Callbacks, ciclo: int) -> None:
        por_id = {custom_id(m): m for m in self.job.messages}
        vistos: set[str] = set()
        cont = Contadores()
        cb.emit("estado", "Lote terminado. Recogiendo resultados…", True)
        while True:
            antes = len(vistos)
            try:
                # Los resultados llegan en cualquier orden: se emparejan por custom_id.
                if not self._apuntar_resultados(por_id, vistos, ciclo, cb):
                    return  # no se puede escribir en disco: el lote sigue en el servidor y el resto se recoge al reanudar
                break
            except Exception as error:
                # Bajar los resultados no cuesta nada, así que repetirlo nunca duplica un cobro: una descarga cortada,
                # una página de un proxy o un tiempo agotado se repiten con espera. Lo demás sigue la política de siempre.
                if len(vistos) > antes:
                    cont = Contadores()  # esta vez se avanzó: los fallos se cuentan sin avance, no en toda una descarga larga
                if self.manejar_error_de_consulta(error, cb, cont) is not Decision.REINTENTAR:
                    return  # recogida a medias: se conserva el batch_id y al reanudar se completa

        for msg in self.job.pendientes():
            if custom_id(msg) not in vistos:
                self.marcar_fallo(msg, "El lote no devolvió resultado para esta orden.", cb, "missing", guardar=False)
        logger.info("Lote %s recogido (servidor %s)", self.job.job_id, self.job.batch_id)
        self.job.batch_id = ""
        if self.punto_de_control(cb):
            self._comprobar_tope(cb)  # ahora que está todo pagado y guardado

    def _apuntar_resultados(self, por_id: dict, vistos: set, ciclo: int, cb: Callbacks) -> bool:
        """Recorre los resultados del servidor y apunta los que faltan. False si hay que dejar de recoger (el disco no admite más).

        El estado del lote y el gasto se escriben cada cierto número de resultados y, pase lo que pase (un fallo de la
        descarga, Ctrl+C), con lo que se haya apuntado hasta ese momento.
        """
        sin_guardar = 0
        ultimo = time.monotonic()
        cada = max(RESULTADOS_ENTRE_PUNTOS_DE_CONTROL, len(por_id) // 20)
        try:
            for entrada in self.client.messages.batches.results(self.job.batch_id):
                msg = por_id.get(entrada.custom_id)
                if msg is None:
                    continue
                vistos.add(entrada.custom_id)
                # Una recogida cortada y reanudada vuelve a pasar por aquí: lo que ya
                # está resuelto no se vuelve a contabilizar (inflaría el gasto) (Y2).
                if msg.status != STATUS_PENDING:
                    continue
                if not self._aplicar(msg, entrada.result, ciclo, cb):
                    return False
                sin_guardar += 1
                if sin_guardar >= cada or time.monotonic() - ultimo > SEGUNDOS_ENTRE_PUNTOS_DE_CONTROL:
                    guardado, sin_guardar = self.punto_de_control(cb), 0
                    if not guardado:
                        return False
                    ultimo = time.monotonic()
            return True
        finally:
            if sin_guardar:
                self.punto_de_control(cb)

    def _aplicar(self, msg: JobMessage, resultado: Any, ciclo: int, cb: Callbacks) -> bool:
        """Apunta el resultado de una orden. False si hay que dejar de recoger (el disco no admite más)."""
        tipo = getattr(resultado, "type", "")
        if tipo == "succeeded":
            return self.registrar_respuesta(
                msg, resultado.message, ciclo, cb, lote=True, comprobar_tope=False, guardar_ya=False
            )
        if tipo == "errored":
            tipo_error, mensaje = _datos_del_error(resultado)
            detalle = f"La API devolvió un error ({tipo_error or 'desconocido'})."
            if tipo_error.startswith("invalid_request"):
                detalle += " La petición es inválida; corrígela antes de reintentar."
                categoria = "bad_request"
            else:
                detalle += " Es un error de servidor; se puede reintentar."
                categoria = "server"
            if mensaje:
                detalle += f" ({mensaje})"
            self.marcar_fallo(msg, detalle, cb, categoria, guardar=False)
        elif tipo == "expired":
            self.marcar_fallo(msg, "Caducada: el lote superó las 24 h. Reintenta la orden.", cb, "expired", guardar=False)
        elif tipo == "canceled":
            self.marcar_fallo(msg, "Cancelada antes de procesarse.", cb, "canceled", guardar=False)
        else:
            self.marcar_fallo(msg, f"Resultado desconocido: {tipo!r}", cb, "unknown", guardar=False)
        return True

    # ------------------------------------------------------------------

    def cancelar_lote(self) -> bool:
        """Cancela en el servidor el lote en curso. Lo ya procesado se cobra y se recoge al reanudar.

        Puede llamarse desde otro hilo mientras el motor termina: éste cierra su cliente al acabar, y una cancelación que
        lo estuviera usando fallaría justo entonces. Por eso, si falla con el cliente del motor, se repite con uno propio.
        """
        if not self.job.batch_id:
            return False
        compartido = self._client
        propio_primero = compartido is None or _cerrado(compartido)
        for intento in (0, 1):
            usar_propio = propio_primero or intento == 1
            cliente: Any = None
            try:
                cliente = self._cliente_nuevo() if usar_propio else compartido
                cliente.messages.batches.cancel(self.job.batch_id)
                logger.info("Cancelación pedida para el lote %s", self.job.batch_id)
                return True
            except Exception:
                logger.exception("No se pudo cancelar el lote %s", self.job.batch_id)
                if usar_propio:
                    return False
            finally:
                cerrar = getattr(cliente, "close", None) if usar_propio else None
                if callable(cerrar):
                    try:
                        cerrar()
                    except Exception:
                        pass
        return False


# ============================================================================
# 18. Clave de API y elección de modelo
#    (claude_batch_monitor/claves.py)
# ============================================================================
#
# Comprobar y guardar la clave de API, y elegir un modelo que exista.
#
# Sin dependencias de interfaz: lo usan la pantalla de bienvenida, las opciones y
# el modo avanzado, y se puede probar contra el SDK real con un servidor simulado.


MAX_MODELOS = 500


class ErrorDeClave(Exception):
    """La clave no se pudo comprobar. `tipo` dice por qué, para redactar un mensaje claro."""

    def __init__(self, tipo: str, detalle: str = ""):
        super().__init__(detalle or tipo)
        self.tipo = tipo  # invalida | sin_conexion | otro
        self.detalle = detalle


def _crear_cliente(clave: str) -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=clave, timeout=15, max_retries=0)


def _traducir(error: Exception) -> ErrorDeClave:
    import anthropic

    if isinstance(error, anthropic.APIConnectionError):  # incluye los timeouts
        return ErrorDeClave("sin_conexion")
    if isinstance(error, anthropic.APIStatusError):
        info = analizar_error(error)
        if info.categoria is Categoria.AUTH:
            return ErrorDeClave("invalida")
        return ErrorDeClave("otro", info.mensaje)
    return ErrorDeClave("otro", str(error))


def probar_clave(clave: str, crear_cliente: Callable[[str], Any] = _crear_cliente) -> list[str]:
    """Comprueba la clave pidiendo la lista de modelos (no cuesta nada) y la devuelve.

    Lanza `ErrorDeClave` si no es válida, no hay conexión o falla otra cosa.
    """
    clave = (clave or "").strip()
    if not clave:
        raise ErrorDeClave("vacia")
    cliente = crear_cliente(clave)
    try:
        return [m.id for m in cliente.models.list(limit=100).data]
    except Exception as error:
        raise _traducir(error) from error
    finally:
        cerrar = getattr(cliente, "close", None)
        if callable(cerrar):
            try:
                cerrar()
            except Exception:
                pass


def listar_modelos(clave: str, crear_cliente: Callable[[str], Any] = _crear_cliente) -> list[str]:
    """Todos los modelos disponibles (recorre las páginas), los más nuevos primero."""
    cliente = crear_cliente(clave.strip())
    ids: list[str] = []
    try:
        # Iterar la página recorre todas; `.data` solo daría la primera (20 por defecto).
        for modelo in cliente.models.list(limit=100):
            ids.append(modelo.id)
            if len(ids) >= MAX_MODELOS:
                break
    except Exception as error:
        raise _traducir(error) from error
    finally:
        cerrar = getattr(cliente, "close", None)
        if callable(cerrar):
            try:
                cerrar()
            except Exception:
                pass
    return sorted(set(ids), reverse=True)


def elegir_modelo_por_defecto(disponibles: list[str], actual: Optional[str] = None) -> str:
    """El modelo a usar dado lo que ofrece la cuenta: el actual si existe, si no el recomendado."""
    if not disponibles:
        return actual or DEFAULT_MODEL
    por_normalizado = {normalizar_id(m): m for m in disponibles}
    if actual and normalizar_id(actual) in por_normalizado:
        return actual
    for preferido in [DEFAULT_MODEL, *MODELOS_SUGERIDOS]:
        if normalizar_id(preferido) in por_normalizado:
            return preferido
    for m in disponibles:
        if "sonnet" in m:
            return m
    return disponibles[0]


def guardar_clave(clave: str) -> str:
    """Guarda la clave de forma segura. Devuelve 'llavero' o 'memoria'.

    Sin llavero del sistema la clave solo vive en esta sesión (variable de entorno
    del propio proceso): nunca se escribe en un fichero de configuración.
    """
    clave = clave.strip()
    if SecureStorage.set(KEY_API, clave):
        return "llavero"
    os.environ["ANTHROPIC_API_KEY"] = clave
    return "memoria"


# ============================================================================
# 19. La cola del modo simple
#    (claude_batch_monitor/cola.py)
# ============================================================================
#
# La cola de preguntas del modo simple.
#
# Una persona escribe preguntas; la cola las guarda en disco, las envía una a una
# en un hilo aparte y, si la API pide esperar (límite por minuto, servidor saturado,
# sin conexión), espera sola y continúa. Se pueden añadir preguntas mientras se
# envía y, si se cierra el programa, lo pendiente sigue ahí al volver a abrirlo.
#
# Esta clase NO sabe nada de ventanas: informa de lo que ocurre a través de
# `EventosCola`, y cada evento se entrega mediante `publicar` (en la interfaz,
# el puente que lo ejecuta en el hilo de la ventana). Eso la hace probable sin
# pantalla.


ETIQUETA = "cola"


@dataclass(frozen=True)
class EstadoCola:
    """Qué está haciendo la cola ahora mismo (lo que se le cuenta a la persona)."""

    tipo: str = "idle"  # idle | sending | waiting | paused | stopped
    motivo: str = ""    # waiting: rate_limit | server | sin_conexion | cuota
    razon: Optional[StopReason] = None  # stopped: por qué
    msg_id: Optional[int] = None
    segundos: float = 0.0
    texto: str = ""     # detalle (mensaje de error de la API, ya redactado)


class EventosCola:
    """Lo que la interfaz recibe. Los métodos por defecto no hacen nada."""

    def orden_cambiada(self, msg: JobMessage) -> None: ...
    def estado(self, estado: EstadoCola) -> None: ...
    def cuenta_atras(self, restante: float) -> None: ...
    def delta(self, msg_id: int, texto: str) -> None: ...
    def gasto(self, usd: Decimal) -> None: ...
    def error(self, texto: str) -> None: ...


FabricaMotor = Callable[[Configuracion, Job], Any]


class Cola:
    def __init__(
        self,
        config: Configuracion,
        eventos: Optional[EventosCola] = None,
        publicar: Optional[Callable[[Callable[[], None]], None]] = None,
        fabrica: FabricaMotor = SequentialEngine,
        job: Optional[Job] = None,
    ):
        self.config = config
        self.eventos = eventos or EventosCola()
        self._publicar = publicar or (lambda f: f())
        self._fabrica = fabrica
        self.job = job
        self.notificaciones = True

        self._lock = threading.RLock()
        self._hilo: Optional[threading.Thread] = None
        self._motor: Any = None
        self._pausada = False
        self._enviando: Optional[int] = None
        self.estado = EstadoCola()
        self.ultimo_error = ""

    # ------------------------------------------------------------------
    # Estado
    # ------------------------------------------------------------------

    @property
    def activa(self) -> bool:
        with self._lock:
            return self._hilo is not None and self._hilo.is_alive()

    @property
    def pausada(self) -> bool:
        return self._pausada

    def preguntas(self) -> list[JobMessage]:
        return list(self.job.messages) if self.job else []

    def pendientes(self) -> int:
        return len(self.job.pendientes()) if self.job else 0

    def gastado(self) -> Decimal:
        return self.job.coste_total() if self.job else Decimal(0)

    def cargar_ultima(self) -> int:
        """Recupera la cola de la sesión anterior. Devuelve cuántas preguntas quedan pendientes."""
        lotes = listar_jobs(etiqueta=ETIQUETA)
        if lotes:
            self.job = Job.load(lotes[0]["job_id"])
        return self.pendientes()

    def _config_de_envio(self) -> Configuracion:
        """La configuración con la que se envía, con los ajustes que fija el modo simple."""
        return replace(
            self.config,
            modo="inmediato",
            concurrencia=1,
            streaming=True,
            pausa_entre_mensajes_sec=0,
            estimar_antes_de_enviar=True,
            reset_gasto_por_lote=True,  # el tope es por cola: parte de lo que esta cola ya gastó
            modo_continuo=False,
            hora_limite="",
        )

    # ------------------------------------------------------------------
    # Acciones de la persona
    # ------------------------------------------------------------------

    def agregar(self, texto: str) -> Optional[JobMessage]:
        """Añade una pregunta a la cola y, si no está pausada, empieza a enviarla."""
        texto = (texto or "").strip()
        if not texto:
            return None
        with self._lock:
            if self.job is None:
                self.job = Job.crear([], self._config_de_envio(), etiqueta=ETIQUETA)
                self.job.save()
            msg = self.job.agregar_orden(texto)
        self._emitir(self.eventos.orden_cambiada, msg)
        self._despertar()
        return msg

    def pausar(self) -> None:
        with self._lock:
            self._pausada = True
            if self._motor is not None:
                self._motor.detener()  # termina la pregunta en curso y no envía más
        self._set_estado(EstadoCola("paused"))

    def reanudar(self) -> None:
        """Quita la pausa y vuelve a intentar lo pendiente (y lo que falló: tras arreglar la causa)."""
        with self._lock:
            self._pausada = False
            self.ultimo_error = ""
            if self.job is not None and self.job.reintentar_fallidas():
                for m in self.job.messages:
                    self._emitir(self.eventos.orden_cambiada, m)
        self._despertar()

    def quitar(self, msg_id: int) -> bool:
        """Saca una pregunta de la cola si aún no se ha enviado."""
        with self._lock:
            msg = self.job.mensaje(msg_id) if self.job else None
            if msg is None or msg.status != STATUS_PENDING or self._enviando == msg_id:
                return False
            msg.status = STATUS_SKIPPED
            msg.detalle = "Quitada de la cola."
            self.job.save()
        self._emitir(self.eventos.orden_cambiada, msg)
        return True

    def nueva(self) -> bool:
        """Empieza una cola en blanco (la anterior queda guardada en disco). Solo si está en reposo."""
        with self._lock:
            if self.activa:
                return False
            self.job = None
            self._pausada = False
            self.ultimo_error = ""
        self._set_estado(EstadoCola("idle"))
        return True

    def actualizar_config(self, config: Configuracion) -> None:
        """Los cambios (tope, modelo…) valen para el siguiente envío."""
        with self._lock:
            self.config = config

    def cerrar(self, timeout: float = 5.0) -> None:
        """Para el hilo. Lo pendiente queda guardado y seguirá la próxima vez."""
        with self._lock:
            self._pausada = True
            motor, hilo = self._motor, self._hilo
            if motor is not None:
                motor.detener()
        if hilo is not None and hilo.is_alive():
            hilo.join(timeout)

    # ------------------------------------------------------------------
    # El hilo
    # ------------------------------------------------------------------

    def _despertar(self) -> None:
        with self._lock:
            if self._pausada or self.job is None or not self.job.pendientes():
                return
            if self._hilo is not None and self._hilo.is_alive():
                return  # el bucle recogerá lo nuevo cuando termine con lo actual
            self._hilo = threading.Thread(target=self._bucle, name="cola-de-preguntas", daemon=True)
            self._hilo.start()

    def _bucle(self) -> None:
        while True:
            with self._lock:
                if self._pausada or self.job is None or not self.job.pendientes():
                    # Decidir salir y soltar el hilo en el MISMO cerrojo: así una pregunta
                    # añadida justo ahora no se queda esperando a nadie.
                    self._hilo = None
                    self._enviando = None
                    self._set_estado(EstadoCola("paused" if self._pausada and self.pendientes() else "idle"))
                    return
                try:
                    motor = self._fabrica(self._config_de_envio(), self.job)
                    motor.notificaciones = self.notificaciones
                except Exception as error:
                    # Sin esto el hilo moría con la excepción sin capturar y la ventana se
                    # quedaba sin saber nada: ni error, ni estado, ni forma de reintentar.
                    logger.exception("No se pudo preparar el envío")
                    self.ultimo_error = f"No se pudo preparar el envío: {error}"
                    self._emitir(self.eventos.error, self.ultimo_error)
                    self._hilo = None
                    self._enviando = None
                    self._set_estado(EstadoCola("stopped", razon=StopReason.FATAL, texto=self.ultimo_error))
                    return
                self._motor = motor
            try:
                # Mientras hay algo que enviar o esperando, el equipo no debe dormirse (si no, «espera sola» sería falso).
                # En Windows esto es POR HILO, así que se activa y se suelta aquí, en el hilo que trabaja.
                with KeepAwake():
                    razon = motor.ejecutar(self._callbacks())
            except Exception as error:  # pragma: no cover - defensa; el motor ya captura casi todo
                logger.exception("Fallo inesperado en la cola")
                self.ultimo_error = f"Error inesperado: {error}"
                self._emitir(self.eventos.error, self.ultimo_error)
                razon = StopReason.FATAL
            with self._lock:
                self._motor = None
                self._enviando = None
                if razon is StopReason.COMPLETED:
                    continue  # ¿llegó algo mientras tanto? el bucle lo comprueba arriba
                if razon is StopReason.STOPPED and self._pausada:
                    continue  # la pausa se resuelve arriba, con el estado correcto
                self._hilo = None
                self._set_estado(EstadoCola("stopped", razon=razon, texto=self.ultimo_error))
                return

    def _callbacks(self) -> Callbacks:
        def estado(texto: str, activo: bool) -> None:
            pass  # el texto del motor está en español; la interfaz redacta el suyo

        def inicio_orden(msg: JobMessage) -> None:
            self._enviando = msg.id
            self._set_estado(EstadoCola("sending", msg_id=msg.id))

        def resultado(msg: JobMessage) -> None:
            if self._enviando == msg.id and msg.status != STATUS_PENDING:
                self._enviando = None
            self._emitir(self.eventos.orden_cambiada, msg)

        def espera(motivo: str, segundos: float) -> None:
            self._set_estado(EstadoCola("waiting", motivo=motivo, segundos=segundos, msg_id=self._enviando))

        def tick(total: float, restante: float) -> None:
            self._emitir(self.eventos.cuenta_atras, restante)

        def gasto(usd: Decimal, consumo: Any) -> None:
            self._emitir(self.eventos.gasto, usd)

        def error(texto: str) -> None:
            self.ultimo_error = texto
            self._emitir(self.eventos.error, texto)

        def delta(msg_id: int, texto: str) -> None:
            self._emitir(self.eventos.delta, msg_id, texto)

        return Callbacks(
            estado=estado, inicio_orden=inicio_orden, resultado=resultado, espera=espera,
            tick=tick, gasto=gasto, error=error, delta=delta,
        )

    # ------------------------------------------------------------------

    def _set_estado(self, estado: EstadoCola) -> None:
        self.estado = estado
        self._emitir(self.eventos.estado, estado)

    def _emitir(self, metodo: Callable[..., None], *args: Any) -> None:
        def entregar() -> None:
            try:
                metodo(*args)
            except Exception:
                logger.exception("Fallo en un evento de la cola")

        self._publicar(entregar)


def hay_fallidas(job: Optional[Job]) -> bool:
    return bool(job and any(m.status == STATUS_FAILED for m in job.messages))


# ============================================================================
# 20. Ejecutar un lote en segundo plano (modo avanzado)
#    (claude_batch_monitor/ejecutor.py)
# ============================================================================
#
# Ejecuta UN lote en un hilo aparte y cuenta lo que pasa a la ventana.
#
# Es el equivalente de `Cola` para el modo avanzado, pero con un lote cerrado: se
# lanza, se sigue su progreso y termina. Tampoco sabe nada de ventanas: cada evento
# se entrega mediante `publicar` (en la interfaz, el puente que lo ejecuta en el
# hilo de la ventana), y el último siempre es `terminado`, así que la ventana sabe
# con certeza cuándo volver a activar sus botones.


class EventosLote:
    """Lo que la ventana recibe. Los métodos por defecto no hacen nada."""

    def estado(self, texto: str, activo: bool) -> None: ...
    def inicio_orden(self, msg: JobMessage) -> None: ...
    def resultado(self, msg: JobMessage) -> None: ...
    def fin_ciclo(self, completadas: int, total: int, ciclo: int) -> None: ...
    def error(self, texto: str) -> None: ...
    def tick(self, total: float, restante: float) -> None: ...
    def gasto(self, usd: Decimal, consumo: Consumo) -> None: ...
    def terminado(self, razon: StopReason, job: Job) -> None: ...


def motor_para(config: Configuracion, job: Job) -> Any:
    """El motor que toca según el modo: inmediato (una petición por orden) o lote (Batches API)."""
    return (BatchEngine if config.es_lote else SequentialEngine)(config, job)


class EjecutorLote:
    def __init__(
        self,
        publicar: Optional[Callable[[Callable[[], None]], None]] = None,
        fabrica: Callable[[Configuracion, Job], Any] = motor_para,
    ):
        self._publicar = publicar or (lambda f: f())
        self._fabrica = fabrica
        self._lock = threading.Lock()
        self._hilo: Optional[threading.Thread] = None
        self._motor: Any = None
        self.notificaciones = True

    @property
    def activo(self) -> bool:
        with self._lock:
            return self._hilo is not None and self._hilo.is_alive()

    @property
    def motor(self) -> Any:
        return self._motor

    def iniciar(self, config: Configuracion, job: Job, eventos: EventosLote) -> bool:
        """Lanza el lote. Devuelve False si ya hay uno en marcha (nunca dos a la vez: se pisarían el gasto)."""
        with self._lock:
            if self._hilo is not None and self._hilo.is_alive():
                return False
            try:
                motor = self._fabrica(config, job)
            except Exception as error:
                logger.exception("No se pudo preparar el lote")
                self._entregar(eventos.error, f"No se pudo preparar el lote: {error}")
                self._entregar(eventos.terminado, StopReason.FATAL, job)
                return True
            motor.notificaciones = self.notificaciones
            self._motor = motor
            self._hilo = threading.Thread(
                target=self._trabajo, args=(motor, job, eventos), daemon=True, name="lote"
            )
            self._hilo.start()
        return True

    def detener(self) -> None:
        """Pide parar: la petición en vuelo termina y no se envía ninguna más."""
        motor = self._motor
        if motor is not None:
            motor.detener()

    def cancelar_lote(self, al_terminar: Optional[Callable[[bool], None]] = None) -> bool:
        """Para el seguimiento y cancela en el servidor el lote en curso (en segundo plano, sin bloquear a quien llama).

        `al_terminar(ok)` se avisa por `publicar` cuando el servidor ha contestado. False si no hay un lote que cancelar.
        Primero se para el motor (siempre el mismo desenlace: el lote queda `canceling` y «Reanudar» recoge lo que quede);
        el motor cierra su cliente al terminar, y `cancelar_lote` del motor lo sabe y usa uno propio si hace falta.
        """
        motor = self._motor
        if motor is None or not hasattr(motor, "cancelar_lote") or not getattr(motor.job, "batch_id", ""):
            return False
        motor.detener()

        def trabajo() -> None:
            ok = False
            try:
                ok = bool(motor.cancelar_lote())
            except Exception:
                logger.exception("Fallo al cancelar el lote")
            if al_terminar is not None:
                self._entregar(al_terminar, ok)

        threading.Thread(target=trabajo, daemon=True, name="cancelar-lote").start()
        return True

    def esperar(self, timeout: Optional[float] = None) -> bool:
        """Espera a que termine el hilo (para el cierre ordenado y las pruebas)."""
        hilo = self._hilo
        if hilo is not None:
            hilo.join(timeout)
        return not self.activo

    # ------------------------------------------------------------------

    def _trabajo(self, motor: Any, job: Job, eventos: EventosLote) -> None:
        cb = self._callbacks(eventos)
        razon = StopReason.FATAL
        try:
            # Windows liga `SetThreadExecutionState` al hilo: se activa y se suelta aquí, no en la ventana.
            with KeepAwake():
                razon = motor.ejecutar(cb)
        except Exception as error:
            logger.exception("Fallo inesperado en el lote")
            self._entregar(eventos.error, f"Error inesperado: {error}")
        finally:
            self._motor = None
            self._entregar(eventos.terminado, razon, job)

    def _callbacks(self, eventos: EventosLote) -> Callbacks:
        return Callbacks(
            estado=lambda texto, activo: self._entregar(eventos.estado, texto, activo),
            inicio_orden=lambda msg: self._entregar(eventos.inicio_orden, msg),
            resultado=lambda msg: self._entregar(eventos.resultado, msg),
            fin_ciclo=lambda c, t, n: self._entregar(eventos.fin_ciclo, c, t, n),
            error=lambda texto: self._entregar(eventos.error, texto),
            tick=lambda total, restante: self._entregar(eventos.tick, total, restante),
            gasto=lambda usd, consumo: self._entregar(eventos.gasto, usd, consumo),
        )

    def _entregar(self, metodo: Callable[..., None], *args: Any) -> None:
        def entregar() -> None:
            try:
                metodo(*args)
            except Exception:
                logger.exception("Fallo en un evento del lote")

        self._publicar(entregar)


# ============================================================================
# 21. Terminal (--cli)
#    (claude_batch_monitor/cli.py)
# ============================================================================
#
# Interfaz de línea de comandos: para servidores, cron y scripts.
#
# Códigos de salida (para encadenar con `&&` o `case $?`):
#
#     0  todo completado
#     1  error fatal o de red, o terminó con órdenes fallidas
#     2  detenido por el tope de gasto
#     3  sin saldo, o límite de gasto de la cuenta de Anthropic
#     4  incompleto (hora límite o cancelado con Ctrl+C)
#     5  uso incorrecto (argumentos, fichero, configuración) o ya hay otra copia del programa en marcha


EXIT_OK, EXIT_FALLO, EXIT_TOPE, EXIT_SIN_SALDO, EXIT_INCOMPLETO, EXIT_USO = 0, 1, 2, 3, 4, 5

_CODIGOS = {
    StopReason.COMPLETED: EXIT_OK,
    StopReason.FATAL: EXIT_FALLO,
    StopReason.NETWORK: EXIT_FALLO,
    StopReason.BUDGET: EXIT_TOPE,
    StopReason.NO_CREDIT: EXIT_SIN_SALDO,
    StopReason.ACCOUNT_LIMIT: EXIT_SIN_SALDO,
    StopReason.STOPPED: EXIT_INCOMPLETO,
    StopReason.DEADLINE: EXIT_INCOMPLETO,
}


def codigo_de_salida(razon: StopReason, job: Optional[Job] = None) -> int:
    codigo = _CODIGOS.get(razon, EXIT_FALLO)
    # Un lote puede "completarse" con órdenes que la API rechazó: no es un éxito limpio.
    if codigo == EXIT_OK and job is not None and job.fallidas():
        return EXIT_FALLO
    return codigo


def forzar_utf8_en_salida() -> None:
    """Evita `UnicodeEncodeError` al redirigir la salida en Windows.

    La consola real de Windows admite UTF-8, pero una tubería o un fichero usan
    cp1252 y cualquier carácter fuera de él (una tilde rara, un emoji de una
    respuesta de Claude) hacía reventar justo el uso documentado en cron.
    """
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


class _Parser(argparse.ArgumentParser):
    """argparse sale con 2 por defecto, que aquí significa "tope de gasto". Uso incorrecto = 5."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USO, f"{self.prog}: error: {message}\n")


def construir_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog=f"{comando_para_ejecutar()} --cli",
        description="Procesa una lista de órdenes con Claude, con tope de gasto y reanudación.",
        epilog="Códigos de salida: 0 ok · 1 fallo · 2 tope de gasto · 3 sin saldo/límite de cuenta · "
        "4 incompleto · 5 uso incorrecto",
    )
    e = p.add_argument_group("entrada")
    e.add_argument("--mensaje", help="Texto literal o ruta a un fichero (.txt, .md, .csv, .tsv, .jsonl)")
    e.add_argument("--campo", help="Columna del CSV o campo del JSONL con el texto (por defecto 'input')")
    e.add_argument("--plantilla", help="Envuelve cada orden: --plantilla 'Traduce:\\n{input}'")
    e.add_argument("--limit", type=int, help="Procesa solo las N primeras órdenes (prueba de humo)")
    e.add_argument("--separador", default=DEFAULT_SEPARATOR, help="Separador de órdenes en texto plano")
    e.add_argument("--reanudar", metavar="JOB_ID", help="Continúa un lote existente")
    e.add_argument("--listar", action="store_true", help="Lista los lotes guardados y sale")
    e.add_argument("--cancelar-lote", metavar="JOB_ID", dest="cancelar_lote",
                   help="Cancela en el servidor el lote en curso (lo ya procesado se cobra) y sale")

    m = p.add_argument_group("modelo")
    m.add_argument("--modelo", help=f"Por defecto {DEFAULT_MODEL}; al reanudar, el del lote")
    m.add_argument("--system", help="Prompt de sistema compartido (texto o ruta a fichero)")
    m.add_argument("--modo", choices=("inmediato", "lote"),
                   help="'lote' = Batches API, 50 %% más barato. Al reanudar se hereda el del lote")
    m.add_argument("--max-tokens", type=int, help="Tokens máximos de salida por orden (por defecto 16000)")
    m.add_argument("--esfuerzo", choices=NIVELES_ESFUERZO, help="Nivel de esfuerzo (solo en modelos que lo soportan)")
    m.add_argument("--sin-cache", action="store_true", help="Desactiva el prompt caching")
    m.add_argument("--concurrencia", type=int, default=1, help="Peticiones en paralelo (modo inmediato)")

    g = p.add_argument_group("gasto")
    g.add_argument("--tope-gasto", default="5", help="USD máximos; 0 = sin tope (por defecto 5)")
    g.add_argument("--precio-in", help="USD por millón de tokens de entrada (si no, el del catálogo)")
    g.add_argument("--precio-out", help="USD por millón de tokens de salida (si no, el del catálogo)")
    g.add_argument("--dry-run", action="store_true", help="Estima el coste y sale sin enviar nada")

    t = p.add_argument_group("tiempos")
    t.add_argument("--bucle", action="store_true", help="ATENCIÓN: repite el lote hasta la hora límite o el tope")
    t.add_argument("--hora-limite", help="HH:MM. En bucle, 23:59 por defecto")
    t.add_argument("--pausa-ciclos", type=int, default=10, help="Segundos entre ciclos del bucle")
    t.add_argument("--pausa", type=int, default=0, help="Segundos de pausa entre órdenes")

    s = p.add_argument_group("salida")
    s.add_argument("--exportar", metavar="FICHERO", help="Escribe los resultados (.csv .jsonl .md .txt)")
    s.add_argument("--formato", choices=FORMATOS, help="Fuerza el formato de --exportar")
    s.add_argument("--json", action="store_true", help="Resumen final en JSON por stdout (el progreso va a stderr)")
    s.add_argument("--silencioso", action="store_true", help="Solo errores y resumen")
    s.add_argument("--version", action="store_true", help="Muestra la versión y sale")
    return p


# ----------------------------------------------------------------------


class _Salida:
    """Dónde va lo que se imprime: con --json, el progreso a stderr para no ensuciar stdout."""

    def __init__(self, args: argparse.Namespace):
        self.json = args.json
        self.silencioso = args.silencioso
        self.flujo = sys.stderr if args.json else sys.stdout
        self.tty = self.flujo.isatty()

    def info(self, texto: str = "") -> None:
        if not self.silencioso:
            print(texto, file=self.flujo, flush=True)

    def aviso(self, texto: str) -> None:
        print(f"AVISO: {texto}", file=sys.stderr, flush=True)

    def error(self, texto: str) -> None:
        print(f"ERROR: {texto}", file=sys.stderr, flush=True)


def _como_utf8(texto: str) -> str:
    """El texto de un argumento tal como lo tecleó quien lo escribió.

    Python lee los argumentos con la codificación del sistema. Si es ASCII (una terminal con LANG=C y sin el modo UTF-8
    de Python), cada carácter no ASCII llega como «sustitutos» y se guardaría como `��`. Aquí se recuperan los bytes
    originales y se leen como UTF-8. Solo para TEXTOS: una ruta de archivo tiene que quedarse tal como llegó.
    """
    if not any("\udc80" <= c <= "\udcff" for c in texto):
        return texto
    try:
        return os.fsencode(texto).decode("utf-8")
    except UnicodeDecodeError:
        return texto


def _es_un_archivo(valor: str) -> bool:
    """¿Es `valor` la ruta de un archivo que existe? Un texto largo (una pregunta) no es una ruta y no debe fallar."""
    if not valor or "\n" in valor or "\x00" in valor:
        return False
    try:
        return Path(valor).is_file()
    except (OSError, ValueError):  # p. ej. «File name too long» con una pregunta de más de 255 caracteres
        return False


def _texto_o_fichero(valor: Optional[str]) -> str:
    if not valor:
        return ""
    try:
        if _es_un_archivo(valor):
            return Path(valor).read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        pass
    return _como_utf8(valor)


def _decimal_de_opcion(valor: Optional[str], nombre: str) -> Optional[Decimal]:
    if valor is None:
        return None
    try:
        d = Decimal(str(valor).replace(",", "."))
    except InvalidOperation:
        raise ErrorDeEntrada(f"{nombre} no es un número: {valor!r}") from None
    if not d.is_finite() or d < 0:
        raise ErrorDeEntrada(f"{nombre} debe ser un número igual o mayor que 0.")
    return d


def _ordenes_desde_args(args: argparse.Namespace) -> list[str]:
    origen = args.mensaje
    plantilla = _como_utf8(args.plantilla or "")
    if _es_un_archivo(origen):
        return leer_ordenes(
            origen, campo=args.campo, separador=args.separador,
            plantilla=plantilla, limite=args.limit,
        )
    ordenes = split_messages(_como_utf8(origen), _como_utf8(args.separador))
    if not ordenes:
        raise ErrorDeEntrada("No se detectó ninguna orden en --mensaje.")
    if args.limit and args.limit > 0:
        ordenes = ordenes[: args.limit]
    return [aplicar_plantilla(plantilla, o) for o in ordenes]


def _configuracion(args: argparse.Namespace, job: Optional[Job]) -> Configuracion:
    """Config a partir de los argumentos. Al reanudar, lo no pedido se hereda del lote (X4)."""
    c = Configuracion()
    c.modelo = args.modelo or (job.modelo if job else DEFAULT_MODEL)
    # `--modo` NO tiene valor por defecto: antes valía 'inmediato' y pisaba el del lote,
    # de modo que reanudar por CLI un lote creado en modo lote lo reenviaba entero por
    # la API normal, al doble de precio y sin recoger el lote ya pagado en el servidor.
    c.modo = args.modo or (job.modo if job else "inmediato")
    c.system_prompt = _texto_o_fichero(args.system) if args.system is not None else (job.system_prompt if job else "")
    c.separador = args.separador
    c.usar_cache = not args.sin_cache
    c.esfuerzo = args.esfuerzo or ""
    if args.max_tokens is not None:
        c.max_tokens = args.max_tokens
    c.concurrencia = max(1, args.concurrencia)
    c.tope_gasto_usd = _decimal_de_opcion(args.tope_gasto, "--tope-gasto") or Decimal(0)
    c.precio_input_mtok = _decimal_de_opcion(args.precio_in, "--precio-in")
    c.precio_output_mtok = _decimal_de_opcion(args.precio_out, "--precio-out")
    c.modo_continuo = args.bucle
    c.hora_limite = args.hora_limite or ""
    c.pausa_ciclos_sec = max(1, args.pausa_ciclos)
    c.pausa_entre_mensajes_sec = max(0, args.pausa)
    return c


def _resumen_del_lote(razon: StopReason, job: Job, engine: Optional[EngineBase], exportado: Optional[Path]) -> dict:
    e = job.estadisticas()
    return {
        "job_id": job.job_id,
        "razon": razon.value,
        "modelo": job.modelo,
        "modo": job.modo,
        "total": e["total"],
        "completadas": e["completadas"],
        "pendientes": e["pendientes"],
        "fallidas": e["fallidas"],
        "rechazadas": e["rechazadas"],
        "coste_usd": str(e["coste_usd"]),
        "gasto_acumulado_usd": str(engine.gasto_actual()) if engine else str(e["coste_usd"]),
        "batch_id": job.batch_id or None,
        "exportado": str(exportado) if exportado else None,
        "codigo_de_salida": codigo_de_salida(razon, job),
    }


def _callbacks(salida: _Salida, args: argparse.Namespace) -> Callbacks:
    def estado(texto: str, activo: bool) -> None:
        salida.info(f"[{datetime.now():%H:%M:%S}] {texto}")

    def resultado(msg: JobMessage) -> None:
        marca = {"completed": "OK", "refused": "RECHAZADA", "failed": "FALLO"}.get(msg.status, msg.status)
        extra = f" {msg.detalle}" if msg.detalle else ""
        salida.info(f"  orden {msg.id}: {marca} · {msg.tokens_input + msg.tokens_output} tok · "
                    f"{formatear_usd(msg.coste_usd, 6)}{extra}")

    def tick(total: float, restante: float) -> None:
        if salida.tty and not salida.silencioso:
            print(f"\r  esperando… {int(restante)} s ", end="", file=salida.flujo, flush=True)

    return Callbacks(
        estado=estado,
        resultado=resultado,
        error=lambda texto: salida.error(texto),
        fin_ciclo=lambda c, t, n: salida.info(f"Ciclo {n} terminado: {c}/{t} completadas."),
        tick=tick,
    )


# ----------------------------------------------------------------------


def _cliente_para_conteo() -> Optional[Any]:
    """Cliente para contar tokens exactos en el dry-run, si hay clave. Si no, estimación local."""
    clave = obtener_api_key()
    if not clave:
        return None
    try:
        import anthropic

        return anthropic.Anthropic(api_key=clave, timeout=15, max_retries=0)
    except Exception:
        return None


def _cancelar_lote(args: argparse.Namespace, salida: _Salida) -> int:
    """`--cancelar-lote`: pide al servidor que cancele el lote en curso de un trabajo guardado."""
    job = Job.load(args.cancelar_lote)
    if job is None:
        salida.error(f"No existe el lote '{args.cancelar_lote}'. Usa --listar para ver los guardados.")
        return EXIT_USO
    if not job.batch_id:
        salida.error(
            f"El lote {job.job_id} no tiene nada en curso en el servidor de Anthropic "
            "(ya terminó, o no se envió en modo lote)."
        )
        return EXIT_USO
    try:
        config = _configuracion(args, job)
    except (ErrorDeEntrada, ErrorDeExportacion) as e:
        salida.error(str(e))
        return EXIT_USO
    if BatchEngine(config, job).cancelar_lote():
        salida.info(
            f"Cancelación pedida para el lote {job.batch_id}. Lo que ya se hubiera procesado se cobra; "
            f"el resto queda como «cancelada». Para recoger el resultado: --reanudar {job.job_id}"
        )
        return EXIT_OK
    salida.error(
        f"No se pudo cancelar el lote {job.batch_id} (mira el registro: {LOG_FILE}). "
        "Comprueba su estado en la consola de Anthropic."
    )
    return EXIT_FALLO


def main_cli(argv: Optional[list[str]] = None) -> int:
    forzar_utf8_en_salida()
    parser = construir_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as salir:  # --help (0) o argumentos incorrectos (5): se devuelve, no se propaga
        return salir.code if isinstance(salir.code, int) else EXIT_USO
    salida = _Salida(args)

    if args.version:
        print(f"Claude Batch Monitor {APP_VERSION}")
        return EXIT_OK

    if args.listar:
        lotes = listar_jobs()
        if args.json:
            print(json.dumps([{**d, "coste_usd": str(d["coste_usd"])} for d in lotes], ensure_ascii=False))
        elif not lotes:
            print("No hay lotes guardados.")
        else:
            for d in lotes:
                marca = "  <- incompleto" if d["incompleto"] else ""
                print(f"{d['job_id']}  {d['created_at'][:16]}  {d['modelo']:<22} {d['modo']:<9} "
                      f"{d['completadas']}/{d['total']}  {formatear_usd(d['coste_usd'])}{marca}")
        return EXIT_OK

    if args.cancelar_lote:
        return _cancelar_lote(args, salida)

    if not args.mensaje and not args.reanudar:
        parser.print_usage(sys.stderr)
        salida.error("indica --mensaje (texto o fichero) o --reanudar JOB_ID")
        return EXIT_USO

    # ---- el lote ----
    job: Optional[Job] = None
    try:
        if args.reanudar:
            job = Job.load(args.reanudar)
            if job is None:
                salida.error(f"No existe el lote '{args.reanudar}'. Usa --listar para ver los guardados.")
                return EXIT_USO
        config = _configuracion(args, job)
        ordenes = None if job else _ordenes_desde_args(args)
    except (ErrorDeEntrada, ErrorDeExportacion) as e:
        salida.error(str(e))
        return EXIT_USO

    if config.modo_continuo and config.es_lote:
        salida.error("--bucle solo existe en modo inmediato: repetir un lote entero sería muy caro.")
        return EXIT_USO

    if job is not None:
        for aviso in job.incompatibilidades(config):
            salida.aviso(aviso)
        reintentadas = job.reintentar_fallidas()
        if reintentadas:
            salida.info(f"{reintentadas} orden(es) fallida(s) se reintentarán.")
        textos = [m.text for m in job.pendientes()]
    else:
        textos = ordenes or []

    validacion = config.validar()
    for aviso in validacion.avisos:
        salida.aviso(aviso)
    if not validacion.ok:
        for err in validacion.errores:
            salida.error(err)
        return EXIT_USO
    if args.limit and args.limit > 0 and job is None:
        salida.aviso(f"--limit {args.limit}: solo se procesarán las primeras {len(textos)} órdenes.")

    # ---- estimación ----
    est = estimar(config, textos, client=_cliente_para_conteo() if args.dry_run else None)
    for aviso in est.avisos:
        if aviso not in validacion.avisos:
            salida.aviso(aviso)
    if args.dry_run:
        if args.json:
            print(json.dumps({
                "ordenes": est.ordenes, "modo": est.modo, "tokens_entrada": est.tokens_entrada,
                "coste_maximo_usd": str(est.coste_maximo), "tope_gasto_usd": str(config.tope_gasto_usd),
            }, ensure_ascii=False))
        else:
            print("\n".join(est.resumen_lineas()))
            tope = "sin tope" if config.tope_gasto_usd == 0 else formatear_usd(config.tope_gasto_usd)
            print(f"Tope configurado   : {tope}")
            print("\nNo se ha enviado nada (--dry-run).")
        return EXIT_OK
    if not textos:
        salida.info("No queda nada pendiente en ese lote.")
        # Aun así se exporta lo que haya, y se sale como completado.

    # Lo que viene gasta dinero y toca el gasto acumulado: que no lo haga a la vez otra copia del programa.
    bloqueo = BloqueoDeInstancia()
    if not bloqueo.adquirir():
        salida.error(aviso_de_otra_copia(bloqueo.dueno))
        return EXIT_USO
    try:
        return _ejecutar_lote(args, salida, config, job, textos, est)
    finally:
        bloqueo.liberar()


def _ejecutar_lote(
    args: argparse.Namespace, salida: _Salida, config: Configuracion, job: Optional[Job], textos: list[str], est: Any
) -> int:
    if job is None:
        job = Job.crear(textos, config)
        job.save()
        salida.info(f"Lote {job.job_id}: {len(textos)} órdenes · {config.modelo} · modo {config.modo} · "
                    f"peor caso {formatear_usd(est.coste_maximo)}")

    # ---- ejecución ----
    try:
        # Los lotes con órdenes sin enviar nunca se borran (lo garantiza la propia función).
        purgar_jobs_antiguos(ConfigManager.cargar().retencion_dias, proteger=job.job_id)
    except Exception:
        logger.exception("No se pudieron purgar los lotes antiguos")

    clase = BatchEngine if config.es_lote else SequentialEngine
    motor = clase(config, job)
    cb = _callbacks(salida, args)
    razon = StopReason.FATAL
    try:
        with KeepAwake():
            razon = motor.ejecutar(cb) if job.hay_trabajo() else StopReason.COMPLETED
    except KeyboardInterrupt:
        salida.info("\nDetenido por el usuario. El lote queda guardado.")
        if job.modo == "lote" and job.batch_id:
            salida.info(
                "El lote sigue procesándose en el servidor y se cobrará al terminar. "
                f"Para cancelarlo: --cancelar-lote {job.job_id}"
            )
        motor.detener()
        razon = StopReason.STOPPED

    # ---- salida ----
    exportado = None
    if args.exportar:
        try:
            exportado = exportar(job, Path(args.exportar), args.formato)
            salida.info(f"Resultados exportados a {exportado}")
        except (ErrorDeExportacion, OSError) as e:
            salida.error(f"No se pudo exportar: {e}")

    resumen = _resumen_del_lote(razon, job, motor, exportado)
    if args.json:
        print(json.dumps(resumen, ensure_ascii=False))
    else:
        print(f"\n{FRASES_DE_PARADA.get(razon, razon.value)}")
        print(f"  Completadas {resumen['completadas']}/{resumen['total']}"
              f" · fallidas {resumen['fallidas']} · rechazadas {resumen['rechazadas']}"
              f" · pendientes {resumen['pendientes']}")
        print(f"  Coste de este lote: {formatear_usd(job.coste_total())}")
        if resumen["pendientes"] or resumen["fallidas"]:
            print(f"  Para continuar: --reanudar {job.job_id}")
    return resumen["codigo_de_salida"]


# ============================================================================
# 22. Interfaz: textos en español e inglés
#    (claude_batch_monitor/gui/i18n.py)
# ============================================================================
#
# Textos del modo simple en español e inglés.
#
# Solo el modo simple está traducido: es lo que ve cualquier persona al abrir el
# programa. El modo avanzado y la terminal siguen en español.


IDIOMAS = ("es", "en")
_idioma_actual = "es"

TEXTOS: dict[str, dict[str, str]] = {
    "es": {
        "app_title": "Claude · Cola de preguntas",
        "tagline": "Escribe tus preguntas. Se guardan y se envían solas; si hay que esperar, esperan.",
        "ask": "Escribe tu pregunta",
        "placeholder": "Por ejemplo: Explícame en tres frases qué es un token.",
        "send": "Enviar",
        "pause": "Pausar",
        "resume": "Reanudar",
        "hint_keys": "Enter envía · Mayús+Enter añade una línea",
        "hint_saved": "Todo se guarda. Si cierras el programa, lo pendiente sigue cuando lo abras.",
        "queue": "Cola",
        "col_q": "Pregunta",
        "col_s": "Estado",
        "answer": "Respuesta",
        "answer_empty": "Selecciona una pregunta de la cola para ver su respuesta.",
        "answer_waiting": "Esperando la respuesta…",
        "copy": "Copiar",
        "format": "Con formato",
        "copied": "Copiado.",
        "save_all": "Guardar todo…",
        "new_queue": "Nueva cola",
        "options": "Opciones",
        "st_pending": "En cola",
        "st_sending": "Enviando…",
        "st_waiting": "Esperando…",
        "st_done": "Lista",
        "st_refused": "Rechazada",
        "st_failed": "Error",
        "st_skipped": "Quitada",
        "status_idle": "Listo",
        "status_idle_n": "Listo · {n} en cola",
        "status_sending": "Enviando la pregunta {n}…",
        "status_wait_rate_limit": "La IA ha llegado a su límite por ahora. Sigo sola en {t}.",
        "status_wait_server": "La IA está saturada. Reintento en {t}.",
        "status_wait_sin_conexion": "Sin conexión a internet. Reintento en {t}.",
        "status_wait_cuota": "Voy un poco más despacio para no pasar el límite. Sigo en {t}.",
        "status_paused": "En pausa. Pulsa «Reanudar» para continuar.",
        "status_paused_n": "En pausa · {n} en cola. Pulsa «Reanudar» para continuar.",
        "status_stopped": "Detenida.",
        "spend": "Gastado {gastado} de {tope}",
        "spend_unlimited": "Gastado {gastado} (sin tope)",
        "menu_copy_answer": "Copiar respuesta",
        "menu_copy_question": "Copiar pregunta",
        "menu_ask_again": "Volver a preguntar",
        "menu_remove": "Quitar de la cola",
        "banner_budget": "Has llegado al tope de gasto ({gastado} de {tope}). La cola se ha detenido; "
                         "lo ya respondido está guardado.",
        "banner_no_credit": "Tu cuenta de Anthropic se ha quedado sin saldo. Añade crédito y pulsa «Reanudar».",
        "banner_account_limit": "Tu cuenta de Anthropic ha alcanzado su límite de gasto y no acepta más "
                                "preguntas por ahora.",
        "banner_auth": "La clave de API no es válida o ha caducado. Cámbiala en Opciones.",
        "banner_model": "El modelo elegido ya no está disponible. Elige otro en Opciones.",
        "banner_network": "Se cortó la conexión con Anthropic. Cuando vuelva internet, pulsa «Reanudar».",
        "banner_fatal": "La cola se ha detenido por un error.",
        "banner_detail": "Detalle: {texto}",
        "btn_raise_cap": "Subir el tope…",
        "btn_open_console": "Abrir la consola de Anthropic",
        "btn_change_key": "Cambiar la clave…",
        "btn_choose_model": "Elegir modelo…",
        "btn_retry": "Reanudar",
        # primera vez
        "welcome_title": "Primeros pasos",
        "welcome_text": "Esta aplicación envía tus preguntas a Claude y las guarda en una cola: si hay que "
                        "esperar (límite de uso, sin conexión…), espera sola y sigue. Para funcionar necesita "
                        "una clave de API de Anthropic.",
        "welcome_step1": "1. Crea una cuenta en la consola de Anthropic.",
        "welcome_step2": "2. Añade un poco de saldo (con unos pocos dólares hay para cientos de preguntas).",
        "welcome_step3": "3. Crea una «API key», cópiala y pégala aquí.",
        "welcome_open_console": "Abrir la consola de Anthropic",
        "welcome_paste": "Pegar",
        "welcome_show": "Mostrar la clave",
        "welcome_test": "Probar y guardar",
        "welcome_privacy": "Pagas a Anthropic solo por lo que uses; esta aplicación no cobra nada. La clave "
                           "se guarda en el llavero de tu sistema y solo se envía a Anthropic.",
        "key_testing": "Probando la clave…",
        "key_ok": "Clave válida.",
        "key_saved_keyring": "Guardada en el llavero de tu sistema.",
        "key_saved_memory": "No pude guardarla de forma segura: tendrás que pegarla cada vez que abras el "
                            "programa (o definir la variable ANTHROPIC_API_KEY).",
        "key_empty": "Pega primero tu clave.",
        "key_bad": "Anthropic no reconoce esa clave. Comprueba que la copiaste entera.",
        "key_offline": "No hay conexión con Anthropic. Comprueba tu internet e inténtalo de nuevo.",
        "key_other": "No se pudo comprobar la clave: {texto}",
        # opciones
        "opt_title": "Opciones",
        "opt_key": "Clave de API",
        "opt_key_env": "Viene de la variable de entorno ANTHROPIC_API_KEY (edítala allí para cambiarla).",
        "opt_key_keyring": "Guardada en el llavero de tu sistema.",
        "opt_key_memory": "Solo en memoria (esta sesión).",
        "opt_key_none": "No hay clave.",
        "opt_model": "Modelo",
        "opt_load_models": "Cargar modelos",
        "opt_models_loaded": "{n} modelos cargados.",
        "opt_cap": "Tope de gasto (USD)",
        "opt_cap_help": "La cola se detiene al llegar a este gasto. 0 = sin tope (no recomendado).",
        "opt_autostart": "Continuar sola lo pendiente al abrir el programa",
        "opt_language": "Idioma (al reiniciar)",
        "opt_theme": "Tema (al reiniciar)",
        "theme_auto": "Automático",
        "theme_light": "Claro",
        "theme_dark": "Oscuro",
        "opt_instructions": "Instrucciones para todas las respuestas (opcional)",
        "opt_instructions_help": "Por ejemplo: «Responde siempre en español y con frases cortas». Se envían con cada pregunta.",
        "queue_empty": "Aquí irán apareciendo tus preguntas, de una en una.",
        "answer_placeholder": "Aquí verás la respuesta, escribiéndose en directo.",
        "unexpected_title": "Algo ha fallado",
        "unexpected_text": "Ha ocurrido un error inesperado dentro del programa. Lo que ya estaba guardado no se ha perdido.\n\n"
                           "{error}\n\nEl detalle está en el registro: {log}",
        "opt_advanced": "Modo avanzado…",
        "opt_folder": "Abrir carpeta de respuestas",
        "opt_save": "Guardar",
        "opt_cancel": "Cancelar",
        "opt_bad_cap": "El tope debe ser un número igual o mayor que 0.",
        "opt_about": "Versión {v}",
        # cierre
        "close_title": "Quedan preguntas en la cola",
        "close_text": "Hay {n} pregunta(s) sin responder. Se guardan y seguirán la próxima vez que abras el programa.",
        "close_quit": "Salir y guardar",
        "close_background": "Seguir en segundo plano",
        "close_cancel": "Volver",
        "new_confirm": "La cola actual se guarda en disco y empiezas una en blanco. ¿Continuar?",
        "new_blocked": "Hay preguntas en marcha. Pausa la cola antes de empezar otra.",
        "save_title": "Guardar las respuestas",
        "save_done": "Guardado en {ruta}",
        "save_error": "No se pudo guardar: {texto}",
        "save_empty": "Todavía no hay nada que guardar.",
        "resumed_found": "Tienes {n} pregunta(s) sin enviar de la última vez.",
        "tray_open": "Abrir",
        "tray_quit": "Salir",
        "no_key_title": "Falta la clave",
        # instalación de la librería que falta
        "install_title": "Falta una pieza",
        "install_text": "Para hablar con Claude, este programa necesita la librería oficial de Anthropic "
                        "(se llama «anthropic»). Se instala una sola vez, con este comando, y no se instala nada más:",
        "install_ask": "¿Instalarla ahora? [s/N] ",
        "install_button": "Instalar ahora",
        "install_quit": "Salir",
        "install_working": "Instalando… puede tardar un minuto.",
        "install_done": "Listo. Abriendo el programa…",
        "install_failed": "No se pudo instalar.",
        "install_copy": "Copiar comando",
        "install_retry": "Reintentar",
        "install_manual": "También puedes hacerlo tú en una terminal con el comando de arriba y volver a abrir el programa.",
        "install_loop": "La instalación terminó, pero Python sigue sin encontrar la librería «anthropic». "
                        "Instálala a mano con: {comando}",
        # error que nadie esperaba al abrir el programa
        "fatal_title": "No se pudo abrir el programa",
        "fatal_text": "Ha ocurrido un error inesperado y el programa se ha cerrado.",
        "fatal_log": "Los detalles están en este archivo (si pides ayuda, adjúntalo):\n{ruta}",
        # una segunda copia del programa
        "already_title": "Ya está abierto",
        "already_text": "Ya hay otra copia de Claude Batch Monitor en marcha{proceso}.\n\n"
                        "Dos copias a la vez se pisarían el gasto acumulado y el tope de gasto dejaría de proteger. "
                        "Búscala en la barra de tareas o en la bandeja del sistema, o ciérrala antes de abrir esta.",
    },
    "en": {
        "app_title": "Claude · Question queue",
        "tagline": "Write your questions. They are saved and sent on their own; if something has to wait, they wait.",
        "ask": "Write your question",
        "placeholder": "For example: Explain in three sentences what a token is.",
        "send": "Send",
        "pause": "Pause",
        "resume": "Resume",
        "hint_keys": "Enter sends · Shift+Enter adds a line",
        "hint_saved": "Everything is saved. If you close the program, what is pending continues when you reopen it.",
        "queue": "Queue",
        "col_q": "Question",
        "col_s": "Status",
        "answer": "Answer",
        "answer_empty": "Select a question in the queue to see its answer.",
        "answer_waiting": "Waiting for the answer…",
        "copy": "Copy",
        "format": "Formatted",
        "copied": "Copied.",
        "save_all": "Save all…",
        "new_queue": "New queue",
        "options": "Options",
        "st_pending": "Queued",
        "st_sending": "Sending…",
        "st_waiting": "Waiting…",
        "st_done": "Done",
        "st_refused": "Declined",
        "st_failed": "Error",
        "st_skipped": "Removed",
        "status_idle": "Ready",
        "status_idle_n": "Ready · {n} queued",
        "status_sending": "Sending question {n}…",
        "status_wait_rate_limit": "The AI hit its usage limit for now. Resuming by itself in {t}.",
        "status_wait_server": "The AI is overloaded. Retrying in {t}.",
        "status_wait_sin_conexion": "No internet connection. Retrying in {t}.",
        "status_wait_cuota": "Slowing down a little to stay under the limit. Continuing in {t}.",
        "status_paused": "Paused. Press “Resume” to continue.",
        "status_paused_n": "Paused · {n} queued. Press “Resume” to continue.",
        "status_stopped": "Stopped.",
        "spend": "Spent {gastado} of {tope}",
        "spend_unlimited": "Spent {gastado} (no cap)",
        "menu_copy_answer": "Copy answer",
        "menu_copy_question": "Copy question",
        "menu_ask_again": "Ask again",
        "menu_remove": "Remove from queue",
        "banner_budget": "You reached your spending cap ({gastado} of {tope}). The queue stopped; "
                         "everything already answered is saved.",
        "banner_no_credit": "Your Anthropic account is out of credit. Add credit and press “Resume”.",
        "banner_account_limit": "Your Anthropic account reached its spending limit and is not accepting "
                                "questions for now.",
        "banner_auth": "The API key is invalid or expired. Change it in Options.",
        "banner_model": "The selected model is no longer available. Pick another in Options.",
        "banner_network": "The connection to Anthropic dropped. When the internet is back, press “Resume”.",
        "banner_fatal": "The queue stopped because of an error.",
        "banner_detail": "Detail: {texto}",
        "btn_raise_cap": "Raise the cap…",
        "btn_open_console": "Open the Anthropic console",
        "btn_change_key": "Change the key…",
        "btn_choose_model": "Choose a model…",
        "btn_retry": "Resume",
        "welcome_title": "Getting started",
        "welcome_text": "This app sends your questions to Claude and keeps them in a queue: if it has to wait "
                        "(usage limit, no connection…), it waits by itself and carries on. It needs an "
                        "Anthropic API key to work.",
        "welcome_step1": "1. Create an account in the Anthropic console.",
        "welcome_step2": "2. Add a little credit (a few dollars cover hundreds of questions).",
        "welcome_step3": "3. Create an “API key”, copy it and paste it here.",
        "welcome_open_console": "Open the Anthropic console",
        "welcome_paste": "Paste",
        "welcome_show": "Show the key",
        "welcome_test": "Test and save",
        "welcome_privacy": "You pay Anthropic only for what you use; this app charges nothing. The key is "
                           "stored in your system keyring and is only ever sent to Anthropic.",
        "key_testing": "Testing the key…",
        "key_ok": "Valid key.",
        "key_saved_keyring": "Saved in your system keyring.",
        "key_saved_memory": "I couldn't store it securely: you will need to paste it every time you open the "
                            "program (or set the ANTHROPIC_API_KEY variable).",
        "key_empty": "Paste your key first.",
        "key_bad": "Anthropic doesn't recognise that key. Check that you copied all of it.",
        "key_offline": "Can't reach Anthropic. Check your internet and try again.",
        "key_other": "The key could not be checked: {texto}",
        "opt_title": "Options",
        "opt_key": "API key",
        "opt_key_env": "Comes from the ANTHROPIC_API_KEY environment variable (edit it there to change it).",
        "opt_key_keyring": "Stored in your system keyring.",
        "opt_key_memory": "Memory only (this session).",
        "opt_key_none": "No key.",
        "opt_model": "Model",
        "opt_load_models": "Load models",
        "opt_models_loaded": "{n} models loaded.",
        "opt_cap": "Spending cap (USD)",
        "opt_cap_help": "The queue stops when it reaches this spend. 0 = no cap (not recommended).",
        "opt_autostart": "Carry on with what's pending when the program opens",
        "opt_language": "Language (on restart)",
        "opt_theme": "Theme (on restart)",
        "theme_auto": "Automatic",
        "theme_light": "Light",
        "theme_dark": "Dark",
        "opt_instructions": "Instructions for every answer (optional)",
        "opt_instructions_help": "For example: “Always answer in English, in short sentences.” They are sent with every question.",
        "queue_empty": "Your questions will appear here, one by one.",
        "answer_placeholder": "You will see the answer here, as it is written.",
        "unexpected_title": "Something went wrong",
        "unexpected_text": "An unexpected error happened inside the program. Nothing that was already saved has been lost.\n\n"
                           "{error}\n\nThe details are in the log: {log}",
        "opt_advanced": "Advanced mode…",
        "opt_folder": "Open the answers folder",
        "opt_save": "Save",
        "opt_cancel": "Cancel",
        "opt_bad_cap": "The cap must be a number equal to or greater than 0.",
        "opt_about": "Version {v}",
        "close_title": "Questions are still queued",
        "close_text": "{n} question(s) are unanswered. They are saved and will continue the next time you open the program.",
        "close_quit": "Quit and save",
        "close_background": "Keep running in the background",
        "close_cancel": "Go back",
        "new_confirm": "The current queue is kept on disk and you start a blank one. Continue?",
        "new_blocked": "Questions are in progress. Pause the queue before starting another.",
        "save_title": "Save the answers",
        "save_done": "Saved to {ruta}",
        "save_error": "Couldn't save: {texto}",
        "save_empty": "There is nothing to save yet.",
        "resumed_found": "You have {n} unsent question(s) from last time.",
        "tray_open": "Open",
        "tray_quit": "Quit",
        "no_key_title": "Missing key",
        # installing the missing library
        "install_title": "One piece is missing",
        "install_text": "To talk to Claude, this program needs Anthropic's official library (called “anthropic”). "
                        "It is installed once, with this command, and nothing else is installed:",
        "install_ask": "Install it now? [y/N] ",
        "install_button": "Install now",
        "install_quit": "Quit",
        "install_working": "Installing… this can take a minute.",
        "install_done": "Done. Opening the program…",
        "install_failed": "The installation failed.",
        "install_copy": "Copy command",
        "install_retry": "Try again",
        "install_manual": "You can also run the command above yourself in a terminal and open the program again.",
        "install_loop": "The installation finished, but Python still cannot find the “anthropic” library. "
                        "Install it by hand with: {comando}",
        # an error nobody expected while opening the program
        "fatal_title": "The program could not start",
        "fatal_text": "An unexpected error happened and the program has closed.",
        "fatal_log": "The details are in this file (attach it if you ask for help):\n{ruta}",
        # a second copy of the program
        "already_title": "Already open",
        "already_text": "Another copy of Claude Batch Monitor is already running{proceso}.\n\n"
                        "Two copies at once would overwrite each other's running spend total and the spend cap "
                        "would stop protecting you. Look for it in the taskbar or the system tray, or close it "
                        "before opening this one.",
    },
}


def _idioma_de(codigo: str) -> str:
    """`es` o `en` si el código de idioma (es_ES.UTF-8, en-US, Spanish_Spain…) dice uno de los dos; si no, "".

    `C` y `POSIX` no dicen ningún idioma.
    """
    codigo = (codigo or "").strip().lower()
    for idioma, nombre in (("es", "spanish"), ("en", "english")):
        if re.match(rf"^({idioma}|{nombre})([_\-.@:]|$)", codigo):
            return idioma
    return ""


def _codigos_del_sistema() -> list[str]:
    """Los códigos de idioma que el sistema declara, del más al menos fiable."""
    codigos: list[str] = []
    try:
        if sys.platform == "win32":
            import ctypes

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
            codigos.append(locale.windows_locale.get(lcid, ""))
        elif sys.platform == "darwin":
            # Un programa abierto con doble clic no recibe LANG: el idioma está en los ajustes del sistema.
            r = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                               capture_output=True, text=True, timeout=3)
            lineas = [linea.strip(' ",()') for linea in r.stdout.splitlines()]  # ("es-ES", "en-US") -> es-ES, en-US
            codigos += [linea for linea in lineas if linea][:3]
    except Exception:
        pass
    codigos += [os.environ[v] for v in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE") if os.environ.get(v)]
    return codigos


def idioma_del_sistema() -> str:
    """`es` o `en` según el idioma del sistema; `es` si no se puede saber (o es otro idioma)."""
    for codigo in _codigos_del_sistema():
        idioma = _idioma_de(codigo)
        if idioma:
            return idioma
    return "es"


def fijar_idioma(codigo: str = "") -> str:
    """Fija el idioma del modo simple. '' = el del sistema. Devuelve el código usado."""
    global _idioma_actual
    codigo = (codigo or "").strip().lower()[:2]
    _idioma_actual = codigo if codigo in IDIOMAS else idioma_del_sistema()
    return _idioma_actual


def idioma() -> str:
    return _idioma_actual


def t(clave: str, **valores: object) -> str:
    """Texto traducido. Si falta la clave en el idioma actual cae al español, y si no, a la clave."""
    texto = TEXTOS.get(_idioma_actual, {}).get(clave) or TEXTOS["es"].get(clave) or clave
    if valores:
        try:
            return texto.format(**valores)
        except (KeyError, IndexError, ValueError):
            return texto
    return texto


# ============================================================================
# 23. Interfaz: colores, tipografías y estilos
#    (claude_batch_monitor/gui/estilo.py)
# ============================================================================
#
# Aspecto común de las ventanas: colores, tipografías y estilos de ttk.


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


# ============================================================================
# 24. Interfaz: el icono, dibujado con código
#    (claude_batch_monitor/gui/icono.py)
# ============================================================================
#
# El icono de la aplicación: un cuadrado azul con tres barras, una cola de preguntas.
#
# No hay ningún archivo de imagen: se dibuja con código y se entrega como PNG en memoria (solo con la
# biblioteca estándar). Sirve igual para la ventana (tkinter), la bandeja del sistema y los iconos de
# los ejecutables.


AZUL = (47, 111, 237)
BLANCO = (255, 255, 255)
MUESTRAS = 3  # puntos por lado de cada píxel: bordes suaves sin que el dibujo tarde


def _dentro_de_rectangulo_redondeado(x: float, y: float, x0: float, y0: float, x1: float, y1: float, radio: float) -> bool:
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    dx = max(x0 + radio - x, 0.0, x - (x1 - radio))
    dy = max(y0 + radio - y, 0.0, y - (y1 - radio))
    return dx * dx + dy * dy <= radio * radio


# Formas en coordenadas de 0 a 1: el cuadrado y las tres barras que se acortan (lo que está en cola).
_CUADRADO = (0.06, 0.06, 0.94, 0.94, 0.22)
_ALTO_BARRA = 0.085
_BARRAS = tuple(
    (0.24, 0.30 + i * 0.16, 0.24 + ancho, 0.30 + i * 0.16 + _ALTO_BARRA, _ALTO_BARRA / 2)
    for i, ancho in enumerate((0.52, 0.40, 0.28))
)


def _color_del_punto(x: float, y: float) -> tuple[bool, bool]:
    """(¿dentro del cuadrado?, ¿dentro de una barra?)."""
    if not _dentro_de_rectangulo_redondeado(x, y, *_CUADRADO):
        return False, False
    return True, any(_dentro_de_rectangulo_redondeado(x, y, *barra) for barra in _BARRAS)


def _chunk(tipo: bytes, datos: bytes) -> bytes:
    return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)


def png_del_icono(tamano: int = 256) -> bytes:
    """El icono como PNG RGBA de `tamano` × `tamano` píxeles."""
    filas = bytearray()
    paso = 1.0 / (tamano * MUESTRAS)
    for j in range(tamano):
        filas.append(0)  # sin filtro
        for i in range(tamano):
            cuadrado = barra = 0
            for sj in range(MUESTRAS):
                y = (j * MUESTRAS + sj + 0.5) * paso
                for si in range(MUESTRAS):
                    x = (i * MUESTRAS + si + 0.5) * paso
                    dentro, es_barra = _color_del_punto(x, y)
                    cuadrado += dentro
                    barra += es_barra
            if not cuadrado:
                filas += b"\x00\x00\x00\x00"
                continue
            mezcla = barra / cuadrado  # parte del píxel ocupada por una barra blanca
            r, g, b = (round(AZUL[k] + (BLANCO[k] - AZUL[k]) * mezcla) for k in range(3))
            filas += bytes((r, g, b, round(255 * cuadrado / (MUESTRAS * MUESTRAS))))
    cabecera = struct.pack(">IIBBBBB", tamano, tamano, 8, 6, 0, 0, 0)  # 8 bits, RGBA
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", cabecera) + _chunk(b"IDAT", zlib.compress(bytes(filas), 9)) + _chunk(b"IEND", b"")


def imagen_pil(tamano: int = 64):
    """El icono como imagen de Pillow (la bandeja del sistema la necesita)."""
    import io

    from PIL import Image

    return Image.open(io.BytesIO(png_del_icono(tamano))).convert("RGBA")


# ============================================================================
# 25. Interfaz: ventanas y paneles comunes
#    (claude_batch_monitor/gui/comun.py)
# ============================================================================
#
# Piezas de interfaz que usan los dos modos: ventanas secundarias y paneles con desplazamiento.


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
            messagebox.showerror(t("unexpected_title"), t("unexpected_text", error=firma, log=LOG_FILE), parent=root)
        except Exception:
            pass  # un fallo al avisar no puede causar otro

    root.report_callback_exception = al_fallar  # type: ignore[method-assign]


# ============================================================================
# 26. Interfaz: puente entre hilos
#    (claude_batch_monitor/gui/bridge.py)
# ============================================================================
#
# Puente entre los hilos de trabajo y el hilo de la ventana.
#
# tkinter solo es seguro desde el hilo que creó la ventana. Llamar a `root.after()`
# desde un hilo trabajador "suele funcionar" y a veces revienta con "main thread is
# not in main loop". Aquí los hilos solo meten funciones en una cola (que sí es
# segura entre hilos) y es el hilo de la ventana quien las ejecuta, por turnos.


MAX_POR_TURNO = 200  # para que una ráfaga de eventos no congele la ventana


class UiBridge:
    def __init__(self, root: Any, intervalo_ms: int = 30):
        self.root = root
        self.intervalo_ms = intervalo_ms
        self._cola: queue.SimpleQueue = queue.SimpleQueue()
        self._vivo = True
        self._after_id: Optional[str] = None
        self._turno()

    def post(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Pide ejecutar `fn(*args)` en el hilo de la ventana. Se puede llamar desde cualquier hilo."""
        if self._vivo:
            self._cola.put((fn, args, kwargs))

    def publicar(self, fn: Callable[[], None]) -> None:
        """Igual que `post`, con la firma que espera `Cola(publicar=...)`."""
        self.post(fn)

    def _turno(self) -> None:
        if not self._vivo:
            return
        for _ in range(MAX_POR_TURNO):
            try:
                fn, args, kwargs = self._cola.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args, **kwargs)
            except Exception:
                # Un evento que falla (p. ej. un widget ya destruido) no debe parar a los demás.
                logger.exception("Fallo al ejecutar un evento de la interfaz")
        try:
            self._after_id = self.root.after(self.intervalo_ms, self._turno)
        except Exception:  # la ventana ya no existe
            self._vivo = False

    def drenar(self) -> None:
        """Ejecuta ya lo pendiente (para tests y para el cierre ordenado)."""
        self._turno_sin_reprogramar()

    def _turno_sin_reprogramar(self) -> None:
        while True:
            try:
                fn, args, kwargs = self._cola.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception("Fallo al ejecutar un evento de la interfaz")

    def cerrar(self) -> None:
        self._vivo = False
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None


# ============================================================================
# 27. Interfaz: icono en la bandeja del sistema (opcional)
#    (claude_batch_monitor/gui/tray.py)
# ============================================================================
#
# Icono en la bandeja del sistema (opcional: necesita `pystray` y `Pillow`).
#
# Permite cerrar la ventana y dejar la cola trabajando en segundo plano. Si las
# librerías o el entorno gráfico no lo permiten, `disponible()` devuelve False y la
# interfaz simplemente no ofrece esa opción.


def bandeja_disponible() -> bool:
    try:
        import PIL.Image  # noqa: F401
        import pystray  # noqa: F401

        return True
    except Exception:
        return False


def _imagen_de_bandeja():
    return imagen_pil(64)


class BandejaDelSistema:
    """`al_abrir` y `al_salir` se llaman desde el hilo del icono: que cada uno
    se encargue de pasar el trabajo al hilo de la ventana (por ejemplo con el puente)."""

    def __init__(
        self,
        titulo: str,
        texto_abrir: str,
        texto_salir: str,
        al_abrir: Callable[[], None],
        al_salir: Callable[[], None],
    ):
        self.titulo, self.texto_abrir, self.texto_salir = titulo, texto_abrir, texto_salir
        self.al_abrir, self.al_salir = al_abrir, al_salir
        self._icono: Optional[object] = None

    def mostrar(self) -> bool:
        if self._icono is not None:
            return True
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem(self.texto_abrir, lambda *_: self.al_abrir(), default=True),
                pystray.MenuItem(self.texto_salir, lambda *_: self.al_salir()),
            )
            icono = pystray.Icon(APP_NAME, _imagen_de_bandeja(), self.titulo, menu)
            self._icono = icono
            threading.Thread(target=self._ejecutar, args=(icono,), daemon=True).start()
            return True
        except Exception:
            logger.warning("No se pudo crear el icono de la bandeja", exc_info=True)
            self._icono = None
            return False

    def _ejecutar(self, icono: object) -> None:
        try:
            icono.run()  # type: ignore[attr-defined]
        except Exception:
            logger.warning("El icono de la bandeja terminó con un error", exc_info=True)

    def ocultar(self) -> None:
        icono, self._icono = self._icono, None
        if icono is not None:
            try:
                icono.stop()  # type: ignore[attr-defined]
            except Exception:
                pass


# ============================================================================
# 28. Interfaz: respuestas con formato
#    (claude_batch_monitor/gui/formato.py)
# ============================================================================
#
# Mostrar las respuestas con formato: títulos, negrita, cursiva, código, listas, citas y tablas.
#
# Claude contesta en Markdown; leerlo con los asteriscos y las almohadillas a la vista cansa. `analizar` convierte
# el texto en trozos con estilos (no necesita ventanas, así que se prueba sola) y `mostrar` los escribe en un
# `Text` de tkinter. Es un Markdown sencillo, pensado para respuestas de chat: si algo no se entiende se deja tal
# cual, y si algo falla se muestra el texto original. Nunca se pierde ni se cambia una respuesta: copiar y exportar
# siempre usan el texto original.


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


# ============================================================================
# 29. Interfaz avanzada: ventanas secundarias
#    (claude_batch_monitor/gui/dialogos.py)
# ============================================================================
#
# Ventanas secundarias del modo avanzado (el modo simple tiene las suyas en `simple.py`).


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
            mostrar_texto_con_formato(self.texto_respuesta, self.respuesta, self.app.p, self.app.f, bool(self.var_formato.get()))
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
        lineas.append(f"Tope configurado   : {'sin tope' if tope == 0 else formatear_usd(tope, 2)}")
        self.lineas = lineas
        marco, texto = _texto_solo_lectura(cuerpo, app, len(lineas) + 1, mono=True)
        marco.grid(row=0, column=0, sticky="ew")
        texto.configure(width=88)
        _escribir_en(texto, "\n".join(lineas))
        fila = 1
        explicacion = (
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


# ============================================================================
# 30. Modo simple
#    (claude_batch_monitor/gui/simple.py)
# ============================================================================
#
# Modo simple: escribir una pregunta, pulsar Enviar y ver cómo la cola trabaja sola.
#
# Pensado para alguien que no quiere saber qué es un lote ni un token. Todo lo
# técnico (modelo, tope de gasto, clave) está en «Opciones»; el resto del tiempo
# solo hay una caja de texto, un botón y una lista que se va llenando.


TOPE_INICIAL = Decimal("2")
MAX_TOKENS_INICIAL = 8000
INTERVALO_DE_REPINTADO_MS = 60  # lo que llega por streaming se pinta como mucho 16 veces por segundo


def formatear_tiempo(segundos: float) -> str:
    """`75.2` -> `1:15`."""
    s = max(0, int(round(segundos)))
    return f"{s // 60}:{s % 60:02d}"


def config_inicial() -> Configuracion:
    """Configuración guardada o, la primera vez, una prudente para quien no sabe de qué va esto."""
    primera_vez = not CONFIG_FILE.exists()
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
                   command=lambda: abrir_ruta(RESPONSES_DIR)).pack(side="left")
        ttk.Label(enlaces, text=t("opt_about", v=APP_VERSION), style="Pequeno.TLabel").pack(side="right")

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
        self._repintado_pendiente = False
        self._seleccion_auto = True
        self._placeholder = False
        self._banner_fijo = False
        self._ultimo_restante: Optional[float] = None
        self._dibujando_estado = EstadoCola()
        self.bandeja: Optional[BandejaDelSistema] = None
        self._usar_bandeja = usar_bandeja and bandeja_disponible()
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
        if not self._repintado_pendiente:
            self._repintado_pendiente = True
            self._luego(INTERVALO_DE_REPINTADO_MS, self._repintar_parcial)

    def _repintar_parcial(self) -> None:
        """Pinta lo recibido hasta ahora. Los trozos que llegan en un instante se juntan en un solo repintado.

        Con un repintado por trozo, una respuesta larga sin saltos de línea dejaba la ventana sin responder mientras
        llegaba: tkinter vuelve a trazar la línea entera en cada inserción (con 6 KB en una línea, 7 ms por trozo y
        subiendo).
        """
        self._repintado_pendiente = False
        elegida = self._seleccionada()
        if elegida is not None and elegida in self._parcial:  # si ya terminó, `on_orden` pintó la definitiva
            self._escribir_respuesta(self._parcial[elegida])

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
            mostrar_texto_con_formato(self.respuesta, texto, self.p, self.f, self.config.mostrar_formato)
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
        self.bandeja = BandejaDelSistema(
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


# ============================================================================
# 31. Modo avanzado
#    (claude_batch_monitor/gui/app.py)
# ============================================================================
#
# Modo avanzado: todos los ajustes, lotes grandes (CSV/JSONL), resultados, exportación y reanudación.
#
# Hace lo mismo que la terminal, con ventana. Para preguntar cosas sueltas está el modo simple.
# Esta ventana solo dibuja y recoge ajustes; el trabajo lo hace `EjecutorLote` en otro hilo y
# cada evento llega por el puente al hilo de la ventana, que es el único que toca tkinter.


ETIQUETA_COLA = ETIQUETA
enviar_prueba_telegram = probar_telegram
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
        self.bandeja: Optional[BandejaDelSistema] = None
        self._usar_bandeja = usar_bandeja and bandeja_disponible()
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
        RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
        self._abrir(RESPONSES_DIR)

    def abrir_logs(self) -> None:
        try:
            LOG_FILE.touch(exist_ok=True)
        except OSError:
            pass
        self._abrir(LOG_FILE)

    def abrir_datos(self) -> None:
        self._abrir(DATA_DIR)

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
            self.bandeja = BandejaDelSistema(
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


# ============================================================================
# 32. Abrir la interfaz
#    (claude_batch_monitor/gui/__init__.py)
# ============================================================================
#
# Interfaces gráficas (tkinter): modo simple y modo avanzado.


EXIT_YA_ABIERTO = 5  # el mismo código de «uso incorrecto» de la terminal


def _activar_dpi_alto() -> None:
    """En Windows, sin esto las ventanas salen borrosas en pantallas con escala superior al 100 %."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:
        pass  # solo es estética: nunca debe impedir abrir el programa


def modo_pedido(argv: list[str]) -> Optional[str]:
    """`simple` o `avanzada` si se pidió en la línea de órdenes; None si no."""
    if "--avanzado" in argv or "--avanzada" in argv:
        return "avanzada"
    if "--simple" in argv:
        return "simple"
    return None


def _avisar_de_otra_copia(dueno: str) -> None:
    """Dice, con una ventana (no todo el mundo ve la consola), que ya hay otra copia abierta."""
    import tkinter as tk
    from tkinter import messagebox

    fijar_idioma(ConfigManager.cargar().idioma)
    texto = t("already_text", proceso=f" (PID {dueno})" if dueno else "")
    print(texto, file=sys.stderr)
    try:
        raiz = tk.Tk()
        raiz.withdraw()
        messagebox.showwarning(t("already_title"), texto, parent=raiz)
        raiz.destroy()
    except tk.TclError:
        pass  # sin pantalla: ya se imprimió


def abrir_interfaz(argv: Optional[list[str]] = None) -> int:
    """Abre la interfaz y alterna entre los dos modos mientras la persona lo pida.

    Sin `--simple` ni `--avanzado` se abre el modo que se usó la última vez (la primera vez,
    el simple). Cambiar de modo desde la ventana guarda la elección para la próxima.
    """
    import tkinter as tk

    bloqueo = BloqueoDeInstancia()
    if not bloqueo.adquirir():
        _avisar_de_otra_copia(bloqueo.dueno)
        return EXIT_YA_ABIERTO
    try:
        return _abrir(argv, tk)
    finally:
        bloqueo.liberar()


def _abrir(argv: Optional[list[str]], tk: Any) -> int:
    modo = modo_pedido(list(argv or [])) or ConfigManager.cargar().interfaz
    _activar_dpi_alto()
    try:
        while True:
            if modo == "avanzada":
                cambio = ejecutar_avanzada()
            else:
                cambio = ejecutar_simple()
            if cambio in ("simple", "avanzada") and cambio != modo:
                modo = cambio
                continue
            return 0
    except tk.TclError as error:
        # Típico en un servidor o por SSH: no hay pantalla donde abrir la ventana.
        print(
            f"No se pudo abrir la ventana ({error}).\n"
            "Si estás en un servidor sin escritorio, usa la terminal:  "
            f"{comando_para_ejecutar()} --cli --help",
            file=sys.stderr,
        )
        return 1


# ============================================================================
# 33. Instalar la librería de Anthropic si falta
#    (claude_batch_monitor/instalacion.py)
# ============================================================================
#
# Instalar la única librería externa que necesita el programa si falta: `anthropic`.
#
# Lo que se ejecuta es exactamente `python -m pip install anthropic` (con `--user` fuera de un entorno
# virtual). No se instala ni se descarga nada más, y siempre se pregunta antes. Si el sistema no deja
# instalar paquetes en su Python (Debian, Ubuntu, Fedora…), se crea una carpeta propia del programa
# (`venv`, dentro de su carpeta de datos) y se instala ahí.


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
    return DATA_DIR / "venv"


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


# ============================================================================
# 34. Arranque
#    (claude_batch_monitor/__main__.py)
# ============================================================================
#
# Punto de entrada: abre la interfaz o, con `--cli`, la terminal.


# Con cualquiera de estas opciones se entiende que se quiere la terminal, aunque
# se olvide `--cli`: abrir una ventana en un servidor sin pantalla no ayuda a nadie.
_OPCIONES_DE_TERMINAL = {
    "--cli", "--mensaje", "--reanudar", "--cancelar-lote", "--listar", "--dry-run", "--version", "--json", "--help", "-h",
}


def _es_terminal(argv: list[str]) -> bool:
    return any(a.split("=", 1)[0] in _OPCIONES_DE_TERMINAL for a in argv)


def _avisar_sin_consola() -> None:
    """Un ejecutable de Windows sin ventana de texto no puede imprimir: se explica con un cuadro de diálogo."""
    mensaje = (
        "Esta versión del programa no tiene consola, así que no puede usarse desde la terminal.\n\n"
        "Para la terminal, abre el programa con Python y ejecuta:\n"
        f"    {nombre_de_python()} claude_batch_monitor.py --cli --help"
    )
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, mensaje, "Claude Batch Monitor", 0x40)  # type: ignore[attr-defined]
    except Exception:
        pass


def _avisar_de_error_fatal(error: BaseException) -> int:
    """Un error que nadie esperaba: se anota en el registro y se explica con una ventana.

    Quien abre el programa con doble clic no ve la consola (se cierra sola al terminar): sin esta
    ventana, el programa «simplemente no se abre» y no hay forma de saber por qué.
    """
    logger.error("El programa se cerró por un error inesperado", exc_info=error)
    traceback.print_exception(type(error), error, error.__traceback__)
    fijar_idioma("")
    texto = f"{t('fatal_text')}\n\n{type(error).__name__}: {error}\n\n{t('fatal_log', ruta=LOG_FILE)}"
    try:
        import tkinter as tk
        from tkinter import messagebox

        raiz = tk.Tk()
        raiz.withdraw()
        messagebox.showerror(t("fatal_title"), texto, parent=raiz)
        raiz.destroy()
    except Exception:  # sin pantalla o sin tkinter: ya se imprimió arriba
        pass
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    terminal = _es_terminal(argv)

    if terminal and (sys.stdout is None or sys.stderr is None):
        _avisar_sin_consola()
        return 1

    # Lo único que no viene con Python y hace falta es la librería de Anthropic: si falta, se ofrece
    # instalarla (con un botón, o con una pregunta en la terminal) en vez de morir con un error.
    if necesita_la_libreria(argv, terminal) and falta_anthropic():
        return resolver_falta_de_la_libreria(terminal)

    if terminal:
        return main_cli([a for a in argv if a != "--cli"])

    try:
        import tkinter  # noqa: F401
    except ImportError:
        # Sin tkinter la ventana no puede abrirse, y en Windows un entry point gráfico
        # moriría sin consola y sin explicación. Se dice qué hacer.
        mensaje = (
            "Este Python no incluye tkinter, que hace falta para la interfaz gráfica.\n"
            "  - Linux (Debian/Ubuntu):  sudo apt install python3-tk\n"
            "  - macOS/Windows: reinstala Python desde python.org marcando 'tcl/tk'.\n"
            f"Mientras tanto puedes usar la terminal:  {comando_para_ejecutar()} --cli --help"
        )
        print(mensaje, file=sys.stderr)
        try:  # si no hay consola (ejecutable sin ventana de texto), al menos que se vea
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, mensaje, "Claude Batch Monitor", 0x10)  # type: ignore[attr-defined]
        except Exception:
            pass
        return 1

    try:
        return abrir_interfaz(argv)
    except Exception as error:  # noqa: BLE001 - último recurso: que quien no ve la consola sepa qué pasó
        return _avisar_de_error_fatal(error)


if __name__ == "__main__":
    sys.exit(main())
