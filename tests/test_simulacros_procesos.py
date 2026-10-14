"""El programa como lo usa la gente: un proceso de verdad, contra el servidor falso, entregado de las dos formas.

Cada prueba se repite con el paquete (`python -m claude_batch_monitor`) y con el archivo único
(`python claude_batch_monitor.py`), que es lo que se descarga. Aquí no hay atajos de tiempo: las esperas son reales.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_batch_monitor.job import STATUS_COMPLETED

from .servidor_falso import sin_marca

pytestmark = pytest.mark.simulacro

RAIZ = Path(__file__).resolve().parent.parent
ARCHIVO_UNICO = RAIZ / "claude_batch_monitor.py"


@pytest.fixture(params=["paquete", "archivo_unico"])
def entrega(request):
    if request.param == "archivo_unico":
        return [sys.executable, str(ARCHIVO_UNICO)]
    return [sys.executable, "-m", "claude_batch_monitor"]


class Proceso:
    """Un proceso hijo con su salida leída en segundo plano, para poder esperar a que diga algo."""

    def __init__(self, orden, entorno, juntar_errores=True):
        self.p = subprocess.Popen(
            orden, env=entorno, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if juntar_errores else subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", cwd=str(RAIZ / "tests"),
        )
        self.salida: list[str] = []
        self.errores: list[str] = []
        self._cola: queue.Queue = queue.Queue()
        self._hilos = [threading.Thread(target=self._leer, args=(self.p.stdout, self.salida), daemon=True)]
        if self.p.stderr is not None:
            self._hilos.append(threading.Thread(target=self._leer, args=(self.p.stderr, self.errores), daemon=True))
        for h in self._hilos:
            h.start()

    def _leer(self, flujo, destino):
        for linea in flujo:
            destino.append(linea)

    @property
    def texto(self) -> str:
        return "".join(self.salida)

    def espera_texto(self, fragmento: str, segundos: float = 30) -> bool:
        fin = time.monotonic() + segundos
        while time.monotonic() < fin:
            if fragmento in self.texto:
                return True
            if self.p.poll() is not None and fragmento not in self.texto:
                time.sleep(0.2)
                return fragmento in self.texto
            time.sleep(0.05)
        return False

    def termina(self, segundos: float = 60) -> int:
        codigo = self.p.wait(timeout=segundos)
        for h in self._hilos:
            h.join(timeout=5)
        return codigo

    def matar(self, senal=signal.SIGKILL) -> int:
        if self.p.poll() is None:
            self.p.send_signal(senal)
        return self.termina(20)


@pytest.fixture
def lanzar(api, tmp_path, entrega):
    """`lanzar("--mensaje", "hola")` arranca el programa en la terminal con su propia carpeta de datos."""
    procesos: list[Proceso] = []
    casa = tmp_path / "casa"

    def hacer(*args, casa_propia=None, juntar_errores=True, extra_entorno=None):
        entorno = {
            **{k: v for k, v in os.environ.items() if k not in ("CBM_ARCHIVO_UNICO", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                                                  "http_proxy", "https_proxy", "all_proxy")},
            "ANTHROPIC_BASE_URL": api.url, "ANTHROPIC_API_KEY": api.clave, "NO_PROXY": "127.0.0.1,localhost",
            "CLAUDE_MONITOR_HOME": str(casa_propia or casa), "PYTHONPATH": str(RAIZ), "PYTHONUTF8": "1",
            **(extra_entorno or {}),
        }
        entorno = {k: v for k, v in entorno.items() if v is not None}     # (None = quitar la variable)
        proceso = Proceso([*entrega, "--cli", *args], entorno, juntar_errores)
        procesos.append(proceso)
        return proceso

    hacer.casa = casa
    yield hacer
    for proceso in procesos:
        if proceso.p.poll() is None:
            proceso.p.kill()
            proceso.p.wait()


def trabajo_de(casa: Path) -> tuple[dict, str]:
    """(contenido, id) del único lote guardado en la carpeta de datos de ese proceso."""
    ficheros = sorted((casa / "responses").glob("job_*.json"))
    assert len(ficheros) == 1, ficheros
    return json.loads(ficheros[0].read_text(encoding="utf-8")), ficheros[0].stem[len("job_"):]


class TestUnaEjecucion:
    def test_ejecuta_sale_con_0_y_el_resumen_json_va_solo_por_stdout(self, api, lanzar):
        p = lanzar("--mensaje", "Cuéntame algo", "--json", "--silencioso", juntar_errores=False)
        assert p.termina() == 0
        resumen = json.loads("".join(p.salida).strip().splitlines()[-1])
        assert resumen["razon"] == "completed" and resumen["completadas"] == 1 and resumen["codigo_de_salida"] == 0
        assert len(api.cobros) == 1 and "Traceback" not in "".join(p.errores)

    def test_exporta_a_csv_con_las_tildes_bien(self, api, lanzar, tmp_path):
        destino = tmp_path / "salida" / "r.csv"
        p = lanzar("--mensaje", "¿Qué es una canción?", "--exportar", str(destino), "--silencioso")
        assert p.termina() == 0
        assert destino.read_bytes().startswith(b"\xef\xbb\xbf")
        assert "Respuesta a: ¿Qué es una canción?" in destino.read_text(encoding="utf-8-sig")

    def test_varias_ordenes_desde_un_csv_con_plantilla(self, api, lanzar, tmp_path):
        datos = tmp_path / "datos.csv"
        datos.write_text("id,texto\n1,uno\n2,dos\n3,tres\n", encoding="utf-8")
        p = lanzar("--mensaje", str(datos), "--campo", "texto", "--plantilla", "Resume: {input}", "--silencioso")
        assert p.termina() == 0
        assert sorted(c.pregunta for c in api.cobros) == ["Resume: dos", "Resume: tres", "Resume: uno"]

    def test_una_pregunta_larga_escrita_como_texto_se_envia_entera(self, api, lanzar):
        """Antes fallaba con «File name too long»: se comprobaba si el texto era el nombre de un archivo."""
        larga = "¿Puedes contarme con todo detalle cómo funciona esto? " * 12
        assert len(larga) > 600
        p = lanzar("--mensaje", larga, "--silencioso")
        assert p.termina() == 0, p.texto
        assert len(api.cobros) == 1 and api.cobros[0].pregunta == larga.strip()

    def test_el_tope_de_gasto_sale_con_2_sin_enviar_nada(self, api, lanzar):
        p = lanzar("--mensaje", "algo", "--tope-gasto", "0.0000001", "--silencioso")
        assert p.termina() == 2
        assert api.peticiones_a("/v1/messages") == [] and "TOPE DE GASTO" in p.texto

    def test_sin_saldo_sale_con_3(self, api, lanzar):
        p = lanzar("--mensaje", "algo [[plan:400saldo]]", "--silencioso")
        assert p.termina() == 3
        assert "saldo" in p.texto.lower() or "credit" in p.texto.lower()

    def test_clave_rechazada_sale_con_1(self, api, lanzar):
        p = lanzar("--mensaje", "algo", "--silencioso", extra_entorno={"ANTHROPIC_API_KEY": "sk-ant-otra"})
        assert p.termina() == 1
        assert "API key" in p.texto

    def test_un_limite_por_minuto_se_espera_de_verdad_y_sigue(self, api, lanzar):
        inicio = time.monotonic()
        p = lanzar("--mensaje", "uno [[plan:429(1),ok]]", "--silencioso")
        assert p.termina() == 0
        assert time.monotonic() - inicio >= 3 and api.veces_facturada("uno") == 1   # 1 s de la API + 2 de margen

    def test_sin_argumentos_explica_el_uso_y_sale_con_5(self, api, lanzar):
        p = lanzar()
        assert p.termina() == 5 and "usage" in p.texto.lower()

    def test_la_clave_nunca_aparece_en_la_salida_ni_en_el_registro(self, api, lanzar):
        p = lanzar("--mensaje", "hola")
        assert p.termina() == 0
        assert api.clave not in p.texto
        for fichero in (lanzar.casa).rglob("*"):
            if fichero.is_file():
                assert api.clave.encode() not in fichero.read_bytes(), fichero


class TestDosCopias:
    def test_la_segunda_no_envia_nada_y_la_primera_acaba_bien(self, api, lanzar, tmp_path):
        primera = lanzar("--mensaje", "lenta [[plan:lento(4)]]", "--silencioso")
        # Esperar a que de verdad esté trabajando: el servidor ha recibido su petición.
        fin = time.monotonic() + 20
        while not api.peticiones_a("/v1/messages") and time.monotonic() < fin:
            time.sleep(0.05)
        segunda = lanzar("--mensaje", "otra", "--silencioso")
        assert segunda.termina() == 5
        assert "otra copia" in segunda.texto
        assert primera.termina() == 0
        assert [sin_marca(c.pregunta) for c in api.cobros] == ["lenta"]       # la segunda no envió nada

    def test_con_carpetas_de_datos_distintas_pueden_convivir(self, api, lanzar, tmp_path):
        primera = lanzar("--mensaje", "uno [[plan:lento(3)]]", "--silencioso", casa_propia=tmp_path / "a")
        segunda = lanzar("--mensaje", "dos", "--silencioso", casa_propia=tmp_path / "b")
        assert segunda.termina() == 0 and primera.termina() == 0
        assert sorted(c.pregunta for c in api.cobros) == ["dos", "uno"]

    def test_tras_morir_la_primera_de_golpe_la_segunda_puede_empezar(self, api, lanzar):
        primera = lanzar("--mensaje", "larga [[plan:lento(30)]]", "--silencioso")
        fin = time.monotonic() + 20
        while not api.peticiones_a("/v1/messages") and time.monotonic() < fin:
            time.sleep(0.05)
        primera.matar(signal.SIGKILL)
        segunda = lanzar("--mensaje", "nueva", "--silencioso")
        assert segunda.termina() == 0


class TestMatarYReanudar:
    PREGUNTAS = ["Pregunta A", "Pregunta B", "Pregunta C [[plan:lento(25),ok]]", "Pregunta D", "Pregunta E"]

    def _lanzar_hasta_la_lenta(self, api, lanzar):
        p = lanzar("--mensaje", "\n\n---\n\n".join(self.PREGUNTAS))
        assert p.espera_texto("orden 2: OK", 30), p.texto
        fin = time.monotonic() + 20
        while len(api.peticiones_a("/v1/messages")) < 3 and time.monotonic() < fin:
            time.sleep(0.05)
        return p

    def test_matado_de_golpe_se_reanuda_sin_repetir_lo_ya_pagado(self, api, lanzar):
        p = self._lanzar_hasta_la_lenta(api, lanzar)
        p.matar(signal.SIGKILL)
        datos, job_id = trabajo_de(lanzar.casa)
        estados = [m["status"] for m in datos["messages"]]
        assert estados[:2] == [STATUS_COMPLETED, STATUS_COMPLETED] and estados[2:] == ["pending"] * 3
        gasto_guardado = json.loads((lanzar.casa / "state.json").read_text(encoding="utf-8"))["gasto_acumulado_usd"]
        assert float(gasto_guardado) > 0                                   # lo pagado antes de morir consta en disco

        r = lanzar("--reanudar", job_id, "--silencioso")
        assert r.termina(90) == 0, r.texto
        datos, _ = trabajo_de(lanzar.casa)
        assert [m["status"] for m in datos["messages"]] == [STATUS_COMPLETED] * 5
        veces = {sin_marca(c.pregunta): 0 for c in api.cobros}
        for c in api.cobros:
            veces[sin_marca(c.pregunta)] += 1
        # A, B, D y E se pagan una vez; C estaba en vuelo cuando se mató el proceso: el servidor pudo cobrarla antes.
        assert all(veces[x] == 1 for x in ("Pregunta A", "Pregunta B", "Pregunta D", "Pregunta E")), veces
        assert veces["Pregunta C"] in (1, 2)

    def test_ctrl_c_guarda_todo_sale_con_4_y_se_reanuda(self, api, lanzar):
        p = self._lanzar_hasta_la_lenta(api, lanzar)
        assert p.matar(signal.SIGINT) == 4, p.texto
        assert "Detenido por el usuario" in p.texto and "--reanudar" in p.texto
        datos, job_id = trabajo_de(lanzar.casa)
        assert [m["status"] for m in datos["messages"]][:2] == [STATUS_COMPLETED, STATUS_COMPLETED]
        r = lanzar("--reanudar", job_id, "--silencioso")
        assert r.termina(90) == 0, r.texto

    def test_la_lista_de_lotes_marca_el_incompleto(self, api, lanzar):
        p = self._lanzar_hasta_la_lenta(api, lanzar)
        p.matar(signal.SIGKILL)
        listado = lanzar("--listar", "--json", juntar_errores=False)
        assert listado.termina() == 0
        lotes = json.loads("".join(listado.salida))
        assert len(lotes) == 1 and lotes[0]["incompleto"] and lotes[0]["completadas"] == 2 and lotes[0]["total"] == 5


class TestEntornosHostiles:
    """El sitio donde lo abra la gente no es el de quien lo programó: otra codificación, rutas raras, sin carpeta propia."""

    SIN_UTF8 = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "ascii", "PYTHONCOERCECLOCALE": "0"}

    def test_con_una_salida_que_no_es_utf8_no_revienta_al_imprimir_acentos(self, api, lanzar, tmp_path):
        destino = tmp_path / "r.md"
        p = lanzar("--mensaje", "¿Qué canción es «Ñandú»?", "--exportar", str(destino), "--json", "--silencioso",
                   juntar_errores=False, extra_entorno=self.SIN_UTF8)
        assert p.termina() == 0, "".join(p.errores)
        assert json.loads("".join(p.salida).strip().splitlines()[-1])["completadas"] == 1
        assert "¿Qué canción es «Ñandú»?" in destino.read_text(encoding="utf-8")
        assert "UnicodeEncodeError" not in "".join(p.errores)

    def test_la_ayuda_con_acentos_tampoco(self, api, lanzar):
        p = lanzar("--help", extra_entorno=self.SIN_UTF8)
        assert p.termina() == 0 and "Procesa una lista de órdenes" in p.texto

    def test_sin_carpeta_de_usuario_escribible_usa_una_temporal_y_funciona(self, api, lanzar, tmp_path):
        no_es_carpeta = tmp_path / "esto_es_un_archivo"
        no_es_carpeta.write_text("no soy una carpeta", encoding="utf-8")
        p = lanzar("--mensaje", "hola", "--silencioso", casa_propia=tmp_path / "ignorada",
                   extra_entorno={"CLAUDE_MONITOR_HOME": None, "HOME": str(no_es_carpeta), "XDG_CONFIG_HOME": None,
                                  "APPDATA": None, "USERPROFILE": None})
        assert p.termina() == 0, p.texto
        assert len(api.cobros) == 1

    def test_rutas_con_espacios_y_caracteres_no_ascii(self, api, entrega, tmp_path):
        carpeta = tmp_path / "mi carpeta ñandú (copia)"
        carpeta.mkdir()
        datos = tmp_path / "datos con ñ y espacios"
        if entrega[-1].endswith("claude_batch_monitor.py"):
            copia = carpeta / "claude_batch_monitor.py"
            copia.write_bytes(ARCHIVO_UNICO.read_bytes())
            orden = [sys.executable, str(copia)]
            cwd = str(carpeta)
        else:
            orden = list(entrega)
            cwd = str(carpeta)
        entorno = {
            **{k: v for k, v in os.environ.items() if k not in ("CBM_ARCHIVO_UNICO", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                                                  "http_proxy", "https_proxy", "all_proxy")},
            "ANTHROPIC_BASE_URL": api.url, "ANTHROPIC_API_KEY": api.clave, "NO_PROXY": "127.0.0.1,localhost",
            "CLAUDE_MONITOR_HOME": str(datos), "PYTHONPATH": str(RAIZ),
        }
        resultado = subprocess.run([*orden, "--cli", "--mensaje", "Pregunta ñ", "--silencioso"], env=entorno, cwd=cwd,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        assert resultado.returncode == 0, resultado.stdout + resultado.stderr
        assert list((datos / "responses").glob("job_*.json")) and api.veces_facturada("Pregunta ñ") == 1

    def test_con_los_avisos_de_python_convertidos_en_errores(self, api, lanzar):
        """Ninguna línea del programa usa algo en desuso que la próxima versión de Python vaya a quitar."""
        p = lanzar("--mensaje", "hola", "--silencioso",
                   extra_entorno={"PYTHONWARNINGS": "error::DeprecationWarning:claude_batch_monitor,"
                                                    "error::DeprecationWarning:claude_batch_monitor_archivo_unico,"
                                                    "error::SyntaxWarning,error::ResourceWarning:claude_batch_monitor"})
        assert p.termina() == 0, p.texto
        assert "Warning" not in p.texto

    def test_sin_conexion_a_internet_de_verdad_espera_en_vez_de_morir(self, api, lanzar):
        """El servidor deja de existir (puerto cerrado): el programa lo toma por «sin conexión» y espera."""
        api.caer()
        p = lanzar("--mensaje", "hola")
        assert p.espera_texto("Sin conexión", 30) or p.espera_texto("conexión", 5), p.texto
        assert p.p.poll() is None                                      # sigue esperando, no ha muerto
        api.levantar()
        assert p.termina(90) == 0, p.texto


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="hace falta una pantalla (xvfb-run -a pytest)")
class TestLaVentanaArranca:
    def _entorno(self, api, tmp_path):
        return {
            **{k: v for k, v in os.environ.items() if k not in ("CBM_ARCHIVO_UNICO",)},
            "ANTHROPIC_BASE_URL": api.url, "ANTHROPIC_API_KEY": api.clave, "NO_PROXY": "127.0.0.1,localhost",
            "CLAUDE_MONITOR_HOME": str(tmp_path / "ventana"), "PYTHONPATH": str(RAIZ), "PYTHONUTF8": "1",
        }

    @pytest.mark.parametrize("modo", ["--simple", "--avanzado"])
    def test_se_abre_y_se_queda_abierta_sin_errores(self, api, entrega, tmp_path, modo):
        orden = [e for e in entrega] + [modo]
        p = Proceso(orden, self._entorno(api, tmp_path))
        try:
            time.sleep(4)
            assert p.p.poll() is None, f"se cerró sola: {p.texto}"
        finally:
            p.p.send_signal(signal.SIGTERM)
            p.p.wait(timeout=20)
        assert "Traceback" not in p.texto

    def test_una_segunda_ventana_no_abre_el_programa_otra_vez(self, api, entrega, tmp_path):
        entorno = self._entorno(api, tmp_path)
        primera = Proceso([*entrega, "--simple"], entorno)
        segunda = None
        try:
            time.sleep(4)
            assert primera.p.poll() is None
            segunda = Proceso([*entrega, "--simple"], entorno)
            time.sleep(3)
            # La segunda enseña un aviso y espera a que se cierre: no abre un programa más ni se cuelga.
            assert segunda.p.poll() is None or segunda.p.returncode == 5
            assert "Traceback" not in segunda.texto
            assert "otra copia" in segunda.texto.lower() or "another copy" in segunda.texto.lower()
        finally:
            for proceso in (segunda, primera):
                if proceso is not None and proceso.p.poll() is None:
                    proceso.p.send_signal(signal.SIGTERM)
                    try:
                        proceso.p.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proceso.p.kill()


# ----------------------------------------------------------------------
# Con el plan de Claude: la terminal de verdad, con un `claude` de mentira de verdad (otro proceso)
# ----------------------------------------------------------------------


def _vivo(pid: int) -> bool:
    from .test_simulacros_plan import vivo

    return vivo(pid)


def _esperar_a(condicion, segundos: float = 20.0) -> bool:
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        if condicion():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def lanzar_plan(claude, tmp_path, entrega):
    """`lanzar_plan("--mensaje", "hola")`: la terminal con el plan, sin clave de API, contra el `claude` de mentira."""
    procesos: list[Proceso] = []
    casa = tmp_path / "casa_plan"

    def hacer(*args, casa_propia=None, juntar_errores=True, extra_entorno=None):
        entorno = {
            **{k: v for k, v in os.environ.items() if k not in ("CBM_ARCHIVO_UNICO", "ANTHROPIC_API_KEY",
                                                                  "ANTHROPIC_AUTH_TOKEN")},
            "CLAUDE_MONITOR_HOME": str(casa_propia or casa), "PYTHONPATH": str(RAIZ), "PYTHONUTF8": "1",
            **(extra_entorno or {}),
        }
        entorno = {k: v for k, v in entorno.items() if v is not None}     # (None = quitar la variable)
        proceso = Proceso([*entrega, "--cli", "--modo", "plan", *args], entorno, juntar_errores)
        procesos.append(proceso)
        return proceso

    hacer.casa = casa
    yield hacer
    for proceso in procesos:
        if proceso.p.poll() is None:
            proceso.p.kill()
            proceso.p.wait()
    # Y los `claude` de mentira que hayan quedado vivos (solo los que son de la prueba: se mira lo que ejecutan).
    for llamada in claude.llamadas():
        pid = llamada.get("pid")
        try:
            lineas = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(b"claude_falso" in x for x in lineas) and _vivo(pid):
            os.kill(pid, signal.SIGKILL)


class TestPlanComoProceso:
    def test_responde_con_el_plan_sin_clave_y_sale_con_0(self, lanzar_plan, claude):
        p = lanzar_plan("--mensaje", "¿Qué es un token?", "--json", "--silencioso", juntar_errores=False,
                        extra_entorno={"ANTHROPIC_API_KEY": "sk-ant-api03-secreta", "ANTHROPIC_AUTH_TOKEN": "otra"})
        assert p.termina() == 0, "".join(p.errores)
        resumen = json.loads("".join(p.salida).strip().splitlines()[-1])
        assert resumen["razon"] == "completed" and resumen["completadas"] == 1
        assert claude.preguntas()[0]["pregunta"] == "¿Qué es un token?"
        # La clave del entorno del programa no llegó a ninguno de los `claude` que lanzó (versión, sesión y pregunta).
        assert claude.llamadas() and all(not any(x["env"].values()) for x in claude.llamadas())

    def test_lo_que_cuenta_en_pantalla_es_el_plan_y_no_dinero(self, lanzar_plan, claude):
        p = lanzar_plan("--mensaje", "Una pregunta", "--modelo", "opus")
        assert p.termina() == 0, p.texto
        assert "con tu plan de Claude (opus)" in p.texto and "Coste de este lote: incluido en tu plan" in p.texto
        assert "Tu plan: sesión 13 %" in p.texto                                  # lo que informa Claude Code
        assert "$" not in p.texto.split("Coste de este lote")[1].splitlines()[0]
        assert claude.preguntas()[0]["valores"]["--model"] == "opus"

    def test_estimar_con_el_plan_no_lanza_claude_ni_habla_de_dinero(self, lanzar_plan, claude):
        p = lanzar_plan("--mensaje", "Una pregunta", "--dry-run")
        assert p.termina() == 0, p.texto
        assert "incluido en tu plan" in p.texto and "Tope" not in p.texto and claude.llamadas() == []

    def test_con_la_salida_sin_utf8_las_tildes_llegan_bien_a_claude(self, lanzar_plan, claude, tmp_path):
        destino = tmp_path / "r.md"
        p = lanzar_plan("--mensaje", "¿Qué canción es «Ñandú»?", "--exportar", str(destino), "--silencioso",
                        juntar_errores=False, extra_entorno=TestEntornosHostiles.SIN_UTF8)
        assert p.termina() == 0, "".join(p.errores)
        assert claude.preguntas()[0]["pregunta"] == "¿Qué canción es «Ñandú»?"
        assert "Respuesta a: ¿Qué canción es «Ñandú»?" in destino.read_text(encoding="utf-8")

    def test_sin_claude_code_sale_con_1_explicando_y_la_pregunta_no_se_pierde(self, lanzar_plan, claude, tmp_path):
        p = lanzar_plan("--mensaje", "Una pregunta",
                        extra_entorno={"CLAUDE_MONITOR_CLAUDE": str(tmp_path / "no-esta-instalado")})
        assert p.termina() == 1, p.texto
        assert "No encuentro Claude Code" in p.texto
        datos, _ = trabajo_de(lanzar_plan.casa)
        assert [m["status"] for m in datos["messages"]] == ["pending"]

    def test_una_clave_de_api_en_claude_code_se_corta_antes_de_que_responda(self, lanzar_plan, claude):
        p = lanzar_plan("--mensaje", "Una pregunta [[plan:clave]]")
        assert p.termina(60) == 1, p.texto
        assert "clave de API" in p.texto
        datos, _ = trabajo_de(lanzar_plan.casa)
        assert [m["status"] for m in datos["messages"]] == ["pending"]
        assert not _esperar_a(lambda: _vivo(claude.preguntas()[0]["pid"]), 0.5)   # y el `claude` no se quedó vivo

    def test_dos_copias_la_segunda_no_pregunta_nada(self, lanzar_plan, claude):
        primera = lanzar_plan("--mensaje", "lenta [[plan:lento(5)]]", "--silencioso")
        assert _esperar_a(lambda: claude.preguntas())
        segunda = lanzar_plan("--mensaje", "otra", "--silencioso")
        assert segunda.termina() == 5 and "otra copia" in segunda.texto
        assert primera.termina() == 0
        assert claude.veces("otra") == 0 and claude.veces("lenta") == 1

    def test_fuera_de_la_franja_espera_sin_preguntar_y_ctrl_c_lo_deja_guardado(self, lanzar_plan, claude):
        from datetime import datetime, timedelta

        ahora = datetime.now()
        fuera = f"{(ahora + timedelta(hours=2)):%H:%M}-{(ahora + timedelta(hours=3)):%H:%M}"
        p = lanzar_plan("--mensaje", "Una pregunta", "--franja", fuera)
        assert p.espera_texto("Fuera de la franja horaria", 30), p.texto
        assert p.matar(signal.SIGINT) == 4, p.texto
        assert claude.preguntas() == []
        datos, _ = trabajo_de(lanzar_plan.casa)
        assert [m["status"] for m in datos["messages"]] == ["pending"]

    def test_una_franja_mal_escrita_se_dice_y_sale_con_5(self, lanzar_plan, claude):
        p = lanzar_plan("--mensaje", "Una pregunta", "--franja", "de noche")
        assert p.termina() == 5 and "franja" in p.texto.lower() and claude.llamadas() == []

    def test_el_bucle_no_existe_con_el_plan(self, lanzar_plan, claude):
        p = lanzar_plan("--mensaje", "Una pregunta", "--bucle")
        assert p.termina() == 5 and "bucle" in p.texto.lower() and claude.llamadas() == []


class TestPlanMatarYReanudar:
    def test_ctrl_c_mientras_claude_responde_lo_mata_sale_con_4_y_se_reanuda(self, lanzar_plan, claude):
        p = lanzar_plan("--mensaje", "Lenta [[plan:lento(40),ok]]")
        assert _esperar_a(lambda: claude.preguntas())
        pid = claude.preguntas()[0]["pid"]
        assert p.matar(signal.SIGINT) == 4, p.texto
        assert "Detenido por el usuario" in p.texto and "--reanudar" in p.texto
        assert _esperar_a(lambda: not _vivo(pid), 10), "el `claude` se quedó vivo tras Ctrl+C"
        datos, job_id = trabajo_de(lanzar_plan.casa)
        assert [m["status"] for m in datos["messages"]] == ["pending"]          # la respuesta no llegó: sigue pendiente
        r = lanzar_plan("--reanudar", job_id, "--silencioso")
        assert r.termina(60) == 0, r.texto
        datos, _ = trabajo_de(lanzar_plan.casa)
        assert [m["status"] for m in datos["messages"]] == [STATUS_COMPLETED]
        assert claude.veces("Lenta") == 2

    def test_matado_de_golpe_se_reanuda_sin_repetir_lo_ya_respondido(self, lanzar_plan, claude):
        p = lanzar_plan("--mensaje", "Pregunta A\n\n---\n\nPregunta B [[plan:lento(8),ok]]\n\n---\n\nPregunta C")
        assert p.espera_texto("orden 1: OK", 30), p.texto
        assert _esperar_a(lambda: claude.veces("Pregunta B") == 1)
        p.matar(signal.SIGKILL)
        datos, job_id = trabajo_de(lanzar_plan.casa)
        assert [m["status"] for m in datos["messages"]] == [STATUS_COMPLETED, "pending", "pending"]
        # El `claude` que respondía a B no tiene a quién avisar: acaba solo, no se queda para siempre.
        pid = claude.preguntas()[1]["pid"]
        assert _esperar_a(lambda: not _vivo(pid), 30), "el `claude` huérfano no terminó solo"
        r = lanzar_plan("--reanudar", job_id, "--silencioso")
        assert r.termina(60) == 0, r.texto
        datos, _ = trabajo_de(lanzar_plan.casa)
        assert [m["status"] for m in datos["messages"]] == [STATUS_COMPLETED] * 3
        assert claude.veces("Pregunta A") == 1 and claude.veces("Pregunta C") == 1 and claude.veces("Pregunta B") == 2

    def test_un_limite_se_recuerda_entre_ejecuciones_y_no_se_vuelve_a_preguntar_antes_de_tiempo(self, lanzar_plan, claude):
        from datetime import datetime, timezone

        casa = lanzar_plan.casa
        p = lanzar_plan("--mensaje", "Una pregunta [[plan:limite(600),ok]]")
        assert p.espera_texto("límite de tu plan", 30), p.texto
        assert p.matar(signal.SIGINT) == 4, p.texto
        estado = json.loads((casa / "state.json").read_text(encoding="utf-8"))
        hasta = datetime.fromisoformat(estado["plan_bloqueado_hasta"])
        assert hasta > datetime.now(timezone.utc)                                   # quedó apuntado en disco
        _, job_id = trabajo_de(casa)
        r = lanzar_plan("--reanudar", job_id)
        assert r.espera_texto("límite de tu plan", 30), r.texto                     # espera sin llamar a claude…
        assert claude.veces("Una pregunta") == 1                                    # …la segunda vez no preguntó nada
        assert r.matar(signal.SIGINT) == 4
