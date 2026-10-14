# Changelog

All notable changes. Format based on [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## [12.4.0] — 2026-10-13

**Works with your Claude plan.** You no longer need an API key or API credit: the program can answer every question with the Claude **Pro / Max subscription** you already pay for, through the official Claude Code program, and wait for the plan's limit to renew on its own.

### Added
- **"With my Claude plan" mode** (third mode next to *immediate* and *batch*). Each question is answered by `claude -p` (Claude Code) with your own login, one at a time, streaming the answer in live. First-run screen with two tabs (*With my Claude plan* / *With an API key*) that checks Claude Code for you (installed? signed in? signed in with a plan and not an API key?); *Options* has the same choice, the model (`sonnet`, `opus`, `haiku` or a full id; empty = your plan's default) and a *Check again* button. Advanced mode has a third radio and a "With your Claude plan" section in *Settings*. Command line: `--modo plan` (`--modelo sonnet`).
- **Waits for the plan's limit to renew, then carries on.** The reset time comes from what Claude Code itself reports (`rate_limit_event`: status, `resetsAt`, the 5-hour and weekly windows with their usage) or, if only a message came (`You've hit your session limit · resets 3:45pm (Europe/Madrid)`), from that text. It waits until that moment plus 90 s, counting wall-clock time in one-minute slices, so closing the lid or suspending the computer does not make it oversleep. The block is also written to disk: closing the program and opening it later does not send a question into a closed door. If no reset time is given it waits in growing steps (5 → 30 min) and stops with an explanation after a while instead of waiting forever.
- **It keeps to your plan, not to your wallet.** `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and the Bedrock/Vertex/Foundry switches are removed from the environment of the process (Claude Code would otherwise silently bill the key); the program reads Claude Code's own report of where its credentials come from (it comes first thing in the output) and stops at once if it says "API key"; and if your plan has *extra usage* switched on, the program **looks before it asks** (when the last answer says a plan window is at 100 % and extra usage is on, it waits for the renewal instead of sending the next question on paid credits) and, as a safety net, the queue **stops as soon as Claude Code reports it started spending those paid credits** (the answer in hand is saved first) and says so.
- **Nothing of yours on the command line.** The question goes through standard input and the instructions through a file; the working folder is an empty folder of the program's own, tools and slash commands are off (`--tools ""`, `--disable-slash-commands`, `--safe-mode`) and no session is kept. Options an older Claude Code does not know are dropped one by one instead of failing. A hung `claude` is ended after two minutes of silence — counted from the last line it printed, so an answer that keeps arriving is never cut — and in any case after an hour; it is ended together with everything it started.
- **Plan usage** shown in the status bar ("Plan: session 13 % (renews at 17:23) · week 2 % (renews on 19/10 at 13:23)") and in the terminal. Exit code `2` also covers "the plan ran out and Claude Code started using extra credits".
- **Time window** (`--franja 22:00-07:00`, and *Work only in a time window* in Options / *Time window* in Settings): outside those hours nothing is sent and the program says when it will carry on; it can cross midnight. It applies to the plan mode and to the API modes (for a batch, only the sending waits: collecting results is not sending).
- Waits of hours or days are shown as `5:12:00` / `1 d 2:00:00` with the clock time they end (`Resuming by itself at 03:10`).
- The *Options* window scrolls on small screens; its buttons stay in view.
- **Finds Claude Code in more places**: besides the PATH, the usual folders of the official installer, Homebrew, npm, nvm, fnm, pnpm, Volta, Bun, asdf, mise, snap and Scoop (a program opened by double click does not inherit the terminal's PATH), and a *Choose where it is…* button on the first screen and in the notices for any other place.
- Tests: a fake `claude` (`tests/claude_falso.py`) that prints the same lines as the real Claude Code 2.1.296 (copied from a real run) and can be told to hit a limit, a weekly limit, extra usage, an expired session, a bad model, a hang, garbage, a crash…; simulations of the plan engine, of the queue and of both windows with it, real processes (Ctrl+C, `kill -9`, two copies, a non-UTF-8 terminal) and 30 seeded random failure runs whose final state and number of asks per question are checked exactly; the reader of Claude Code's output is also fed random garbage (property tests). The suite went from 1,482 to 2,045 tests.

### Changed
- Choosing the plan on the first-run screen does not ask for the `anthropic` library (only the API modes need it).
- Changing how you use Claude (plan or key) or the model in *Options* updates the mode and model the queue records, so exporting it or resuming it from the terminal uses what it is doing now.
- After an order failed because of the model or the key, saving a new model or key in *Options* retries that order (before, only orders still waiting were retried).
- Claude Code's status is remembered for 90 s only when it was fine; a problem is checked again every time, so fixing it and pressing *Resume* works at once.

### Fixed
- **The program could abort while closing** (signal 6, `FATAL: exception not rethrown`, the exit code lost) when it stopped within a few milliseconds of a notification: the notification's background thread asks the system keyring (native code) and Python cannot shut down in the middle of that. About 2.5 % of such closes on a busy machine, found by a real-process test. Pending notifications now get up to 4 seconds to finish when the program closes.

## [12.3.0] — 2026-10-13

**Stress-tested.** The program was run, as a real process and in-process, against a simulated Anthropic server (the real `anthropic` SDK over real sockets) with every failure the API and the network can produce, plus seeded random failure runs and property-based tests. That found the problems below; each one now has a test that fails without its fix.

### Fixed
- **A request that timed out after being sent could be billed and then re-sent** up to `max_error_attempts` times, with the spend cap blind to the earlier attempts. The worst case of an abandoned request now counts against the cap as possible spend, the retry runs as a stream (no total time to exceed) and an order that times out three times stops the run.
- **A batch could be created twice.** If the request that creates a batch timed out, was cut or got a 5xx — or the program was closed in the middle of sending — the retry created a second batch (billed twice). The attempt is now written to disk *before* it is made; before any retry, in this run or the next, the account's batches created *after the newest one that existed just before sending* are searched for the one that may already exist (same size, not followed by another job) and that one is followed instead. With several candidates it stops and says so. (If the account could not be read at that moment, the search falls back to the last few minutes, tolerating ten minutes of clock difference.)
- **Streaming** (the simple mode always streams): a `200` that is not a stream (a proxy or captive-portal page), an empty stream, or a stream that ended cleanly before the end of the message either stopped the whole queue with an unexplained error or could have been recorded as a complete answer. Not a stream → retried; cut → stops without re-sending (the server may have billed it).
- A `200` with a non-JSON body (captive portal) stopped the whole run; it is now retried with backoff.
- `retry-after` values that are not numbers (`nan`, `inf`, `1e999`) crashed the engine thread; negative ones produced zero or negative waits.
- HTTP `408` and `409` were treated as fatal; like the SDK itself, they are now retried.
- **A paid response could be lost when the disk was full or read-only** (only the log noticed, and the run kept spending). The run now stops at once with a clear message; the answer stays on screen (copy / export) and is written as soon as the disk allows. Batches stop collecting and keep the rest on the server for the next resume.
- Writing a file could fail for a moment on Windows when an antivirus or indexer held the destination: it is retried briefly. Files are written byte-exact (no `\n` → `\r\n` conversion inside saved answers).
- **Collecting a big batch took quadratic time.** The batch file was rewritten in full after every single result: 2,000 results took 38 s and 4,000 took 136 s of pure disk writing, so the documented maximum of 100,000 orders would have needed about a day. The batch state and the spend total are now written together every 200 results (or every 5 % of the batch, or every 20 s) and always on the way out, Ctrl+C included; 8,000 results take 5 s. An interruption between two checkpoints makes the next resume collect those results again — never count them twice.
- A `5xx` or a timeout while *downloading* a batch's results ended the run as if it were complete, with orders still pending; a cut stopped it. Downloading costs nothing, so it is now retried with a pause, and if it cannot be fixed the run stops with the batch kept for the next resume.
- **Waiting for a batch stopped at the first hiccup.** A dropped connection while checking a batch's status ("paused to avoid duplicate billing") or three timeouts in a row ended the wait — in a wait that can last 24 hours. Checking the status, listing batches and downloading results cost nothing and can be repeated, so they are now retried with a pause (a download counts only the attempts that made no progress); the stop-instead-of-resend rule stays for what can bill twice: sending a request or a batch.
- **`--mensaje "<a long question>"` crashed** with `OSError: File name too long` whenever the literal text was over ~255 characters (the program checked whether the text was a file name). With a non-UTF-8 terminal setup (`LANG=C` without Python's UTF-8 mode) accented characters in arguments arrived as garbage; they are now recovered.
- *Stop* on a batch asked to cancel it while the engine was closing its own connection, so the cancellation could fail at random: it now stops the engine first and cancels with a connection of its own if the engine's is gone.
- **The window stalled on long answers**: every streamed fragment repainted the whole answer, and a long paragraph without line breaks made each repaint slower than the last. Fragments arriving within 60 ms are now painted together.
- A JSONL line with U+2028, U+0085 or similar inside a string was cut in half (lines are now split on `\n` only); Windows line endings stopped `---` from separating orders; `Infinity` in `config.json` made the whole file be set aside as corrupt; a lone UTF-16 surrogate (half an emoji from an odd paste) made saving fail; usage counters given as numeric strings counted as zero.

### Added
- **One copy at a time** per data folder (an OS lock that is released by the system even if the program is killed). A second window shows a message; a second terminal run exits with code `5`.
- **Cancel a batch on the server**: *Stop* in advanced mode asks whether to cancel it there too (a stopped batch keeps running and billing); `--cancelar-lote JOB_ID` on the command line.
- CSV export prefixes text cells that start with `=`, `+`, `-` or `@` with an apostrophe, so spreadsheets do not run them as formulas (use the JSONL export if a program will read the answers: it is unmodified).
- `docs/ROADMAP.md`: similar projects and the ideas they suggest.
- Tests: a simulated server (`tests/servidor_falso.py`) and simulations of the immediate engine, batches, the queue, the disk, real processes (`kill -9`, Ctrl+C, two copies at once, the single file and the package) and the window; 100 seeded random runs; property-based tests (`hypothesis`). The suite went from 1,022 to 1,482 tests; all pass on Python 3.9, 3.10, 3.11, 3.12 and 3.13 (SDK 0.69 and 1.x), both against the modules and against the single file.

### Changed
- Exit code `5` also means "another copy of the program is already running".
- `Job.save()`, `EstadoMonitor.guardar()` and `BatchEngine._aplicar()` now say whether they succeeded.

## [12.2.0] — 2026-10-05

**Easy to open, easy to read.** The whole program is now one plain file you can download, read and run.

### Added
- **`claude_batch_monitor.py` — the entire program in a single file.** Plain Python: no packing, no obfuscation, no `exec`/`eval`. It opens a table of contents at the top and numbered sections below. It is *generated* from the modules in `claude_batch_monitor/` by `tools/build_single_file.py`; CI fails if it is out of date, runs the complete test suite against it, and tests that it contains no hidden-code tricks (`exec`, `eval`, decoding or decompressing code) and mentions only the web addresses it declares.
- **It starts working on its own.** The only thing that does not ship with Python is the `anthropic` library: if it is missing, a window (or a question in the terminal) offers to install it — exactly `pip install anthropic`, nothing else — and reopens the program. Systems that refuse (Debian/Ubuntu "externally managed environment") get a private folder inside the program's data folder instead.
- **Answers with formatting**: headings, bold, italic, `code`, code blocks, lists, quotes, tables and links are shown as such, with a *Con formato* switch to see the original text. What is saved, copied and exported is never altered.
- **Dark theme** (*Options → Theme*): Automatic (follows the system), Light or Dark. Every text/background pair is tested for WCAG AA contrast.
- **Instructions for every answer** in the simple mode's options (e.g. "answer in Spanish, briefly"); an empty-queue hint; a friendly error window when something unexpected happens — including at start-up, where double-clicking the file would otherwise close silently.
- Commands shown to the user match their system (`py` on Windows, `python3` elsewhere; the single file, the package or the downloaded app, as appropriate).
- The icon is drawn by code (no binary file to ship or doubt).

### Changed
- Clearer names everywhere (no jokes, no slang, no names that need explaining); the old `modo_fantasma` option is now *start minimized* (`iniciar_minimizado`) and the old key is no longer read.
- Colours come from a palette (`gui/estilo.py`), so another theme is one more palette.

## [12.1.0] — 2026-10-04

First public version of the rewrite. The original single-file script (v11.0.0) is preserved in the first commit; see [REVIEW.md](REVIEW.md) for the audit that motivated the rewrite.

### Added
- **Simple mode**: a queue you can type into. First-run screen that tests and stores the API key, live streaming answers, a visible queue that survives closing the program, countdowns while it waits, banners with the button that fixes each stop (raise the cap, add credit, change key/model, resume), per-question menu (copy, ask again, remove), export, tray/background mode, English and Spanish.
- **Advanced mode** (rebuilt, with everything v11 had): tabs for panel and settings; load `.txt`/`.md`/`.csv`/`.tsv`/`.jsonl` with a column picker; per-row template; *first N rows* smoke test; immediate or **Batches API (−50 %)** mode; cost estimate; results table with a full-answer viewer; export to CSV/JSONL/Markdown/text; past batches; resume with the batch's own model and mode; loop mode with deadline; Telegram (with test button) and desktop notifications; validated settings.
- **Command line** (`--cli`): `--dry-run`, `--limit`, `--campo`, `--plantilla`, `--modo lote`, `--esfuerzo`, `--concurrencia`, `--exportar`, `--json`, `--listar`, `--reanudar`, meaningful exit codes (0–5).
- Engines: immediate (sequential or parallel) and Batches; effort levels and prompt caching; refusal and truncation handling; proactive throttling from rate-limit headers; full-jitter backoff.
- **Account-limit handling**: transient `429` vs. monthly spend cap of the usage tier (stops and reports the date access returns) vs. a limit set by the user in the console (`400`) vs. no credit (`402`/`400`).
- Packaging: PyInstaller spec, icons, CI workflow (lint + tests on Python 3.9 and 3.12) and a release workflow that builds Windows/macOS/Linux and opens a **draft** release.
- 700+ automated tests (window tests run under a virtual display and skip cleanly without one).

### Changed (vs. v11)
- Default model is **Claude Sonnet 5.5** (Anthropic's docs list `claude-haiku-4-5` for retirement on 2026-10-15); default `max_tokens` 16 000 (8 000 in simple mode).
- A deadline exists only if you set one (loop mode defaults to 23:59). v11 always applied 23:59.
- Prices come from a model catalogue (with cache and batch multipliers); unknown models are priced at the *expensive* end. Manual prices still override.
- Money is `Decimal`, not `float`. Settings with an invalid number are refused instead of silently replaced.
- Auto-start no longer asks for confirmation.
- The old data directory (`ClaudeMonitor`) and `config.json` / job files from v11 are read as they are.

### Fixed (all described in REVIEW.md)
- A response already paid for was discarded when it hit the spend cap, and re-requested (and re-billed) on resume.
- The spend cap was only checked after spending; now the worst case is reserved before every request.
- The monthly-cap `429` was retried forever; `413` was retried for hours; a network blip or a slow response aborted the whole run.
- Wait time used the bucket-refill header instead of `retry-after`.
- Typing the key in the window failed without `keyring`.
- The worker thread touched Tk directly; the sleep lock was taken and released on different threads; refusals were saved as normal answers.
- Old batches were never cleaned up. Retention is now configurable (30 days by default) and never deletes a batch — or the simple-mode queue — that still has unsent questions.

### Security
- The API key and the Telegram token are never written to `config.json`, logs or exports; transport errors are logged by type only (their text can contain the bot URL).
- `anthropic` 1.x depends on `httpx2` (0.x on `httpx`): connection errors are detected by class name so both work.
