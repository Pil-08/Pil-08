"""Hablar con Claude Code (`claude -p`): localizarlo, comprobar la sesión, lanzarlo y entender lo que imprime.

Es la mitad «de fuera» del modo plan (la otra mitad es `plan.py`). Este programa NO toca tus credenciales ni la
API: lanza el programa oficial `claude`, que ya está conectado a tu cuenta, le pasa la pregunta y lee su salida
(`--output-format stream-json`, documentada por Anthropic). Todo lo que sabe de un límite del plan lo lee de ahí.

Lo que se vigila, porque es lo que haría pagar de más:
- Si el entorno trae una clave de API (`ANTHROPIC_API_KEY`…), `claude -p` la usa SIEMPRE en vez de tu plan: se quita
  del entorno del hijo, y además se comprueba que Claude Code diga que no usa clave (`apiKeySource`).
- Si el plan se agota y tienes activado el «uso extra» de claude.ai, Claude Code seguiría con créditos de pago:
  el evento `rate_limit_event` lo cuenta (`isUsingOverage`) y el motor se para en cuanto lo ve.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Callable, Optional

from .. import paths
from ..paths import logger

# Para probar con un `claude` falso (o forzar uno concreto). Si está puesta, es la única que se mira.
VARIABLE_RUTA = "CLAUDE_MONITOR_CLAUDE"
VERSION_MINIMA = (2, 0, 0)
SEGUNDOS_DE_CACHE = 90.0
MAX_BYTES_STDERR = 8000
MAX_LINEAS_AJENAS = 20
# Si el reinicio del límite que dice Claude Code es absurdo (más lejos que esto, o ya muy pasado), no se le cree.
MAX_DIAS_HASTA_EL_REINICIO = 40
MARGEN_TRAS_EL_REINICIO_SEC = 90.0

# Variables del entorno que harían que Claude Code NO use tu plan sino una clave o un proveedor de pago por uso.
VARIABLES_DE_PAGO_POR_USO = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
# `claude auth status` dice cómo está iniciada la sesión: estos son un plan; el resto, no.
METODOS_DE_PLAN = ("claude.ai", "oauth_token")
METODOS_DE_PAGO_POR_USO = ("api_key", "api_key_helper", "third_party")
# `apiKeySource` del evento de inicio: así de «ninguna clave» se ve cuando se usa el plan.
ORIGENES_SIN_CLAVE = ("", "none", "oauth")

SISTEMA_BASE = (
    "You are Claude, an AI assistant made by Anthropic. The user is typing questions into a simple question-queue "
    "app and reading the answers there. Answer each question helpfully and completely, in the language of the "
    "question, using Markdown when it helps. You have no tools in this chat: you cannot read files, browse or run "
    "commands, so answer from what you know and from what the question itself contains."
)


# ----------------------------------------------------------------------
# Dónde está Claude Code y cómo se lanza
# ----------------------------------------------------------------------


def _rutas_habituales() -> list[str]:
    casa = Path.home()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(casa / "AppData" / "Local")
        datos = os.environ.get("APPDATA") or str(casa / "AppData" / "Roaming")
        return [
            str(casa / ".local" / "bin" / "claude.exe"),
            str(Path(local) / "Programs" / "claude" / "claude.exe"),
            str(casa / ".claude" / "local" / "claude.exe"),
            str(Path(datos) / "npm" / "claude.cmd"),
        ]
    return [
        str(casa / ".local" / "bin" / "claude"),
        str(casa / ".claude" / "local" / "claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        str(casa / ".npm-global" / "bin" / "claude"),
        str(casa / ".bun" / "bin" / "claude"),
        str(casa / ".volta" / "bin" / "claude"),
    ]


def _es_ejecutable(ruta: str) -> bool:
    try:
        return os.path.isfile(ruta) and os.access(ruta, os.X_OK)
    except (OSError, ValueError):
        return False


def buscar_claude(ruta_configurada: str = "") -> Optional[str]:
    """Dónde está el programa `claude`, o None si no se encuentra.

    Un programa abierto con doble clic suele no heredar el PATH de la terminal, por eso se miran también los
    sitios donde el instalador oficial lo deja.
    """
    forzada = (os.environ.get(VARIABLE_RUTA) or "").strip()
    if forzada:
        return forzada if _es_ejecutable(forzada) else None
    candidatos = [(ruta_configurada or "").strip(), shutil.which("claude") or "", *_rutas_habituales()]
    for candidato in candidatos:
        if candidato and _es_ejecutable(candidato):
            return candidato
    return None


def entorno_para_el_plan(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """El entorno con el que se lanza `claude`: el del programa, sin nada que lo desvíe del pago por uso."""
    entorno = dict(os.environ if base is None else base)
    for nombre in VARIABLES_DE_PAGO_POR_USO:
        entorno.pop(nombre, None)
    return entorno


def carpeta_de_trabajo() -> Path:
    """Una carpeta vacía y propia donde se lanza `claude`: así no carga ningún `CLAUDE.md` ni proyecto ajeno."""
    carpeta = paths.DATA_DIR / "plan"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def _opciones_de_proceso() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    return {"start_new_session": True}  # su propio grupo: se le puede matar con todo lo que lance


# ----------------------------------------------------------------------
# ¿Está listo? (instalado, con la sesión iniciada y usando un plan)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class EstadoClaude:
    estado: str  # ok | no_instalado | antiguo | sin_sesion | no_es_plan | error
    ruta: str = ""
    version: str = ""
    metodo: str = ""   # cómo está iniciada la sesión (`authMethod`)
    detalle: str = ""

    @property
    def listo(self) -> bool:
        return self.estado == "ok"

    def explicacion(self) -> str:
        """Qué le pasa a Claude Code y qué hacer, en cristiano."""
        return {
            "ok": "Claude Code está listo y usa tu plan.",
            "no_instalado": (
                "No encuentro Claude Code en este ordenador. Es el programa oficial de Anthropic con el que se usa tu "
                "plan de Claude (Pro o Max). Instálalo desde https://code.claude.com/docs/en/setup, ábrelo una vez e "
                "inicia sesión con tu cuenta."
            ),
            "antiguo": (
                f"Tu Claude Code ({self.version}) es demasiado antiguo para este programa. "
                "Actualízalo con:  claude update"
            ),
            "sin_sesion": (
                "Claude Code está instalado pero no has iniciado sesión. En una terminal ejecuta:  claude auth login  "
                "(e inicia sesión con tu cuenta de Claude)."
            ),
            "no_es_plan": (
                f"Claude Code está iniciado con {self.metodo or 'una clave'}, que se paga por uso, no con tu plan. "
                "Para usar tu plan, en una terminal ejecuta:  claude auth logout  y luego  claude auth login  "
                "(elige tu cuenta de Claude, no «Anthropic Console»)."
            ),
        }.get(self.estado, f"No pude comprobar Claude Code: {self.detalle or 'error desconocido'}")


_cache: dict[str, tuple[float, EstadoClaude]] = {}


def _ejecutar_corto(
    orden: list[str], timeout: float, ejecutar: Callable[..., Any] = subprocess.run
) -> tuple[Optional[int], str, str]:
    """Lanza una orden breve de Claude Code y devuelve (código, stdout, stderr)."""
    try:
        r = ejecutar(
            orden, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=str(carpeta_de_trabajo()),
            env=entorno_para_el_plan(), **({"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
                                           if sys.platform == "win32" else {}),
        )
    except subprocess.TimeoutExpired:
        return None, "", "no respondió a tiempo"
    except OSError as error:
        return None, "", str(error)
    return r.returncode, (r.stdout or b"").decode("utf-8", "replace"), (r.stderr or b"").decode("utf-8", "replace")


def _version(texto: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", texto or "")
    return tuple(int(g) for g in m.groups()) if m else ()


def comprobar_claude(
    ruta_configurada: str = "", ejecutar: Callable[..., Any] = subprocess.run, usar_cache: bool = True
) -> EstadoClaude:
    """¿Se puede usar el plan a través de Claude Code? No gasta nada: solo pregunta versión y estado de la sesión."""
    ruta = buscar_claude(ruta_configurada)
    if ruta is None:
        return EstadoClaude("no_instalado")
    ahora = time.monotonic()
    guardado = _cache.get(ruta)
    if usar_cache and guardado and ahora - guardado[0] < SEGUNDOS_DE_CACHE:
        return guardado[1]
    estado = _comprobar(ruta, ejecutar)
    if estado.listo:  # solo se recuerda lo que está bien: quien acaba de arreglar algo (iniciar sesión…) no espera 90 s
        _cache[ruta] = (ahora, estado)
    else:
        _cache.pop(ruta, None)
    return estado


def olvidar_comprobaciones() -> None:
    _cache.clear()


def _comprobar(ruta: str, ejecutar: Callable[..., Any]) -> EstadoClaude:
    codigo, salida, error = _ejecutar_corto([ruta, "--version"], 30, ejecutar)
    if codigo is None:
        return EstadoClaude("error", ruta, detalle=error)
    version_txt = salida.strip().splitlines()[0] if salida.strip() else ""
    version = _version(version_txt)
    if version and version < VERSION_MINIMA:
        return EstadoClaude("antiguo", ruta, ".".join(map(str, version)))
    numero = ".".join(map(str, version))

    codigo, salida, error = _ejecutar_corto([ruta, "auth", "status"], 45, ejecutar)
    if codigo is None:
        return EstadoClaude("error", ruta, numero, detalle=error)
    datos = _json_de(salida)
    if datos is None:
        if re.search(r"unknown command|too many arguments|did you mean", f"{salida}\n{error}", re.I):
            return EstadoClaude("ok", ruta, numero)  # una versión que aún no sabe contarlo: se confía y se comprueba al usar
        if codigo == 0:
            return EstadoClaude("ok", ruta, numero)
        return EstadoClaude("sin_sesion", ruta, numero, detalle=(error or salida).strip()[:200])
    metodo = str(datos.get("authMethod") or "")
    if datos.get("loggedIn") is False or metodo == "none" or codigo not in (0, None) and not metodo:
        return EstadoClaude("sin_sesion", ruta, numero, metodo)
    if metodo in METODOS_DE_PAGO_POR_USO:
        return EstadoClaude("no_es_plan", ruta, numero, metodo)
    return EstadoClaude("ok", ruta, numero, metodo)


def _json_de(texto: str) -> Optional[dict]:
    """El primer objeto JSON de un texto (tolera avisos antes o después)."""
    texto = (texto or "").strip()
    for candidato in (texto, texto[texto.find("{"): texto.rfind("}") + 1] if "{" in texto else ""):
        if not candidato:
            continue
        try:
            datos = json.loads(candidato)
        except ValueError:
            continue
        if isinstance(datos, dict):
            return datos
    return None


# ----------------------------------------------------------------------
# La línea de órdenes
# ----------------------------------------------------------------------

# Opciones que mejoran el resultado pero no son imprescindibles: si esta versión de Claude Code no las conoce, se
# quitan y se vuelve a intentar (ver `opcion_rechazada`). La primera palabra de cada tupla es la opción.
_OPCIONALES: tuple[tuple[str, ...], ...] = (
    ("--include-partial-messages",),      # ver la respuesta escribirse
    ("--no-session-persistence",),        # no llenar el historial de Claude Code con cada pregunta
    ("--tools", ""),                      # sin herramientas: es una conversación, no un agente
    ("--disable-slash-commands",),        # una pregunta que empiece por «/» no debe ejecutar un comando
    ("--safe-mode",),                     # sin CLAUDE.md, hooks ni plugins de la persona: respuestas iguales para todos
)
MODELO_VALIDO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,79}$")


def argumentos_de_claude(
    ruta: str, modelo: str = "", archivo_sistema: Optional[Path] = None, sin: Collection[str] = ()
) -> list[str]:
    """La orden que lanza una pregunta. La pregunta NO va aquí: va por la entrada estándar."""
    orden = [ruta, "-p", "--output-format", "stream-json", "--verbose"]
    opcionales = list(_OPCIONALES)
    if archivo_sistema is not None:
        opcionales.append(("--system-prompt-file", str(archivo_sistema)))
    for opcion in opcionales:
        if opcion[0] not in sin:
            orden.extend(opcion)
    if modelo and MODELO_VALIDO.match(modelo):
        orden.extend(["--model", modelo])
    return orden


_OPCION_DESCONOCIDA = re.compile(r"unknown option '(--?[\w-]+)'", re.I)
_OPCION_SIN_VALOR = re.compile(r"option '(--[\w-]+)[^']*' argument missing", re.I)


def opcion_rechazada(texto: str) -> str:
    """La opción que esta versión de Claude Code no entiende (o a la que no le llegó el valor), si el texto lo dice."""
    for patron in (_OPCION_DESCONOCIDA, _OPCION_SIN_VALOR):
        m = patron.search(texto or "")
        if m:
            return m.group(1)
    return ""


def texto_del_sistema(instrucciones: str) -> str:
    instrucciones = (instrucciones or "").strip()
    if not instrucciones:
        return SISTEMA_BASE
    return f"{SISTEMA_BASE}\n\nThe user asks you to follow these instructions in every answer:\n{instrucciones}"


# ----------------------------------------------------------------------
# Lo que Claude Code cuenta del plan
# ----------------------------------------------------------------------


def _desde_epoch(valor: Any, ahora: Optional[datetime] = None) -> Optional[datetime]:
    """Un instante en segundos (o milisegundos) desde 1970, o None si no tiene sentido."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    try:
        segundos = float(valor)
        if segundos > 10**11:  # milisegundos
            segundos /= 1000
        instante = datetime.fromtimestamp(segundos, tz=timezone.utc)
    except (OverflowError, ValueError, OSError):
        return None
    ahora = ahora or datetime.now(timezone.utc)
    if instante < ahora - timedelta(days=1) or instante > ahora + timedelta(days=MAX_DIAS_HASTA_EL_REINICIO):
        return None
    return instante


