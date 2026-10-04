#!/usr/bin/env python3
"""Compatibilidad con la v11: `python claude_batch_monitor.py` sigue funcionando.

Todo el código vive ahora en el paquete `claude_batch_monitor/`. Este fichero solo
lo arranca; puedes seguir lanzándolo igual que antes (con o sin `--cli`).
"""

from claude_batch_monitor.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
