"""Simulacros del disco: lo ya pagado no se pierde aunque el disco se llene o no deje escribir.

Se hace fallar la escritura de ficheros a bajo nivel (`Path.write_bytes`), de modo que vale igual contra los
módulos y contra el archivo único.
"""

from __future__ import annotations

import errno
import json
import re
from pathlib import Path

import pytest

from claude_batch_monitor import paths
from claude_batch_monitor.core.base import Callbacks, StopReason
from claude_batch_monitor.export import exportar, leer_respuesta
from claude_batch_monitor.job import STATUS_COMPLETED, STATUS_PENDING, Job
from claude_batch_monitor.state import EstadoMonitor

from .simulacros_comun import comprobar_invariantes, configuracion, correr

pytestmark = pytest.mark.simulacro

CINCO = [f"Pregunta del disco {i}" for i in range(1, 6)]
RESPUESTA = re.compile(r"_c\d+_m\d+\.txt\.")
TRABAJO = re.compile(r"^job_[0-9a-f]+\.json\.")
ESTADO = re.compile(r"^state\.json\.")


class DiscoQueFalla:
    """Deja escribir solo `respuestas` ficheros de respuesta (y siempre el resto), o lo que se pida."""

    def __init__(self):
        self.respuestas = None   # None = sin límite
        self.escritas = 0
        self.fallos = 0
        self.trabajo_falla = False
        self.estado_falla = False

    def lleno(self):
        self.respuestas = self.escritas

    def liberar(self):
        self.respuestas = None
        self.trabajo_falla = self.estado_falla = False


@pytest.fixture
def disco(monkeypatch):
    d = DiscoQueFalla()
    original = Path.write_bytes

    def escribir(self, *args, **kwargs):
        nombre = self.name
        fallar = False
        if RESPUESTA.search(nombre):
            if d.respuestas is not None and d.escritas >= d.respuestas:
                fallar = True
            else:
                d.escritas += 1
        elif (d.trabajo_falla and TRABAJO.search(nombre)) or (d.estado_falla and ESTADO.search(nombre)):
            fallar = True
        if fallar:
            d.fallos += 1
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", escribir)
    return d


def ficheros_de_respuesta(job):
    return sorted(p.name for p in paths.RESPONSES_DIR.glob(f"job_{job.job_id}_c*_m*.txt"))


class TestInmediato:
    def test_si_el_disco_se_llena_se_para_y_la_respuesta_pagada_sigue_viéndose(self, api, esperas, disco):
        disco.respuestas = 2
        c = correr(CINCO, configuracion())
        assert c.razon is StopReason.FATAL
        assert any("No se pudo guardar en el disco" in e for e in c.errores)
        assert len(api.cobros) == 3                          # la tercera se pagó y se contó; la cuarta ni se envió
        assert c.estado_de(3) == STATUS_COMPLETED and c.estado_de(4) == STATUS_PENDING
        assert not (paths.RESPONSES_DIR / f"job_{c.job.job_id}_c1_m003.txt").exists()
        assert "Respuesta a: Pregunta del disco 3" in leer_respuesta(c.job, 3, 1)       # sigue disponible
        assert c.job.coste_total() > 0 and c.motor.gasto_actual() == c.job.coste_total()

    def test_se_puede_exportar_lo_que_no_se_pudo_guardar(self, api, esperas, disco, tmp_path):
        disco.respuestas = 2
        c = correr(CINCO, configuracion())
        registros = [json.loads(x) for x in exportar(c.job, tmp_path / "r.jsonl").read_text(encoding="utf-8").splitlines()]
        assert "Respuesta a: Pregunta del disco 3" in registros[2]["respuesta"]

    def test_al_liberar_espacio_y_reanudar_se_vuelca_lo_pendiente_y_no_se_cobra_dos_veces(self, api, esperas, disco):
        disco.respuestas = 2
        c1 = correr(CINCO, configuracion())
        assert c1.razon is StopReason.FATAL and len(c1.job.respuestas_sin_guardar) == 1
        disco.liberar()
        c2 = correr([], configuracion(), job=c1.job)
        assert c2.razon is StopReason.COMPLETED and c2.completadas == 5
        assert c2.job.respuestas_sin_guardar == {}
        assert len(ficheros_de_respuesta(c2.job)) == 5
        comprobar_invariantes(api, c2)                       # cada pregunta se facturó una sola vez

    def test_aunque_el_disco_siga_lleno_al_cerrar_no_revienta(self, api, esperas, disco):
        disco.respuestas = 0
        c = correr(CINCO[:2], configuracion())
        assert c.razon is StopReason.FATAL and c.job.respuestas_sin_guardar

    def test_con_concurrencia_se_avisa_una_sola_vez(self, api, esperas, disco):
        disco.respuestas = 0
        c = correr([f"Pregunta {i}" for i in range(12)], configuracion(concurrencia=4))
        assert c.razon is StopReason.FATAL
        assert sum("No se pudo guardar en el disco" in e for e in c.errores) == 1
        assert len(api.cobros) <= 4                           # solo las que ya iban en vuelo

    def test_si_no_se_puede_guardar_el_gasto_acumulado_tambien_se_para(self, api, esperas, disco):
        disco.estado_falla = True
        c = correr(CINCO, configuracion())
        assert c.razon is StopReason.FATAL and len(api.cobros) == 1
        assert any("el gasto acumulado" in e for e in c.errores)

    def test_si_no_se_puede_guardar_el_estado_del_lote_tambien_se_para(self, api, esperas, disco):
        c0 = correr(CINCO[:1], configuracion())              # solo para tener un lote válido en disco
        assert c0.razon is StopReason.COMPLETED
        disco.trabajo_falla = True
        c = correr(CINCO, configuracion())
        assert c.razon is StopReason.FATAL and any("el estado del lote" in e for e in c.errores)
        assert len(api.cobros) == 2                           # una del primer lote y una de este


