"""Pure hard-risk predicates shared by live supervision and future exit policy.

The functions here do not access configuration, a broker, mutable account state,
or wall-clock time.  They only turn an explicit snapshot into a deterministic
obligation, so a hard exit can be replayed and cannot be vetoed by thesis logic.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .session_clock import SessionSnapshot
from .time_utils import as_utc


class HardRiskAction(str, Enum):
    HOLD = "HOLD"
    EXIT_POSITION = "EXIT_POSITION"
    FLATTEN_ACCOUNT = "FLATTEN_ACCOUNT"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


class HardRiskReason(str, Enum):
    RISK_CATASTROPHIC_STOP = "RISK_CATASTROPHIC_STOP"
    RISK_DAILY_LOSS = "RISK_DAILY_LOSS"
    RISK_PROTECTION_FAILURE = "RISK_PROTECTION_FAILURE"
    SESSION_FORCED_FLAT = "SESSION_FORCED_FLAT"
    OPERATOR_EMERGENCY_FLATTEN = "OPERATOR_EMERGENCY_FLATTEN"
    OPERATOR_POSITION_CLOSE = "OPERATOR_POSITION_CLOSE"
    EXEC_BROKER_STATE_UNKNOWN = "EXEC_BROKER_STATE_UNKNOWN"
    HOLD_NO_HARD_RISK = "HOLD_NO_HARD_RISK"


@dataclass(frozen=True)
class HardRiskPolicy:
    """Pinned policy values needed by hard-risk evaluation."""

    policy_version: str = "hard-risk-v1"
    require_protection: bool = True
    mark_max_age_seconds: float = 120.0


@dataclass(frozen=True)
class HardRiskSnapshot:
    """Account and optional position facts at one explicit supervisor cycle."""

    session: SessionSnapshot
    position_key: Optional[str] = None
    signed_quantity: int = 0
    direction: Optional[str] = None
    mark_price: Optional[float] = None
    mark_time: Optional[datetime] = None
    hard_stop_price: Optional[float] = None
    daily_loss_latched: bool = False
    protection_failed: bool = False
    broker_state_known: bool = True
    operator_close_requested: bool = False
    operator_emergency_requested: bool = False


@dataclass(frozen=True)
class HardRiskDecision:
    action: HardRiskAction
    primary_reason_code: HardRiskReason
    contributing_reason_codes: Tuple[HardRiskReason, ...] = field(default_factory=tuple)
    policy_version: str = "hard-risk-v1"


def _finite_positive(value: Optional[float]) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def is_fresh_mark(snapshot: HardRiskSnapshot, policy: HardRiskPolicy) -> bool:
    if not _finite_positive(snapshot.mark_price):
        return False
    try:
        mark_time = as_utc(snapshot.mark_time)
        if mark_time is None:
            return False
        age = (snapshot.session.observed_at - mark_time).total_seconds()
        return -5 <= age <= policy.mark_max_age_seconds
    except (TypeError, ValueError, OverflowError):
        return False


def evaluate_hard_risk(
    snapshot: HardRiskSnapshot, policy: HardRiskPolicy
) -> HardRiskDecision:
    """Return the strongest deterministic hard-risk obligation.

    Precedence intentionally mirrors EXIT_REASON_CODES.  A known flat account
    still receives account-level daily/session obligations so pending entries
    can be cancelled; a position-specific stop only applies to known exposure.
    """

    causes = []
    if snapshot.operator_emergency_requested:
        causes.append(HardRiskReason.OPERATOR_EMERGENCY_FLATTEN)
    if snapshot.daily_loss_latched:
        causes.append(HardRiskReason.RISK_DAILY_LOSS)
    if (
        snapshot.signed_quantity != 0
        and snapshot.broker_state_known
        and is_fresh_mark(snapshot, policy)
        and _finite_positive(snapshot.hard_stop_price)
        and (
            (snapshot.signed_quantity > 0 and snapshot.direction == "BUY")
            or (snapshot.signed_quantity < 0 and snapshot.direction == "SELL")
        )
    ):
        breached = (
            snapshot.direction == "BUY"
            and snapshot.mark_price <= snapshot.hard_stop_price
        ) or (
            snapshot.direction == "SELL"
            and snapshot.mark_price >= snapshot.hard_stop_price
        )
        if breached:
            causes.append(HardRiskReason.RISK_CATASTROPHIC_STOP)

    if snapshot.session.forced_flatten_due:
        causes.append(HardRiskReason.SESSION_FORCED_FLAT)
    if (
        snapshot.signed_quantity != 0
        and policy.require_protection
        and snapshot.protection_failed
    ):
        causes.append(HardRiskReason.RISK_PROTECTION_FAILURE)
    if snapshot.operator_close_requested:
        causes.append(HardRiskReason.OPERATOR_POSITION_CLOSE)
    if not snapshot.broker_state_known:
        causes.append(HardRiskReason.EXEC_BROKER_STATE_UNKNOWN)

    if causes:
        account_causes = {
            HardRiskReason.OPERATOR_EMERGENCY_FLATTEN,
            HardRiskReason.RISK_DAILY_LOSS,
            HardRiskReason.SESSION_FORCED_FLAT,
        }
        if account_causes.intersection(causes):
            action = HardRiskAction.FLATTEN_ACCOUNT
        elif not snapshot.broker_state_known:
            action = HardRiskAction.RECONCILE_REQUIRED
        else:
            action = HardRiskAction.EXIT_POSITION
        return HardRiskDecision(
            action=action,
            primary_reason_code=causes[0],
            contributing_reason_codes=tuple(causes[1:]),
            policy_version=policy.policy_version,
        )

    return HardRiskDecision(
        action=HardRiskAction.HOLD,
        primary_reason_code=HardRiskReason.HOLD_NO_HARD_RISK,
        policy_version=policy.policy_version,
    )
