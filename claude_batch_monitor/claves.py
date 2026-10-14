"""Comprobar y guardar la clave de API, y elegir un modelo que exista.

Sin dependencias de interfaz: lo usan la pantalla de bienvenida, las opciones y
el modo avanzado, y se puede probar contra el SDK real con un servidor simulado.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from .pricing import DEFAULT_MODEL, MODELOS_SUGERIDOS, normalizar_id
from .state import KEY_API, SecureStorage

MAX_MODELOS = 500


class ErrorDeClave(Exception):
    """La clave no se pudo comprobar. `tipo` dice por qué, para redactar un mensaje claro."""

    def __init__(self, tipo: str, detalle: str = ""):
        super().__init__(detalle or tipo)
        self.tipo = tipo  # invalida | sin_conexion | libreria | otro
        self.detalle = detalle


def _crear_cliente(clave: str) -> Any:
    try:
        import anthropic
    except ImportError as error:  # solo pasa si se eligió la API sin haber instalado su librería
        raise ErrorDeClave("libreria") from error
    return anthropic.Anthropic(api_key=clave, timeout=15, max_retries=0)


def _traducir(error: Exception) -> ErrorDeClave:
    import anthropic

    from .core.errors import Categoria, analizar_error

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
