"""Lo que toca al plan de Claude fuera de su motor: configuración, estimación, lote, cola, instalación y terminal.

El motor del plan (y la ventana) tienen sus propias pruebas: `test_simulacros_plan.py`, `test_claude_code.py`,
`test_gui_plan.py`. Aquí se comprueba que el resto del programa sabe qué hacer cuando el modo es `plan`.
"""

import json
import sys
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from claude_batch_monitor import cli, instalacion
from claude_batch_monitor.cola import Cola, EstadoCola, EventosCola
from claude_batch_monitor.config import (
    MODELO_DEL_PLAN,
    MODELO_PLAN_VALIDO,
    MODOS,
    ConfigManager,
    Configuracion,
)
from claude_batch_monitor.core.base import StopReason
from claude_batch_monitor.core.batch import BatchEngine
from claude_batch_monitor.core.plan import PlanEngine
from claude_batch_monitor.core.sequential import SequentialEngine
from claude_batch_monitor.ejecutor import motor_para
from claude_batch_monitor.estimator import estimar
from claude_batch_monitor.job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    Job,
    listar_jobs,
)

from .test_cola import Registro, fabrica_con, hasta
from .test_engine import FakeResponse


def plan(**kw):
    return Configuracion(modo="plan", **kw)


# ----------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------


class TestConfiguracion:
    def test_los_modos(self):
        assert MODOS == ("inmediato", "lote", "plan")
        assert plan().es_plan and not plan().es_lote and not Configuracion().es_plan

    def test_el_nombre_del_modelo_con_el_plan(self):
        assert plan().nombre_de_modelo == MODELO_DEL_PLAN
        assert plan(modelo_plan="opus").nombre_de_modelo == "opus"
        assert Configuracion(modelo="claude-haiku-4-5", modelo_plan="opus").nombre_de_modelo == "claude-haiku-4-5"

    def test_un_plan_normal_no_da_ni_errores_ni_avisos(self):
        v = plan(modelo_plan="sonnet", franja="22:00-07:00").validar()
        assert v.ok and v.avisos == []

    def test_con_el_plan_no_se_revisa_lo_que_es_solo_de_la_api(self):
        v = plan(max_tokens=0, tope_gasto_usd=D("-1"), modelo="", esfuerzo="inventado").validar()
        assert v.ok
        assert not Configuracion(max_tokens=0).validar().ok          # con la API, sí

    @pytest.mark.parametrize("modelo", ["sonnet; rm -rf /", "a b", "$(x)", "--help", "ñ", "a" * 100])
    def test_un_modelo_con_pinta_rara_no_vale(self, modelo):
        v = plan(modelo_plan=modelo).validar()
        assert not v.ok and "modelo" in v.errores[0].lower()

    def test_el_bucle_no_existe_con_el_plan(self):
        v = plan(modo_continuo=True).validar()
        assert not v.ok and "bucle" in v.errores[0].lower()

    def test_pedir_varias_a_la_vez_solo_avisa(self):
        v = plan(concurrencia=4).validar()
        assert v.ok and any("de una en una" in a for a in v.avisos)

    @pytest.mark.parametrize("modo", MODOS)
    def test_una_franja_mala_es_un_error_en_cualquier_modo(self, modo):
        v = Configuracion(modo=modo, franja="de noche").validar()
        assert not v.ok and any("franja" in e.lower() for e in v.errores)

    def test_un_modo_desconocido_es_un_error(self):
        assert not Configuracion(modo="turbo").validar().ok

    def test_se_guarda_y_se_lee(self, tmp_path):
        c = plan(modelo_plan="opus", ruta_claude="/x/claude", franja="22:00-07:00", system_prompt="Sé breve")
        ruta = tmp_path / "config.json"
        ConfigManager.guardar(c, ruta)
        leida = ConfigManager.cargar(ruta)
        assert (leida.modo, leida.modelo_plan, leida.ruta_claude, leida.franja) == ("plan", "opus", "/x/claude", "22:00-07:00")
        assert leida == c

    def test_una_configuracion_de_antes_se_lee_con_valores_por_omision(self):
        c = Configuracion.desde_dict({"modelo": "claude-haiku-4-5", "modo": "lote", "tope_gasto_usd": "3"})
        assert (c.modo, c.modelo_plan, c.ruta_claude, c.franja) == ("lote", "", "", "")

    @pytest.mark.parametrize("valor", [None, 5, True, ["a"], {"a": 1}, "  22:00-07:00  "])
    def test_valores_raros_en_los_campos_nuevos_no_rompen_la_lectura(self, valor):
        c = Configuracion.desde_dict({"modo": "plan", "modelo_plan": valor, "franja": valor, "ruta_claude": valor})
        assert c.es_plan and isinstance(c.modelo_plan, str) and isinstance(c.franja, str)

    def test_un_modo_inventado_en_el_archivo_vuelve_al_de_siempre(self):
        assert Configuracion.desde_dict({"modo": "turbo"}).modo == "inmediato"
        assert Configuracion.desde_dict({"modo": "PLAN"}).modo == "inmediato"       # ni se adivina ni se acepta a medias


