# Claude Batch Monitor

**A desktop queue for Claude.** Type your questions; the app sends them one by one, **waits by itself** when the API says "slow down" (rate limit, overloaded servers, no internet) and carries on exactly where it stopped — behind a **hard spend cap**, so a long run can never surprise you with the bill.

<p align="center">
  <img src="docs/screenshot-waiting.png" width="760" alt="The simple mode while the API limit is reached: question 2 is waiting and resumes by itself in 8 seconds">
</p>

> Personal project by [Samuel Pil](https://github.com/Pil-08) (not client work). Screenshots use a simulated API; no real requests were made to take them.

## What it does

- **Simple mode** for anyone: paste your API key once, type a question, press Enter. Answers stream in live; questions pile up in a queue that survives closing the program.
- **Waits instead of failing.** Per-minute limit → waits for the exact time the API asks. Server overloaded → backs off and retries. No internet → keeps trying for up to 10 minutes. Closing the app mid-queue → the pending questions are still there next time.
- **Stops instead of overspending.** Before every request it estimates the *worst case* and refuses to send it if it would cross your cap. A response that was already paid for is **always saved first**, even if it is the one that hits the cap.
- **Says why it stopped** when waiting cannot help: out of credit, the account's monthly spend limit (with the date access returns), a rejected key, a model that no longer exists — each with the button that fixes it.
- **Advanced mode** for bigger jobs: load a `.csv` / `.tsv` / `.jsonl` (one order per row, pick the column), wrap every row in a template, use the **Batches API at half price**, run requests in parallel, estimate the cost first, see/copy/export every answer, resume old batches, loop with a deadline, Telegram and desktop notifications, system-tray background mode.
- **Command line** (`--cli`) for servers and cron, with meaningful exit codes and `--json` output.

<p align="center">
  <img src="docs/screenshot-advanced.png" width="560" alt="Advanced mode: a finished batch with per-order tokens and cost">
  &nbsp;
  <img src="docs/screenshot-spend-cap.png" width="560" alt="The simple mode after reaching the spend cap, with a button to raise it">
</p>

## Get it

**Download** (no Python needed): the [Releases](../../releases) page has a Windows `.exe`, a macOS `.zip` and a Linux binary.
They are not code-signed, so Windows SmartScreen / macOS Gatekeeper will warn you the first time (*More info → Run anyway* / right-click → *Open*).

**Or with Python 3.9+** (needs Tk: on Debian/Ubuntu `sudo apt install python3-tk`):

```bash
git clone https://github.com/Pil-08/claude-batch-monitor
cd claude-batch-monitor
pip install -e ".[all]"        # [all] adds keyring, system tray and desktop notifications (all optional)
python -m claude_batch_monitor # opens the window (simple mode the first time)
```

You need an **Anthropic API key** (pay-per-use, from [platform.claude.com](https://platform.claude.com)). The first-run screen walks you through it; a few dollars are enough for hundreds of questions.

> **API vs. subscription.** This program uses the *Anthropic API*. It cannot see or use the limits of a claude.ai Pro/Max plan or of Claude Code's subscription — those are a different system with their own quotas. The "limit" it handles is the API's own (requests/tokens per minute, monthly spend tier, your credit balance).

## Using it

| | |
|---|---|
| **Simple mode** | `python -m claude_batch_monitor` — type, press **Enter**, done. *Shift+Enter* adds a line. *Options* has the model, spend cap and language (English/Spanish). |
| **Advanced mode** | `python -m claude_batch_monitor --avanzado` (or *Options → Advanced mode…*). Switch back with *Modo simple*. |
| **Command line** | `python -m claude_batch_monitor --cli --help` |

```bash
# What would this cost? Nothing is sent.
claude-batch-monitor --cli --mensaje datos.csv --campo texto --plantilla "Summarise: {input}" --dry-run

# Try the first 2 rows, then run everything at half price with a $3 cap and export the results
claude-batch-monitor --cli --mensaje datos.csv --campo texto --plantilla "Summarise: {input}" --limit 2
claude-batch-monitor --cli --mensaje datos.csv --campo texto --plantilla "Summarise: {input}" \
    --modo lote --tope-gasto 3 --exportar resultados.csv

# Pick up an interrupted run
claude-batch-monitor --cli --listar
claude-batch-monitor --cli --reanudar 1a2b3c4d
```

Exit codes: `0` done · `1` failure/network · `2` spend cap · `3` no credit / account limit · `4` incomplete (stopped or deadline) · `5` bad usage.
*The command-line flags and the advanced window are in Spanish for now; the simple mode is bilingual.*

## Safety

- **Spend cap on by default** ($2 in simple mode, $5 elsewhere; `0` disables it). Prices come from a built-in catalogue, the unknown-model fallback is the *expensive* price, and you can override them.
- **Your key never touches a file.** It lives in the OS keyring (or an environment variable, or memory for the session) — never in `config.json`, the repo, logs or exports. It is sent only to Anthropic.
- **Everything is on your disk:** `%APPDATA%\ClaudeMonitor` (Windows), `~/Library/Application Support/ClaudeMonitor` (macOS) or `~/.config/ClaudeMonitor` (Linux); override with `CLAUDE_MONITOR_HOME`. Finished batches are removed after 30 days; batches with pending work never are.
- **No duplicate charges:** if the connection drops *mid-response* it stops instead of resending, because the server may already have billed that request.

## Status

Beta. The engine is covered by 700+ automated tests, including runs against the real `anthropic` SDK (0.69 and 1.x) with a simulated server, on Python 3.9 and 3.12. It has **not** yet been exercised against the live API in long production runs, the Windows/macOS executables are built by CI but not hand-tested, and the windows were only exercised under a virtual display. Start with `--limit 2` (or *Solo las primeras 2* in advanced mode) before pointing it at a big file. See [REVIEW.md](REVIEW.md) for what changed from the original single-file script and why, and [CHANGELOG.md](CHANGELOG.md).

## Develop

```bash
pip install -e ".[all]" pytest ruff
ruff check .
xvfb-run -a python -m pytest      # window tests need a display; on a desktop just: python -m pytest
```

Build the executable for your OS (`pip install pyinstaller`, then `pyinstaller packaging/claude-batch-monitor.spec`); tagging `v*` builds all three on GitHub Actions and opens a **draft** release.

```
claude_batch_monitor/
  core/        engines: immediate (one request per order) and batch (Messages Batches API), error policy
  gui/         simple mode, advanced mode, dialogs, tray, i18n
  cola.py      the simple-mode queue (thread + events, no windows)   ejecutor.py   same, for one batch
  cli.py  config.py  job.py  pricing.py  estimator.py  inputs.py  export.py  notify.py  claves.py …
tests/         pytest suite (window tests are skipped without a display)
packaging/     PyInstaller spec, launcher and icons
```

## License

[MIT](LICENSE) © Samuel Pil. Not affiliated with Anthropic. "Claude" is a trademark of Anthropic.
