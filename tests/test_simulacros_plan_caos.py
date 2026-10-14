"""Caos con semillas, con el plan de Claude: cada pregunta recibe, al azar (pero repetible), una tanda de fallos.

El `claude` de mentira responde a cada pregunta con los pasos que lleva escritos (`[[plan:saturado,limite(1),ok]]`).
Cada semilla fabrica un lote, lo ejecuta y lo reanuda mientras haga falta, y comprueba que pase lo que pase:

- el motor termina siempre (no se cuelga) y no deja ningún `claude` vivo;
- una pregunta solo se vuelve a hacer si algo falló, y nunca más veces de las que el guion le da;
- lo que queda como respondido tiene su respuesta en disco y es la de SU pregunta;
- no hay dinero por medio: nada de coste, ni acumulado, ni en cada orden;
- con fallos pasajeros, todas las preguntas acaban resueltas (respondidas, rechazadas por el modelo o falladas por inválidas).

Si falla una semilla, se reproduce con `pytest tests/test_simulacros_plan_caos.py -k "semilla-17"`.
"""

from __future__ import annotations

import random

import pytest

from claude_batch_monitor.core import plan as modulo_plan
from claude_batch_monitor.core.base import StopReason
from claude_batch_monitor.job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REFUSED,
    Job,
)
from claude_batch_monitor.state import EstadoMonitor

from .simulacros_comun import configuracion, correr
from .test_simulacros_plan import vivo

pytestmark = pytest.mark.simulacro

# Cada pregunta lleva un guion: unos pasos que se repiten (límites y fallos de servidor, que se vuelven a pedir) y un
# paso final que la cierra (respondida, rechazada por el modelo o inválida).
LIMITES = ["limite(1)", "semanal(1)"]                       # el plan dice «no hay más hasta dentro de 1 s»: se espera y se repite
SERVIDOR = ["saturado", "error500", "basura", "crash", "sin_resultado"]   # fallos que cuentan hacia el máximo de errores
FINALES = [("ok", 40), ("ok(5)", 8), ("lento(0.3)", 4), ("vacio", 2), ("trunca", 2), ("grande(30)", 1), ("rechazo", 3), ("invalido", 2)]
SEMILLAS = range(30)


def nombre(paso: str) -> str:
    return paso.split("(")[0]


def guion_al_azar(azar: random.Random) -> list[str]:
    antes = azar.choices(LIMITES * 2 + SERVIDOR * 2, k=azar.choice([0, 0, 1, 1, 2, 3]))
    final = azar.choices([f for f, _ in FINALES], weights=[w for _, w in FINALES])[0]
    return [*antes, final]


def escenario(semilla: int):
    azar = random.Random(semilla)
    guiones = [guion_al_azar(azar) for _ in range(azar.randint(3, 8))]
    preguntas = [f"Pregunta {i} de la semilla {semilla} [[plan:{','.join(g)}]]" for i, g in enumerate(guiones, start=1)]
    config = configuracion(
        modo="plan", modelo_plan=azar.choice(["", "", "sonnet", "opus"]), max_error_attempts=azar.choice([2, 3, 4, 6]),
        pausa_entre_mensajes_sec=azar.choice([0, 0, 0, 1]), streaming=azar.random() < 0.5,
    )
    return preguntas, guiones, config


def esperado(guion: list[str], maximo_de_errores: int) -> tuple[str, int]:
    """Cómo debe acabar una pregunta con este guion y cuántas veces se le habrá preguntado a Claude Code."""
    errores = 0
    for vistas, paso in enumerate(guion, start=1):
        if nombre(paso) in (nombre(x) for x in SERVIDOR):
            errores += 1
            if errores >= maximo_de_errores:
                return STATUS_FAILED, vistas           # demasiados fallos seguidos: queda para «Reintentar fallidas»
        elif nombre(paso) == "invalido":
            return STATUS_FAILED, vistas
        elif nombre(paso) == "rechazo":
            return STATUS_REFUSED, vistas
        elif nombre(paso) not in (nombre(x) for x in LIMITES):
            return STATUS_COMPLETED, vistas
    raise AssertionError(f"un guion sin final: {guion}")