@given(texto=st.text(max_size=100))
def test_lo_que_vale_como_modelo_no_puede_ser_una_opcion_ni_llevar_espacios_ni_saltos(texto):
    if MODELO_PLAN_VALIDO.match(texto):
        assert not texto.startswith("-") and not any(c.isspace() for c in texto) and "\n" not in texto
        assert all(c not in texto for c in ";&|$`'\"\\<>(){}*?~!#")


# ----------------------------------------------------------------------
# Estimación y lote guardado
# ----------------------------------------------------------------------


class TestEstimacion:
    def test_con_el_plan_no_hay_coste_ni_se_cuenta_con_la_api(self):
        class Cliente:
            def __getattr__(self, nombre):
                raise AssertionError("con el plan no se habla con la API")

        e = estimar(plan(), ["una pregunta", "otra"], client=Cliente())
        assert e.plan and e.coste_maximo == 0 and e.ordenes == 2 and e.tokens_entrada > 0 and not e.exacta
        texto = "\n".join(e.resumen_lineas())
        assert "incluido en tu plan" in texto and "Coste MÁXIMO" not in texto and "$" not in texto

    def test_con_la_api_sigue_igual(self):
        e = estimar(Configuracion(), ["una pregunta"])
        assert not e.plan and e.coste_maximo > 0 and any("Coste MÁXIMO" in x for x in e.resumen_lineas())


class TestElLote:
    def test_guarda_el_modo_y_el_modelo_con_el_plan(self):
        j = Job.crear(["a", "b"], plan(modelo_plan="opus"))
        assert (j.modo, j.modelo) == ("plan", "opus")
        assert Job.crear(["a"], plan()).modelo == MODELO_DEL_PLAN
        assert Job.crear(["a"], Configuracion(modelo="claude-haiku-4-5")).modelo == "claude-haiku-4-5"

    def test_sobrevive_a_guardarse_y_leerse(self):
        j = Job.crear(["a"], plan(modelo_plan="opus"))
        j.save()
        leido = Job.load(j.job_id)
        assert (leido.modo, leido.modelo) == ("plan", "opus")
        assert [x for x in listar_jobs() if x["job_id"] == j.job_id][0]["modo"] == "plan"

    def test_avisa_si_se_reanuda_con_otra_cosa(self):
        j = Job.crear(["a"], plan(modelo_plan="opus"))
        assert j.incompatibilidades(plan(modelo_plan="opus")) == []
        avisos = " ".join(j.incompatibilidades(Configuracion(modelo="claude-haiku-4-5")))
        assert "se paga por token" in avisos and "plan" in avisos
        assert any("opus" in a for a in j.incompatibilidades(plan(modelo_plan="sonnet")))


# ----------------------------------------------------------------------
# El motor que toca
# ----------------------------------------------------------------------


@pytest.mark.parametrize("config, clase", [
    (Configuracion(), SequentialEngine), (Configuracion(modo="lote"), BatchEngine), (plan(), PlanEngine),
])
def test_motor_para(config, clase):
    assert type(motor_para(config, Job.crear(["a"], config))) is clase


# ----------------------------------------------------------------------
# Instalación: con el plan no hace falta la librería de la API
# ----------------------------------------------------------------------


