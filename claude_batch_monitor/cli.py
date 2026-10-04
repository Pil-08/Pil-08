"""Interfaz de línea de comandos: para servidores, cron y scripts.

Códigos de salida (para encadenar con `&&` o `case $?`):

    0  todo completado
    1  error fatal o de red, o terminó con órdenes fallidas
    2  detenido por el tope de gasto
    3  sin saldo, o límite de gasto de la cuenta de Anthropic
    4  incompleto (hora límite o cancelado con Ctrl+C)
    5  uso incorrecto (argumentos, fichero, configuración)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from . import paths
from .config import ConfigManager, Configuracion
from .core.base import FRASES_DE_PARADA, Callbacks, EngineBase, StopReason
from .core.batch import BatchEngine
from .core.sequential import SequentialEngine
from .estimator import estimar
from .export import FORMATOS, ErrorDeExportacion, exportar
from .inputs import ErrorDeEntrada, aplicar_plantilla, leer_ordenes
from .job import Job, JobMessage, listar_jobs, purgar_jobs_antiguos
from .parsing import DEFAULT_SEPARATOR, split_messages
from .paths import logger
from .platform_utils import KeepAwake
from .pricing import DEFAULT_MODEL, NIVELES_ESFUERZO, formatear_usd

EXIT_OK, EXIT_FALLO, EXIT_TOPE, EXIT_SIN_SALDO, EXIT_INCOMPLETO, EXIT_USO = 0, 1, 2, 3, 4, 5

_CODIGOS = {
    StopReason.COMPLETED: EXIT_OK,
    StopReason.FATAL: EXIT_FALLO,
    StopReason.NETWORK: EXIT_FALLO,
    StopReason.BUDGET: EXIT_TOPE,
    StopReason.NO_CREDIT: EXIT_SIN_SALDO,
    StopReason.ACCOUNT_LIMIT: EXIT_SIN_SALDO,
    StopReason.STOPPED: EXIT_INCOMPLETO,
    StopReason.DEADLINE: EXIT_INCOMPLETO,
}


def codigo_de_salida(razon: StopReason, job: Optional[Job] = None) -> int:
    codigo = _CODIGOS.get(razon, EXIT_FALLO)
    # Un lote puede "completarse" con órdenes que la API rechazó: no es un éxito limpio.
    if codigo == EXIT_OK and job is not None and job.fallidas():
        return EXIT_FALLO
    return codigo


def forzar_utf8_en_salida() -> None:
    """Evita `UnicodeEncodeError` al redirigir la salida en Windows.

    La consola real de Windows admite UTF-8, pero una tubería o un fichero usan
    cp1252 y cualquier carácter fuera de él (una tilde rara, un emoji de una
    respuesta de Claude) hacía reventar justo el uso documentado en cron.
    """
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


class _Parser(argparse.ArgumentParser):
    """argparse sale con 2 por defecto, que aquí significa "tope de gasto". Uso incorrecto = 5."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USO, f"{self.prog}: error: {message}\n")