@dataclass
class VentanaPlan:
    uso: Optional[float] = None            # porcentaje consumido (0-100), si Claude Code lo dijo
    reinicia: Optional[datetime] = None    # cuándo se renueva (con zona horaria)


@dataclass
class EstadoPlan:
    """Lo último que se sabe del cupo del plan."""

    sesion: VentanaPlan = field(default_factory=VentanaPlan)   # la ventana de 5 horas
    semana: VentanaPlan = field(default_factory=VentanaPlan)   # la ventana semanal
    situacion: str = ""            # allowed | allowed_warning | rejected
    usando_extra: bool = False     # Claude Code está gastando créditos extra (de pago)
    extra_activado: Optional[bool] = None   # la cuenta tiene el «uso extra» activado
    bloqueado_hasta: Optional[datetime] = None   # hasta cuándo no se puede enviar (un límite alcanzado)

    def conocido(self) -> bool:
        return any(v is not None for v in (self.sesion.uso, self.sesion.reinicia, self.semana.uso, self.semana.reinicia))

    def resumen(self) -> str:
        partes = []
        for nombre, ventana in (("sesión", self.sesion), ("semana", self.semana)):
            if ventana.uso is None and ventana.reinicia is None:
                continue
            texto = f"{nombre} {round(ventana.uso)} %" if ventana.uso is not None else nombre
            if ventana.reinicia is not None:
                local = ventana.reinicia.astimezone()
                dia = "" if local.date() == datetime.now().astimezone().date() else f" {local:%d/%m}"
                texto += f" (se renueva{dia} a las {local:%H:%M})"
            partes.append(texto)
        return " · ".join(partes)


