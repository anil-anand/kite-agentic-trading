"""Development-mode flag.

Set KITE_DEV_MODE=1 (or true/yes/on) to run the backend against a mock Kite
client with synthetic market data — no real Zerodha login, credentials, or
network access required. Intended purely for local UI/engine development; it is
off by default and has no effect in production.
"""

import os


def is_dev_mode() -> bool:
    return os.environ.get("KITE_DEV_MODE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
