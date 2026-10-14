"""Aislamiento de los tests.

Ningún test puede tocar los datos reales del usuario, su llavero ni la red.
La variable de entorno se fija ANTES de importar el paquete (el import crea el
directorio de datos); además cada test redirige las rutas a su propio tmp_path.
"""

import gc
import os
import tempfile

_TMP_SESION = tempfile.mkdtemp(prefix="cbm-tests-")
os.environ["CLAUDE_MONITOR_HOME"] = _TMP_SESION
for _var in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_MODEL"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402

if os.environ.get("CBM_ARCHIVO_UNICO"):
    # Los mismos tests contra `claude_batch_monitor.py` (el archivo único) en vez de contra el paquete.
    from . import archivo_unico

    archivo_unico.instalar()

from claude_batch_monitor import paths, platform_utils, state  # noqa: E402
from claude_batch_monitor.core import base  # noqa: E402
from claude_batch_monitor.gui import i18n  # noqa: E402


@pytest.fixture(autouse=True)
def datos_aislados(request, tmp_path, monkeypatch):
    # Los motores avisan por escritorio/Telegram; en tests no debe salir nada ni abrirse hilos.
    monkeypatch.setattr(base, "notificar", lambda *a, **k: None)
    # Ningún test toca la suspensión del sistema (systemd-inhibit, caffeinate…), salvo los que la prueban a propósito.
    if "keepawake_real" not in request.keywords:
        monkeypatch.setattr(platform_utils.KeepAwake, "enable", lambda self: None)
        monkeypatch.setattr(platform_utils.KeepAwake, "disable", lambda self: None)
    # El idioma del equipo de quien ejecute las pruebas no puede cambiar los textos que se esperan.
    if "idioma_real" not in request.keywords:
        monkeypatch.setattr(i18n, "_codigos_del_sistema", lambda: [])
    # Ninguna prueba lanza el `claude` de verdad (gastaría el plan de quien las ejecute): la ruta apunta a la nada,
    # y la fixture `claude` la cambia por un `claude` de mentira.
    monkeypatch.setenv("CLAUDE_MONITOR_CLAUDE", str(tmp_path / "no-hay-claude"))
    respuestas = tmp_path / "responses"
    respuestas.mkdir()
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "RESPONSES_DIR", respuestas)
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(paths, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(paths, "LOG_FILE", tmp_path / "claude_monitor.log")
    # Sin llavero real: los tests que lo necesitan instalan uno falso.
    monkeypatch.setattr(state, "HAS_KEYRING", False)
    for var in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    # La marca anti-bucle de la instalación (`reiniciar_con` la deja puesta) no puede pasar de un test al siguiente ni a
    # los procesos que éste lance: cada test empieza sin ella.
    os.environ.pop("CLAUDE_MONITOR_YA_INSTALADO", None)
    yield tmp_path


class LlaveroFalso:
    """Sustituto en memoria de `keyring` para probar SecureStorage."""

    def __init__(self):
        self.datos = {}

    def get_password(self, servicio, clave):
        return self.datos.get((servicio, clave))

    def set_password(self, servicio, clave, valor):
        self.datos[(servicio, clave)] = valor

    def delete_password(self, servicio, clave):
        if (servicio, clave) not in self.datos:
            raise KeyError(clave)
        del self.datos[(servicio, clave)]


@pytest.fixture
def llavero(monkeypatch):
    falso = LlaveroFalso()
    monkeypatch.setattr(state, "keyring", falso)
    monkeypatch.setattr(state, "HAS_KEYRING", True)
    return falso


@pytest.fixture
def raiz():
    """La ventana raíz de tkinter para las pruebas de interfaz. Sin tkinter o sin pantalla, se salta."""
    from . import gui_comun

    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No hay pantalla (usa: xvfb-run -a pytest)")
    root.geometry("1020x700+0+0")
    yield root
    # Cada ventana se cierra de verdad (para sus hilos) y la basura de tkinter se recoge AQUÍ, en el hilo
    # de la ventana: si la recogiera un hilo de trabajo, `Variable.__del__` se quedaría esperando a un
    # `mainloop()` que en las pruebas no existe y el lote parecería colgado.
    gui_comun.cerrar_todo()
    try:
        root.destroy()
    except tk.TclError:
        pass
    gc.collect()


@pytest.fixture
def api(monkeypatch):
    """Un servidor falso por prueba (ver servidor_falso.py), y el SDK apuntando a él."""
    from .servidor_falso import ServidorFalso

    with ServidorFalso() as servidor:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", servidor.url)
        monkeypatch.setenv("ANTHROPIC_API_KEY", servidor.clave)
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(variable, raising=False)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        yield servidor


@pytest.fixture
def esperas(monkeypatch):
    """Comprime el tiempo: cada espera del motor dura como mucho 20 ms, y se anota lo que pidió esperar."""
    pedidas: list[float] = []
    original = base.EngineBase.esperar

    def esperar(self, segundos, cb, atras=0.0):
        pedidas.append(segundos + atras)
        return original(self, min(segundos, 0.02), cb, atras=atras)

    monkeypatch.setattr(base.EngineBase, "esperar", esperar)
    return pedidas


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """Un `claude` de mentira (ver claude_falso.py) y el programa apuntando a él."""
    from claude_batch_monitor.core import claude_code

    from .claude_falso import ClaudeFalso

    if os.name == "nt":
        pytest.skip("el `claude` de mentira es un script de sh: estas pruebas son de Linux y macOS")
    falso = ClaudeFalso(tmp_path / "claude_falso")
    for nombre, valor in falso.entorno().items():
        monkeypatch.setenv(nombre, valor)
    for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    claude_code.olvidar_comprobaciones()
    yield falso
    claude_code.olvidar_comprobaciones()


@pytest.fixture
def sin_margen(monkeypatch):
    """Tras el reinicio del límite del plan se espera 90 s por prudencia: en las pruebas, nada."""
    from claude_batch_monitor.core import plan as modulo_plan

    monkeypatch.setattr(modulo_plan, "MARGEN_TRAS_EL_REINICIO_SEC", 0.0)
    monkeypatch.setattr(modulo_plan, "ESPERA_MINIMA_SEC", 0.05)
