import concurrent.futures
import json
import math
import sys
import threading
import traceback
import uuid
from datetime import timedelta

from kiteconnect import KiteConnect

from .analytics import analytics
from .broker_models import (
    SnapshotQuality,
    order_snapshot_to_renderer_dto,
    position_to_backend_dict,
)
from .config import config_manager
from .dev_mode import is_dev_mode
from .execution_gateway import execution_gateway
from .journal import journal
from .kite_client import kite_client
from .llm_client import OPENCODE_PLANS, OpenAICompatibleClient
from .request_policy import Priority, broker_gateway
from .risk_manager import risk_manager
from .scanner import scanner
from .ticker import ticker_manager
from .time_utils import now_utc
from .trading_engine import trading_engine
from .utils import DateTimeEncoder

# Research work may fetch history or call an LLM. It must not occupy the serial
# stdin dispatcher that admits an operator close or an emergency flatten.
_RESEARCH_METHODS = {
    "discover_models",
    "analytics_llm_post_mortem",
    "analytics_what_if",
    "run_backtest",
    "scan_now",
    "get_historical",
}
_RESEARCH_WORKERS = 2
_BROKER_WORKERS = 4
_OPERATOR_WORKERS = 2
_FAST_CONTROL_METHODS = {
    "stop_agent",
    "agent_status",
    "agent_set_mode",
    "agent_emergency_flatten",
}
_OPERATOR_METHODS = {"agent_close_position", "cancel_order"}
_LIFECYCLE_METHODS = {
    "set_credentials",
    "login",
    "check_session",
    "logout",
    "generate_session",
    "start_agent",
    "resume_supervision",
}
_lifecycle_lock = threading.RLock()


def _request_error(req, code, message):
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": req.get("id") if isinstance(req, dict) else None,
    }


def _validate_request(req):
    if not isinstance(req, dict):
        return _request_error(req, -32600, "Request must be an object")
    if not isinstance(req.get("method"), str) or not req["method"]:
        return _request_error(req, -32600, "method must be a nonempty string")
    if req.get("jsonrpc", "2.0") != "2.0":
        return _request_error(req, -32600, "jsonrpc must be 2.0")
    if not isinstance(req.get("params", {}), dict):
        return _request_error(req, -32602, "params must be an object")
    return None


