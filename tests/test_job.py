import json
import os
import time
from decimal import Decimal as D

from claude_batch_monitor import paths
from claude_batch_monitor.config import Configuracion
from claude_batch_monitor.job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REFUSED,
    STATUS_SKIPPED,
    TERMINAL_STATUSES,
    Job,
    JobMessage,
    listar_jobs,
    purgar_jobs_antiguos,
)


def _job(n=3, **kw):
    return Job.crear([f"orden {i}" for i in range(1, n + 1)], Configuracion(**kw))


class TestCreacion:
    def test_ids_correlativos_desde_uno(self):
        assert [m.id for m in _job(3).messages] == [1, 2, 3]

    def test_hereda_la_configuracion(self):
        j = _job(modelo="claude-opus-5-5", modo="lote", system_prompt="breve", separador="===")
        assert (j.modelo, j.modo, j.system_prompt, j.separador) == (
            "claude-opus-5-5", "lote", "breve", "===",
        )

    def test_todo_empieza_pendiente(self):
        assert len(_job().pendientes()) == 3

    def test_los_estados_terminales(self):
        assert TERMINAL_STATUSES == {STATUS_COMPLETED, STATUS_REFUSED, STATUS_SKIPPED}
        assert STATUS_FAILED not in TERMINAL_STATUSES


class TestPersistencia:
    def test_ida_y_vuelta_conserva_el_estado_por_orden(self):
        j = _job()
        m = j.messages[0]
        m.status, m.errores, m.truncated = STATUS_COMPLETED, 2, True
        m.tokens_input, m.tokens_output, m.tokens_cache_read = 100, 50, 30
        m.coste_usd, m.latency_ms = D("0.001234"), 850
        m.detalle, m.error_type = "algo", "server"
        j.batch_id, j.ciclos_completados = "msgbatch_1", 2
        j.save()

        k = Job.load(j.job_id)
        assert k.to_dict() == j.to_dict()
        assert k.messages[0].coste_usd == D("0.001234")
        assert isinstance(k.messages[0].coste_usd, D)

    def test_load_de_un_job_inexistente(self):
        assert Job.load("ffffffff") is None

    def test_load_de_un_fichero_corrupto(self):
        (paths.RESPONSES_DIR / "job_deadbeef.json").write_text("{roto", encoding="utf-8")
        assert Job.load("deadbeef") is None

    def test_carga_un_job_de_la_v11(self):
        # La v11 guardaba "attempts", no "errores", y no tenía modo ni system prompt.
        v11 = {
            "job_id": "v11v11aa",
            "created_at": "2026-01-01T10:00:00",
            "modelo": "claude-haiku-4-5-20251001",
            "separador": "---",
            "ciclos_completados": 0,
            "messages": [
                {"id": 1, "text": "hola", "status": "completed", "attempts": 0, "truncated": False},
                {"id": 2, "text": "adiós", "status": "pending", "attempts": 3, "truncated": False},
            ],
        }
        (paths.RESPONSES_DIR / "job_v11v11aa.json").write_text(json.dumps(v11), encoding="utf-8")
        j = Job.load("v11v11aa")
        assert j.modo == "inmediato" and j.system_prompt == "" and j.batch_id == ""
        assert [m.errores for m in j.messages] == [0, 3]
        assert j.completed_count() == 1

    def test_el_fichero_se_escribe_en_el_directorio_de_respuestas(self):
        j = _job()
        j.save()
        assert j.file_path.parent == paths.RESPONSES_DIR and j.file_path.exists()

    def test_no_quedan_temporales(self):
        j = _job()
        for _ in range(5):
            j.save()
        assert not list(paths.RESPONSES_DIR.glob("*.tmp"))

    def test_rutas_de_respuesta_y_resumen(self):
        j = _job()
        assert j.response_path(7, 2).name == f"job_{j.job_id}_c2_m007.txt"
        assert j.resumen_path(2).name == f"job_{j.job_id}_ciclo_2_resumen.md"


