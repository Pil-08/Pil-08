#!/usr/bin/env python3
"""Genera `claude_batch_monitor.py`: el programa completo en un solo archivo.

Las fuentes son los módulos de `claude_batch_monitor/`. Este script los une, en orden, en un único
archivo de texto normal: sin comprimir, sin ofuscar y sin `exec`. Lo único que cambia respecto a los
módulos es lo que deja de hacer falta al estar todo junto:

  * las importaciones entre módulos (`from .config import Configuracion`) desaparecen;
  * `paths.DATA_DIR`, `tray.BandejaDelSistema`… pasan a ser `DATA_DIR`, `BandejaDelSistema`…;
  * las importaciones de la biblioteca estándar se reúnen una sola vez arriba;
  * tkinter queda protegido: sin tkinter, el archivo se puede cargar y la terminal funciona.

    python tools/build_single_file.py            # escribe claude_batch_monitor.py
    python tools/build_single_file.py --check    # falla si el archivo no está al día (para CI)

Si dos módulos definen un mismo nombre, o algo no se puede unir con seguridad, el script se detiene
con un mensaje en vez de generar un archivo dudoso.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import io
import re
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

RAIZ = Path(__file__).resolve().parent.parent
PAQUETE = "claude_batch_monitor"
SALIDA = RAIZ / "claude_batch_monitor.py"

# (módulo, título de la sección). Es el orden de lectura y respeta las dependencias de importación:
# lo que se usa al cargar un módulo ya está definido antes. El script lo comprueba.
SECCIONES: list[tuple[str, str]] = [
    ("paths", "Carpetas de datos, registro de eventos y escritura atómica"),
    ("parsing", "Trocear el texto del usuario en órdenes"),
    ("franja", "Franja horaria: trabajar solo entre dos horas"),
    ("pricing", "Modelos, precios y coste"),
    ("config", "Configuración"),
    ("state", "Estado guardado y almacén seguro de secretos"),
    ("job", "El lote: órdenes, estado de cada una y guardado en disco"),
    ("inputs", "Leer órdenes desde texto, CSV, TSV y JSONL"),
    ("ratelimit", "Respetar los límites de la API"),
    ("export", "Exportar resultados"),
    ("estimator", "Estimar el coste antes de gastar"),
    ("notify", "Avisos: escritorio y Telegram"),
    ("platform_utils", "Sistema operativo: anti-suspensión y abrir carpetas"),
    ("instancia_unica", "Una sola copia del programa gastando a la vez"),
    ("core.errors", "Motores: clasificar los errores de la API"),
    ("core.base", "Motores: lo común (contabilidad, esperas, guardado y parada)"),
    ("core.sequential", "Motor inmediato (Messages API)"),
    ("core.batch", "Motor de lote (Message Batches API, mitad de precio)"),
    ("core.claude_code", "Plan de Claude: hablar con Claude Code (claude -p)"),
    ("core.plan", "Motor del plan de Claude (tu suscripción, sin pagar por token)"),
    ("claves", "Clave de API y elección de modelo"),
    ("ejecutor", "Ejecutar un lote en segundo plano (modo avanzado) y elegir el motor"),
    ("cola", "La cola del modo simple"),
    ("cli", "Terminal (--cli)"),
    ("gui.i18n", "Interfaz: textos en español e inglés"),
    ("gui.estilo", "Interfaz: colores, tipografías y estilos"),
    ("gui.icono", "Interfaz: el icono, dibujado con código"),
    ("gui.comun", "Interfaz: ventanas y paneles comunes"),
    ("gui.bridge", "Interfaz: puente entre hilos"),
    ("gui.tray", "Interfaz: icono en la bandeja del sistema (opcional)"),
    ("gui.formato", "Interfaz: respuestas con formato"),
    ("gui.dialogos", "Interfaz avanzada: ventanas secundarias"),
    ("gui.simple", "Modo simple"),
    ("gui.app", "Modo avanzado"),
    ("gui", "Abrir la interfaz"),
    ("instalacion", "Instalar la librería de Anthropic si falta"),
    ("__main__", "Arranque"),
]

# Paquetes cuyo `__init__.py` no aporta nada propio (no son una sección).
SIN_CODIGO_PROPIO = {"core"}

TKINTER = "tkinter"
ANCHO = 78


class ErrorDeConstruccion(Exception):
    pass


@dataclass
class Modulo:
    nombre: str  # "core.base"
    titulo: str
    ruta: Path
    fuente: str
    arbol: ast.Module
    importaciones_al_cargar: set[str] = field(default_factory=set)
    definidos: dict[str, int] = field(default_factory=dict)  # nombre de nivel superior -> línea

    @property
    def es_paquete(self) -> bool:
        return self.ruta.name == "__init__.py"


# ----------------------------------------------------------------------
# Lectura de los módulos
# ----------------------------------------------------------------------


def _ruta_del_modulo(nombre: str) -> Path:
    base = RAIZ / PAQUETE / Path(*nombre.split("."))
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    raise ErrorDeConstruccion(f"No existe el módulo {nombre!r} ({base}).")


def _modulos_del_paquete() -> set[str]:
    raiz = RAIZ / PAQUETE
    encontrados = set()
    for ruta in raiz.rglob("*.py"):
        partes = list(ruta.relative_to(raiz).with_suffix("").parts)
        if partes[-1] == "__init__":
            partes = partes[:-1]
        if partes:
            encontrados.add(".".join(partes))
    return encontrados


def cargar_modulos() -> list[Modulo]:
    modulos = []
    for nombre, titulo in SECCIONES:
        ruta = _ruta_del_modulo(nombre)
        fuente = ruta.read_text(encoding="utf-8")
        modulos.append(Modulo(nombre, titulo, ruta, fuente, ast.parse(fuente, filename=str(ruta))))
    # Un módulo nuevo en el paquete obliga a decidir su sitio en SECCIONES.
    faltan = _modulos_del_paquete() - {m.nombre for m in modulos} - SIN_CODIGO_PROPIO
    if faltan:
        raise ErrorDeConstruccion(f"Módulos que no están en SECCIONES: {sorted(faltan)}. Añádelos donde les corresponda.")
    return modulos


# ----------------------------------------------------------------------
# Análisis del árbol
# ----------------------------------------------------------------------


def _es_importacion_del_paquete(nodo: ast.AST) -> bool:
    if isinstance(nodo, ast.ImportFrom):
        return nodo.level > 0 or (nodo.module or "").split(".")[0] == PAQUETE
    if isinstance(nodo, ast.Import):
        return any(a.name.split(".")[0] == PAQUETE for a in nodo.names)
    return False


def _es_type_checking(nodo: ast.AST) -> bool:
    return (
        isinstance(nodo, ast.If)
        and isinstance(nodo.test, ast.Name)
        and nodo.test.id == "TYPE_CHECKING"
        and not nodo.orelse
    )


def _es_guarda_main(nodo: ast.AST) -> bool:
    return (
        isinstance(nodo, ast.If)
        and isinstance(nodo.test, ast.Compare)
        and isinstance(nodo.test.left, ast.Name)
        and nodo.test.left.id == "__name__"
    )


def _es_docstring(nodo: ast.AST) -> bool:
    return (
        isinstance(nodo, ast.Expr)
        and isinstance(nodo.value, ast.Constant)
        and isinstance(nodo.value.value, str)
    )


def _resolver_relativo(actual: Modulo, nivel: int, modulo: Optional[str]) -> str:
    """El nombre (sin el paquete raíz) al que apunta `from <puntos>modulo import ...`."""
    partes = actual.nombre.split(".")
    if not actual.es_paquete:
        partes = partes[:-1]
    if nivel - 1 > len(partes):
        raise ErrorDeConstruccion(f"{actual.nombre}: una importación relativa sale del paquete.")
    partes = partes[: len(partes) - (nivel - 1)]
    if modulo:
        partes = partes + modulo.split(".")
    return ".".join(partes)


def _sentencias_anidadas(nodo: ast.stmt) -> list[ast.stmt]:
    """Lo que hay dentro de try/if de nivel superior (se ejecuta al cargar el módulo)."""
    if isinstance(nodo, ast.Try):
        return nodo.body + nodo.orelse + nodo.finalbody + [s for h in nodo.handlers for s in h.body]
    if isinstance(nodo, ast.If):
        return nodo.body + nodo.orelse
    return []


def _nombres_de_nivel_superior(arbol: ast.Module) -> dict[str, int]:
    """Los nombres que el módulo deja definidos al cargarse (def, class, asignaciones e importaciones en try/if)."""
    nombres: dict[str, int] = {}

    def anotar(nodo: ast.stmt, anidado: bool) -> None:
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombres.setdefault(nodo.name, nodo.lineno)
        elif isinstance(nodo, ast.Assign):
            for destino in nodo.targets:
                for n in ast.walk(destino):
                    if isinstance(n, ast.Name):
                        nombres.setdefault(n.id, nodo.lineno)
        elif isinstance(nodo, (ast.AnnAssign, ast.AugAssign)) and isinstance(nodo.target, ast.Name):
            nombres.setdefault(nodo.target.id, nodo.lineno)
        elif anidado and isinstance(nodo, (ast.Import, ast.ImportFrom)) and not _es_importacion_del_paquete(nodo):
            for a in nodo.names:
                nombres.setdefault(a.asname or a.name.split(".")[0], nodo.lineno)
        for sub in _sentencias_anidadas(nodo):
            anotar(sub, True)

    for nodo in arbol.body:
        anotar(nodo, False)
    return nombres


def _dependencias_al_cargar(m: Modulo, por_nombre: dict[str, Modulo]) -> set[str]:
    """Módulos del paquete que se importan al cargar este (no en funciones ni bajo `TYPE_CHECKING`)."""
    dependencias: set[str] = set()

    def visitar(sentencias: list[ast.stmt]) -> None:
        for s in sentencias:
            if isinstance(s, ast.ImportFrom) and _es_importacion_del_paquete(s):
                destino = _resolver_relativo(m, s.level, s.module)
                for a in s.names:
                    sub = f"{destino}.{a.name}" if destino else a.name
                    dependencias.add(sub if sub in por_nombre else destino)
            elif not _es_type_checking(s):
                visitar(_sentencias_anidadas(s))

    visitar(m.arbol.body)
    dependencias.discard(m.nombre)
    return dependencias


def _nombres_ligados_en_algun_sitio(arbol: ast.AST) -> set[str]:
    """Todo nombre que en algún lugar del módulo se asigna, se recibe como parámetro o se define."""
    nombres: set[str] = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            nombres.add(n.id)
        elif isinstance(n, ast.arg):
            nombres.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            nombres.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombres.add(n.name)
    return nombres


# ----------------------------------------------------------------------
# Edición del texto por posiciones
# ----------------------------------------------------------------------


class Texto:
    """El código de un módulo en bytes, con ediciones por posición (línea y columna del AST)."""

    def __init__(self, fuente: str):
        self.datos = fuente.encode("utf-8")
        self.lineas = self.datos.splitlines(keepends=True)
        self.inicio_de_linea = [0]
        for linea in self.lineas:
            self.inicio_de_linea.append(self.inicio_de_linea[-1] + len(linea))
        self.ediciones: list[tuple[int, int, bytes]] = []

    def reemplazar_nodo(self, nodo: ast.AST, nuevo: str) -> None:
        a = self.inicio_de_linea[nodo.lineno - 1] + nodo.col_offset  # type: ignore[attr-defined]
        b = self.inicio_de_linea[nodo.end_lineno - 1] + nodo.end_col_offset  # type: ignore[attr-defined]
        self.ediciones.append((a, b, nuevo.encode("utf-8")))

    def reemplazar_lineas(self, nodo: ast.AST, nuevo: str) -> None:
        """Sustituye las líneas completas que ocupa `nodo`. Si se quita del todo, también las líneas en blanco que le siguen."""
        a = self.inicio_de_linea[nodo.lineno - 1]  # type: ignore[attr-defined]
        ultima = nodo.end_lineno  # type: ignore[attr-defined]
        if not nuevo:
            while ultima < len(self.lineas) and not self.lineas[ultima].strip():
                ultima += 1
        self.ediciones.append((a, self.inicio_de_linea[ultima], nuevo.encode("utf-8")))

    def aplicar(self) -> str:
        resultado = bytearray()
        cursor = 0
        for a, b, nuevo in sorted(self.ediciones, key=lambda e: (e[0], e[1])):
            if a < cursor:
                raise ErrorDeConstruccion("Dos ediciones se solapan: revisa las importaciones del módulo.")
            resultado += self.datos[cursor:a] + nuevo
            cursor = b
        resultado += self.datos[cursor:]
        return resultado.decode("utf-8")


# ----------------------------------------------------------------------
# Importaciones que se reúnen arriba
# ----------------------------------------------------------------------


@dataclass
class Importaciones:
    simples: dict[str, Optional[str]] = field(default_factory=dict)  # "os.path" -> alias
    desde: dict[str, set[tuple[str, Optional[str]]]] = field(default_factory=dict)  # módulo -> {(nombre, alias)}

    def anotar(self, nodo: ast.stmt, donde: str) -> None:
        if isinstance(nodo, ast.Import):
            for a in nodo.names:
                if self.simples.setdefault(a.name, a.asname) != a.asname:
                    raise ErrorDeConstruccion(f"{donde}: `import {a.name}` con dos alias distintos.")
        elif isinstance(nodo, ast.ImportFrom):
            if nodo.module == "__future__":
                return  # se pone una vez, arriba del todo
            conjunto = self.desde.setdefault(nodo.module or "", set())
            for a in nodo.names:
                if a.name == "*":
                    raise ErrorDeConstruccion(f"{donde}: `import *` no se puede unir con seguridad.")
                conjunto.add((a.name, a.asname))

    def nombres_ligados(self) -> dict[str, str]:
        """Nombre que queda definido -> de dónde viene."""
        ligados: dict[str, str] = {}
        for modulo, alias in self.simples.items():
            ligados[alias or modulo.split(".")[0]] = f"import {modulo}"
        for modulo, nombres in self.desde.items():
            for nombre, alias in nombres:
                ligados[alias or nombre] = f"from {modulo} import {nombre}"
        return ligados


# ----------------------------------------------------------------------
# Transformación de un módulo
# ----------------------------------------------------------------------


@dataclass
class Transformado:
    modulo: Modulo
    cuerpo: str
    docstring: str


def transformar(modulo: Modulo, por_nombre: dict[str, Modulo], importaciones: Importaciones) -> Transformado:
    arbol, donde = modulo.arbol, modulo.nombre
    texto = Texto(modulo.fuente)

    # 1. El docstring del módulo pasa a ser un comentario bajo el título de la sección.
    docstring = ast.get_docstring(arbol, clean=True) or ""
    if arbol.body and _es_docstring(arbol.body[0]):
        texto.reemplazar_lineas(arbol.body[0], "")

    quitar: set[int] = set()  # id() de las sentencias que desaparecen
    reemplazos: dict[int, str] = {}  # id() de sentencia -> asignaciones de alias que la sustituyen
    alias_de_modulo: dict[str, str] = {}  # `from . import paths [as p]` -> {"paths": "paths"}

    # 2. Importaciones del propio paquete: desaparecen.
    for nodo in ast.walk(arbol):
        if not _es_importacion_del_paquete(nodo):
            continue
        if isinstance(nodo, ast.Import):
            raise ErrorDeConstruccion(f"{donde}:{nodo.lineno}: usa importaciones relativas dentro del paquete.")
        assert isinstance(nodo, ast.ImportFrom)
        destino = _resolver_relativo(modulo, nodo.level, nodo.module)
        quitar.add(id(nodo))
        asignaciones = []
        for a in nodo.names:
            if a.name == "*":
                raise ErrorDeConstruccion(f"{donde}:{nodo.lineno}: `import *` no se puede unir con seguridad.")
            submodulo = f"{destino}.{a.name}" if destino else a.name
            if submodulo in por_nombre:  # `from . import paths`: el módulo entero
                alias_de_modulo[a.asname or a.name] = submodulo
                continue
            origen = por_nombre.get(destino)
            if origen is None:
                raise ErrorDeConstruccion(f"{donde}:{nodo.lineno}: importa de {destino!r}, que no es una sección.")
            if a.name not in origen.definidos:
                raise ErrorDeConstruccion(f"{donde}:{nodo.lineno}: {destino} no define {a.name!r}.")
            if a.asname and a.asname != a.name:
                asignaciones.append(f"{a.asname} = {a.name}")
        if asignaciones:
            sangria = " " * nodo.col_offset
            reemplazos[id(nodo)] = "".join(f"{sangria}{linea}\n" for linea in asignaciones)

    # 3. Importaciones externas de nivel superior: se reúnen arriba (las de try/if y las de funciones se quedan).
    for nodo in arbol.body:
        if isinstance(nodo, (ast.Import, ast.ImportFrom)) and not _es_importacion_del_paquete(nodo):
            importaciones.anotar(nodo, donde)
            quitar.add(id(nodo))

    # 4. `if TYPE_CHECKING:` que solo tenía importaciones del paquete, y la guarda `if __name__ == ...`: se quitan enteros.
    enteros: set[int] = set()
    for nodo in ast.walk(arbol):
        if _es_type_checking(nodo) and all(id(s) in quitar for s in nodo.body):  # type: ignore[attr-defined]
            enteros.add(id(nodo))
            for s in nodo.body:  # type: ignore[attr-defined]
                quitar.discard(id(s))
                reemplazos.pop(id(s), None)
    for nodo in arbol.body:
        if _es_guarda_main(nodo):
            enteros.add(id(nodo))
    for nodo in ast.walk(arbol):
        if id(nodo) in enteros:
            texto.reemplazar_lineas(nodo, "")

    # 5. Quitar las sentencias marcadas. Un bloque que se queda sin nada recibe un `pass`.
    huecos: dict[int, int] = {}  # id(primera sentencia de un bloque vaciado) -> columna
    for padre in ast.walk(arbol):
        if isinstance(padre, ast.Module) or id(padre) in enteros:
            continue
        for campo in ("body", "orelse", "finalbody"):
            bloque = getattr(padre, campo, None)
            if isinstance(bloque, list) and bloque and isinstance(bloque[0], ast.stmt):
                if all(id(s) in quitar and id(s) not in reemplazos for s in bloque):
                    huecos[id(bloque[0])] = bloque[0].col_offset
    for nodo in ast.walk(arbol):
        if id(nodo) not in quitar:
            continue
        nuevo = reemplazos.get(id(nodo), "")
        if not nuevo and id(nodo) in huecos:
            nuevo = " " * huecos[id(nodo)] + "pass\n"
        texto.reemplazar_lineas(nodo, nuevo)

    # 6. `paths.X` -> `X`, `tray.X` -> `X`…
    if alias_de_modulo:
        locales = _nombres_ligados_en_algun_sitio(arbol)
        for alias in alias_de_modulo:
            if alias in locales:
                raise ErrorDeConstruccion(f"{donde}: el nombre {alias!r} (un módulo) se reutiliza como variable local.")
        bases = {id(n.value) for n in ast.walk(arbol) if isinstance(n, ast.Attribute)}
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name) and nodo.value.id in alias_de_modulo:
                origen = por_nombre[alias_de_modulo[nodo.value.id]]
                if nodo.attr not in origen.definidos:
                    raise ErrorDeConstruccion(f"{donde}:{nodo.lineno}: {origen.nombre} no define {nodo.attr!r}.")
                if nodo.attr in locales:
                    raise ErrorDeConstruccion(
                        f"{donde}:{nodo.lineno}: {nodo.attr!r} es también una variable local; renómbrala para poder unir."
                    )
                texto.reemplazar_nodo(nodo, nodo.attr)
            elif isinstance(nodo, ast.Name) and nodo.id in alias_de_modulo and id(nodo) not in bases:
                raise ErrorDeConstruccion(f"{donde}:{nodo.lineno}: el módulo {nodo.id!r} se usa sin `.nombre`.")

    return Transformado(modulo, texto.aplicar(), docstring)


# ----------------------------------------------------------------------
# Limpieza de texto
# ----------------------------------------------------------------------


def _lineas_dentro_de_cadenas(texto: str) -> set[int]:
    dentro: set[int] = set()
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    for tok in tokenize.generate_tokens(io.StringIO(texto).readline):
        if tok.end[0] > tok.start[0] and (tok.type == tokenize.STRING or (fstring_middle and tok.type == fstring_middle)):
            dentro.update(range(tok.start[0] + 1, tok.end[0] + 1))
    return dentro


def compactar_lineas_en_blanco(texto: str, maximo: int = 2) -> str:
    """Nunca más de `maximo` líneas en blanco seguidas, ninguna justo tras abrir un bloque, y sin espacios al final.

    No toca el interior de las cadenas de varias líneas.
    """
    protegidas = _lineas_dentro_de_cadenas(texto)
    salida: list[str] = []
    seguidas = 0
    for numero, linea in enumerate(texto.split("\n"), start=1):
        if numero in protegidas:
            salida.append(linea)
            seguidas = 0
            continue
        linea = linea.rstrip()
        if not linea:
            seguidas += 1
            tras_abrir_bloque = bool(salida) and salida[-1].endswith(":") and not salida[-1].lstrip().startswith("#")
            if seguidas > maximo or tras_abrir_bloque:
                continue
        else:
            seguidas = 0
        salida.append(linea)
    return "\n".join(salida).strip("\n") + "\n"


# ----------------------------------------------------------------------
# Montaje del archivo
# ----------------------------------------------------------------------


def _comentario(texto: str) -> str:
    return "\n".join(("# " + linea).rstrip() for linea in texto.splitlines())


def _banner(numero: int, modulo: Modulo, docstring: str) -> str:
    raya = "# " + "=" * (ANCHO - 2)
    archivo = f"{PAQUETE}/{modulo.ruta.relative_to(RAIZ / PAQUETE).as_posix()}"
    partes = [raya, f"# {numero}. {modulo.titulo}", f"#    ({archivo})", raya]
    if docstring:
        partes += ["#", _comentario(docstring)]
    return "\n".join(partes)


def _nombres_usados(texto: str) -> set[str]:
    return {n.id for n in ast.walk(ast.parse(texto)) if isinstance(n, ast.Name)}


def _formatear_importaciones(imp: Importaciones, usados: set[str]) -> tuple[str, list[str], list[str]]:
    """(importaciones normales, líneas de tkinter, nombres que tkinter deja definidos). Solo lo que se usa."""
    normales: list[str] = []
    de_tk: list[str] = []
    nombres_tk: list[str] = []

    for modulo in sorted(imp.simples, key=str.lower):
        alias = imp.simples[modulo]
        ligado = alias or modulo.split(".")[0]
        if ligado not in usados:
            continue
        linea = f"import {modulo}" + (f" as {alias}" if alias else "")
        if modulo.split(".")[0] == TKINTER:
            de_tk.append(linea)
            nombres_tk.append(ligado)
        else:
            normales.append(linea)

    for modulo in sorted(imp.desde, key=str.lower):
        pares = sorted(imp.desde[modulo], key=lambda p: (p[0].lower(), p[1] or ""))
        pares = [(n, a) for n, a in pares if (a or n) in usados]
        if not pares:
            continue
        lineas = []
        sueltos = [n for n, a in pares if not a]
        if sueltos:
            lineas.append(f"from {modulo} import {', '.join(sueltos)}")
        lineas += [f"from {modulo} import {n} as {a}" for n, a in pares if a]
        if modulo.split(".")[0] == TKINTER:
            de_tk += lineas
            nombres_tk += [a or n for n, a in pares]
        else:
            normales += lineas

    # `import x` primero y `from x import y` después, como ordena isort
    normales.sort(key=lambda linea: (linea.startswith("from "), linea.split()[1].lower()))
    return "\n".join(normales), de_tk, nombres_tk


BLOQUE_TKINTER = '''\
try:
{importaciones}
except ImportError:  # un Python sin tkinter: la terminal funciona y el arranque explica qué instalar

    class _VentanaSinTk:
        """Ocupa el sitio de una clase de tkinter cuando tkinter no está instalado."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError("Este Python no incluye tkinter: la interfaz gráfica no está disponible.")

    class _SinTk:
        """Ocupa el sitio del módulo tkinter: así el resto del archivo se puede cargar igualmente."""

        def __getattr__(self, nombre):
            return _VentanaSinTk

    {nombres} = _SinTk()
