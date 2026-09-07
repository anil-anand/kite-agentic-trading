import json
import uuid
from datetime import datetime

from .config import config_manager
from .kite_client import kite_client
from .nifty_universe import get_nifty100_universe
from .risk_manager import risk_manager
from .trading_engine import trading_engine
from .utils import push_log


class AgentGateway:
    """
    AgentGateway serves as the strict capability boundary for LLM/agent integration.
    It ensures that an LLM can never bypass deterministic trading and risk controls.
    """

    def __init__(self):
        pass

    def validate_and_route_proposal(self, llm_output: str, model_info: dict) -> dict:
        """
        Validates a trade proposal from an LLM and routes it to the deterministic trading engine if valid.
        """
        decision = {"status": "REJECTED", "reason": "", "proposal": None}

        try:
            proposal = json.loads(llm_output)
        except json.JSONDecodeError:
            decision["reason"] = "Malformed output: Not valid JSON"
            self._audit_log(model_info, llm_output, decision)
            return decision

        if not isinstance(proposal, dict):
            decision["reason"] = (
                "Malformed output: JSON must be an object, not array/null/scalar"
            )
            self._audit_log(model_info, llm_output, decision)
            return decision

        decision["proposal"] = proposal

        symbol = proposal.get("tradingsymbol")
        direction = proposal.get("direction")
        quantity = proposal.get("quantity")
        order_type = proposal.get("order_type")
        price = proposal.get("price")
        stop_loss = proposal.get("stop_loss")
        target = proposal.get("target")

        if not all([symbol, direction, order_type, price, stop_loss, target]):
            decision["reason"] = "Incomplete output: Missing required fields"
            self._audit_log(model_info, llm_output, decision)
            return decision

        if quantity is None:
            decision["reason"] = "Incomplete output: Missing quantity field"
            self._audit_log(model_info, llm_output, decision)
            return decision

        allowlist = get_nifty100_universe() + config_manager.get_watchlist()
        if symbol not in allowlist:
            decision["reason"] = f"Symbol '{symbol}' not in allowlist"
            self._audit_log(model_info, llm_output, decision)
            return decision

        if direction not in ["BUY", "SELL"]:
            decision["reason"] = f"Invalid direction: {direction}"
            self._audit_log(model_info, llm_output, decision)
            return decision

        try:
            quantity = int(quantity)
            if quantity <= 0 or quantity > 100000:
                decision["reason"] = f"Invalid quantity bounds: {quantity}"
                self._audit_log(model_info, llm_output, decision)
                return decision
        except ValueError:
            decision["reason"] = "Quantity must be an integer"
            self._audit_log(model_info, llm_output, decision)
            return decision

        if order_type not in ["LIMIT", "MARKET"]:
            decision["reason"] = f"Invalid order type: {order_type}"
            self._audit_log(model_info, llm_output, decision)
            return decision

        try:
            price = float(price)
            stop_loss = float(stop_loss)
            target = float(target)
            if price <= 0 or stop_loss <= 0 or target <= 0:
                decision["reason"] = "Prices must be positive"
                self._audit_log(model_info, llm_output, decision)
                return decision

            if direction == "BUY":
                if stop_loss >= price or target <= price:
                    decision["reason"] = "Invalid price levels for BUY"
                    self._audit_log(model_info, llm_output, decision)
                    return decision
            else:
                if stop_loss <= price or target >= price:
                    decision["reason"] = "Invalid price levels for SELL"
                    self._audit_log(model_info, llm_output, decision)
                    return decision
        except ValueError:
            decision["reason"] = "Prices must be numeric"
            self._audit_log(model_info, llm_output, decision)
            return decision

        # Recompute quantity using the deterministic risk sizing path to enforce
        # the configured per-trade risk budget; treat the LLM's quantity as advisory.
        sized_quantity = risk_manager.calculate_position_size(price, stop_loss)
        if sized_quantity <= 0:
            decision["reason"] = "Risk manager returned zero position size"
            self._audit_log(model_info, llm_output, decision)
            return decision
        # Clamp: never exceed what the LLM requested, and always use the risk-sized value.
        quantity = min(quantity, sized_quantity)

        candidate_signal = {
            "id": str(uuid.uuid4()),
            "tradingsymbol": symbol,
            "direction": direction,
            "entryPrice": price,
            "stopLoss": stop_loss,
            "target": target,
            "quantity": quantity,
            "strategy": "llm_agent",
            "reasoning": proposal.get("reasoning", ""),
            "signal_score": 100,
            # estimated_probability is intentionally omitted: the normal calibration
            # pipeline will populate it only when statistically backed.
        }

        try:
            open_orders = []
            try:
                open_orders = kite_client.get_orders()
            except Exception:
                pass

            can_accept, reject_reason = risk_manager.can_accept_position(
                symbol=symbol,
                direction=direction,
                qty=quantity,
                price=price,
                active_trades=trading_engine.active_trades,
                open_orders=open_orders,
            )

            if not can_accept:
                decision["reason"] = f"Risk limit rejected: {reject_reason}"
                self._audit_log(model_info, llm_output, decision)
                return decision

            executed = trading_engine.execute_signal(candidate_signal)
            if executed:
                decision["status"] = "ACCEPTED"
                decision["reason"] = "Proposal executed successfully"
            else:
                decision["reason"] = "Trading engine rejected or failed execution"

        except Exception as e:
            decision["reason"] = f"Execution error: {str(e)}"

        self._audit_log(model_info, llm_output, decision)
        return decision

    def _audit_log(self, model_info: dict, output: str, decision: dict):
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "model": model_info.get("model", "unknown"),
            "prompt_version": model_info.get("prompt_version", "unknown"),
            "llm_output": output,
            "decision": decision["status"],
            "reason": decision["reason"],
        }
        push_log(f"AgentGateway Audit: {json.dumps(log_entry)}", level="info")


agent_gateway = AgentGateway()
