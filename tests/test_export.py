import csv
import json
from decimal import Decimal as D

import pytest

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.export import ErrorDeExportacion, exportar, formato_por_extension
from claude_batch_monitor.job import STATUS_COMPLETED, STATUS_FAILED, Job


@pytest.fixture
def job():
    j = Job.crear(["¿Qué es un token?", "Segunda, con coma\ny salto"], Configuracion(modelo="claude-sonnet-5-5"))
    a, b = j.messages
    a.status, a.tokens_input, a.tokens_output, a.coste_usd, a.latency_ms = (
        STATUS_COMPLETED, 12, 40, D("0.000984"), 700,
    )
    b.status, b.error_type, b.detalle = STATUS_FAILED, "auth", "clave inválida"
    j.response_path(1, 1).write_text(
        "====== SOLICITUD ======\n¿Qué es un token?\n\n====== RESPUESTA ======\nUna unidad de texto.\n",
        encoding="utf-8",
    )
    return j


def _leer_csv(ruta):
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class TestCsv:
    def test_columnas_y_valores(self, job, tmp_path):
        filas = _leer_csv(exportar(job, tmp_path / "r.csv"))
        assert list(filas[0].keys()) == [
            "id", "estado", "tokens_entrada", "tokens_salida", "coste_usd", "latencia_ms",
            "truncada", "error_type", "detalle", "solicitud", "respuesta",
        ]
        assert filas[0]["estado"] == "completed" and filas[0]["respuesta"] == "Una unidad de texto."
        assert filas[0]["coste_usd"] == "0.000984" and filas[0]["latencia_ms"] == "700"
        assert filas[1]["estado"] == "failed" and filas[1]["error_type"] == "auth"
        assert filas[1]["solicitud"] == "Segunda, con coma\ny salto"

    def test_lleva_bom_para_que_excel_respete_las_tildes(self, job, tmp_path):
        ruta = exportar(job, tmp_path / "r.csv")
        assert ruta.read_bytes().startswith(b"\xef\xbb\xbf")


class TestOtrosFormatos:
    def test_jsonl_una_linea_por_orden(self, job, tmp_path):
        lineas = exportar(job, tmp_path / "r.jsonl").read_text(encoding="utf-8").splitlines()
        regs = [json.loads(x) for x in lineas]
        assert len(regs) == 2
        assert regs[0]["respuesta"] == "Una unidad de texto." and regs[0]["modelo"] == "claude-sonnet-5-5"
        assert regs[1]["error_type"] == "auth" and regs[1]["latencia_ms"] == 0

    def test_markdown(self, job, tmp_path):
        md = exportar(job, tmp_path / "r.md").read_text(encoding="utf-8")
        assert f"# Resultados del lote {job.job_id}" in md
        assert "## Orden 1 — completed" in md and "Una unidad de texto." in md
        assert "## Orden 2 — failed: clave inválida" in md

    def test_texto_plano(self, job, tmp_path):
        txt = exportar(job, tmp_path / "r.txt").read_text(encoding="utf-8")
        assert "Una unidad de texto." in txt and "[orden 2: failed: clave inválida]" in txt


class TestFormato:
    def test_se_deduce_de_la_extension(self, tmp_path):
        assert formato_por_extension(tmp_path / "a.CSV") == "csv"
        assert formato_por_extension(tmp_path / "a.markdown") == "md"

    def test_extension_desconocida(self, job, tmp_path):
        with pytest.raises(ErrorDeExportacion):
            exportar(job, tmp_path / "r.xlsx")

    def test_formato_explicito_manda_sobre_la_extension(self, job, tmp_path):
        ruta = exportar(job, tmp_path / "salida.dat", formato="jsonl")
        assert json.loads(ruta.read_text(encoding="utf-8").splitlines()[0])["id"] == 1

    def test_crea_el_directorio_destino(self, job, tmp_path):
        assert exportar(job, tmp_path / "a" / "b" / "r.csv").exists()

    def test_orden_sin_respuesta_guardada_exporta_vacio(self, job, tmp_path):
        filas = _leer_csv(exportar(job, tmp_path / "r.csv"))
        assert filas[1]["respuesta"] == ""
