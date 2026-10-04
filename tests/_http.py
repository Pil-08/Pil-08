"""La librería HTTP que usa el SDK instalado.

`anthropic` 0.x depende de `httpx`; desde la 1.0 depende de `httpx2`. Los tests
construyen peticiones y respuestas para fabricar errores del SDK, así que
necesitan la misma que use el SDK de este entorno.
"""

import anthropic._base_client as _base

httpx = getattr(_base, "httpx2", None) or _base.httpx

__all__ = ["httpx"]
