"""El lote de trabajo: órdenes, estado por orden y persistencia en disco.

Cada lote es un `job_<id>.json` en el directorio de respuestas. Es lo que hace
que cerrar la aplicación a mitad no pierda nada: al volver a abrirla se reanuda
solo lo pendiente, sin repetir (ni volver a pagar) lo ya hecho.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from . import paths
from .config import a_decimal
from .parsing import DEFAULT_SEPARATOR, limpiar_sustitutos
from .paths import logger, write_atomic
from .pricing import normalizar_id

if TYPE_CHECKING:
    from .config import Configuracion

STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_REFUSED = "refused"  # el modelo se negó a responder (HTTP 200, pero se cobra)
STATUS_SKIPPED = "skipped"  # omitida a petición del usuario
TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_REFUSED, STATUS_SKIPPED}

_ID_JOB = re.compile(r"^job_([0-9a-f]+)(?:_|\.)")


@dataclass
class JobMessage:
    id: int
    text: str
    status: str = STATUS_PENDING
    errores: int = 0  # solo errores reales de servidor/red; los 429 NO cuentan
    truncated: bool = False
    tokens_input: int = 0  # entrada total, incluida la leída de caché
    tokens_output: int = 0
    tokens_cache_read: int = 0
    coste_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    detalle: str = ""
    error_type: str = ""  # categoría legible por máquina: refusal, auth, server…

    def __post_init__(self) -> None:
        self.text = limpiar_sustitutos(self.text)

    def a_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "status": self.status,
            "errores": self.errores,
            "truncated": self.truncated,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "tokens_cache_read": self.tokens_cache_read,
            "coste_usd": str(self.coste_usd),
            "latency_ms": self.latency_ms,
            "detalle": self.detalle,
            "error_type": self.error_type,
        }

    @classmethod
    def desde_dict(cls, d: dict) -> JobMessage:
        def entero(clave: str) -> int:
            try:
                return max(0, int(d.get(clave, 0)))
            except (TypeError, ValueError):
                return 0

        return cls(
            id=int(d["id"]),
            text=str(d["text"]),
            status=str(d.get("status", STATUS_PENDING)),
            # La v11 guardaba el contador como "attempts".
            errores=max(0, int(d.get("errores", d.get("attempts", 0)) or 0)),
            truncated=bool(d.get("truncated", False)),
            tokens_input=entero("tokens_input"),
            tokens_output=entero("tokens_output"),
            tokens_cache_read=entero("tokens_cache_read"),
            coste_usd=a_decimal(d.get("coste_usd"), Decimal(0)),
            latency_ms=entero("latency_ms"),
            detalle=str(d.get("detalle", "")),
            error_type=str(d.get("error_type", "")),
        )


class Job:
    def __init__(
        self,
        job_id: str,
        created_at: str,
        modelo: str,
        mensajes: Iterable[str],
        separador: str = DEFAULT_SEPARATOR,
        modo: str = "inmediato",
        system_prompt: str = "",
        batch_id: str = "",
        etiqueta: str = "",
    ):
        self.job_id = job_id
        self.created_at = created_at
        self.modelo = modelo
        self.separador = separador
        self.modo = modo
        self.system_prompt = system_prompt
        self.batch_id = batch_id  # id del lote en el servidor (modo lote)
        # Un envío de lote que pudo llegar al servidor sin que nos llegara su respuesta (tiempo agotado, corte, programa
        # cerrado a mitad): desde cuándo y cuántas órdenes. Mientras esté puesto, antes de enviar se busca ese lote.
        self.envio_incierto_desde = ""
        self.envio_incierto_ordenes = 0
        # El lote más reciente de la cuenta justo antes de intentarlo ("" = ninguno; None = no se pudo mirar): lo que
        # aparezca por delante en la lista de lotes lo creó el envío (o alguien más), no es anterior.
        self.envio_incierto_tras: Optional[str] = None
        # Respuestas ya pagadas que no se pudieron escribir en disco (disco lleno, sin permisos…): se guardan en
        # memoria para poder verlas, copiarlas o exportarlas mientras el programa siga abierto. No se persisten.
        self.respuestas_sin_guardar: dict[int, tuple[int, str]] = {}  # orden -> (ciclo, contenido del fichero)
        self.etiqueta = etiqueta  # "cola" = la cola del modo simple; "" = un lote normal
        self.ciclos_completados = 0
        self.messages: list[JobMessage] = [
            JobMessage(id=i + 1, text=m) for i, m in enumerate(mensajes)
        ]
        # Con concurrencia > 1 varios hilos guardan el mismo job; sin este cerrojo
        # dos `save()` podían mezclar dos instantes distintos en el mismo fichero.
        self._save_lock = threading.Lock()
        self._ids_lock = threading.Lock()

    @classmethod
    def crear(cls, mensajes: Iterable[str], config: Configuracion, etiqueta: str = "") -> Job:
        return cls(
            uuid.uuid4().hex[:8],
            datetime.now().isoformat(),
            config.nombre_de_modelo,  # con el plan, el alias elegido o «plan de Claude»
            mensajes,
            separador=config.separador,
            modo=config.modo,
            system_prompt=config.system_prompt,
            etiqueta=etiqueta,
        )

    # ---------- rutas ----------

    @property
    def file_path(self) -> Path:
        return paths.RESPONSES_DIR / f"job_{self.job_id}.json"

    def response_path(self, msg_id: int, ciclo: int) -> Path:
        return paths.RESPONSES_DIR / f"job_{self.job_id}_c{ciclo}_m{msg_id:03d}.txt"

    def resumen_path(self, ciclo: int) -> Path:
        return paths.RESPONSES_DIR / f"job_{self.job_id}_ciclo_{ciclo}_resumen.md"

    # ---------- persistencia ----------

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "created_at": self.created_at,
            "modelo": self.modelo,
            "separador": self.separador,
            "modo": self.modo,
            "system_prompt": self.system_prompt,
            "batch_id": self.batch_id,
            "envio_incierto_desde": self.envio_incierto_desde,
            "envio_incierto_ordenes": self.envio_incierto_ordenes,
            "envio_incierto_tras": self.envio_incierto_tras,
            "etiqueta": self.etiqueta,
            "ciclos_completados": self.ciclos_completados,
            "messages": [m.a_dict() for m in list(self.messages)],
        }

    def save(self) -> bool:
        """Escribe el lote en disco. False si no se pudo: quien gasta debe enterarse."""
        with self._save_lock:
            try:
                write_atomic(
                    self.file_path, json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
                )
            except Exception:
                logger.exception("No se pudo guardar el Job %s", self.job_id)
                return False
            return True

    @classmethod
    def load(cls, job_id: str) -> Optional[Job]:
        path = paths.RESPONSES_DIR / f"job_{job_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            job = cls(
                data["job_id"],
                data["created_at"],
                data["modelo"],
                [],
                separador=data.get("separador", DEFAULT_SEPARATOR),
                modo=data.get("modo", "inmediato"),
                system_prompt=data.get("system_prompt", ""),
                batch_id=data.get("batch_id", "") or "",
                etiqueta=data.get("etiqueta", "") or "",
            )
            job.ciclos_completados = int(data.get("ciclos_completados", 0))
            job.envio_incierto_desde = str(data.get("envio_incierto_desde", "") or "")
            job.envio_incierto_ordenes = max(0, int(data.get("envio_incierto_ordenes", 0) or 0))
            tras = data.get("envio_incierto_tras")
            job.envio_incierto_tras = None if tras is None else str(tras)
            job.messages = [JobMessage.desde_dict(m) for m in data["messages"]]
            return job
        except Exception:
            logger.exception("No se pudo cargar el Job %s", job_id)
            return None

    def agregar_orden(self, texto: str) -> JobMessage:
        """Añade una orden nueva al final (la cola del modo simple crece mientras se envía)."""
        with self._ids_lock:
            nuevo = JobMessage(id=max((m.id for m in self.messages), default=0) + 1, text=texto)
            self.messages.append(nuevo)
        self.save()
        return nuevo

    # ---------- consultas ----------

    def mensaje(self, msg_id: int) -> Optional[JobMessage]:
        for m in self.messages:
            if m.id == msg_id:
                return m
        return None

    def completed_count(self) -> int:
        return sum(1 for m in self.messages if m.status == STATUS_COMPLETED)

    def total_count(self) -> int:
        return len(self.messages)

    def pendientes(self) -> list[JobMessage]:
        """Órdenes que aún hay que enviar (las fallidas se reintentan a propósito)."""
        return [m for m in self.messages if m.status == STATUS_PENDING]

    def fallidas(self) -> list[JobMessage]:
        return [m for m in self.messages if m.status == STATUS_FAILED]

    def rechazadas(self) -> list[JobMessage]:
        return [m for m in self.messages if m.status == STATUS_REFUSED]

    def terminado(self) -> bool:
        return all(m.status in TERMINAL_STATUSES for m in self.messages)

    def hay_trabajo(self) -> bool:
        """True si queda algo por enviar o reintentar."""
        return any(m.status not in TERMINAL_STATUSES for m in self.messages)

    def coste_total(self) -> Decimal:
        return sum((m.coste_usd for m in self.messages), Decimal(0))

    def estadisticas(self) -> dict[str, Any]:
        return {
            "total": self.total_count(),
            "completadas": self.completed_count(),
            "pendientes": len(self.pendientes()),
            "fallidas": len(self.fallidas()),
            "rechazadas": len(self.rechazadas()),
            "coste_usd": self.coste_total(),
        }

    # ---------- transiciones ----------

    def reintentar_fallidas(self) -> int:
        """Devuelve a pendiente las órdenes fallidas. Devuelve cuántas eran."""
        n = 0
        for m in self.messages:
            if m.status == STATUS_FAILED:
                m.status = STATUS_PENDING
                m.errores = 0
                m.detalle = ""
                m.error_type = ""
                n += 1
        if n:
            self.save()
        return n

    def reset_for_new_cycle(self) -> None:
        for m in self.messages:
            m.status = STATUS_PENDING
            m.errores = 0
            m.truncated = False
            m.detalle = ""
            m.error_type = ""
        self.batch_id = ""
        self.envio_incierto_desde = ""
        self.envio_incierto_ordenes = 0
        self.envio_incierto_tras = None
        self.ciclos_completados += 1
        self.save()

    # ---------- reanudación segura ----------

    def incompatibilidades(self, config: Configuracion) -> list[str]:
        """Avisos si al reanudar la configuración actual difiere de la del lote.

        Mezclar en un mismo lote respuestas de dos modelos distintos, o reenviar
        por la API normal (al doble de precio) un lote creado en modo lote, suele
        ser un accidente. Se avisa; la decisión es del usuario.
        """
        avisos: list[str] = []
        if normalizar_id(config.nombre_de_modelo) != normalizar_id(self.modelo):
            avisos.append(
                f"El lote se creó con el modelo {self.modelo} y ahora está seleccionado "
                f"{config.nombre_de_modelo}: las órdenes pendientes se responderán con otro modelo."
            )
        if config.modo != self.modo:
            extra = ""
            if self.modo == "lote" and config.modo == "inmediato":
                extra = " Se reenviaría por la API normal, al doble de precio."
            elif self.modo == "plan":
                extra = " Se reenviaría por la API, que se paga por token, en vez de gastar el cupo de tu plan."
            avisos.append(
                f"El lote se creó en modo '{self.modo}' y ahora está en modo '{config.modo}'.{extra}"
            )
        if (config.system_prompt or "").strip() != (self.system_prompt or "").strip():
            avisos.append("El prompt de sistema ha cambiado desde que se creó el lote.")
        return avisos


# ----------------------------------------------------------------------
# Utilidades sobre el conjunto de lotes guardados
# ----------------------------------------------------------------------


def listar_jobs(etiqueta: Optional[str] = None) -> list[dict]:
    """Resumen de los lotes en disco, el más reciente primero.

    Con `etiqueta` solo los de esa etiqueta (`""` = los lotes normales, `"cola"` = la
    cola del modo simple); sin ella, todos.
    """
    salida: list[dict] = []
    for f in paths.RESPONSES_DIR.glob("job_*.json"):
        job = Job.load(f.stem[len("job_"):])
        if job is None or (etiqueta is not None and job.etiqueta != etiqueta):
            continue
        e = job.estadisticas()
        salida.append(
            {
                "job_id": job.job_id,
                "created_at": job.created_at,
                "modelo": job.modelo,
                "modo": job.modo,
                "batch_id": job.batch_id,
                "etiqueta": job.etiqueta,
                "completadas": e["completadas"],
                "total": e["total"],
                "fallidas": e["fallidas"],
                "coste_usd": e["coste_usd"],
                "incompleto": job.hay_trabajo(),
            }
        )
    salida.sort(key=lambda d: d["created_at"], reverse=True)
    return salida


def purgar_jobs_antiguos(
    dias: int, proteger: Optional[Iterable[str] | str] = None, conservar_pendientes: bool = True
) -> int:
    """Borra los ficheros de lotes más viejos que `dias` días. Devuelve cuántos.

    Nunca se tocan:
    - los lotes de `proteger` (ids), por viejos que sean: sin esto la purga se llevaba también los
      ficheros del lote en curso y un lote dejado a medias más días que la retención se volvía irreanudable;
    - con `conservar_pendientes` (por defecto), cualquier lote que aún tenga órdenes sin enviar,
      incluida la cola del modo simple: perder preguntas que nadie ha visto responder es peor que ocupar disco.
    """
    if dias <= 0:
        return 0
    if proteger is None:
        protegidos: set[str] = set()
    elif isinstance(proteger, str):
        protegidos = {proteger}
    else:
        protegidos = {str(p) for p in proteger}

    limite = time.time() - dias * 86400
    viejos: dict[str, list[Path]] = {}
    for f in paths.RESPONSES_DIR.glob("job_*"):
        m = _ID_JOB.match(f.name)
        if not m or m.group(1) in protegidos:
            continue
        try:
            if f.stat().st_mtime < limite:
                viejos.setdefault(m.group(1), []).append(f)
        except OSError:
            logger.warning("No se pudo examinar %s", f)

    borrados = 0
    for job_id, ficheros in viejos.items():
        if conservar_pendientes:
            job = Job.load(job_id)
            if job is not None and job.pendientes():
                continue
        for f in ficheros:
            try:
                f.unlink()
                borrados += 1
            except OSError:
                logger.warning("No se pudo borrar %s", f)
    return borrados
