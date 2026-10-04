"""Throttling proactivo y política de backoff.

Fija las lecciones aprendidas de la práctica documentada para la API de
Anthropic: leer las cabeceras en todas las respuestas, priorizar `Retry-After`
y usar *full jitter* en el backoff.
"""

import statistics
from decimal import Decimal

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.job import Job
from claude_batch_monitor.ratelimit import (
    ESPERA_MAXIMA_SEGUNDOS,
    RateLimitTracker,
    observar_seguro,
)


def _cabeceras(**cubos):
    """`_cabeceras(requests=(100, 5, "30s"))` -> cabeceras de la API."""
    salida = {}
    for familia, (limite, restante, reset) in cubos.items():
        nombre = familia.replace("_", "-")
        salida[f"anthropic-ratelimit-{nombre}-limit"] = str(limite)
        salida[f"anthropic-ratelimit-{nombre}-remaining"] = str(restante)
        salida[f"anthropic-ratelimit-{nombre}-reset"] = reset
    return salida


class TestRateLimitTracker:
    def test_sin_datos_no_frena(self):
        assert RateLimitTracker().espera_recomendada() == 0.0

    def test_con_cuota_de_sobra_no_frena(self):
        t = RateLimitTracker()
        t.observar(_cabeceras(requests=(1000, 900, "60s")))
        assert t.espera_recomendada() == 0.0

    def test_cuota_agotada_espera_al_reset(self):
        t = RateLimitTracker()
        t.observar(_cabeceras(requests=(1000, 0, "30s")))
        assert 25 <= t.espera_recomendada() <= 30

    def test_cuota_baja_reparte_el_tiempo_restante(self):
        # 5 peticiones para 50 s -> ~10 s entre peticiones para llegar justo.
        t = RateLimitTracker()
        t.observar(_cabeceras(requests=(1000, 5, "50s")))
        espera = t.espera_recomendada()
        assert 8 <= espera <= 11

    def test_manda_el_cubo_mas_apretado(self):
        t = RateLimitTracker()
        t.observar(_cabeceras(requests=(1000, 900, "60s"), tokens=(100000, 0, "40s")))
        # Requests va sobrado, pero tokens está a cero: hay que frenar.
        assert t.espera_recomendada() > 30

    def test_la_espera_esta_acotada(self):
        t = RateLimitTracker()
        t.observar(_cabeceras(requests=(1000, 0, "1h")))
        assert t.espera_recomendada() == ESPERA_MAXIMA_SEGUNDOS

    def test_cabeceras_basura_no_revientan(self):
        t = RateLimitTracker()
        t.observar({"anthropic-ratelimit-requests-limit": "no es un número"})
        t.observar(None)
        t.observar({})
        assert t.espera_recomendada() == 0.0

    def test_resumen_legible(self):
        t = RateLimitTracker()
        assert "sin datos" in t.resumen()
        t.observar(_cabeceras(requests=(1000, 250, "30s")))
        assert "requests 250/1000" in t.resumen()


class TestObservarSeguro:
    def test_lee_headers_directos(self):
        class Cruda:
            headers = _cabeceras(requests=(10, 1, "5s"))

        t = RateLimitTracker()
        observar_seguro(t, Cruda())
        assert t.hay_datos

    def test_lee_headers_de_un_error(self):
        class Respuesta:
            headers = _cabeceras(tokens=(100, 0, "5s"))

        class Error:
            response = Respuesta()

        t = RateLimitTracker()
        observar_seguro(t, Error())
        assert t.hay_datos

    def test_objeto_sin_headers_no_revienta(self):
        t = RateLimitTracker()
        observar_seguro(t, object())
        observar_seguro(None, object())
        assert not t.hay_datos


def _engine():
    config = Configuracion(
        modelo="claude-haiku-4-5",
        max_tokens=1024,
        tope_gasto_usd=Decimal("10"),
        estimar_antes_de_enviar=False,
    )
    job = Job.crear(["orden"], config)
    return SequentialEngine(config, job)


class FakeHttpResponse:
    def __init__(self, headers):
        self.headers = headers


class FakeError(Exception):
    def __init__(self, headers):
        super().__init__("simulado")
        self.response = FakeHttpResponse(headers)


class TestPrioridadDeEspera:
    def test_retry_after_gana_a_las_cabeceras_de_reset(self):
        # El orden recomendado es Retry-After -> reset -> backoff. La v12.0.2
        # miraba el reset primero.
        engine = _engine()
        error = FakeError(
            {"retry-after": "7", "anthropic-ratelimit-tokens-reset": "300s"}
        )
        assert engine.espera_rate_limit(error) == 9.0   # 7 + 2

    def test_sin_retry_after_usa_el_reset(self):
        engine = _engine()
        assert engine.espera_rate_limit(
            FakeError({"anthropic-ratelimit-tokens-reset": "45s"})
        ) == 47.0

    def test_sin_nada_cae_a_un_minuto(self):
        assert _engine().espera_rate_limit(FakeError({})) == 60.0


class TestFullJitter:
    def test_el_backoff_sortea_en_todo_el_intervalo(self):
        # Con jitter aditivo pequeño, todos los hilos reintentaban casi a la vez
        # y volvían a tumbar al servidor. El full jitter los desincroniza.
        engine = _engine()
        error = FakeError({})
        muestras = [engine.espera_error_servidor(error, 4) for _ in range(400)]
        techo = 30 * (2 ** 3)  # 240 s en el 4º intento

        assert min(muestras) >= 1.0
        assert max(muestras) <= techo
        # Reparto ancho: la media cae cerca de la mitad del techo, no pegada a él.
        assert techo * 0.35 < statistics.mean(muestras) < techo * 0.65
        # Y hay dispersión real, no un valor casi fijo.
        assert statistics.pstdev(muestras) > techo * 0.15

    def test_retry_after_sigue_mandando_sobre_el_jitter(self):
        engine = _engine()
        assert engine.espera_error_servidor(FakeError({"retry-after": "12"}), 5) == 17.0

    def test_crece_con_los_intentos(self):
        engine = _engine()
        error = FakeError({})
        pocos = statistics.mean(engine.espera_error_servidor(error, 1) for _ in range(200))
        muchos = statistics.mean(engine.espera_error_servidor(error, 5) for _ in range(200))
        assert muchos > pocos
