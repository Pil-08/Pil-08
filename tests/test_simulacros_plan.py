"""El motor del plan de Claude contra un `claude` de mentira (ver claude_falso.py).

El `claude` falso imprime lo mismo que el de verdad (copiado de una ejecución real) y se programa por pregunta:
`[[plan:limite(2),ok]]` = la primera vez el plan dice que no hay más hasta dentro de 2 s; la segunda responde bien.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from claude_batch_monitor import paths
from claude_batch_monitor.core import plan as modulo_plan
from claude_batch_monitor.core.base import Callbacks, StopReason
from claude_batch_monitor.core.plan import PlanEngine
from claude_batch_monitor.job import STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING, Job
from claude_batch_monitor.state import EstadoMonitor

from .simulacros_comun import configuracion, correr

pytestmark = pytest.mark.simulacro

TRES = [f"Pregunta del plan {i}" for i in range(1, 4)]


def plan(**kw):
    return configuracion(modo="plan", **kw)


def respuesta_de(c, n):
    return c.job.response_path(n, c.job.ciclos_completados + 1).read_text(encoding="utf-8")


def vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Un proceso terminado cuyo padre no lo ha recogido sigue existiendo como «zombi»: se mira su estado.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


class TestCaminoFeliz:
    def test_cada_pregunta_se_responde_una_vez_y_se_guarda(self, claude, esperas):
        c = correr(TRES, plan())
        assert c.razon is StopReason.COMPLETED and c.completadas == 3, (c.razon, c.errores)
        for m in c.job.messages:
            assert f"Respuesta a: {m.text}" in respuesta_de(c, m.id)
        assert len(claude.preguntas()) == 3
        assert all(m.status == STATUS_COMPLETED and m.coste_usd == 0 for m in c.job.messages)

    def test_no_cuesta_dolares_pero_si_cuenta_los_tokens(self, claude, esperas):
        c = correr(["Una [[plan:ok(77)]]"], plan())
        m = c.job.mensaje(1)
        assert m.coste_usd == 0 and m.tokens_output == 77 and m.tokens_input > 0
        assert EstadoMonitor.cargar().gasto_acumulado_usd == 0 and c.motor.gasto_actual() == 0

    def test_la_respuesta_llega_escribiendose(self, claude, esperas):
        trozos = []
        c = correr(["Una pregunta algo larga para que haya varios trozos"],
                   plan(), callbacks=Callbacks(delta=lambda i, t: trozos.append((i, t))))
        assert c.completadas == 1
        assert len(trozos) > 1 and all(i == 1 for i, _ in trozos)
        assert "".join(t for _, t in trozos) == "Respuesta a: Una pregunta algo larga para que haya varios trozos"[:72]

    def test_una_pregunta_que_se_anade_con_el_motor_en_marcha_se_responde_sin_reiniciarlo(self, claude, esperas):
        def resultado(msg):
            if msg.id == 1:
                c_job.agregar_orden("Añadida a mitad")

        config = plan()
        c_job = Job.crear(["Primera"], config)
        c_job.save()
        c = correr([], config, job=c_job, callbacks=Callbacks(resultado=resultado))
        assert c.completadas == 2 and c.razon is StopReason.COMPLETED


class TestQueSeLanza:
    def test_las_opciones_la_entrada_y_la_carpeta(self, claude, esperas):
        c = correr(["¿Qué es un token?"], plan(modelo_plan="sonnet", system_prompt="Responde como un pirata"))
        assert c.completadas == 1
        ll = claude.preguntas()[0]
        assert ll["argv"][:5] == ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        for opcion in ("--no-session-persistence", "--disable-slash-commands", "--safe-mode"):
            assert opcion in ll["argv"]
        assert ll["valores"]["--tools"] == "" and ll["valores"]["--model"] == "sonnet"
        assert ll["pregunta"] == "¿Qué es un token?"                # por la entrada: nada de la persona en la línea de órdenes
        assert "¿Qué es un token?" not in " ".join(ll["argv"]) and "pirata" not in " ".join(ll["argv"])
        assert ll["sistema"].startswith("You are Claude") and "pirata" in ll["sistema"]
        assert ll["cwd"] == str(paths.DATA_DIR / "plan")           # una carpeta vacía y propia

    def test_sin_modelo_elegido_usa_el_del_plan(self, claude, esperas):
        correr(["Una"], plan())
        assert "--model" not in claude.preguntas()[0]["argv"]

    def test_una_clave_de_api_en_el_entorno_nunca_llega_a_claude(self, claude, esperas, monkeypatch):
        """`claude -p` usa SIEMPRE la clave si la ve: eso sería pagar por token en vez de gastar el plan."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-secreta")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "otra-cosa")
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        c = correr(TRES, plan())
        assert c.completadas == 3
        llamadas = claude.llamadas()
        assert llamadas and all(not any(x["env"].values()) for x in llamadas), "ni una sola llamada vio esas variables"

    def test_una_pregunta_que_empieza_por_barra_no_se_toma_por_un_comando(self, claude, esperas):
        correr(["/usage dime cuánto llevo"], plan())
        ll = claude.preguntas()[0]
        assert "--disable-slash-commands" in ll["argv"] and ll["pregunta"] == "/usage dime cuánto llevo"

    def test_si_esa_version_no_sabe_desactivar_los_comandos_la_pregunta_va_protegida(self, claude, esperas, monkeypatch):
        monkeypatch.setenv("CLAUDE_FALSO_SIN", "--disable-slash-commands")
        c = correr(["/usage dime cuánto llevo"], plan())
        assert c.completadas == 1
        assert claude.preguntas()[0]["pregunta"] == "Pregunta: /usage dime cuánto llevo"

    def test_las_opciones_que_esta_version_no_conoce_se_quitan_y_se_sigue(self, claude, esperas, monkeypatch):
        monkeypatch.setenv("CLAUDE_FALSO_SIN", "--safe-mode,--tools,--include-partial-messages")
        c = correr(TRES, plan())
        assert c.razon is StopReason.COMPLETED and c.completadas == 3, (c.razon, c.errores)
        rechazadas = [x["rechazo"] for x in claude.llamadas() if x.get("modo") == "rechazada"]
        assert sorted(rechazadas) == ["--include-partial-messages", "--safe-mode", "--tools"]  # una sola vez cada una
        final = claude.preguntas()[-1]["argv"]
        assert "--safe-mode" not in final and "--tools" not in final
        assert len(claude.preguntas()) == 3                      # y ninguna pregunta se perdió ni se repitió

    def test_si_falta_el_archivo_de_sistema_las_instrucciones_viajan_con_la_pregunta(self, claude, esperas, monkeypatch):
        monkeypatch.setenv("CLAUDE_FALSO_SIN", "--system-prompt-file")
        c = correr(["Una"], plan(system_prompt="Contesta en una frase"))
        assert c.completadas == 1
        pregunta = claude.preguntas()[0]["pregunta"]
        assert "Contesta en una frase" in pregunta and pregunta.endswith("Una")


