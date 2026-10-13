"""Throttling proactivo a partir de las cabeceras de rate limit.

Lección tomada de la práctica documentada para la API de Anthropic: hay que
**leer las cabeceras `anthropic-ratelimit-*` en TODAS las respuestas, no solo en
los 429**. Anthropic aplica un token bucket que se rellena de forma continua
(peticiones/min, tokens de entrada/min y tokens de salida/min) y las cabeceras
dicen cuánto queda en cada cubo. Frenar un poco antes de agotarlos sale más
barato que comerse el 429 y esperar el reset entero — y con concurrencia > 1
evita que todos los hilos choquen a la vez contra el muro.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .paths import logger

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
