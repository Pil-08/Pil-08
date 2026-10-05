# Changelog

All notable changes. Format based on [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

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
