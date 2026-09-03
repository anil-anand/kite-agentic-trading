import json
import sys
import traceback

from .analytics import analytics
from .config import config_manager
from .journal import journal
from .kite_client import kite_client
from .scanner import scanner
from .ticker import ticker_manager
from .trading_engine import trading_engine
from .utils import DateTimeEncoder


def handle_request(req):
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
        if method == "login":
            creds = config_manager.get_credentials()
            api_key = params.get("api_key", creds.get("apiKey"))
            api_secret = params.get("api_secret", creds.get("apiSecret"))

            if not api_key:
                return error(-32602, "API Key required")

            config_manager.save_credentials(api_key, api_secret)
            kite_client.init(api_key)
            return success({"login_url": kite_client.login_url()})

        elif method == "check_session":
            creds = config_manager.get_credentials()
            api_key = creds.get("apiKey")
            access_token = creds.get("accessToken")
            if not api_key or not access_token:
                return success({"is_valid": False})

            kite_client.init(api_key)
            kite_client.set_access_token(access_token)

            # Verify the token is actually still valid with the Kite API
            try:
                kite_client.get_margins()
            except Exception:
                return success({"is_valid": False})

            # Start ticker on resume
            ticker_manager.start(api_key, access_token)

            return success({"is_valid": True})

        elif method == "generate_session":
            request_token = params.get("request_token")
            api_key = params.get("api_key")
            api_secret = params.get("api_secret")

            # Initialize client before generating session
            kite_client.init(api_key)

            session = kite_client.generate_session(request_token, api_secret)

            # Save all creds including token
            config_manager.save_credentials(
                api_key, api_secret, session["access_token"]
            )

            ticker_manager.start(api_key, session["access_token"])
            return success(session)

        elif method == "get_positions":
            return success(kite_client.get_positions())

        elif method == "get_orders":
            return success(kite_client.get_orders())

        elif method == "get_holdings":
            return success(kite_client.get_holdings())

        elif method == "get_margins":
            return success(kite_client.get_margins())

        elif method == "place_order":
            order_id = kite_client.place_order(**params)
            return success({"order_id": order_id})

        elif method == "cancel_order":
            res = kite_client.cancel_order(**params)
            return success(res)

        elif method == "modify_order":
            res = kite_client.modify_order(**params)
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

        elif method == "start_agent":
            mode = params.get("mode", "auto")
            trading_engine.start(mode)
            return success({"status": "started", "mode": mode})

        elif method == "stop_agent":
            trading_engine.stop()
            return success({"status": "stopped"})

        elif method == "agent_status":
            return success(trading_engine.status())

        elif method == "get_settings":
            return success(config_manager.config)

        elif method == "save_settings":
            config_manager.config.update(params)
            config_manager.save()
            return success({"status": "saved"})

        elif method == "save_llm_api_key":
            config_manager.save_llm_api_key(params.get("llmApiKey", ""))
            return success({"status": "saved"})

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
            available_margin = equity_margin.get("available", {}).get("live_balance", 0)
            if not available_margin:
                available_margin = equity_margin.get("net", 0)

            positions = kite_client.get_positions().get("net", [])
            total_pnl = sum(p.get("pnl", p.get("m2m", 0)) for p in positions)

            calculated_used_margin = 0
            for p in positions:
                if p.get("quantity", 0) != 0:
                    multiplier = 0.2 if p.get("product") == "MIS" else 1.0
                    avg_price = p.get("averagePrice", 0)
                    if avg_price == 0:
                        avg_price = (
                            p.get("buyPrice", 0)
                            if p.get("quantity", 0) > 0
                            else p.get("sellPrice", 0)
                        )
                    calculated_used_margin += (
                        abs(p.get("quantity", 0)) * avg_price * multiplier
                    )

            used_margin = calculated_used_margin

            # Count trades and win rate based on realized positions (quantity == 0) and open positions
            trades_today = len(positions)
            winning_trades = sum(
                1 for p in positions if p.get("pnl", p.get("m2m", 0)) > 0
            )
            win_rate = (winning_trades / trades_today * 100) if trades_today > 0 else 0

            summary = {
                "totalPnl": round(total_pnl, 2),
                "tradesToday": trades_today,
                "winRate": round(win_rate, 2),
                "availableMargin": available_margin,
                "usedMargin": used_margin,
            }
            return success(summary)

        elif method == "execute_signal":
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

        elif method == "analytics_confidence_calibration":
            return success(analytics.get_confidence_calibration())

        elif method == "analytics_exit_reason_effectiveness":
            return success(analytics.get_exit_reason_effectiveness())

        elif method == "analytics_trade_replay":
            return success(analytics.get_trade_replay(params.get("trade_id")))

        elif method == "analytics_what_if":
            return success(analytics.get_what_if_analysis(params.get("trade_id")))

        elif method == "analytics_llm_post_mortem":
            return success(analytics.generate_llm_post_mortem(params.get("trade_id")))

        else:
            return error(-32601, f"Method '{method}' not found")

    except Exception as e:
        return error(-32000, str(e), traceback.format_exc())


def main():
    from .trading_engine import _stdout_lock

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            res = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }
            with _stdout_lock:
                print(json.dumps(res))
                sys.stdout.flush()
            continue

        res = handle_request(req)
        with _stdout_lock:
            print(json.dumps(res, cls=DateTimeEncoder))
            sys.stdout.flush()


if __name__ == "__main__":
    main()
