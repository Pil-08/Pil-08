import argparse
import html
import json
import logging
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta, time as dt_time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any, Tuple

import tkinter as tk
from tkinter import messagebox, ttk, filedialog
from tkinter.scrolledtext import ScrolledText

try:
    import anthropic
except ImportError:
    print("Falta la librería 'anthropic'. Instálala con: pip install anthropic")
    sys.exit(1)

try:
    from plyer import notification
    HAS_PLYER = True
except ImportError:
    HAS_PLYER = False

try:
    import keyring
    HAS_KEYRING = True
except ImportError:
    HAS_KEYRING = False

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_PYSTRAY = True
except ImportError:
    HAS_PYSTRAY = False

# ============================================================
# UTILIDADES DEL SISTEMA (ANTI-SUSPENSIÓN MULTIPLATAFORMA)
# ============================================================

class KeepAwake:
    """Evita que el sistema operativo entre en suspensión durante procesos largos."""
    _process = None

    @staticmethod
    def enable() -> None:
        if platform.system() == "Windows":
            try:
                import ctypes
                # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000002 | 0x00000040)
            except Exception as e:
                logging.getLogger(APP_NAME).warning(f"No se pudo bloquear la suspensión de Windows: {e}")
        elif platform.system() == "Darwin":  # macOS
            try:
                KeepAwake._process = subprocess.Popen(["caffeinate", "-d", "-w", str(os.getpid())])
            except Exception as e:
                logging.getLogger(APP_NAME).warning(f"No se pudo ejecutar caffeinate en Mac: {e}")
        # Linux carece de un estándar único; se omite deliberadamente.

    @staticmethod
    def disable() -> None:
        if platform.system() == "Windows":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # ES_CONTINUOUS
            except Exception:
                pass
        elif platform.system() == "Darwin" and KeepAwake._process:
            try:
                KeepAwake._process.terminate()
                KeepAwake._process = None
            except Exception:
                pass

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

APP_NAME = "ClaudeMonitor"
APP_VERSION = "11.0.0"

# El botón "Cargar modelos" consulta la API en vivo; este default solo es el arranque.
# Haiku es el más barato -> el default sensato para procesar lotes.
DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# Solo los ERRORES DE SERVIDOR/RED cuentan hacia este máximo. Los 429 (rate limit) NO.
MAX_ERROR_ATTEMPTS = 8
DELAY_BETWEEN_MESSAGES_SEC = 3
DEFAULT_CUTOFF_TIME = "23:59"
DELAY_BETWEEN_CYCLES_SEC = 10
DEFAULT_API_TIMEOUT_SEC = 120  # subido desde 35: respuestas largas de Opus pueden tardar más.
DEFAULT_SEPARATOR = "---"

# Precios de REFERENCIA por millón de tokens (input, output). CAMBIAN con el tiempo y por modelo.
# Solo se usan para autocompletar los campos editables de la UI; el usuario manda.
PRICE_REFERENCE: Dict[str, Tuple[float, float]] = {
    "haiku": (1.0, 5.0),
    "sonnet": (3.0, 15.0),
    "opus": (5.0, 25.0),
}
DEFAULT_PRICE_IN, DEFAULT_PRICE_OUT = PRICE_REFERENCE["haiku"]

SERVICE_NAME = "claude_monitor"
KEY_API = "api_key"
KEY_TELEGRAM_TOKEN = "telegram_token"
KEY_TELEGRAM_CHAT_ID = "telegram_chat_id"


def get_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    (path / "responses").mkdir(exist_ok=True)
    return path


DATA_DIR = get_data_dir()
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "claude_monitor.log"

# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
    logger.addHandler(handler)

# ============================================================
# UTILIDAD: PARSEO DE MENSAJES
# ============================================================

def split_messages(text: str, separator: str) -> List[str]:
    """Parte el texto en bloques por una LÍNEA que sea exactamente el separador.

    Predecible y sin colisión accidental: '----' (4 guiones) no parte si el sep es '---'.
    Esto arregla el bug original re.split(r'-{3,}') que rompía frontmatter/reglas de Markdown.
    """
    sep = separator.strip() or DEFAULT_SEPARATOR
    pattern = rf'(?m)^[ \t]*{re.escape(sep)}[ \t]*$'
    return [m.strip() for m in re.split(pattern, text) if m.strip()]

# ============================================================
# MODELOS DE DATOS
# ============================================================

@dataclass
class Configuracion:
    mensaje: str = ""
    modelo: str = DEFAULT_MODEL
    separador: str = DEFAULT_SEPARATOR
    telegram_chat_id: str = ""
    modo_continuo: bool = False  # CAMBIADO a False: evita bucles infinitos accidentales que queman saldo.
    hora_limite: str = DEFAULT_CUTOFF_TIME
    pausa_ciclos_sec: int = DELAY_BETWEEN_CYCLES_SEC
    pausa_entre_mensajes_sec: int = DELAY_BETWEEN_MESSAGES_SEC
    auto_arranque: bool = False
    modo_fantasma: bool = False
    timeout_api_sec: int = DEFAULT_API_TIMEOUT_SEC
    max_error_attempts: int = MAX_ERROR_ATTEMPTS
    # --- Blindaje de gasto ---
    tope_gasto_usd: float = 5.0        # 0 = sin tope.
    precio_input_mtok: float = DEFAULT_PRICE_IN
    precio_output_mtok: float = DEFAULT_PRICE_OUT
    reset_gasto_por_lote: bool = True  # True: cada lote nuevo empieza el contador a 0.


@dataclass
class EstadoMonitor:
    exitos: int = 0
    ultimo_exito: str = ""
    current_job_id: Optional[str] = None
    gasto_acumulado_usd: float = 0.0
    tokens_input_total: int = 0
    tokens_output_total: int = 0

    def guardar(self) -> None:
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except Exception:
            logger.exception("No se pudo guardar el estado")

    @classmethod
    def cargar(cls) -> "EstadoMonitor":
        if not STATE_FILE.exists():
            return cls()
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            exitos = data.get("exitos", 0)
            if not isinstance(exitos, int) or exitos < 0:
                exitos = 0
            return cls(
                exitos=exitos,
                ultimo_exito=str(data.get("ultimo_exito", "")),
                current_job_id=data.get("current_job_id"),
                gasto_acumulado_usd=float(data.get("gasto_acumulado_usd", 0.0)),
                tokens_input_total=int(data.get("tokens_input_total", 0)),
                tokens_output_total=int(data.get("tokens_output_total", 0)),
            )
        except Exception:
            try:
                STATE_FILE.replace(STATE_FILE.with_suffix(".corrupt.json"))
            except Exception:
                pass
            return cls()


class SecureStorage:
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


