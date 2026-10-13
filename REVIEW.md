# Review of the original v11 script

This is a fresh audit of the original single-file script (v11.0.0, kept verbatim in the first commit of this repository: `git show 72c79e5:claude_batch_monitor.py` — the file of that name in the repository today is the v12 single-file build, not the original). Line numbers refer to that file. For each finding: what was wrong, what it cost the user, and where v12 fixes it (and the test that keeps it fixed).

> **Provenance.** v12 is a rewrite of that script as a tested package (also distributed as one generated file, `claude_batch_monitor.py`, built from the modules and tested with the same suite). An earlier iteration of this work was done in a disposable environment that was lost before it reached a repository; **this version was rebuilt from the v11 file and the same design, then re-verified from scratch.** The findings below were re-derived by reading v11 again, not recalled from the lost notes.

## Money and data loss

| # | v11 behaviour | Consequence | v12 |
|---|---|---|---|
| 1 | **A paid response was thrown away when it reached the spend cap.** `_contabilizar_coste` runs at line 634, *before* the answer is written (644) and the order marked `completed` (653), and returns `False` when the cap is reached. | The model had already been billed, but the answer was never saved and the order stayed pending, so the next run **asked and paid again**. | `registrar_respuesta` persists the answer *first* and only then checks the cap. `test_engine.py::test_C1_persiste_antes_de_comprobar_el_tope` |
| 2 | **The cap was only checked after spending** (524-548) and `max_tokens` was fixed at 4096 (630). | A request could always overshoot the cap by one full response; with parallelism by many. | The worst case of the *next* request is reserved before sending it; with concurrency each in-flight request reserves its own. `test_engine.py::TestReservaDePresupuesto` |
| 3 | **Incomplete cost model.** Only `input_tokens` + `output_tokens` (524-527) priced with three hard-coded families (111-113); cache reads/writes and the Batches discount ignored; an unknown model silently kept the *previous* prices. | The cap protected less than it promised (worst for Opus-class prices left over from a cheaper model). | Model catalogue with cache multipliers and the 50 % batch discount; unknown models get the *expensive* family price. `test_pricing.py::TestPreciosDelCatalogo`, `::TestModelosDesconocidos`, `::TestCoste` |
| 4 | Money held in `float` and accumulated thousands of times (196, 538). | Rounding drift in the one number that decides when to stop. | `Decimal` everywhere. |
| 5 | **Typing the API key in the window could not work without `keyring`.** The UI accepted it (`_get_api_key_from_ui`, 1109-1114) but the engine only reads the environment or the keyring (`_get_api_key`, 396-397); the failure appeared after you confirmed the batch. | The documented GUI path failed for anyone without `keyring`. | `guardar_clave` falls back to the process environment and tells you the key won't survive a restart. `test_claves.py::TestGuardarClave` |
| 6 | Refusals (`stop_reason: "refusal"`, HTTP 200, billed) were saved as ordinary answers; `model_context_window_exceeded` was not recognised. | A refusal looked like a success. | New `refused` status, kept and billed; truncation flagged. `test_engine.py::TestRechazos` |

## Waiting and limits (the point of the program)