class TestUsaElPlan:
    def test_leer_una_opcion_de_la_linea(self):
        assert instalacion._valor_de(["--cli", "--modo", "plan"], "--modo") == "plan"
        assert instalacion._valor_de(["--cli", "--modo=lote"], "--modo") == "lote"
        assert instalacion._valor_de(["--cli", "--modo"], "--modo") == ""
        assert instalacion._valor_de([], "--modo") == ""

    @pytest.mark.parametrize("argv, esperado", [
        (["--cli", "--modo", "plan", "--mensaje", "x"], True), (["--cli", "--modo=plan"], True),
        (["--cli", "--modo", "lote"], False), (["--cli", "--modo", "inmediato"], False), (["--cli", "--mensaje", "x"], False),
    ])
    def test_en_la_terminal_lo_dice_la_opcion(self, argv, esperado):
        assert instalacion.usa_el_plan(argv, terminal=True) is esperado
        assert instalacion.necesita_la_libreria(argv, terminal=True) is (not esperado and "--dry-run" not in argv)

    def test_al_reanudar_lo_dice_el_lote(self):
        j_plan, j_api = Job.crear(["a"], plan()), Job.crear(["a"], Configuracion())
        j_plan.save()
        j_api.save()
        assert instalacion.usa_el_plan(["--cli", "--reanudar", j_plan.job_id], terminal=True)
        assert instalacion.usa_el_plan(["--cli", f"--reanudar={j_plan.job_id}"], terminal=True)
        assert not instalacion.usa_el_plan(["--cli", "--reanudar", j_api.job_id], terminal=True)
        assert not instalacion.usa_el_plan(["--cli", "--reanudar", "noexiste"], terminal=True)

    def test_en_la_ventana_la_primera_vez_tampoco_hace_falta_porque_se_elige_alli(self):
        assert instalacion.usa_el_plan([], terminal=False)
        assert not instalacion.necesita_la_libreria([], terminal=False)

    def test_en_la_ventana_despues_manda_lo_guardado(self):
        ConfigManager.guardar(plan())
        assert instalacion.usa_el_plan([], terminal=False) and not instalacion.necesita_la_libreria([], terminal=False)
        ConfigManager.guardar(Configuracion())
        assert not instalacion.usa_el_plan([], terminal=False) and instalacion.necesita_la_libreria([], terminal=False)

    def test_una_configuracion_ilegible_se_trata_como_la_api(self, tmp_path):
        from claude_batch_monitor import paths

        paths.CONFIG_FILE.write_text("{ esto no es json", encoding="utf-8")
        assert instalacion.necesita_la_libreria([], terminal=False)


# ----------------------------------------------------------------------
# La cola del modo simple, con el plan
# ----------------------------------------------------------------------


class RegistroDelPlan(Registro):
    def __init__(self):
        super().__init__()
        self.planes = []

    def plan(self, estado):
        self.planes.append(estado)


