"""LLM advisory layer for strategy selection (issue #62, Phase 2).

The deterministic regime classifier (Phase 1) remains authoritative and is the
source of truth for the fallback. This layer is *advisory only*: given the same
market context, the deterministic recommendation, and the per-strategy
historical expectancy from the journal, an LLM may confirm or nudge the enabled
set and supply a richer rationale.

Hard guarantees:

- The LLM can only ever choose from the **permitted** ids handed to it; an
  unknown or unpermitted id is dropped, and a response that resolves to an empty
  set is rejected.
- On *any* problem — no API key, network/API error, malformed output, invalid
  ids — we fall back to the deterministic decision unchanged. The advisory layer
  can never make the selection worse than Phase 1.

``build_prompt`` and ``parse_response`` are pure so they can be unit-tested
without a network; ``advise`` is the thin orchestrator.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

from .strategy_selector import STRATEGY_FAMILIES


def _family_of(strategy_id: str) -> str:
    for family, ids in STRATEGY_FAMILIES.items():
        if strategy_id in ids:
            return family
    return "unknown"


def build_prompt(
    context: dict,
    deterministic: dict,
    available_ids: List[str],
    expectancy: Optional[List[dict]] = None,
    news: Optional[List[str]] = None,
) -> str:
    """Build the advisory prompt. Pure — no I/O."""
    expectancy = expectancy or []
    exp_by_strategy = {
        row.get("strategy"): row for row in expectancy if row.get("strategy")
    }

    lines = []
    for sid in available_ids:
        row = exp_by_strategy.get(sid)
        if row:
            hist = (
                f"trades={row.get('total_trades', 0)}, "
                f"win%={row.get('win_rate_pct', 0)}, "
                f"profit_factor={row.get('profit_factor', 0)}, "
                f"avg_R={row.get('avg_r_multiple', 0)}"
            )
        else:
            hist = "no history yet"
        lines.append(f"- {sid} (family: {_family_of(sid)}) — {hist}")
    strategy_block = "\n".join(lines)

    # Optional, best-effort market news. Absent by design when unavailable.
    news_block = ""
    if news:
        headlines = "\n".join(f"- {h}" for h in news)
        news_block = (
            "\n\nMarket news headlines for today (best-effort, may be incomplete "
            "or noisy — weigh accordingly):\n" + headlines
        )

    return f"""You are a trading-strategy selector for an intraday equities agent.
A deterministic classifier has already assessed today's market regime. Your job
is to CONFIRM or make small, well-justified ADJUSTMENTS to which strategies to
enable for the session, using the historical edge of each strategy.

Today's market context (first minutes of the session):
- regime: {deterministic.get("regime")}
- net move %: {context.get("net_move_pct")}
- avg abs move %: {context.get("avg_abs_move_pct")}
- breadth (% names up): {context.get("breadth_up_pct")}

Deterministic recommendation:
- enable: {deterministic.get("enabled")}
- disable: {deterministic.get("disabled")}

Strategies you may choose from (with historical performance):
{strategy_block}{news_block}

Rules:
- You may ONLY enable strategies from the list above. Do not invent ids.
- Enable at least one strategy.
- Prefer the deterministic recommendation; deviate only when a strategy's
  historical edge clearly justifies it for this regime.
- Respond with ONLY a JSON object, no prose, in exactly this shape:
  {{"enabled": ["id1", "id2"], "rationale": "one or two sentences"}}
"""


def parse_response(text: str, available_ids: List[str]) -> Dict[str, object]:
    """Parse and validate the LLM response. Pure.

    Returns ``{"enabled": [...], "rationale": str}`` on success. Raises
    ``ValueError`` if the text has no JSON object, the ``enabled`` field is not a
    non-empty list, or it resolves to no permitted ids.
    """
    if not text:
        raise ValueError("empty response")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in response")
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from e

    enabled = obj.get("enabled")
    if not isinstance(enabled, list) or not enabled:
        raise ValueError("'enabled' must be a non-empty list")

    permitted = set(available_ids)
    # Drop anything not permitted; preserve order and de-dupe.
    validated = [
        sid
        for sid in dict.fromkeys(enabled)
        if isinstance(sid, str) and sid in permitted
    ]
    if not validated:
        raise ValueError("no permitted strategy ids in response")

    rationale = obj.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        rationale = "LLM advisory selection."
    return {"enabled": validated, "rationale": rationale.strip()}


def advise(
    context: dict,
    deterministic: dict,
    available_ids: List[str],
    generate_fn: Callable[[str], str],
    expectancy: Optional[List[dict]] = None,
    news: Optional[List[str]] = None,
) -> Dict[str, object]:
    """Ask the LLM to confirm/adjust the deterministic decision.

    ``generate_fn`` takes the prompt and returns the raw model text (injected so
    this is testable and provider-agnostic). ``news`` is optional best-effort
    market-news context. Returns a decision record in the same shape as the
    deterministic one. On any failure the deterministic decision is returned
    unchanged (with an ``llm_error`` note for the audit).
    """
    # Nothing to advise on if the deterministic layer made no applicable call.
    if not deterministic.get("applied"):
        return deterministic

    try:
        prompt = build_prompt(context, deterministic, available_ids, expectancy, news)
        raw = generate_fn(prompt)
        parsed = parse_response(raw, available_ids)
    except Exception as e:
        fallback = dict(deterministic)
        fallback["llm_error"] = str(e)
        return fallback

    enabled = parsed["enabled"]
    enabled_set = set(enabled)
    disabled = [sid for sid in available_ids if sid not in enabled_set]

    result = dict(deterministic)
    result["source"] = "llm_advisory"
    result["enabled"] = enabled
    result["disabled"] = disabled
    # Preserve the deterministic recommendation for auditability.
    result["deterministic"] = {
        "enabled": deterministic.get("enabled"),
        "disabled": deterministic.get("disabled"),
        "rationale": deterministic.get("rationale"),
    }
    result["rationale"] = f"[LLM advisory] {parsed['rationale']}"
    return result