class TestLimiteDelPlan:
    def test_espera_a_la_hora_que_dice_claude_y_sigue_sola_sin_perder_nada(self, claude, esperas, sin_margen):
        motivos, estados, pedidas = [], [], []
        t0 = time.time()
        c = correr(["Una [[plan:limite(2),ok]]", "Otra"], plan(), limite_s=60,
                   callbacks=Callbacks(espera=lambda m, s: (motivos.append(m), pedidas.append(s)),
                                       estado=lambda t, o: estados.append(t)))
        assert c.razon is StopReason.COMPLETED and c.completadas == 2, (c.razon, c.errores)
        assert claude.veces("Una") == 2 and claude.veces("Otra") == 1           # la rechazada se repitió, una vez
        assert "plan" in motivos and any("límite de tu plan" in e for e in estados)
        # «Vuelve dentro de 2 s», pero la hora que da Claude es un segundo entero (se corta hacia abajo): faltan entre 1 y 2 s.
        assert pedidas and 0.9 <= pedidas[0] <= 2.1, pedidas
        assert time.time() - t0 >= pedidas[0] - 0.05, "debía esperar a que volviera el acceso"
        assert "Respuesta a: Una" in respuesta_de(c, 1)
        assert EstadoMonitor.cargar().plan_bloqueado_hasta == ""                  # y al volver, ya no hay bloqueo

    def test_el_limite_semanal_se_espera_igual_y_se_nombra(self, claude, esperas, sin_margen):
        estados = []
        c = correr(["Una [[plan:semanal(1),ok]]"], plan(), limite_s=60, callbacks=Callbacks(estado=lambda t, o: estados.append(t)))
        assert c.razon is StopReason.COMPLETED and c.completadas == 1
        assert any("la semana" in e for e in estados)

    def test_un_limite_solo_con_texto_se_entiende_por_la_hora_que_trae(self, claude, esperas, monkeypatch):
        """Sin el evento de límite, solo queda «resets 3:47pm»: minuto a minuto, a la hora de este equipo."""
        objetivos = []

        def esperar_hasta(self, instante, cb):
            objetivos.append(instante)
            return True

        monkeypatch.setattr(PlanEngine, "_esperar_hasta", esperar_hasta)
        c = correr(["Una [[plan:limite_texto(150),ok]]"], plan(), limite_s=60)
        assert c.razon is StopReason.COMPLETED and claude.veces("Una") == 2
        assert len(objetivos) == 1
        falta = (objetivos[0] - datetime.now(timezone.utc)).total_seconds() - modulo_plan.MARGEN_TRAS_EL_REINICIO_SEC
        assert 80 <= falta <= 160, falta                # 150 s menos lo que se pierde al redondear al minuto

    def test_un_limite_sin_hora_de_vuelta_se_reintenta_con_esperas_crecientes(self, claude, esperas, monkeypatch):
        objetivos = []

        def esperar_hasta(self, instante, cb):
            objetivos.append((instante - datetime.now(timezone.utc)).total_seconds())
            return True

        monkeypatch.setattr(PlanEngine, "_esperar_hasta", esperar_hasta)
        c = correr(["Una [[plan:limite_sin_hora,limite_sin_hora,ok]]"], plan(), limite_s=60)
        assert c.razon is StopReason.COMPLETED and claude.veces("Una") == 3
        assert 290 <= objetivos[0] <= 301 and 590 <= objetivos[1] <= 601         # 5 min, luego 10

    def test_si_nunca_dice_cuando_vuelve_acaba_por_rendirse_y_lo_cuenta(self, claude, esperas, monkeypatch):
        monkeypatch.setattr(PlanEngine, "_esperar_hasta", lambda self, instante, cb: True)
        monkeypatch.setattr(modulo_plan, "MAX_ESPERAS_SIN_REINICIO", 3)
        c = correr(["Una [[plan:limite_sin_hora]]"], plan(), limite_s=60)
        assert c.razon is StopReason.FATAL and c.pendientes == 1
        assert claude.veces("Una") == 4 and any("no cuándo vuelve" in e for e in c.errores)

    def test_un_limite_que_no_termina_de_levantarse_no_se_insiste_cada_medio_minuto(self, claude, esperas, monkeypatch):
        pausas = []

        def esperar_hasta(self, instante, cb):
            pausas.append((instante - datetime.now(timezone.utc)).total_seconds())
            return True

        monkeypatch.setattr(PlanEngine, "_esperar_hasta", esperar_hasta)
        monkeypatch.setattr(modulo_plan, "MARGEN_TRAS_EL_REINICIO_SEC", 0.0)
        # Dice que vuelve dentro de «-30 s» (ya pasó) tres veces seguidas: las esperas deben ir creciendo.
        c = correr(["Una [[plan:limite(-30),limite(-30),limite(-30),ok]]"], plan(), limite_s=60)
        assert c.razon is StopReason.COMPLETED
        assert pausas[0] < pausas[1] < pausas[2], pausas

    def test_el_bloqueo_se_recuerda_en_disco_y_otro_motor_no_vuelve_a_preguntar_antes_de_tiempo(
            self, claude, esperas, sin_margen):
        config = plan()
        job = Job.crear(["Una [[plan:limite(3),ok]]"], config)
        job.save()
        parar = {}

        def espera(motivo, segundos):                       # el primer motor se cierra en plena espera
            if motivo == "plan" and "m" in parar:
                parar["m"].detener()

        c1 = correr([], config, job=job, antes=lambda m: parar.update(m=m), callbacks=Callbacks(espera=espera))
        assert c1.razon is StopReason.STOPPED and c1.pendientes == 1
        assert EstadoMonitor.cargar().plan_bloqueado_hasta, "debía quedar anotado hasta cuándo no hay acceso"
        antes_de_reabrir = time.time()
        c2 = correr([], config, job=Job.load(job.job_id), limite_s=60)
        assert c2.razon is StopReason.COMPLETED and c2.completadas == 1
        asks = [x["t"] for x in claude.preguntas()]
        assert len(asks) == 2 and asks[1] - asks[0] >= 2.0, "al reabrir debía esperar el resto del bloqueo"
        assert time.time() - antes_de_reabrir >= 1.0


