"""Catálogo de modelos, precios y cálculo de coste (siempre en `Decimal`).

Los precios salen de https://platform.claude.com/docs/en/about-claude/pricing
(revisado en octubre de 2026). CAMBIAN con el tiempo: por eso el usuario puede
sobrescribirlos a mano, y por eso un modelo desconocido se valora con el precio
CARO de su familia, nunca con el barato. Un tope de gasto calculado con precios
demasiado bajos protege menos de lo que promete.

Reglas de facturación que se modelan aquí:
- Escritura de caché de 5 min: 1,25 x la entrada; de 1 h: 2 x.
- Lectura de caché: 0,1 x la entrada (0,05 x en Opus 5.5; 0,025 x en Fable 5.1).
- Message Batches API: 50 % de descuento en todo, y se acumula con la caché.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

MILLON = Decimal(1_000_000)
D = Decimal

ESCRITURA_CACHE_5M = D("1.25")
ESCRITURA_CACHE_1H = D("2")
DESCUENTO_LOTE = D("0.5")

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
    lectura_cache: Decimal = D("0.1")  # multiplicador sobre `entrada`
    max_salida: Optional[int] = None
    contexto: Optional[int] = None
    min_cache: Optional[int] = None  # prefijo mínimo cacheable, en tokens
    esfuerzo: tuple = ()
    retirado: bool = False


def _m(id_, nombre, familia, e, s, **kw) -> ModeloInfo:
    return ModeloInfo(id_, nombre, familia, D(str(e)), D(str(s)), **kw)


_MODELOS = [
    _m("claude-fable-5-1", "Claude Fable 5.1", "fable", 10, 50, lectura_cache=D("0.025"),
       max_salida=128_000, contexto=1_000_000, min_cache=512, esfuerzo=_E5),
    _m("claude-fable-5", "Claude Fable 5", "fable", 10, 50, esfuerzo=_E5),
    _m("claude-mythos-5-1", "Claude Mythos 5.1", "mythos", 10, 50, lectura_cache=D("0.025"),
       min_cache=512, esfuerzo=_E5),
    _m("claude-mythos-5", "Claude Mythos 5", "mythos", 10, 50, esfuerzo=_E5),
    _m("claude-opus-5-5", "Claude Opus 5.5", "opus", 4, 20, lectura_cache=D("0.05"),
       max_salida=128_000, contexto=1_000_000, min_cache=512, esfuerzo=_E5),
    _m("claude-opus-5", "Claude Opus 5", "opus", 5, 25, min_cache=512, esfuerzo=_E5),
    _m("claude-opus-4-8", "Claude Opus 4.8", "opus", 5, 25, min_cache=1024, esfuerzo=_E5),
    _m("claude-opus-4-7", "Claude Opus 4.7", "opus", 5, 25, min_cache=2048, esfuerzo=_E5),
    _m("claude-opus-4-6", "Claude Opus 4.6", "opus", 5, 25, min_cache=4096, esfuerzo=_E4),
    _m("claude-opus-4-5", "Claude Opus 4.5", "opus", 5, 25, min_cache=4096, esfuerzo=_E3),
    _m("claude-opus-4-1", "Claude Opus 4.1", "opus", 15, 75, retirado=True),
    _m("claude-opus-4", "Claude Opus 4", "opus", 15, 75, retirado=True),
    _m("claude-sonnet-5-5", "Claude Sonnet 5.5", "sonnet", 2, 10,
       max_salida=128_000, contexto=1_000_000, min_cache=512, esfuerzo=_E5),
    _m("claude-sonnet-5", "Claude Sonnet 5", "sonnet", 2, 10, min_cache=1024, esfuerzo=_E5),
    _m("claude-sonnet-4-6", "Claude Sonnet 4.6", "sonnet", 3, 15, min_cache=1024, esfuerzo=_E4),
    _m("claude-sonnet-4-5", "Claude Sonnet 4.5", "sonnet", 3, 15, min_cache=1024),
    _m("claude-sonnet-4", "Claude Sonnet 4", "sonnet", 3, 15, retirado=True),
    _m("claude-haiku-4-5", "Claude Haiku 4.5", "haiku", 1, 5,
       max_salida=64_000, contexto=200_000, min_cache=4096),
    _m("claude-haiku-3-5", "Claude Haiku 3.5", "haiku", "0.8", 4, retirado=True),
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
    "fable": (D(10), D(50)),
    "mythos": (D(10), D(50)),
    "opus": (D(5), D(25)),
    "sonnet": (D(3), D(15)),
    "haiku": (D(1), D(5)),
}
_DESCONOCIDO = (D(10), D(50))

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
            base = Precios(e, s, D("0.1"), "familia", modelo)
        else:
            e, s = _DESCONOCIDO
            base = Precios(e, s, D("0.1"), "desconocido", modelo)
    if entrada is None and salida is None:
        return base
    return Precios(
        D(entrada) if entrada is not None else base.entrada,
        D(salida) if salida is not None else base.salida,
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


def _n(obj: Any, nombre: str) -> int:
    """Lee un contador de tokens de un objeto `usage` sin fiarse de que exista."""
    valor = getattr(obj, nombre, 0)
    if isinstance(valor, bool) or not isinstance(valor, (int, float, Decimal)):
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
    escritura = _n(uso, "cache_creation_input_tokens")
    desglose = getattr(uso, "cache_creation", None)
    w5 = _n(desglose, "ephemeral_5m_input_tokens") if desglose is not None else 0
    w1 = _n(desglose, "ephemeral_1h_input_tokens") if desglose is not None else 0
    if w5 + w1 < escritura:
        w5 += escritura - (w5 + w1)  # sin desglose: se asume TTL de 5 minutos
    return Consumo(
        entrada=_n(uso, "input_tokens"),
        salida=_n(uso, "output_tokens"),
        escritura_5m=w5,
        escritura_1h=w1,
        lectura_cache=_n(uso, "cache_read_input_tokens"),
    )


def coste_consumo(consumo: Consumo, precios: Precios, lote: bool = False) -> Decimal:
    e, s = precios.entrada, precios.salida
    total = (
        D(consumo.entrada) * e
        + D(consumo.escritura_5m) * e * ESCRITURA_CACHE_5M
        + D(consumo.escritura_1h) * e * ESCRITURA_CACHE_1H
        + D(consumo.lectura_cache) * e * precios.lectura_cache
        + D(consumo.salida) * s
    ) / MILLON
    return total * DESCUENTO_LOTE if lote else total


def coste_uso(uso: Any, precios: Precios, lote: bool = False) -> Decimal:
    return coste_consumo(leer_consumo(uso), precios, lote)


def coste_maximo(
    tokens_entrada: int,
    max_tokens: int,
    precios: Precios,
    lote: bool = False,
    mult_entrada: Decimal = D(1),
) -> Decimal:
    """Peor caso de UNA petición: toda la entrada a precio completo y `max_tokens` de salida.

    `mult_entrada` > 1 cubre la primera escritura de caché (1,25 x), que es más
    cara que no cachear.
    """
    total = (
        D(max(0, tokens_entrada)) * precios.entrada * mult_entrada
        + D(max(0, max_tokens)) * precios.salida
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
    return f"${D(valor):.{decimales}f}"


def formatear_precio(valor: Decimal) -> str:
    """`$2` o `$0.80` (hasta 3 decimales, sin ceros sobrantes)."""
    texto = f"{D(valor):.3f}".rstrip("0").rstrip(".")
    if "." in texto and len(texto.split(".")[1]) < 2:
        texto += "0"
    return f"${texto}"


def descripcion_modelo(modelo: str) -> str:
    info = buscar_modelo(modelo)
    p = resolver_precios(modelo)
    nombre = info.nombre if info else modelo
    return f"{nombre}: {formatear_precio(p.entrada)} entrada / {formatear_precio(p.salida)} salida por millón"
