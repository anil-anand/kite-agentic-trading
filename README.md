# Kite Agentic Trading App

An automated, quantitative algorithmic trading application built on top of the **Zerodha Kite Connect API**. 

The app features a modern Electron/React frontend communicating with a high-performance, multi-threaded Python backend. It acts as an autonomous agent that continuously scans the NIFTY 50 universe (plus custom watchlists) using 17 distinct technical analysis strategies, grouping them by **Confluence** to generate high-probability trade signals.

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
2. **Python** (v3.9 or higher)
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
   # Optional: Create a virtual environment first
   # python -m venv venv
   # source venv/bin/activate (Mac/Linux) or venv\Scripts\activate (Windows)
   
   pip install -r requirements.txt
   ```

The NIFTY 50 universe is refreshed from NSE's live index constituents endpoint once per day. If NSE is unavailable or blocks the request, the bundled list in `backend/nifty_universe.py` is used instead.

## 🚀 Running the App

To start the application in development mode (which automatically launches both the Vite frontend server and the Python background processes via Electron):

```bash
npm run dev
```

On your first launch, navigate to the **Settings** tab to enter your Kite API Key and API Secret. The app will encrypt and save them locally.

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
