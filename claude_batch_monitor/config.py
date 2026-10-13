"""Configuración del usuario: modelo, tope de gasto, tiempos… con validación central.

Todo el dinero va en `Decimal`. Con `float`, 0.1 + 0.2 != 0.3 y un contador de
gasto que se acumula miles de veces acaba con errores de céntimos justo en la
cifra que decide si se para el lote.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from . import paths
from .parsing import DEFAULT_SEPARATOR
from .paths import logger, write_atomic
from .pricing import (
    DEFAULT_MODEL,
    NIVELES_ESFUERZO,
    Precios,
    buscar_modelo,
    resolver_precios,
    soporta_esfuerzo,
)

MODOS = ("inmediato", "lote")
INTERFACES = ("simple", "avanzada")
TEMAS = ("auto", "claro", "oscuro")  # auto = el del sistema
MAX_TOKENS_STREAMING = 16_000  # por encima, el SDK exige streaming
MAX_ERROR_ATTEMPTS = 8
DELAY_BETWEEN_CYCLES_SEC = 10
DEFAULT_API_TIMEOUT_SEC = 300


def a_decimal(valor: Any, defecto: Decimal) -> Decimal:
    """Convierte lo que haya en el JSON (str, int, float) a Decimal sin arrastrar ruido binario."""
    if valor is None or isinstance(valor, bool):
        return defecto
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return defecto
    return d if d.is_finite() else defecto


def _int(valor: Any, defecto: int, minimo: int, maximo: int) -> int:
    try:
        return max(minimo, min(maximo, int(valor)))
    except (TypeError, ValueError, OverflowError):  # (OverflowError: `Infinity`, que Python sí lee en un JSON)
        return defecto


@dataclass
class Validacion:
    errores: list = field(default_factory=list)  # impiden ejecutar
    avisos: list = field(default_factory=list)   # se muestran pero no bloquean

    @property
    def ok(self) -> bool:
        return not self.errores


@dataclass
class Configuracion:
    mensaje: str = ""
    plantilla: str = ""  # envuelve cada orden; `{input}` marca el hueco (ver inputs.aplicar_plantilla)
    modelo: str = DEFAULT_MODEL
    separador: str = DEFAULT_SEPARATOR
    modo: str = "inmediato"  # inmediato | lote (Batches API, -50 %)
    system_prompt: str = ""
    usar_cache: bool = True
    esfuerzo: str = ""  # "" = no enviarlo; low | medium | high | xhigh | max
    max_tokens: int = 16_000
    concurrencia: int = 1
    streaming: bool = False  # mostrar la respuesta según se escribe

    # --- Blindaje de gasto ---
    tope_gasto_usd: Decimal = Decimal("5")  # 0 = sin tope
    # None = usar el catálogo. Un valor fijado a mano manda sobre el catálogo.
    precio_input_mtok: Optional[Decimal] = None
    precio_output_mtok: Optional[Decimal] = None
    reset_gasto_por_lote: bool = True
    estimar_antes_de_enviar: bool = True

    # --- Tiempos ---
    modo_continuo: bool = False  # repetir el lote en bucle (¡gasta!)
    hora_limite: str = ""        # "" = sin hora límite (en bucle se aplica 23:59)
    pausa_ciclos_sec: int = DELAY_BETWEEN_CYCLES_SEC
    pausa_entre_mensajes_sec: int = 0
    timeout_api_sec: int = DEFAULT_API_TIMEOUT_SEC
    max_error_attempts: int = MAX_ERROR_ATTEMPTS

    # --- Aplicación ---
    telegram_chat_id: str = ""
    auto_arranque: bool = False
    iniciar_minimizado: bool = False  # abrir directamente en la bandeja del sistema
    retencion_dias: int = 30
    interfaz: str = "simple"  # simple | avanzada
    mostrar_formato: bool = True  # respuestas con negrita, listas, código… (el texto original no cambia)
    idioma: str = ""          # "" = el del sistema
    tema: str = "auto"        # auto (el del sistema) | claro | oscuro

    # ------------------------------------------------------------------

    @property
    def usa_streaming(self) -> bool:
        return self.streaming or self.max_tokens > MAX_TOKENS_STREAMING

    @property
    def es_lote(self) -> bool:
        return self.modo == "lote"

    def precios(self) -> Precios:
        return resolver_precios(self.modelo, self.precio_input_mtok, self.precio_output_mtok)

    def validar(self) -> Validacion:
        v = Validacion()
        if not self.modelo.strip():
            v.errores.append("Falta el modelo.")
        if self.modo not in MODOS:
            v.errores.append(f"Modo desconocido '{self.modo}'. Usa: {', '.join(MODOS)}.")
        if self.max_tokens < 1:
            v.errores.append("max_tokens debe ser al menos 1.")
        if self.tope_gasto_usd < 0:
            v.errores.append("El tope de gasto no puede ser negativo.")
        if self.concurrencia < 1:
            v.errores.append("La concurrencia debe ser al menos 1.")
        if self.esfuerzo and self.esfuerzo not in NIVELES_ESFUERZO:
            v.errores.append(
                f"Nivel de esfuerzo desconocido '{self.esfuerzo}'. Usa: {', '.join(NIVELES_ESFUERZO)}."
            )

        info = buscar_modelo(self.modelo)
        if info and info.max_salida and self.max_tokens > info.max_salida:
            v.errores.append(
                f"{info.nombre} admite como máximo {info.max_salida:,} tokens de salida "
                f"(has pedido {self.max_tokens:,})."
            )
        if info and info.retirado:
            v.avisos.append(f"{info.nombre} está retirado o en retirada: puede dejar de funcionar.")
        if self.esfuerzo and soporta_esfuerzo(self.modelo, self.esfuerzo) is False:
            v.avisos.append(
                f"{self.modelo} no admite el esfuerzo '{self.esfuerzo}': se ignorará al enviar."
            )

        aviso_precio = self.precios().aviso()
        if aviso_precio:
            v.avisos.append(aviso_precio)
        if self.tope_gasto_usd == 0:
            v.avisos.append("Sin tope de gasto: nada impedirá que el lote consuma todo tu saldo.")
        if self.modo_continuo:
            v.avisos.append("Modo bucle: el lote se repetirá hasta la hora límite o el tope de gasto.")
        if self.es_lote and self.concurrencia > 1:
            v.avisos.append("La concurrencia no aplica al modo lote (la API ya lo procesa en paralelo).")
        return v

    # ------------------------------------------------------------------

    def a_dict(self) -> dict:
        d = asdict(self)
        for clave in ("tope_gasto_usd", "precio_input_mtok", "precio_output_mtok"):
            d[clave] = None if d[clave] is None else str(d[clave])
        return d

    @classmethod
    def desde_dict(cls, data: dict) -> Configuracion:
        """Construye una configuración válida a partir de cualquier JSON (incluido el de la v11)."""
        base = cls()

        def texto(clave: str, defecto: str) -> str:
            valor = data.get(clave, defecto)
            return defecto if valor is None else str(valor)

        def precio(clave: str) -> Optional[Decimal]:
            if data.get(clave) is None:
                return None
            d = a_decimal(data.get(clave), Decimal(-1))
            return d if d >= 0 else None

        modo = texto("modo", base.modo)
        interfaz = texto("interfaz", base.interfaz)
        tema = texto("tema", base.tema).strip().lower()
        esfuerzo = texto("esfuerzo", "")
        return cls(
            mensaje=texto("mensaje", ""),
            plantilla=texto("plantilla", ""),
            modelo=texto("modelo", base.modelo).strip() or base.modelo,
            separador=texto("separador", base.separador).strip() or base.separador,
            modo=modo if modo in MODOS else base.modo,
            system_prompt=texto("system_prompt", ""),
            usar_cache=bool(data.get("usar_cache", base.usar_cache)),
            esfuerzo=esfuerzo if esfuerzo in NIVELES_ESFUERZO else "",
            max_tokens=_int(data.get("max_tokens"), base.max_tokens, 1, 1_000_000),
            concurrencia=_int(data.get("concurrencia"), 1, 1, 32),
            streaming=bool(data.get("streaming", False)),
            tope_gasto_usd=max(Decimal(0), a_decimal(data.get("tope_gasto_usd"), base.tope_gasto_usd)),
            precio_input_mtok=precio("precio_input_mtok"),
            precio_output_mtok=precio("precio_output_mtok"),
            reset_gasto_por_lote=bool(data.get("reset_gasto_por_lote", True)),
            estimar_antes_de_enviar=bool(data.get("estimar_antes_de_enviar", True)),
            modo_continuo=bool(data.get("modo_continuo", False)),
            hora_limite=texto("hora_limite", "").strip(),
            pausa_ciclos_sec=_int(data.get("pausa_ciclos_sec"), DELAY_BETWEEN_CYCLES_SEC, 1, 3600),
            pausa_entre_mensajes_sec=_int(data.get("pausa_entre_mensajes_sec"), 0, 0, 60),
            timeout_api_sec=_int(data.get("timeout_api_sec"), DEFAULT_API_TIMEOUT_SEC, 15, 900),
            max_error_attempts=_int(data.get("max_error_attempts"), MAX_ERROR_ATTEMPTS, 1, 100),
            telegram_chat_id=texto("telegram_chat_id", ""),
            auto_arranque=bool(data.get("auto_arranque", False)),
            iniciar_minimizado=bool(data.get("iniciar_minimizado", False)),
            retencion_dias=_int(data.get("retencion_dias"), 30, 0, 3650),
            interfaz=interfaz if interfaz in INTERFACES else base.interfaz,
            mostrar_formato=bool(data.get("mostrar_formato", True)),
            idioma=texto("idioma", "").strip().lower()[:5].strip(),
            tema=tema if tema in TEMAS else base.tema,
        )


class ConfigManager:
    """Lectura y escritura de `config.json`. La clave de API NO se guarda aquí jamás."""

    @staticmethod
    def cargar(path: Optional[Path] = None) -> Configuracion:
        path = path or paths.CONFIG_FILE
        if not path.exists():
            return Configuracion()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config.json no es un objeto")
            return Configuracion.desde_dict(data)
        except Exception:
            logger.exception("config.json ilegible; se aparta como .corrupt.json")
            try:
                path.replace(path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return Configuracion()

    @staticmethod
    def guardar(config: Configuracion, path: Optional[Path] = None) -> None:
        path = path or paths.CONFIG_FILE
        try:
            write_atomic(path, json.dumps(config.a_dict(), indent=2, ensure_ascii=False))
        except Exception:
            logger.exception("No se pudo guardar la configuración")
