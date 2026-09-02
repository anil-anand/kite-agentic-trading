import json
from pathlib import Path

from cryptography.fernet import Fernet


class ConfigManager:
    def __init__(self):
        self.config_dir = Path.home() / ".kite-agentic-trading"
        self.config_file = self.config_dir / "config.json"
        self.key_file = self.config_dir / ".key"
        self.config = {}

        self.default_config = {
            "risk": {
                "maxCapitalPerTrade": 10000,
                "maxDailyLoss": 2000,
                "maxSimultaneousPositions": 5,
                "noNewTradesAfter": "15:00",
                "autoSquareOff": True,
                "squareOffTime": "15:15",
                "defaultStopLossPercent": 1.5,
                "defaultTargetPercent": 3,
                "positionRevalWeakExitMins": 15,
                "positionRevalBreakevenMins": 45,
            },
            "strategies": {
                "ema_crossover": {"enabled": True},
                "rsi_reversal": {"enabled": True},
                "vwap_bounce": {"enabled": True},
                "supertrend": {"enabled": True},
                "macd_cross": {"enabled": True},
                "bollinger_breakout": {"enabled": True},
                "stochastic_reversal": {"enabled": True},
                "adx_momentum": {"enabled": True},
                "psar_trend": {"enabled": True},
                "donchian_breakout": {"enabled": True},
                "cci_reversal": {"enabled": True},
                "williams_r": {"enabled": True},
                "mfi_exhaustion": {"enabled": True},
                "keltner_breakout": {"enabled": True},
                "awesome_oscillator": {"enabled": True},
                "tsi_cross": {"enabled": True},
                "stoc_rsi": {"enabled": True},
            },
            "watchlist": [
                "RELIANCE",
                "TCS",
                "HDFCBANK",
                "INFY",
                "ICICIBANK",
                "HINDUNILVR",
                "ITC",
                "SBIN",
                "BHARTIARTL",
                "KOTAKBANK",
                "LT",
                "AXISBANK",
                "ASIANPAINT",
                "MARUTI",
                "TITAN",
                "SUNPHARMA",
                "BAJFINANCE",
                "WIPRO",
                "ULTRACEMCO",
                "NESTLEIND",
            ],
            "credentials": {"apiKey": "", "apiSecret": ""},
            "mode": "auto",
        }

        self._init_dir()
        self._init_key()
        self.load()

    def _init_dir(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def _init_key(self):
        if not self.key_file.exists():
            key = Fernet.generate_key()
            with open(self.key_file, "wb") as f:
                f.write(key)

        with open(self.key_file, "rb") as f:
            self.cipher_suite = Fernet(f.read())

    def _encrypt(self, text: str) -> str:
        if not text:
            return ""
        return self.cipher_suite.encrypt(text.encode()).decode()

    def _decrypt(self, text: str) -> str:
        if not text:
            return ""
        try:
            return self.cipher_suite.decrypt(text.encode()).decode()
        except Exception:
            return ""

    def load(self):
        if self.config_file.exists():
            try:
                with open(self.config_file, "r") as f:
                    loaded = json.load(f)

                # Merge with defaults to ensure new keys/strategies are present
                self.config = self.default_config.copy()
                for k, v in loaded.items():
                    if isinstance(v, dict) and k in self.config:
                        self.config[k].update(v)
                    else:
                        self.config[k] = v
            except json.JSONDecodeError:
                self.config = self.default_config.copy()
        else:
            self.config = self.default_config.copy()
            self.save()

    def save(self):
        with open(self.config_file, "w") as f:
            json.dump(self.config, f, indent=4)

    def get_credentials(self):
        creds = self.config.get("credentials", {})
        return {
            "apiKey": self._decrypt(creds.get("apiKey", "")),
            "apiSecret": self._decrypt(creds.get("apiSecret", "")),
            "accessToken": self._decrypt(creds.get("accessToken", "")),
        }

    def save_credentials(self, api_key: str, api_secret: str, access_token: str = ""):
        if "credentials" not in self.config:
            self.config["credentials"] = {}

        self.config["credentials"]["apiKey"] = self._encrypt(api_key)
        self.config["credentials"]["apiSecret"] = self._encrypt(api_secret)
        if access_token:
            self.config["credentials"]["accessToken"] = self._encrypt(access_token)
        self.save()

    def get_risk_config(self):
        return self.config.get("risk", self.default_config["risk"])

    def get_strategy_config(self):
        return self.config.get("strategies", self.default_config["strategies"])

    def get_watchlist(self):
        return self.config.get("watchlist", self.default_config["watchlist"])

    def get_app_order_ids(self) -> set:
        path = self.config_dir / "app_orders.json"
        if path.exists():
            try:
                with open(path, "r") as f:
                    return set(json.load(f))
            except Exception:
                return set()
        return set()

    def add_app_order_id(self, order_id: str):
        if not order_id:
            return
        orders = self.get_app_order_ids()
        orders.add(str(order_id))
        path = self.config_dir / "app_orders.json"
        with open(path, "w") as f:
            json.dump(list(orders), f)

    def get_historical_orders(self) -> dict:
        path = self.config_dir / "historical_orders.json"
        if path.exists():
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_historical_orders(self, orders: dict):
        path = self.config_dir / "historical_orders.json"
        with open(path, "w") as f:
            json.dump(orders, f, indent=4, default=str)


config_manager = ConfigManager()
