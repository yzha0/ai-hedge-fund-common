from __future__ import annotations

import json

from langchain_core.messages import HumanMessage

from src.graph.state import AgentState, show_agent_reasoning
from src.utils.agent_ids import get_agent_key
from src.utils.architecture import (
    MANAGER_STYLE_BY_KEY,
    STYLE_PRIOR_WEIGHTS,
    create_default_attribution_state,
)
from src.utils.progress import progress


def _signal_to_sign(signal: str | None) -> int:
    normalized = (signal or "").lower()
    if normalized == "bullish":
        return 1
    if normalized == "bearish":
        return -1
    return 0


def capital_allocator_agent(state: AgentState, agent_id: str = "capital_allocator_agent"):
    """Aggregate manager proposals into final target exposures."""
    data = state["data"]
    tickers = data["tickers"]
    portfolio = data["portfolio"]
    positions = portfolio.get("positions", {})
    current_prices = data.get("current_prices", {})
    central_risk_review = data.get("central_risk_review", {})
    manager_proposals = data.get("manager_proposals", {})
    attribution_state = data.setdefault("attribution_state", create_default_attribution_state())
    pm_weights = attribution_state.setdefault("pm_weights", {})

    portfolio_limits = central_risk_review.get("portfolio_limits", {})
    ticker_limits = central_risk_review.get("ticker_limits", {})
    proposal_review = central_risk_review.get("proposal_review", {})
    equity = float(portfolio_limits.get("equity", float(portfolio.get("cash", 0.0))))
    max_gross = float(portfolio_limits.get("max_gross_exposure", equity * 1.5))
    max_net = float(portfolio_limits.get("max_net_exposure", equity * 0.6))

    current_net_notional: dict[str, float] = {}
    running_gross = 0.0
    running_net = 0.0
    for ticker in tickers:
        pos = positions.get(ticker, {})
        price = float(current_prices.get(ticker, 0.0))
        net_notional = (float(pos.get("long", 0) or 0) - float(pos.get("short", 0) or 0)) * price
        current_net_notional[ticker] = net_notional
        running_gross += abs(net_notional)
        running_net += net_notional

    raw_allocations: dict[str, dict] = {}
    pm_budget_scores: dict[str, float] = {}
    for ticker in tickers:
        entries = []
        for pm_id, proposals in manager_proposals.items():
            proposal = proposals.get(ticker, {})
            review = proposal_review.get(pm_id, {}).get(ticker, {})
            if review.get("status") == "block":
                continue

            pm_key = get_agent_key(pm_id)
            signal = str(proposal.get("signal", "neutral")).lower()
            sign = _signal_to_sign(signal)
            if sign == 0:
                continue

            approved_weight_pct = float(review.get("approved_weight_pct", 0.0) or 0.0)
            conviction = float(proposal.get("conviction", 0.0) or 0.0) / 100.0
            risk_adjustment = float(review.get("risk_adjustment", 1.0) or 1.0)
            style = MANAGER_STYLE_BY_KEY[pm_key]
            strength = (
                float(pm_weights.get(pm_key, 1.0))
                * STYLE_PRIOR_WEIGHTS[style]
                * conviction
                * max(risk_adjustment, 0.0)
            )
            if approved_weight_pct <= 0.0 or strength <= 0.0:
                continue

            weighted_signed_weight = sign * approved_weight_pct * strength  # This is the core of the allocator's decision: 
                                                                            # manager proposals are weighted by their 
                                                                            # conviction, style, and any risk adjustments, 
                                                                            # and then aggregated into a candidate weight for the ticker
            entries.append(
                {
                    "pm_id": pm_id,
                    "pm_key": pm_key,
                    "style": style,
                    "signal": signal,
                    "approved_weight_pct": approved_weight_pct,
                    "strength": strength,
                    "weighted_signed_weight": weighted_signed_weight,
                    "conviction": float(proposal.get("conviction", 0.0) or 0.0),
                }
            )
            pm_budget_scores[pm_id] = pm_budget_scores.get(pm_id, 0.0) + abs(weighted_signed_weight)

        if not entries:
            raw_allocations[ticker] = {
                "candidate_weight_pct": 0.0,
                "candidate_notional": 0.0,
                "entries": [],
            }
            continue

        candidate_weight_pct = sum(entry["weighted_signed_weight"] for entry in entries) #aggregate the weighted signals into a candidate weight for the ticker
        max_abs_weight_pct = float(ticker_limits.get(ticker, {}).get("max_abs_weight_pct", 0.0))
        candidate_weight_pct = max(-max_abs_weight_pct, min(max_abs_weight_pct, candidate_weight_pct))# enforce any ticker-level weight limits on the candidate weight
        candidate_notional = equity * candidate_weight_pct / 100.0
        raw_allocations[ticker] = {
            "candidate_weight_pct": candidate_weight_pct,
            "candidate_notional": candidate_notional,
            "entries": entries,
        }

    ticker_order = sorted(
        tickers,
        key=lambda ticker: abs(float(raw_allocations.get(ticker, {}).get("candidate_notional", 0.0))),
        reverse=True,
    )

    ticker_allocations: dict[str, dict] = {}
    simulated_net = dict(current_net_notional)
    for ticker in ticker_order:
        progress.update_status(agent_id, ticker, "Allocating capital")
        allocation = raw_allocations.get(ticker, {})
        candidate_notional = float(allocation.get("candidate_notional", 0.0) or 0.0)
        candidate_weight_pct = float(allocation.get("candidate_weight_pct", 0.0) or 0.0)
        entries = allocation.get("entries", [])
        old_notional = float(simulated_net.get(ticker, 0.0))

        new_notional = candidate_notional
        projected_gross = running_gross - abs(old_notional) + abs(new_notional)
        projected_net = running_net - old_notional + new_notional

        scale = 1.0
        if projected_gross > max_gross and abs(new_notional) > 0:
            allowed_abs = max(0.0, max_gross - (running_gross - abs(old_notional)))
            scale = min(scale, allowed_abs / abs(new_notional))
        projected_net_abs = abs(projected_net)
        if projected_net_abs > max_net and abs(new_notional - old_notional) > 0:
            allowed_net_change = max(0.0, max_net - abs(running_net - old_notional))
            baseline_sign = 1.0 if new_notional >= 0 else -1.0
            allowed_target = baseline_sign * allowed_net_change
            if abs(new_notional) > 0:
                scale = min(scale, abs(allowed_target) / max(abs(new_notional), 1e-9))

        if scale < 1.0:
            new_notional *= scale
            candidate_weight_pct *= scale

        running_gross = running_gross - abs(old_notional) + abs(new_notional)
        running_net = running_net - old_notional + new_notional
        simulated_net[ticker] = new_notional

        price = float(current_prices.get(ticker, 0.0))
        target_net_shares = 0
        if price > 0.0 and abs(new_notional) > 0.0:
            target_net_shares = float(abs(new_notional) // price)
            if new_notional < 0:
                target_net_shares *= -1

        aligned_entries = [
            entry["pm_id"]
            for entry in entries
            if _signal_to_sign(entry["signal"]) == (1 if new_notional > 0 else -1 if new_notional < 0 else 0)
        ]
        avg_conviction = (
            sum(float(entry["conviction"]) for entry in entries) / len(entries) if entries else 0.0
        )
        ticker_allocations[ticker] = {
            "net_signal": "bullish" if new_notional > 0 else "bearish" if new_notional < 0 else "neutral",
            "allocated_notional": round(new_notional, 4),
            "allocated_weight_pct": round(candidate_weight_pct, 4),
            "target_net_shares": float(target_net_shares),
            "winning_pm_ids": aligned_entries,
            "allocator_confidence": round(avg_conviction, 2),
            "reasoning": (
                "Allocator netted manager proposals and clipped to firm constraints"
                if entries
                else "No active manager proposals"
            ),
        }

    for ticker in tickers:
        ticker_allocations.setdefault(
            ticker,
            {
                "net_signal": "neutral",
                "allocated_notional": 0.0,
                "allocated_weight_pct": 0.0,
                "target_net_shares": 0,
                "winning_pm_ids": [],
                "allocator_confidence": 0.0,
                "reasoning": "No allocation generated",
            },
        )

    capital_allocation = {
        "pm_budgets": {pm_id: round(score, 4) for pm_id, score in pm_budget_scores.items()},
        "ticker_allocations": ticker_allocations,
        "cash_reserve": round(float(portfolio.get("cash", 0.0)), 4),
    }

    data["capital_allocation"] = capital_allocation
    data.setdefault("workflow_outputs", {})[agent_id] = capital_allocation

    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(capital_allocation, "Capital Allocator")

    progress.update_status(agent_id, None, "Done")
    message = HumanMessage(content=json.dumps(capital_allocation), name=agent_id)
    return {"messages": [message], "data": data}
