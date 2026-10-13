# Related projects, and what they suggest

Looked up in October 2026 while reviewing this program. Pages change and projects come and go: check a link before relying on it. Nothing here is a promise; it is the list of ideas considered, in the order they seem worth doing.

## What already exists

This program is a queue for the **Anthropic API** (pay per use, with a spend cap). Most similar tools solve a different problem: waiting out the **subscription** limit of Claude Code (the 5-hour window) and continuing a session.

| Project | What it does | Worth borrowing |
|---|---|---|
| [JCSnap/claude-code-queue](https://github.com/JCSnap/claude-code-queue) | Queue of Claude Code prompts (Markdown files with front matter) that waits for the subscription limit to reset and continues the same conversation. Priorities, retry limit, several accounts, queue survives restarts. | Priorities; limits tracked per account; says plainly that delivery is *at least once* (a task may run twice). |
| [PHSM22/claude-code-auto-resume](https://github.com/PHSM22/claude-code-auto-resume) | A hook queues interrupted sessions; a timer sends one tiny **probe request** every 5 minutes and resumes the queue when the probe succeeds. Drops entries older than 24 h; guards against two writers on one session. | Probe instead of trusting a parsed clock; a guard so two copies never write the same thing (done here: one copy at a time). |
| [cheapestinference/claude-auto-retry](https://github.com/cheapestinference/claude-auto-retry) | tmux wrapper: reads the reset time (time-zone and DST aware), waits for it plus a safety margin; exponential backoff with jitter and a cumulative cap for 429/5xx/529. | A cumulative cap on retries; patterns anchored so a quoted error message does not trigger a retry. |
| [oguztecimer/claude-auto-continue](https://github.com/oguztecimer/claude-auto-continue) | Pseudo-terminal wrapper that reads the exact reset timestamp and shows a countdown. | Live countdown (the simple mode already has one). |
| [anthropics/claude-code#35744](https://github.com/anthropics/claude-code/issues/35744) | Feature request for "continue automatically when the limit resets". A commenter shows the desktop app now has that checkbox; the CLI request stays open. | The subscription side is being solved by Anthropic itself. |
| [anthropics/claude-code#79500](https://github.com/anthropics/claude-code/issues/79500) | `claude -p` can report an API failure as `subtype: "success"` with exit code 0, while `is_error` is `true`. | If a `claude -p` backend is ever added, trust `is_error` and nothing else. |
| [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) and [ccusage](https://github.com/ccusage/ccusage) | Burn rate and time-to-exhaustion computed from Claude Code's local logs. | "At this pace you reach your cap in…". |
| [LiteLLM budgets](https://docs.litellm.ai/docs/proxy/users) | `max_budget` plus `budget_duration` (daily, weekly, monthly) per key, with a reset time zone. | A *periodic* cap on top of the cumulative one. |
| [apache/airflow#72400](https://github.com/apache/airflow/pull/72400) (closed, not merged), [gregochr/goldenhour#949](https://github.com/gregochr/goldenhour/pull/949), [EugeneButusov/kvorum#617](https://github.com/EugeneButusov/kvorum/issues/617) | Three unrelated projects hit the same trap: retrying the call that creates a batch creates **another, billable batch**, and a restart orphans a batch that is still running. Their fixes: store the batch id durably, and before any retry list the recent batches and adopt the one created since the first attempt (same number of requests, not already tracked; stop if more than one matches). | Done here, the same way (see below). They also note that another process using the same key is outside such guards. |

Forums and write-ups (Hacker News, DEV Community) ask for the same two things again and again: *wait for the reset and carry on unattended*, and *do not lose a night of work or pay twice*. Examples: [Ask HN: What are you moving on to now that Claude Code is so rate limited?](https://news.ycombinator.com/item?id=47626833), [Show HN: Claude-Nonstop](https://news.ycombinator.com/item?id=47082232), [Rate Limits Cost Me a Whole Night of Work](https://dev.to/bokuwalily/rate-limits-cost-me-a-whole-night-of-work-heres-the-46-line-script-that-fixed-it-7o3) and [I Built a Shell Wrapper That Makes Claude Code Auto-Resume After Rate Limits](https://dev.to/karthikeyan_natarajan_1cb/i-built-a-shell-wrapper-that-makes-claude-code-auto-resume-after-rate-limits-2lje). Automating the resume of a *subscription* session may conflict with Anthropic's terms of use: read them before building on it. The API, which this program uses, is built for programmatic use.

Anthropic's own documentation adds three facts that shaped this version: results of a batch are kept 29 days; cancelled and expired requests are not billed; and **batches may go slightly over a workspace spend limit**, so the only reliable cap is the one checked *before* sending (this program checks the worst case of the whole batch).

## Done because of this review

- A batch is never created twice: the attempt is written down before it is made, and before any retry (this run or the next) the account's recent batches are searched for the one that may already exist.
- A batch can be cancelled on the server (`--cancelar-lote`, or *Stop* in advanced mode).
- One copy at a time per data folder, as the auto-resume tools do for sessions.
- Requests that timed out count towards the spend cap as possible spend.

## Ideas, in order

1. **Periodic cap** ("$20 per month", reset by calendar) with 50 % / 80 % warnings, next to today's cumulative cap.
2. **Projection**: "at this pace the queue will cost $X and end around HH:MM".
3. **Run only between hours** (an overnight window), complementing the existing deadline.
4. **Receipts**: keep Anthropic's `request-id` for every answer in the export, to reconcile with the Console.
5. **Priorities and re-ordering** in the queue; saved prompt templates.
6. **Suggest the Batches API** (50 % cheaper) when a big queue is not urgent.
7. **A second backend for subscription users** that drives the official `claude -p` (never internal endpoints, `is_error` as the truth). Not planned until Anthropic's native auto-continue settles: it may make this unnecessary.
