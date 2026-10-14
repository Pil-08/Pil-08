# Claude Batch Monitor — guía completa

*(English summary in [README.md](README.md).)*

**Una cola de preguntas para Claude.** Escribes tus preguntas; el programa las guarda y las envía una a una. Si Claude pide esperar (se acabó el cupo de tu plan, límite por minuto de la API, servidores saturados, sin internet) **espera solo y continúa justo donde se quedó**. Funciona **con tu plan de Claude (Pro/Max), sin pagar nada más**, o con una clave de la API protegida por un **tope de gasto** que no se puede saltar por accidente.

¿Es una cola que «guarda lo que le pones y lo manda solo cuando la IA vuelve a estar activa»? Sí, y con las dos formas de usar Claude:

- **Con tu plan de Claude (Pro/Max):** usa la sesión que ya pagas, a través de Claude Code (el programa oficial de Anthropic). **No gasta dinero por pregunta; si el plan está lleno y tienes el uso extra activado, espera a que se renueve, y se detiene si Claude Code empieza a usar créditos extra.** Cuando se acaba el cupo de tu plan, espera a la hora exacta en que se renueva y sigue sola (ver [Con tu plan de Claude](#con-tu-plan-de-claude-promax)).
- **Con una clave de la API** (de pago por uso): con el tope de gasto de siempre.

## Índice

1. [Instalar](#instalar)
2. [Primeros pasos](#primeros-pasos)
3. [Con tu plan de Claude (Pro/Max)](#con-tu-plan-de-claude-promax)
4. [Modo simple](#modo-simple)
5. [Modo avanzado](#modo-avanzado)
6. [Terminal (`--cli`)](#terminal---cli)
7. [Franja horaria: trabajar solo de noche](#franja-horaria-trabajar-solo-de-noche)
8. [Qué pasa cuando hay un límite o un error](#qué-pasa-cuando-hay-un-límite-o-un-error)
9. [Dinero: tope, precios, caché y modo lote](#dinero-tope-precios-caché-y-modo-lote)
10. [Datos, privacidad y seguridad](#datos-privacidad-y-seguridad)
11. [Preguntas frecuentes](#preguntas-frecuentes)
12. [Problemas habituales](#problemas-habituales)

## Instalar

### La forma fácil: un solo archivo

1. Instala [Python 3.9 o superior](https://www.python.org/downloads/). En Windows y macOS deja marcada la casilla *tcl/tk*; en Debian/Ubuntu: `sudo apt install python3-tk`.
2. Descarga **[`claude_batch_monitor.py`](claude_batch_monitor.py)**: ese único archivo **es** todo el programa (en GitHub, ábrelo y pulsa *Download raw file*).
3. **Windows:** doble clic. **macOS / Linux:** en una terminal, `python3 claude_batch_monitor.py`.
4. La primera pantalla te pregunta cómo quieres usar Claude: **con mi plan de Claude** (comprueba que Claude Code esté listo; no hay nada que instalar ni que pegar) o **con una clave de API** (te ofrece instalar, con un botón, la única librería que necesita —`anthropic`, desde PyPI— y te pide la clave). Después escribes una pregunta y pulsas Enter.

Es Python normal y legible: ábrelo con cualquier editor. Arriba tiene un índice con las secciones numeradas. No hay instalador, ni empaquetado, ni pasos ocultos. *(Opcional, en Windows: renómbralo a `claude_batch_monitor.pyw` para que no se abra una ventana negra junto al programa.)*

### Sin Python

En la página de [Releases](../../releases): `.exe` para Windows, `.zip` para macOS y un binario para Linux. No están firmados, así que la primera vez Windows (SmartScreen: *Más información → Ejecutar de todas formas*) y macOS (clic derecho → *Abrir*) avisarán.

### Desde el repositorio (para leerlo, cambiarlo o colaborar)

```bash
git clone https://github.com/Pil-08/claude-batch-monitor
cd claude-batch-monitor
pip install -e ".[all]"
python -m claude_batch_monitor
```

`[all]` añade tres extras opcionales: **keyring** (guardar la clave en el llavero del sistema), **bandeja del sistema** (seguir trabajando con la ventana cerrada) y **notificaciones de escritorio**. Sin ellos todo funciona igual, salvo esas tres cosas.

En todos los ejemplos de esta guía, `python -m claude_batch_monitor` equivale a `python3 claude_batch_monitor.py` (en Windows, `py` en lugar de `python3`).

## Primeros pasos

La primera pantalla tiene dos pestañas:

- **Con mi plan de Claude** (la primera). Necesitas [Claude Code](https://code.claude.com/docs/en/setup) instalado y con tu sesión iniciada con tu cuenta de Claude (abre `claude` una vez, o ejecuta `claude auth login`). El programa lo comprueba solo y te dice qué falta (*no está instalado*, *no has iniciado sesión*, *está iniciado con una clave de API, no con tu plan*…); cuando está listo se activa **Usar mi plan**. No hay clave que pegar ni saldo que añadir.
- **Con una clave de API.** Necesitas una **clave de API de Anthropic**: crea una cuenta en [platform.claude.com](https://platform.claude.com), añade un poco de saldo (con unos dólares hay para cientos de preguntas) y crea una «API key». La pestaña tiene un botón para abrir la consola; pega la clave y pulsa **Probar y guardar**.

La clave se guarda en el llavero de tu sistema (Windows Credential Manager, Keychain de macOS, Secret Service de Linux). Si tu sistema no tiene llavero, el programa te lo dice y tendrás que pegarla cada vez (o definir la variable de entorno `ANTHROPIC_API_KEY`). **Nunca** se escribe en ningún archivo. Con el plan no hay ninguna clave: la sesión es la de Claude Code y este programa no la ve.

Puedes cambiar de una a otra cuando quieras en **Opciones → Cómo usar Claude**.

## Con tu plan de Claude (Pro/Max)

Cada pregunta la responde **Claude Code** (`claude -p`), el programa oficial de Anthropic, con la sesión que ya tienes iniciada. Lo que se gasta es el **cupo de tu suscripción**, no dinero por pregunta ni créditos de la API. La cola, el formato de las respuestas, guardar/exportar y el resto funcionan igual que con la clave.

**Cuando se acaba el cupo** (la ventana de 5 horas, o la semanal), el programa **no falla ni se rinde**: lee de Claude Code a qué hora se renueva, lo cuenta en la barra de abajo («*Has llegado al límite de tu plan. Sigo sola a las 17:23 (faltan 1:52:10)*») y **sigue solo en cuanto se renueva** (con 90 segundos de margen). Si cierras el programa o apagas el equipo, la espera queda apuntada en disco y se retoma al volver a abrirlo.

Qué hace para quedarse **en tu plan y no en tu cartera**:

- Lanza el programa `claude` **sin modificarlo**, una pregunta cada vez, con tu propia sesión. **Nunca lee, guarda ni reenvía tus credenciales.**
- Quita `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` y los interruptores de Bedrock/Vertex/Foundry del entorno de ese proceso (si no, Claude Code usaría la clave y la cobraría en lugar de gastar tu plan). Además lee lo que dice Claude Code sobre de dónde salen sus credenciales y **se detiene al momento** si dice «clave de API».
- Si tu cuenta tiene activado el **«uso extra»** (créditos de pago más allá del cupo), Claude Code seguiría gastándolos al agotarse el plan. Por eso el programa **mira antes de preguntar**: si la última respuesta dice que una ventana de tu plan (la de 5 horas o la semanal) está al 100 % y el uso extra está activado, espera a que esa ventana se renueve en vez de enviar la pregunta siguiente. Como red de seguridad, además, **se detiene en cuanto Claude Code avisa de que ha empezado a usar créditos de pago**, guardando antes la respuesta que acaba de recibir, y te lo dice. Claude Code da las dos informaciones al final de una respuesta, así que el programa no puede saberlo antes de la primera respuesta de una ejecución y *una* respuesta aún puede salir con esos créditos; para descartar incluso eso, desactiva el uso extra en los ajustes de tu cuenta de claude.ai.
- Tu pregunta viaja por la entrada estándar y las instrucciones por un archivo, así que nada tuyo aparece en la lista de procesos; la carpeta de trabajo es una vacía y propia del programa, las herramientas y los comandos de Claude Code están desactivados y no se guarda ninguna sesión.
- Si Claude Code avisa de un límite pero no dice cuándo vuelve, espera en pasos crecientes (de 5 a 30 minutos) y, pasado un rato largo, **se detiene explicando por qué** en vez de esperar para siempre.

Lo que se ve de tu plan: abajo, en la ventana simple, «*Plan: sesión 13 % (se renueva a las 17:23) · semana 2 % (se renueva el 19/10 a las 13:23)*» (también en la terminal). El enlace **Ver mi uso** abre [claude.ai/settings/usage](https://claude.ai/settings/usage).

Conviene saber:
- El cupo es **compartido** con claude.ai y con Claude Code: lo que preguntes aquí gasta del mismo cupo.
- Busca `claude` en tu PATH y en los sitios habituales (el instalador oficial, Homebrew, npm, nvm, fnm, pnpm, Volta, Bun, asdf, mise…), cosa que importa cuando el programa se abre con doble clic y no hereda el PATH de la terminal. Si el tuyo está en otro sitio, *Elegir dónde está…* (en la primera pantalla o en el aviso que sale) te deja señalarlo.
- Los límites de los planes están pensados para un uso personal normal («*ordinary, individual usage*», dicen las [condiciones de Claude Code](https://code.claude.com/docs/en/legal-and-compliance)). Si vas a automatizar mucho, léelas antes.
- **Qué cuenta Anthropic hoy (octubre de 2026):** su [artículo de ayuda](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) dice que `claude -p` con tu sesión de plan sigue gastando **los límites de tu suscripción**. Anthropic había anunciado un cambio en cómo se cuenta ese uso (un cupo de créditos aparte, desde el 15 de junio de 2026) y después **lo dejó en pausa** («*for now, nothing has changed*»). Puede volver a cambiar: si un día `claude -p` pasa a gastar otra cosa, este programa se detiene en cuanto Claude Code avise de créditos extra o de pago (ver arriba), pero conviene que lo compruebes en esa página.
- Depende del formato de salida de tu versión de Claude Code (`--output-format stream-json`). Si una versión futura lo cambia de forma que el programa no entienda un límite, hará lo prudente: esperar en pasos y, si no se arregla, detenerse con un aviso.
- Necesita Claude Code 2.0 o más nuevo (`claude update`).

## Modo simple

El que se abre por defecto. Escribes en la caja de arriba y pulsas **Enter** (*Mayús+Enter* añade una línea). La pregunta pasa a la **cola** de la izquierda y la respuesta aparece a la derecha, escribiéndose en directo.

- Puedes seguir escribiendo preguntas mientras se responde otra: se envían **de una en una y en orden**.
- **Pausar / Reanudar** detiene el envío sin perder nada.
- Clic derecho en una pregunta: copiar respuesta, copiar pregunta, volver a preguntar, quitar de la cola.
- **Guardar todo…** exporta cola y respuestas a `.csv`, `.jsonl`, `.md` o `.txt`.
- **Nueva cola** vacía la lista (la anterior se queda en disco).
- Si cierras el programa con preguntas pendientes, te pregunta si quieres dejarlo trabajando en la bandeja o cerrarlo. Lo pendiente **no se pierde**: al volver a abrir te lo ofrece (o lo retoma solo si activas *Continuar sola lo pendiente* en Opciones).
- Las respuestas se ven **con formato** (títulos, negrita, cursiva, listas, citas, código, tablas y enlaces). El interruptor *Con formato* enseña el texto original; lo que se guarda, se copia y se exporta no cambia nunca.
- **Opciones**: **cómo usar Claude** (con tu plan o con una clave; con el plan, qué modelo: `sonnet`, `opus`, `haiku`, un identificador completo o «El de mi plan»), modelo de la API, **instrucciones para todas las respuestas** (p. ej. «Responde siempre en español y con frases cortas»; se envían con cada pregunta), **franja horaria**, tope de gasto, idioma (español/inglés), **tema** (automático, claro u oscuro; se aplica al reiniciar) y enlace al modo avanzado.

Valores prudentes la primera vez con la clave: tope de **2 $**, respuestas de hasta 8 000 tokens y esfuerzo medio si el modelo lo admite.

La barra de abajo cuenta lo que pasa en lenguaje normal: *«La IA ha llegado a su límite por ahora. Sigo sola en 0:08»*, *«Has llegado al límite de tu plan. Sigo sola a las 17:23 (faltan 1:52:10)»*, *«Sin conexión a internet. Reintento en 0:30»*…, y cuando algo no se arregla esperando, aparece un aviso con el botón que lo soluciona (*Subir el tope…*, *Abrir la consola de Anthropic*, *Cambiar la clave…*, *Elegir modelo…*, *Reanudar*).

## Modo avanzado

`python -m claude_batch_monitor --avanzado`, o *Opciones → Modo avanzado…*. Para trabajos grandes o repetibles. Tiene dos pestañas.

**Panel**
- **Órdenes**: una por bloque, separadas por una línea que solo contenga `---` (se puede cambiar el separador). *Cargar archivo…* admite `.txt`/`.md` (se muestra tal cual, para editarlo) y `.csv`/`.tsv`/`.jsonl`: te pregunta qué **columna** lleva el texto y cada fila es una orden. Detecta el separador de Excel en español (`;`).
- **Plantilla**: envuelve cada orden, p. ej. `Traduce al inglés: {input}`. Sin `{input}`, la orden se añade al final.
- **Inmediato** (una petición por orden), **Lote** (Batches API: **50 % más barato**, resultados en hasta 24 h; el programa lo consulta solo y recoge los resultados) o **Con mi plan de Claude** (a través de Claude Code, sin pagar por token; las órdenes van de una en una y, si se acaba el cupo, espera a que se renueve).
- **Solo las primeras N**: prueba de humo con 2 filas antes de lanzar 5 000.
- **Estimar coste**: tokens y *peor caso* (si cada respuesta llenara `max_tokens`) antes de gastar nada.
- **Resultados** (con tokens y coste por orden; doble clic para ver y copiar la respuesta completa) y **Actividad** (registro en vivo).
- **Iniciar lote**, **Reanudar** (se convierte en *Reintentar fallidas* si solo quedan las que fallaron), **Detener** (en modo lote pregunta si quieres **cancelar también el lote en el servidor**: sigue procesándose y cobrándose aunque dejes de esperar), **Exportar…**, **Lotes anteriores…** (abrir uno guardado para ver, exportar o reanudar) y **Más** (carpeta de respuestas, registro, carpeta de datos).

**Ajustes** (todo se guarda solo y un valor inválido te dice cuál es y por qué, en vez de sustituirlo en silencio)
- **Con tu plan de Claude (Claude Code)**: el botón *Comprobar Claude Code* y el modelo con el plan.
- Clave de API y modelo (*Cargar modelos* consulta tu cuenta), esfuerzo de razonamiento, máximo de tokens por respuesta, prompt de sistema y caché de prompt.
- Peticiones en paralelo, reintentos por orden, pausas, tiempo máximo por petición.
- **Tope de gasto**, precios (vacío = catálogo), calcular el peor caso antes de cada petición.
- Hora límite, **franja horaria** y **bucle** (repite el lote hasta la hora límite o el tope: ¡gasta!; no existe con el plan ni con el modo lote).
- Telegram (token y *chat id* con botón de prueba), iniciar/reanudar al abrir, empezar minimizado, días que se conservan los lotes.

Al reanudar un lote se usan **su modelo y su modo** (no los de la pantalla): mezclar modelos, o reenviar por la API normal un lote creado a mitad de precio, sería un accidente. Los lotes con órdenes sin enviar **no se borran nunca** por antigüedad.

## Terminal (`--cli`)

Para servidores, `cron` y scripts. Los nombres de las opciones están en español.

```bash
python -m claude_batch_monitor --cli --mensaje preguntas.txt --dry-run   # solo estima, no envía nada
python -m claude_batch_monitor --cli --mensaje datos.csv --campo texto --plantilla "Resume: {input}" \
       --modo lote --tope-gasto 3 --exportar resultados.csv
python -m claude_batch_monitor --cli --modo plan --mensaje preguntas.txt --franja 22:00-07:00   # con tu plan, solo de noche
python -m claude_batch_monitor --cli --listar
python -m claude_batch_monitor --cli --reanudar 1a2b3c4d
python -m claude_batch_monitor --cli --cancelar-lote 1a2b3c4d   # cancela en el servidor un lote en curso
```

| Grupo | Opciones |
|---|---|
| Entrada | `--mensaje` (texto o archivo `.txt .md .csv .tsv .jsonl`) · `--campo` (columna) · `--plantilla` · `--limit N` · `--separador` · `--reanudar ID` · `--cancelar-lote ID` · `--listar` |
| Modelo | `--modelo` (con `--modo plan`: `sonnet`, `opus`, `haiku`…) · `--system` (texto o archivo) · `--modo inmediato\|lote\|plan` · `--max-tokens` · `--esfuerzo low\|medium\|high\|xhigh\|max` · `--sin-cache` · `--concurrencia` |
| Gasto | `--tope-gasto USD` (5 por defecto, 0 = sin tope) · `--precio-in` · `--precio-out` · `--dry-run` |
| Tiempos | `--bucle` · `--hora-limite HH:MM` · `--franja HH:MM-HH:MM` · `--pausa-ciclos` · `--pausa` |
| Salida | `--exportar ARCHIVO` · `--formato csv\|jsonl\|md\|txt` · `--json` (resumen JSON por stdout, el progreso va a stderr) · `--silencioso` · `--version` |

**Códigos de salida:** `0` completado · `1` fallo, red o terminó con órdenes fallidas · `2` tope de gasto (o: con el plan, se agotó y Claude Code empezó a usar créditos extra) · `3` sin saldo o límite de la cuenta · `4` incompleto (detenido o hora límite) · `5` uso incorrecto, o ya hay otra copia del programa en marcha. Con `--reanudar` el modo se hereda del lote.

Los archivos exportados incluyen `error_type` por orden: `refusal`, `auth`, `bad_request`, `model_not_found`, `not_found`, `too_large`, `server`, `connection`, `max_retries`, `expired`, `canceled`, `missing`, `unexpected`, `other`.

## Franja horaria: trabajar solo de noche

*Opciones → Trabajar solo en una franja horaria* (modo simple), *Ajustes → Franja horaria* (avanzado) o `--franja 22:00-07:00` (terminal). Fuera de esas horas **no se envía nada**: la cola espera y la barra de abajo dice cuándo sigue («*Fuera de tu franja horaria. Sigo sola a las 22:00 (faltan 3:12:00)*»). La franja puede cruzar la medianoche (`22:00-07:00` = «desde las diez de la noche hasta las siete de la mañana»). El inicio cuenta; el fin, no.

Vale para los tres modos. Con el modo lote solo espera el *envío*: recoger los resultados no es enviar. Una pregunta que ya se está respondiendo cuando termina la franja acaba normal: la franja decide cuándo *empezar* una pregunta, no la corta a mitad.

## Qué pasa cuando hay un límite o un error

| Situación | Qué hace el programa |
|---|---|
| **Con el plan: se acabó el cupo** (ventana de 5 horas o semanal) | Lee la hora de renovación de Claude Code, **espera hasta entonces + 90 s** y vuelve a preguntar lo mismo. No cuenta como fallo. La espera se guarda en disco. |
| **Con el plan: avisa del límite pero no dice cuándo vuelve** | Espera en pasos crecientes (5, 10, 15, 30 min) y, pasado un rato largo (unas 7 horas; unos 7 días si era el límite semanal), **para** y te lo explica. |
| **Con el plan: «uso extra» activado y se acabó el cupo** | **Espera a que se renueve** si la última respuesta dice que el plan está al 100 %; y **para en cuanto Claude Code avisa** de que empezó a usar créditos de pago, guardando antes la respuesta recibida (una respuesta pudo salir ya con ellos: desactiva el uso extra en tu cuenta para descartarlo). |
| **Con el plan: te piden créditos / límite de gasto de la cuenta / cuenta en espera** | **Para** con el aviso que corresponde (y el botón *Ver mi uso*). Reanuda cuando lo arregles. |
| **Con el plan: sesión caducada, Claude Code sin instalar o antiguo, o iniciado con una clave de API** | **Para antes de enviar** y dice qué hacer (`claude auth login`, `claude update`…). |
| **429, límite por minuto** (peticiones o tokens) | Espera exactamente lo que indica la API (`retry-after`) y reintenta. **No cuenta como fallo.** Además frena antes de chocar si las cabeceras muestran que quedan pocos tokens. |
| **429, tope mensual de gasto de tu nivel de uso** | **Para** (esperar minutos no sirve) y te dice cuándo vuelve el acceso («You will regain access on …»). La orden queda pendiente, no fallida. |
| **Límite de gasto que fijaste tú en la consola** (la API responde 400) | **Para** con un aviso claro. Súbelo en la consola y pulsa *Reanudar*. |
| **Sin saldo** (402, o 400 «credit balance is too low») | **Para**; botón para abrir la facturación. Lo ya respondido está guardado. |
| **Servidor saturado / error 5xx / 529** | Reintenta con espera creciente y aleatoria. Solo estos errores cuentan hacia «reintentos por orden». |
| **Sin conexión a internet** | Espera y reintenta hasta 10 minutos en total; si no vuelve, para y se puede reanudar. |
| **Conexión cortada a mitad de una respuesta** (también en streaming, o un flujo que termina sin el final del mensaje) | **Para** en vez de reenviar: el servidor puede haber cobrado ya esa petición y repetirla la cobraría dos veces. |
| **Tiempo de espera agotado** | Reintenta, pero la petición abandonada **cuenta como posible gasto** (el servidor pudo cobrarla) y el reintento va en streaming, que no tiene un tiempo total que agotar. Si una misma orden agota el tiempo 3 veces, **para**. |
| **Una respuesta «correcta» que no es de la API** (la página de un proxy o de un portal de wifi, un 200 con HTML) | Reintenta con espera: el servidor no llegó a procesar nada. |
| **Errores 408 y 409** | Se reintentan como los de servidor (el propio SDK de Anthropic hace lo mismo). |
| **Se envía un lote y no llega la respuesta** (tiempo agotado, corte, 5xx, o cerraste el programa en pleno envío) | **No crea un segundo lote.** Antes de enviar anota cuál es el último lote de tu cuenta y apunta el envío; antes de repetirlo (ahora o al volver a abrir) busca, entre los lotes posteriores a ese, uno del mismo tamaño que no sea de otra tarea. Si está, sigue con ese. Si hay varios candidatos, para y te lo dice. |
| **Falla una consulta** (ver el estado de un lote, listar los recientes, bajar los resultados): corte, tiempo agotado, 5xx, página de un proxy | **Reintenta con espera** y no cuenta como riesgo de cobro doble: consultar no cuesta nada. Si no se arregla en los reintentos permitidos (los que no avanzan, al bajar resultados), para y el lote **sigue en el servidor** para reanudar. |
| **Disco lleno, o sin permiso para escribir** | **Para al momento** (seguir sería pagar respuestas que luego se perderían). La respuesta ya pagada sigue en pantalla para copiarla o exportarla y se escribe en disco en cuanto se pueda. |
| **Clave inválida (401/403)**, **modelo inexistente (404)**, **petición rechazada (400)** | Para con un mensaje que dice qué cambiar. |
| **Orden demasiado grande (413)** | Esa orden falla; **las demás siguen**. |
| **El modelo se niega a responder** (`refusal`) | Se marca *Rechazada*; **se cobra** igualmente (es lo que hace la API) y queda guardada. |
| **Respuesta cortada por `max_tokens`** | Se guarda con una advertencia al final y se marca como truncada. |
| **Se alcanza tu tope de gasto** | Antes de **cada** petición se calcula su peor caso y, si no cabe, no se envía. Una respuesta ya pagada **se guarda siempre antes** de comprobar el tope. |

Con el plan, la hora de renovación **no se adivina**: sale de lo que informa Claude Code (o del texto de su aviso). Si Claude Code no la dice, el programa espera en pasos y acaba parando con una explicación, nunca inventando una hora.

## Dinero: tope, precios, caché y modo lote

- El **tope** se compara contra lo ya gastado **más el peor caso de lo que está en vuelo**. Con varias peticiones en paralelo cada una reserva su peor caso, de modo que N hilos no se pasan N veces. Por eso puede detenerse un poco antes de llegar a la cifra exacta: es deliberado.
- Los **precios** salen de un catálogo interno (por millón de tokens, entrada/salida): Sonnet 5.5 2/10 $, Haiku 4.5 1/5 $, Opus 5.5 4/20 $, Fable 5.1 10/50 $… Un modelo que no esté en el catálogo se valora con el precio **más caro** de su familia (o el más caro de todos), nunca con el barato, para que el tope no proteja menos de lo que promete. Puedes fijar tus propios precios en Ajustes. **Los precios de Anthropic cambian: compruébalos.**
- **Caché de prompt**: con un prompt de sistema largo y repetido, las lecturas de caché cuestan una fracción (escrituras ×1,25 la primera vez). El coste real se calcula con lo que informa la API (entrada, salida, lecturas y escrituras de caché).
- **Modo lote**: −50 % en todo, hasta 100 000 órdenes o 256 MB por lote, resultados no ordenados (se casan por identificador), caducan a las 24 h (las caducadas no se cobran y se pueden reintentar).
- El modelo por defecto es **Claude Sonnet 5.5**. La documentación de Anthropic (octubre de 2026) anuncia la retirada de Haiku 4.5 para el 15/10/2026.

## Datos, privacidad y seguridad

- Todo se guarda en tu equipo, en `%APPDATA%\ClaudeMonitor` (Windows), `~/Library/Application Support/ClaudeMonitor` (macOS) o `~/.config/ClaudeMonitor` (Linux). Se puede cambiar con la variable `CLAUDE_MONITOR_HOME` (útil para una instalación portátil).
  - `config.json` (ajustes, **sin claves**) · `state.json` (gasto acumulado) · `claude_monitor.log` · `instancia.lock` (el bloqueo de «una sola copia»; solo guarda un número de proceso) · `responses/` (un `job_<id>.json` por lote y un `.txt` por respuesta).
- Con una clave de API, tus preguntas y respuestas viajan **solo** a `api.anthropic.com`. Con tu plan, este programa no hace ninguna conexión por ellas: las hace Claude Code, a Anthropic. (Los avisos, van a `api.telegram.org` si configuras Telegram; y la descarga de `anthropic` desde PyPI, si aceptas instalarla.) El programa no tiene servidor propio, ni telemetría, ni comprobación de actualizaciones, ni cuentas. Los enlaces (consola, facturación, la página de uso de tu plan, dónde conseguir Claude Code, documentación) se abren en tu navegador solo cuando pulsas.
- La clave y el token de Telegram **nunca** se escriben en archivos, registros ni exportaciones; los errores de red se registran sin su texto porque la URL de Telegram contiene el token.
- Los lotes terminados se borran a los 30 días (configurable, 0 = nunca); los que tienen órdenes sin enviar **no se borran jamás**.
- **Una sola copia a la vez** por carpeta de datos: dos copias se pisarían el gasto acumulado y el tope dejaría de proteger. La segunda avisa y no se abre (en la terminal sale con el código 5). El bloqueo lo suelta el propio sistema si el programa muere, así que no se queda «atascado».
- Las exportaciones a CSV llevan un apóstrofo delante de las celdas de texto que empiezan por `=`, `+`, `-` o `@`, para que Excel o Calc no las ejecuten como fórmulas.
- Mientras trabaja, el programa pide al sistema que no se suspenda (Windows: una llamada de Win32; macOS: `caffeinate`; Linux con `systemd`: `systemd-inhibit`).
- Los demás programas que lanza: con el plan, `claude` (Claude Code) con `--version`, `auth status` y `-p` (la pregunta); `pip install anthropic` (solo si aceptas, y solo para la API), `xdg-open` / `open` / `os.startfile` para abrir una carpeta cuando pulsas el botón, y `defaults` / `gsettings` (en Windows, el registro) para saber si tu sistema usa el tema oscuro. Nada más.
- **No hay código escondido.** El archivo no contiene `exec`, `eval`, `compile` de cadenas, nada decodificado ni descomprimido. Una prueba lo comprueba en cada generación, y otra comprueba que las únicas direcciones web del archivo son las anteriores.
- **Puedes comprobarlo tú**: `python tools/build_single_file.py --check` vuelve a generar el archivo único a partir de los módulos de `claude_batch_monitor/` y te dice si difieren.

## Preguntas frecuentes

**¿Sirve con mi plan Pro/Max de claude.ai o con Claude Code?** Sí: elige *Con mi plan de Claude*. Necesitas Claude Code instalado y con tu sesión iniciada; el programa lo usa como lo usarías tú en una terminal y gasta el cupo de tu plan, no créditos de la API ni créditos extra (si estos están activados, se detiene antes de gastarlos). Ver [Con tu plan de Claude](#con-tu-plan-de-claude-promax).

**¿Por qué no conecta directamente con mi cuenta de claude.ai?** Porque no debe. Según las [condiciones de Claude Code](https://code.claude.com/docs/en/legal-and-compliance), un programa de terceros no puede usar las credenciales de un plan Free/Pro/Max, ni guardarlas ni hacer de intermediario; lo que sí vale es que **tú** inicies sesión, con tu suscripción, en el Claude Code oficial sin modificar. Por eso este programa solo **arranca** ese Claude Code, que ya está conectado, y no ve, guarda ni envía ninguna credencial.

**¿Cuánto cuesta usarlo?** El programa es gratis (MIT). Con tu plan, nada más que lo que ya pagas; con una clave de API, pagas a Anthropic lo que consumas. Antes de un lote grande usa *Estimar coste* o `--dry-run`.

**¿Puedo cerrarlo y que siga?** En modo simple, sí: déjalo en la bandeja del sistema (si tienes el extra) o ciérralo y vuelve más tarde, porque lo pendiente se queda en disco. El ordenador tiene que estar encendido para que se envíe algo.

**¿Dónde están mis respuestas?** En la ventana, en *Guardar todo…* / *Exportar…* y en la carpeta `responses/`.

**¿Y si pulso *Detener* en mitad de una respuesta?** Termina la petición en curso (para no perder lo que ya se está cobrando) y no envía ninguna más. En modo lote, el lote ya está en los servidores de Anthropic: *Detener* te pregunta si quieres cancelarlo también allí (lo ya procesado se cobra; lo demás, no).

**¿Puedo abrir dos copias a la vez?** No sobre la misma carpeta de datos (ver arriba). Si de verdad las necesitas, dale a cada una su carpeta con `CLAUDE_MONITOR_HOME`.

**¿Qué no puede garantizar?**
- Con tu plan: Claude Code cuenta el uso del plan, y si ha empezado a usar créditos extra, al final de una respuesta. El programa espera cuando el plan está lleno y el uso extra activado, pero no sabe nada antes de la primera respuesta de una ejecución, así que una respuesta (o la que cruza la línea) pudo salir ya con créditos; el programa se detiene justo después. Para descartarlo, desactiva el uso extra en los ajustes de tu cuenta de claude.ai.
- Con tu plan: el programa depende del formato de salida de Claude Code y de las normas de Anthropic para los planes, que no controla. Si el formato cambia y no se entiende un límite, espera en pasos y luego se detiene con una explicación; nunca inventa una hora de renovación.
- Que un `claude` que quede vivo si matas este programa de golpe (`kill -9`, un corte de luz) se pare: acaba solo su única respuesta y la pregunta se vuelve a hacer al abrir de nuevo, así que como mucho se gasta dos veces el cupo de una respuesta.
- Que una respuesta cuya conexión se corta *después* de que el servidor la procesara no se cobre: el programa no puede contar lo que nunca recibió. Por eso para y lo dice, en lugar de reenviar.
- Que una petición abandonada por tiempo cuente siempre: su peor caso se suma al gasto **durante esa ejecución**, pero no se recuerda si cierras y vuelves a abrir (la factura de Anthropic es la referencia).
- Que el reloj de tu equipo coincida con el de Anthropic: solo importa si, al enviar un lote, no se pudo leer la lista de tu cuenta; entonces la búsqueda de un lote que quizá ya existe se limita por fecha y se fía de que no difieran más de diez minutos. Y un lote que el servidor aún esté creando justo en ese instante no se ve, por eso antes de reintentar se espera unos segundos.
- Que el gasto anotado sea exacto si matas el programa en el instante de escribir un punto de control al recoger un lote grande: puede contar dos veces como máximo el 5 % del lote (siempre por exceso, nunca por defecto).
- Que Anthropic no deje pasarse un poco a un lote del límite de gasto del *workspace*: el tope de este programa se comprueba **antes de enviar**, con el peor caso de todo el lote.
- Que el tope vea lo que gastan otras herramientas con la misma clave: solo cuenta lo que envía este programa.
- Que un CSV sirva para leer las respuestas desde otro programa: las celdas que empiezan por `=`, `+`, `-` o `@` llevan un apóstrofo delante (por seguridad al abrirlo en una hoja de cálculo). Para leerlas con un programa, usa la exportación JSONL, que no se modifica.

## Problemas habituales

- **Con doble clic no pasa nada, o se abre y se cierra** (Windows): abre una terminal en la carpeta del archivo y ejecuta `py claude_batch_monitor.py`; ahí verás el mensaje. Si el programa tropieza con algo inesperado, además enseña una ventana con el motivo y la ruta del registro.
- **En macOS el doble clic abre un editor**: usa la Terminal (`python3 claude_batch_monitor.py`) o *Abrir con → Python Launcher*.
- **«externally-managed-environment» al instalar `anthropic`** (Debian, Ubuntu, Fedora…): el programa lo detecta y crea una carpeta propia (`venv`, dentro de su carpeta de datos) donde instalarla; no toca el Python del sistema.
- **«Este Python no incluye tkinter»**: instala `python3-tk` (Linux) o reinstala Python desde python.org marcando *tcl/tk* (Windows/macOS). Mientras tanto la terminal funciona.
- **En un servidor sin pantalla** el programa avisa y sugiere `--cli`.
- **No aparece el icono de la bandeja en Linux**: depende del entorno de escritorio (GNOME necesita la extensión *AppIndicator*). Sin bandeja, el programa simplemente no ofrece esa opción.
- **«Anthropic no reconoce esa clave»**: copiaste la clave incompleta, o es de otra cuenta/organización.
- **Una orden aparece como «Error»** en el modo avanzado: doble clic para leer el motivo; arregla la causa y pulsa *Reintentar fallidas*.
