"""Estimación del coste ANTES de gastar nada.

El número que importa es el peor caso: cada respuesta usando `max_tokens`
completo. Es una cota, no una predicción; lo normal es que el gasto real sea
bastante menor, pero un tope de gasto solo protege si se razona con el peor caso.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from .paths import logger
from .pricing import (
    ESCRITURA_CACHE_5M,
    Precios,
    buscar_modelo,
    coste_maximo,
    estimar_tokens,
    formatear_usd,
)

if TYPE_CHECKING:
    from .config import Configuracion

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
    plan: bool = False  # con el plan de Claude no se paga por token: no hay precios ni coste máximo

    def resumen_lineas(self) -> list[str]:
        origen = "contados por la API" if self.exacta else "estimación local"
        if self.plan:
            return [
                f"Órdenes detectadas : {self.ordenes}",
                f"Modo               : {self.modo}",
                f"Tokens entrada     : ~{self.tokens_entrada:,} ({origen})",
                "Coste              : incluido en tu plan (gasta el cupo de tu plan, no dinero por token)",
            ]
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
    if config.es_plan:
        tokens = [estimar_tokens(o) + estimar_tokens(config.system_prompt) for o in ordenes]
        return Estimacion(
            ordenes=len(ordenes), tokens_entrada=sum(tokens), exacta=False, coste_maximo=Decimal(0),
            coste_solo_entrada=Decimal(0), precios=precios, modo="plan de Claude", max_tokens=0, plan=True,
        )
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
