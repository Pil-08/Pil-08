"""El icono de la aplicación: un cuadrado azul con tres barras, una cola de preguntas.

Se dibuja con Pillow (la bandeja y el generador de iconos lo usan). La ventana usa el PNG ya
generado (`icono.png`), que tkinter lee sin Pillow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

AZUL = "#2f6fed"
BLANCO = "#ffffff"
RUTA_PNG = Path(__file__).with_name("icono.png")


def dibujar(tamano: int = 256) -> Any:
    """Devuelve una imagen RGBA cuadrada de `tamano` píxeles (se dibuja grande y se reduce: bordes suaves)."""
    from PIL import Image, ImageDraw

    grande = 1024
    img = Image.new("RGBA", (grande, grande), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    margen = int(grande * 0.06)
    d.rounded_rectangle((margen, margen, grande - margen, grande - margen), radius=int(grande * 0.22), fill=AZUL)
    # Tres barras que se acortan: lo que está en cola.
    x0 = int(grande * 0.24)
    alto = int(grande * 0.085)
    for i, ancho in enumerate((0.52, 0.40, 0.28)):
        y = int(grande * (0.30 + i * 0.16))
        d.rounded_rectangle((x0, y, x0 + int(grande * ancho), y + alto), radius=alto // 2, fill=BLANCO)
    return img.resize((tamano, tamano), Image.LANCZOS)
