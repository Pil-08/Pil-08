"""Genera los iconos (necesita Pillow): `python packaging/make_icons.py`.

Escribe el PNG que lee la ventana, y el .ico (Windows), el .icns (macOS) y un PNG grande (Linux)
que usa el empaquetado. Los resultados se guardan en el repositorio; no hace falta regenerarlos.
"""

from pathlib import Path

from claude_batch_monitor.gui.icono import RUTA_PNG, dibujar

AQUI = Path(__file__).parent

dibujar(256).save(RUTA_PNG)
dibujar(512).save(AQUI / "icon.png")
dibujar(256).save(AQUI / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
dibujar(512).save(AQUI / "icon.icns")
print("Iconos generados en", AQUI, "y", RUTA_PNG)