class ConfigManager:
    @staticmethod
    def cargar() -> Configuracion:
        if not CONFIG_FILE.exists():
            return Configuracion()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return Configuracion(
                mensaje=str(data.get("mensaje", "")),
                modelo=str(data.get("modelo", DEFAULT_MODEL)),
                separador=str(data.get("separador", DEFAULT_SEPARATOR)),
                telegram_chat_id=str(data.get("telegram_chat_id", "")),
                modo_continuo=bool(data.get("modo_continuo", False)),
                hora_limite=str(data.get("hora_limite", DEFAULT_CUTOFF_TIME)),
                pausa_ciclos_sec=max(1, min(3600, int(data.get("pausa_ciclos_sec", DELAY_BETWEEN_CYCLES_SEC)))),
                pausa_entre_mensajes_sec=max(0, min(60, int(data.get("pausa_entre_mensajes_sec", DELAY_BETWEEN_MESSAGES_SEC)))),
                auto_arranque=bool(data.get("auto_arranque", False)),
                modo_fantasma=bool(data.get("modo_fantasma", False)),
                timeout_api_sec=max(15, min(600, int(data.get("timeout_api_sec", DEFAULT_API_TIMEOUT_SEC)))),
                max_error_attempts=max(1, min(100, int(data.get("max_error_attempts", MAX_ERROR_ATTEMPTS)))),
                tope_gasto_usd=max(0.0, float(data.get("tope_gasto_usd", 5.0))),
                precio_input_mtok=max(0.0, float(data.get("precio_input_mtok", DEFAULT_PRICE_IN))),
                precio_output_mtok=max(0.0, float(data.get("precio_output_mtok", DEFAULT_PRICE_OUT))),
                reset_gasto_por_lote=bool(data.get("reset_gasto_por_lote", True)),
            )
        except Exception:
            try:
                CONFIG_FILE.replace(CONFIG_FILE.with_suffix(".corrupt.json"))
            except Exception:
                pass
            return Configuracion()

    @staticmethod
    def guardar(config: Configuracion) -> None:
        try:
            tmp = CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CONFIG_FILE)
        except Exception:
            logger.exception("No se pudo guardar la configuración")

# ============================================================
# ARQUITECTURA DE TRABAJO (JOB)
# ============================================================

@dataclass
class JobMessage:
    id: int
    text: str
    status: str = "pending"          # pending | completed | failed
    errores: int = 0                 # SOLO errores reales (no 429)
    truncated: bool = False


class Job:
    def __init__(self, job_id: str, created_at: str, modelo: str, mensajes: List[str],
                 separador: str = DEFAULT_SEPARATOR):
        self.job_id = job_id
        self.created_at = created_at
        self.modelo = modelo
        self.separador = separador
        self.ciclos_completados = 0
        self.messages: List[JobMessage] = [JobMessage(id=i + 1, text=m) for i, m in enumerate(mensajes)]

    @property
    def file_path(self) -> Path:
        return DATA_DIR / "responses" / f"job_{self.job_id}.json"

    def to_dict(self) -> Dict[str, Any]:
        """Serialización EXPLÍCITA. Evita la fragilidad del self.__dict__ original."""
        return {
            "job_id": self.job_id,
            "created_at": self.created_at,
            "modelo": self.modelo,
            "separador": self.separador,
            "ciclos_completados": self.ciclos_completados,
            "messages": [asdict(m) for m in self.messages],
        }

    def save(self) -> None:
        try:
            tmp = self.file_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.file_path)
        except Exception as e:
            logger.error(f"No se pudo guardar el Job {self.job_id}: {e}")

    @classmethod
    def load(cls, job_id: str) -> Optional["Job"]:
        path = DATA_DIR / "responses" / f"job_{job_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            job = cls(data["job_id"], data["created_at"], data["modelo"], [],
                      separador=data.get("separador", DEFAULT_SEPARATOR))
            job.ciclos_completados = data.get("ciclos_completados", 0)
            job.messages = [
                JobMessage(
                    id=m["id"], text=m["text"], status=m.get("status", "pending"),
                    errores=m.get("errores", m.get("attempts", 0)), truncated=m.get("truncated", False),
                )
                for m in data["messages"]
            ]
            return job
        except Exception:
            logger.exception(f"No se pudo cargar el Job {job_id}")
            return None

    def completed_count(self) -> int:
        return sum(1 for m in self.messages if m.status == "completed")

    def total_count(self) -> int:
        return len(self.messages)

    def reset_for_new_cycle(self) -> None:
        for m in self.messages:
            m.status = "pending"
            m.errores = 0
        self.ciclos_completados += 1
        self.save()

# ============================================================
# CORE DEL MONITOR (PROCESADOR DE TRABAJOS)
# ============================================================

