"""El icono de la aplicación: un cuadrado azul con tres barras, una cola de preguntas.

No hay ningún archivo de imagen: se dibuja con código y se entrega como PNG en memoria (solo con la
biblioteca estándar). Sirve igual para la ventana (tkinter), la bandeja del sistema y los iconos de
los ejecutables.
"""

from __future__ import annotations

import struct
import zlib

AZUL = (47, 111, 237)
BLANCO = (255, 255, 255)
MUESTRAS = 3  # puntos por lado de cada píxel: bordes suaves sin que el dibujo tarde


def _dentro_de_rectangulo_redondeado(x: float, y: float, x0: float, y0: float, x1: float, y1: float, radio: float) -> bool:
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    dx = max(x0 + radio - x, 0.0, x - (x1 - radio))
    dy = max(y0 + radio - y, 0.0, y - (y1 - radio))
    return dx * dx + dy * dy <= radio * radio


# Formas en coordenadas de 0 a 1: el cuadrado y las tres barras que se acortan (lo que está en cola).
_CUADRADO = (0.06, 0.06, 0.94, 0.94, 0.22)
_ALTO_BARRA = 0.085
_BARRAS = tuple(
    (0.24, 0.30 + i * 0.16, 0.24 + ancho, 0.30 + i * 0.16 + _ALTO_BARRA, _ALTO_BARRA / 2)
    for i, ancho in enumerate((0.52, 0.40, 0.28))
)


def _color_del_punto(x: float, y: float) -> tuple[bool, bool]:
    """(¿dentro del cuadrado?, ¿dentro de una barra?)."""
    if not _dentro_de_rectangulo_redondeado(x, y, *_CUADRADO):
        return False, False
    return True, any(_dentro_de_rectangulo_redondeado(x, y, *barra) for barra in _BARRAS)


def _chunk(tipo: bytes, datos: bytes) -> bytes:
    return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)


def png_del_icono(tamano: int = 256) -> bytes:
    """El icono como PNG RGBA de `tamano` × `tamano` píxeles."""
    filas = bytearray()
    paso = 1.0 / (tamano * MUESTRAS)
    for j in range(tamano):
        filas.append(0)  # sin filtro
        for i in range(tamano):
            cuadrado = barra = 0
            for sj in range(MUESTRAS):
                y = (j * MUESTRAS + sj + 0.5) * paso
                for si in range(MUESTRAS):
                    x = (i * MUESTRAS + si + 0.5) * paso
                    dentro, es_barra = _color_del_punto(x, y)
                    cuadrado += dentro
                    barra += es_barra
            if not cuadrado:
                filas += b"\x00\x00\x00\x00"
                continue
            mezcla = barra / cuadrado  # parte del píxel ocupada por una barra blanca
            r, g, b = (round(AZUL[k] + (BLANCO[k] - AZUL[k]) * mezcla) for k in range(3))
            filas += bytes((r, g, b, round(255 * cuadrado / (MUESTRAS * MUESTRAS))))
    cabecera = struct.pack(">IIBBBBB", tamano, tamano, 8, 6, 0, 0, 0)  # 8 bits, RGBA
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", cabecera) + _chunk(b"IDAT", zlib.compress(bytes(filas), 9)) + _chunk(b"IEND", b"")


def imagen_pil(tamano: int = 64):
    """El icono como imagen de Pillow (la bandeja del sistema la necesita)."""
    import io

    from PIL import Image

    return Image.open(io.BytesIO(png_del_icono(tamano))).convert("RGBA")
