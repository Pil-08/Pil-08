"""La CLI de punta a punta, con los motores sustituidos por clientes falsos."""

import io
import json
import sys
from decimal import Decimal as D

import pytest

from claude_batch_monitor import __main__ as punto_de_entrada
from claude_batch_monitor import cli
from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.core.base import StopReason
from claude_batch_monitor.core.batch import BatchEngine, custom_id
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.job import STATUS_COMPLETED, STATUS_FAILED, Job, listar_jobs

from .test_batch import FakeBatches, FakeClientLote, errored, exito
from .test_engine import FakeResponse
from .test_errors import TOPE_MENSUAL, error, interno
from .test_integration import FakeClient, status_error


@pytest.fixture
def cliente(monkeypatch):
    """Instala un cliente falso en el motor inmediato y devuelve su guion mutable."""
    estado = {"guion": [FakeResponse()] * 50, "cliente": None}

    def inicializar(self):
        estado["cliente"] = FakeClient(estado["guion"])
        self._client = estado["cliente"]

    monkeypatch.setattr(SequentialEngine, "inicializar_cliente", inicializar)
    monkeypatch.setattr(SequentialEngine, "espera_error_servidor", lambda self, e, i: 0.01)
    return estado


class _SinAnestesia:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def sin_efectos_en_el_sistema(monkeypatch):
    """Los tests no deben bloquear la suspensión del equipo ni lanzar notificaciones."""
    monkeypatch.setattr(cli, "KeepAwake", _SinAnestesia)


def correr(capsys, *argv):
    codigo = cli.main_cli(list(argv))
    cap = capsys.readouterr()
    return codigo, cap.out, cap.err


class TestUsoIncorrecto:
    def test_sin_argumentos_sale_con_5(self, capsys):
        codigo, _, err = correr(capsys)
        assert codigo == 5 and "--mensaje" in err

    def test_argumento_desconocido_sale_con_5_no_con_2(self, capsys):
        # argparse sale con 2 por defecto; aquí 2 significa "tope de gasto".
        codigo, _, err = correr(capsys, "--no-existe")
        assert codigo == 5

    def test_numero_invalido(self, capsys):
        codigo, _, err = correr(capsys, "--mensaje", "hola", "--tope-gasto", "mucho")
        assert codigo == 5 and "--tope-gasto" in err

    def test_tope_negativo(self, capsys):
        assert correr(capsys, "--mensaje", "hola", "--tope-gasto", "-3")[0] == 5

    def test_fichero_inexistente_se_toma_como_texto_literal(self, capsys, cliente):
        # Sin fichero con ese nombre, el valor se interpreta como la propia orden.
        codigo, out, _ = correr(capsys, "--mensaje", "no_existe.csv")
        assert codigo == 0

    def test_columna_que_no_existe_falla_antes_de_gastar(self, capsys, tmp_path, cliente):
        f = tmp_path / "datos.csv"
        f.write_text("id,comentario\n1,hola\n", encoding="utf-8")
        codigo, _, err = correr(capsys, "--mensaje", str(f), "--campo", "texto")
        assert codigo == 5
        assert "no tiene la columna 'texto'" in err and "id, comentario" in err
        assert cliente["cliente"] is None  # no se llegó ni a crear el cliente

    def test_bucle_en_modo_lote_no_existe(self, capsys):
        assert correr(capsys, "--mensaje", "hola", "--modo", "lote", "--bucle")[0] == 5

    def test_max_tokens_por_encima_del_limite_del_modelo(self, capsys):
        codigo, _, err = correr(capsys, "--mensaje", "hola", "--modelo", "claude-haiku-4-5", "--max-tokens", "999999")
        assert codigo == 5 and "64,000" in err


