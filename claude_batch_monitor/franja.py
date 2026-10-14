"""Franja horaria: trabajar solo entre dos horas del día (por ejemplo, de noche).

Se escribe `HH:MM-HH:MM`. Si la hora de inicio es mayor que la de fin, la franja cruza la medianoche:
`22:00-07:00` es «desde las diez de la noche hasta las siete de la mañana».
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Optional

Franja = tuple[dt_time, dt_time]

_FORMATO = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(?:-|–|—|a)\s*(\d{1,2}):(\d{2})\s*$")


def leer_franja(texto: str) -> Optional[Franja]:
    """`22:00-07:00` -> (22:00, 07:00). Un texto vacío es «sin franja» (None). Si no se entiende, `ValueError`."""
    texto = (texto or "").strip()
    if not texto:
        return None
    m = _FORMATO.match(texto)
    if not m:
        raise ValueError(f"La franja horaria debe escribirse HH:MM-HH:MM (por ejemplo 22:00-07:00), no «{texto}».")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError(f"La franja horaria «{texto}» tiene una hora que no existe.")
    inicio, fin = dt_time(h1, m1), dt_time(h2, m2)
    if inicio == fin:
        raise ValueError("La franja horaria empieza y termina a la misma hora: escribe dos horas distintas.")
    return inicio, fin


def franja_valida(texto: str) -> bool:
    try:
        leer_franja(texto)
    except ValueError:
        return False
    return True


def texto_de_franja(franja: Franja) -> str:
    return f"{franja[0]:%H:%M}-{franja[1]:%H:%M}"


def en_franja(franja: Franja, ahora: datetime) -> bool:
    """¿Estamos dentro de la franja? El inicio cuenta; el fin, no."""
    inicio, fin = franja
    t = ahora.time().replace(second=0, microsecond=0)
    if inicio < fin:
        return inicio <= t < fin
    return t >= inicio or t < fin  # cruza la medianoche


def proximo_inicio(franja: Franja, ahora: datetime) -> datetime:
    """La próxima vez que empieza la franja (hoy si aún no ha empezado, si no mañana)."""
    candidato = datetime.combine(ahora.date(), franja[0], tzinfo=ahora.tzinfo)
    if candidato <= ahora:
        candidato += timedelta(days=1)
    return candidato


def segundos_hasta_inicio(franja: Franja, ahora: datetime) -> float:
    """0 si ya estamos dentro; si no, los segundos que faltan para que empiece."""
    if en_franja(franja, ahora):
        return 0.0
    return (proximo_inicio(franja, ahora) - ahora).total_seconds()