'''

CABECERA = '''\
#!/usr/bin/env python3
"""Claude Batch Monitor {version}: una cola de preguntas para Claude, con tu plan o con una clave de API.

ESTE ARCHIVO ES EL PROGRAMA COMPLETO. No hace falta nada más que Python.

  Cómo abrirlo
    1. Instala Python 3.9 o superior desde https://www.python.org/downloads/
       (Windows y macOS: deja marcada la casilla de tcl/tk. Debian/Ubuntu: sudo apt install python3-tk)
    2. Windows: doble clic en este archivo.   Otros sistemas, en una terminal:
           python3 claude_batch_monitor.py
    3. La primera pantalla pregunta cómo quieres usar Claude:
         - "Con mi plan de Claude" (Pro/Max): necesita Claude Code (el programa oficial de Anthropic)
           instalado y con tu sesión iniciada. No hay clave ni saldo ni nada que instalar aquí.
         - "Con una clave de API": ofrece instalar la única pieza externa que necesita, la librería
           oficial `anthropic` (un botón; en la terminal, responde "s"), y te pide la clave.
    4. Escribe tus preguntas y pulsa "Enviar". Si se acaba el cupo de tu plan o se alcanza un límite,
       la cola espera a que se renueve y sigue sola; con una clave, nunca gasta más que el tope que pongas.

  Terminal y servidores:   python3 claude_batch_monitor.py --cli --help

  In English
    One plain Python file: no packing, no obfuscation, no hidden steps. Install Python 3.9+, then
    double-click it on Windows or run `python3 claude_batch_monitor.py` elsewhere. The first screen
    asks how to use Claude: with your Claude plan (Pro/Max, through the Claude Code you already have
    signed in: no key, no per-question cost) or with an API key (it offers to install the `anthropic`
    library and asks for the key, with a spending cap you choose). Waiting for a limit to renew and
    retrying is automatic.

  Librerías externas
    Para usar una clave de API: `anthropic`, la oficial de Anthropic (si falta, el programa se ofrece a
    instalarla). Con tu plan de Claude no hace falta: se usa Claude Code, que ya tienes.
    Opcionales, el programa funciona sin ellas: `keyring` (la clave en el llavero del sistema),
    `pystray` y `Pillow` (icono en la bandeja), `plyer` (avisos de escritorio).
    Todo lo demás es la biblioteca estándar de Python.

  Qué hace en tu ordenador
    - Con una clave de API se conecta a api.anthropic.com. Con tu plan de Claude no se conecta a nada
      por sí mismo: arranca `claude` (Claude Code, sin modificarlo y con tu propia sesión) y es éste
      quien habla con Anthropic; el programa nunca ve tus credenciales. Nada más, salvo avisos por
      Telegram si los activas (api.telegram.org) y la descarga de `anthropic` desde PyPI si aceptas
      instalarla.
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
'''

PIE = '''


if __name__ == "__main__":
    sys.exit(main())
'''

MARCA_LINEA = "@@LINEA:{n}@@"


def _numero_de_seccion(transformados: list[Transformado], modulo: str) -> int:
    return next(n for n, t in enumerate(transformados, start=1) if t.modulo.nombre == modulo)


def _indice(transformados: list[Transformado]) -> str:
    raya = "# " + "=" * (ANCHO - 2)
    filas = [raya, "# ÍNDICE   (cada sección empieza con un título numerado: busca su número)", raya]
    for n, t in enumerate(transformados, start=1):
        filas.append(f"#   {n:>2}. {t.modulo.titulo:<64} línea {MARCA_LINEA.format(n=n)}")
    filas.append(raya)
    # El archivo va de lo más básico a lo más alto (cada sección solo usa lo anterior): para seguir el
    # programa en el orden en que se ejecuta, se empieza por el final.
    arranque, simple, motor = (_numero_de_seccion(transformados, m) for m in ("__main__", "gui.simple", "core.sequential"))
    filas.append(f"# Para leerlo en el orden en que se ejecuta: {arranque} (arranque), {simple} (modo simple), {motor} (motor inmediato).")
    return "\n".join(filas)


def _numerar_indice(texto: str, transformados: list[Transformado]) -> str:
    lineas = texto.split("\n")
    for n, t in enumerate(transformados, start=1):
        titulo = f"# {n}. {t.modulo.titulo}"
        encontradas = [i for i, linea in enumerate(lineas, start=1) if linea == titulo]
        if len(encontradas) != 1:
            raise ErrorDeConstruccion(f"No encuentro (o está repetido) el título de la sección {n}.")
        texto = texto.replace(MARCA_LINEA.format(n=n), f"{encontradas[0]:>5}")
    return texto


def _comprobar_colisiones(modulos: list[Modulo], imp: Importaciones) -> None:
    problemas: list[str] = []
    dueno: dict[str, str] = {}
    for m in modulos:
        for nombre in m.definidos:
            if nombre in dueno:
                problemas.append(f"{nombre!r} está definido en {dueno[nombre]} y en {m.nombre}")
            else:
                dueno[nombre] = m.nombre
    for nombre, origen in imp.nombres_ligados().items():
        if nombre in dueno:
            problemas.append(f"{nombre!r} es a la vez `{origen}` y una definición de {dueno[nombre]}")
    if problemas:
        raise ErrorDeConstruccion("Nombres repetidos al unir los módulos:\n  - " + "\n  - ".join(problemas))


def _comprobar_orden(modulos: list[Modulo]) -> None:
    posicion = {m.nombre: i for i, m in enumerate(modulos)}
    for m in modulos:
        for dependencia in m.importaciones_al_cargar:
            if dependencia in posicion and posicion[dependencia] > posicion[m.nombre]:
                raise ErrorDeConstruccion(f"{m.nombre} necesita {dependencia} al cargarse, pero {dependencia} va después en SECCIONES.")


def _version(modulos: list[Modulo]) -> str:
    paths = next(m for m in modulos if m.nombre == "paths")
    coincidencia = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', paths.fuente, re.M)
    if not coincidencia:
        raise ErrorDeConstruccion("No encuentro APP_VERSION en paths.py.")
    return coincidencia.group(1)


def construir() -> str:
    modulos = cargar_modulos()
    por_nombre = {m.nombre: m for m in modulos}
    for m in modulos:
        m.definidos = _nombres_de_nivel_superior(m.arbol)
    for m in modulos:
        m.importaciones_al_cargar = _dependencias_al_cargar(m, por_nombre)
    _comprobar_orden(modulos)

    importaciones = Importaciones()
    transformados = [transformar(m, por_nombre, importaciones) for m in modulos]
    _comprobar_colisiones(modulos, importaciones)

    secciones = []
    for numero, t in enumerate(transformados, start=1):
        cuerpo = compactar_lineas_en_blanco(t.cuerpo).strip("\n")
        secciones.append(_banner(numero, t.modulo, t.docstring) + "\n\n\n" + cuerpo)
    cuerpo_total = "\n\n\n".join(secciones) + PIE

    normales, de_tk, nombres_tk = _formatear_importaciones(importaciones, _nombres_usados(cuerpo_total))
    bloque_tk = ""
    if de_tk:
        sangradas = "\n".join("    " + linea for linea in de_tk)
        bloque_tk = BLOQUE_TKINTER.format(importaciones=sangradas, nombres=" = ".join(nombres_tk))

    cabecera = CABECERA.format(version=_version(modulos))
    partes = [cabecera.rstrip("\n"), normales, bloque_tk.rstrip("\n"), _indice(transformados), cuerpo_total]
    texto = compactar_lineas_en_blanco("\n\n".join(p for p in partes if p))
    return _numerar_indice(texto, transformados)


# ----------------------------------------------------------------------
# Programa
# ----------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Genera claude_batch_monitor.py a partir de los módulos del paquete.")
    ap.add_argument("--check", action="store_true", help="no escribe: falla si el archivo no está al día")
    ap.add_argument("--salida", type=Path, default=SALIDA, help="archivo de salida (por defecto, el de la raíz del proyecto)")
    args = ap.parse_args(argv)
    try:
        texto = construir()
        compile(texto, str(args.salida), "exec")  # que al menos sea Python válido
    except (ErrorDeConstruccion, SyntaxError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if args.check:
        actual = args.salida.read_text(encoding="utf-8") if args.salida.exists() else ""
        if actual != texto:
            diff = difflib.unified_diff(actual.splitlines(), texto.splitlines(), "en el repositorio", "regenerado", lineterm="", n=1)
            print("\n".join(list(diff)[:40]), file=sys.stderr)
            print(f"\n{args.salida.name} no está al día: ejecuta  python tools/build_single_file.py", file=sys.stderr)
            return 1
        print(f"{args.salida.name} está al día.")
        return 0
    args.salida.write_text(texto, encoding="utf-8", newline="\n")
    try:
        args.salida.chmod(0o755)  # empieza con `#!/usr/bin/env python3`: en Linux y macOS se puede ejecutar tal cual
    except OSError:
        pass
    print(f"Escrito {args.salida} ({len(texto.splitlines())} líneas).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