class MonitorCore:
    def __init__(self, config: Configuracion, job: Job):
        self.config = config
        self.job = job
        self.estado = EstadoMonitor.cargar()
        self.estado.current_job_id = job.job_id
        if config.reset_gasto_por_lote:
            self.estado.gasto_acumulado_usd = 0.0
        self.estado.guardar()

        self.evento_detener = threading.Event()
        self._client: Optional[anthropic.Anthropic] = None
        self.deadline: Optional[datetime] = None

    # ---------- credenciales ----------

    def _get_api_key(self) -> Optional[str]:
        return os.getenv("ANTHROPIC_API_KEY") or SecureStorage.get(KEY_API)

    def _get_telegram_credentials(self) -> Tuple[str, str]:
        token = os.getenv("TELEGRAM_BOT_TOKEN") or SecureStorage.get(KEY_TELEGRAM_TOKEN) or ""
        chat_id = os.getenv("TELEGRAM_CHAT_ID") or self.config.telegram_chat_id or SecureStorage.get(KEY_TELEGRAM_CHAT_ID) or ""
        return token, chat_id

    def inicializar_cliente(self) -> None:
        api_key = self._get_api_key()
        if not api_key:
            raise ValueError("No se encontró una API Key de Anthropic.")
        self._client = anthropic.Anthropic(api_key=api_key, timeout=float(self.config.timeout_api_sec), max_retries=0)

    # ---------- tiempos ----------

    def calcular_deadline(self) -> datetime:
        try:
            hora = datetime.strptime(self.config.hora_limite.strip(), "%H:%M").time()
        except ValueError:
            hora = dt_time(23, 59)
        ahora = datetime.now()
        deadline = datetime.combine(ahora.date(), hora)
        if deadline <= ahora:
            deadline += timedelta(days=1)
        return deadline

    def _espera_rate_limit(self, error) -> float:
        """Espera para 429: usa el header de reset EXACTO de Anthropic; sin backoff exponencial."""
        headers = dict(getattr(getattr(error, "response", None), "headers", None) or {})
        reset = (headers.get("anthropic-ratelimit-tokens-reset")
                 or headers.get("anthropic-ratelimit-requests-reset"))
        if reset:
            seconds = self._parse_reset(reset)
            if seconds and seconds > 0:
                return seconds + 2
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after) + 2
            except (TypeError, ValueError):
                pass
        match = re.search(r"try again in\s+([\d.]+)\s*s", str(error), re.IGNORECASE)
        if match:
            return float(match.group(1)) + 2
        return 60.0  # fallback sensato, NO una hora.

    def _espera_error_servidor(self, error, intentos: int) -> float:
        """Espera para 5xx: backoff exponencial con jitter (no sabemos cuándo se recupera)."""
        headers = dict(getattr(getattr(error, "response", None), "headers", None) or {})
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after) + 5
            except (TypeError, ValueError):
                pass
        exponential = min(30 * (2 ** min(max(intentos - 1, 0), 6)), 1800)
        return exponential + random.uniform(2, 10)

    @staticmethod
    def _parse_reset(value: str) -> Optional[float]:
        value = str(value).strip()
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        if len(value) >= 2 and value[-1].lower() in units:
            try:
                return float(value[:-1]) * units[value[-1].lower()]
            except ValueError:
                return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError):
            return None

    # ---------- notificaciones ----------

    def enviar_telegram(self, titulo: str, mensaje: str) -> bool:
        token, chat_id = self._get_telegram_credentials()
        if not token or not chat_id:
            return False
        text = f"<b>{html.escape(titulo)}</b>\n\n{html.escape(mensaje)}"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(request, timeout=10) as r:
                    data = json.loads(r.read().decode("utf-8", errors="replace"))
                    if not data.get("ok", False):
                        raise RuntimeError(data.get("description", "Telegram rechazó el mensaje"))
                    return True
            except Exception:
                if attempt < 3:
                    time.sleep(2 ** (attempt - 1))
        logger.error(f"Telegram falló definitivamente: {titulo}")
        return False

    def notificar(self, titulo: str, mensaje: str) -> None:
        if HAS_PLYER:
            try:
                notification.notify(title=titulo, message=mensaje[:250], app_name=APP_NAME, timeout=8)
            except Exception:
                pass
        threading.Thread(target=self.enviar_telegram, args=(titulo, mensaje), daemon=True).start()

    # ---------- espera cancelable ----------

    def _esperar(self, seconds: float, on_tick: Optional[Callable[[float, float], None]]) -> bool:
        end = time.monotonic() + seconds
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return True
            if self.deadline and datetime.now() >= self.deadline:
                return False
            if self.evento_detener.wait(min(0.5, remaining)):
                return False
            if on_tick:
                on_tick(seconds, remaining)

    # ---------- contabilidad de gasto ----------

    def _contabilizar_coste(self, response,
                            on_gasto: Optional[Callable[[float, int, int], None]],
                            on_error: Optional[Callable[[str], None]]) -> bool:
        """Suma el coste REAL de la respuesta ya recibida. Devuelve False si el tope obliga a parar."""
        usage = getattr(response, "usage", None)
        if not usage:
            return True
        tin = int(getattr(usage, "input_tokens", 0) or 0)
        tout = int(getattr(usage, "output_tokens", 0) or 0)
        coste = (tin / 1_000_000) * self.config.precio_input_mtok + (tout / 1_000_000) * self.config.precio_output_mtok

        self.estado.gasto_acumulado_usd += coste
        self.estado.tokens_input_total += tin
        self.estado.tokens_output_total += tout
        self.estado.guardar()

        if on_gasto:
            on_gasto(self.estado.gasto_acumulado_usd, tin, tout)

        if self.config.tope_gasto_usd > 0 and self.estado.gasto_acumulado_usd >= self.config.tope_gasto_usd:
            err = (f"🛑 TOPE DE GASTO alcanzado: ${self.estado.gasto_acumulado_usd:.4f} / "
                   f"${self.config.tope_gasto_usd:.2f}. Lote detenido para proteger tu saldo.")
            logger.critical(err)
            self.notificar("🛑 Tope de gasto", err)
            if on_error:
                on_error(err)
            return False
        return True

    # ---------- bucle principal ----------

    def ejecutar(
        self,
        on_estado: Optional[Callable[[str, bool], None]] = None,
        on_exito_mensaje: Optional[Callable[[str, int, int], None]] = None,
        on_fin_ciclo: Optional[Callable[[int, int, int], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_tick: Optional[Callable[[float, float], None]] = None,
        on_gasto: Optional[Callable[[float, int, int], None]] = None,
    ) -> None:

        try:
            self.inicializar_cliente()
        except Exception as error:
            if on_error:
                on_error(str(error))
            return

        self.deadline = self.calcular_deadline()
        logger.info(f"Monitor iniciado. Hora límite: {self.deadline}. Tope: ${self.config.tope_gasto_usd:.2f}")

        while not self.evento_detener.is_set() and datetime.now() < self.deadline:
            ciclo = self.job.ciclos_completados + 1
            total = self.job.total_count()

            for msg in self.job.messages:
                if self.evento_detener.is_set() or datetime.now() >= self.deadline:
                    break
                if msg.status == "completed":
                    continue

                if not self._procesar_mensaje(msg, ciclo, total, on_estado, on_exito_mensaje,
                                              on_error, on_tick, on_gasto):
                    return  # parada dura (tope, 402, error fatal, red)

                # Pausa de cortesía entre mensajes distintos.
                if msg.status == "completed" and msg.id < total and not self.evento_detener.is_set():
                    delay = self.config.pausa_entre_mensajes_sec
                    if delay > 0:
                        if on_estado:
                            on_estado(f"⏸ Pausa de seguridad ({delay}s)...", False)
                        if not self._esperar(delay, on_tick):
                            break

            self._cerrar_ciclo(ciclo, total, on_fin_ciclo)

            if self.evento_detener.is_set() or datetime.now() >= self.deadline:
                break
            if not self.config.modo_continuo:
                break

            self.job.reset_for_new_cycle()
            self.notificar("🔄 Ciclo Completado",
                           f"Ciclo {ciclo} finalizado. Iniciando ciclo {ciclo + 1} en {self.config.pausa_ciclos_sec}s.")
            if on_estado:
                on_estado(f"🔄 Ciclo {ciclo} completado. Pausa larga...", False)
            if not self._esperar(self.config.pausa_ciclos_sec, on_tick):
                break

        if self.job.completed_count() == self.job.total_count() and not self.config.modo_continuo:
            self.estado.current_job_id = None
            self.estado.guardar()

    def _procesar_mensaje(self, msg: JobMessage, ciclo: int, total: int,
                          on_estado, on_exito_mensaje, on_error, on_tick, on_gasto) -> bool:
        """Procesa UN mensaje hasta completarlo. Devuelve False si hay que abortar todo el lote.

        Clave del arreglo: los 429 reintentan INDEFINIDAMENTE (esperando el reset exacto) y
        NO incrementan msg.errores. Solo los 5xx/errores reales cuentan hacia max_error_attempts.
        """
        while not self.evento_detener.is_set() and msg.status != "completed":
            if datetime.now() >= self.deadline:
                return True  # se acabó el tiempo; no es un fallo del lote.

            if on_estado:
                on_estado(f"[Ciclo {ciclo} | Msj {msg.id}/{total}] Enviando (fallos: {msg.errores})", True)

            try:
                response = self._client.messages.create(
                    model=self.config.modelo,
                    max_tokens=4096,
                    messages=[{"role": "user", "content": msg.text}],
                )

                if not self._contabilizar_coste(response, on_gasto, on_error):
                    return False  # tope alcanzado -> parada limpia.

                text_blocks = [b.text for b in response.content if hasattr(b, "text")]
                text = "\n".join(text_blocks).strip() or "(Claude devolvió una respuesta vacía.)"
                if getattr(response, "stop_reason", None) == "max_tokens":
                    text += "\n\n[⚠️ ADVERTENCIA: Respuesta truncada por límite de tokens]"
                    msg.truncated = True

                resp_file = DATA_DIR / "responses" / f"job_{self.job.job_id}_c{ciclo}_m{msg.id:03d}.txt"
                resp_file.write_text(
                    f"====== SOLICITUD ======\n{msg.text}\n\n====== RESPUESTA ======\n{text}\n",
                    encoding="utf-8",
                )

                self.estado.exitos += 1
                self.estado.ultimo_exito = datetime.now(timezone.utc).isoformat()
                self.estado.guardar()

                msg.status = "completed"
                self.job.save()
                if on_exito_mensaje:
                    on_exito_mensaje(text, msg.id, total)
                return True

            except anthropic.RateLimitError as error:
                # 429: NO cuenta como fallo. Esperar el turno y reintentar.
                seconds = self._espera_rate_limit(error)
                if on_estado:
                    on_estado(f"⏳ [Msj {msg.id}] Rate limit. Reintento en {int(seconds)}s (no cuenta como fallo)", False)
                if not self._esperar(seconds, on_tick):
                    return True  # detenido o deadline durante la espera.

            except anthropic.APIStatusError as error:
                status = getattr(error, "status_code", 0)
                if status == 402:
                    err = "🛑 SIN SALDO (402). Los créditos de la API no se recargan solos. Lote detenido."
                    logger.critical(err)
                    self.notificar("🛑 Saldo Agotado", err)
                    if on_error:
                        on_error(err)
                    return False
                if status in (400, 401, 403, 404):
                    if on_error:
                        on_error(f"Error fatal HTTP {status}: {error}")
                    return False
                # 5xx u otros: error real, cuenta hacia el máximo.
                msg.errores += 1
                self.job.save()
                if msg.errores >= self.config.max_error_attempts:
                    err = f"Máximo de errores reales en mensaje {msg.id} ({msg.errores}). Lote pausado."
                    self.notificar("⛔ Lote Pausado", err)
                    if on_error:
                        on_error(err)
                    return False
                seconds = self._espera_error_servidor(error, msg.errores)
                if on_estado:
                    on_estado(f"⚠️ [Msj {msg.id}] Error servidor {status}. Reintento en {int(seconds)}s", False)
                if not self._esperar(seconds, on_tick):
                    return True

            except anthropic.APIConnectionError:
                # No sabemos si el servidor llegó a procesar -> parar para no facturar duplicado.
                if on_estado:
                    on_estado("⚠️ ERROR DE RED. Lote pausado (evita duplicados).", False)
                if on_error:
                    on_error("Fallo de red. Lote pausado para evitar duplicados.")
                return False

            except Exception as error:
                logger.exception("Error inesperado")
                if on_error:
                    on_error(f"Error inesperado: {error}")
                return False

        return True

    def _cerrar_ciclo(self, ciclo: int, total: int, on_fin_ciclo) -> None:
        completados = self.job.completed_count()
        summary_file = DATA_DIR / "responses" / f"job_{self.job.job_id}_ciclo_{ciclo}_resumen.md"
        try:
            with open(summary_file, "w", encoding="utf-8") as f:
                f.write(f"# Resumen del Ciclo {ciclo}\n")
                f.write(f"Fecha: {datetime.now().isoformat()}\n")
                f.write(f"Completados: {completados}/{total}\n")
                f.write(f"Gasto acumulado: ${self.estado.gasto_acumulado_usd:.4f}\n\n---\n\n")
                for m in self.job.messages:
                    if m.status == "completed":
                        p = DATA_DIR / "responses" / f"job_{self.job.job_id}_c{ciclo}_m{m.id:03d}.txt"
                        if p.exists():
                            f.write(f"## Mensaje {m.id}\n")
                            f.write(p.read_text(encoding="utf-8"))
                            f.write("\n\n---\n\n")
        except Exception as e:
            logger.error(f"Error creando resumen: {e}")
        if on_fin_ciclo:
            on_fin_ciclo(completados, total, ciclo)

    def detener(self) -> None:
        self.evento_detener.set()

# ============================================================
# GUI
# ============================================================

class MonitorGUI:
    UI_LOG_MAX_LINES = 300

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Claude API Batch Processor v{APP_VERSION}")
        self.root.geometry("760x880")
        self.root.minsize(680, 720)          # redimensionable con mínimo razonable.
        self.root.resizable(True, True)

        style = ttk.Style()
        if 'clam' in style.theme_names():
            style.theme_use('clam')

        self.core: Optional[MonitorCore] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.tray_icon = None
        self._save_after_id: Optional[str] = None  # para el debounce de guardado.

        self.var_estado = tk.StringVar(value="Estado: En espera")
        self.var_intentos = tk.StringVar(value="Progreso de cola: 0/0")
        self.var_prox = tk.StringVar(value="Próximo intento: --:--")
        self.var_error = tk.StringVar(value="Último error: Ninguno")
        self.var_exitos = tk.StringVar(value="Éxitos históricos: 0")
        self.var_gasto = tk.StringVar(value="Gasto: $0.0000 / sin tope")
        self.var_msg_count = tk.StringVar(value="0 mensajes detectados")
        self.var_auth = tk.StringVar()

        self.var_modo_continuo = tk.BooleanVar(value=False)
        self.var_auto_arranque = tk.BooleanVar(value=False)
        self.var_modo_fantasma = tk.BooleanVar(value=False)
        self.var_reset_gasto = tk.BooleanVar(value=True)

        self.api_key_override = False

        self._build_ui()
        self._load_config()
        self._bind_instant_save()
        self._recontar_mensajes()

        self.root.after(500, self._check_startup_logic)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- construcción de UI ----------

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        tab_principal = ttk.Frame(notebook)
        tab_ajustes = ttk.Frame(notebook)
        notebook.add(tab_principal, text="🚀 Panel Principal")
        notebook.add(tab_ajustes, text="⚙️ Ajustes")

        self._build_tab_principal(tab_principal)
        self._build_tab_ajustes(tab_ajustes)

    def _build_tab_ajustes(self, parent: ttk.Frame) -> None:
        auth = ttk.LabelFrame(parent, text=" 🔒 Autenticación ")
        auth.pack(fill="x", padx=15, pady=10)

        ttk.Label(auth, text="API Key de Anthropic:").pack(anchor="w", padx=15, pady=(10, 2))
        key_row = ttk.Frame(auth)
        key_row.pack(fill="x", padx=15, pady=2)
        self.entry_key = ttk.Entry(key_row, show="*")
        self.entry_key.pack(side="left", fill="x", expand=True)
        self.btn_key = ttk.Button(key_row, text="✏️ Editar", command=self._enable_api_key_input)
        self.btn_key.pack(side="left", padx=(8, 0))
        ttk.Label(auth, textvariable=self.var_auth).pack(anchor="w", padx=15, pady=(2, 10))

        model_row = ttk.Frame(auth)
        model_row.pack(fill="x", padx=15, pady=(0, 10))
        ttk.Label(model_row, text="Modelo:").pack(side="left", padx=(0, 10))
        self.combo_model = ttk.Combobox(model_row, state="readonly", values=[DEFAULT_MODEL])
        self.combo_model.set(DEFAULT_MODEL)
        self.combo_model.pack(side="left", fill="x", expand=True)
        self.btn_models = ttk.Button(model_row, text="🔄 Cargar", command=self.load_models)
        self.btn_models.pack(side="left", padx=(8, 0))

        # --- Blindaje de gasto ---
        gasto = ttk.LabelFrame(parent, text=" 💸 Blindaje de gasto (protege tu saldo) ")
        gasto.pack(fill="x", padx=15, pady=10)
        grow1 = ttk.Frame(gasto)
        grow1.pack(fill="x", pady=6)
        ttk.Label(grow1, text="Tope de gasto ($, 0=sin tope):").pack(side="left", padx=(12, 5))
        self.entry_tope = ttk.Entry(grow1, width=8)
        self.entry_tope.pack(side="left")
        ttk.Checkbutton(grow1, text="Reiniciar contador en cada lote nuevo",
                        variable=self.var_reset_gasto, command=self._save_instant_config).pack(side="left", padx=20)
        grow2 = ttk.Frame(gasto)
        grow2.pack(fill="x", pady=6)
        ttk.Label(grow2, text="Precio IN ($/Mtok):").pack(side="left", padx=(12, 5))
        self.entry_price_in = ttk.Entry(grow2, width=7)
        self.entry_price_in.pack(side="left")
        ttk.Label(grow2, text="Precio OUT ($/Mtok):").pack(side="left", padx=(20, 5))
        self.entry_price_out = ttk.Entry(grow2, width=7)
        self.entry_price_out.pack(side="left")
        ttk.Label(gasto, text="⚠️ Los precios cambian y varían por modelo. Ajústalos a TU modelo real.",
                  foreground="#a05a00").pack(anchor="w", padx=15, pady=(2, 8))

        telegram = ttk.LabelFrame(parent, text=" 📱 Telegram (opcional) ")
        telegram.pack(fill="x", padx=15, pady=10)
        ttk.Label(telegram, text="Bot Token:").grid(row=0, column=0, sticky="w", padx=15, pady=8)
        self.entry_tg_token = ttk.Entry(telegram, show="*")
        self.entry_tg_token.grid(row=0, column=1, sticky="ew", padx=15)
        ttk.Label(telegram, text="Chat ID:").grid(row=1, column=0, sticky="w", padx=15, pady=8)
        self.entry_chat = ttk.Entry(telegram)
        self.entry_chat.grid(row=1, column=1, sticky="ew", padx=15)
        telegram.columnconfigure(1, weight=1)
        self.btn_test_tg = ttk.Button(telegram, text="✈️ Probar Telegram", command=self.test_telegram)
        self.btn_test_tg.grid(row=2, column=0, columnspan=2, pady=10)

        automa = ttk.LabelFrame(parent, text=" ⚡ Automatización ")
        automa.pack(fill="x", padx=15, pady=10)
        ttk.Checkbutton(automa, text="Autoiniciar la secuencia al abrir el programa",
                        variable=self.var_auto_arranque, command=self._save_instant_config).pack(anchor="w", padx=15, pady=5)
        ttk.Checkbutton(automa, text="Iniciar minimizado en la bandeja del sistema",
                        variable=self.var_modo_fantasma, command=self._save_instant_config).pack(anchor="w", padx=15, pady=5)

        settings = ttk.LabelFrame(parent, text=" ⏱ Tiempos y Límites ")
        settings.pack(fill="x", padx=15, pady=10)
        row1 = ttk.Frame(settings)
        row1.pack(fill="x", pady=5)
        ttk.Label(row1, text="Hora límite (HH:MM):").pack(side="left", padx=(10, 5))
        self.entry_cutoff = ttk.Entry(row1, width=8)
        self.entry_cutoff.pack(side="left")
        ttk.Label(row1, text="Pausa entre ciclos (s):").pack(side="left", padx=(20, 5))
        self.spin_cycle = ttk.Spinbox(row1, from_=1, to=3600, width=6)
        self.spin_cycle.pack(side="left")
        row2 = ttk.Frame(settings)
        row2.pack(fill="x", pady=5)
        ttk.Label(row2, text="Máx. errores por msg:").pack(side="left", padx=(10, 5))
        self.spin_errors = ttk.Spinbox(row2, from_=1, to=100, width=6)
        self.spin_errors.pack(side="left")
        ttk.Label(row2, text="Pausa entre msgs (s):").pack(side="left", padx=(20, 5))
        self.spin_delay_msgs = ttk.Spinbox(row2, from_=0, to=60, width=6)
        self.spin_delay_msgs.pack(side="left")
        row3 = ttk.Frame(settings)
        row3.pack(fill="x", pady=5)
        ttk.Label(row3, text="Timeout API (s):").pack(side="left", padx=(10, 5))
        self.spin_timeout = ttk.Spinbox(row3, from_=15, to=600, width=6)
        self.spin_timeout.pack(side="left")
        ttk.Label(row3, text="Separador de bloques:").pack(side="left", padx=(20, 5))
        self.entry_sep = ttk.Entry(row3, width=8)
        self.entry_sep.pack(side="left")
        ttk.Checkbutton(row3, text="Repetir lote en bucle", variable=self.var_modo_continuo,
                        command=self._save_instant_config).pack(side="left", padx=20)

    def _build_tab_principal(self, parent: ttk.Frame) -> None:
        message_frame = ttk.LabelFrame(parent, text=" 📝 Secuencia de Órdenes ")
        message_frame.pack(fill="both", expand=True, padx=15, pady=(10, 6))

        header = ttk.Frame(message_frame)
        header.pack(fill="x", padx=10, pady=(6, 0))
        ttk.Label(header, textvariable=self.var_msg_count, font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Button(header, text="📂 Cargar archivo (.txt / .md)", command=self._cargar_archivo_txt).pack(side="right")

        self.txt_msg = ScrolledText(message_frame, height=9, wrap=tk.WORD, font=("Consolas", 10))
        self.txt_msg.pack(fill="both", expand=True, padx=10, pady=8)

        # Panel de log en vivo (mejora UX: ya no hay que abrir el fichero para ver qué pasa).
        log_frame = ttk.LabelFrame(parent, text=" 🖥 Actividad en vivo ")
        log_frame.pack(fill="both", expand=True, padx=15, pady=6)
        self.txt_log = ScrolledText(log_frame, height=6, wrap=tk.WORD, font=("Consolas", 9), state="disabled",
                                    background="#1e1e1e", foreground="#d4d4d4")
        self.txt_log.pack(fill="both", expand=True, padx=10, pady=8)

        monitor = ttk.LabelFrame(parent, text=" 📊 Monitor ")
        monitor.pack(fill="x", padx=15, pady=6)
        ttk.Label(monitor, textvariable=self.var_estado, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=15, pady=(8, 4))
        self.progress = ttk.Progressbar(monitor, mode="determinate")
        self.progress.pack(fill="x", padx=15, pady=4)
        grid = ttk.Frame(monitor)
        grid.pack(fill="x", padx=15, pady=(2, 8))
        ttk.Label(grid, textvariable=self.var_intentos).grid(row=0, column=0, sticky="w", padx=(0, 20))
        ttk.Label(grid, textvariable=self.var_gasto, foreground="#0a6b3c").grid(row=0, column=1, sticky="w")
        ttk.Label(grid, textvariable=self.var_prox).grid(row=1, column=0, sticky="w", padx=(0, 20))
        ttk.Label(grid, textvariable=self.var_exitos).grid(row=1, column=1, sticky="w")
        ttk.Label(grid, textvariable=self.var_error, foreground="#8b1a1a").grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        buttons = ttk.Frame(parent)
        buttons.pack(pady=12)
        self.btn_start = ttk.Button(buttons, text="▶ Iniciar Lote", command=self.start_new_job)
        self.btn_start.pack(side="left", padx=5)
        self.btn_stop = ttk.Button(buttons, text="⏹ Detener", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=5)
        ttk.Button(buttons, text="📂 Respuestas", command=self.open_responses).pack(side="left", padx=15)
        ttk.Button(buttons, text="📄 Logs", command=self.open_logs).pack(side="left", padx=5)

    # ---------- log en vivo ----------

    def _log_ui(self, msg: str) -> None:
        try:
            self.txt_log.config(state="normal")
            self.txt_log.insert(tk.END, f"[{datetime.now():%H:%M:%S}] {msg}\n")
            # Recorte para que el widget no crezca sin límite.
            lines = int(self.txt_log.index('end-1c').split('.')[0])
            if lines > self.UI_LOG_MAX_LINES:
                self.txt_log.delete("1.0", f"{lines - self.UI_LOG_MAX_LINES}.0")
            self.txt_log.see(tk.END)
            self.txt_log.config(state="disabled")
        except Exception:
            pass

    # ---------- guardado con debounce ----------

    def _bind_instant_save(self) -> None:
        self.txt_msg.bind("<KeyRelease>", self._on_text_change)
        for w in (self.entry_cutoff, self.spin_cycle, self.spin_errors, self.spin_delay_msgs,
                  self.spin_timeout, self.entry_chat, self.entry_tope, self.entry_price_in,
                  self.entry_price_out, self.entry_sep):
            w.bind("<FocusOut>", self._save_instant_config)
        self.combo_model.bind("<<ComboboxSelected>>", self._on_model_selected)

    def _on_text_change(self, *_) -> None:
        self._recontar_mensajes()
        # Debounce: reprograma el guardado 600 ms tras la última tecla en vez de escribir a disco por pulsación.
        if self._save_after_id:
            self.root.after_cancel(self._save_after_id)
        self._save_after_id = self.root.after(600, self._save_instant_config)

    def _recontar_mensajes(self) -> None:
        sep = self.entry_sep.get().strip() or DEFAULT_SEPARATOR
        n = len(split_messages(self.txt_msg.get("1.0", tk.END), sep))
        self.var_msg_count.set(f"{n} mensaje{'s' if n != 1 else ''} detectado{'s' if n != 1 else ''}")

    def _on_model_selected(self, *_) -> None:
        # Autocompleta precios de referencia por familia (editable después).
        name = self.combo_model.get().lower()
        for fam, (pin, pout) in PRICE_REFERENCE.items():
            if fam in name:
                self.entry_price_in.delete(0, tk.END)
                self.entry_price_in.insert(0, str(pin))
                self.entry_price_out.delete(0, tk.END)
                self.entry_price_out.insert(0, str(pout))
                break
        self._save_instant_config()

    def _cargar_archivo_txt(self) -> None:
        filepath = filedialog.askopenfilename(
            filetypes=[("Text files", "*.txt"), ("Markdown files", "*.md"), ("All files", "*.*")])
        if not filepath:
            return
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            self.txt_msg.delete("1.0", tk.END)
            self.txt_msg.insert(tk.END, content)
            self._recontar_mensajes()
            self._save_instant_config()
            messagebox.showinfo("Éxito", "Archivo cargado correctamente.")
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo leer el archivo:\n{e}")

    def _read_float(self, entry: ttk.Entry, fallback: float) -> float:
        try:
            return max(0.0, float(entry.get().strip().replace(",", ".")))
        except (ValueError, AttributeError):
            return fallback

    def _read_int(self, spin: ttk.Spinbox, fallback: int) -> int:
        try:
            return int(spin.get())
        except (ValueError, AttributeError):
            return fallback

    def _save_instant_config(self, *_) -> None:
        self._save_after_id = None
        config = ConfigManager.cargar()
        config.mensaje = self.txt_msg.get("1.0", tk.END).strip()
        config.modelo = self.combo_model.get().strip()
        config.separador = self.entry_sep.get().strip() or DEFAULT_SEPARATOR
        config.hora_limite = self.entry_cutoff.get().strip() or DEFAULT_CUTOFF_TIME
        config.pausa_ciclos_sec = self._read_int(self.spin_cycle, DELAY_BETWEEN_CYCLES_SEC)
        config.max_error_attempts = self._read_int(self.spin_errors, MAX_ERROR_ATTEMPTS)
        config.pausa_entre_mensajes_sec = self._read_int(self.spin_delay_msgs, DELAY_BETWEEN_MESSAGES_SEC)
        config.timeout_api_sec = self._read_int(self.spin_timeout, DEFAULT_API_TIMEOUT_SEC)
        config.telegram_chat_id = self.entry_chat.get().strip()
        config.tope_gasto_usd = self._read_float(self.entry_tope, 5.0)
        config.precio_input_mtok = self._read_float(self.entry_price_in, DEFAULT_PRICE_IN)
        config.precio_output_mtok = self._read_float(self.entry_price_out, DEFAULT_PRICE_OUT)
        config.modo_continuo = self.var_modo_continuo.get()
        config.auto_arranque = self.var_auto_arranque.get()
        config.modo_fantasma = self.var_modo_fantasma.get()
        config.reset_gasto_por_lote = self.var_reset_gasto.get()
        ConfigManager.guardar(config)

    def _load_config(self) -> None:
        config = ConfigManager.cargar()
        self.txt_msg.insert("1.0", config.mensaje)
        self.entry_chat.insert(0, config.telegram_chat_id)
        self.combo_model.set(config.modelo)
        self.entry_cutoff.insert(0, config.hora_limite)
        self.entry_sep.insert(0, config.separador)
        self.spin_cycle.set(config.pausa_ciclos_sec)
        self.spin_errors.set(config.max_error_attempts)
        self.spin_delay_msgs.set(config.pausa_entre_mensajes_sec)
        self.spin_timeout.set(config.timeout_api_sec)
        self.entry_tope.insert(0, f"{config.tope_gasto_usd:g}")
        self.entry_price_in.insert(0, f"{config.precio_input_mtok:g}")
        self.entry_price_out.insert(0, f"{config.precio_output_mtok:g}")

        self.var_modo_continuo.set(config.modo_continuo)
        self.var_auto_arranque.set(config.auto_arranque)
        self.var_modo_fantasma.set(config.modo_fantasma)
        self.var_reset_gasto.set(config.reset_gasto_por_lote)

        state = EstadoMonitor.cargar()
        self.var_exitos.set(f"Éxitos históricos: {state.exitos}")
        self._actualizar_label_gasto(state.gasto_acumulado_usd, config.tope_gasto_usd)

        env_key = os.getenv("ANTHROPIC_API_KEY")
        keyring_key = SecureStorage.get(KEY_API)
        if env_key:
            self.var_auth.set("✓ API Key cargada desde entorno")
            self.entry_key.config(state="disabled")
        elif keyring_key:
            self.var_auth.set("✓ API Key segura en Keyring")
            self.entry_key.config(state="disabled")
        else:
            self.var_auth.set("⚠️ Introduce tu API Key. NO se guardará si no hay Keyring.")

    def _actualizar_label_gasto(self, gasto: float, tope: float) -> None:
        tope_txt = f"${tope:.2f}" if tope > 0 else "sin tope"
        self.var_gasto.set(f"Gasto: ${gasto:.4f} / {tope_txt}")

    # ---------- arranque / reanudación ----------

    def _check_startup_logic(self) -> None:
        estado = EstadoMonitor.cargar()
        resumed = False
        if estado.current_job_id:
            job = Job.load(estado.current_job_id)
            if job and any(m.status != "completed" for m in job.messages):
                resp = messagebox.askyesno(
                    "Lote Incompleto",
                    f"Se encontró un lote sin terminar (ID: {job.job_id}).\n"
                    f"Completados: {job.completed_count()}/{job.total_count()}\n\n"
                    f"¿Deseas reanudarlo ahora?")
                if resp:
                    self._resume_job(job)
                    resumed = True
                else:
                    estado.current_job_id = None
                    estado.guardar()
        if not resumed and self.var_auto_arranque.get() and self.txt_msg.get("1.0", tk.END).strip():
            self.start_new_job()
        if self.var_modo_fantasma.get():
            self._minimize_to_tray()

    def _resume_job(self, job: Job) -> None:
        if not self._get_api_key_from_ui():
            messagebox.showerror("Falta API Key", "Introduce tu API Key para reanudar el lote.")
            return
        config = ConfigManager.cargar()
        config.modelo = job.modelo
        config.separador = job.separador
        config.reset_gasto_por_lote = False  # al reanudar NO reiniciamos el contador de gasto.
        self.combo_model.set(job.modelo)
        self._launch_core(config, job)

    # ---------- API key ----------

    def _enable_api_key_input(self) -> None:
        self.api_key_override = True
        self.entry_key.config(state="normal")
        self.entry_key.delete(0, tk.END)
        self.entry_key.focus_set()
        self.var_auth.set("Modo manual: introduce una API Key nueva")

    def _get_api_key_from_ui(self) -> str:
        value = self.entry_key.get().strip()
        if value:
            return value
        return os.getenv("ANTHROPIC_API_KEY") or SecureStorage.get(KEY_API) or ""

    # ---------- ciclo de vida del job ----------

    def start_new_job(self) -> None:
        self._save_instant_config()
        message = self.txt_msg.get("1.0", tk.END).strip()
        if not message:
            messagebox.showwarning("Mensaje vacío", "Escribe las órdenes que quieres enviar.")
            return
        if not self._get_api_key_from_ui():
            messagebox.showerror("Falta API Key", "No se encontró una API Key de Anthropic.")
            return

        config = ConfigManager.cargar()

        if self.api_key_override and self.entry_key.get().strip() and HAS_KEYRING:
            SecureStorage.set(KEY_API, self.entry_key.get().strip())
        tg_token = self.entry_tg_token.get().strip()
        if tg_token and HAS_KEYRING:
            SecureStorage.set(KEY_TELEGRAM_TOKEN, tg_token)
        if config.telegram_chat_id and HAS_KEYRING:
            SecureStorage.set(KEY_TELEGRAM_CHAT_ID, config.telegram_chat_id)

        mensajes = split_messages(message, config.separador)
        if not mensajes:
            messagebox.showwarning("Sin mensajes", "No se detectaron mensajes válidos.")
            return

        # Confirmación previa: el usuario ve cuántas órdenes y el tope antes de gastar.
        tope_txt = f"${config.tope_gasto_usd:.2f}" if config.tope_gasto_usd > 0 else "SIN TOPE"
        if not messagebox.askyesno(
                "Confirmar lote",
                f"Órdenes detectadas: {len(mensajes)}\n"
                f"Modelo: {config.modelo}\n"
                f"Tope de gasto: {tope_txt}\n"
                f"Bucle continuo: {'Sí' if config.modo_continuo else 'No'}\n\n"
                f"¿Iniciar?"):
            return

        job = Job(uuid.uuid4().hex[:8], datetime.now().isoformat(), config.modelo, mensajes, config.separador)
        job.save()
        self._launch_core(config, job)

    def _launch_core(self, config: Configuracion, job: Job) -> None:
        self.core = MonitorCore(config, job)
        self._reset_ui_for_start()
        self._toggle_ui(False)
        self._log_ui(f"Lote {job.job_id} iniciado: {job.total_count()} órdenes, modelo {config.modelo}.")
        KeepAwake.enable()
        self.worker_thread = threading.Thread(target=self._run, daemon=True)
        self.worker_thread.start()

    def stop(self) -> None:
        if self.core:
            self.core.detener()
        self.var_estado.set("Estado: Cancelando (esperando fin de conexión)...")
        self._log_ui("Cancelación solicitada por el usuario.")

    def _run(self) -> None:
        if not self.core:
            return

        def on_estado(text: str, active: bool) -> None:
            self.root.after(0, lambda: self.var_estado.set(f"Estado: {text}"))
            self.root.after(0, lambda: self._log_ui(text))
            if active:
                self.root.after(0, self._set_progress_indeterminate)
            else:
                self.root.after(0, self._set_progress_determinate)

        def on_exito_mensaje(text: str, idx: int, total: int) -> None:
            self.root.after(0, lambda: self.var_intentos.set(f"Progreso de cola: {idx}/{total} completados"))
            self.root.after(0, lambda: self.var_exitos.set(f"Éxitos históricos: {self.core.estado.exitos}"))
            self.root.after(0, lambda: self._log_ui(f"✅ Msj {idx}/{total} OK — {text[:60].replace(chr(10), ' ')}..."))

        def on_gasto(usd: float, tin: int, tout: int) -> None:
            self.root.after(0, lambda: self._actualizar_label_gasto(usd, self.core.config.tope_gasto_usd))

        def on_error(error: str) -> None:
            self.root.after(0, self._finish_progress)
            self.root.after(0, lambda: self.var_error.set(f"Último error: {error}"))
            self.root.after(0, lambda: self._log_ui(f"❌ {error}"))
            if self.root.state() not in ('iconic', 'withdrawn'):
                self.root.after(0, lambda: messagebox.showerror("Lote Pausado", error))

        def on_tick(total: float, remaining: float) -> None:
            minutes, seconds = divmod(int(remaining), 60)
            next_time = datetime.now() + timedelta(seconds=int(remaining))
            elapsed = max(0, total - remaining)
            self.root.after(0, lambda: self.var_prox.set(f"Próximo intento: {next_time:%H:%M:%S} ({minutes:02d}:{seconds:02d})"))
            self.root.after(0, lambda: self.progress.config(maximum=max(total, 1), value=elapsed))

        def on_fin_ciclo(completados: int, total: int, ciclo: int) -> None:
            self.root.after(0, lambda: self.var_intentos.set(f"Ciclo {ciclo} finalizado: {completados}/{total}"))
            if not self.core.config.modo_continuo:
                self.root.after(0, lambda: self.var_estado.set(f"Estado: 🎉 Secuencia finalizada ({completados}/{total})"))
                self.root.after(0, lambda: self._log_ui(f"🎉 Secuencia finalizada: {completados}/{total}."))
                if self.root.state() not in ('iconic', 'withdrawn'):
                    self.root.after(0, lambda: messagebox.showinfo("Finalizado", f"Se completaron {completados}/{total} mensajes."))

        self.core.ejecutar(on_estado=on_estado, on_exito_mensaje=on_exito_mensaje, on_fin_ciclo=on_fin_ciclo,
                           on_error=on_error, on_tick=on_tick, on_gasto=on_gasto)

        KeepAwake.disable()
        self.root.after(0, self._finish_progress)
        self.root.after(0, lambda: self.var_estado.set("Estado: Detenido o completado."))
        self.root.after(0, lambda: self.var_prox.set("Próximo intento: --:--"))
        self.root.after(0, lambda: self._toggle_ui(True))

    # ---------- telegram / modelos ----------

    def test_telegram(self) -> None:
        token = self.entry_tg_token.get().strip()
        chat_id = self.entry_chat.get().strip()
        if not token or not chat_id:
            messagebox.showwarning("Datos incompletos", "Introduce el Bot Token y el Chat ID.")
            return
        self.btn_test_tg.config(state="disabled")

        def _worker():
            try:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = json.dumps({"chat_id": chat_id, "text": "✅ Telegram configurado correctamente."}).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode("utf-8", errors="replace"))
                    if not data.get("ok", False):
                        raise RuntimeError(data.get("description", "Error desconocido"))
                self.root.after(0, lambda: messagebox.showinfo("Telegram", "✓ Mensaje de prueba enviado."))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error de Telegram", str(e)))
            finally:
                self.root.after(0, lambda: self.btn_test_tg.config(state="normal"))
        threading.Thread(target=_worker, daemon=True).start()

    def load_models(self) -> None:
        key = self._get_api_key_from_ui()
        if not key:
            messagebox.showwarning("Falta API Key", "Necesitas una API Key para cargar modelos.")
            return
        self.btn_models.config(state="disabled")

        def _worker():
            try:
                client = anthropic.Anthropic(api_key=key, timeout=15, max_retries=0)
                models = sorted(set(m.id for m in client.models.list().data), reverse=True)
                if not models:
                    raise RuntimeError("La API no devolvió modelos.")

                def update():
                    self.combo_model.config(values=models)
                    if self.combo_model.get() not in models:
                        self.combo_model.set(models[0])
                    messagebox.showinfo("Modelos", f"Se cargaron {len(models)} modelos.")
                self.root.after(0, update)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", f"Fallo al cargar:\n\n{e}"))
            finally:
                self.root.after(0, lambda: self.btn_models.config(state="normal"))
        threading.Thread(target=_worker, daemon=True).start()

    # ---------- progreso / UI helpers ----------

    def _reset_ui_for_start(self) -> None:
        self.var_estado.set("Estado: Iniciando secuencia...")
        self.var_intentos.set("Progreso de cola: Evaluando...")
        self.var_prox.set("Próximo intento: --:--")
        self.var_error.set("Último error: Ninguno")
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)

    def _set_progress_indeterminate(self) -> None:
        self.progress.stop()
        self.progress.config(mode="indeterminate")
        self.progress.start(15)

    def _set_progress_determinate(self) -> None:
        self.progress.stop()
        self.progress.config(mode="determinate")

    def _finish_progress(self) -> None:
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)

    def _toggle_ui(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        inverse = "disabled" if enabled else "normal"
        self.btn_start.config(state=state)
        self.btn_stop.config(state=inverse)
        for widget in (self.txt_msg, self.entry_tg_token, self.entry_chat, self.combo_model,
                       self.spin_errors, self.spin_timeout, self.btn_test_tg, self.btn_models,
                       self.entry_cutoff, self.spin_cycle, self.spin_delay_msgs, self.entry_tope,
                       self.entry_price_in, self.entry_price_out, self.entry_sep):
            try:
                widget.config(state=state)
            except Exception:
                pass
        if not self.api_key_override:
            self.entry_key.config(state="disabled")

    def open_logs(self) -> None:
        LOG_FILE.touch(exist_ok=True)
        self._open_path(LOG_FILE)

    def open_responses(self) -> None:
        resp_dir = DATA_DIR / "responses"
        resp_dir.mkdir(exist_ok=True)
        self._open_path(resp_dir)

    @staticmethod
    def _open_path(path: Path) -> None:
        try:
            if platform.system() == "Windows":
                os.startfile(path)  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo abrir:\n{e}")

    # ---------- bandeja / cierre ----------

    def _create_tray_icon(self):
        image = Image.new('RGB', (64, 64), color=(43, 45, 48))
        dc = ImageDraw.Draw(image)
        dc.rectangle((16, 16, 48, 48), fill=(86, 179, 129))
        return image

    def _on_close(self) -> None:
        if self.core and self.worker_thread and self.worker_thread.is_alive():
            resp = messagebox.askyesnocancel(
                "Monitor en ejecución",
                "El monitor está procesando un lote.\n\n¿Qué deseas hacer?\n\n"
                "Sí = Minimizar a bandeja y seguir\nNo = Pausar el lote y cerrar\nCancelar = Volver")
            if resp is None:
                return
            if resp:
                self._minimize_to_tray()
            else:
                self.stop()
                self.root.after(300, self._quit_app)
        else:
            self._quit_app()

    def _minimize_to_tray(self) -> None:
        self.root.withdraw()
        if not self.tray_icon and HAS_PYSTRAY:
            menu = pystray.Menu(
                pystray.MenuItem("Restaurar", self._restore_window, default=True),
                pystray.MenuItem("Salir", self._quit_app))
            self.tray_icon = pystray.Icon(APP_NAME, self._create_tray_icon(), "Claude Batch Monitor", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        elif not HAS_PYSTRAY:
            messagebox.showinfo("Bandeja no disponible", "Instala 'pystray' y 'Pillow' para minimizar a la bandeja.")
            self.root.deiconify()

    def _restore_window(self, icon=None, item=None) -> None:
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.after(0, self.root.deiconify)
        self.root.after(100, self.root.lift)
        self.root.after(100, self.root.focus_force)

    def _quit_app(self, icon=None, item=None) -> None:
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        if self.core:
            self.core.detener()
        KeepAwake.disable()
        try:
            self.root.destroy()
        except Exception:
            pass
        sys.exit(0)

# ============================================================
# CLI
# ============================================================

def main_cli() -> None:
    parser = argparse.ArgumentParser(description="Claude Batch Monitor - modo CLI")
    parser.add_argument("--mensaje", required=True, help="Texto (o ruta a archivo) con las órdenes.")
    parser.add_argument("--modelo", default=DEFAULT_MODEL)
    parser.add_argument("--separador", default=DEFAULT_SEPARATOR)
    parser.add_argument("--hora-limite", default=DEFAULT_CUTOFF_TIME)
    parser.add_argument("--una-sola-vez", action="store_true")
    parser.add_argument("--pausa-ciclos", type=int, default=DELAY_BETWEEN_CYCLES_SEC)
    parser.add_argument("--tope-gasto", type=float, default=5.0, help="Límite de gasto en USD (0 = sin tope).")
    parser.add_argument("--precio-in", type=float, default=DEFAULT_PRICE_IN)
    parser.add_argument("--precio-out", type=float, default=DEFAULT_PRICE_OUT)
    args = parser.parse_args()

    mensaje_texto = args.mensaje
    if os.path.isfile(args.mensaje):
        with open(args.mensaje, 'r', encoding='utf-8') as f:
            mensaje_texto = f.read()

    config = Configuracion(
        mensaje=mensaje_texto, modelo=args.modelo, separador=args.separador,
        hora_limite=args.hora_limite, modo_continuo=not args.una_sola_vez,
        pausa_ciclos_sec=args.pausa_ciclos, tope_gasto_usd=args.tope_gasto,
        precio_input_mtok=args.precio_in, precio_output_mtok=args.precio_out,
    )

    mensajes = split_messages(config.mensaje, config.separador)
    if not mensajes:
        print("No se detectaron mensajes válidos.")
        sys.exit(1)

    job = Job(uuid.uuid4().hex[:8], datetime.now().isoformat(), args.modelo, mensajes, args.separador)
    job.save()

    core = MonitorCore(config, job)
    KeepAwake.enable()
    try:
        core.ejecutar(
            on_estado=lambda text, active: print(f"[{datetime.now():%H:%M:%S}] {text}"),
            on_exito_mensaje=lambda text, idx, total: print(f"\n✅ RESPUESTA [{idx}/{total}]:\n{text[:150]}..."),
            on_fin_ciclo=lambda c, t, ciclo: print(f"\n🎉 CICLO {ciclo} FINALIZADO. Completados: {c}/{t}"),
            on_error=lambda error: print(f"\n❌ ERROR:\n{error}"),
            on_tick=lambda total, remaining: print(f"\rEsperando: {int(remaining)} s...", end="", flush=True),
            on_gasto=lambda usd, tin, tout: print(f"\n💸 Gasto acumulado: ${usd:.4f}"),
        )
    except KeyboardInterrupt:
        print("\nDetenido por el usuario. El lote se ha pausado.")
        core.detener()
    finally:
        KeepAwake.disable()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        main_cli()
    else:
        root = tk.Tk()
        app = MonitorGUI(root)
        root.mainloop()