| # | v11 behaviour | Consequence | v12 |
|---|---|---|---|
| 7 | **Wait time came from the wrong header.** It preferred `anthropic-ratelimit-*-reset` (426-431), which is when a bucket is *full again*, over `retry-after` (432), which is when to retry. | Waits of minutes where seconds were enough. | `retry-after` first, then the reset headers, with jitter. `test_ratelimit.py::TestPrioridadDeEspera` |
| 8 | **The monthly spend cap of the usage tier looped forever.** That 429 carries no `retry-after`, so v11 fell through to `return 60.0` (441) and retried every minute "without counting as a failure" until the deadline. | A permanent condition treated as transient. | Recognised by its error code; stops and reports *when access returns*. Several 429s with no timing hint also stop. `test_errores_del_motor.py::TestTopeMensualDelTier`, `::TestRateLimitSinInformacion` |
| 9 | A spend limit set *by the user* in the console arrives as HTTP **400**, which v11 reported as a generic "fatal HTTP 400" with raw SDK text (676). | The user couldn't tell what to fix. | Classified and explained; the order stays pending. `test_errores_del_motor.py::TestLimiteFijadoPorElUsuario`, `::TestSinSaldo` |
| 10 | **Losing the connection stopped the whole run** (`except APIConnectionError`, 695-701). And because `APITimeoutError` *subclasses* `APIConnectionError`, a merely slow response (120 s timeout, non-streaming) was also reported as a network failure. | A queue meant to "wait until the AI is back" died at the first Wi-Fi blip. | No-connection → wait up to 10 minutes; timeout → backoff retry; a cut *mid-response* still stops (the server may have billed it). `test_errores_del_motor.py::TestSinConexion` |
| 11 | **HTTP 413 was retried like a server error** (681-694): up to 8 times with exponential backoff (30 s … 30 min) for a request that can never succeed, then the whole batch aborted. | Hours lost, other orders blocked. | That order fails; the rest continue. `test_errores_del_motor.py::TestOtrosErroresHttp::test_orden_demasiado_grande_no_para_el_resto` |
| 12 | **A 23:59 deadline was always applied** (`calcular_deadline`, 411-419, default `hora_limite="23:59"`), even for a one-off batch. | A batch started at 23:58 silently stopped a minute later, labelled "stopped or completed". | A deadline exists only if you set one (loops get 23:59), and the stop reason is reported. `test_motor_extra.py` (deadline tests) |

## Threading and the window

| # | v11 behaviour | v12 |
|---|---|---|
| 13 | The worker thread called Tk directly (`root.after(0, …)`, 1177-1195, and `root.state()` inside `on_error`). Tk is not thread-safe: intermittent *"main thread is not in main loop"* failures. | Workers only enqueue; the window drains the queue on its own thread (`gui/bridge.py`). |
| 14 | `KeepAwake.enable()` on the UI thread (1162), `disable()` on the worker (1217). On Windows the execution state is **per thread**, so the sleep lock was never released; it also used `ES_AWAYMODE_REQUIRED` (meant for media recorders), and did nothing on Linux. | Enabled and released in the worker thread, `systemd-inhibit` on Linux. `test_platform_utils.py` |
| 15 | Settings were parsed leniently (`_read_float`/`_read_int`, 993-1003): typing `abc` in the cap field silently stored `5.0`. | Invalid values are refused and the field is named. `test_gui_avanzada.py::TestAjustes` |
| 16 | Auto-start still showed a confirmation dialog (1144), defeating unattended start. Old batches were never purged. Emoji in widget labels can crash Tk/Xft on some Linux setups. The Telegram test showed `str(e)`, which for urllib can contain the bot URL (and token). | Auto-start doesn't ask; retention purge that never touches pending work; no emoji in widgets; errors report only the exception *type*. |
| 17 | Atomic writes used one fixed temp name per file (`state.tmp`, `config.tmp`, 202…). Harmless while a single thread writes, but corrupting once requests run in parallel. | Unique temp names per writer. |

## Second review: what the stress simulations found in v12 itself (v12.3)

The audit above is of v11. After v12 existed, the program was run against a simulated Anthropic server (`tests/servidor_falso.py`: the real SDK over real sockets, with failures planned per question), under seeded random failures, as real processes, and with property-based tests. These are defects *of v12.2*, found that way. All are fixed in 12.3.0 and described in [CHANGELOG.md](CHANGELOG.md); each has a test that fails without its fix.

