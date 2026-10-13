"""Un servidor HTTP local que imita la API de Anthropic, con sockets de verdad.

Los dobles de los otros tests sustituyen el transporte del SDK; este llega más lejos: el SDK real abre una
conexión TCP de verdad contra `127.0.0.1`, de modo que se pueden provocar los fallos que un doble no
reproduce (conexión cortada a mitad del cuerpo, reinicio de la conexión, servidor caído y vuelto a levantar,
respuestas lentas que agotan el tiempo, basura en lugar de JSON…).

Cómo se programa lo que ocurre: con una marca dentro del texto de la pregunta,

    "¿Qué hora es? [[plan:429(1),529,ok(40)]]"

Cada vez que el servidor recibe esa pregunta aplica el siguiente paso del plan (429 con `retry-after: 1`, luego
un 529, luego una respuesta correcta que factura 40 tokens de salida); cuando se acaban, repite el último. Sin
marca, responde bien. Así un mismo escenario sirve para la terminal, para la cola y para la ventana.

El servidor lleva la contabilidad de lo que FACTURARÍA (`cobros`): es la verdad contra la que se comprueban
el gasto que anota el programa, el tope y los cobros duplicados.

Pasos del plan (todos aceptan un argumento opcional entre paréntesis):
  ok(N)          respuesta correcta; factura N tokens de salida (20 por omisión)
  429(S)         límite por minuto con `retry-after: S`
  429sin         límite sin `retry-after` ni explicación (tope de gasto, para el programa)
  429tope        tope mensual del nivel de uso: sin `retry-after`, dice cuándo vuelve el acceso
  529 500 502 503 504 408 409     errores de servidor (sin factura)
  400saldo       «credit balance is too low»
  400limite      límite de gasto fijado por la persona en la consola
  401 403 404modelo 413 422       errores del cliente
  corte          cabeceras y la mitad del cuerpo, y se cierra la conexión (FACTURA)
  corte_stream   un par de eventos SSE y se cierra la conexión (FACTURA)
  flujo_trunco   un par de eventos SSE y el flujo se da por terminado «bien», sin el final del mensaje (FACTURA)
  flujo_vacio    cabeceras de flujo y el cuerpo termina sin ningún evento (no factura)
  perdida        procesa la petición (FACTURA) y cierra la conexión sin responder
  cierre         cierra la conexión sin procesar nada (no factura)
  reset          reinicio TCP (RST) sin procesar nada (no factura)
  lento(S)       espera S segundos y responde bien
  basura         200 con un cuerpo que no es JSON
  vacio          respuesta correcta sin contenido
  refusal        el modelo se niega (stop_reason `refusal`; FACTURA)
  maxtokens      respuesta truncada por max_tokens
  uso_raro       respuesta con un `usage` incompleto o con tipos raros
  uso_enorme     respuesta con un `usage` descomunal
  retry_after(X) 429 con un `retry-after` raro: X puede ser nan, inf, -5, abc, 1e999…

En un lote (`/v1/messages/batches`) se usa el primer paso de cada orden: ok(N), refusal, maxtokens (resultado
correcto), `errored`, `expired` y `canceled` (resultado sin factura).
"""

from __future__ import annotations

import json
import random
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

MARCA = re.compile(r"\[\[plan:([^\]]*)\]\]")
PASO = re.compile(r"([a-z_0-9]+)(?:\(([^)]*)\))?")
MODELO = "claude-sonnet-5-5"


def parsear_plan(texto: str) -> list[tuple[str, str]]:
    m = MARCA.search(texto or "")
    return [(nombre, arg) for nombre, arg in PASO.findall(m.group(1))] if m else []


def sin_marca(texto: str) -> str:
    return MARCA.sub("", texto or "").strip()


@dataclass
class Cobro:
    """Una petición que el servidor habría facturado."""

    pregunta: str
    entrada: int
    salida: int
    completa: bool  # False: la conexión se cortó antes de entregar la respuesta entera
    ruta: str       # "mensajes" | "lote"


