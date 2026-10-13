"""Caos con semillas: escenarios al azar (pero repetibles) contra el servidor falso, y las mismas invariantes de siempre.

Cada semilla fabrica un lote con fallos de todo tipo repartidos al azar, lo ejecuta, lo reanuda mientras haga falta y
comprueba que, pase lo que pase: el tope no se supera, nada se factura dos veces (salvo tras un corte de conexión,
que es justo lo que se avisa), la contabilidad cuadra con la del servidor y lo pagado está guardado en disco.

Si falla una semilla, se reproduce con `pytest tests/test_simulacros_caos.py -k "semilla-17"`.
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal as D

import pytest

from claude_batch_monitor.core.base import StopReason

from .simulacros_comun import comprobar_invariantes, configuracion, correr

pytestmark = pytest.mark.simulacro

# (paso del plan, peso). Los que no terminan en respuesta correcta se combinan con un `ok` final casi siempre.
PASOS = [
    ("ok", 40), ("ok(5)", 8), ("ok(120)", 4), ("429(1)", 7), ("529", 5), ("500", 4), ("503", 3), ("408", 2), ("409", 1),
    ("refusal", 3), ("maxtokens", 3), ("vacio", 2), ("uso_raro", 2), ("basura", 2), ("413", 2),
    ("cierre", 3), ("reset", 2), ("corte", 3), ("perdida", 3),
    ("401", 0.5), ("400saldo", 0.5), ("429sin", 0.5), ("404modelo", 0.5),
]
SOLO_TRANSITORIOS = [
    "ok", "ok(5)", "ok(120)", "429(1)", "529", "500", "503", "408", "409", "refusal", "maxtokens", "vacio", "uso_raro", "basura",
]
CON_RESPUESTA_PERDIDA = {"corte", "corte_stream", "perdida"}
CONEXION_CORTADA = {"cierre", "reset", "corte", "perdida"}


def plan_al_azar(azar: random.Random, pasos: list[str], siempre_termina_bien: bool = False) -> str:
    n = azar.choice([0, 0, 1, 1, 2, 3])
    elegidos = azar.choices(pasos, weights=[dict(PASOS).get(p, 1) for p in pasos], k=n)
    if siempre_termina_bien or azar.random() < 0.92:
        elegidos.append("ok")
    return f" [[plan:{','.join(elegidos)}]]" if elegidos else ""


def pasos_de(texto: str) -> set[str]:
    if "[[plan:" not in texto:
        return set()
    return {p.split("(")[0] for p in texto.split("[[plan:")[1].split("]]")[0].split(",")}


def escenario(semilla: int, pasos: list[str], siempre_termina_bien: bool = False):
    azar = random.Random(semilla)
    n = azar.randint(3, 9)
    preguntas = [
        f"Pregunta {i} de la semilla {semilla}" + plan_al_azar(azar, pasos, siempre_termina_bien) for i in range(1, n + 1)
    ]
    tope = azar.choice([D(0), D(5), D(5), D("0.004"), D("0.01")])
    config = configuracion(
        tope_gasto_usd=tope, concurrencia=azar.choice([1, 1, 2, 4]), streaming=azar.random() < 0.25,
        max_tokens=azar.choice([50, 200, 1000]),
    )
    return preguntas, config


def reanudar_hasta_el_final(preguntas, config, api, maximo=8):
    """Ejecuta y reanuda como lo haría una persona. Devuelve las corridas hechas."""
    corridas = []
    job = None
    sin_avance = 0
    for _ in range(maximo):
        antes = len(api.peticiones)
        c = correr([] if job else preguntas, config, job=job, limite_s=60)
        corridas.append(c)
        job = c.job
        if c.razon is StopReason.COMPLETED or not job.hay_trabajo():
            break
        sin_avance = sin_avance + 1 if len(api.peticiones) == antes else 0
        if sin_avance >= 2 or c.razon in (StopReason.NO_CREDIT, StopReason.ACCOUNT_LIMIT, StopReason.BUDGET):
            break
    return corridas


@pytest.mark.parametrize("semilla", range(40), ids=lambda s: f"semilla-{s}")
def test_inmediato_con_fallos_de_todo_tipo(api, esperas, semilla):
    preguntas, config = escenario(semilla, [p for p, _ in PASOS])
    corridas = reanudar_hasta_el_final(preguntas, config, api)
    todos_los_pasos = set().union(*(pasos_de(q) for q in preguntas))
    duplicados = tuple(q for q in preguntas if pasos_de(q) & (CON_RESPUESTA_PERDIDA | CONEXION_CORTADA))
    ultima = corridas[-1]
    comprobar_invariantes(
        api, ultima, duplicados_permitidos=duplicados,
        respuestas_perdidas=bool(todos_los_pasos & CON_RESPUESTA_PERDIDA),
    )
    # Cada corrida terminó con un motivo claro y sin hilos colgados (correr() ya falla si el motor no termina).
    assert all(c.razon is not None for c in corridas)


@pytest.mark.parametrize("semilla", range(40, 70), ids=lambda s: f"semilla-{s}")
def test_con_solo_fallos_transitorios_siempre_se_completa_a_la_primera(api, esperas, semilla):
    preguntas, config = escenario(semilla, SOLO_TRANSITORIOS, siempre_termina_bien=True)
    config = configuracion(  # sin tope que lo corte: aquí se prueba que la espera y el reintento bastan
        tope_gasto_usd=D(0), concurrencia=config.concurrencia, streaming=config.streaming, max_tokens=config.max_tokens,
        max_error_attempts=6,
    )
    c = correr(preguntas, config, limite_s=60)
    assert c.razon is StopReason.COMPLETED, (c.razon, c.errores, [m.status for m in c.job.messages])
    assert c.pendientes == 0
    comprobar_invariantes(api, c)


@pytest.mark.parametrize("semilla", range(70, 100), ids=lambda s: f"semilla-{s}")
def test_lote_con_fallos_al_crearlo_y_resultados_de_todo_tipo(api, esperas, semilla):
    azar = random.Random(semilla)
    n = azar.randint(2, 8)
    primeros = [("ok", 60), ("ok(5)", 10), ("refusal", 6), ("maxtokens", 6), ("errored", 6), ("expired", 6), ("canceled", 6)]
    preguntas = []
    for i in range(1, n + 1):
        paso = azar.choices([p for p, _ in primeros], weights=[w for _, w in primeros])[0]
        preguntas.append(f"Pregunta {i} del lote de la semilla {semilla}" + (f" [[plan:{paso}]]" if paso != "ok" else ""))
    api.guion_crear = azar.choices(["ok", "500", "529", "creado500", "perdida", "cierre"], weights=[5, 2, 1, 2, 2, 2], k=azar.randint(0, 3))
    api.retraso_lote = azar.choice([0.0, 0.1, 0.3])
    api.desfase_reloj = timedelta(minutes=azar.choice([0, 0, 0, 3, -3]))
    config = configuracion(modo="lote", tope_gasto_usd=D(5), max_tokens=200)

    corridas = reanudar_hasta_el_final(preguntas, config, api, maximo=10)
    c = corridas[-1]
    assert c.razon is StopReason.COMPLETED, (c.razon, c.errores, api.guion_crear)
    assert len(api.lotes) == 1, "un envío, un lote: nunca dos"
    assert c.job.batch_id == "" and c.job.envio_incierto_desde == ""
    comprobar_invariantes(api, c)


def test_las_semillas_son_repetibles():
    a = escenario(7, [p for p, _ in PASOS])
    b = escenario(7, [p for p, _ in PASOS])
    assert a[0] == b[0] and a[1].tope_gasto_usd == b[1].tope_gasto_usd
