"""Clasificación de los errores de la API de Anthropic.

Lo importante es distinguir cosas que el SDK trata igual y que exigen respuestas
opuestas:

- 429 *transitorio* (rate limit por minuto): esperar `retry-after` y seguir.
- 429 *de tope de gasto* (`enforced_spend_limit_reached`): NO tiene `retry-after`
  y no se levanta hasta el día 1 del mes siguiente. Reintentar es inútil: hay que
  parar y decirlo. Tratarlo como un rate limit más deja el programa dando vueltas
  durante semanas.
- 400 por un límite de gasto que puso el propio usuario ("You have reached your
  specified API usage limits"): la orden está bien; la petición no es "inválida".
- 402 o "credit balance is too low": sin saldo.

Referencia: https://platform.claude.com/docs/en/api/errors
y https://platform.claude.com/docs/en/api/rate-limits#reaching-your-spend-cap
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class Categoria(str, Enum):
    RATE_LIMIT = "rate_limit"      # esperar y reintentar; no cuenta como fallo
    LIMITE_CUENTA = "account_limit"  # tope de gasto (del tier o propio): parar
    SIN_SALDO = "no_credit"        # parar
    AUTH = "auth"                  # 401 / 403
    PETICION = "bad_request"       # 400 genuino
    MODELO = "model_not_found"     # 404 sobre el modelo
    NO_ENCONTRADO = "not_found"
    DEMASIADO_GRANDE = "too_large"  # 413
    SERVIDOR = "server"            # 5xx / 529 / error en mitad del stream: backoff
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
    if status >= 500 or tipo in ("overloaded_error", "api_error", "timeout_error"):
        return devolver(Categoria.SERVIDOR)
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