class ServidorFalso:
    def __init__(self, clave: str = "sk-ant-simulada", semilla: int = 0, retraso_lote: float = 0.4):
        self.clave = clave
        self.retraso_lote = retraso_lote
        self.azar = random.Random(semilla)
        self._cobros: list[Cobro] = []
        self.peticiones: list[dict[str, Any]] = []
        self.lotes: dict[str, dict[str, Any]] = {}
        self.cancelados: list[str] = []
        self.lote_creado_con_retraso = 0.0   # segundos que tarda en responder el POST de crear un lote (ya creado)
        self.guion_resultados: list[str] = []  # un paso por cada descarga de resultados: ok, 500, basura, corte
        # Un paso por cada GET de estado de un lote o de lista de lotes: ok, 500, 529, basura, cierre, reset, corte, lento(S)
        self.guion_consulta: list[str] = []
        self.guion_crear: list[str] = []     # un paso por cada POST de crear un lote: ok, 500, 529, 400, cierre, perdida, creado500
        self.lotes_ajenos_al_crear = 0       # cuántos lotes "de otra persona" (mismo tamaño) aparecen justo antes de crear
        self.desfase_reloj = timedelta(0)    # lo que se adelanta (+) o atrasa (-) el reloj del servidor respecto al de la máquina
        self.cuota_por_minuto: Optional[int] = None   # si se fija, supera → 429 (con retry-after)
        self._ventana: list[float] = []
        self._intentos: dict[str, int] = {}
        self._lock = threading.Lock()
        self._numero_lote = 0
        self._servidor: Optional[ThreadingHTTPServer] = None
        self._hilo: Optional[threading.Thread] = None
        self.puerto = 0

    # ------------------------------------------------------------------
    # Arranque y parada
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.puerto}"

    def iniciar(self) -> ServidorFalso:
        self._escuchar(self.puerto)
        return self

    def _escuchar(self, puerto: int) -> None:
        class Servidor(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        servidor = Servidor(("127.0.0.1", puerto), _Manejador)
        servidor.api = self  # type: ignore[attr-defined]
        self._servidor = servidor
        self.puerto = servidor.server_address[1]
        self._hilo = threading.Thread(target=servidor.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._hilo.start()

    def caer(self) -> None:
        """Deja de escuchar (conexión rechazada), como un servidor apagado o una red caída."""
        if self._servidor is not None:
            self._servidor.shutdown()
            self._servidor.server_close()
            self._servidor = None

    def levantar(self) -> None:
        """Vuelve a escuchar en el mismo puerto."""
        if self._servidor is None:
            self._escuchar(self.puerto)

    def detener(self) -> None:
        self.caer()

    def __enter__(self) -> ServidorFalso:
        return self.iniciar()

    def __exit__(self, *exc: object) -> None:
        self.detener()

    # ------------------------------------------------------------------
    # Contabilidad (lo que se facturaría)
    # ------------------------------------------------------------------

    @property
    def cobros(self) -> list[Cobro]:
        """Lo que se habría facturado hasta ahora. Un lote terminado se factura aunque nadie lo consulte."""
        for id_ in list(self.lotes):
            lote = self.lotes[id_]
            if lote["cancelado"] or time.monotonic() - lote["creado"] >= self.retraso_lote:
                self._resolver_lote(id_)
        return self._cobros

    def facturado_usd(self, precio_entrada_mtok: float, precio_salida_mtok: float, lote: Optional[bool] = None) -> float:
        total = 0.0
        for c in self.cobros:
            if lote is not None and (c.ruta == "lote") != lote:
                continue
            descuento = 0.5 if c.ruta == "lote" else 1.0
            total += descuento * (c.entrada * precio_entrada_mtok + c.salida * precio_salida_mtok) / 1_000_000
        return total

    def cobros_de(self, pregunta: str) -> list[Cobro]:
        return [c for c in self.cobros if c.pregunta == sin_marca(pregunta)]

    def veces_facturada(self, pregunta: str) -> int:
        return len(self.cobros_de(pregunta))

    def peticiones_a(self, ruta: str, metodo: str = "POST") -> list[dict[str, Any]]:
        return [p for p in self.peticiones if p["ruta"] == ruta and p["metodo"] == metodo]

    # ------------------------------------------------------------------
    # Respuestas
    # ------------------------------------------------------------------

    @staticmethod
    def texto_de_respuesta(pregunta: str) -> str:
        limpia = sin_marca(pregunta)
        return f"Respuesta a: {limpia[:60]}"

    @staticmethod
    def tokens_de_entrada(cuerpo: dict[str, Any]) -> int:
        texto = ""
        for m in cuerpo.get("messages") or []:
            contenido = m.get("content")
            texto += contenido if isinstance(contenido, str) else json.dumps(contenido)
        sistema = cuerpo.get("system")
        if sistema:
            texto += sistema if isinstance(sistema, str) else json.dumps(sistema)
        return max(1, len(texto) // 4)

    def mensaje(self, pregunta: str, entrada: int, salida: int, stop: str = "end_turn", contenido: Optional[str] = "", usage: Any = None) -> dict[str, Any]:
        texto = self.texto_de_respuesta(pregunta) if contenido == "" else contenido
        return {
            "id": f"msg_{self.azar.randrange(10**8):08d}", "type": "message", "role": "assistant", "model": MODELO,
            "content": [{"type": "text", "text": texto}] if texto else [],
            "stop_reason": stop, "stop_sequence": None,
            "usage": usage if usage is not None else {"input_tokens": entrada, "output_tokens": salida},
            **({"stop_details": {"type": "refusal", "category": "cyber", "explanation": "simulada"}} if stop == "refusal" else {}),
        }

    def siguiente_paso(self, pregunta: str, ruta: str) -> tuple[str, str]:
        pasos = parsear_plan(pregunta)
        clave = f"{ruta}|{pregunta}"
        with self._lock:
            n = self._intentos.get(clave, 0)
            self._intentos[clave] = n + 1
        if not pasos:
            return "ok", ""
        return pasos[min(n, len(pasos) - 1)]

    def superada_la_cuota(self) -> bool:
        if not self.cuota_por_minuto:
            return False
        ahora = time.monotonic()
        with self._lock:
            self._ventana = [t for t in self._ventana if ahora - t < 60]
            if len(self._ventana) >= self.cuota_por_minuto:
                return True
            self._ventana.append(ahora)
        return False

    def registrar_cobro(self, cobro: Cobro) -> None:
        with self._lock:
            self._cobros.append(cobro)

    # ------------------------------------------------------------------
    # Lotes
    # ------------------------------------------------------------------

    def crear_lote(self, cuerpo: dict[str, Any]) -> dict[str, Any]:
        peticiones = cuerpo.get("requests") or []
        with self._lock:
            self._numero_lote += 1
            id_ = f"msgbatch_{self._numero_lote:04d}"
            self.lotes[id_] = {
                "peticiones": peticiones, "creado": time.monotonic(), "cancelado": False,
                "resultados": None, "fin": None, "creado_utc": datetime.now(timezone.utc) + self.desfase_reloj,
            }
        return self.vista_del_lote(id_)

    def _resolver_lote(self, id_: str) -> None:
        """Calcula los resultados la primera vez que se consulta un lote ya terminado."""
        lote = self.lotes[id_]
        with self._lock:
            if lote["resultados"] is not None:
                return
            resultados = []
            for p in lote["peticiones"]:
                params = p.get("params") or {}
                pregunta = ""
                mensajes = params.get("messages") or [{}]
                contenido = mensajes[-1].get("content", "")
                pregunta = contenido if isinstance(contenido, str) else json.dumps(contenido)
                paso, arg = (parsear_plan(pregunta) or [("ok", "")])[0]
                entrada = self.tokens_de_entrada(params)
                cid = p.get("custom_id")
                if lote["cancelado"] and paso not in ("errored", "expired"):
                    resultados.append({"custom_id": cid, "result": {"type": "canceled"}})
                elif paso == "errored":
                    resultados.append({"custom_id": cid, "result": {"type": "errored", "error": {
                        "type": "error", "error": {"type": "invalid_request_error", "message": "simulado"}, "request_id": None}}})
                elif paso == "expired":
                    resultados.append({"custom_id": cid, "result": {"type": "expired"}})
                elif paso == "canceled":
                    resultados.append({"custom_id": cid, "result": {"type": "canceled"}})
                else:
                    salida = int(arg) if arg.isdigit() else 20
                    stop = {"refusal": "refusal", "maxtokens": "max_tokens"}.get(paso, "end_turn")
                    self._cobros.append(Cobro(sin_marca(pregunta), entrada, salida, True, "lote"))
                    resultados.append({"custom_id": cid, "result": {
                        "type": "succeeded", "message": self.mensaje(pregunta, entrada, salida, stop)}})
            self.azar.shuffle(resultados)  # llegan sin orden
            lote["resultados"] = resultados
            lote["fin"] = time.monotonic()

    def vista_del_lote(self, id_: str) -> dict[str, Any]:
        lote = self.lotes[id_]
        terminado = lote["cancelado"] or time.monotonic() - lote["creado"] >= self.retraso_lote
        if terminado:
            self._resolver_lote(id_)
        n = len(lote["peticiones"])
        contador = {"processing": n, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}
        if terminado:
            contador["processing"] = 0
            for r in lote["resultados"]:
                contador[{"succeeded": "succeeded", "errored": "errored", "canceled": "canceled", "expired": "expired"}[r["result"]["type"]]] += 1
        creado = lote["creado_utc"]
        iso = lambda f: f.strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # noqa: E731
        ahora = iso(datetime.now(timezone.utc) + self.desfase_reloj)
        return {
            "id": id_, "type": "message_batch",
            "processing_status": "ended" if terminado else ("canceling" if lote["cancelado"] else "in_progress"),
            "request_counts": contador, "ended_at": ahora if terminado else None, "created_at": iso(creado),
            "expires_at": iso(creado + timedelta(days=1)), "cancel_initiated_at": ahora if lote["cancelado"] else None,
            "results_url": f"{self.url}/v1/messages/batches/{id_}/results" if terminado else None,
        }

    def listar_lotes(self, limite: int = 20, despues_de: str = "") -> dict[str, Any]:
        """Como `GET /v1/messages/batches`: del más nuevo al más viejo, con paginación por `after_id`."""
        with self._lock:
            ids = sorted(self.lotes, key=lambda i: self.lotes[i]["creado_utc"], reverse=True)
        if despues_de and despues_de in ids:
            ids = ids[ids.index(despues_de) + 1:]
        pagina = ids[:limite]
        datos = [self.vista_del_lote(i) for i in pagina]
        return {"data": datos, "has_more": len(ids) > limite, "first_id": pagina[0] if pagina else None,
                "last_id": pagina[-1] if pagina else None}

    def sembrar_lote(self, ordenes: int, hace: float = 0.0, pregunta: str = "ajena") -> str:
        """Crea un lote «de otra persona» con `ordenes` órdenes, como si se hubiera creado hace `hace` segundos."""
        peticiones = [
            {"custom_id": f"x{i:06d}", "params": {"model": MODELO, "max_tokens": 10,
                                                  "messages": [{"role": "user", "content": f"{pregunta} {i}"}]}}
            for i in range(ordenes)
        ]
        vista = self.crear_lote({"requests": peticiones})
        with self._lock:
            self.lotes[vista["id"]]["creado_utc"] -= timedelta(seconds=hace)
        return vista["id"]

    def cancelar_lote(self, id_: str) -> dict[str, Any]:
        with self._lock:
            self.lotes[id_]["cancelado"] = True
            self.cancelados.append(id_)
        return self.vista_del_lote(id_)


def _flujo(servidor: ServidorFalso, pregunta: str, entrada: int, salida: int, stop: str = "end_turn") -> list[tuple[str, dict]]:
    texto = servidor.texto_de_respuesta(pregunta)
    trozos = [texto[i:i + 12] for i in range(0, len(texto), 12)] or [""]
    eventos: list[tuple[str, dict]] = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_stream", "type": "message", "role": "assistant", "model": MODELO, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": entrada, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ]
    eventos += [("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}}) for t in trozos]
    eventos += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": salida}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return eventos


class _Manejador(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # silencio
        pass

    @property
    def api(self) -> ServidorFalso:
        return self.server.api  # type: ignore[attr-defined]

    # -- utilidades ------------------------------------------------------

    def _cuerpo(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self, estado: int, objeto: Any, cabeceras: Optional[dict[str, str]] = None, crudo: Optional[bytes] = None) -> None:
        datos = crudo if crudo is not None else json.dumps(objeto).encode()
        self.send_response(estado)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        for k, v in (cabeceras or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(datos)

    def _error(self, estado: int, tipo: str, mensaje: str, cabeceras: Optional[dict[str, str]] = None, detalles: Optional[dict] = None) -> None:
        cuerpo: dict[str, Any] = {"type": "error", "error": {"type": tipo, "message": mensaje}, "request_id": "req_simulada"}
        if detalles:
            cuerpo["error"]["details"] = detalles
        self._json(estado, cuerpo, cabeceras)

    def _cortar(self, con_reset: bool = False) -> None:
        self.close_connection = True
        try:
            if con_reset:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            else:
                self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass

    # -- despacho --------------------------------------------------------

    def do_GET(self) -> None:
        self._despachar("GET")

    def do_POST(self) -> None:
        self._despachar("POST")

    def _despachar(self, metodo: str) -> None:
        api = self.api
        crudo = self._cuerpo()
        ruta = self.path.split("?")[0]
        consulta = parse_qs(urlsplit(self.path).query)
        try:
            cuerpo = json.loads(crudo) if crudo else None
        except ValueError:
            cuerpo = None
        with api._lock:
            api.peticiones.append({"metodo": metodo, "ruta": ruta, "cuerpo": cuerpo, "cabeceras": {k.lower(): v for k, v in self.headers.items()}})
        if self.headers.get("x-api-key") != api.clave:
            self._error(401, "authentication_error", "invalid x-api-key")
            return
        try:
            if ruta == "/v1/messages" and metodo == "POST":
                self._mensajes(cuerpo or {})
            elif ruta == "/v1/messages/count_tokens" and metodo == "POST":
                self._json(200, {"input_tokens": api.tokens_de_entrada(cuerpo or {})})
            elif ruta == "/v1/models" and metodo == "GET":
                ids = [MODELO, "claude-haiku-4-5", "claude-opus-5-5"]
                self._json(200, {"data": [{"id": i, "type": "model", "display_name": i, "created_at": "2026-01-01T00:00:00Z"} for i in ids],
                                 "has_more": False, "first_id": ids[0], "last_id": ids[-1]})
            elif ruta == "/v1/messages/batches" and metodo == "POST":
                self._crear_lote(cuerpo or {})
            elif ruta == "/v1/messages/batches" and metodo == "GET":
                if self._fallo_de_consulta():
                    return
                limite = int((consulta.get("limit") or ["20"])[0])
                self._json(200, api.listar_lotes(limite, (consulta.get("after_id") or [""])[0]))
            elif ruta.startswith("/v1/messages/batches/"):
                self._lote(ruta, metodo)
            else:
                self._error(404, "not_found_error", f"ruta desconocida: {ruta}")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # -- /v1/messages ------------------------------------------------------

    def _mensajes(self, cuerpo: dict[str, Any]) -> None:
        api = self.api
        mensajes = cuerpo.get("messages") or [{}]
        contenido = mensajes[-1].get("content", "")
        pregunta = contenido if isinstance(contenido, str) else json.dumps(contenido)
        limpia = sin_marca(pregunta)
        entrada = api.tokens_de_entrada(cuerpo)
        stream = bool(cuerpo.get("stream"))
        paso, arg = api.siguiente_paso(pregunta, "mensajes")

        if api.superada_la_cuota():
            self._error(429, "rate_limit_error", "Number of request tokens has exceeded your per-minute rate limit", {"retry-after": "1"})
            return

        simples = {
            "429tope": (429, "rate_limit_error", "Your organization has reached its monthly API usage threshold. You will regain access on 2026-11-01 at 00:00 UTC.",
                        {}, {"error_code": "enforced_spend_limit_reached"}),
            "529": (529, "overloaded_error", "Overloaded", {}, None),
            "500": (500, "api_error", "Internal server error", {}, None),
            "502": (502, "api_error", "Bad gateway", {}, None),
            "503": (503, "api_error", "Service unavailable", {}, None),
            "504": (504, "timeout_error", "Request timed out", {}, None),
            "408": (408, "timeout_error", "Request timeout", {}, None),
            "409": (409, "api_error", "Conflict", {}, None),
            "400saldo": (400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.", {}, None),
            "400limite": (400, "invalid_request_error", "You have reached your specified API usage limits. You will regain access on 2026-11-01 at 00:00 UTC.", {}, None),
            "401": (401, "authentication_error", "invalid x-api-key", {}, None),
            "403": (403, "permission_error", "Your API key does not have permission to use the specified resource.", {}, None),
            "404modelo": (404, "not_found_error", "model: claude-inexistente", {}, None),
            "413": (413, "request_too_large", "Request exceeds the maximum allowed number of bytes.", {}, None),
            "422": (422, "invalid_request_error", "Unprocessable", {}, None),
        }
        if paso == "429":
            self._error(429, "rate_limit_error", "Number of request tokens has exceeded your per-minute rate limit", {"retry-after": arg or "1"})
            return
        if paso == "429sin":
            self._error(429, "rate_limit_error", "rate limited")
            return
        if paso == "retry_after":
            self._error(429, "rate_limit_error", "rate limited", {"retry-after": arg})
            return
        if paso in simples:
            estado, tipo, texto, cabeceras, detalles = simples[paso]
            self._error(estado, tipo, texto, cabeceras, detalles)
            return
        if paso == "cierre":
            self._cortar()
            return
        if paso == "reset":
            self._cortar(con_reset=True)
            return
        if paso == "basura":
            self._json(200, None, crudo=b"<html><body>Bad gateway page</body></html>")
            return

        # A partir de aquí la petición se procesa (y se factura, salvo que se diga lo contrario)
        salida = int(arg) if paso == "ok" and arg.isdigit() else 20
        if paso == "lento":
            time.sleep(float(arg or 1))
        stop = {"refusal": "refusal", "maxtokens": "max_tokens"}.get(paso, "end_turn")
        contenido_respuesta: Optional[str] = "" if paso != "vacio" else None
        usage = None
        if paso == "uso_raro":
            usage = {"input_tokens": None, "output_tokens": "7", "cache_read_input_tokens": -3}
            entrada, salida = 0, 7   # lo que el programa puede leer de ese `usage`: es lo que se factura
        elif paso == "uso_enorme":
            usage = {"input_tokens": 10**12, "output_tokens": 10**12}
            entrada, salida = 10**12, 10**12

        if paso == "perdida":
            api.registrar_cobro(Cobro(limpia, entrada, salida, False, "mensajes"))
            self._cortar()
            return
        if paso == "corte" and not stream:
            datos = json.dumps(api.mensaje(pregunta, entrada, salida)).encode()
            api.registrar_cobro(Cobro(limpia, entrada, salida, False, "mensajes"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos[: len(datos) // 2])
            self.wfile.flush()
            self._cortar()
            return

        if stream:
            if paso == "flujo_vacio":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
            self._emitir_stream(
                pregunta, limpia, entrada, salida, stop,
                cortar=paso in ("corte", "corte_stream", "flujo_trunco"), limpio=paso == "flujo_trunco")
            return

        cuerpo_resp = api.mensaje(pregunta, entrada, salida, stop, contenido_respuesta, usage)
        api.registrar_cobro(Cobro(limpia, entrada, salida, True, "mensajes"))
        cabeceras = {
            "anthropic-ratelimit-requests-limit": "1000", "anthropic-ratelimit-requests-remaining": "999",
            "anthropic-ratelimit-requests-reset": "2026-10-12T20:01:00Z",
            "anthropic-ratelimit-tokens-limit": "1000000", "anthropic-ratelimit-tokens-remaining": "999000",
            "anthropic-ratelimit-tokens-reset": "2026-10-12T20:01:00Z",
        }
        self._json(200, cuerpo_resp, cabeceras)

    def _emitir_stream(
        self, pregunta: str, limpia: str, entrada: int, salida: int, stop: str, cortar: bool, limpio: bool = False
    ) -> None:
        api = self.api
        eventos = _flujo(api, pregunta, entrada, salida, stop)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def trozo(datos: bytes) -> None:
            self.wfile.write(f"{len(datos):x}\r\n".encode() + datos + b"\r\n")
            self.wfile.flush()

        hasta = 4 if cortar else len(eventos)
        for nombre, datos in eventos[:hasta]:
            trozo(f"event: {nombre}\ndata: {json.dumps(datos)}\n\n".encode())
        if cortar:
            api.registrar_cobro(Cobro(limpia, entrada, salida, False, "mensajes"))
            if limpio:   # el cuerpo se cierra correctamente (sin error de HTTP), pero el mensaje no llegó a su final
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                self._cortar()
            return
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        api.registrar_cobro(Cobro(limpia, entrada, salida, True, "mensajes"))

    # -- lotes -------------------------------------------------------------

    def _fallo_de_consulta(self) -> bool:
        """Aplica el siguiente paso de `guion_consulta` a un GET de solo lectura. True si ya respondió (mal)."""
        api = self.api
        with api._lock:
            texto = api.guion_consulta.pop(0) if api.guion_consulta else "ok"
        m = PASO.fullmatch(texto)
        paso, arg = (m.group(1), m.group(2) or "") if m else ("ok", "")
        if paso in ("500", "529"):
            self._error(int(paso), "overloaded_error" if paso == "529" else "api_error", "simulado")
            return True
        if paso == "basura":
            self._json(200, None, crudo=b"<html><body>Bad gateway page</body></html>")
            return True
        if paso in ("cierre", "reset"):
            self._cortar(con_reset=paso == "reset")
            return True
        if paso == "corte":          # promete un cuerpo entero, entrega la mitad y cierra
            datos = json.dumps({"relleno": "x" * 400}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos[: len(datos) // 2])
            self.wfile.flush()
            self._cortar()
            return True
        if paso == "lento":          # tarda S segundos y luego responde bien (si el cliente aún espera)
            time.sleep(float(arg or 1))
        return False

    def _crear_lote(self, cuerpo: dict[str, Any]) -> None:
        api = self.api
        with api._lock:
            paso = api.guion_crear.pop(0) if api.guion_crear else "ok"
        if paso == "cierre":          # se cierra la conexión sin crear nada
            self._cortar()
            return
        if paso in ("500", "502", "529"):
            self._error(int(paso), "overloaded_error" if paso == "529" else "api_error", "simulado")
            return
        if paso == "400":
            self._error(400, "invalid_request_error", "requests: simulado")
            return
        for _ in range(api.lotes_ajenos_al_crear):
            api.sembrar_lote(len(cuerpo.get("requests") or []))
        vista = api.crear_lote(cuerpo)
        if paso == "perdida":         # se crea (y se cobrará) pero la respuesta no llega nunca
            self._cortar()
            return
        if paso == "creado500":       # se crea, pero una pasarela intermedia contesta 500 en lugar de la respuesta
            self._error(500, "api_error", "simulado")
            return
        if api.lote_creado_con_retraso:
            time.sleep(api.lote_creado_con_retraso)
        try:
            self._json(200, vista)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _lote(self, ruta: str, metodo: str) -> None:
        api = self.api
        partes = ruta.split("/")  # ['', 'v1', 'messages', 'batches', ID, ('results'|'cancel')?]
        id_ = partes[4] if len(partes) > 4 else ""
        if id_ not in api.lotes:
            self._error(404, "not_found_error", f"batch not found: {id_}")
            return
        accion = partes[5] if len(partes) > 5 else ""
        if accion == "" and metodo == "GET":
            if self._fallo_de_consulta():
                return
            self._json(200, api.vista_del_lote(id_))
        elif accion == "cancel" and metodo == "POST":
            self._json(200, api.cancelar_lote(id_))
        elif accion == "results" and metodo == "GET":
            api.vista_del_lote(id_)
            lote = api.lotes[id_]
            if lote["resultados"] is None:
                self._error(409, "invalid_request_error", "batch not ended")
                return
            with api._lock:
                paso = api.guion_resultados.pop(0) if api.guion_resultados else "ok"
            if paso == "500":
                self._error(500, "api_error", "simulado")
                return
            if paso == "basura":
                self._json(200, None, crudo=b"<html><body>Bad gateway page</body></html>")
                return
            datos = "".join(json.dumps(r) + "\n" for r in lote["resultados"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-jsonl")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            if paso == "corte":          # la mitad del cuerpo y se cierra la conexión
                self.wfile.write(datos[: len(datos) // 2])
                self.wfile.flush()
                self._cortar()
                return
            self.wfile.write(datos)
        else:
            self._error(404, "not_found_error", f"ruta de lote desconocida: {ruta}")