def construir_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="claude-batch-monitor --cli",
        description="Procesa una lista de órdenes con Claude, con tope de gasto y reanudación.",
        epilog="Códigos de salida: 0 ok · 1 fallo · 2 tope de gasto · 3 sin saldo/límite de cuenta · "
        "4 incompleto · 5 uso incorrecto",
    )
    e = p.add_argument_group("entrada")
    e.add_argument("--mensaje", help="Texto literal o ruta a un fichero (.txt, .md, .csv, .tsv, .jsonl)")
    e.add_argument("--campo", help="Columna del CSV o campo del JSONL con el texto (por defecto 'input')")
    e.add_argument("--plantilla", help="Envuelve cada orden: --plantilla 'Traduce:\\n{input}'")
    e.add_argument("--limit", type=int, help="Procesa solo las N primeras órdenes (prueba de humo)")
    e.add_argument("--separador", default=DEFAULT_SEPARATOR, help="Separador de órdenes en texto plano")
    e.add_argument("--reanudar", metavar="JOB_ID", help="Continúa un lote existente")
    e.add_argument("--listar", action="store_true", help="Lista los lotes guardados y sale")

    m = p.add_argument_group("modelo")
    m.add_argument("--modelo", help=f"Por defecto {DEFAULT_MODEL}; al reanudar, el del lote")
    m.add_argument("--system", help="Prompt de sistema compartido (texto o ruta a fichero)")
    m.add_argument("--modo", choices=("inmediato", "lote"),
                   help="'lote' = Batches API, 50 %% más barato. Al reanudar se hereda el del lote")
    m.add_argument("--max-tokens", type=int, help="Tokens máximos de salida por orden (por defecto 16000)")
    m.add_argument("--esfuerzo", choices=NIVELES_ESFUERZO, help="Nivel de esfuerzo (solo en modelos que lo soportan)")
    m.add_argument("--sin-cache", action="store_true", help="Desactiva el prompt caching")
    m.add_argument("--concurrencia", type=int, default=1, help="Peticiones en paralelo (modo inmediato)")

    g = p.add_argument_group("gasto")
    g.add_argument("--tope-gasto", default="5", help="USD máximos; 0 = sin tope (por defecto 5)")
    g.add_argument("--precio-in", help="USD por millón de tokens de entrada (si no, el del catálogo)")
    g.add_argument("--precio-out", help="USD por millón de tokens de salida (si no, el del catálogo)")
    g.add_argument("--dry-run", action="store_true", help="Estima el coste y sale sin enviar nada")

    t = p.add_argument_group("tiempos")
    t.add_argument("--bucle", action="store_true", help="ATENCIÓN: repite el lote hasta la hora límite o el tope")
    t.add_argument("--hora-limite", help="HH:MM. En bucle, 23:59 por defecto")
    t.add_argument("--pausa-ciclos", type=int, default=10, help="Segundos entre ciclos del bucle")
    t.add_argument("--pausa", type=int, default=0, help="Segundos de pausa entre órdenes")

    s = p.add_argument_group("salida")
    s.add_argument("--exportar", metavar="FICHERO", help="Escribe los resultados (.csv .jsonl .md .txt)")
    s.add_argument("--formato", choices=FORMATOS, help="Fuerza el formato de --exportar")
    s.add_argument("--json", action="store_true", help="Resumen final en JSON por stdout (el progreso va a stderr)")
    s.add_argument("--silencioso", action="store_true", help="Solo errores y resumen")
    s.add_argument("--version", action="store_true", help="Muestra la versión y sale")
    return p


# ----------------------------------------------------------------------


class _Salida:
    """Dónde va lo que se imprime: con --json, el progreso a stderr para no ensuciar stdout."""

    def __init__(self, args: argparse.Namespace):
        self.json = args.json
        self.silencioso = args.silencioso
        self.flujo = sys.stderr if args.json else sys.stdout
        self.tty = self.flujo.isatty()

    def info(self, texto: str = "") -> None:
        if not self.silencioso:
            print(texto, file=self.flujo, flush=True)

    def aviso(self, texto: str) -> None:
        print(f"AVISO: {texto}", file=sys.stderr, flush=True)

    def error(self, texto: str) -> None:
        print(f"ERROR: {texto}", file=sys.stderr, flush=True)


def _texto_o_fichero(valor: Optional[str]) -> str:
    if not valor:
        return ""
    ruta = Path(valor)
    try:
        if ruta.is_file():
            return ruta.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        pass
    return valor


def _decimal(valor: Optional[str], nombre: str) -> Optional[Decimal]:
    if valor is None:
        return None
    try:
        d = Decimal(str(valor).replace(",", "."))
    except InvalidOperation:
        raise ErrorDeEntrada(f"{nombre} no es un número: {valor!r}") from None
    if not d.is_finite() or d < 0:
        raise ErrorDeEntrada(f"{nombre} debe ser un número igual o mayor que 0.")
    return d


def _ordenes_desde_args(args: argparse.Namespace) -> list[str]:
    origen = args.mensaje
    if Path(origen).is_file():
        return leer_ordenes(
            origen, campo=args.campo, separador=args.separador,
            plantilla=args.plantilla or "", limite=args.limit,
        )
    ordenes = split_messages(origen, args.separador)
    if not ordenes:
        raise ErrorDeEntrada("No se detectó ninguna orden en --mensaje.")
    if args.limit and args.limit > 0:
        ordenes = ordenes[: args.limit]
    return [aplicar_plantilla(args.plantilla or "", o) for o in ordenes]