def _ventana_de(nombre: str) -> str:
    return {"five_hour": "sesion", "seven_day": "semana", "seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}.get(
        nombre or "", "otra" if nombre else "")


# ----------------------------------------------------------------------
# Entender el texto de un límite («You've hit your session limit · resets 3:45pm»)
# ----------------------------------------------------------------------

_DIAS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_MESES = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_ZONA = re.compile(r"\(([A-Za-z][A-Za-z0-9_+\-/]{1,40})\)")
_HORA_AMPM = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b\.?", re.I)
_HORA_24 = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_DIA_SEMANA = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I)
_FECHA = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b"
    r"|\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.I,
)
_GASTO = re.compile(r"(monthly|individual|org['’]?s|team['’]?s|shared)\b[^.\n]{0,40}\b(spend|budget)|spend limit", re.I)


def tipo_de_limite(texto: str) -> str:
    """'' si el texto no habla de un límite de uso; si no: sesion | semana | opus | sonnet | gasto | otro."""
    t = (texto or "").lower()
    if _GASTO.search(t):
        return "gasto"
    if "opus limit" in t:
        return "opus"
    if "sonnet limit" in t:
        return "sonnet"
    if re.search(r"\b(weekly|7-day|seven[- ]day)\b[^.\n]{0,30}\blimit", t):
        return "semana"
    if re.search(r"session limit|\b5-hour\b|usage limit reached|your limit will reset|\d+-hour limit", t):
        return "sesion"
    if re.search(r"(hit|reached|exceeded) (your |the )?[\w' -]{1,30}limit", t):
        return "otro"
    return ""


