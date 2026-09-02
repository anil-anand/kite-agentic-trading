# Kite Agentic Trading App

An automated, quantitative algorithmic trading application built on top of the **Zerodha Kite Connect API**. 

The app features a modern Electron/React frontend communicating with a high-performance, multi-threaded Python backend. It acts as an autonomous agent that continuously scans the NIFTY 100 universe (plus custom watchlists) using 17 distinct technical analysis strategies, grouping them by **Confluence** to generate high-probability trade signals.

## 🚀 Features

* **Advanced Confluence UI**: Groups signals by stock and trade direction. Stocks that trigger multiple strategies simultaneously are ranked at the top, allowing you to instantly spot the highest-probability setups.
* **17 Quantitative Strategies Built-in**: 
  * *Trend/Momentum*: Supertrend, ADX Momentum, Parabolic SAR, MACD Cross, TSI Cross, Awesome Oscillator.
  * *Mean Reversion*: RSI Reversal, CCI Reversal, Williams %R, Stochastic Reversal, StochRSI.
  * *Volatility/Breakout*: Bollinger Breakout, Keltner Breakout, Donchian Breakout.
  * *Volume*: VWAP Bounce, MFI Exhaustion.
  * *Moving Averages*: EMA Crossover.
* **Agentic Execution Modes**:
  * **Full Auto**: The agent strictly executes trades automatically based on risk configurations.
  * **Signal + Confirm**: The agent generates setups and targets, but waits for manual 1-click execution.
* **Parallel Scanning**: The Python engine uses intelligent ThreadPool execution to scan the entire market in parallel while strictly respecting Kite Connect's 3 req/sec rate limit.
* **Local Persistence**: All trade histories, activity logs, and pending signals are securely stored locally across sessions.

## 🛠️ Tech Stack

* **Frontend**: Electron, React 18, TypeScript, Tailwind CSS, Zustand (with local persistence), Vite.
* **Backend**: Python 3, `kiteconnect`, `pandas`, `ta` (Technical Analysis), custom JSON-RPC bridge.
* **Security**: API keys are locally encrypted using `cryptography.fernet` and stored in `~/.kite-agentic-trading/config.json`.

## 📦 Prerequisites

1. **Node.js** (v18 or higher recommended)
2. **uv** (latest version recommended; installs Python environments and packages)
3. An active **Zerodha Account**
4. A **Kite Connect API** subscription (API Key + Secret)

## ⚙️ Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/kite-agentic-trading.git
   cd kite-agentic-trading
   ```

2. **Install Frontend Dependencies**
   ```bash
   npm install
   ```

3. **Install Backend Dependencies**
   ```bash
   uv sync
   ```

`uv sync` creates the project environment in `.venv` and installs the locked backend dependencies. The development app and production Python build both invoke Python through `uv`.

## 🤖 AI Agent Setup Instructions

Copy and paste the following prompt into Claude, Codex, or another coding agent from the repository root:

```text
Set up this repository for local development. First verify that Node.js and uv are installed. Run `npm install` for the frontend, then run `uv sync` to provision the Python environment and install backend dependencies. After setup, run `npm run lint`, `npm run typecheck`, `npm run build`, and `uv run pytest`, and report any failures with their causes. Do not request, print, commit, or modify real Kite API credentials; credentials must be configured locally through the app's Settings screen.
```

The NIFTY 100 universe is refreshed from NSE's live index constituents endpoint once per day. If NSE is unavailable or blocks the request, the bundled list in `backend/nifty_universe.py` is used instead.

## 🚀 Running the App

To start the application in development mode (which automatically launches both the Vite frontend server and the Python background processes via Electron):

```bash
npm run dev
```

On your first launch, navigate to the **Settings** tab to enter your Kite API Key and API Secret. The app will encrypt and save them locally.

### Developing without a Kite login (`KITE_DEV_MODE`)

Kite access tokens expire daily, so live development normally means re-doing the Zerodha OAuth login each day. To develop the UI and engine with **no login, no credentials, and no network**, start the app with `KITE_DEV_MODE=1`:

```bash
KITE_DEV_MODE=1 npm run dev
```

In dev mode the backend swaps the real Kite client for a **mock** that serves synthetic-but-plausible market data (instruments, quotes, LTP, and deterministic historical candles), the login screen is skipped entirely, and the scanner/strategies produce real signals off the synthetic candles. The account book starts empty and orders are accepted but sent nowhere — for a simulated trading book, use **Paper** mode. Dev mode is off by default and has no effect in production.

## 🧰 Installing the Application

On macOS or Linux, run the installer from the repository root:

```bash
./scripts/install.sh
```

The script verifies the required tools, installs frontend and backend
dependencies, and runs the production packaging flow. The generated installer
artifacts are placed in `release/`. Use `--dry-run` to inspect the commands
without executing them:

```bash
./scripts/install.sh --dry-run
```

The installer never asks for or modifies Kite API credentials. Configure
credentials through the app's login screen after installation.

## 🧪 Running Tests

The backend test suite (`pytest`) covers the trading strategies. The strategy
signal calculations are pure functions, so the tests run fully offline — no Kite
API credentials or network access required.

```bash
uv run pytest
```

Useful variants:

```bash
uv run pytest -v                                          # list every test
uv run pytest backend/tests/strategies/test_triggers.py   # a single file
uv run pytest -k williams                                 # tests matching a keyword
```

Tests live in `backend/tests/`. `pytest` is included in the dev dependencies, so
`uv sync` provisions it automatically.

## 🎨 Code Style (Python)

The Python backend follows **PEP 8**, enforced with [Ruff](https://docs.astral.sh/ruff/)
(configured in `pyproject.toml`). Ruff is included in the dev dependencies, so
`uv sync` installs it.

```bash
uvx ruff check backend/ run_backend.py     # lint
uvx ruff format backend/ run_backend.py    # auto-format
```

The frontend continues to use ESLint (`npm run lint`).

## 🏗️ Building for Production

To create a standalone executable (e.g., a `.dmg` for macOS or `.exe` for Windows), there is a two-step process:

1. **Compile the source code** (TypeScript to JavaScript):
   ```bash
   npm run build
   ```

2. **Package the application** (Creates the `.dmg` / installer):
   ```bash
   npm run dist
   ```

The final compiled installer files will be available in the `release/` directory.

## ⚠️ Disclaimer

**This software is for educational and research purposes only.** Algorithmic trading involves significant risk of loss. The creators of this software are not registered financial advisors. Always test your strategies in a paper-trading environment before deploying real capital. You are solely responsible for any trades executed by this agent.
