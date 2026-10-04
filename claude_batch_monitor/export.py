"""Exportación de resultados a CSV, JSONL, Markdown y texto."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

from .job import Job, JobMessage

FORMATOS = ("csv", "jsonl", "md", "txt")
MARCA_RESPUESTA = "====== RESPUESTA ======"
MARCA_SOLICITUD = "====== SOLICITUD ======"


class ErrorDeExportacion(ValueError):
    pass


def formato_por_extension(destino: Path) -> str:
    ext = destino.suffix.lower().lstrip(".")
    if ext == "markdown":
        ext = "md"
    if ext not in FORMATOS:
        raise ErrorDeExportacion(
            f"No sé exportar a '.{ext}'. Usa una de estas extensiones: "
            + ", ".join(f".{f}" for f in FORMATOS)
        )
    return ext


def leer_respuesta(job: Job, msg_id: int, ciclo: int) -> str:
    """Texto de la respuesta guardada de una orden ('' si no hay)."""
    ruta = job.response_path(msg_id, ciclo)
    try:
        contenido = ruta.read_text(encoding="utf-8")
    except OSError:
        return ""
    _, _, respuesta = contenido.partition(MARCA_RESPUESTA)
    return respuesta.strip()


def _texto_estado(m: JobMessage) -> str:
    return m.status if not m.detalle else f"{m.status}: {m.detalle}"


def exportar(
    job: Job,
    destino: Path,
    formato: Optional[str] = None,
    ciclo: Optional[int] = None,
) -> Path:
    """Escribe los resultados del lote en `destino` y devuelve su ruta."""
    destino = Path(destino)
    formato = (formato or formato_por_extension(destino)).lower()
    if formato not in FORMATOS:
        raise ErrorDeExportacion(f"Formato desconocido '{formato}'. Usa: {', '.join(FORMATOS)}.")
    ciclo = ciclo or (job.ciclos_completados + 1)
    destino.parent.mkdir(parents=True, exist_ok=True)

    if formato == "csv":
        _csv(job, destino, ciclo)
    elif formato == "jsonl":
        _jsonl(job, destino, ciclo)
    elif formato == "md":
        _md(job, destino, ciclo)
    else:
        _txt(job, destino, ciclo)
    return destino


def _csv(job: Job, destino: Path, ciclo: int) -> None:
    # utf-8-sig para que Excel en Windows detecte el UTF-8 y no rompa las tildes.
    with open(destino, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "id",
                "estado",
                "tokens_entrada",
                "tokens_salida",
                "coste_usd",
                "latencia_ms",
                "truncada",
                "error_type",
                "detalle",
                "solicitud",
                "respuesta",
            ]
        )
        for msg in job.messages:
            writer.writerow(
                [
                    msg.id,
                    msg.status,
                    msg.tokens_input,
                    msg.tokens_output,
                    f"{msg.coste_usd:.6f}",
                    msg.latency_ms,
                    "sí" if msg.truncated else "no",
                    msg.error_type,
                    msg.detalle,
                    msg.text,
                    leer_respuesta(job, msg.id, ciclo),
                ]
            )


def _jsonl(job: Job, destino: Path, ciclo: int) -> None:
    with open(destino, "w", encoding="utf-8", newline="\n") as handle:
        for msg in job.messages:
            registro = {
                "id": msg.id,
                "estado": msg.status,
                "modelo": job.modelo,
                "tokens_entrada": msg.tokens_input,
                "tokens_salida": msg.tokens_output,
                "coste_usd": str(msg.coste_usd),
                "latencia_ms": msg.latency_ms,
                "truncada": msg.truncated,
                "error_type": msg.error_type,
                "detalle": msg.detalle,
                "solicitud": msg.text,
                "respuesta": leer_respuesta(job, msg.id, ciclo),
            }
            handle.write(json.dumps(registro, ensure_ascii=False) + "\n")


def _md(job: Job, destino: Path, ciclo: int) -> None:
    partes = [
        f"# Resultados del lote {job.job_id}",
        "",
        f"- Modelo: `{job.modelo}`",
        f"- Modo: {job.modo}",
        f"- Completadas: {job.completed_count()}/{job.total_count()}",
        f"- Coste: ${job.coste_total():.4f}",
        "",
    ]
    for msg in job.messages:
        partes += [f"## Orden {msg.id} — {_texto_estado(msg)}", "", "**Solicitud**", ""]
        partes += ["> " + linea for linea in msg.text.splitlines() or [""]]
        respuesta = leer_respuesta(job, msg.id, ciclo)
        if respuesta:
            partes += ["", "**Respuesta**", "", respuesta]
        partes += ["", "---", ""]
    destino.write_text("\n".join(partes), encoding="utf-8")


def _txt(job: Job, destino: Path, ciclo: int) -> None:
    bloques = []
    for msg in job.messages:
        respuesta = leer_respuesta(job, msg.id, ciclo)
        bloques.append(respuesta if respuesta else f"[orden {msg.id}: {_texto_estado(msg)}]")
    destino.write_text("\n\n---\n\n".join(bloques) + "\n", encoding="utf-8")