def _assert_logout_safe():
    """Pause admission before proving authentication is no longer needed."""

    trading_engine.stop()
    if (
        trading_engine._has_residual_obligations()
        or trading_engine._pending_lifecycle_obligations()
    ):
        raise ValueError(
            "Logout requires all managed exposure and orders to be settled"
        )
    # Read orders before positions: an entry filling between a first position
    # read and a terminal order read must not be mistaken for a flat account.
    orders = trading_engine._order_snapshot(critical=True).require_complete()
    terminal = {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED", "REJECTED AMO"}
    if any(str(order.status).upper() not in terminal for order in orders.orders):
        raise ValueError("Logout requires all account orders to be terminal")
    positions = trading_engine._position_snapshot(critical=True).require_complete()
    if any(position.signed_quantity != 0 for position in positions.net):
        raise ValueError("Logout requires all account positions to be flat")
    if trading_engine._has_residual_obligations():
        raise ValueError(
            "Logout requires all managed exposure and orders to be settled"
        )


def _resume_authenticated_supervision():
    if not trading_engine.status()["supervisionActive"]:
        trading_engine.resume_supervision()


def _verify_candidate_account(candidate):
    """Authenticate off to the side before replacing a supervised connection."""

    profile = broker_gateway.execute(candidate.profile, priority=Priority.CRITICAL)
    account_id = str(profile.get("user_id") or "UNKNOWN")
    if account_id == "UNKNOWN":
        raise ValueError("Broker account identity could not be verified")
    previous_account = getattr(kite_client, "account_id", "UNKNOWN")
    if previous_account not in {None, "", "UNKNOWN"} and previous_account != account_id:
        raise ValueError(
            "Log out of the current account before switching broker accounts"
        )
    # Startup can have durable owners before the broker identity is restored.
    with trading_engine._trade_lock:
        owned_accounts = {
            trade.get("account_id")
            for trade in trading_engine.active_trades.values()
            if trade.get("account_id") not in {None, "", "UNKNOWN", "TEST_COMPAT"}
        }
    if owned_accounts and owned_accounts != {account_id}:
        raise ValueError("Broker account differs from unresolved managed exposure")
    scope = trading_engine._control_state_scope
    if scope and scope[1] != account_id and trading_engine._has_residual_obligations():
        raise ValueError("Broker account differs from unresolved operator obligations")
    return account_id


def _install_candidate_session(candidate, access_token, account_id):
    trading_engine.stop()
    # Both the old and candidate connections have the same verified account.
    # Do not call init()/set_access_token(): those clear account_id to UNKNOWN
    # while the risk worker can still be reading the shared client.
    kite_client.kite = candidate
    kite_client.access_token = access_token
    kite_client.account_id = account_id


def handle_request(req):
    invalid = _validate_request(req)
    if invalid is not None:
        return invalid
    # Authentication mutates a shared broker client. A concurrent logout/login
    # must not invalidate the identity halfway through startup reconciliation.
    if req["method"] in _LIFECYCLE_METHODS:
        with _lifecycle_lock:
            return _handle_request(req)
    return _handle_request(req)


def _handle_request(req):
    method = req.get("method")
    params = req.get("params", {})
    req_id = req.get("id")

    def success(result):
        return {"jsonrpc": "2.0", "result": result, "id": req_id}

    def error(code, message, data=None):
        return {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message, "data": data},
            "id": req_id,
        }

    try:
        if method == "set_credentials":
            config_manager.set_credentials(params.get("credentials", {}))
            return success({"status": "credentials_set"})

        elif method == "migrate_credentials":
            return success(config_manager.get_legacy_credentials())

        elif method == "clear_legacy_credentials":
            config_manager.clear_legacy_credentials()
            return success({"status": "legacy_credentials_cleared"})

        elif method == "login":
            creds = config_manager.get_credentials()
            api_key = params.get("api_key", creds.get("apiKey"))

            if not api_key:
                return error(-32602, "API Key required")

            candidate = KiteConnect(api_key=api_key)
            return success({"login_url": candidate.login_url()})

        elif method == "check_session":
            # Development bypass: no Zerodha login required. Start the ticker so
            # the synthetic dev emitter can feed the Watchlist (it uses the mock
            # client, not a real websocket).
            if is_dev_mode():
                ticker_manager.start("dev", "dev")
                _resume_authenticated_supervision()
                return success({"is_valid": True})

            creds = config_manager.get_credentials()
            api_key = creds.get("apiKey")
            access_token = creds.get("accessToken")
            if not api_key or not access_token:
                return success({"is_valid": False})

            # Status refreshes must not reset the shared account identity while
            # the independent supervisor is reconciling existing exposure.
            same_session = (
                kite_client.kite is not None
                and kite_client.access_token == access_token
                and getattr(kite_client.kite, "api_key", None) == api_key
            )
            # Verify the token is actually still valid with the Kite API
            try:
                if same_session:
                    kite_client.get_margins()
                    account_id = kite_client.account_id
                    if account_id == "UNKNOWN":
                        account_id = _verify_candidate_account(kite_client.kite)
                        kite_client.account_id = account_id
                else:
                    candidate = KiteConnect(api_key=api_key)
                    candidate.set_access_token(access_token)
                    account_id = _verify_candidate_account(candidate)
                    _install_candidate_session(candidate, access_token, account_id)
            except Exception:
                return success({"is_valid": False})
            if account_id == "UNKNOWN":
                risk_manager.reconciliation_status = "RECONCILIATION_PENDING"
                return success({"is_valid": False})

            # Start ticker on resume
            ticker_manager.start(api_key, access_token)
            if same_session:
                _resume_authenticated_supervision()
            else:
                trading_engine.resume_supervision()

            return success({"is_valid": True})

        elif method == "logout":
            _assert_logout_safe()
            config_manager.clear_access_token()
            kite_client.set_access_token(None)
            ticker_manager.stop()
            return success({"status": "logged_out"})

        elif method == "generate_session":
            request_token = params.get("request_token")
            api_key = params.get("api_key")
            api_secret = params.get("api_secret")

            candidate = KiteConnect(api_key=api_key)
            session = candidate.generate_session(request_token, api_secret)
            candidate.set_access_token(session["access_token"])
            account_id = _verify_candidate_account(candidate)
            _install_candidate_session(candidate, session["access_token"], account_id)

            # Save all creds including token
            config_manager.save_credentials(
                api_key, api_secret, session["access_token"]
            )

            ticker_manager.start(api_key, session["access_token"])
            trading_engine.resume_supervision()
            return success(session)

        elif method == "get_positions":
            return success(kite_client.get_positions())

        elif method == "get_orders":
            if hasattr(kite_client, "get_order_history_snapshot"):
                return success(
                    order_snapshot_to_renderer_dto(
                        kite_client.get_order_history_snapshot()
                    )
                )
            return success(kite_client.get_orders())

        elif method == "get_holdings":
            return success(kite_client.get_holdings())

        elif method == "get_margins":
            return success(kite_client.get_margins())

        elif method == "place_order":
            # The legacy ticket has no server-owned stop/thesis and cannot be
            # admitted safely as an entry.  Keep the route explicit and fail
            # closed until it is wired through the signal/reservation flow.
            return error(
                -32004,
                "Manual entries are disabled: submit a fresh validated signal "
                "through execute_signal, or use a managed reduction command.",
            )

        elif method == "cancel_order":
            order_id = params.get("order_id", params.get("orderId"))
            if not order_id:
                return error(-32602, "orderId is required")
            return success(trading_engine.request_operator_cancel(order_id))

        elif method == "modify_order":
            res = execution_gateway.modify_order(**params)
            return success(res)

        elif method == "get_historical":
            res = kite_client.get_historical_data(**params)
            return success(res)

        elif method == "get_quote":
            return success(kite_client.get_quote(params.get("instruments", [])))

        elif method == "get_ltp":
            return success(kite_client.get_ltp(params.get("instruments", [])))

        elif method == "get_instruments":
            return success(kite_client.get_instruments(params.get("exchange")))

        elif method == "search_instruments":
            return success(kite_client.search_instruments(params.get("query", "")))

        elif method == "ticker_subscribe":
            ticker_manager.subscribe(params.get("tokens", []))
            return success(
                {"status": "subscribed", "count": len(ticker_manager.tokens)}
            )

        elif method == "ticker_unsubscribe":
            ticker_manager.unsubscribe(params.get("tokens", []))
            return success({"status": "unsubscribed"})

        elif method == "ticker_status":
            return success(ticker_manager.status())

        elif method == "start_agent":
            mode = params.get("mode", "auto")
            return success(trading_engine.start(mode))

        elif method == "stop_agent":
            return success(trading_engine.stop())

        elif method == "agent_status":
            return success(trading_engine.status())

        elif method == "resume_supervision":
            return success(trading_engine.resume_supervision())

        elif method == "agent_set_mode":
            return success(trading_engine.set_mode(params.get("mode")))

        elif method == "agent_close_position":
            return success(
                trading_engine.request_operator_close(params.get("positionKey"))
            )

        elif method == "agent_emergency_flatten":
            return success(
                trading_engine.request_emergency_flatten(params.get("scope"))
            )

        elif method == "get_settings":
            return success(config_manager.get_settings())

        elif method == "save_settings":
            config_manager.save_settings(params)
            return success({"status": "saved"})

        elif method == "save_llm_api_key":
            config_manager.save_llm_api_key(params.get("llmApiKey", ""))
            return success({"status": "saved"})

        elif method == "discover_models":
            llm = config_manager.get_llm_settings()
            credentials = config_manager.get_credentials()
            api_key = params.get("apiKey") or credentials.get("llmApiKey", "")
            provider = params.get("provider", llm.get("provider", "Gemini"))
            base_url = params.get("baseUrl", llm.get("baseUrl", ""))
            plan = params.get("openCodePlan", llm.get("openCodePlan", "zen"))
            if provider == "OpenCode":
                plan = plan if plan in OPENCODE_PLANS else "zen"
                base_url = OPENCODE_PLANS[plan]["baseUrl"]
            if provider not in {"Ollama", "OpenCode"} and not api_key:
                return error(-32602, "LLM API Key not configured in settings.")
            if provider == "OpenCode":
                models = OpenAICompatibleClient().discover_models(
                    provider, base_url, api_key, plan=plan
                )
            else:
                models = OpenAICompatibleClient().discover_models(
                    provider, base_url, api_key
                )
            return success(models)

        elif method == "scan_now":
            from .nifty_universe import get_nifty100_universe
            from .screener import screener_engine

            custom_watchlist = config_manager.get_watchlist()
            full_universe = list(set(get_nifty100_universe() + custom_watchlist))

            # Run the AI screener
            top_stocks = screener_engine.generate_daily_watchlist(
                universe=full_universe, limit=12
            )
            # Scan those top stocks
            signals = scanner.scan_watchlist(top_stocks)
            return success(signals)

        elif method == "dashboard_summary":
            margins = kite_client.get_margins()
            equity_margin = margins.get("equity", {})
            available_margin = equity_margin.get("available", {}).get("live_balance")
            if available_margin is None:
                available_margin = equity_margin.get("net")
            if (
                isinstance(available_margin, bool)
                or not isinstance(available_margin, (int, float))
                or not math.isfinite(available_margin)
            ):
                available_margin = None

            if risk_manager.reconciliation_status == "RECONCILIATION_PENDING":
                try:
                    risk_manager.reconcile_state()
                except Exception as e:
                    from .utils import push_log

                    push_log(
                        f"Auto-reconcile on dashboard failed: {e}", level="warning"
                    )

            position_snapshot = kite_client.get_positions_snapshot()
            positions = (
                [
                    position_to_backend_dict(position)
                    for position in position_snapshot.net
                ]
                if position_snapshot.quality is SnapshotQuality.COMPLETE
                else []
            )
            known_pnl = [p["pnl"] for p in positions if p.get("pnl") is not None]
            total_pnl = (
                sum(known_pnl)
                if position_snapshot.quality is SnapshotQuality.COMPLETE
                and len(known_pnl) == len(positions)
                else None
            )

            calculated_used_margin = 0.0
            used_margin_known = position_snapshot.quality is SnapshotQuality.COMPLETE
            for p in positions:
                if p.get("quantity", 0) != 0:
                    multiplier = 0.2 if p.get("product") == "MIS" else 1.0
                    avg_price = p.get("average_price")
                    if avg_price is None:
                        avg_price = (
                            p.get("buy_price")
                            if p.get("quantity", 0) > 0
                            else p.get("sell_price")
                        )
                    if avg_price is None or avg_price <= 0:
                        used_margin_known = False
                        continue
                    calculated_used_margin += (
                        abs(p.get("quantity", 0)) * avg_price * multiplier
                    )

            used_margin = calculated_used_margin if used_margin_known else None

            # Count trades and win rate based on realized positions (quantity == 0) and open positions
            counts = journal.get_todays_trade_counts()
            trades_today = counts["total"]
            verified_today = journal.get_verified_todays_outcomes()
            winning_trades = sum(
                1 for trade in verified_today if (trade.get("net_pnl") or 0) > 0
            )
            losing_trades = sum(
                1 for trade in verified_today if (trade.get("net_pnl") or 0) <= 0
            )
            win_rate = (
                winning_trades / len(verified_today) * 100 if verified_today else 0
            )

            summary = {
                "totalPnl": round(total_pnl, 2) if total_pnl is not None else None,
                "netPnl": (
                    round(risk_manager.daily_pnl, 2)
                    if risk_manager.accounting_quality != "UNAVAILABLE"
                    else None
                ),
                "tradesToday": trades_today,
                "winRate": round(win_rate, 2),
                "winningTrades": winning_trades,
                "losingTrades": losing_trades,
                "availableMargin": available_margin,
                "usedMargin": used_margin,
                "killSwitchActive": risk_manager.kill_switch_active,
                "reconciliationStatus": risk_manager.reconciliation_status,
            }
            return success(summary)

        elif method == "execute_signal":
            if not trading_engine.status().get("supervisionActive", False):
                return error(
                    -32004,
                    "Entry requires active, reconciled position supervision",
                )
            res = trading_engine.execute_signal(params.get("signal", {}))
            return success({"executed": res})

        elif method == "journal_get_trades":
            return success(journal.get_trades())

        elif method == "journal_get_events":
            return success(journal.get_trade_events(params.get("trade_id")))

        elif method == "analytics_strategy_expectancy":
            return success(analytics.get_strategy_expectancy())

        elif method == "analytics_confluence_validation":
            return success(analytics.get_confluence_validation())

        elif method == "analytics_signal_score_calibration":
            return success(analytics.get_signal_score_calibration())

        elif method == "analytics_exit_reason_effectiveness":
            return success(analytics.get_exit_reason_effectiveness())

        elif method == "analytics_trade_replay":
            return success(analytics.get_trade_replay(params.get("trade_id")))

        elif method == "analytics_what_if":
            return success(analytics.get_what_if_analysis(params.get("trade_id")))

        elif method == "analytics_llm_post_mortem":
            return success(analytics.generate_llm_post_mortem(params.get("trade_id")))

        elif method == "run_backtest":
            strategy_id = params.get("strategy_id")
            symbol = params.get("symbol")
            days = params.get("days", 30)
            initial_capital = params.get("initial_capital", 100000.0)

            # Look up the strategy from scanner
            strategy = scanner.strategies.get(strategy_id)
            if not strategy:
                return error(-32602, f"Strategy {strategy_id} not found")

            import pandas as pd

            from .backtesting.backtest_engine import BacktestEngine
            from .backtesting.metrics_evaluator import MetricsEvaluator

            # Fetch data (mocking the date range based on days parameter)
            now = now_utc()
            from_date = now - timedelta(days=days)

            instruments = kite_client.get_instruments("NSE")
            instrument_map = {
                i["tradingsymbol"]: i["instrument_token"] for i in instruments
            }
            token = instrument_map.get(symbol)

            if not token:
                return error(-32602, f"Symbol {symbol} not found in instruments")

            records = kite_client.get_historical_data(
                instrument_token=token,
                from_date=from_date,
                to_date=now,
                interval="5minute",
            )

            if not records:
                return error(-32000, "No historical data found for backtest")

            df = pd.DataFrame(records)
            for col in ["open", "high", "low", "close"]:
                if col in df.columns:
                    df[col] = df[col].astype(float)

            engine = BacktestEngine(strategy, initial_capital=initial_capital)
            engine.load_data(symbol, df)
            engine.run()

            metrics = MetricsEvaluator.evaluate(engine.broker.trades, initial_capital)
            return success({"metrics": metrics, "trades": engine.broker.trades})

        else:
            return error(-32601, f"Method '{method}' not found")

    except Exception as e:
        return error(-32000, str(e), traceback.format_exc())