def _zona(nombre: str) -> Optional[tzinfo]:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(nombre)
    except Exception:  # sin la base de zonas (Windows sin `tzdata`) o un nombre que no existe
        return None


def reinicio_desde_texto(texto: str, ahora: Optional[datetime] = None) -> Optional[datetime]:
    """Cuándo vuelve el acceso según el texto de Claude Code, o None si no se entiende.

    Entiende «resets 3:45pm», «resets Mon 12:00am», «resets Oct 20, 9am», «resets 15:45 (Europe/Madrid)» y el formato
    antiguo «usage limit reached|1760000000». Una hora sin zona es la de este equipo: es la que Claude Code enseña.
    """
    ahora = ahora or datetime.now().astimezone()
    if ahora.tzinfo is None:
        ahora = ahora.astimezone()
    texto = texto or ""

    m = re.search(r"\|\s*(\d{9,13})\b", texto)
    if m:
        return _desde_epoch(int(m.group(1)), ahora)

    m = re.search(r"\bresets?\b(.*)", texto, re.I | re.S)
    if not m:
        return None
    resto = m.group(1)[:90]

    zona: tzinfo = ahora.tzinfo  # type: ignore[assignment]
    mz = _ZONA.search(resto)
    if mz:
        z = _zona(mz.group(1))
        if z is not None:
            zona = z
            ahora = ahora.astimezone(z)

    hora = _buscar_hora(resto)
    if hora is None:
        return None
    h, minuto = hora

    try:
        fecha = _FECHA.search(resto)
        dia = _DIA_SEMANA.search(resto)
        if fecha:
            nombre_mes = fecha.group(1) or fecha.group(4)
            dia_mes = int(fecha.group(2) or fecha.group(3))
            candidato = datetime(ahora.year, _MESES[nombre_mes[:3].lower()], dia_mes, h, minuto, tzinfo=zona)
            if candidato < ahora - timedelta(days=1):
                candidato = candidato.replace(year=ahora.year + 1)
        elif dia:
            objetivo = _DIAS[dia.group(1).lower()[:3]]
            candidato = datetime.combine(ahora.date(), dt_time(h, minuto), tzinfo=zona)
            candidato += timedelta(days=(objetivo - ahora.weekday()) % 7)
            if candidato <= ahora:
                candidato += timedelta(days=7)
        else:
            candidato = datetime.combine(ahora.date(), dt_time(h, minuto), tzinfo=zona)
            if candidato <= ahora:
                candidato += timedelta(days=1)
    except (ValueError, KeyError, OverflowError):
        return None
    if candidato > ahora + timedelta(days=MAX_DIAS_HASTA_EL_REINICIO):
        return None
    return candidato


