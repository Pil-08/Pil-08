"""Pasar los tests contra `claude_batch_monitor.py` (el archivo único) en vez de contra el paquete.

Con `CBM_ARCHIVO_UNICO=1`, `conftest.py` llama a `instalar()` antes de importar nada: el archivo único se
carga como un módulo y cada módulo del paquete (`claude_batch_monitor.config`, `...core.base`,
`...gui.simple`…) pasa a ser un sustituto que lee y escribe en el espacio de nombres de ese archivo. Así
los mismos tests, sin cambiar una línea, comprueban el archivo que se entrega.

(Es solo una ayuda para las pruebas: el programa nunca importa nada de aquí.)
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PAQUETE = "claude_batch_monitor"
NOMBRE_DEL_ARCHIVO = "claude_batch_monitor_archivo_unico"


def ruta_del_archivo() -> Path:
    valor = os.environ.get("CBM_ARCHIVO_UNICO", "")
    return Path(valor) if valor not in ("", "1") else RAIZ / "claude_batch_monitor.py"


class _ModuloSustituto(types.ModuleType):
    """Un módulo del paquete que en realidad lee y escribe en el espacio de nombres del archivo único."""

    def __init__(self, nombre: str, plano: types.ModuleType, es_paquete: bool):
        super().__init__(nombre)
        self.__dict__["_plano"] = plano
        if es_paquete:
            self.__dict__["__path__"] = []

    def __getattr__(self, nombre: str):
        hijo = sys.modules.get(f"{self.__name__}.{nombre}")
        if hijo is not None:
            return hijo
        try:
            return getattr(self.__dict__["_plano"], nombre)
        except AttributeError:
            raise AttributeError(f"module {self.__name__!r} has no attribute {nombre!r}") from None

    def __setattr__(self, nombre: str, valor: object) -> None:
        setattr(self.__dict__["_plano"], nombre, valor)

    def __delattr__(self, nombre: str) -> None:
        delattr(self.__dict__["_plano"], nombre)


def _nombres_de_modulos() -> list[tuple[str, bool]]:
    """(nombre completo, ¿es paquete?) de todo lo que hay en el paquete, empezando por los de arriba."""
    raiz = RAIZ / PAQUETE
    nombres = {(PAQUETE, True)}
    for ruta in raiz.rglob("*.py"):
        partes = list(ruta.relative_to(raiz).with_suffix("").parts)
        if partes[-1] == "__init__":
            partes = partes[:-1]
            if partes:
                nombres.add((".".join([PAQUETE, *partes]), True))
        else:
            nombres.add((".".join([PAQUETE, *partes]), False))
    return sorted(nombres, key=lambda n: (n[0].count("."), n[0]))


def instalar() -> types.ModuleType:
    if PAQUETE in sys.modules and not isinstance(sys.modules[PAQUETE], _ModuloSustituto):
        raise RuntimeError("El paquete ya está importado: `instalar()` debe llamarse antes de importar nada.")
    ruta = ruta_del_archivo()
    spec = importlib.util.spec_from_file_location(NOMBRE_DEL_ARCHIVO, ruta)
    assert spec is not None and spec.loader is not None
    plano = importlib.util.module_from_spec(spec)
    sys.modules[NOMBRE_DEL_ARCHIVO] = plano  # los dataclasses buscan aquí sus anotaciones
    spec.loader.exec_module(plano)
    for nombre, es_paquete in _nombres_de_modulos():
        sys.modules[nombre] = _ModuloSustituto(nombre, plano, es_paquete)
    return plano