class TestVersionYListar:
    def test_version(self, capsys):
        codigo, out, _ = correr(capsys, "--version")
        assert codigo == 0 and out.startswith("Claude Batch Monitor 12.")

    def test_listar_sin_lotes(self, capsys):
        assert "No hay lotes" in correr(capsys, "--listar")[1]

    def test_listar_con_lotes_marca_los_incompletos(self, capsys):
        j = Job.crear(["a", "b"], Configuracion(modelo="claude-haiku-4-5"))
        j.messages[0].status = STATUS_COMPLETED
        j.save()
        out = correr(capsys, "--listar")[1]
        assert j.job_id in out and "1/2" in out and "incompleto" in out

    def test_listar_en_json(self, capsys):
        Job.crear(["a"], Configuracion()).save()
        datos = json.loads(correr(capsys, "--listar", "--json")[1])
        assert datos[0]["total"] == 1 and isinstance(datos[0]["coste_usd"], str)


class TestDryRun:
    def test_estima_sin_enviar_ni_necesitar_clave(self, capsys, tmp_path):
        f = tmp_path / "reseñas.csv"
        f.write_text("id,comentario\n1,me encantó\n2,llegó roto\n3,correcto\n", encoding="utf-8")
        codigo, out, err = correr(
            capsys, "--mensaje", str(f), "--campo", "comentario", "--limit", "2",
            "--plantilla", "Clasifica:\n{input}", "--modelo", "claude-haiku-4-5", "--dry-run",
        )
        assert codigo == 0
        assert "Órdenes detectadas : 2" in out and "Coste MÁXIMO" in out and "No se ha enviado nada" in out
        assert "--limit 2" in err

    def test_coste_maximo_correcto(self, capsys):
        # Haiku 4.5 (1/5), 2 órdenes, max_tokens 16000: ~2 x $0.08 de salida.
        out = correr(capsys, "--mensaje", "uno\n---\ndos", "--modelo", "claude-haiku-4-5", "--dry-run")[1]
        assert "$0.1600" in out or "$0.1601" in out

    def test_dry_run_en_json(self, capsys):
        datos = json.loads(correr(capsys, "--mensaje", "uno", "--dry-run", "--json")[1])
        assert datos["ordenes"] == 1 and D(datos["coste_maximo_usd"]) > 0

    def test_avisa_si_el_peor_caso_supera_el_tope(self, capsys):
        _, _, err = correr(capsys, "--mensaje", "uno\n---\ndos", "--dry-run", "--tope-gasto", "0.01")
        assert "supera tu tope" in err

    def test_el_modo_lote_cuesta_la_mitad(self, capsys):
        normal = correr(capsys, "--mensaje", "uno", "--modelo", "claude-haiku-4-5", "--dry-run", "--json")[1]
        lote = correr(capsys, "--mensaje", "uno", "--modelo", "claude-haiku-4-5", "--dry-run", "--json", "--modo", "lote")[1]
        assert D(json.loads(lote)["coste_maximo_usd"]) * 2 == D(json.loads(normal)["coste_maximo_usd"])


