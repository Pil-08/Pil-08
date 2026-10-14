"""Lo que comparten los simulacros: el servidor falso, cómo correr un lote y qué debe cumplirse siempre.

Los simulacros ejecutan el programa de verdad (motores, cola, terminal) contra `ServidorFalso` con el SDK real
de Anthropic por HTTP. Después de cada uno se comprueban unas invariantes:

  1. El tope de gasto NUNCA se supera, mirando lo que el servidor habría FACTURADO (no lo que anota el programa).
  2. Nada se factura dos veces, salvo que la persona vuelva a preguntar.
  3. Lo que anota el programa coincide con lo que facturó el servidor (sin deriva).
  4. Todo lo pagado y completado está guardado en disco, y lo guardado es lo que dijo el servidor.
  5. El motor termina (no se cuelga).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal as D
from typing import Any, Callable, Optional

import pytest

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core.base import Callbacks, StopReason
from claude_batch_monitor.core.batch import BatchEngine
from claude_batch_monitor.core.plan import PlanEngine
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.job import STATUS_COMPLETED, STATUS_PENDING, STATUS_REFUSED, Job

from .servidor_falso import ServidorFalso, sin_marca

PRECIO_ENTRADA = 3.0   # dólares por millón de tokens
PRECIO_SALIDA = 15.0


def configuracion(**kw: Any) -> Configuracion:
    base_ = dict(
        modelo="claude-sonnet-5-5", max_tokens=200, tope_gasto_usd=D("5"),
        precio_input_mtok=D(str(PRECIO_ENTRADA)), precio_output_mtok=D(str(PRECIO_SALIDA)),
        max_error_attempts=4, timeout_api_sec=15,
    )
    base_.update(kw)
    return Configuracion(**base_)


@dataclass
class Corrida:
    job: Job
    motor: Any
    razon: Optional[StopReason]
    errores: list[str] = field(default_factory=list)
    estados: list[str] = field(default_factory=list)
    config: Optional[Configuracion] = None

    def estado_de(self, n: int) -> str:
        return self.job.mensaje(n).status

    @property
    def completadas(self) -> int:
        return sum(1 for m in self.job.messages if m.status in (STATUS_COMPLETED, STATUS_REFUSED))

    @property
    def pendientes(self) -> int:
        return sum(1 for m in self.job.messages if m.status == STATUS_PENDING)


def correr(
    preguntas: list[str],
    config: Optional[Configuracion] = None,
    job: Optional[Job] = None,
    antes: Optional[Callable[[Any], None]] = None,
    limite_s: float = 40.0,
    callbacks: Optional[Callbacks] = None,
) -> Corrida:
    """Ejecuta un lote con el motor que toque. Si el motor no termina en `limite_s`, la prueba falla."""
    config = config or configuracion()
    if job is None:
        job = Job.crear(preguntas, config)
        job.save()
    clase = PlanEngine if config.es_plan else (BatchEngine if config.es_lote else SequentialEngine)
    motor = clase(config, job)
    motor.notificaciones = False
    if isinstance(motor, BatchEngine):
        motor.intervalo_min = motor.intervalo_max = 0.05
    corrida = Corrida(job, motor, None, config=config)
    cb = callbacks or Callbacks()
    cb.error = cb.error or corrida.errores.append
    cb.estado = cb.estado or (lambda texto, ocupado: corrida.estados.append(texto))
    if antes:
        antes(motor)
    hilo = threading.Thread(target=lambda: setattr(corrida, "razon", motor.ejecutar(cb)), daemon=True)
    hilo.start()
    hilo.join(timeout=limite_s)
    assert not hilo.is_alive(), f"el motor no terminó en {limite_s} s (¿se colgó?). Estados: {corrida.estados[-3:]}"
    return corrida


def comprobar_invariantes(
    api: ServidorFalso, c: Corrida, duplicados_permitidos: tuple[str, ...] = (), con_tope: bool = True,
    respuestas_perdidas: bool = False,
) -> None:
    """`respuestas_perdidas`: hubo cortes de conexión tras procesar la petición. El programa no puede contar lo que
    nunca recibió, así que el servidor puede haber facturado hasta eso por encima del tope (y lo avisa al parar)."""
    config = c.config or c.motor.config
    facturado = api.facturado_usd(PRECIO_ENTRADA, PRECIO_SALIDA)

    # 1. el tope nunca se supera (visto desde el servidor)
    if con_tope and config.tope_gasto_usd > 0:
        perdido = 0.0
        if respuestas_perdidas:
            perdido = sum(
                (x.entrada * PRECIO_ENTRADA + x.salida * PRECIO_SALIDA) / 1_000_000 for x in api.cobros if not x.completa)
        assert facturado <= float(config.tope_gasto_usd) + perdido + 1e-9, (
            f"el servidor habría facturado ${facturado:.6f} y el tope era ${config.tope_gasto_usd} (+${perdido:.6f} perdido)")

    # 2. nada se factura dos veces
    vistas: dict[str, int] = {}
    for cobro in api.cobros:
        vistas[cobro.pregunta] = vistas.get(cobro.pregunta, 0) + 1
    repetidas = {p: n for p, n in vistas.items() if n > 1 and p not in {sin_marca(x) for x in duplicados_permitidos}}
    assert not repetidas, f"preguntas facturadas más de una vez: {repetidas}"

    # 3. la contabilidad del programa coincide con la del servidor para lo que se completó
    del_programa = sum((m.coste_usd for m in c.job.messages), D(0))
    completadas = {sin_marca(m.text) for m in c.job.messages if m.status in (STATUS_COMPLETED, STATUS_REFUSED)}
    del_servidor = 0.0
    for cobro in api.cobros:
        if cobro.completa and cobro.pregunta in completadas:
            descuento = 0.5 if cobro.ruta == "lote" else 1.0
            del_servidor += descuento * (cobro.entrada * PRECIO_ENTRADA + cobro.salida * PRECIO_SALIDA) / 1_000_000
    assert float(del_programa) == pytest.approx(del_servidor, rel=1e-6, abs=1e-9), (
        f"el programa anota ${del_programa} y el servidor facturó ${del_servidor:.9f} por lo completado")

    # 4. lo completado está guardado en disco
    for m in c.job.messages:
        ruta = c.job.response_path(m.id, c.job.ciclos_completados + 1)
        if m.status in (STATUS_COMPLETED, STATUS_REFUSED):
            assert ruta.exists(), f"falta el archivo de la orden {m.id} (completada)"
            assert sin_marca(m.text) in ruta.read_text(encoding="utf-8")