| # | What the simulation found | Consequence | Test |
|---|---|---|---|
| 18 | A request that timed out *after being sent* was re-sent up to `max_error_attempts` times; the server may have billed each. | Cost above the cap that the program could not see. | `test_simulacros_secuencial.py::TestTimeouts` |
| 19 | If creating a batch timed out, was cut or got a 5xx, the retry created **another batch**; closing the program mid-send had the same effect on resume. | The whole batch billed twice (at half price each). | `test_simulacros_lote.py::TestEnvioQueQuedoSinRespuesta` |
| 20 | In streaming (always used by the simple mode) a `200` that was not a stream, or a stream ended before its last event, raised an SDK `AssertionError` taken for an unexpected failure; a truncated stream could be saved as a complete answer. | The whole queue stopped, or half an answer was kept as final. | `test_simulacros_secuencial.py::TestRespuestasRaras`, `test_simulacros_caos.py` |
| 21 | `retry-after: nan / inf / 1e999` crashed the engine thread; negative values gave zero waits. `408`/`409` were fatal. | Silent hang or a stopped queue. | `TestLimitesDeLaApi`, `test_errors.py` |
| 22 | A failed write of a paid answer was only logged; the run went on spending. | Paid answers lost. | `test_simulacros_disco.py` |
| 23 | Two copies could run on one data folder; each rewrote `state.json` whole. | The running spend total was overwritten: the cap stopped protecting. | `test_instancia_unica.py`, `test_simulacros_procesos.py::TestDosCopias` |
| 24 | JSONL lines split on U+2028/U+0085; `---` ignored with Windows line endings; `Infinity` in `config.json` discarded the whole file; a lone surrogate made saving fail; numeric-string token counts read as 0. | Corrupt input read as valid, settings reset, saves failing. | `test_propiedades.py` |
| 25 | CSV export wrote model text verbatim. | A cell starting with `=` runs as a formula in a spreadsheet. | `test_export.py::TestInyeccionDeFormulas` |
| 26 | `BatchEngine.cancelar_lote()` existed and nothing called it. | Pressing *Stop* on a batch left it running and billing on the server. | `test_simulacros_lote.py::TestCancelar`, `test_gui_avanzada.py::TestCancelarElLoteEnElServidor` |
| 27 | Collecting a batch rewrote the whole batch file and the spend total after **every** result; a failed or cut results download was not retried (a `5xx` ended the run as "complete" with orders pending). | 2,000 results: 38 s of disk writes; 4,000: 136 s; the documented 100,000 would take about a day. Now linear (8,000 results in 5 s), written together at checkpoints. | `test_simulacros_lote.py::TestRecogida`, `::TestRecogidaInterrumpida` |
| 28 | `--mensaje "<question>"` asked the file system whether the text was a file name. | Any literal question over ~255 characters crashed with `OSError: File name too long`. With `LANG=C` (no Python UTF-8 mode) accented arguments arrived as garbage. | `test_cli.py::TestTextoLiteralComoArgumento`, `test_simulacros_procesos.py::TestEntornosHostiles` |
| 29 | The simple window repainted the whole answer for every streamed fragment. | A long paragraph with no line breaks made each repaint slower than the last (7 ms per fragment at 6 KB, rising): the window stalled while the answer arrived. | `test_gui_simple.py::TestEnviar::test_los_trozos_que_llegan_juntos_se_pintan_de_una_vez` |
| 30 | Looking for a batch that may already exist used the clock (created in the last ten minutes) — nothing tied the match to *our* sending. | A same-size batch from someone else created just before could be adopted as ours (all orders "missing"), and a server clock more than ten minutes behind hid our own batch (a second one was created). Now: the newest batch in the account is noted before sending and only later ones are candidates; dates are only the fallback. | `test_simulacros_lote.py::TestEnvioQueQuedoSinRespuesta` (`…linea_base…`) |
| 31 | The calls that only *read* (a batch's status, the list of batches, the results download) followed the billing-safety policy: a cut connection stopped the run "to avoid duplicate billing", and three timeouts in a row stopped it too. | A 24-hour wait ended at the first network hiccup, with a message about a billing risk that did not exist. These calls are now retried with a pause. | `test_simulacros_lote.py::TestConsultasDeSoloLectura`, `test_batch.py::TestConsultasDeSoloLectura` |

## Gaps (features, not bugs)

No way to see answers in the app, export them, estimate cost, retry only the failures, load spreadsheets, use the Batches API, or run a simple "just ask" queue. These are the new simple mode, the rebuilt advanced mode (`gui/`), `estimator.py`, `inputs.py`, `export.py` and `core/batch.py`.

## Method and limits of this review

- Every v11 claim above was checked against the file at the cited lines.
- Official Anthropic documentation (rate limits, errors, Batches, caching, pricing) was used for the API behaviour; it was last checked in October 2026 and **will drift** — prices and model names especially.
- The v12 engines are tested against the real `anthropic` SDK (0.69 and 1.x) with a **simulated** server, including failure injection, random failure runs and real processes (see the second review). They have **not** been run against the live API at scale; the two behaviours most worth a real-world check are the monthly-cap `429` (no `retry-after`) and the `400` for a user-set limit, whose exact wording is inferred from the docs.
- Windows and macOS builds come from CI and haven't been hand-tested; the windows were exercised only under a virtual display (Xvfb).