class TestEjecucion:
    def test_lote_completo(self, capsys, cliente, tmp_path):
        destino = tmp_path / "salida.csv"
        codigo, out, _ = correr(capsys, "--mensaje", "uno\n---\ndos\n---\ntres", "--exportar", str(destino))
        assert codigo == 0
        assert "Completadas 3/3" in out and "Completado." in out
        assert destino.exists() and destino.read_bytes().startswith(b"\xef\xbb\xbf")
        assert cliente["cliente"].messages.llamadas == 3

    def test_json_deja_stdout_limpio_y_el_progreso_va_a_stderr(self, capsys, cliente):
        codigo, out, err = correr(capsys, "--mensaje", "uno\n---\ndos", "--json")
        datos = json.loads(out)  # stdout es SOLO el JSON
        assert codigo == 0 and datos["completadas"] == 2 and datos["razon"] == "completed"
        assert D(datos["coste_usd"]) == D("0.014")  # 2 órdenes x $0.007 (Sonnet 5.5: 2/10)
        assert "orden 1: OK" in err

    def test_al_empezar_purga_lo_viejo_segun_la_retencion_guardada_pero_no_lo_pendiente(self, capsys, cliente):
        import os
        import time

        from claude_batch_monitor import paths
        from claude_batch_monitor.config import ConfigManager

        ConfigManager.guardar(Configuracion(retencion_dias=10))
        viejo_hecho = Job.crear(["a"], Configuracion())
        viejo_hecho.messages[0].status = STATUS_COMPLETED
        viejo_pendiente = Job.crear(["b"], Configuracion())          # p. ej. la cola del modo simple
        for j in (viejo_hecho, viejo_pendiente):
            j.save()
            hace_mucho = time.time() - 60 * 86400
            for f in paths.RESPONSES_DIR.glob(f"job_{j.job_id}*"):
                os.utime(f, (hace_mucho, hace_mucho))
        assert correr(capsys, "--mensaje", "uno")[0] == 0
        assert not viejo_hecho.file_path.exists()
        assert viejo_pendiente.file_path.exists()

    def test_silencioso_solo_muestra_el_resumen(self, capsys, cliente):
        _, out, _ = correr(capsys, "--mensaje", "uno", "--silencioso")
        assert "orden 1" not in out and "Completado." in out

    def test_plantilla_y_system_desde_fichero(self, capsys, cliente, tmp_path):
        sistema = tmp_path / "sistema.txt"
        sistema.write_text("Eres un traductor.", encoding="utf-8")
        assert correr(capsys, "--mensaje", "hola", "--plantilla", "Traduce: {input}", "--system", str(sistema))[0] == 0
        job = Job.load(listar_jobs()[0]["job_id"])
        assert job.messages[0].text == "Traduce: hola"
        assert job.system_prompt == "Eres un traductor."

    def test_el_tope_de_gasto_sale_con_2(self, capsys, cliente):
        codigo, out, err = correr(capsys, "--mensaje", "\n---\n".join(["x"] * 10), "--tope-gasto", "0.005",
                                  "--precio-in", "1", "--precio-out", "5", "--max-tokens", "100")
        assert codigo == 2 and "tope de gasto" in out.lower()

    def test_sin_saldo_sale_con_3(self, capsys, cliente):
        cliente["guion"] = [FakeResponse(), status_error(402)]
        codigo, out, _ = correr(capsys, "--mensaje", "a\n---\nb")
        assert codigo == 3 and "sin saldo" in out.lower()

    def test_tope_mensual_de_la_cuenta_sale_con_3(self, capsys, cliente):
        cliente["guion"] = [error(429, interno(TOPE_MENSUAL))]
        codigo, out, err = correr(capsys, "--mensaje", "a")
        assert codigo == 3 and "2026-09-01" in err and "Para continuar: --reanudar" in out

    def test_error_fatal_sale_con_1(self, capsys, cliente):
        cliente["guion"] = [status_error(401)]
        assert correr(capsys, "--mensaje", "a")[0] == 1

    def test_ctrl_c_sale_con_4_y_deja_el_lote_reanudable(self, capsys, monkeypatch):
        def interrumpido(self, cb=None):
            raise KeyboardInterrupt
        monkeypatch.setattr(SequentialEngine, "ejecutar", interrumpido)
        codigo, out, _ = correr(capsys, "--mensaje", "a\n---\nb")
        assert codigo == 4 and "Para continuar: --reanudar" in out

    def test_sin_clave_de_api_falla_con_mensaje_claro(self, capsys):
        codigo, _, err = correr(capsys, "--mensaje", "hola")
        assert codigo == 1 and "API key" in err


