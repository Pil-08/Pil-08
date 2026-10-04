# Claude Batch Monitor — guía completa

*(English summary in [README.md](README.md).)*

**Una cola de preguntas para Claude.** Escribes tus preguntas; el programa las guarda y las envía una a una. Si la API pide esperar (límite por minuto, servidores saturados, sin internet) **espera solo y continúa justo donde se quedó**, y todo va protegido por un **tope de gasto** que no se puede saltar por accidente.

¿Es una cola que «guarda lo que le pones y lo manda solo cuando la IA vuelve a estar activa»? Sí, con una precisión importante: usa la **API de Anthropic** (de pago por uso). Los límites de una suscripción Pro/Max de claude.ai o de Claude Code son otro sistema y este programa no los ve (ver [Límites](#qué-pasa-cuando-hay-un-límite-o-un-error)).

## Índice

1. [Instalar](#instalar)
2. [Primeros pasos](#primeros-pasos)
3. [Modo simple](#modo-simple)
4. [Modo avanzado](#modo-avanzado)
5. [Terminal (`--cli`)](#terminal---cli)
6. [Qué pasa cuando hay un límite o un error](#qué-pasa-cuando-hay-un-límite-o-un-error)
7. [Dinero: tope, precios, caché y modo lote](#dinero-tope-precios-caché-y-modo-lote)
8. [Datos, privacidad y seguridad](#datos-privacidad-y-seguridad)
9. [Preguntas frecuentes](#preguntas-frecuentes)
10. [Problemas habituales](#problemas-habituales)

## Instalar

**Sin Python** — en la página de [Releases](../../releases): `.exe` para Windows, `.zip` para macOS y un binario para Linux. No están firmados, así que la primera vez Windows (SmartScreen: *Más información → Ejecutar de todas formas*) y macOS (clic derecho → *Abrir*) avisarán.

**Con Python 3.9 o superior** (hace falta Tk: en Debian/Ubuntu `sudo apt install python3-tk`):

```bash
git clone https://github.com/Pil-08/claude-batch-monitor
cd claude-batch-monitor
pip install -e ".[all]"
python -m claude_batch_monitor
```

`[all]` añade tres extras opcionales: **keyring** (guardar la clave en el llavero del sistema), **bandeja del sistema** (seguir trabajando con la ventana cerrada) y **notificaciones de escritorio**. Sin ellos todo funciona igual, salvo esas tres cosas.

## Primeros pasos

Necesitas una **clave de API de Anthropic**: crea una cuenta en [platform.claude.com](https://platform.claude.com), añade un poco de saldo (con unos dólares hay para cientos de preguntas) y crea una «API key». La primera vez que abres el programa te lo explica y tiene un botón para abrir la consola; pega la clave y pulsa **Probar y guardar**.

La clave se guarda en el llavero de tu sistema (Windows Credential Manager, Keychain de macOS, Secret Service de Linux). Si tu sistema no tiene llavero, el programa te lo dice y tendrás que pegarla cada vez (o definir la variable de entorno `ANTHROPIC_API_KEY`). **Nunca** se escribe en ningún archivo.

## Modo simple

El que se abre por defecto. Escribes en la caja de arriba y pulsas **Enter** (*Mayús+Enter* añade una línea). La pregunta pasa a la **cola** de la izquierda y la respuesta aparece a la derecha, escribiéndose en directo.

- Puedes seguir escribiendo preguntas mientras se responde otra: se envían **de una en una y en orden**.
- **Pausar / Reanudar** detiene el envío sin perder nada.
- Clic derecho en una pregunta: copiar respuesta, copiar pregunta, volver a preguntar, quitar de la cola.
- **Guardar todo…** exporta cola y respuestas a `.csv`, `.jsonl`, `.md` o `.txt`.
- **Nueva cola** vacía la lista (la anterior se queda en disco).
- Si cierras el programa con preguntas pendientes, te pregunta si quieres dejarlo trabajando en la bandeja o cerrarlo. Lo pendiente **no se pierde**: al volver a abrir te lo ofrece (o lo retoma solo si activas *Continuar sola lo pendiente* en Opciones).
- **Opciones**: modelo, tope de gasto, idioma (español/inglés), enlace al modo avanzado.

Valores prudentes la primera vez: tope de **2 $**, respuestas de hasta 8 000 tokens y esfuerzo medio si el modelo lo admite.

La barra de abajo cuenta lo que pasa en lenguaje normal: *«La IA ha llegado a su límite por ahora. Sigo sola en 0:08»*, *«Sin conexión a internet. Reintento en 0:30»*…, y cuando algo no se arregla esperando, aparece un aviso con el botón que lo soluciona (*Subir el tope…*, *Abrir la consola de Anthropic*, *Cambiar la clave…*, *Elegir modelo…*, *Reanudar*).

## Modo avanzado

`python -m claude_batch_monitor --avanzado`, o *Opciones → Modo avanzado…*. Para trabajos grandes o repetibles. Tiene dos pestañas.

**Panel**
- **Órdenes**: una por bloque, separadas por una línea que solo contenga `---` (se puede cambiar el separador). *Cargar archivo…* admite `.txt`/`.md` (se muestra tal cual, para editarlo) y `.csv`/`.tsv`/`.jsonl`: te pregunta qué **columna** lleva el texto y cada fila es una orden. Detecta el separador de Excel en español (`;`).
- **Plantilla**: envuelve cada orden, p. ej. `Traduce al inglés: {input}`. Sin `{input}`, la orden se añade al final.
- **Inmediato** (una petición por orden) o **Lote** (Batches API: **50 % más barato**, resultados en hasta 24 h; el programa lo consulta solo y recoge los resultados).
- **Solo las primeras N**: prueba de humo con 2 filas antes de lanzar 5 000.
- **Estimar coste**: tokens y *peor caso* (si cada respuesta llenara `max_tokens`) antes de gastar nada.
- **Resultados** (con tokens y coste por orden; doble clic para ver y copiar la respuesta completa) y **Actividad** (registro en vivo).
- **Iniciar lote**, **Reanudar** (se convierte en *Reintentar fallidas* si solo quedan las que fallaron), **Detener**, **Exportar…**, **Lotes anteriores…** (abrir uno guardado para ver, exportar o reanudar) y **Más** (carpeta de respuestas, registro, carpeta de datos).

**Ajustes** (todo se guarda solo y un valor inválido te dice cuál es y por qué, en vez de sustituirlo en silencio)
- Clave de API y modelo (*Cargar modelos* consulta tu cuenta), esfuerzo de razonamiento, máximo de tokens por respuesta, prompt de sistema y caché de prompt.
- Peticiones en paralelo, reintentos por orden, pausas, tiempo máximo por petición.
- **Tope de gasto**, precios (vacío = catálogo), calcular el peor caso antes de cada petición.
- Hora límite y **bucle** (repite el lote hasta la hora límite o el tope: ¡gasta!).
- Telegram (token y *chat id* con botón de prueba), iniciar/reanudar al abrir, empezar minimizado, días que se conservan los lotes.

Al reanudar un lote se usan **su modelo y su modo** (no los de la pantalla): mezclar modelos, o reenviar por la API normal un lote creado a mitad de precio, sería un accidente. Los lotes con órdenes sin enviar **no se borran nunca** por antigüedad.

## Terminal (`--cli`)

Para servidores, `cron` y scripts. Los nombres de las opciones están en español.

```bash
python -m claude_batch_monitor --cli --mensaje preguntas.txt --dry-run   # solo estima, no envía nada
python -m claude_batch_monitor --cli --mensaje datos.csv --campo texto --plantilla "Resume: {input}" \
       --modo lote --tope-gasto 3 --exportar resultados.csv
python -m claude_batch_monitor --cli --listar
python -m claude_batch_monitor --cli --reanudar 1a2b3c4d
```

| Grupo | Opciones |
|---|---|
| Entrada | `--mensaje` (texto o archivo `.txt .md .csv .tsv .jsonl`) · `--campo` (columna) · `--plantilla` · `--limit N` · `--separador` · `--reanudar ID` · `--listar` |
| Modelo | `--modelo` · `--system` (texto o archivo) · `--modo inmediato\|lote` · `--max-tokens` · `--esfuerzo low\|medium\|high\|xhigh\|max` · `--sin-cache` · `--concurrencia` |
| Gasto | `--tope-gasto USD` (5 por defecto, 0 = sin tope) · `--precio-in` · `--precio-out` · `--dry-run` |
| Tiempos | `--bucle` · `--hora-limite HH:MM` · `--pausa-ciclos` · `--pausa` |
| Salida | `--exportar ARCHIVO` · `--formato csv\|jsonl\|md\|txt` · `--json` (resumen JSON por stdout, el progreso va a stderr) · `--silencioso` · `--version` |

**Códigos de salida:** `0` completado · `1` fallo, red o terminó con órdenes fallidas · `2` tope de gasto · `3` sin saldo o límite de la cuenta · `4` incompleto (detenido o hora límite) · `5` uso incorrecto. Con `--reanudar` el modo se hereda del lote.

Los archivos exportados incluyen `error_type` por orden: `refusal`, `auth`, `bad_request`, `model_not_found`, `not_found`, `too_large`, `server`, `connection`, `max_retries`, `expired`, `canceled`, `missing`, `unexpected`, `other`.

## Qué pasa cuando hay un límite o un error

| Situación | Qué hace el programa |
|---|---|
| **429, límite por minuto** (peticiones o tokens) | Espera exactamente lo que indica la API (`retry-after`) y reintenta. **No cuenta como fallo.** Además frena antes de chocar si las cabeceras muestran que quedan pocos tokens. |
| **429, tope mensual de gasto de tu nivel de uso** | **Para** (esperar minutos no sirve) y te dice cuándo vuelve el acceso («You will regain access on …»). La orden queda pendiente, no fallida. |
| **Límite de gasto que fijaste tú en la consola** (la API responde 400) | **Para** con un aviso claro. Súbelo en la consola y pulsa *Reanudar*. |
| **Sin saldo** (402, o 400 «credit balance is too low») | **Para**; botón para abrir la facturación. Lo ya respondido está guardado. |
| **Servidor saturado / error 5xx / 529** | Reintenta con espera creciente y aleatoria. Solo estos errores cuentan hacia «reintentos por orden». |
| **Sin conexión a internet** | Espera y reintenta hasta 10 minutos en total; si no vuelve, para y se puede reanudar. |
| **Conexión cortada a mitad de una respuesta** | **Para** en vez de reenviar: el servidor puede haber cobrado ya esa petición y repetirla la cobraría dos veces. |
| **Tiempo de espera agotado** | Reintenta (cuenta como error de servidor). |
| **Clave inválida (401/403)**, **modelo inexistente (404)**, **petición rechazada (400)** | Para con un mensaje que dice qué cambiar. |
| **Orden demasiado grande (413)** | Esa orden falla; **las demás siguen**. |
| **El modelo se niega a responder** (`refusal`) | Se marca *Rechazada*; **se cobra** igualmente (es lo que hace la API) y queda guardada. |
| **Respuesta cortada por `max_tokens`** | Se guarda con una advertencia al final y se marca como truncada. |
| **Se alcanza tu tope de gasto** | Antes de **cada** petición se calcula su peor caso y, si no cabe, no se envía. Una respuesta ya pagada **se guarda siempre antes** de comprobar el tope. |

El programa **no** intenta adivinar el reinicio de una suscripción de claude.ai ni de Claude Code: no es la API.

## Dinero: tope, precios, caché y modo lote

- El **tope** se compara contra lo ya gastado **más el peor caso de lo que está en vuelo**. Con varias peticiones en paralelo cada una reserva su peor caso, de modo que N hilos no se pasan N veces. Por eso puede detenerse un poco antes de llegar a la cifra exacta: es deliberado.
- Los **precios** salen de un catálogo interno (por millón de tokens, entrada/salida): Sonnet 5.5 2/10 $, Haiku 4.5 1/5 $, Opus 5.5 4/20 $, Fable 5.1 10/50 $… Un modelo que no esté en el catálogo se valora con el precio **más caro** de su familia (o el más caro de todos), nunca con el barato, para que el tope no proteja menos de lo que promete. Puedes fijar tus propios precios en Ajustes. **Los precios de Anthropic cambian: compruébalos.**
- **Caché de prompt**: con un prompt de sistema largo y repetido, las lecturas de caché cuestan una fracción (escrituras ×1,25 la primera vez). El coste real se calcula con lo que informa la API (entrada, salida, lecturas y escrituras de caché).
- **Modo lote**: −50 % en todo, hasta 100 000 órdenes o 256 MB por lote, resultados no ordenados (se casan por identificador), caducan a las 24 h (las caducadas no se cobran y se pueden reintentar).
- El modelo por defecto es **Claude Sonnet 5.5**. La documentación de Anthropic (octubre de 2026) anuncia la retirada de Haiku 4.5 para el 15/10/2026.

## Datos, privacidad y seguridad

- Todo se guarda en tu equipo, en `%APPDATA%\ClaudeMonitor` (Windows), `~/Library/Application Support/ClaudeMonitor` (macOS) o `~/.config/ClaudeMonitor` (Linux). Se puede cambiar con la variable `CLAUDE_MONITOR_HOME` (útil para una instalación portátil).
  - `config.json` (ajustes, **sin claves**) · `state.json` (gasto acumulado) · `claude_monitor.log` · `responses/` (un `job_<id>.json` por lote y un `.txt` por respuesta).
- Tus preguntas y respuestas viajan **solo** a Anthropic (y los avisos, a Telegram si lo configuras). El programa no tiene servidor propio ni telemetría.
- La clave y el token de Telegram **nunca** se escriben en archivos, registros ni exportaciones; los errores de red se registran sin su texto porque la URL de Telegram contiene el token.
- Los lotes terminados se borran a los 30 días (configurable, 0 = nunca); los que tienen órdenes sin enviar **no se borran jamás**.
- Mientras trabaja, el programa pide al sistema que no se suspenda (Windows, macOS y Linux con `systemd`).

## Preguntas frecuentes

**¿Sirve con mi plan Pro/Max de claude.ai o con Claude Code?** No. Esos límites no pasan por la API, y no hay forma de consultarlos ni de «esperar a que se reinicien» desde fuera. Este programa trabaja con una clave de la API (de pago por uso).

**¿Cuánto cuesta usarlo?** El programa es gratis (MIT). Pagas a Anthropic lo que consumas. Antes de un lote grande usa *Estimar coste* o `--dry-run`.

**¿Puedo cerrarlo y que siga?** En modo simple, sí: déjalo en la bandeja del sistema (si tienes el extra) o ciérralo y vuelve más tarde, porque lo pendiente se queda en disco. El ordenador tiene que estar encendido para que se envíe algo.

**¿Dónde están mis respuestas?** En la ventana, en *Guardar todo…* / *Exportar…* y en la carpeta `responses/`.

**¿Y si pulso *Detener* en mitad de una respuesta?** Termina la petición en curso (para no perder lo que ya se está cobrando) y no envía ninguna más.

## Problemas habituales

- **«Este Python no incluye tkinter»**: instala `python3-tk` (Linux) o reinstala Python desde python.org marcando *tcl/tk* (Windows/macOS). Mientras tanto la terminal funciona.
- **En un servidor sin pantalla** el programa avisa y sugiere `--cli`.
- **No aparece el icono de la bandeja en Linux**: depende del entorno de escritorio (GNOME necesita la extensión *AppIndicator*). Sin bandeja, el programa simplemente no ofrece esa opción.
- **«Anthropic no reconoce esa clave»**: copiaste la clave incompleta, o es de otra cuenta/organización.
- **Una orden aparece como «Error»** en el modo avanzado: doble clic para leer el motivo; arregla la causa y pulsa *Reintentar fallidas*.