class TestLote:
    def test_recoger_se_detiene_y_al_reanudar_se_recoge_el_resto_sin_pagar_otra_vez(self, api, esperas, disco):
        disco.respuestas = 2
        config = configuracion(modo="lote")
        c1 = correr(CINCO, config)
        assert c1.razon is StopReason.FATAL
        assert c1.job.batch_id, "el lote sigue en el servidor para poder recoger el resto"
        assert len(api.lotes) == 1
        disco.liberar()
        c2 = correr([], config, job=c1.job)
        assert c2.razon is StopReason.COMPLETED and c2.completadas == 5 and c2.job.batch_id == ""
        assert len(api.lotes) == 1
        assert len(ficheros_de_respuesta(c2.job)) == 5
        comprobar_invariantes(api, c2)


class TestEscrituraAtomica:
    def test_un_permiso_denegado_pasajero_se_supera_con_reintentos(self, tmp_path, monkeypatch):
        """En Windows un antivirus puede tener abierto el destino un instante: no es motivo para perder datos."""
        intentos = []
        original = Path.replace

        def replace(self, destino):
            intentos.append(1)
            if len(intentos) < 3:
                raise PermissionError(13, "Permission denied")
            return original(self, destino)

        monkeypatch.setattr(Path, "replace", replace)
        monkeypatch.setattr(paths.time, "sleep", lambda s: None)
        paths.write_atomic(tmp_path / "x.txt", "hola")
        assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hola" and len(intentos) == 3
        assert list(tmp_path.glob("*.tmp")) == []

    def test_si_el_permiso_nunca_llega_falla_y_no_deja_temporales(self, tmp_path, monkeypatch):
        def replace(self, destino):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "replace", replace)
        monkeypatch.setattr(paths.time, "sleep", lambda s: None)
        with pytest.raises(PermissionError):
            paths.write_atomic(tmp_path / "x.txt", "hola")
        assert list(tmp_path.glob("*.tmp")) == []


class TestGuardarDevuelveSiPudo:
    def test_estado(self, tmp_path, disco):
        assert EstadoMonitor().guardar(tmp_path / "state.json") is True
        disco.estado_falla = True
        assert EstadoMonitor().guardar(tmp_path / "state.json") is False

    def test_lote(self, disco):
        job = Job.crear(["a"], configuracion())
        assert job.save() is True
        disco.trabajo_falla = True
        assert job.save() is False


def test_los_callbacks_reciben_el_error_una_vez(api, esperas, disco):
    errores = []
    disco.respuestas = 0
    c = correr(CINCO[:3], configuracion(), callbacks=Callbacks(error=errores.append))
    assert c.razon is StopReason.FATAL and len(errores) == 1