def _buscar_hora(texto: str) -> Optional[tuple[int, int]]:
    m = _HORA_AMPM.search(texto)
    if m:
        h = int(m.group(1))
        if not 1 <= h <= 12:
            return None
        return h % 12 + (12 if m.group(3).lower() == "p" else 0), int(m.group(2) or 0)
    m = _HORA_24.search(texto)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


# ----------------------------------------------------------------------
# Leer la salida de `claude -p --output-format stream-json`
# ----------------------------------------------------------------------

TIPOS_DE_VEREDICTO = ("ok", "limite", "gasto", "creditos", "auth", "cuenta", "modelo", "saturado", "invalido",
                      "opcion", "otro")


@dataclass
class Veredicto:
    """Qué pasó con una pregunta."""

    tipo: str
    texto: str = ""                        # la respuesta (ok) o el mensaje de Claude Code (error)
    reinicia: Optional[datetime] = None    # limite: cuándo vuelve el acceso, si se sabe
    ventana: str = ""                      # limite: sesion | semana | opus | sonnet | otro
    uso: dict = field(default_factory=dict)  # ok: los tokens que dice el resultado
    stop_reason: str = ""
    session_id: str = ""
    usando_extra: bool = False             # el plan se agotó y Claude Code gastó créditos extra con esta pregunta
    opcion: str = ""                       # opcion: la opción que esta versión no entiende
    estado_http: Optional[int] = None


_ERRORES_DE_AUTENTICACION = ("authentication_failed", "oauth_org_not_allowed")
_ERRORES_DE_SERVIDOR = ("overloaded", "server_error", "rate_limit", "unknown")
_PATRON_AUTH = re.compile(
    r"not logged in|please run /login|run /login|invalid api key|invalid x-api-key|oauth token (has )?(expired|revoked)|"
    r"authentication[_ ](failed|error)|invalid authentication|please (log in|sign in)|\blogin required\b", re.I)
_PATRON_CUENTA = re.compile(r"account[_ ]on[_ ]hold|account (is )?(on hold|suspended|disabled)|oauth_org_not_allowed", re.I)
_PATRON_CREDITOS = re.compile(
    r"credits?_required|usage credits required|credit balance is too low|billing[_ ]error|out of (usage )?credits", re.I)
_PATRON_MODELO = re.compile(
    r"model_not_found|may not exist or you may not have access|issue with the selected model|"
    r"model[^.\n]{0,40}(not found|not available|does not exist)", re.I)
_PATRON_INVALIDO = re.compile(r"invalid[_ ]request|prompt is too long|request too large|exceed(s|ed)? the context", re.I)
_PATRON_SERVIDOR = re.compile(
    r"overloaded|server is temporarily limiting|temporary capacity|api error: (5\d\d|429)|internal server error|"
    r"bad gateway|service unavailable|gateway time-?out|econnreset|etimedout|socket hang up|fetch failed|"
    r"unable to connect|connection (error|refused|reset)|network error|request timed out", re.I)


