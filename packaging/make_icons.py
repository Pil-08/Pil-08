"""Genera los iconos de los ejecutables (necesita Pillow): `python packaging/make_icons.py`.

Escribe el .ico (Windows), el .icns (macOS) y un PNG grande (Linux) a partir del mismo dibujo que usa
la ventana (`claude_batch_monitor/gui/icono.py`). Los resultados se guardan en el repositorio; no hace
falta regenerarlos salvo que cambie el dibujo.
"""

from pathlib import Path

from claude_batch_monitor.gui.icono import imagen_pil

AQUI = Path(__file__).parent

imagen_pil(512).save(AQUI / "icon.png")
imagen_pil(256).save(AQUI / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
imagen_pil(512).save(AQUI / "icon.icns")
print("Iconos generados en", AQUI)
