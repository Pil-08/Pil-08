"""Motor del plan de Claude: cada orden se responde con Claude Code (`claude -p`), con el cupo de tu plan.

Para quien tiene un plan Pro o Max de Claude y no quiere pagar por token: el programa oficial `claude`, ya conectado a
tu cuenta, responde cada pregunta. Aquí no hay precios ni tope de gasto en dólares: lo que se gasta es el cupo del
plan, que se renueva solo (una ventana de 5 horas y otra semanal). Cuando se agota, Claude Code lo dice y cuándo
vuelve; este motor espera hasta entonces y sigue solo, sin perder ni repetir ninguna respuesta.

Reglas que protegen de gastar de más (lo que la persona pidió: «dentro del plan, no con los créditos extra»):
- Se lanza `claude` sin ninguna clave de API en el entorno y se comprueba que no use una (ver `claude_code.py`).
- Si el plan se agota y Claude Code empieza a gastar créditos extra (`isUsingOverage`), se para en el acto.
- Un límite alcanzado se recuerda en disco: si se cierra y se vuelve a abrir antes del reinicio, no se insiste.
"""

from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Optional

from ..config import Configuracion
from ..job import Job, JobMessage
from ..paths import logger, write_atomic
from ..pricing import Precios
from .base import Callbacks, Contadores, Decision, EngineBase, StopReason
from .claude_code import (
    FIN,
    MARGEN_TRAS_EL_REINICIO_SEC,
    Ejecucion,
    EstadoPlan,
    LectorDeSalida,
    Veredicto,
    argumentos_de_claude,
    carpeta_de_trabajo,
    comprobar_claude,
    entorno_para_el_plan,
    texto_del_sistema,
)

PRECIOS_DEL_PLAN = Precios(Decimal(0), Decimal(0), Decimal(0), "manual", "plan")
ESPERA_MINIMA_SEC = 30.0
MINIMO_TIMEOUT_SEC = 120.0  # Claude Code reintenta por su cuenta y puede tardar: nunca se le da menos que esto
# El tiempo de espera cuenta desde la ÚLTIMA línea que llegó, no desde que empezó: una respuesta larga que va llegando
# no se mata a medias (se perdería lo gastado). Por si algo gotea para siempre, hay también un tope en total.
TOPE_TOTAL_SEC = 3600.0
# Si Claude Code dice que se alcanzó el límite pero no cuándo vuelve: se reintenta con esperas crecientes.
ESPERAS_SIN_REINICIO_SEC = (300.0, 600.0, 900.0, 1800.0)
MAX_ESPERAS_SIN_REINICIO = 16          # unas 7 horas
MAX_ESPERAS_SIN_REINICIO_SEMANAL = 340  # unos 7 días a razón de media hora
MINIMO_DE_ESPERA_TRAS_VARIOS_RECHAZOS_SEC = 1800.0
NOMBRE_DE_LA_VENTANA = {
    "sesion": "la sesión de 5 horas",
    "semana": "la semana",
    "opus": "el cupo semanal de Opus",
    "sonnet": "el cupo semanal de Sonnet",
    "otro": "el cupo",
}
OPCIONES_QUE_SE_PUEDEN_QUITAR = (
    "--include-partial-messages", "--no-session-persistence", "--tools", "--disable-slash-commands", "--safe-mode",
    "--system-prompt-file",
)


def _duracion(segundos: float) -> str:
    segundos = max(0, int(round(segundos)))
    horas, resto = divmod(segundos, 3600)
    minutos = resto // 60
    if horas:
        return f"{horas} h {minutos:02d} min"
    if minutos:
        return f"{minutos} min"
    return f"{segundos} s"


def _respuesta_de(v: Veredicto) -> Any:
    """Lo que `registrar_respuesta` espera de una respuesta (es la forma de la de la API de mensajes)."""
    uso = v.uso or {}

    def n(clave: str) -> int:
        valor = uso.get(clave)
        return valor if isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0 else 0

    return SimpleNamespace(
        content=[SimpleNamespace(text=v.texto)],
        usage=SimpleNamespace(
            input_tokens=n("input_tokens"), output_tokens=n("output_tokens"),
            cache_read_input_tokens=n("cache_read_input_tokens"),
            cache_creation_input_tokens=n("cache_creation_input_tokens"),
        ),
        stop_reason=v.stop_reason or "end_turn",
    )


