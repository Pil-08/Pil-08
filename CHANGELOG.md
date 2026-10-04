# Changelog

All notable changes. Format based on [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

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