def _write_response(response):
    from .utils import stdout_lock as _stdout_lock

    with _stdout_lock:
        print(json.dumps(response, cls=DateTimeEncoder))
        sys.stdout.flush()


def main():
    from .utils import stdout_lock as _stdout_lock

    generation = str(uuid.uuid4())
    with _stdout_lock:
        print(
            json.dumps(
                {
                    "event": "backend:ready",
                    "data": {"ready": True, "generation": generation},
                }
            )
        )
        sys.stdout.flush()

    pools = {
        name: (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix=f"{name}-rpc"
            ),
            threading.BoundedSemaphore(workers),
        )
        for name, workers in (
            ("research", _RESEARCH_WORKERS),
            ("broker", _BROKER_WORKERS),
            ("operator", _OPERATOR_WORKERS),
        )
    }

    def run_worker(req, capacity):
        try:
            _write_response(handle_request(req))
        finally:
            capacity.release()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                _write_response(_request_error(None, -32700, "Parse error"))
                continue

            invalid = _validate_request(req)
            if invalid is not None:
                _write_response(invalid)
                continue
            method = req["method"]
            if method in _FAST_CONTROL_METHODS:
                _write_response(handle_request(req))
                continue

            pool = (
                "research"
                if method in _RESEARCH_METHODS
                else "operator"
                if method in _OPERATOR_METHODS
                else "broker"
            )
            executor, capacity = pools[pool]
            if not capacity.acquire(blocking=False):
                _write_response(
                    _request_error(req, -32005, f"{pool} worker capacity is full")
                )
                continue
            executor.submit(run_worker, req, capacity)
    finally:
        for executor, _ in pools.values():
            executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