class PlanEngine(EngineBase):
    """Responde las órdenes pendientes una a una con Claude Code, esperando a que el plan se renueve si hace falta."""

    def __init__(self, config: Configuracion, job: Job):
        super().__init__(config, job)
        self.precios = PRECIOS_DEL_PLAN  # con el plan no se paga por token: todo cuesta 0
        self.plan = EstadoPlan()
        self._ruta = ""
        self._sin: set[str] = set()                  # opciones que esta versión de Claude Code no entiende
        self._archivo_sistema: Optional[Any] = None
        self._ejecucion: Optional[Ejecucion] = None
        self._lock_ejecucion = threading.Lock()
        self._esperas_sin_reinicio = 0
        self._rechazos_seguidos = 0
        self.problema = ""  # por qué se paró, en una palabra (la ventana redacta su propio aviso): ver `_abortar`

    # ------------------------------------------------------------------
    # Bucle principal
    # ------------------------------------------------------------------

    def ejecutar(self, cb: Optional[Callbacks] = None) -> StopReason:
        cb = cb or Callbacks()
        self.deadline = self.calcular_deadline()
        ciclo = self.job.ciclos_completados + 1
        logger.info("Lote %s iniciado con el plan de Claude: %d órdenes", self.job.job_id, self.job.total_count())
        try:
            if self._preparar(cb):
                self._recorrer(ciclo, cb)
        finally:
            self._matar_ejecucion(esperar=True)
            self.cerrar_ciclo(ciclo, cb)
        return self._finalizar()

    def inicializar_cliente(self) -> None:  # no hay cliente de la API
        return None

    def detener(self) -> None:
        super().detener()
        self._matar_ejecucion(esperar=False)  # no bloquea a quien pulsa «Detener»: el hilo del motor termina el trabajo

    def _preparar(self, cb: Callbacks) -> bool:
        estado = comprobar_claude(self.config.ruta_claude)
        if not estado.listo:
            self._abortar(cb, estado.explicacion(), problema=estado.estado)
            return False
        self._ruta = estado.ruta
        self._escribir_sistema()
        bloqueado = self._bloqueo_recordado()
        if bloqueado is not None:
            self.plan.bloqueado_hasta = bloqueado
        return True

    def _recorrer(self, ciclo: int, cb: Callbacks) -> None:
        vistas: set[int] = set()
        while not self._debe_parar():
            pendientes = [m for m in self.job.pendientes() if m.id not in vistas]
            if not pendientes:
                return
            msg = pendientes[0]
            vistas.add(msg.id)
            if not self._procesar_mensaje(msg, ciclo, cb):
                return
            if self.job.pendientes() and not self._pausa_entre_ordenes(cb):
                return

    def _pausa_entre_ordenes(self, cb: Callbacks) -> bool:
        d = self.config.pausa_entre_mensajes_sec
        if d <= 0:
            return True
        cb.emit("estado", f"Pausa de cortesía ({d}s)...", False)
        return self.esperar(d, cb)

    # ------------------------------------------------------------------
    # Una orden
    # ------------------------------------------------------------------

    def _procesar_mensaje(self, msg: JobMessage, ciclo: int, cb: Callbacks) -> bool:
        """Procesa UNA orden hasta terminarla. False si hay que parar todo."""
        total = self.job.total_count()
        cont = Contadores()
        while True:
            if self._debe_parar() or not self._esperar_franja(cb) or not self._esperar_si_bloqueado(cb):
                return False
            cb.emit("inicio_orden", msg)
            cb.emit("estado", f"[Ciclo {ciclo} | Orden {msg.id}/{total}] Preguntando a Claude Code…", True)
            veredicto, ms = self._preguntar(msg, cb)
            if veredicto is None:
                return False
            self._anotar_plan(cb)
            decision = self._atender(veredicto, msg, ciclo, cb, cont, ms)
            if decision is Decision.PARAR:
                return False
            if decision is Decision.SIGUIENTE:
                return True

    def _atender(
        self, v: Veredicto, msg: JobMessage, ciclo: int, cb: Callbacks, cont: Contadores, ms: int
    ) -> Decision:
        """Qué hacer con lo que pasó. REINTENTAR = volver a preguntar la misma orden."""
        if v.tipo == "ok":
            self._esperas_sin_reinicio = self._rechazos_seguidos = 0
            self._desbloquear()
            if not self.registrar_respuesta(msg, _respuesta_de(v), ciclo, cb, ms, comprobar_tope=False):
                return Decision.PARAR
            if v.usando_extra:
                self._parar_por_uso_extra(cb)
                return Decision.PARAR
            return Decision.SIGUIENTE

        if v.tipo == "limite":
            return Decision.REINTENTAR if self._esperar_al_reinicio(v, cb) else Decision.PARAR

        if v.tipo == "creditos":
            self._abortar(
                cb,
                "Tu plan se ha quedado sin uso incluido y Claude Code pide créditos de pago para seguir. Este "
                "programa no los gasta. Cuando el plan se renueve, pulsa «Reanudar».",
                StopReason.NO_CREDIT, "creditos",
            )
            return Decision.PARAR

        if v.tipo == "gasto":
            self._abortar(
                cb,
                "Has llegado a un límite de gasto de tu cuenta de Claude. Súbelo en https://claude.ai/settings/usage "
                f"o espera a que se renueve, y pulsa «Reanudar». (Claude Code dijo: «{v.texto}»)",
                StopReason.ACCOUNT_LIMIT, "gasto",
            )
            return Decision.PARAR

        if v.tipo in ("auth", "cuenta"):
            if v.tipo == "auth":
                texto = ("Claude Code no tiene la sesión iniciada, o ha caducado. En una terminal ejecuta:  "
                         "claude auth login  y luego pulsa «Reanudar».")
            else:
                texto = ("Tu cuenta de Claude no puede usar Claude Code ahora (en espera, suspendida o sin permiso para "
                         f"tu organización). Claude Code dijo: «{v.texto}»")
            self._abortar(cb, texto, problema=v.tipo)
            return Decision.PARAR

        if v.tipo == "modelo":
            elegido = self.config.modelo_plan or "el predeterminado"
            detalle = (f"Tu plan no permite el modelo «{elegido}» o no existe. Elige otro en Opciones "
                       f"(o déjalo en blanco para usar el de tu plan). Claude Code dijo: «{v.texto}»")
            self.marcar_fallo(msg, detalle, cb, "model_not_found")
            self._abortar(cb, detalle, problema="modelo")
            return Decision.PARAR

        if v.tipo == "invalido":
            detalle = f"Claude Code rechazó esta pregunta: {v.texto}"
            self.marcar_fallo(msg, detalle, cb, "bad_request")
            cb.emit("error", detalle)
            return Decision.SIGUIENTE

        if v.tipo == "opcion":
            if v.opcion in OPCIONES_QUE_SE_PUEDEN_QUITAR and v.opcion not in self._sin:
                logger.warning("Esta versión de Claude Code no entiende %s: se prueba sin ella", v.opcion)
                self._sin.add(v.opcion)
                return Decision.REINTENTAR
            self._abortar(cb, "Esta versión de Claude Code no es compatible con el programa. Actualízala con:  "
                              f"claude update  (no entiende la opción {v.opcion}).", problema="antiguo")
            return Decision.PARAR

        que = "Claude Code no pudo responder ahora" if v.tipo == "saturado" else "Claude Code falló"
        decision = self._reintentar_con_backoff(msg, RuntimeError(v.texto), cb, cont, f"{que} ({v.texto[:120]})")
        return decision

    # ------------------------------------------------------------------
    # Lanzar Claude Code
    # ------------------------------------------------------------------

    def _escribir_sistema(self) -> None:
        """Las instrucciones van en un archivo (`--system-prompt-file`): nada de texto de la persona en la línea de órdenes."""
        ruta = carpeta_de_trabajo() / "instrucciones.txt"
        try:
            write_atomic(ruta, texto_del_sistema(self.config.system_prompt))
        except OSError:
            logger.exception("No se pudo escribir el archivo de instrucciones")
            self._sin.add("--system-prompt-file")
            return
        self._archivo_sistema = ruta

    def _entrada(self, msg: JobMessage) -> str:
        texto = msg.text
        if "--system-prompt-file" in self._sin or self._archivo_sistema is None:
            instrucciones = (self.config.system_prompt or "").strip()
            if instrucciones:  # sin archivo de sistema, las instrucciones viajan con la pregunta
                texto = f"[Instructions for this answer: {instrucciones}]\n\n{texto}"
        if "--disable-slash-commands" in self._sin and texto.lstrip().startswith("/"):
            texto = f"Pregunta: {texto}"  # que no se tome por un comando de Claude Code
        return texto

    def _preguntar(self, msg: JobMessage, cb: Callbacks) -> tuple[Optional[Veredicto], int]:
        """Lanza `claude -p` con la pregunta y espera su desenlace. (None, 0) si hay que parar todo."""
        orden = argumentos_de_claude(
            self._ruta, self.config.modelo_plan, None if "--system-prompt-file" in self._sin else self._archivo_sistema,
            self._sin,
        )
        try:
            ejecucion = Ejecucion(orden, self._entrada(msg), entorno_para_el_plan(), carpeta_de_trabajo())
        except OSError as error:
            self._abortar(cb, f"No se pudo ejecutar Claude Code ({self._ruta}): {error}", problema="error")
            return None, 0
        with self._lock_ejecucion:
            self._ejecucion = ejecucion
        lector = LectorDeSalida()
        inicio = ultima = time.monotonic()
        limite = max(float(self.config.timeout_api_sec), MINIMO_TIMEOUT_SEC)
        comprobado = False
        agotado = False
        try:
            while True:
                if self.evento_detener.is_set():  # la persona pulsó «Detener» (la hora límite espera a que acabe esta)
                    ejecucion.terminar()
                    return None, 0
                ahora = time.monotonic()
                if ahora - ultima > limite or ahora - inicio > max(TOPE_TOTAL_SEC, limite):
                    agotado = True
                    ejecucion.terminar()
                    break
                linea = ejecucion.siguiente(0.25)
                if linea is None:
                    continue
                ultima = time.monotonic()
                if linea is FIN:
                    break
                nuevo = lector.alimentar(linea)
                if nuevo:
                    cb.emit("delta", msg.id, nuevo)
                if lector.inicio and not comprobado:
                    comprobado = True
                    if lector.usa_clave_de_api():  # antes de que se envíe nada: se corta
                        ejecucion.terminar()
                        self._abortar(
                            cb,
                            "Claude Code dice que está usando una clave de API, no tu plan, y eso se paga por token. "
                            "No se ha enviado nada. Quita la clave (variable ANTHROPIC_API_KEY o «apiKeyHelper» de "
                            "Claude Code) o inicia sesión con tu cuenta:  claude auth login",
                            problema="clave_de_api",
                        )
                        return None, 0
            codigo = ejecucion.esperar_codigo(15)
            if codigo is None:
                ejecucion.terminar()
            if self.evento_detener.is_set():
                return None, 0
            ms = int((time.monotonic() - inicio) * 1000)
            if agotado:
                return Veredicto("saturado", f"Claude Code no dio señales de vida en {int(limite)} s"), ms
            self._mezclar_plan(lector.plan)
            return lector.veredicto(ejecucion.codigo, ejecucion.stderr), ms
        finally:
            ejecucion.terminar()
            ejecucion.cerrar()
            with self._lock_ejecucion:
                if self._ejecucion is ejecucion:
                    self._ejecucion = None

    def _matar_ejecucion(self, esperar: bool) -> None:
        with self._lock_ejecucion:
            ejecucion = self._ejecucion
        if ejecucion is None:
            return
        if esperar:
            ejecucion.terminar()
        else:
            ejecucion.pedir_que_termine()

    # ------------------------------------------------------------------
    # El cupo del plan
    # ------------------------------------------------------------------

    def _mezclar_plan(self, nuevo: EstadoPlan) -> None:
        """Lo leído en esta pregunta se suma a lo ya sabido (lo que no se dijo, no se borra)."""
        actual = self.plan
        for propia, ajena in ((actual.sesion, nuevo.sesion), (actual.semana, nuevo.semana)):
            if ajena.uso is not None:
                propia.uso = ajena.uso
            if ajena.reinicia is not None:
                propia.reinicia = ajena.reinicia
        if nuevo.situacion:
            actual.situacion = nuevo.situacion
        if nuevo.extra_activado is not None:
            actual.extra_activado = nuevo.extra_activado
        actual.usando_extra = actual.usando_extra or nuevo.usando_extra
        if nuevo.bloqueado_hasta is not None:
            actual.bloqueado_hasta = nuevo.bloqueado_hasta

    def _anotar_plan(self, cb: Callbacks) -> None:
        if self.plan.conocido():
            cb.emit("plan", copy.deepcopy(self.plan))

    def _esperar_al_reinicio(self, v: Veredicto, cb: Callbacks) -> bool:
        """El plan dijo que no hay más por ahora: espera a que se renueve. False si se interrumpió o se rindió."""
        ahora = datetime.now(timezone.utc)
        self._rechazos_seguidos += 1
        reinicia = v.reinicia
        if reinicia is not None:
            hasta = reinicia + timedelta(seconds=MARGEN_TRAS_EL_REINICIO_SEC)
            if hasta <= ahora:  # ya debería haber vuelto y sigue sin dejar: no se insiste cada medio minuto
                extra = min(ESPERA_MINIMA_SEC * 2 ** (self._rechazos_seguidos - 1), MINIMO_DE_ESPERA_TRAS_VARIOS_RECHAZOS_SEC)
                hasta = ahora + timedelta(seconds=extra)
        else:
            limite = MAX_ESPERAS_SIN_REINICIO_SEMANAL if v.ventana == "semana" else MAX_ESPERAS_SIN_REINICIO
            if self._esperas_sin_reinicio >= limite:
                self._abortar(
                    cb,
                    "Claude Code dice que se alcanzó el límite de tu plan pero no cuándo vuelve, y tras esperar mucho "
                    f"sigue igual. Revisa tu uso en https://claude.ai/settings/usage. (Dijo: «{v.texto}»)",
                    problema="limite_sin_hora",
                )
                return False
            pasos = ESPERAS_SIN_REINICIO_SEC
            hasta = ahora + timedelta(seconds=pasos[min(self._esperas_sin_reinicio, len(pasos) - 1)])
            self._esperas_sin_reinicio += 1
        self._bloquear(hasta)
        return self._esperar_plan(hasta, v.ventana or "otro", cb, v.reinicia is not None)

    def _esperar_si_bloqueado(self, cb: Callbacks) -> bool:
        hasta = self.plan.bloqueado_hasta
        if hasta is None or hasta <= datetime.now(timezone.utc):
            return True
        return self._esperar_plan(hasta, "", cb, True)

    def _esperar_plan(self, hasta: datetime, ventana: str, cb: Callbacks, exacta: bool) -> bool:
        segundos = (hasta - datetime.now(timezone.utc)).total_seconds()
        local = hasta.astimezone()
        cual = NOMBRE_DE_LA_VENTANA.get(ventana, "")
        dia = "" if local.date() == datetime.now().astimezone().date() else f" del {local:%d/%m}"
        cuando = f"a las {local:%H:%M}{dia}" if exacta else f"en unos {_duracion(segundos)}"
        texto = (f"Has llegado al límite de tu plan{f' ({cual})' if cual else ''}. "
                 f"Sigo solo {cuando} (faltan {_duracion(segundos)}).")
        logger.info(texto)
        cb.emit("espera", "plan", max(0.0, segundos))
        cb.emit("estado", texto, False)
        self._avisar("Límite del plan alcanzado", texto)
        if not self._esperar_hasta(hasta, cb):
            return False
        self._desbloquear()
        return True

    def _bloquear(self, hasta: datetime) -> None:
        self.plan.bloqueado_hasta = hasta
        with self._lock_estado:
            self.estado.plan_bloqueado_hasta = hasta.astimezone(timezone.utc).isoformat()
            self.estado.guardar()

    def _desbloquear(self) -> None:
        self.plan.bloqueado_hasta = None
        with self._lock_estado:
            if self.estado.plan_bloqueado_hasta:
                self.estado.plan_bloqueado_hasta = ""
                self.estado.guardar()

    def _bloqueo_recordado(self) -> Optional[datetime]:
        """Un límite alcanzado en una ejecución anterior que todavía no ha terminado."""
        texto = self.estado.plan_bloqueado_hasta
        if not texto:
            return None
        try:
            hasta = datetime.fromisoformat(texto)
        except ValueError:
            return None
        if hasta.tzinfo is None:
            hasta = hasta.replace(tzinfo=timezone.utc)
        return hasta if hasta > datetime.now(timezone.utc) else None

    # ------------------------------------------------------------------
    # Paradas
    # ------------------------------------------------------------------

    def _abortar(self, cb: Callbacks, texto: str, razon: StopReason = StopReason.FATAL, problema: str = "") -> None:
        logger.error(texto)
        self.problema = problema
        cb.emit("error", texto)
        self._avisar("Cola detenida", texto)
        self.marcar_parada(razon)

    def _parar_por_uso_extra(self, cb: Callbacks) -> None:
        self._abortar(
            cb,
            "Tu plan se ha agotado y Claude Code ha empezado a gastar créditos extra (de pago) con la última pregunta "
            "(ya está respondida y guardada). Se ha detenido todo para no gastar más. Si no quieres que Claude use "
            "créditos extra, desactívalos en https://claude.ai/settings/usage. Cuando el plan se renueve, pulsa «Reanudar».",
            StopReason.EXTRA_USAGE, "uso_extra",
        )
