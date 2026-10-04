# -*- mode: python ; coding: utf-8 -*-
"""Especificación de PyInstaller.

Desde la raíz del repositorio:

    pip install -e ".[all]" pyinstaller
    pyinstaller packaging/claude-batch-monitor.spec --noconfirm

Genera `dist/ClaudeBatchMonitor.exe` (Windows), `dist/ClaudeBatchMonitor` (Linux) o
`dist/ClaudeBatchMonitor.app` (macOS). Es una aplicación de ventana: no abre consola.
Los ejecutables no están firmados: Windows (SmartScreen) y macOS (Gatekeeper) avisarán la primera vez.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

RAIZ = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH lo define PyInstaller
NOMBRE = "ClaudeBatchMonitor"
IDENTIFICADOR = "io.github.pil-08.claude-batch-monitor"

# Estos paquetes cargan sus módulos por nombre en tiempo de ejecución (un backend por sistema operativo):
# el análisis estático no los ve y sin esto el ejecutable no encontraría llavero, bandeja ni notificaciones.
imports_ocultos = []
for paquete in ("keyring.backends", "pystray", "plyer.platforms"):
    imports_ocultos += collect_submodules(paquete)

icono = {
    "win32": RAIZ / "packaging" / "icon.ico",
    "darwin": RAIZ / "packaging" / "icon.icns",
}.get(sys.platform)

a = Analysis(  # noqa: F821
    [str(RAIZ / "packaging" / "launcher.py")],
    pathex=[str(RAIZ)],
    binaries=[],
    datas=[(str(RAIZ / "claude_batch_monitor" / "gui" / "icono.png"), "claude_batch_monitor/gui")],
    hiddenimports=imports_ocultos,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "numpy", "matplotlib", "pandas", "scipy", "IPython", "setuptools", "pkg_resources"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

if sys.platform == "darwin":
    # En macOS la aplicación es una carpeta `.app` (el modo de un solo archivo está en desuso allí).
    exe = EXE(  # noqa: F821
        pyz, a.scripts, [], exclude_binaries=True, name=NOMBRE, console=False, icon=str(icono),
        debug=False, strip=False, upx=False,
    )
    coleccion = COLLECT(exe, a.binaries, a.datas, name=NOMBRE, strip=False, upx=False)  # noqa: F821
    app = BUNDLE(  # noqa: F821
        coleccion,
        name=f"{NOMBRE}.app",
        icon=str(icono),
        bundle_identifier=IDENTIFICADOR,
        info_plist={
            "CFBundleDisplayName": "Claude Batch Monitor",
            "NSHighResolutionCapable": True,
        },
    )
else:
    # Windows y Linux: un único archivo que se descarga y se ejecuta.
    exe = EXE(  # noqa: F821
        pyz, a.scripts, a.binaries, a.datas, [], name=NOMBRE, console=False,
        icon=str(icono) if icono else None, debug=False, strip=False, upx=False,
    )