class TestReanudar:
    def _lote_a_medias(self, **kw):
        c = Configuracion(modelo="claude-haiku-4-5", **kw)
        job = Job.crear(["a", "b", "c"], c)
        job.messages[0].status = STATUS_COMPLETED
        job.messages[0].coste_usd = D("0.0035")
        job.save()
        return job

    def test_solo_se_envian_los_pendientes(self, capsys, cliente):
        job = self._lote_a_medias()
        codigo, out, _ = correr(capsys, "--reanudar", job.job_id)
        assert codigo == 0 and cliente["cliente"].messages.llamadas == 2
        assert Job.load(job.job_id).completed_count() == 3

    def test_lote_inexistente(self, capsys):
        codigo, _, err = correr(capsys, "--reanudar", "zzzzzzzz")
        assert codigo == 5 and "--listar" in err

    def test_las_fallidas_se_reintentan(self, capsys, cliente):
        job = self._lote_a_medias()
        job.messages[1].status = STATUS_FAILED
        job.messages[1].errores = 5
        job.save()
        codigo, out, _ = correr(capsys, "--reanudar", job.job_id)
        assert codigo == 0 and "1 orden(es) fallida(s) se reintentarán" in out
        assert Job.load(job.job_id).completed_count() == 3

    def test_el_contador_de_gasto_parte_de_lo_ya_gastado(self, capsys, cliente):
        # Tope $0.005; la orden ya hecha costó $0.0035: solo cabe UNA más.
        job = self._lote_a_medias()
        codigo, _, _ = correr(capsys, "--reanudar", job.job_id, "--tope-gasto", "0.005",
                              "--precio-in", "1", "--precio-out", "5", "--max-tokens", "100")
        assert codigo == 2 and cliente["cliente"].messages.llamadas == 1

    def test_X4_hereda_el_modo_lote_en_vez_de_reenviarlo_por_la_api_normal(self, capsys, monkeypatch, cliente):
        # Antes `--modo` valía 'inmediato' por defecto y pisaba el del lote: reanudar un lote
        # creado en modo lote lo reenviaba entero al doble de precio.
        job = self._lote_a_medias(modo="lote")
        job.batch_id = "msgbatch_previo"
        job.save()
        b = FakeBatches({custom_id(m): exito("del lote") for m in job.messages})
        usados = []

        def inicializar(self):
            usados.append(type(self).__name__)
            self._client = FakeClientLote(b)

        monkeypatch.setattr(BatchEngine, "inicializar_cliente", inicializar)
        monkeypatch.setattr(BatchEngine, "esperar", lambda self, s, cb: True)
        codigo, out, _ = correr(capsys, "--reanudar", job.job_id)
        assert codigo == 0 and usados == ["BatchEngine"]
        assert cliente["cliente"] is None and b.creados == []  # ni API normal ni lote nuevo

    def test_avisa_si_cambia_el_modelo(self, capsys, cliente):
        job = self._lote_a_medias()
        _, _, err = correr(capsys, "--reanudar", job.job_id, "--modelo", "claude-sonnet-5-5")
        assert "otro modelo" in err

    def test_la_estimacion_cuenta_solo_lo_pendiente(self, capsys):
        job = self._lote_a_medias()
        datos = json.loads(correr(capsys, "--reanudar", job.job_id, "--dry-run", "--json")[1])
        assert datos["ordenes"] == 2  # no 3: una ya está pagada


class TestLote:
    def test_modo_lote_de_punta_a_punta(self, capsys, monkeypatch):
        b = FakeBatches({"m000001": exito("uno"), "m000002": exito("dos")})
        monkeypatch.setattr(BatchEngine, "inicializar_cliente", lambda self: setattr(self, "_client", FakeClientLote(b)))
        monkeypatch.setattr(BatchEngine, "esperar", lambda self, s, cb: True)
        codigo, out, _ = correr(capsys, "--mensaje", "a\n---\nb", "--modo", "lote", "--json",
                                "--modelo", "claude-haiku-4-5")
        datos = json.loads(out)
        assert codigo == 0 and datos["modo"] == "lote" and datos["completadas"] == 2
        # 2 órdenes x (1000 in x $1/M + 500 out x $5/M) x 50 % de descuento de lote
        assert D(datos["coste_usd"]) == D("0.0035")

    def test_un_lote_con_ordenes_fallidas_no_sale_con_0(self, capsys, monkeypatch):
        b = FakeBatches({"m000001": exito(), "m000002": errored()})
        monkeypatch.setattr(BatchEngine, "inicializar_cliente", lambda self: setattr(self, "_client", FakeClientLote(b)))
        monkeypatch.setattr(BatchEngine, "esperar", lambda self, s, cb: True)
        codigo, out, _ = correr(capsys, "--mensaje", "a\n---\nb", "--modo", "lote")
        assert codigo == 1 and "fallidas 1" in out and "Para continuar: --reanudar" in out