class TestTransiciones:
    def test_reintentar_fallidas_las_devuelve_a_pendiente(self):
        j = _job()
        a, b, c = j.messages
        a.status = STATUS_COMPLETED
        b.status, b.errores, b.detalle, b.error_type = STATUS_FAILED, 5, "x", "server"
        c.status = STATUS_REFUSED
        assert j.reintentar_fallidas() == 1
        assert (b.status, b.errores, b.detalle, b.error_type) == (STATUS_PENDING, 0, "", "")
        assert a.status == STATUS_COMPLETED and c.status == STATUS_REFUSED

    def test_reintentar_sin_fallidas(self):
        assert _job().reintentar_fallidas() == 0

    def test_nuevo_ciclo_resetea_todo(self):
        j = _job()
        for m in j.messages:
            m.status, m.errores, m.truncated = STATUS_COMPLETED, 1, True
        j.batch_id = "msgbatch_x"
        j.reset_for_new_cycle()
        assert j.ciclos_completados == 1 and j.batch_id == ""
        assert all(m.status == STATUS_PENDING and m.errores == 0 and not m.truncated
                   for m in j.messages)

    def test_hay_trabajo_cuenta_las_fallidas_pero_no_las_rechazadas(self):
        j = _job(2)
        j.messages[0].status = STATUS_COMPLETED
        j.messages[1].status = STATUS_REFUSED
        assert not j.hay_trabajo() and j.terminado()
        j.messages[1].status = STATUS_FAILED
        assert j.hay_trabajo() and not j.terminado()

    def test_coste_total_suma_decimales(self):
        j = _job(3)
        for m in j.messages:
            m.coste_usd = D("0.1")
        assert j.coste_total() == D("0.3")

    def test_estadisticas(self):
        j = _job(4)
        j.messages[0].status = STATUS_COMPLETED
        j.messages[1].status = STATUS_FAILED
        j.messages[2].status = STATUS_REFUSED
        e = j.estadisticas()
        assert (e["total"], e["completadas"], e["pendientes"], e["fallidas"], e["rechazadas"]) == (4, 1, 1, 1, 1)


class TestIncompatibilidades:
    def test_misma_configuracion_no_avisa(self):
        c = Configuracion(modelo="claude-haiku-4-5")
        assert Job.crear(["a"], c).incompatibilidades(c) == []

    def test_el_id_con_fecha_no_cuenta_como_otro_modelo(self):
        j = Job.crear(["a"], Configuracion(modelo="claude-haiku-4-5-20251001"))
        assert j.incompatibilidades(Configuracion(modelo="claude-haiku-4-5")) == []

    def test_cambio_de_modelo(self):
        j = Job.crear(["a"], Configuracion(modelo="claude-haiku-4-5"))
        avisos = j.incompatibilidades(Configuracion(modelo="claude-opus-5-5"))
        assert len(avisos) == 1 and "otro modelo" in avisos[0]

    def test_reanudar_un_lote_en_modo_inmediato_avisa_del_doble_precio(self):
        j = Job.crear(["a"], Configuracion(modo="lote"))
        avisos = j.incompatibilidades(Configuracion(modo="inmediato"))
        assert any("doble de precio" in a for a in avisos)

    def test_cambio_de_system_prompt(self):
        j = Job.crear(["a"], Configuracion(system_prompt="A"))
        assert any("prompt de sistema" in a for a in j.incompatibilidades(Configuracion(system_prompt="B")))

    def test_espacios_en_el_system_prompt_no_cuentan(self):
        j = Job.crear(["a"], Configuracion(system_prompt="A"))
        assert j.incompatibilidades(Configuracion(system_prompt="  A \n")) == []


