# AGENTS.md

Guidance for AI coding agents (Claude Code, Gemini CLI, Cursor, Codex, etc.)
working in this repository. Human contributors should read `README.md`.

## Project

Agentic intraday trading desktop app for Zerodha Kite. An Electron + React
(TypeScript) frontend talks over a JSON-RPC bridge (stdin/stdout) to a Python
backend that scans the market with 17 technical-analysis strategies.

- `backend/` — Python backend (strategies, scanner, trading engine, risk manager).
- `backend/tests/` — pytest suite for the strategies (pure, offline).
- `src/` — Electron main process and React renderer.

## Environment

- Python is managed with **uv**. Run `uv sync` to provision `.venv`.
- Frontend deps: `npm install`.

## Required checks — run before committing

Any change to Python code MUST pass all of the following. CI
(`.github/workflows/ci.yml`) enforces the same checks and will block merge:

```bash
uv run ruff check backend/ run_backend.py     # lint (PEP 8)
uv run ruff format backend/ run_backend.py    # format (use --check in CI)
uv run pytest                                 # tests
```

Frontend changes:

```bash
npm run lint
npm run typecheck
npm run build
```

## Code style

- Python follows **PEP 8**, enforced by **Ruff** (configured in `pyproject.toml`).
  Ruff is the single source of truth — do not hand-format against a different
  style. Fix lint findings rather than suppressing them; only add `# noqa` with a
  specific rule code and a reason.
- Match the conventions of the surrounding code.

## Tests

- Strategy signal functions are pure (`DataFrame -> signals`); test them with
  synthetic candle data via the fixtures in `backend/tests/conftest.py`. Do not
  hit the Kite API or the network in tests.
- Add or update tests for any change to strategy or trading logic.

## Safety — non-negotiable

- **Never** request, print, hardcode, commit, or modify real Kite API
  credentials. Credentials are configured by the user through the app's Settings
  screen and stored encrypted in `~/.kite-agentic-trading/`.
- This app can place real trades. Be conservative with any change to the trading
  engine, order placement, or risk manager, and call out risk in your summary.