class LectorDeSalida:
    """Va leyendo, línea a línea, lo que imprime `claude -p --output-format stream-json`.

    No lanza nada ni espera: solo entiende líneas. Así se puede probar sin ningún proceso.
    """

    def __init__(self) -> None:
        self.parcial = ""                  # el texto según llega (para enseñarlo escribiéndose)
        self.texto_final = ""              # el del mensaje completo del asistente
        self.error_asistente = ""          # `error` del mensaje del asistente: rate_limit, authentication_failed…
        self.inicio: dict = {}
        self.resultado: Optional[dict] = None
        self.rechazo: Optional[dict] = None   # el último `rate_limit_event` con estado «rejected»
        self.plan = EstadoPlan()
        self.ajenas: list[str] = []        # líneas que no eran JSON
        self.reintentos = 0                # `system/api_retry`: Claude Code reintentando por su cuenta

    # -- entrada -----------------------------------------------------------

    def alimentar(self, linea: str, ahora: Optional[datetime] = None) -> str:
        """Entiende una línea. Devuelve el texto nuevo de la respuesta, si esa línea lo traía."""
        linea = (linea or "").strip()
        if not linea:
            return ""
        try:
            obj = json.loads(linea)
        except ValueError:
            if len(self.ajenas) < MAX_LINEAS_AJENAS:
                self.ajenas.append(linea[:300])
            return ""
        if not isinstance(obj, dict):
            return ""
        tipo = obj.get("type")
        if tipo == "stream_event":
            return self._evento_de_flujo(obj)
        if tipo == "assistant":
            self._asistente(obj)
        elif tipo == "system":
            if obj.get("subtype") == "init":
                self.inicio = obj
            elif obj.get("subtype") == "api_retry":
                self.reintentos += 1
        elif tipo == "rate_limit_event":
            self._limite(obj.get("rate_limit_info"), ahora)
        elif tipo == "result":
            self.resultado = obj
        return ""

    def _evento_de_flujo(self, obj: dict) -> str:
        if obj.get("parent_tool_use_id"):
            return ""
        evento = obj.get("event")
        if not isinstance(evento, dict) or evento.get("type") != "content_block_delta":
            return ""
        delta = evento.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
            self.parcial += delta["text"]
            return delta["text"]
        return ""

    def _asistente(self, obj: dict) -> None:
        if obj.get("parent_tool_use_id"):
            return
        mensaje = obj.get("message")
        if isinstance(mensaje, dict):
            bloques = mensaje.get("content")
            if isinstance(bloques, list):
                texto = "".join(b.get("text", "") for b in bloques if isinstance(b, dict) and b.get("type") == "text"
                                and isinstance(b.get("text"), str))
                if texto:
                    self.texto_final = texto
        error = obj.get("error")
        if isinstance(error, str) and error:
            self.error_asistente = error

    def _limite(self, info: Any, ahora: Optional[datetime]) -> None:
        if not isinstance(info, dict):
            return
        plan = self.plan
        estado = info.get("status")
        if isinstance(estado, str):
            plan.situacion = estado
        if info.get("isUsingOverage") is True:
            plan.usando_extra = True
        if info.get("overageStatus") in ("allowed", "rejected"):
            plan.extra_activado = info.get("overageStatus") == "allowed"

        bruto = info.get("rateLimitType")
        nombre: str = bruto if isinstance(bruto, str) else ""
        reinicio = _desde_epoch(info.get("resetsAt"), ahora)
        crudas = info.get("unifiedWindows")
        ventanas: dict = crudas if isinstance(crudas, dict) else {}
        for clave, destino in (("five_hour", plan.sesion), ("seven_day", plan.semana)):
            datos = ventanas.get(clave)
            if isinstance(datos, dict):
                uso = datos.get("utilization")
                if isinstance(uso, (int, float)) and not isinstance(uso, bool) and 0 <= uso <= 1000:
                    destino.uso = float(uso) * 100 if uso <= 1 else float(uso)  # llega como fracción: 0.13 = 13 %
                destino.reinicia = _desde_epoch(datos.get("resetsAt"), ahora) or destino.reinicia
        if nombre == "five_hour" and reinicio:
            plan.sesion.reinicia = reinicio
        elif nombre == "seven_day" and reinicio:
            plan.semana.reinicia = reinicio

        if estado in ("allowed", "allowed_warning"):
            plan.bloqueado_hasta = None
        if estado == "rejected":
            propio = {"five_hour": plan.sesion, "seven_day": plan.semana}.get(nombre)
            para = (propio.reinicia if propio is not None else None) or reinicio
            rechazo = dict(info)
            rechazo["_reinicia"] = para
            rechazo["_ventana"] = _ventana_de(nombre)
            self.rechazo = rechazo
            plan.bloqueado_hasta = para

    # -- veredicto -----------------------------------------------------------

    def veredicto(
        self, codigo: Optional[int] = None, stderr: str = "", ahora: Optional[datetime] = None
    ) -> Veredicto:
        """Qué pasó, a partir de todo lo leído, del código de salida del proceso y de su stderr."""
        ahora = ahora or datetime.now().astimezone()
        r = self.resultado
        if r is not None:
            sesion = str(r.get("session_id") or self.inicio.get("session_id") or "")
            texto = r.get("result") if isinstance(r.get("result"), str) else ""
            estado_http = r.get("api_error_status") if isinstance(r.get("api_error_status"), int) else None
            es_error = bool(r.get("is_error")) or str(r.get("subtype") or "success") != "success"
            if not es_error:
                if not texto:
                    texto = self.texto_final or self.parcial
                consumo = r.get("usage")
                uso: dict = consumo if isinstance(consumo, dict) else {}
                return Veredicto(
                    "ok", texto, uso=uso, stop_reason=str(r.get("stop_reason") or ""), session_id=sesion,
                    usando_extra=self.plan.usando_extra,
                )
            errores = r.get("errors")                       # lo normal es una lista de textos, pero no nos fiamos
            if isinstance(errores, str):
                errores = [errores]
            elif not isinstance(errores, (list, tuple)):
                errores = []
            fuentes = [texto, self.texto_final, self.parcial, *[str(e) for e in errores], stderr]
            return self._clasificar([f for f in fuentes if f], estado_http, ahora, sesion)

        # Sin línea final. Si Claude Code marcó el mensaje como un error, su texto ES el error; si no, es una respuesta
        # a medias y lo que explica el fallo es lo que dijo el programa al caerse (se mira después).
        if self.error_asistente:
            fuentes = [self.texto_final, self.parcial, stderr, *self.ajenas]
        else:
            fuentes = [stderr, *self.ajenas, self.texto_final, self.parcial]
        if self.rechazo is not None or codigo not in (0, None) or self.error_asistente:
            return self._clasificar([f for f in fuentes if f], None, ahora, "")
        detalle = "Claude Code terminó sin dar ninguna respuesta"
        if codigo == 0:
            detalle += " (la salida no tenía el formato esperado)"
        return Veredicto("otro", (stderr or "").strip()[-500:] or detalle)

    def _clasificar(self, fuentes: list[str], estado_http: Optional[int], ahora: datetime, sesion: str) -> Veredicto:
        todo = "\n".join(fuentes)
        resumen = (fuentes[0] if fuentes else "").strip()[:500]
        error = self.error_asistente

        opcion = opcion_rechazada(todo)
        if opcion:
            return Veredicto("opcion", resumen, opcion=opcion, session_id=sesion)

        rechazo = self.rechazo
        if rechazo is not None and rechazo.get("errorCode") == "credits_required":
            return Veredicto("creditos", resumen, session_id=sesion)
        if _PATRON_CREDITOS.search(todo):
            return Veredicto("creditos", resumen, session_id=sesion)

        tipo = ""
        for fuente in fuentes:
            tipo = tipo_de_limite(fuente)
            if tipo:
                break
        if tipo == "gasto":
            return Veredicto("gasto", resumen, session_id=sesion)
        if rechazo is not None or tipo:
            reinicia = rechazo.get("_reinicia") if rechazo else None
            ventana = (rechazo.get("_ventana") if rechazo else "") or (tipo if tipo != "otro" else "")
            if reinicia is None:
                for fuente in fuentes:
                    reinicia = reinicio_desde_texto(fuente, ahora)
                    if reinicia:
                        break
            return Veredicto("limite", resumen, reinicia=reinicia, ventana=ventana or "otro", session_id=sesion)

        if error in _ERRORES_DE_AUTENTICACION or _PATRON_AUTH.search(todo) or estado_http == 401:
            if error == "oauth_org_not_allowed" or _PATRON_CUENTA.search(todo):
                return Veredicto("cuenta", resumen, session_id=sesion)
            return Veredicto("auth", resumen, session_id=sesion)
        if error == "account_on_hold" or _PATRON_CUENTA.search(todo) or estado_http == 403:
            return Veredicto("cuenta", resumen, session_id=sesion)
        if error in ("model_not_found",) or _PATRON_MODELO.search(todo):
            return Veredicto("modelo", resumen, session_id=sesion)
        if error in ("invalid_request",) or estado_http in (400, 413, 422) or _PATRON_INVALIDO.search(todo):
            return Veredicto("invalido", resumen, session_id=sesion, estado_http=estado_http)
        if (error in _ERRORES_DE_SERVIDOR or (estado_http is not None and (estado_http >= 500 or estado_http in (408, 409, 429)))
                or _PATRON_SERVIDOR.search(todo)):
            return Veredicto("saturado", resumen, session_id=sesion, estado_http=estado_http)
        return Veredicto("otro", resumen or (todo.strip()[:500]), session_id=sesion, estado_http=estado_http)

    # -- comprobaciones al empezar --------------------------------------------

    def usa_clave_de_api(self) -> bool:
        """True si Claude Code dice (en su evento de inicio) que está usando una clave de API en vez del plan."""
        origen = self.inicio.get("apiKeySource")
        return isinstance(origen, str) and origen.strip().lower() not in ORIGENES_SIN_CLAVE


