"""Lectura de órdenes desde texto, CSV, TSV y JSONL.

Lo normal es tener los datos en una hoja de cálculo, no en un `.txt` con
separadores. Si la columna pedida no existe se falla AQUÍ, antes de gastar nada,
listando las que sí hay: procesar filas vacías y darse cuenta después de pagar
es el peor resultado posible.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Optional, Union

from .parsing import DEFAULT_SEPARATOR, split_messages

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
    for n, linea in enumerate(contenido.splitlines(), start=1):
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
    for linea in contenido.splitlines():
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
