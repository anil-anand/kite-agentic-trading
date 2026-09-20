import datetime
import json
import os
import tempfile
import threading
from copy import deepcopy
from pathlib import Path

from cryptography.fernet import Fernet

from .time_utils import as_utc


class ConfigManager:
    def __init__(self):
        self.config_dir = Path.home() / ".kite-agentic-trading"
        self.config_file = self.config_dir / "config.json"
        self.key_file = self.config_dir / ".key"
        self.config = {}
        self._state_file_lock = threading.RLock()

        self.default_config = {
            "risk": {
                "maxCapitalPerTrade": 10000,
                "leverageMultiplier": 5,
                "maxDailyLoss": 2000,
                "maxDailyTrades": 10,
                "maxTradesPerSymbolPerDay": 2,
                "tradeCooldownMins": 15,
                "maxSimultaneousPositions": 5,
                "startTradeAfter": "09:45",
                "noNewTradesAfter": "15:00",
                "autoSquareOff": True,
                "squareOffTime": "15:15",
                "defaultStopLossPercent": 1.5,
                "defaultTargetPercent": 3,
                "positionRevalIntervalMins": 30,
                "positionRevalWeakExitMins": 60,
                "positionRevalBreakevenMins": 45,
                "stopOrderType": "SL",
                "haltAutoTradesOnStopFailure": True,
                "hardRiskPolicyVersion": "hard-risk-v1",
                "supervisorIntervalSeconds": 5,
                "marketOpenTime": "09:15",
                "marketCloseTime": "15:30",
                "maxGrossExposure": 200000,
                "maxNetExposure": 100000,
                "maxSingleSymbolExposure": 50000,
                "maxSectorExposure": 75000,
                "maxCorrelatedExposure": 75000,
                "correlationThreshold": 0.70,
                "correlationLookbackDays": 30,
            },
            "strategies": {
                "evaluateOnIncompleteCandle": False,
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
            "families": {
                "trend": {"weight": 1.0, "enabled": True},
                "mean_reversion": {"weight": 1.0, "enabled": True},
                "breakout": {"weight": 1.0, "enabled": True},
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
            "screener": {
                "weights": {
                    "baseline": 1.0,
                    "gap": 1.0,
                    "volatility": 1.0,
                    "trend": 1.0,
                    "volume": 1.0,
                    "liquidity": 1.0,
                },
                "filters": {
                    "min_volume": 100000,
                    "min_value_traded": 10000000,
                },
                "refreshSchedule": {
                    "openingPeriodMins": 15,
                    "normalSessionMins": 60,
                    "lateSessionMins": 30,
                },
            },
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
            # Execution recovery is independent of discretionary exit policy.
            # Pinning this value makes lifecycle traces interpretable across
            # future broker-capability changes.
            "orderLifecycle": {
                "policyVersion": "order-lifecycle-v1",
                "workingAttemptTimeoutSeconds": 15,
            },
        }

        self.in_memory_credentials = {}

        self._init_dir()
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

    def clear_legacy_credentials(self):
        """Called after successful migration to OS native storage."""
        if "credentials" in self.config:
            del self.config["credentials"]
        if "llm" in self.config and "apiKey" in self.config["llm"]:
            self.config["llm"]["apiKey"] = ""
        self.save()
        if self.key_file.exists():
            try:
                self.key_file.unlink()
            except Exception:
                pass

    def save(self):
        # Settings and lifecycle metadata can be updated by independent
        # recovery/control paths.  A complete JSON file is not the lifecycle
        # source of truth (SQLite is), but it must never be torn or partially
        # overwrite a compatible checkpoint on process interruption.
        with self._state_file_lock:
            self._atomic_write_json(self.config_file, self.config)

    def get_legacy_credentials(self):
        """Extract credentials from legacy encrypted config.json."""
        self._init_key()
        creds = self.config.get("credentials", {})
        llm = self.config.get("llm", {})
        encrypted_llm_key = llm.get("apiKey") or creds.get("llmApiKey", "")
        return {
            "apiKey": self._decrypt(creds.get("apiKey", "")),
            "apiSecret": self._decrypt(creds.get("apiSecret", "")),
            "accessToken": self._decrypt(creds.get("accessToken", "")),
            "llmApiKey": self._decrypt(encrypted_llm_key),
        }

    def set_credentials(self, creds: dict):
        self.in_memory_credentials.update(creds)

    def get_credentials(self):
        return {
            "apiKey": self.in_memory_credentials.get("apiKey", ""),
            "apiSecret": self.in_memory_credentials.get("apiSecret", ""),
            "accessToken": self.in_memory_credentials.get("accessToken", ""),
            "llmApiKey": self.in_memory_credentials.get("llmApiKey", ""),
        }

    def get_llm_settings(self):
        return deepcopy(self.config.get("llm", self.default_config["llm"]))

    def get_settings(self):
        settings = deepcopy(self.config)
        settings.setdefault("llm", deepcopy(self.default_config["llm"]))
        settings["llm"]["apiKey"] = ""
        settings["llm"]["apiKeyConfigured"] = bool(
            self.in_memory_credentials.get("llmApiKey")
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
            # Update in-memory, but do not write to disk
            for key in ("apiKey", "apiSecret", "accessToken"):
                value = incoming_credentials.get(key, "")
                if value and value != "********":
                    self.in_memory_credentials[key] = value

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
                self.in_memory_credentials["llmApiKey"] = api_key
        self.save()

    def save_credentials(self, api_key: str, api_secret: str, access_token: str = ""):
        self.in_memory_credentials["apiKey"] = api_key
        self.in_memory_credentials["apiSecret"] = api_secret
        if access_token:
            self.in_memory_credentials["accessToken"] = access_token

    def clear_access_token(self):
        self.in_memory_credentials["accessToken"] = ""

    def save_llm_api_key(self, llm_api_key: str):
        self.in_memory_credentials["llmApiKey"] = llm_api_key

    def get_risk_config(self):
        return self.config.get("risk", self.default_config["risk"])

    def get_strategy_config(self):
        return self.config.get("strategies", self.default_config["strategies"])

    def get_families_config(self):
        return self.config.get("families", self.default_config.get("families", {}))

    def get_screener_config(self):
        return self.config.get("screener", self.default_config.get("screener", {}))

    def get_order_lifecycle_config(self):
        return self.config.get("orderLifecycle", self.default_config["orderLifecycle"])

    def get_watchlist(self):
        return self.config.get("watchlist", self.default_config["watchlist"])

    def get_app_order_ids(self) -> set:
        with self._state_file_lock:
            path = self.config_dir / "app_orders.json"
            if path.exists():
                try:
                    with open(path, "r") as f:
                        return {str(order_id) for order_id in json.load(f)}
                except Exception:
                    return set()
            return set()

    def get_app_order_roles(self) -> dict[str, str]:
        with self._state_file_lock:
            legacy_ids = self.get_app_order_ids()
            path = self.config_dir / "app_order_roles.json"
            roles = {}
            if path.exists():
                try:
                    with open(path, "r") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        roles = {
                            str(order_id): str(role).upper()
                            for order_id, role in loaded.items()
                        }
                except Exception:
                    roles = {}
            for order_id in legacy_ids:
                roles.setdefault(order_id, "UNKNOWN")
            return roles

    def add_app_order_id(self, order_id: str, role: str = "UNKNOWN"):
        if not order_id:
            return
        normalized_role = str(role).upper()
        if normalized_role not in {"ENTRY", "PROTECTION", "REDUCTION", "UNKNOWN"}:
            normalized_role = "UNKNOWN"
        with self._state_file_lock:
            orders = self.get_app_order_ids()
            orders.add(str(order_id))
            path = self.config_dir / "app_orders.json"
            self._atomic_write_json(path, sorted(orders))

            roles = self.get_app_order_roles()
            roles[str(order_id)] = normalized_role
            self._atomic_write_json(self.config_dir / "app_order_roles.json", roles)

    def get_historical_orders(self) -> dict:
        with self._state_file_lock:
            path = self.config_dir / "historical_orders.json"
            if path.exists():
                try:
                    with open(path, "r") as f:
                        loaded = json.load(f)
                    return loaded if isinstance(loaded, dict) else {}
                except Exception:
                    return {}
            return {}

    def save_historical_orders(self, orders: dict):
        with self._state_file_lock:
            path = self.config_dir / "historical_orders.json"
            self._atomic_write_json(path, orders)

    def save_active_trades(self, trades: dict):
        """Persist the trading engine's active_trades to disk.

        Execution, observation and reevaluation timestamps are stored as ISO
        strings and restored by load_active_trades after a crash or restart.
        """
        path = self.config_dir / "active_trades.json"
        serializable = {}
        for symbol, trade in trades.items():
            record = dict(trade)
            for field in ("entry_time", "entry_observed_at", "last_reeval_time"):
                timestamp = record.get(field)
                if isinstance(timestamp, (datetime.datetime, datetime.date)):
                    record[field] = timestamp.isoformat()
            serializable[symbol] = record
        self._atomic_write_json(path, serializable)

    @staticmethod
    def _atomic_write_json(path: Path, data):
        """Write JSON atomically: serialize to a temp file in the same dir, then
        os.replace() it into place. A crash mid-write (the exact scenario this
        persistence guards against) or a concurrent write can never leave a
        truncated/corrupt file — the previous complete version stays until the
        rename succeeds. A unique temp name keeps concurrent writers from
        clobbering each other's temp file.
        """
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=4, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load_active_trades(self) -> dict:
        """Restore known timestamps while preserving unknown execution times.

        Returns {} if there's nothing persisted or the file is unreadable —
        the engine reconciles against live positions regardless.
        """
        path = self.config_dir / "active_trades.json"
        if not path.exists():
            return {}
        try:
            with open(path, "r") as f:
                trades = json.load(f)
        except Exception:
            return {}
        for trade in trades.values():
            for field in ("entry_time", "entry_observed_at", "last_reeval_time"):
                if field not in trade:
                    continue
                timestamp = trade[field]
                try:
                    trade[field] = as_utc(timestamp) if timestamp is not None else None
                except (TypeError, ValueError):
                    trade[field] = None
        return trades

    def save_daily_risk_state(self, state: dict):
        """Persist the daily risk state to disk."""
        path = self.config_dir / "daily_risk_state.json"
        self._atomic_write_json(path, state)

    def load_daily_risk_state(self) -> dict:
        """Load persisted daily risk state."""
        path = self.config_dir / "daily_risk_state.json"
        if not path.exists():
            return {}
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _operator_state_key(namespace: str, account_id: str) -> str:
        if not namespace or not account_id or account_id == "UNKNOWN":
            raise ValueError("operator state requires a verified account identity")
        return json.dumps([str(namespace), str(account_id)], separators=(",", ":"))

    def _load_operator_states(self) -> dict:
        path = self.config_dir / "operator_state.json"
        if not path.exists():
            return {}
        with path.open() as stream:
            states = json.load(stream)
        if not isinstance(states, dict) or any(
            not isinstance(state, dict) for state in states.values()
        ):
            raise ValueError("persisted operator state is malformed")
        return states

    def load_operator_state(self, namespace: str, account_id: str) -> dict:
        """Restore control obligations only for their original account.

        Unreadable state is a recovery error, never proof that no flatten or
        pause was requested. These records contain no authentication material.
        """
        key = self._operator_state_key(namespace, account_id)
        with self._state_file_lock:
            return deepcopy(self._load_operator_states().get(key, {}))

    def save_operator_state(self, namespace: str, account_id: str, state: dict) -> None:
        key = self._operator_state_key(namespace, account_id)
        if not isinstance(state, dict):
            raise ValueError("operator state must be an object")
        with self._state_file_lock:
            states = self._load_operator_states()
            states[key] = deepcopy(state)
            self._atomic_write_json(self.config_dir / "operator_state.json", states)


config_manager = ConfigManager()
