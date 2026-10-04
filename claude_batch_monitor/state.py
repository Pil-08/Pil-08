"""Estado persistente (gasto acumulado, lote en curso) y almacén seguro de secretos."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from . import paths
from .config import a_decimal
from .paths import logger, write_atomic

try:  # opcional: sin keyring la clave solo puede venir del entorno
    import keyring

    HAS_KEYRING = True
except ImportError:  # pragma: no cover - depende del entorno
    keyring = None  # type: ignore[assignment]
    HAS_KEYRING = False

SERVICE_NAME = "claude_monitor"
KEY_API = "api_key"
KEY_TELEGRAM_TOKEN = "telegram_token"
KEY_TELEGRAM_CHAT_ID = "telegram_chat_id"


@dataclass
class EstadoMonitor:
    exitos: int = 0
    ultimo_exito: str = ""
    current_job_id: Optional[str] = None
    gasto_acumulado_usd: Decimal = Decimal(0)
    tokens_input_total: int = 0
    tokens_output_total: int = 0

    def guardar(self, path: Optional[Path] = None) -> None:
        path = path or paths.STATE_FILE
        datos = {
            "exitos": self.exitos,
            "ultimo_exito": self.ultimo_exito,
            "current_job_id": self.current_job_id,
            "gasto_acumulado_usd": str(self.gasto_acumulado_usd),
            "tokens_input_total": self.tokens_input_total,
            "tokens_output_total": self.tokens_output_total,
        }
        try:
            write_atomic(path, json.dumps(datos, indent=2, ensure_ascii=False))
        except Exception:
            logger.exception("No se pudo guardar el estado")

    @classmethod
    def cargar(cls, path: Optional[Path] = None) -> EstadoMonitor:
        path = path or paths.STATE_FILE
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            exitos = data.get("exitos", 0)
            if not isinstance(exitos, int) or exitos < 0:
                exitos = 0
            job_id = data.get("current_job_id")
            return cls(
                exitos=exitos,
                ultimo_exito=str(data.get("ultimo_exito", "")),
                current_job_id=str(job_id) if job_id else None,
                gasto_acumulado_usd=max(Decimal(0), a_decimal(data.get("gasto_acumulado_usd"), Decimal(0))),
                tokens_input_total=max(0, int(data.get("tokens_input_total", 0))),
                tokens_output_total=max(0, int(data.get("tokens_output_total", 0))),
            )
        except Exception:
            logger.exception("state.json ilegible; se aparta como .corrupt.json")
            try:
                path.replace(path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return cls()


class SecureStorage:
    """Secretos en el llavero del sistema operativo (nunca en config.json)."""

    @staticmethod
    def disponible() -> bool:
        return HAS_KEYRING

    @staticmethod
    def get(key: str) -> Optional[str]:
        if not HAS_KEYRING:
            return None
        try:
            return keyring.get_password(SERVICE_NAME, key)
        except Exception:
            return None

    @staticmethod
    def set(key: str, value: str) -> bool:
        if not HAS_KEYRING or not value:
            return False
        try:
            keyring.set_password(SERVICE_NAME, key, value)
            return True
        except Exception:
            return False

    @staticmethod
    def delete(key: str) -> bool:
        if not HAS_KEYRING:
            return False
        try:
            keyring.delete_password(SERVICE_NAME, key)
            return True
        except Exception:
            return False


def obtener_api_key() -> Optional[str]:
    """Orden de prioridad: variable de entorno, luego llavero."""
    return (os.getenv("ANTHROPIC_API_KEY") or "").strip() or SecureStorage.get(KEY_API)


def origen_api_key() -> str:
    """'entorno' | 'llavero' | '' — para mostrar al usuario de dónde sale su clave."""
    if (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        return "entorno"
    if SecureStorage.get(KEY_API):
        return "llavero"
    return ""
