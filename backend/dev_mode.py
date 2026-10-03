"""Development-mode flag.

Set KITE_DEV_MODE=1 (or true/yes/on) to run the backend against a mock Kite
client with synthetic market data — no real Zerodha login, credentials, or
network access required. Intended purely for local UI/engine development; it is
off by default. Explicit DEV sessions use separate persistence even in packaged builds.
"""

import os
from pathlib import Path


def is_dev_mode() -> bool:
    return os.environ.get("KITE_DEV_MODE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def runtime_data_dir() -> Path:
    """Keep existing LIVE recovery files in place; DEV never reads or migrates them."""
    root = Path.home() / ".kite-agentic-trading"
    return root / "dev" if is_dev_mode() else root
