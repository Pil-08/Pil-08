"""Comprobación y guardado de la clave, contra el SDK real con un servidor simulado."""

import json
import os

import anthropic
import pytest

from claude_batch_monitor import claves
from claude_batch_monitor.claves import (
    ErrorDeClave,
    elegir_modelo_por_defecto,
    guardar_clave,
    listar_modelos,
    probar_clave,
)
from claude_batch_monitor.state import KEY_API, SecureStorage, origen_api_key

from ._http import httpx


def servidor(*respuestas):
    guion = list(respuestas)
    vistas = []

    def manejador(request):
        vistas.append(dict(request.headers))
        sig = guion.pop(0) if len(guion) > 1 else guion[0]
        if isinstance(sig, BaseException):
            raise sig
        return sig

    manejador.vistas = vistas
    return manejador


def crear(manejador):
    return lambda clave: anthropic.Anthropic(
        api_key=clave, max_retries=0, timeout=5,
        http_client=httpx.Client(transport=httpx.MockTransport(manejador)),
    )


def pagina(ids, mas=False):
    return httpx.Response(200, content=json.dumps({
        "data": [{"id": i, "type": "model", "display_name": i, "created_at": "2026-01-01T00:00:00Z"} for i in ids],
        "has_more": mas, "first_id": ids[0], "last_id": ids[-1],
    }).encode(), headers={"content-type": "application/json"})


def error(status, tipo, mensaje="x"):
    cuerpo = {"type": "error", "error": {"type": tipo, "message": mensaje}}
    return httpx.Response(status, content=json.dumps(cuerpo).encode(), headers={"content-type": "application/json"})


class TestProbarClave:
    def test_clave_valida_devuelve_los_modelos(self):
        m = servidor(pagina(["claude-sonnet-5-5", "claude-haiku-4-5"]))
        assert probar_clave("sk-ant-bien", crear(m)) == ["claude-sonnet-5-5", "claude-haiku-4-5"]
        assert m.vistas[0]["x-api-key"] == "sk-ant-bien"

    def test_se_quitan_los_espacios_que_deja_copiar_y_pegar(self):
        m = servidor(pagina(["a"]))
        probar_clave("  sk-ant-bien \n", crear(m))
        assert m.vistas[0]["x-api-key"] == "sk-ant-bien"

    def test_clave_vacia(self):
        with pytest.raises(ErrorDeClave) as e:
            probar_clave("   ")
        assert e.value.tipo == "vacia"

    def test_clave_invalida(self):
        m = servidor(error(401, "authentication_error", "invalid x-api-key"))
        with pytest.raises(ErrorDeClave) as e:
            probar_clave("sk-ant-mal", crear(m))
        assert e.value.tipo == "invalida"

    def test_clave_sin_permiso(self):
        m = servidor(error(403, "permission_error"))
        with pytest.raises(ErrorDeClave) as e:
            probar_clave("sk-ant-x", crear(m))
        assert e.value.tipo == "invalida"

    def test_sin_conexion(self):
        m = servidor(httpx.ConnectError("sin ruta"))
        with pytest.raises(ErrorDeClave) as e:
            probar_clave("sk-ant-x", crear(m))
        assert e.value.tipo == "sin_conexion"

    def test_timeout_cuenta_como_sin_conexion(self):
        m = servidor(httpx.ConnectTimeout("lento"))
        with pytest.raises(ErrorDeClave) as e:
            probar_clave("sk-ant-x", crear(m))
        assert e.value.tipo == "sin_conexion"

    def test_otro_error_conserva_el_detalle(self):
        m = servidor(error(500, "api_error", "algo se rompió"))
        with pytest.raises(ErrorDeClave) as e:
            probar_clave("sk-ant-x", crear(m))
        assert e.value.tipo == "otro" and "algo se rompió" in e.value.detalle


class TestListarModelos:
    def test_recorre_todas_las_paginas(self):
        # La v11 usaba `.data`: solo la primera página (20 modelos).
        m = servidor(pagina(["m1", "m2"], mas=True), pagina(["m3"]))
        assert listar_modelos("k", crear(m)) == ["m3", "m2", "m1"]

    def test_sin_duplicados(self):
        m = servidor(pagina(["a", "a", "b"]))
        assert listar_modelos("k", crear(m)) == ["b", "a"]

    def test_error_de_clave(self):
        m = servidor(error(401, "authentication_error"))
        with pytest.raises(ErrorDeClave):
            listar_modelos("k", crear(m))


class TestElegirModelo:
    OFRECIDOS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001"]

    def test_conserva_el_actual_si_existe(self):
        assert elegir_modelo_por_defecto(self.OFRECIDOS, "claude-opus-5-5") == "claude-opus-5-5"

    def test_el_id_con_fecha_cuenta_como_el_mismo_modelo(self):
        assert elegir_modelo_por_defecto(self.OFRECIDOS, "claude-haiku-4-5") == "claude-haiku-4-5"

    def test_si_el_actual_ya_no_existe_cae_al_recomendado(self):
        assert elegir_modelo_por_defecto(self.OFRECIDOS, "claude-retirado") == "claude-sonnet-5-5"

    def test_sin_recomendado_busca_un_sonnet(self):
        assert elegir_modelo_por_defecto(["otro", "claude-sonnet-9"], None) == "claude-sonnet-9"

    def test_sin_nada_conocido_el_primero(self):
        assert elegir_modelo_por_defecto(["zzz", "yyy"], None) == "zzz"

    def test_lista_vacia(self):
        assert elegir_modelo_por_defecto([], "mi-modelo") == "mi-modelo"
        assert elegir_modelo_por_defecto([]) == claves.DEFAULT_MODEL


class TestGuardarClave:
    def test_con_llavero(self, llavero):
        assert guardar_clave("  sk-ant-1 ") == "llavero"
        assert SecureStorage.get(KEY_API) == "sk-ant-1"
        assert origen_api_key() == "llavero"

    def test_sin_llavero_vive_solo_en_memoria(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert guardar_clave("sk-ant-2") == "memoria"
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-2"
        assert origen_api_key() == "entorno"
        monkeypatch.delenv("ANTHROPIC_API_KEY")

    def test_la_clave_no_se_escribe_en_ningun_fichero(self, tmp_path, llavero):
        from claude_batch_monitor import paths
        guardar_clave("sk-ant-secreta-123")
        for f in list(paths.DATA_DIR.rglob("*")) + list(tmp_path.rglob("*")):
            if f.is_file():
                assert "sk-ant-secreta-123" not in f.read_text(encoding="utf-8", errors="ignore")
