"""Punto de entrada del ejecutable: PyInstaller necesita un script, no un paquete."""

from claude_batch_monitor.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