# ----------------------------------------------------------------------
# Un `claude -p` en marcha
# ----------------------------------------------------------------------

FIN = object()  # lo que `siguiente` devuelve cuando ya no hay más líneas


class Ejecucion:
    """Un `claude -p` lanzado: le da la pregunta, entrega las líneas que imprime y se puede matar."""

    def __init__(self, orden: list[str], entrada: str, entorno: Mapping[str, str], carpeta: Path):
        self.proc = subprocess.Popen(
            orden, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(carpeta), env=dict(entorno), **_opciones_de_proceso(),
        )
        self._lineas: queue.Queue = queue.Queue()
        self._stderr = bytearray()
        datos = entrada.encode("utf-8", "replace")
        self._hilos = [
            threading.Thread(target=self._escribir, args=(datos,), daemon=True, name="claude-entrada"),
            threading.Thread(target=self._leer_salida, daemon=True, name="claude-salida"),
            threading.Thread(target=self._leer_errores, daemon=True, name="claude-errores"),
        ]
        for hilo in self._hilos:
            hilo.start()

    # -- hilos ----------------------------------------------------------------

    def _escribir(self, datos: bytes) -> None:
        try:
            self.proc.stdin.write(datos)  # type: ignore[union-attr]
            self.proc.stdin.close()  # type: ignore[union-attr]
        except (BrokenPipeError, OSError, ValueError):
            pass  # el proceso ya terminó o no quiere entrada: lo contará su salida

    def _leer_salida(self) -> None:
        try:
            for crudo in iter(self.proc.stdout.readline, b""):  # type: ignore[union-attr]
                self._lineas.put(crudo.decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass
        finally:
            self._lineas.put(FIN)

    def _leer_errores(self) -> None:
        try:
            for crudo in iter(self.proc.stderr.readline, b""):  # type: ignore[union-attr]
                self._stderr.extend(crudo)
                if len(self._stderr) > MAX_BYTES_STDERR:
                    del self._stderr[: len(self._stderr) - MAX_BYTES_STDERR]
        except (OSError, ValueError):
            pass

    # -- uso ------------------------------------------------------------------

    def siguiente(self, espera: float) -> Any:
        """La siguiente línea de salida, `FIN` si ya no habrá más, o None si pasó `espera` sin nada."""
        try:
            return self._lineas.get(timeout=espera)
        except queue.Empty:
            return None

    @property
    def codigo(self) -> Optional[int]:
        return self.proc.poll()

    def esperar_codigo(self, timeout: float = 10.0) -> Optional[int]:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    @property
    def stderr(self) -> str:
        if self.proc.poll() is not None:
            self._hilos[2].join(timeout=1.0)  # que se haya leído todo lo que dejó escrito
        return bytes(self._stderr).decode("utf-8", "replace")

    def pedir_que_termine(self) -> None:
        """Avisa al proceso (y a lo que haya lanzado) de que termine, sin esperar a que lo haga."""
        if self.proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True, timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
            else:
                os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
            pass

    def terminar(self, gracia: float = 3.0) -> None:
        """Mata el proceso y todo lo que haya lanzado, y espera a que de verdad haya terminado."""
        if self.proc.poll() is not None:
            return
        self.pedir_que_termine()
        try:
            self.proc.wait(timeout=gracia)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if sys.platform == "win32":
                self.proc.kill()
            else:
                os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("Un proceso de Claude Code no terminó tras matarlo (pid %s)", self.proc.pid)

    def cerrar(self) -> None:
        """Recoge los hilos y cierra las tuberías. Siempre, también tras matar el proceso."""
        for hilo in self._hilos:
            hilo.join(timeout=2)
        for flujo in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if flujo is not None:
                    flujo.close()
            except (OSError, ValueError):
                pass
