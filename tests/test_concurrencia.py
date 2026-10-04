"""Concurrencia: escritura atómica, guardado de lotes y reserva de presupuesto.

Aquí viven los fallos que solo aparecen con varios hilos y que ningún test
secuencial puede ver (Y1, Y3) más la garantía de que N hilos no se pasan N veces
del tope.
"""

import json
import threading
from decimal import Decimal as D

from claude_batch_monitor import paths
from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core.base import Callbacks, StopReason
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.job import Job
from claude_batch_monitor.paths import write_atomic

from .test_engine import FakeResponse
from .test_integration import FakeClient


def en_paralelo(n, fn):
    """Lanza `fn(i)` en n hilos a la vez (barrera) y devuelve las excepciones que hubo."""
    barrera = threading.Barrier(n)
    fallos = []

    def trabajo(i):
        try:
            barrera.wait()
            fn(i)
        except Exception as e:  # noqa: BLE001 - se recogen todas para afirmar sobre ellas
            fallos.append(e)

    hilos = [threading.Thread(target=trabajo, args=(i,)) for i in range(n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)
    return fallos


class TestEscrituraAtomica:
    def test_Y1_doce_hilos_escribiendo_el_mismo_fichero_no_se_pisan(self):
        # Con un temporal de nombre fijo, un hilo hacía replace() del temporal que otro
        # aún escribía y el segundo reventaba con FileNotFoundError.
        destino = paths.RESPONSES_DIR / "compartido.json"
        fallos = en_paralelo(12, lambda i: [write_atomic(destino, json.dumps({"hilo": i, "x": "y" * 5000})) for _ in range(25)])
        assert fallos == []
        assert json.loads(destino.read_text(encoding="utf-8"))["x"] == "y" * 5000  # entero, no truncado
        assert list(paths.RESPONSES_DIR.glob("*.tmp")) == []

    def test_un_lector_nunca_ve_un_fichero_a_medias(self):
        destino = paths.RESPONSES_DIR / "vivo.json"
        write_atomic(destino, json.dumps({"n": 0}))
        parar = threading.Event()
        corruptos = []

        def lector():
            while not parar.is_set():
                try:
                    json.loads(destino.read_text(encoding="utf-8"))
                except Exception as e:  # noqa: BLE001
                    corruptos.append(e)

        t = threading.Thread(target=lector)
        t.start()
        en_paralelo(4, lambda i: [write_atomic(destino, json.dumps({"n": k, "relleno": "z" * 20000})) for k in range(40)])
        parar.set()
        t.join()
        assert corruptos == []

    def test_si_la_escritura_falla_no_quedan_temporales(self, monkeypatch):
        destino = paths.RESPONSES_DIR / "x.json"
        original = type(destino).replace

        def falla(self, objetivo):
            raise OSError("disco lleno")

        monkeypatch.setattr(type(destino), "replace", falla)
        try:
            write_atomic(destino, "contenido")
        except OSError:
            pass
        monkeypatch.setattr(type(destino), "replace", original)
        assert list(paths.RESPONSES_DIR.glob("*.tmp")) == []


class TestGuardadoDeLotes:
    def test_Y3_guardar_el_mismo_job_desde_muchos_hilos(self):
        job = Job.crear([f"o{i}" for i in range(50)], Configuracion())

        def guardar(i):
            for k in range(20):
                job.messages[(i + k) % 50].errores = k
                job.save()

        assert en_paralelo(12, guardar) == []
        recargado = Job.load(job.job_id)
        assert recargado is not None and recargado.total_count() == 50

    def test_cada_hilo_ve_siempre_un_job_valido_en_disco(self):
        job = Job.crear(["a", "b", "c"], Configuracion())
        job.save()
        invalidos = []

        def ciclo(i):
            for _ in range(30):
                job.save()
                if Job.load(job.job_id) is None:
                    invalidos.append(i)

        assert en_paralelo(8, ciclo) == [] and invalidos == []


def _motor(n, **kw):
    base = dict(
        modelo="claude-haiku-4-5", max_tokens=1024, concurrencia=4,
        precio_input_mtok=D("1"), precio_output_mtok=D("5"),
    )
    base.update(kw)
    config = Configuracion(**base)
    job = Job.crear([f"orden {i}" for i in range(1, n + 1)], config)
    job.save()
    motor = SequentialEngine(config, job)
    motor.notificaciones = False
    motor._client = FakeClient([FakeResponse()] * (n + 10))
    motor.inicializar_cliente = lambda *a, **k: None
    return motor, job


class TestMotorConcurrente:
    def test_cada_orden_se_envia_exactamente_una_vez(self):
        motor, job = _motor(40, tope_gasto_usd=D("100"), estimar_antes_de_enviar=False)
        assert motor.ejecutar(Callbacks()) == StopReason.COMPLETED
        assert motor._client.messages.llamadas == 40
        assert job.completed_count() == 40

    def test_los_callbacks_llegan_todos(self):
        motor, job = _motor(30, tope_gasto_usd=D("100"), estimar_antes_de_enviar=False)
        vistos, gastos = [], []
        motor.ejecutar(Callbacks(resultado=lambda m: vistos.append(m.id), gasto=lambda u, c: gastos.append(u)))
        assert sorted(vistos) == list(range(1, 31)) and len(gastos) == 30

    def test_el_gasto_final_es_la_suma_exacta(self):
        motor, job = _motor(25, tope_gasto_usd=D("100"), estimar_antes_de_enviar=False)
        motor.ejecutar(Callbacks())
        # Sin una sola actualización perdida por carrera: 25 x $0.0035.
        assert motor.estado.gasto_acumulado_usd == D("0.0875") == job.coste_total()

    def test_N_hilos_no_se_pasan_N_veces_del_tope(self):
        # Peor caso por orden ~ $0.0051; reales $0.0035. Con tope $0.02 caben pocas, y
        # con 4 hilos en vuelo SIN reserva se enviarían 4 de golpe y se pasaría.
        motor, job = _motor(30, tope_gasto_usd=D("0.02"), estimar_antes_de_enviar=True)
        assert motor.ejecutar(Callbacks()) == StopReason.BUDGET
        assert motor.estado.gasto_acumulado_usd <= D("0.02")
        assert 0 < job.completed_count() < 30
        assert motor._reservado == 0  # nada de reservas colgadas

    def test_con_reserva_y_sin_ella(self):
        # La reserva es lo que protege: el mismo escenario con la estimación desactivada
        # sí se pasa del tope (solo se comprueba después de cada respuesta).
        motor, job = _motor(30, tope_gasto_usd=D("0.02"), estimar_antes_de_enviar=False)
        motor.ejecutar(Callbacks())
        assert motor.estado.gasto_acumulado_usd >= D("0.02")

    def test_una_orden_que_falla_para_a_las_demas_y_conserva_lo_pagado(self):
        motor, job = _motor(20, tope_gasto_usd=D("100"), estimar_antes_de_enviar=False)
        motor._client = FakeClient([FakeResponse()] * 3 + [RuntimeError("boom")] + [FakeResponse()] * 30)
        assert motor.ejecutar(Callbacks()) == StopReason.FATAL
        pagadas = job.completed_count()
        assert pagadas >= 3
        for m in job.messages:
            if m.status == "completed":
                assert job.response_path(m.id, 1).exists()