class TestCodigosDeSalida:
    @pytest.mark.parametrize("razon,codigo", [
        (StopReason.COMPLETED, 0), (StopReason.FATAL, 1), (StopReason.NETWORK, 1),
        (StopReason.BUDGET, 2), (StopReason.NO_CREDIT, 3), (StopReason.ACCOUNT_LIMIT, 3),
        (StopReason.STOPPED, 4), (StopReason.DEADLINE, 4),
    ])
    def test_tabla(self, razon, codigo):
        assert cli.codigo_de_salida(razon) == codigo


class TestUtf8:
    def test_X2_la_salida_redirigida_en_cp1252_no_revienta(self, monkeypatch):
        # La consola real de Windows admite UTF-8, pero una tubería o un fichero usan cp1252.
        crudo = io.BytesIO()
        flujo = io.TextIOWrapper(crudo, encoding="cp1252", errors="strict")
        monkeypatch.setattr(sys, "stdout", flujo)
        monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict"))
        with pytest.raises(UnicodeEncodeError):
            flujo.write("日本語 ✓ 🚀")
            flujo.flush()
        crudo.seek(0)
        crudo.truncate()
        cli.forzar_utf8_en_salida()
        sys.stdout.write("日本語 ✓ 🚀")
        sys.stdout.flush()
        assert crudo.getvalue().decode("utf-8") == "日本語 ✓ 🚀"

    def test_un_flujo_sin_reconfigure_no_da_error(self, monkeypatch):
        class Viejo:
            pass
        monkeypatch.setattr(sys, "stdout", Viejo())
        cli.forzar_utf8_en_salida()


class TestPuntoDeEntrada:
    def test_las_opciones_de_terminal_se_reconocen(self):
        assert punto_de_entrada._es_terminal(["--mensaje", "x"])
        assert punto_de_entrada._es_terminal(["--reanudar=abc"])
        assert punto_de_entrada._es_terminal(["--dry-run"])
        assert not punto_de_entrada._es_terminal([])
        assert not punto_de_entrada._es_terminal(["--avanzado"])

    def test_con_opciones_de_terminal_no_abre_la_interfaz(self, capsys):
        assert punto_de_entrada.main(["--version"]) == 0
        assert "Claude Batch Monitor" in capsys.readouterr().out

    def test_sin_tkinter_explica_que_hacer(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "tkinter", None)  # import tkinter -> ImportError
        assert punto_de_entrada.main([]) == 1
        err = capsys.readouterr().err
        assert "tkinter" in err and "python3-tk" in err and "--cli" in err

    def test_sin_anthropic_explica_que_instalar(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "anthropic", None)
        assert punto_de_entrada.main([]) == 1
        assert "pip install anthropic" in capsys.readouterr().err

    def test_un_ejecutable_sin_consola_no_puede_usarse_desde_la_terminal(self, monkeypatch):
        avisos = []
        monkeypatch.setattr(punto_de_entrada, "_avisar_sin_consola", lambda: avisos.append(True))
        monkeypatch.setattr(sys, "stdout", None)      # así queda `sys.stdout` en un .exe «windowed»
        assert punto_de_entrada.main(["--version"]) == 1
        assert avisos == [True]

    def test_el_aviso_sin_consola_no_falla_fuera_de_windows(self):
        punto_de_entrada._avisar_sin_consola()        # no hay `ctypes.windll`: debe pasar en silencio

    def test_sin_consola_la_interfaz_grafica_si_arranca(self, monkeypatch):
        # Lo normal en un .exe «windowed»: sin argumentos no hay terminal que echar de menos.
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        llamado = []
        import claude_batch_monitor.gui as gui
        monkeypatch.setattr(gui, "run", lambda argv: llamado.append(argv) or 0)
        pytest.importorskip("tkinter")
        assert punto_de_entrada.main([]) == 0 and llamado == [[]]
