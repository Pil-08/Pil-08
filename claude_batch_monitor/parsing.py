"""Troceado del texto del usuario en órdenes independientes."""

from __future__ import annotations

import re

DEFAULT_SEPARATOR = "---"


def split_messages(text: str, separator: str = DEFAULT_SEPARATOR) -> list[str]:
    """Parte el texto en bloques por una LÍNEA que sea exactamente el separador.

    Predecible y sin colisión accidental: `----` (4 guiones) no parte si el
    separador es `---`. Un `re.split(r"-{3,}")` rompe el frontmatter y las
    reglas horizontales de Markdown dentro de una orden.
    """
    sep = (separator or "").strip() or DEFAULT_SEPARATOR
    pattern = rf"(?m)^[ \t]*{re.escape(sep)}[ \t]*$"
    return [m.strip() for m in re.split(pattern, text) if m.strip()]