def _configuracion(args: argparse.Namespace, job: Optional[Job]) -> Configuracion:
    """Config a partir de los argumentos. Al reanudar, lo no pedido se hereda del lote (X4)."""
    c = Configuracion()
    c.modelo = args.modelo or (job.modelo if job else DEFAULT_MODEL)
    # `--modo` NO tiene valor por defecto: antes valía 'inmediato' y pisaba el del lote,
    # de modo que reanudar por CLI un lote creado en modo lote lo reenviaba entero por
    # la API normal, al doble de precio y sin recoger el lote ya pagado en el servidor.
    c.modo = args.modo or (job.modo if job else "inmediato")
    c.system_prompt = _texto_o_fichero(args.system) if args.system is not None else (job.system_prompt if job else "")
    c.separador = args.separador
    c.usar_cache = not args.sin_cache
    c.esfuerzo = args.esfuerzo or ""
    if args.max_tokens is not None:
        c.max_tokens = args.max_tokens
    c.concurrencia = max(1, args.concurrencia)
    c.tope_gasto_usd = _decimal(args.tope_gasto, "--tope-gasto") or Decimal(0)
    c.precio_input_mtok = _decimal(args.precio_in, "--precio-in")
    c.precio_output_mtok = _decimal(args.precio_out, "--precio-out")
    c.modo_continuo = args.bucle
    c.hora_limite = args.hora_limite or ""
    c.pausa_ciclos_sec = max(1, args.pausa_ciclos)
    c.pausa_entre_mensajes_sec = max(0, args.pausa)
    return c


def _resumen(razon: StopReason, job: Job, engine: Optional[EngineBase], exportado: Optional[Path]) -> dict:
    e = job.estadisticas()
    return {
        "job_id": job.job_id,
        "razon": razon.value,
        "modelo": job.modelo,
        "modo": job.modo,
        "total": e["total"],
        "completadas": e["completadas"],
        "pendientes": e["pendientes"],
        "fallidas": e["fallidas"],
        "rechazadas": e["rechazadas"],
        "coste_usd": str(e["coste_usd"]),
        "gasto_acumulado_usd": str(engine.gasto_actual()) if engine else str(e["coste_usd"]),
        "batch_id": job.batch_id or None,
        "exportado": str(exportado) if exportado else None,
        "codigo_de_salida": codigo_de_salida(razon, job),
    }


def _callbacks(salida: _Salida, args: argparse.Namespace) -> Callbacks:
    def estado(texto: str, activo: bool) -> None:
        salida.info(f"[{datetime.now():%H:%M:%S}] {texto}")

    def resultado(msg: JobMessage) -> None:
        marca = {"completed": "OK", "refused": "RECHAZADA", "failed": "FALLO"}.get(msg.status, msg.status)
        extra = f" {msg.detalle}" if msg.detalle else ""
        salida.info(f"  orden {msg.id}: {marca} · {msg.tokens_input + msg.tokens_output} tok · "
                    f"{formatear_usd(msg.coste_usd, 6)}{extra}")

    def tick(total: float, restante: float) -> None:
        if salida.tty and not salida.silencioso:
            print(f"\r  esperando… {int(restante)} s ", end="", file=salida.flujo, flush=True)

    return Callbacks(
        estado=estado,
        resultado=resultado,
        error=lambda texto: salida.error(texto),
        fin_ciclo=lambda c, t, n: salida.info(f"Ciclo {n} terminado: {c}/{t} completadas."),
        tick=tick,
    )


# ----------------------------------------------------------------------


def _cliente_para_conteo() -> Optional[Any]:
    """Cliente para contar tokens exactos en el dry-run, si hay clave. Si no, estimación local."""
    from .state import obtener_api_key

    clave = obtener_api_key()
    if not clave:
        return None
    try:
        import anthropic

        return anthropic.Anthropic(api_key=clave, timeout=15, max_retries=0)
    except Exception:
        return None


