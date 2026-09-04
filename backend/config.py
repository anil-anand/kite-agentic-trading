import json
from copy import deepcopy
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
                "startTradeAfter": "09:45",
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
            "llm": {
                "provider": "Gemini",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
                "model": "gemini-2.5-flash",
                "openCodePlan": "zen",
                "apiKey": "",
                "temperature": 0.2,
                "maxTokens": 1024,
            },
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
                self.config = deepcopy(self.default_config)
                for k, v in loaded.items():
                    if isinstance(v, dict) and k in self.config:
                        self.config[k].update(v)
                    else:
                        self.config[k] = v
            except json.JSONDecodeError:
                self.config = deepcopy(self.default_config)
        else:
            self.config = deepcopy(self.default_config)
            self.save()

        self._migrate_legacy_llm_key()

    def _migrate_legacy_llm_key(self):
        legacy_key = self.config.get("credentials", {}).get("llmApiKey", "")
        llm = self.config.setdefault("llm", deepcopy(self.default_config["llm"]))
        if llm.get("openCodePlan") not in {"zen", "go"}:
            llm["openCodePlan"] = "zen"
        if llm.get("baseUrl", "").endswith("/openai"):
            llm["baseUrl"] = llm["baseUrl"][:-6]
            self.save()
        if legacy_key and not llm.get("apiKey"):
            decrypted_key = self._decrypt(legacy_key)
            if decrypted_key:
                llm["apiKey"] = self._encrypt(decrypted_key)
                self.save()

    def save(self):
        with open(self.config_file, "w") as f:
            json.dump(self.config, f, indent=4)

    def get_credentials(self):
        creds = self.config.get("credentials", {})
        llm = self.config.get("llm", {})
        encrypted_llm_key = llm.get("apiKey") or creds.get("llmApiKey", "")
        return {
            "apiKey": self._decrypt(creds.get("apiKey", "")),
            "apiSecret": self._decrypt(creds.get("apiSecret", "")),
            "accessToken": self._decrypt(creds.get("accessToken", "")),
            "llmApiKey": self._decrypt(encrypted_llm_key),
        }

    def get_llm_settings(self):
        return deepcopy(self.config.get("llm", self.default_config["llm"]))

    def get_settings(self):
        settings = deepcopy(self.config)
        settings.setdefault("llm", deepcopy(self.default_config["llm"]))
        settings["llm"]["apiKey"] = ""
        settings["llm"]["apiKeyConfigured"] = bool(
            self.config["llm"].get("apiKey")
            or self.config.get("credentials", {}).get("llmApiKey")
        )
        settings["credentials"] = {key: "" for key in settings.get("credentials", {})}
        return settings

    def save_settings(self, settings: dict):
        incoming = deepcopy(settings)
        incoming_llm = incoming.pop("llm", None)
        incoming_credentials = incoming.pop("credentials", None)
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(self.config.get(key), dict):
                self.config[key].update(value)
            else:
                self.config[key] = value

        if incoming_credentials is not None:
            credentials = self.config.setdefault("credentials", {})
            for key in ("apiKey", "apiSecret", "accessToken"):
                value = incoming_credentials.get(key, "")
                if value and value != "********":
                    credentials[key] = self._encrypt(value)

        if incoming_llm is not None:
            current_llm = self.config.setdefault(
                "llm", deepcopy(self.default_config["llm"])
            )
            api_key = incoming_llm.pop("apiKey", "")
            incoming_llm.pop("apiKeyConfigured", None)
            current_llm.update(incoming_llm)
            if current_llm.get("openCodePlan") not in {"zen", "go"}:
                current_llm["openCodePlan"] = "zen"
            if current_llm.get("provider") == "OpenCode":
                from .llm_client import OPENCODE_PLANS

                current_llm["baseUrl"] = OPENCODE_PLANS[current_llm["openCodePlan"]][
                    "baseUrl"
                ]
            if api_key and api_key != "********":
                current_llm["apiKey"] = self._encrypt(api_key)
        self.save()

    def save_credentials(self, api_key: str, api_secret: str, access_token: str = ""):
        if "credentials" not in self.config:
            self.config["credentials"] = {}

        self.config["credentials"]["apiKey"] = self._encrypt(api_key)
        self.config["credentials"]["apiSecret"] = self._encrypt(api_secret)
        if access_token:
            self.config["credentials"]["accessToken"] = self._encrypt(access_token)
        self.save()

    def clear_access_token(self):
        if "credentials" in self.config:
            self.config["credentials"]["accessToken"] = ""
            self.save()

    def save_llm_api_key(self, llm_api_key: str):
        self.config.setdefault("llm", deepcopy(self.default_config["llm"]))
        self.config["llm"]["apiKey"] = self._encrypt(llm_api_key)
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