def sin_marca(texto: str) -> str:
    return texto.split(" [[plan:")[0]


@pytest.mark.parametrize("semilla", SEMILLAS, ids=lambda s: f"semilla-{s}")
def test_con_fallos_al_azar_el_plan_acaba_bien_y_sin_dejar_nada_roto(semilla, claude, esperas, monkeypatch):
    monkeypatch.setattr(modulo_plan, "MARGEN_TRAS_EL_REINICIO_SEC", 0.0)
    monkeypatch.setattr(modulo_plan, "ESPERA_MINIMA_SEC", 0.05)
    preguntas, guiones, config = escenario(semilla)
    job = Job.crear(preguntas, config)
    job.save()

    razones = []
    for _ronda in range(12):                                  # el programa se reanuda mientras quede algo
        corrida = correr([], config, job=job, limite_s=90)
        razones.append(corrida.razon)
        assert corrida.razon in (StopReason.COMPLETED, StopReason.FATAL, StopReason.STOPPED), razones
        if not job.pendientes():
            break
    assert not job.pendientes(), f"tras {len(razones)} rondas siguen pendientes: {[m.id for m in job.pendientes()]} ({razones})"

    for m, guion in zip(job.messages, guiones):
        pregunta = sin_marca(m.text)
        estado, veces = esperado(guion, config.max_error_attempts)
        assert m.status == estado, (m.id, guion, m.status, m.detalle)
        assert claude.veces(pregunta) == veces, (m.id, guion, claude.veces(pregunta))   # ni una vez más de las necesarias
        assert m.coste_usd == 0
        if m.status in (STATUS_COMPLETED, STATUS_REFUSED):
            texto = job.response_path(m.id, job.ciclos_completados + 1).read_text(encoding="utf-8")
            assert (f"Respuesta a: {pregunta}"[:60] in texto) or nombre(guion[-1]) in ("vacio", "grande") or "vacía" in texto, (
                m.id, guion, texto[:80])
        else:
            assert m.detalle                                                           # y dice por qué falló

    # Nada de dinero por medio, ni en memoria ni en disco.
    assert EstadoMonitor.cargar().gasto_acumulado_usd == 0
    # Y ningún `claude` se quedó vivo.
    pids = {x["pid"] for x in claude.llamadas()}
    assert not any(vivo(p) for p in pids), [p for p in pids if vivo(p)]


@pytest.mark.parametrize("semilla", range(5), ids=lambda s: f"semilla-{s}")
def test_con_una_parada_a_media_tanda_tambien(semilla, claude, esperas, monkeypatch):
    """Se para el motor al azar (como Pausar o Ctrl+C) en mitad de la tanda y se reanuda: nada se pierde ni se repite de más."""
    import threading
    import time

    monkeypatch.setattr(modulo_plan, "MARGEN_TRAS_EL_REINICIO_SEC", 0.0)
    azar = random.Random(1000 + semilla)
    preguntas = [f"Pregunta {i} de la parada {semilla} [[plan:lento(0.4),ok]]" for i in range(1, 6)]
    config = configuracion(modo="plan")
    job = Job.crear(preguntas, config)
    job.save()

    def parar_pronto(motor):
        def detener():
            time.sleep(azar.uniform(0.2, 1.4))
            motor.detener()

        threading.Thread(target=detener, daemon=True).start()

    primera = correr([], config, job=job, antes=parar_pronto, limite_s=60)
    hechas = sum(1 for m in job.messages if m.status == STATUS_COMPLETED)
    assert primera.razon in (StopReason.STOPPED, StopReason.COMPLETED)
    segunda = correr([], config, job=job, limite_s=60)
    assert segunda.razon is StopReason.COMPLETED and not job.pendientes()
    assert all(m.status == STATUS_COMPLETED for m in job.messages)
    assert claude.veces("Pregunta") >= len(preguntas)
    for m in job.messages[:hechas]:                                  # lo que ya estaba respondido no se volvió a preguntar
        assert claude.veces(sin_marca(m.text)) == 1, (m.id, claude.veces(sin_marca(m.text)))
    assert not any(vivo(x["pid"]) for x in claude.llamadas())
    assert STATUS_PENDING not in {m.status for m in job.messages}