class TestLaCola:
    def test_lo_que_se_envia_depende_de_si_es_el_plan(self):
        c = Cola(plan(modo_continuo=True, concurrencia=5, pausa_entre_mensajes_sec=30)).__dict__["config"]
        envio = Cola(c)._config_de_envio()
        assert envio.es_plan and envio.concurrencia == 1 and not envio.modo_continuo and envio.pausa_entre_mensajes_sec == 0
        assert Cola(Configuracion(modo="lote"))._config_de_envio().modo == "inmediato"       # (lote: de la ventana avanzada)

    def test_una_pregunta_se_responde_con_el_plan_y_la_cola_cuenta_el_uso(self, claude, esperas):
        ev = RegistroDelPlan()
        cola = Cola(plan(), ev)
        msg = cola.agregar("¿Qué es un token?")
        assert hasta(lambda: msg.status == STATUS_COMPLETED and not cola.activa, 30)
        assert "Respuesta a: ¿Qué es un token?" in cola.job.response_path(1, 1).read_text(encoding="utf-8")
        assert cola.gastado() == 0 and ev.planes and ev.planes[-1].sesion.uso is not None
        assert ev.estados[-1].tipo == "idle"
        assert (cola.job.modo, cola.job.modelo) == ("plan", MODELO_DEL_PLAN)

    def test_las_esperas_del_plan_llegan_con_su_motivo(self, claude, esperas, sin_margen):
        ev = RegistroDelPlan()
        cola = Cola(plan(), ev)
        msg = cola.agregar("Una [[plan:limite(2),ok]]")
        assert hasta(lambda: msg.status == STATUS_COMPLETED and not cola.activa, 40)
        esperando = [e for e in ev.estados if e.tipo == "waiting"]
        assert esperando and esperando[0].motivo == "plan" and 0 < esperando[0].segundos <= 3
        assert claude.veces("Una") == 2

    def test_el_uso_extra_para_la_cola_con_su_codigo_y_se_puede_reanudar(self, claude, esperas):
        ev = RegistroDelPlan()
        cola = Cola(plan(), ev)
        cola.agregar("Primera [[plan:extra,ok]]")
        cola.agregar("Segunda")
        assert hasta(lambda: cola.estado.tipo == "stopped", 30)
        e = cola.estado
        assert e.razon is StopReason.EXTRA_USAGE and e.codigo == "uso_extra"
        assert [m.status for m in cola.preguntas()] == [STATUS_COMPLETED, STATUS_PENDING]    # la respuesta recibida no se pierde
        cola.reanudar()
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa, 30)
        assert claude.veces("Segunda") == 1

    @pytest.mark.parametrize("marca, razon, codigo", [
        ("creditos", StopReason.NO_CREDIT, "creditos"), ("gasto", StopReason.ACCOUNT_LIMIT, "gasto"),
        ("auth", StopReason.FATAL, "auth"), ("cuenta", StopReason.FATAL, "cuenta"),
    ])
    def test_cada_problema_llega_con_su_codigo(self, claude, esperas, marca, razon, codigo):
        cola = Cola(plan())
        cola.agregar(f"Una [[plan:{marca}]]")
        assert hasta(lambda: cola.estado.tipo == "stopped", 30)
        assert cola.estado.razon is razon and cola.estado.codigo == codigo
        assert cola.pendientes() == 1                                       # la pregunta sigue en la cola

    def test_el_modelo_malo_marca_la_pregunta_y_reanudar_la_reintenta(self, claude, esperas):
        cola = Cola(plan(modelo_plan="modelo-que-no-existe"))
        cola.agregar("Una [[plan:modelo,ok]]")
        assert hasta(lambda: cola.estado.tipo == "stopped", 30)
        assert cola.estado.codigo == "modelo" and cola.preguntas()[0].status == STATUS_FAILED
        cola.reanudar()
        assert hasta(lambda: cola.preguntas()[0].status == STATUS_COMPLETED and not cola.activa, 30)

    def test_sin_claude_code_la_cola_se_para_diciendolo(self, esperas):
        cola = Cola(plan())
        cola.agregar("Una")
        assert hasta(lambda: cola.estado.tipo == "stopped", 30)
        assert cola.estado.codigo == "no_instalado" and "No encuentro Claude Code" in cola.ultimo_error

    def test_pausar_mata_la_pregunta_en_vuelo_y_sigue_pendiente(self, claude, esperas):
        cola = Cola(plan())
        cola.agregar("Lenta [[plan:lento(60),ok]]")
        assert hasta(lambda: claude.preguntas(), 20)
        cola.pausar()
        assert hasta(lambda: not cola.activa, 20)
        assert cola.pendientes() == 1 and cola.estado.tipo == "paused"
        cola.reanudar()
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa, 30)

    def test_cambiar_de_la_clave_al_plan_con_la_cola_ya_empezada_deja_el_lote_al_dia(self, claude, esperas):
        cola = Cola(Configuracion(modelo="claude-haiku-4-5", max_tokens=512, tope_gasto_usd=D("5")),
                    fabrica=fabrica_con([FakeResponse("de la API")] * 3))
        cola.agregar("Primera, con la clave")
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa)
        assert (cola.job.modo, cola.job.modelo) == ("inmediato", "claude-haiku-4-5")
        cola.actualizar_config(plan(modelo_plan="opus"))
        cola._fabrica = motor_para                                           # (en la ventana ya es el de siempre)
        cola.agregar("Segunda, con el plan")
        assert hasta(lambda: cola.pendientes() == 0 and not cola.activa, 30)
        assert (cola.job.modo, cola.job.modelo) == ("plan", "opus")
        guardado = Job.load(cola.job.job_id)
        assert (guardado.modo, guardado.modelo) == ("plan", "opus")          # y está en disco, no solo en memoria
        assert claude.preguntas()[0]["valores"]["--model"] == "opus"

    def test_el_evento_del_plan_por_omision_no_hace_nada(self):
        EventosCola().plan(object())
        assert EstadoCola().codigo == ""


# ----------------------------------------------------------------------
# La terminal, con el plan (en el mismo proceso; los procesos de verdad están en test_simulacros_procesos.py)
# ----------------------------------------------------------------------


class _SinAnestesia:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def terminal(monkeypatch, capsys):
    monkeypatch.setattr(cli, "KeepAwake", _SinAnestesia)

    def correr(*argv):
        codigo = cli.main_cli(list(argv))
        cap = capsys.readouterr()
        return codigo, cap.out, cap.err

    return correr