class TestListadoYPurga:
    def test_listar_jobs_ordena_por_fecha(self):
        a, b = _job(1), _job(1)
        a.created_at, b.created_at = "2026-01-01T00:00:00", "2026-02-01T00:00:00"
        a.save()
        b.save()
        assert [d["job_id"] for d in listar_jobs()] == [b.job_id, a.job_id]

    def test_listar_jobs_marca_los_incompletos(self):
        j = _job(2)
        j.messages[0].status = STATUS_COMPLETED
        j.save()
        d = listar_jobs()[0]
        assert d["incompleto"] and (d["completadas"], d["total"]) == (1, 2)

    def _envejecer(self, j, dias):
        antes = time.time() - dias * 86400
        for f in paths.RESPONSES_DIR.glob(f"job_{j.job_id}*"):
            os.utime(f, (antes, antes))

    @staticmethod
    def _terminado(n=1):
        j = _job(n)
        for m in j.messages:
            m.status = STATUS_COMPLETED
        j.save()
        return j

    def test_purga_lo_viejo_y_conserva_lo_reciente(self):
        viejo, nuevo = self._terminado(), self._terminado()
        viejo.response_path(1, 1).write_text("x", encoding="utf-8")
        self._envejecer(viejo, 40)
        assert purgar_jobs_antiguos(30) == 2
        assert not viejo.file_path.exists() and nuevo.file_path.exists()

    def test_nunca_purga_un_lote_con_ordenes_sin_enviar_por_viejo_que_sea(self):
        # Incluida la cola del modo simple: sus preguntas pendientes son lo único que el usuario no ha visto respondido.
        sin_enviar = _job(2)
        sin_enviar.messages[0].status = STATUS_COMPLETED
        sin_enviar.save()
        cola = _job(1)
        cola.etiqueta = "cola"
        cola.save()
        self._envejecer(sin_enviar, 400)
        self._envejecer(cola, 400)
        assert purgar_jobs_antiguos(30) == 0
        assert sin_enviar.file_path.exists() and cola.file_path.exists()

    def test_si_se_pide_purga_tambien_lo_pendiente(self):
        j = _job(1)
        j.save()
        self._envejecer(j, 400)
        assert purgar_jobs_antiguos(30, conservar_pendientes=False) == 1

    def test_un_lote_con_fallidas_pero_nada_pendiente_si_caduca(self):
        # Lo fallido se puede reintentar mientras dure la retención; después no debe acumularse para siempre.
        j = _job(1)
        j.messages[0].status = STATUS_FAILED
        j.save()
        self._envejecer(j, 400)
        assert purgar_jobs_antiguos(30) == 1

    def test_un_fichero_de_lote_corrupto_y_viejo_se_borra(self):
        j = self._terminado()
        j.file_path.write_text("{no es json", encoding="utf-8")
        self._envejecer(j, 400)
        assert purgar_jobs_antiguos(30) == 1

    def test_purga_no_toca_el_lote_protegido(self):
        # Sin esto, un lote dejado a medias más días que la retención se volvía irreanudable.
        en_curso = self._terminado()
        self._envejecer(en_curso, 90)
        assert purgar_jobs_antiguos(30, proteger=en_curso.job_id) == 0
        assert en_curso.file_path.exists()
        assert purgar_jobs_antiguos(30, proteger=[en_curso.job_id]) == 0

    def test_retencion_cero_no_borra_nunca(self):
        j = _job(1)
        j.save()
        self._envejecer(j, 9999)
        assert purgar_jobs_antiguos(0) == 0 and j.file_path.exists()

    def test_la_purga_ignora_ficheros_ajenos(self):
        ajeno = paths.RESPONSES_DIR / "notas.txt"
        ajeno.write_text("mío", encoding="utf-8")
        os.utime(ajeno, (0, 0))
        purgar_jobs_antiguos(1)
        assert ajeno.exists()


def test_jobmessage_desde_dict_tolera_basura():
    m = JobMessage.desde_dict({"id": "4", "text": 5, "tokens_input": "x", "coste_usd": "no"})
    assert (m.id, m.text, m.tokens_input, m.coste_usd) == (4, "5", 0, D(0))


class TestColaViva:
    """La cola del modo simple crece mientras se envía."""

    def test_agregar_orden_asigna_el_siguiente_id_y_guarda(self):
        j = _job(2)
        n = j.agregar_orden("tercera")
        assert (n.id, n.text, n.status) == (3, "tercera", STATUS_PENDING)
        assert Job.load(j.job_id).total_count() == 3

    def test_agregar_a_un_lote_vacio(self):
        j = Job.crear([], Configuracion())
        assert j.agregar_orden("primera").id == 1

    def test_ids_unicos_con_hilos_a_la_vez(self):
        import threading
        j = Job.crear([], Configuracion())
        hilos = [threading.Thread(target=lambda: [j.agregar_orden("x") for _ in range(10)]) for _ in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        ids = [m.id for m in j.messages]
        assert len(ids) == 80 and len(set(ids)) == 80

    def test_la_etiqueta_se_guarda_y_filtra_el_listado(self):
        cola = Job.crear([], Configuracion(), etiqueta="cola")
        cola.agregar_orden("hola")
        lote = _job(1)
        lote.save()
        assert Job.load(cola.job_id).etiqueta == "cola"
        assert [d["job_id"] for d in listar_jobs(etiqueta="cola")] == [cola.job_id]
        assert [d["job_id"] for d in listar_jobs(etiqueta="")] == [lote.job_id]
        assert len(listar_jobs()) == 2