class TestLoQueNoSeEspera:
    def test_gastar_creditos_extra_para_en_el_acto_pero_no_pierde_la_respuesta(self, claude, esperas):
        c = correr(["Primera [[plan:extra]]", "Segunda"], plan())
        assert c.razon is StopReason.EXTRA_USAGE
        assert c.estado_de(1) == STATUS_COMPLETED and "Respuesta a: Primera" in respuesta_de(c, 1)
        assert c.estado_de(2) == STATUS_PENDING and claude.veces("Segunda") == 0
        assert any("créditos extra" in e for e in c.errores)

    @pytest.mark.parametrize("paso,razon,fragmento", [
        ("creditos", StopReason.NO_CREDIT, "no los gasta"),
        ("gasto", StopReason.ACCOUNT_LIMIT, "límite de gasto"),
        ("auth", StopReason.FATAL, "claude auth login"),
        ("cuenta", StopReason.FATAL, "no puede usar Claude Code"),
    ])
    def test_los_problemas_de_la_cuenta_paran_con_un_motivo_claro_y_la_pregunta_sigue_pendiente(
            self, claude, esperas, paso, razon, fragmento):
        c = correr([f"Una [[plan:{paso}]]", "Otra"], plan(), limite_s=60)
        assert c.razon is razon, (c.razon, c.errores)
        assert any(fragmento in e for e in c.errores), c.errores
        assert c.estado_de(1) == STATUS_PENDING and c.estado_de(2) == STATUS_PENDING
        assert claude.veces("Otra") == 0 and claude.veces("Una") == 1       # no se insiste

    def test_un_modelo_que_el_plan_no_tiene_para_y_marca_la_pregunta(self, claude, esperas):
        c = correr(["Una [[plan:modelo]]"], plan(modelo_plan="opus"))
        assert c.razon is StopReason.FATAL and c.estado_de(1) == STATUS_FAILED
        assert c.job.mensaje(1).error_type == "model_not_found" and "opus" in c.job.mensaje(1).detalle

    def test_una_pregunta_que_claude_rechaza_falla_sola_y_las_demas_siguen(self, claude, esperas):
        c = correr(["Mala [[plan:invalido]]", "Buena"], plan())
        assert c.razon is StopReason.COMPLETED
        assert c.estado_de(1) == STATUS_FAILED and c.job.mensaje(1).error_type == "bad_request"
        assert c.estado_de(2) == STATUS_COMPLETED

    def test_un_servidor_saturado_se_reintenta_con_espera_y_se_cuenta_el_fallo(self, claude, esperas):
        c = correr(["Una [[plan:saturado,error500,ok]]"], plan(max_error_attempts=4))
        assert c.razon is StopReason.COMPLETED and c.completadas == 1
        assert c.job.mensaje(1).errores == 2 and claude.veces("Una") == 3

    @pytest.mark.parametrize("paso", ["saturado", "crash", "basura", "sin_resultado", "error500"])
    def test_si_no_se_arregla_nunca_se_rinde_tras_los_intentos_permitidos(self, claude, esperas, paso):
        c = correr([f"Una [[plan:{paso}]]", "Otra"], plan(max_error_attempts=3), limite_s=60)
        assert c.razon is StopReason.FATAL and c.estado_de(1) == STATUS_FAILED
        assert claude.veces("Una") == 3 and claude.veces("Otra") == 0
        assert c.job.mensaje(1).error_type == "max_retries"

    def test_una_respuesta_vacia_se_guarda_con_el_aviso_de_siempre(self, claude, esperas):
        c = correr(["Una [[plan:vacio]]"], plan())
        assert c.completadas == 1 and "respuesta vacía" in respuesta_de(c, 1)

    def test_una_respuesta_cortada_por_max_tokens_se_marca(self, claude, esperas):
        c = correr(["Una [[plan:trunca]]"], plan())
        assert c.job.mensaje(1).truncated and "truncada" in respuesta_de(c, 1)

    def test_una_negativa_del_modelo_se_guarda_como_rechazada(self, claude, esperas):
        c = correr(["Una [[plan:rechazo]]"], plan())
        assert c.estado_de(1) == "refused" and "se negó" in respuesta_de(c, 1)

    def test_una_respuesta_enorme_llega_entera(self, claude, esperas):
        c = correr(["Una [[plan:grande(600)]]"], plan())
        assert c.completadas == 1 and respuesta_de(c, 1).count("x") == 600 * 1024


