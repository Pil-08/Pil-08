"""Troceado del texto del usuario en órdenes independientes."""

from __future__ import annotations

import re

DEFAULT_SEPARATOR = "---"

_SUSTITUTOS = re.compile("[\ud800-\udfff]")


def limpiar_sustitutos(texto: str) -> str:
    """Cambia los «sustitutos» sueltos de UTF-16 por U+FFFD.

    Un emoji partido por la mitad (de un pegado raro, de un texto cortado en mal sitio) es una cadena que Python
    acepta pero que NO se puede escribir como UTF-8: sin esto, guardar la pregunta o la respuesta fallaba.
    """
    return _SUSTITUTOS.sub("\ufffd", texto) if texto else texto


def split_messages(text: str, separator: str = DEFAULT_SEPARATOR) -> list[str]:
    """Parte el texto en bloques por una LÍNEA que sea exactamente el separador.

    Predecible y sin colisión accidental: `----` (4 guiones) no parte si el
    separador es `---`. Un `re.split(r"-{3,}")` rompe el frontmatter y las
    reglas horizontales de Markdown dentro de una orden.
    """
    sep = (separator or "").strip() or DEFAULT_SEPARATOR
    pattern = rf"(?m)^[ \t]*{re.escape(sep)}[ \t]*$"
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # `$` solo reconoce `\n`: sin esto, un texto de Windows no se parte
    return [m.strip() for m in re.split(pattern, text) if m.strip()]