class TestLaTerminal:
    def test_responde_y_dice_que_va_incluido_en_el_plan(self, terminal, claude, esperas):
        codigo, out, err = terminal("--modo", "plan", "--mensaje", "Una\n\n---\n\nDos")
        assert codigo == 0, err
        assert "con tu plan de Claude · sin coste por token" in out + err
        assert "Coste de este lote: incluido en tu plan" in out
        assert not [x for x in (out + err).splitlines() if "$" in x]               # ni una línea habla de dólares
        assert len(claude.preguntas()) == 2

    def test_cada_orden_no_enseña_un_coste(self, terminal, claude, esperas):
        _, out, err = terminal("--modo", "plan", "--mensaje", "Una")
        linea = [x for x in (out + err).splitlines() if "orden 1:" in x][0]
        assert "OK" in linea and "tok" in linea and "$" not in linea

    def test_el_modelo_es_un_alias_y_va_a_claude_code(self, terminal, claude, esperas):
        codigo, out, err = terminal("--modo", "plan", "--modelo", "opus", "--mensaje", "Una")
        assert codigo == 0 and claude.preguntas()[0]["valores"]["--model"] == "opus"
        assert "con tu plan de Claude (opus)" in out + err

    def test_el_resumen_json_cuenta_el_modo_y_cero_de_coste(self, terminal, claude, esperas):
        codigo, out, _ = terminal("--modo", "plan", "--mensaje", "Una", "--json", "--silencioso")
        resumen = json.loads(out.strip().splitlines()[-1])
        assert codigo == 0 and resumen["modo"] == "plan" and D(resumen["coste_usd"]) == 0 and resumen["completadas"] == 1

    def test_estimar_no_envia_nada_y_no_habla_de_dinero(self, terminal, claude):
        codigo, out, _ = terminal("--modo", "plan", "--mensaje", "Una\n---\nDos", "--dry-run")
        assert codigo == 0 and "incluido en tu plan" in out and "Tope configurado" not in out and claude.llamadas() == []
        codigo, out, _ = terminal("--modo", "plan", "--mensaje", "Una", "--dry-run", "--json")
        assert json.loads(out)["plan"] is True and json.loads(out)["coste_maximo_usd"] == "0"

    @pytest.mark.parametrize("extra", [["--franja", "de noche"], ["--franja", "25:00-07:00"], ["--bucle"],
                                       ["--modelo", "sonnet; rm -rf /"]])
    def test_lo_que_no_vale_sale_con_5_sin_lanzar_claude(self, terminal, claude, extra):
        codigo, _, err = terminal("--modo", "plan", "--mensaje", "Una", *extra)
        assert codigo == 5 and err.strip() and claude.llamadas() == []

    @pytest.mark.parametrize("marca, codigo_esperado", [("extra", 2), ("creditos", 3), ("gasto", 3), ("auth", 1), ("cuenta", 1)])
    def test_los_codigos_de_salida(self, terminal, claude, esperas, marca, codigo_esperado):
        codigo, out, err = terminal("--modo", "plan", "--mensaje", f"Una [[plan:{marca}]]", "--silencioso")
        assert codigo == codigo_esperado, (out, err)

    def test_sin_claude_code_sale_con_1(self, terminal, esperas):
        codigo, _, err = terminal("--modo", "plan", "--mensaje", "Una")
        assert codigo == 1 and "No encuentro Claude Code" in err

    def test_reanudar_hereda_el_plan_y_el_modelo_del_lote(self, terminal, claude, esperas):
        j = Job.crear(["Una", "Otra"], plan(modelo_plan="haiku"))
        j.save()
        codigo, out, err = terminal("--reanudar", j.job_id, "--silencioso")
        assert codigo == 0, err
        assert [x["valores"]["--model"] for x in claude.preguntas()] == ["haiku", "haiku"]
        assert Job.load(j.job_id).completed_count() == 2

    def test_reanudar_un_lote_del_plan_por_la_api_avisa_antes(self, terminal, claude):
        j = Job.crear(["Una"], plan(modelo_plan="haiku"))
        j.save()
        codigo, out, err = terminal("--reanudar", j.job_id, "--modo", "inmediato", "--dry-run")
        assert "se paga por token" in out + err

    def test_la_lista_de_lotes_los_marca_como_del_plan(self, terminal):
        j = Job.crear(["Una"], plan(modelo_plan="haiku"))
        j.save()
        codigo, out, _ = terminal("--listar", "--json")
        assert codigo == 0 and [x for x in json.loads(out) if x["job_id"] == j.job_id][0]["modo"] == "plan"

    def test_no_pide_la_libreria_de_la_api_aunque_no_este(self, terminal, claude, esperas, monkeypatch):
        monkeypatch.setitem(sys.modules, "anthropic", None)
        llamadas = []
        monkeypatch.setattr(instalacion, "resolver_falta_de_la_libreria", lambda terminal: llamadas.append(terminal) or 7)
        from claude_batch_monitor import __main__ as punto_de_entrada

        assert punto_de_entrada.main(["--cli", "--modo", "plan", "--mensaje", "Una", "--silencioso"]) == 0
        assert llamadas == []
