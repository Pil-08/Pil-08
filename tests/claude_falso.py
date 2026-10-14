"""Un `claude` de mentira que imprime lo mismo que el de verdad (`claude -p --output-format stream-json`).

Las líneas de salida están copiadas de una ejecución real de Claude Code 2.1.296 (inicio, estado, eventos del mensaje,
el mensaje completo, `rate_limit_event` y el resultado) y los desenlaces que no se pueden provocar a voluntad (el límite
del plan, un uso extra, una sesión caducada…) siguen lo que documenta Anthropic. Se usa de dos maneras:

- como programa: `python claude_falso.py -p …` (lo lanza el motor del plan; lo instala `ClaudeFalso`);
- como módulo, con `ClaudeFalso(carpeta)`, que crea el ejecutable `claude` en una carpeta de pruebas y lee lo que anotó.

Cómo se programa lo que ocurre: con una marca dentro del texto de la pregunta,

    "¿Qué hora es? [[plan:limite(2),ok]]"

Cada vez que llega esa pregunta se aplica el siguiente paso del plan (la cuenta se lleva en disco entre ejecuciones);
al acabarse, se repite el último. Sin marca, responde bien.

  ok(N)             respuesta correcta que gasta N tokens de salida (20 por omisión)
  limite(S)         límite de la sesión de 5 horas; vuelve dentro de S segundos (evento + texto)
  limite_texto(S)   lo mismo pero SOLO con el texto («resets 3:45pm»), sin evento: la hora es de minuto en minuto
  semanal(S)        límite semanal
  limite_sin_hora   límite sin evento y sin hora de vuelta
  extra             respuesta correcta, pero Claude Code ya gastó créditos extra (isUsingOverage)
  creditos          el plan no tiene más uso incluido y piden créditos (credits_required)
  gasto             límite de gasto mensual de la cuenta
  auth              sesión caducada
  cuenta            cuenta en espera (account_on_hold)
  modelo            el modelo no existe o no está en el plan
  invalido          petición inválida (400)
  saturado          servidor saturado (529)
  error500          error 500
  cuelga(S)         no imprime nada durante S segundos (y luego responde bien)
  goteo(S)          responde bien, pero escribiendo poco a poco: tarda S segundos EN TOTAL y nunca calla mucho rato
  lento(S)          tarda S segundos en responder bien
  basura            imprime algo que no es JSON y sale con 0
  crash             empieza a responder y muere (código 3) sin resultado
  vacio             respuesta correcta sin texto
  trunca            respuesta cortada por max_tokens
  rechazo           el modelo se niega (stop_reason refusal)
  clave             el inicio dice que usa una clave de API (el motor debe cortar antes de que responda)
  grande(K)         respuesta de K kilobytes
  sin_resultado     eventos correctos pero sin la línea final `result`, sale con 0

Variables que lo controlan: CLAUDE_FALSO_DIR (carpeta de estado, obligatoria), CLAUDE_FALSO_AUTH (ok | oauth | none |
api_key | helper | antiguo), CLAUDE_FALSO_VERSION, CLAUDE_FALSO_SIN (opciones que «no conoce», separadas por comas).
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Optional

MARCA = re.compile(r"\[\[plan:([^\]]*)\]\]")
PASO = re.compile(r"([a-z_0-9]+)(?:\(([^)]*)\))?")
VERSION = "2.1.296"
SIN_VALOR = {"-p", "--verbose", "--include-partial-messages", "--no-session-persistence", "--disable-slash-commands",
             "--safe-mode"}
CON_VALOR = {"--output-format", "--model", "--system-prompt-file", "--tools", "--append-system-prompt",
             "--system-prompt", "--permission-mode"}
VARIABLES_VIGILADAS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                       "CLAUDE_CODE_USE_FOUNDRY")


def _plan_de(texto: str) -> list[tuple[str, str]]:
    m = MARCA.search(texto)
    return [(n, a) for n, a in PASO.findall(m.group(1))] if m else []


def _sin_marca(texto: str) -> str:
    return MARCA.sub("", texto).strip()


def _hora_de(instante: float) -> str:
    """`3:45pm`, como la enseña Claude Code (hora local, de minuto en minuto)."""
    t = time.localtime(instante)
    h = t.tm_hour % 12 or 12
    return f"{h}:{t.tm_min:02d}{'pm' if t.tm_hour >= 12 else 'am'}"


def _linea(objeto: dict) -> None:
    sys.stdout.write(json.dumps(objeto, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _inicio(sesion: str, origen_clave: str = "none") -> dict:
    return {"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": sesion, "tools": [],
            "mcp_servers": [], "model": "claude-sonnet-5-5", "permissionMode": "default", "slash_commands": [],
            "apiKeySource": origen_clave, "claude_code_version": VERSION, "output_style": "default",
            "uuid": "0000-init"}


def _uso(entrada: int, salida: int) -> dict:
    return {"input_tokens": entrada, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 2424,
            "output_tokens": salida, "service_tier": "standard"}


def _evento_de_limite(estado: str, reinicio: Optional[float], tipo: str = "five_hour", extra: Optional[dict] = None,
                      usando_extra: bool = False, ventanas: bool = True) -> dict:
    info: dict[str, Any] = {"status": estado, "rateLimitType": tipo, "overageStatus": "rejected",
                            "overageDisabledReason": "out_of_credits", "isUsingOverage": usando_extra}
    if ventanas:
        info["unifiedWindows"] = {"five_hour": {"utilization": 0.13, "resetsAt": int(time.time()) + 4 * 3600},
                                  "seven_day": {"utilization": 0.02, "resetsAt": int(time.time()) + 6 * 86400}}
    if reinicio is not None:
        info["resetsAt"] = int(reinicio)
        if tipo in info.get("unifiedWindows", {}):
            info["unifiedWindows"][tipo]["resetsAt"] = int(reinicio)
    info.update(extra or {})
    return {"type": "rate_limit_event", "rate_limit_info": info, "uuid": "0000-limite", "session_id": "s"}


def _responder(pregunta: str, sesion: str, salida: int, texto: Optional[str] = None, stop: str = "end_turn",
               con_evento: bool = True, usando_extra: bool = False, con_resultado: bool = True,
               origen_clave: str = "none", pausa: float = 0.0) -> None:
    texto = f"Respuesta a: {_sin_marca(pregunta)[:60]}" if texto is None else texto
    entrada = max(1, len(pregunta) // 4)
    _linea(_inicio(sesion, origen_clave))
    _linea({"type": "system", "subtype": "status", "status": "requesting", "session_id": sesion, "uuid": "0000-estado"})
    _linea({"type": "stream_event", "event": {"type": "message_start", "message": {
        "model": "claude-sonnet-5-5", "id": "msg_falso", "type": "message", "role": "assistant", "content": [],
        "stop_reason": None, "usage": _uso(entrada, 1)}}, "session_id": sesion, "parent_tool_use_id": None,
        "uuid": "0000-1"})
    _linea({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                              "content_block": {"type": "text", "text": ""}},
            "session_id": sesion, "parent_tool_use_id": None, "uuid": "0000-2"})
    trozos = [texto[i:i + 12] for i in range(0, len(texto), 12)] or [""]
    for trozo in trozos:
        if pausa:
            time.sleep(pausa)
        if trozo:
            _linea({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                      "delta": {"type": "text_delta", "text": trozo}},
                    "session_id": sesion, "parent_tool_use_id": None, "uuid": "0000-3"})
    _linea({"type": "assistant", "message": {"model": "claude-sonnet-5-5", "id": "msg_falso", "type": "message",
                                             "role": "assistant", "content": [{"type": "text", "text": texto}],
                                             "stop_reason": None, "usage": _uso(entrada, salida)},
            "parent_tool_use_id": None, "session_id": sesion, "uuid": "0000-4"})
    _linea({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}, "session_id": sesion,
            "parent_tool_use_id": None, "uuid": "0000-5"})
    _linea({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": stop},
                                              "usage": _uso(entrada, salida)},
            "session_id": sesion, "parent_tool_use_id": None, "uuid": "0000-6"})
    _linea({"type": "stream_event", "event": {"type": "message_stop"}, "session_id": sesion,
            "parent_tool_use_id": None, "uuid": "0000-7"})
    if con_evento:
        _linea(_evento_de_limite("allowed", time.time() + 4 * 3600, usando_extra=usando_extra))
    if con_resultado:
        _linea({"type": "result", "subtype": "success", "is_error": False, "api_error_status": None, "duration_ms": 12,
                "num_turns": 1, "result": texto, "stop_reason": stop, "session_id": sesion, "total_cost_usd": 0.0005,
                "usage": _uso(entrada, salida), "permission_denials": [], "terminal_reason": "completed",
                "uuid": "0000-fin"})


def _fallo(sesion: str, texto: str, estado: Optional[int], error: str, evento: Optional[dict] = None,
           subtipo: str = "success") -> None:
    """Un error de la API tal como lo cuenta Claude Code: mensaje del asistente con `error`, evento y resultado."""
    _linea(_inicio(sesion))
    _linea({"type": "system", "subtype": "status", "status": "requesting", "session_id": sesion, "uuid": "0000-estado"})
    _linea({"type": "assistant", "message": {"model": "<synthetic>", "id": "msg_error", "type": "message",
                                             "role": "assistant", "content": [{"type": "text", "text": texto}],
                                             "stop_reason": "stop_sequence", "usage": _uso(0, 0)},
            "error": error, "parent_tool_use_id": None, "session_id": sesion, "uuid": "0000-error"})
    if evento is not None:
        _linea(evento)
    _linea({"type": "result", "subtype": subtipo, "is_error": True, "api_error_status": estado, "duration_ms": 5,
            "num_turns": 1, "result": texto, "stop_reason": "stop_sequence", "session_id": sesion,
            "total_cost_usd": 0, "usage": _uso(0, 0), "permission_denials": [], "terminal_reason": "api_error",
            "uuid": "0000-fin"})


def _siguiente_paso(carpeta: Path, pregunta: str) -> tuple[str, str]:
    pasos = _plan_de(pregunta)
    if not pasos:
        return "ok", ""
    archivo = carpeta / "contadores.json"
    try:
        cuentas = json.loads(archivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cuentas = {}
    clave = _sin_marca(pregunta) + "||" + MARCA.search(pregunta).group(1)  # type: ignore[union-attr]
    n = cuentas.get(clave, 0)
    cuentas[clave] = n + 1
    archivo.write_text(json.dumps(cuentas), encoding="utf-8")
    return pasos[min(n, len(pasos) - 1)]


def _numero(arg: str, defecto: float) -> float:
    try:
        return float(arg)
    except ValueError:
        return defecto


def principal(argv: list[str]) -> int:
    # El `claude` de verdad (Node) escribe siempre en UTF-8, sea cual sea el idioma del equipo: éste también.
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    carpeta = Path(os.environ["CLAUDE_FALSO_DIR"])
    carpeta.mkdir(parents=True, exist_ok=True)
    sin = {o for o in os.environ.get("CLAUDE_FALSO_SIN", "").split(",") if o}
    auth = os.environ.get("CLAUDE_FALSO_AUTH", "ok")
    version = os.environ.get("CLAUDE_FALSO_VERSION", VERSION)

    registro: dict[str, Any] = {"argv": argv, "cwd": os.getcwd(), "pid": os.getpid(), "t": time.time(),
                                "env": {v: (v in os.environ) for v in VARIABLES_VIGILADAS}}

    def anotar() -> None:
        with (carpeta / "llamadas.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")

    if argv[:1] == ["--version"]:
        registro["modo"] = "version"
        anotar()
        print(f"{version} (Claude Code)")
        return 0
    if argv[:1] == ["--help"]:
        registro["modo"] = "ayuda"
        anotar()
        print("Usage: claude [options] [command] [prompt]")
        return 0
    if argv[:2] == ["auth", "status"]:
        registro["modo"] = "auth"
        anotar()
        if auth == "antiguo":
            sys.stderr.write("error: unknown command 'auth'\n")
            return 1
        if auth == "none":
            print(json.dumps({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}))
            return 1
        metodo = {"ok": "claude.ai", "oauth": "oauth_token", "api_key": "api_key", "helper": "api_key_helper"}.get(
            auth, "claude.ai")
        print(json.dumps({"loggedIn": True, "authMethod": metodo, "apiProvider": "firstParty",
                          "configDirectory": str(carpeta)}))
        return 0

    # --- claude -p ---
    opciones: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in sin or not (a in SIN_VALOR or a in CON_VALOR):
            sys.stderr.write(f"error: unknown option '{a}'\n")
            registro["modo"] = "rechazada"
            registro["rechazo"] = a
            anotar()
            return 1
        if a in CON_VALOR:
            if i + 1 >= len(argv) and a != "--tools":
                sys.stderr.write(f"error: option '{a} <value>' argument missing\n")
                return 1
            opciones.append(a)
            registro.setdefault("valores", {})[a] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        else:
            opciones.append(a)
            i += 1
    pregunta = sys.stdin.buffer.read().decode("utf-8", "replace")
    registro.update({"modo": "p", "opciones": opciones, "pregunta": pregunta})
    archivo_sistema = registro.get("valores", {}).get("--system-prompt-file")
    if archivo_sistema:
        try:
            registro["sistema"] = Path(archivo_sistema).read_text(encoding="utf-8")
        except OSError:
            registro["sistema"] = None

    paso, arg = _siguiente_paso(carpeta, pregunta)
    registro["paso"] = f"{paso}({arg})" if arg else paso
    anotar()
    sesion = f"sesion-{os.getpid()}"
    retraso = _numero(os.environ.get("CLAUDE_FALSO_RETRASO", "0"), 0.0)
    if retraso:
        time.sleep(retraso)

    ahora = time.time()
    if paso == "ok":
        _responder(pregunta, sesion, int(_numero(arg, 20)) if arg else 20)
    elif paso == "grande":
        _responder(pregunta, sesion, 2000, texto="x" * int(_numero(arg, 100) * 1024))
    elif paso == "vacio":
        _responder(pregunta, sesion, 1, texto="")
    elif paso == "trunca":
        _responder(pregunta, sesion, 200, stop="max_tokens")
    elif paso == "rechazo":
        _responder(pregunta, sesion, 5, stop="refusal")
    elif paso == "extra":
        _responder(pregunta, sesion, 20, usando_extra=True)
    elif paso == "sin_resultado":
        _responder(pregunta, sesion, 20, con_resultado=False)
    elif paso == "clave":
        _linea(_inicio(sesion, "ANTHROPIC_API_KEY"))
        time.sleep(20)  # el motor debe haberlo cortado mucho antes
        _responder(pregunta, sesion, 20, origen_clave="ANTHROPIC_API_KEY")
    elif paso in ("limite", "semanal"):
        segundos = _numero(arg, 5)
        tipo = "five_hour" if paso == "limite" else "seven_day"
        cual = "session" if paso == "limite" else "weekly"
        cuando = _hora_de(ahora + segundos) if paso == "limite" else f"Mon {_hora_de(ahora + segundos)}"
        _fallo(sesion, f"You've hit your {cual} limit · resets {cuando}", 429, "rate_limit",
               _evento_de_limite("rejected", ahora + segundos, tipo))
        return 1
    elif paso == "limite_texto":
        segundos = _numero(arg, 120)
        _fallo(sesion, f"You've hit your session limit · resets {_hora_de(ahora + segundos)}", 429, "rate_limit")
        return 1
    elif paso == "limite_sin_hora":
        _fallo(sesion, "You've hit your session limit", 429, "rate_limit",
               _evento_de_limite("rejected", None, ventanas=False))
        return 1
    elif paso == "creditos":
        _fallo(sesion, "Usage credits required to continue", 429, "rate_limit",
               _evento_de_limite("rejected", ahora + 3600, extra={"errorCode": "credits_required",
                                                                  "canUserPurchaseCredits": True}))
        return 1
    elif paso == "gasto":
        _fallo(sesion, "You've hit your monthly spend limit · raise it at https://claude.ai/settings/usage", 429,
               "rate_limit")
        return 1
    elif paso == "auth":
        _fallo(sesion, "Not logged in · Please run /login", 401, "authentication_failed")
        return 1
    elif paso == "cuenta":
        _fallo(sesion, "Your account is on hold. Contact support.", 403, "account_on_hold")
        return 1
    elif paso == "modelo":
        _fallo(sesion, "There's an issue with the selected model (x). It may not exist or you may not have access "
                       "to it. Run /model to pick a different model.", 404, "model_not_found")
        return 1
    elif paso == "invalido":
        _fallo(sesion, "API Error: 400 invalid_request_error: messages: text content blocks must be non-empty", 400,
               "invalid_request")
        return 1
    elif paso == "saturado":
        _fallo(sesion, "API Error: Overloaded (529)", 529, "overloaded")
        return 1
    elif paso == "error500":
        _fallo(sesion, "API Error: 500 Internal server error", 500, "server_error")
        return 1
    elif paso in ("cuelga", "lento"):
        time.sleep(_numero(arg, 5))
        _responder(pregunta, sesion, 20)
    elif paso == "goteo":
        texto = "Respuesta larga que va llegando poco a poco, sin parar, durante un buen rato. " * 2   # unos 150 caracteres
        trozos = max(1, -(-len(texto) // 12))
        _responder(pregunta, sesion, 20, texto=texto, pausa=_numero(arg, 3) / trozos)
    elif paso == "basura":
        print("esto no es JSON, es una página de un portal cautivo")
        return 0
    elif paso == "crash":
        _linea(_inicio(sesion))
        sys.stderr.write("Error: el proceso se vino abajo\n")
        return 3
    else:
        _responder(pregunta, sesion, 20)
    return 0


# ----------------------------------------------------------------------
# Para los tests: crear el ejecutable `claude` y leer lo que anotó
# ----------------------------------------------------------------------


class ClaudeFalso:
    def __init__(self, carpeta: Path):
        self.carpeta = carpeta
        self.estado = carpeta / "estado"
        self.estado.mkdir(parents=True, exist_ok=True)
        self.ruta = carpeta / "claude"
        self.ruta.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).resolve()}" "$@"\n', encoding="utf-8")
        self.ruta.chmod(self.ruta.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def entorno(self) -> dict[str, str]:
        return {"CLAUDE_FALSO_DIR": str(self.estado), "CLAUDE_MONITOR_CLAUDE": str(self.ruta)}

    def llamadas(self) -> list[dict]:
        archivo = self.estado / "llamadas.jsonl"
        if not archivo.exists():
            return []
        return [json.loads(linea) for linea in archivo.read_text(encoding="utf-8").splitlines() if linea.strip()]

    def preguntas(self) -> list[dict]:
        """Las veces que se le pidió responder (no las consultas de versión o de sesión)."""
        return [x for x in self.llamadas() if x.get("modo") == "p"]

    def veces(self, texto: str) -> int:
        """Cuántas veces se le hizo una pregunta que contiene este texto."""
        return sum(1 for x in self.preguntas() if texto in x["pregunta"])


if __name__ == "__main__":
    sys.exit(principal(sys.argv[1:]))