def main_cli(argv: Optional[list[str]] = None) -> int:
    forzar_utf8_en_salida()
    parser = construir_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as salir:  # --help (0) o argumentos incorrectos (5): se devuelve, no se propaga
        return salir.code if isinstance(salir.code, int) else EXIT_USO
    salida = _Salida(args)

    if args.version:
        print(f"Claude Batch Monitor {paths.APP_VERSION}")
        return EXIT_OK

    if args.listar:
        lotes = listar_jobs()
        if args.json:
            print(json.dumps([{**d, "coste_usd": str(d["coste_usd"])} for d in lotes], ensure_ascii=False))
        elif not lotes:
            print("No hay lotes guardados.")
        else:
            for d in lotes:
                marca = "  <- incompleto" if d["incompleto"] else ""
                print(f"{d['job_id']}  {d['created_at'][:16]}  {d['modelo']:<22} {d['modo']:<9} "
                      f"{d['completadas']}/{d['total']}  {formatear_usd(d['coste_usd'])}{marca}")
        return EXIT_OK

    if not args.mensaje and not args.reanudar:
        parser.print_usage(sys.stderr)
        salida.error("indica --mensaje (texto o fichero) o --reanudar JOB_ID")
        return EXIT_USO

    # ---- el lote ----
    job: Optional[Job] = None
    try:
        if args.reanudar:
            job = Job.load(args.reanudar)
            if job is None:
                salida.error(f"No existe el lote '{args.reanudar}'. Usa --listar para ver los guardados.")
                return EXIT_USO
        config = _configuracion(args, job)
        ordenes = None if job else _ordenes_desde_args(args)
    except (ErrorDeEntrada, ErrorDeExportacion) as e:
        salida.error(str(e))
        return EXIT_USO

    if config.modo_continuo and config.es_lote:
        salida.error("--bucle solo existe en modo inmediato: repetir un lote entero sería muy caro.")
        return EXIT_USO

    if job is not None:
        for aviso in job.incompatibilidades(config):
            salida.aviso(aviso)
        reintentadas = job.reintentar_fallidas()
        if reintentadas:
            salida.info(f"{reintentadas} orden(es) fallida(s) se reintentarán.")
        textos = [m.text for m in job.pendientes()]
    else:
        textos = ordenes or []

    validacion = config.validar()
    for aviso in validacion.avisos:
        salida.aviso(aviso)
    if not validacion.ok:
        for err in validacion.errores:
            salida.error(err)
        return EXIT_USO
    if args.limit and args.limit > 0 and job is None:
        salida.aviso(f"--limit {args.limit}: solo se procesarán las primeras {len(textos)} órdenes.")

    # ---- estimación ----
    est = estimar(config, textos, client=_cliente_para_conteo() if args.dry_run else None)
    for aviso in est.avisos:
        if aviso not in validacion.avisos:
            salida.aviso(aviso)
    if args.dry_run:
        if args.json:
            print(json.dumps({
                "ordenes": est.ordenes, "modo": est.modo, "tokens_entrada": est.tokens_entrada,
                "coste_maximo_usd": str(est.coste_maximo), "tope_gasto_usd": str(config.tope_gasto_usd),
            }, ensure_ascii=False))
        else:
            print("\n".join(est.resumen_lineas()))
            tope = "sin tope" if config.tope_gasto_usd == 0 else formatear_usd(config.tope_gasto_usd)
            print(f"Tope configurado   : {tope}")
            print("\nNo se ha enviado nada (--dry-run).")
        return EXIT_OK
    if not textos:
        salida.info("No queda nada pendiente en ese lote.")
        # Aun así se exporta lo que haya, y se sale como completado.

    if job is None:
        job = Job.crear(textos, config)
        job.save()
        salida.info(f"Lote {job.job_id}: {len(textos)} órdenes · {config.modelo} · modo {config.modo} · "
                    f"peor caso {formatear_usd(est.coste_maximo)}")

    # ---- ejecución ----
    try:
        # Los lotes con órdenes sin enviar nunca se borran (lo garantiza la propia función).
        purgar_jobs_antiguos(ConfigManager.cargar().retencion_dias, proteger=job.job_id)
    except Exception:
        logger.exception("No se pudieron purgar los lotes antiguos")

    clase = BatchEngine if config.es_lote else SequentialEngine
    motor = clase(config, job)
    cb = _callbacks(salida, args)
    razon = StopReason.FATAL
    try:
        with KeepAwake():
            razon = motor.ejecutar(cb) if job.hay_trabajo() else StopReason.COMPLETED
    except KeyboardInterrupt:
        salida.info("\nDetenido por el usuario. El lote queda guardado.")
        motor.detener()
        razon = StopReason.STOPPED

    # ---- salida ----
    exportado = None
    if args.exportar:
        try:
            exportado = exportar(job, Path(args.exportar), args.formato)
            salida.info(f"Resultados exportados a {exportado}")
        except (ErrorDeExportacion, OSError) as e:
            salida.error(f"No se pudo exportar: {e}")

    resumen = _resumen(razon, job, motor, exportado)
    if args.json:
        print(json.dumps(resumen, ensure_ascii=False))
    else:
        print(f"\n{FRASES_DE_PARADA.get(razon, razon.value)}")
        print(f"  Completadas {resumen['completadas']}/{resumen['total']}"
              f" · fallidas {resumen['fallidas']} · rechazadas {resumen['rechazadas']}"
              f" · pendientes {resumen['pendientes']}")
        print(f"  Coste de este lote: {formatear_usd(job.coste_total())}")
        if resumen["pendientes"] or resumen["fallidas"]:
            print(f"  Para continuar: --reanudar {job.job_id}")
    return resumen["codigo_de_salida"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main_cli())
