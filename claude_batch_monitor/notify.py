"""Avisos al usuario: notificación de escritorio y Telegram.

Nada de esto es imprescindible. Si falla, se registra y el lote sigue.
"""

from __future__ import annotations

import html
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from .paths import APP_NAME, logger
from .state import KEY_TELEGRAM_CHAT_ID, KEY_TELEGRAM_TOKEN, SecureStorage

try:  # opcional
    from plyer import notification as _plyer_notification

    HAS_PLYER = True
except Exception:  # pragma: no cover - depende del entorno
    _plyer_notification = None
    HAS_PLYER = False


def credenciales_telegram(chat_id_config: str = "") -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or SecureStorage.get(KEY_TELEGRAM_TOKEN) or ""
    chat_id = (
        os.getenv("TELEGRAM_CHAT_ID")
        or chat_id_config
        or SecureStorage.get(KEY_TELEGRAM_CHAT_ID)
        or ""
    )
    return token.strip(), str(chat_id).strip()


def enviar_telegram(
    titulo: str,
    mensaje: str,
    chat_id_config: str = "",
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    reintentos: int = 3,
) -> bool:
    """Envía un mensaje por Telegram. Nunca lanza ni escribe el token en el log."""
    t, c = credenciales_telegram(chat_id_config)
    token, chat_id = token or t, chat_id or c
    if not token or not chat_id:
        return False
    texto = f"<b>{html.escape(titulo)}</b>\n\n{html.escape(mensaje)}"
    carga = json.dumps({"chat_id": chat_id, "text": texto, "parse_mode": "HTML"}).encode("utf-8")
    peticion = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=carga,
        headers={"Content-Type": "application/json"},
    )
    for intento in range(1, reintentos + 1):
        try:
            with urllib.request.urlopen(peticion, timeout=10) as r:
                datos = json.loads(r.read().decode("utf-8", errors="replace"))
            if not datos.get("ok", False):
                raise RuntimeError(datos.get("description", "Telegram rechazó el mensaje"))
            return True
        except Exception as e:
            # Se registra el TIPO de error, no su texto: el de urllib puede incluir la URL con el token.
            logger.warning("Telegram falló (intento %d/%d): %s", intento, reintentos, type(e).__name__)
            if intento < reintentos:
                time.sleep(2 ** (intento - 1))
    logger.error("Telegram falló definitivamente: %s", titulo)
    return False


class ErrorDeTelegram(Exception):
    """La prueba de Telegram falló; el mensaje explica por qué sin incluir el token."""


def probar_telegram(token: str, chat_id: str, timeout: float = 10.0) -> None:
    """Envía un mensaje de prueba. Lanza `ErrorDeTelegram` con la causa si no llega."""
    token, chat_id = (token or "").strip(), (chat_id or "").strip()
    if not token or not chat_id:
        raise ErrorDeTelegram("Faltan el token del bot y el Chat ID.")
    carga = json.dumps({"chat_id": chat_id, "text": "Telegram configurado correctamente."}).encode("utf-8")
    peticion = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=carga,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as r:
            datos = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        # Telegram explica el motivo en el cuerpo (token mal escrito, chat inexistente…).
        detalle = ""
        try:
            detalle = json.loads(e.read().decode("utf-8", errors="replace")).get("description", "")
        except Exception:
            pass
        raise ErrorDeTelegram(detalle or f"Telegram respondió con el error {e.code}.") from None
    except Exception as e:
        # No se incluye `str(e)`: el de urllib puede llevar la URL y, con ella, el token.
        raise ErrorDeTelegram(f"No se pudo conectar con Telegram ({type(e).__name__}).") from None
    if not datos.get("ok", False):
        raise ErrorDeTelegram(str(datos.get("description") or "Telegram rechazó el mensaje."))


def notificar_escritorio(titulo: str, mensaje: str) -> bool:
    if not HAS_PLYER:
        return False
    try:
        _plyer_notification.notify(title=titulo, message=mensaje[:250], app_name=APP_NAME, timeout=8)
        return True
    except Exception:
        return False


def notificar(titulo: str, mensaje: str, chat_id_config: str = "") -> None:
    """Escritorio + Telegram (este en un hilo aparte para no frenar el lote)."""
    notificar_escritorio(titulo, mensaje)
    threading.Thread(
        target=enviar_telegram, args=(titulo, mensaje, chat_id_config), daemon=True
    ).start()