class TestAntesDeEmpezar:
    def test_sin_claude_code_lo_explica_y_no_lanza_nada(self, claude, esperas, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_MONITOR_CLAUDE", str(tmp_path / "no-existe"))
        c = correr(TRES, plan())
        assert c.razon is StopReason.FATAL and c.pendientes == 3
        assert any("No encuentro Claude Code" in e for e in c.errores)
        assert not claude.llamadas()

    def test_sin_sesion_iniciada_lo_explica(self, claude, esperas, monkeypatch):
        monkeypatch.setenv("CLAUDE_FALSO_AUTH", "none")
        c = correr(TRES, plan())
        assert c.razon is StopReason.FATAL and c.pendientes == 3
        assert any("claude auth login" in e for e in c.errores) and not claude.preguntas()

    @pytest.mark.parametrize("auth", ["api_key", "helper"])
    def test_si_claude_code_esta_iniciado_con_una_clave_de_api_no_lo_usa(self, claude, esperas, monkeypatch, auth):
        monkeypatch.setenv("CLAUDE_FALSO_AUTH", auth)
        c = correr(TRES, plan())
        assert c.razon is StopReason.FATAL and c.pendientes == 3
        assert any("se paga por uso" in e for e in c.errores) and not claude.preguntas()

    def test_si_claude_dice_en_el_inicio_que_usa_una_clave_se_corta_antes_de_que_responda(
            self, claude, esperas):
        t0 = time.time()
        c = correr(["Una [[plan:clave]]", "Otra"], plan(), limite_s=60)
        assert time.time() - t0 < 15, "debía cortarlo en cuanto lo supo, no esperar a que respondiera"
        assert c.razon is StopReason.FATAL and c.pendientes == 2
        assert any("clave de API" in e for e in c.errores)
        assert not vivo(claude.preguntas()[0]["pid"])

    def test_una_version_demasiado_antigua_se_dice(self, claude, esperas, monkeypatch):
        monkeypatch.setenv("CLAUDE_FALSO_VERSION", "1.0.88")
        c = correr(TRES, plan())
        assert c.razon is StopReason.FATAL and any("claude update" in e for e in c.errores)

    def test_una_version_que_no_sabe_decir_su_sesion_se_prueba_igualmente(self, claude, esperas, monkeypatch):
        monkeypatch.setenv("CLAUDE_FALSO_AUTH", "antiguo")
        c = correr(TRES, plan())
        assert c.razon is StopReason.COMPLETED and c.completadas == 3


class TestPararYMatar:
    def test_detener_mata_a_claude_en_el_acto_y_la_pregunta_sigue_pendiente(self, claude, esperas):
        parar = {}

        def inicio(msg):
            threading.Timer(1.0, lambda: parar["m"].detener()).start()

        t0 = time.time()
        c = correr(["Una [[plan:lento(60)]]", "Otra"], plan(), limite_s=30, antes=lambda m: parar.update(m=m),
                   callbacks=Callbacks(inicio_orden=inicio))
        assert time.time() - t0 < 12, "no debía esperar a que Claude terminara de responder"
        assert c.razon is StopReason.STOPPED and c.pendientes == 2
        assert not vivo(claude.preguntas()[0]["pid"])

    def test_si_claude_no_contesta_a_tiempo_se_mata_y_se_vuelve_a_preguntar(self, claude, esperas, monkeypatch):
        monkeypatch.setattr(modulo_plan, "MINIMO_TIMEOUT_SEC", 1.0)
        c = correr(["Una [[plan:cuelga(60),ok]]"], plan(timeout_api_sec=1), limite_s=60)
        assert c.razon is StopReason.COMPLETED and c.completadas == 1
        primera = claude.preguntas()[0]
        assert claude.veces("Una") == 2 and not vivo(primera["pid"]), "el que se colgó debía haber muerto"
        assert c.job.mensaje(1).errores == 1

    def test_una_respuesta_larga_que_va_llegando_no_se_mata_aunque_pase_del_tiempo_de_espera(self, claude, esperas, monkeypatch):
        """El tiempo de espera es de silencio, no de duración: matar a mitad lo que ya se está gastando sería perderlo."""
        monkeypatch.setattr(modulo_plan, "MINIMO_TIMEOUT_SEC", 1.0)
        t0 = time.time()
        c = correr(["Larga [[plan:goteo(4)]]"], plan(timeout_api_sec=1), limite_s=60)
        assert time.time() - t0 > 3.5                                  # tardó más que el tiempo de espera…
        assert c.razon is StopReason.COMPLETED and c.completadas == 1  # …y aun así acabó
        assert claude.veces("Larga") == 1 and c.job.mensaje(1).errores == 0

    def test_si_lo_que_llega_se_corta_de_golpe_se_mata_y_se_reintenta(self, claude, esperas, monkeypatch):
        monkeypatch.setattr(modulo_plan, "MINIMO_TIMEOUT_SEC", 1.0)
        c = correr(["Una [[plan:cuelga(60),goteo(2)]]"], plan(timeout_api_sec=1), limite_s=60)
        assert c.razon is StopReason.COMPLETED and claude.veces("Una") == 2

    def test_hay_un_tope_en_total_aunque_siga_llegando_algo(self, claude, esperas, monkeypatch):
        monkeypatch.setattr(modulo_plan, "MINIMO_TIMEOUT_SEC", 1.0)
        monkeypatch.setattr(modulo_plan, "TOPE_TOTAL_SEC", 3.0)
        t0 = time.time()
        c = correr(["Goteo eterno [[plan:goteo(30),goteo(1)]]"], plan(timeout_api_sec=1, max_error_attempts=3), limite_s=60)
        assert time.time() - t0 < 20                                       # no esperó a que el goteo acabara (30 s)
        assert claude.veces("Goteo eterno") == 2 and c.completadas == 1    # la primera se cortó en el tope; la segunda cabía
        assert not vivo(claude.preguntas()[0]["pid"])

    def test_no_queda_ningun_proceso_de_claude_vivo_al_terminar(self, claude, esperas):
        correr(TRES, plan())
        assert not any(vivo(x["pid"]) for x in claude.llamadas())


class TestLaFranjaHoraria:
    def test_fuera_de_la_franja_espera_y_dentro_trabaja(self, claude, esperas, monkeypatch):
        ahora = datetime.now()
        fuera = f"{(ahora + timedelta(hours=2)):%H:%M}-{(ahora + timedelta(hours=3)):%H:%M}"
        dentro = f"{(ahora - timedelta(hours=1)):%H:%M}-{(ahora + timedelta(hours=1)):%H:%M}"
        motivos = []
        parar = {}

        def espera(motivo, segundos):
            motivos.append(motivo)
            if "m" in parar:
                parar["m"].detener()

        c = correr(TRES, plan(franja=fuera), antes=lambda m: parar.update(m=m), callbacks=Callbacks(espera=espera))
        assert c.razon is StopReason.STOPPED and motivos == ["franja"] and not claude.preguntas()
        c2 = correr(TRES, plan(franja=dentro))
        assert c2.razon is StopReason.COMPLETED and c2.completadas == 3


class TestVariasHerramientasAlaVez:
    def test_doce_preguntas_con_un_limite_en_medio(self, claude, esperas, sin_margen):
        preguntas = [f"P{i:02d}" for i in range(1, 13)]
        preguntas[5] = "P06 [[plan:limite(2),ok]]"
        c = correr(preguntas, plan(), limite_s=90)
        assert c.razon is StopReason.COMPLETED and c.completadas == 12
        assert claude.veces("P06") == 2
        assert all(claude.veces(f"P{i:02d}") == 1 for i in range(1, 13) if i != 6)


def test_el_motor_no_se_cuelga_si_el_programa_no_se_puede_ejecutar(claude, esperas, tmp_path, monkeypatch):
    """Un archivo `claude` que existe pero no se puede ejecutar de verdad (formato roto): error claro, sin colgarse."""
    roto = tmp_path / "roto"
    roto.write_bytes(b"\x00\x01\x02 no soy un programa")
    roto.chmod(0o755)
    monkeypatch.setenv("CLAUDE_MONITOR_CLAUDE", str(roto))
    c = correr(TRES, plan(), limite_s=60)
    assert c.razon is StopReason.FATAL and c.pendientes == 3
