from __future__ import annotations

import json
import statistics

from langchain_core.messages import HumanMessage

from src.graph.state import AgentState, show_agent_reasoning
from src.utils.agent_ids import get_agent_key
from src.utils.architecture import (
    RESEARCH_ANALYST_KEYS,
    RESEARCH_ANALYST_TO_FACTOR,
    STYLE_FACTOR_WEIGHTS,
    create_default_attribution_state,
)
from src.utils.progress import progress


def _signal_to_score(signal: str | None) -> float:
    normalized = (signal or "").lower()
    if normalized == "bullish":
        return 1.0
    if normalized == "bearish":
        return -1.0
    return 0.0


def _score_to_signal(score: float, threshold: float = 0.2) -> str:
    if score >= threshold:
        return "bullish"
    if score <= -threshold:
        return "bearish"
    return "neutral"


def research_synthesizer_agent(state: AgentState, agent_id: str = "research_synthesizer_agent"):
    """Normalize research analyst outputs into a shared packet for manager agents."""
    data = state["data"]
    tickers = data["tickers"]
    analyst_signals = data["analyst_signals"]
    attribution_state = data.setdefault("attribution_state", create_default_attribution_state())
    analyst_weights = attribution_state.setdefault("analyst_weights", {})

    research_summary: dict[str, dict] = {}
    synthesizer_signals: dict[str, dict] = {}

    for ticker in tickers:
        progress.update_status(agent_id, ticker, "Synthesizing research signals")

        factor_panel: dict[str, dict] = {}
        raw_evidence_panel: dict[str, list[dict]] = {}
        factor_scores: list[float] = []
        factor_confidences: list[float] = []
        bullish_agents: list[str] = []
        bearish_agents: list[str] = []
        neutral_agents: list[str] = []

        for research_key in RESEARCH_ANALYST_KEYS:
            factor_name = RESEARCH_ANALYST_TO_FACTOR[research_key]
            entries = []
            for source_agent_id, signals in analyst_signals.items():
                if get_agent_key(source_agent_id) != research_key or ticker not in signals:
                    continue
                payload = signals[ticker]
                score = _signal_to_score(payload.get("signal"))
                confidence = float(payload.get("confidence", 0.0) or 0.0)
                weight = float(
                    analyst_weights.get(source_agent_id, analyst_weights.get(research_key, 1.0))
                )
                raw_evidence = payload.get("raw_evidence") or {
                    "schema_version": "legacy_reasoning_v1",
                    "factor": factor_name,
                    "signal": payload.get("signal"),
                    "confidence": confidence,
                    "reasoning": payload.get("reasoning"),
                }
                entries.append(
                    {
                        "agent_id": source_agent_id,
                        "score": score,
                        "confidence": confidence,
                        "weight": weight,
                        "reasoning": payload.get("reasoning"),
                        "raw_evidence": raw_evidence,
                    }
                )

            if entries:
                weighted_sum = sum(item["score"] * item["confidence"] * item["weight"] for item in entries)
                weight_total = sum(item["confidence"] * item["weight"] for item in entries)
                factor_score = weighted_sum / weight_total if weight_total else 0.0
                factor_confidence = sum(item["confidence"] for item in entries) / len(entries)
                source_ids = [item["agent_id"] for item in entries]
                raw_evidence_panel[factor_name] = [
                    {
                        "agent_id": item["agent_id"],
                        "weight": item["weight"],
                        "evidence": item["raw_evidence"],
                    }
                    for item in entries
                ]
                for item in entries:
                    if item["score"] > 0:
                        bullish_agents.append(item["agent_id"])
                    elif item["score"] < 0:
                        bearish_agents.append(item["agent_id"])
                    else:
                        neutral_agents.append(item["agent_id"])
            else:
                factor_score = 0.0
                factor_confidence = 0.0
                source_ids = []
                raw_evidence_panel[factor_name] = []

            factor_panel[factor_name] = {
                "signal": _score_to_signal(factor_score),
                "score": round(factor_score, 4),
                "confidence": round(factor_confidence, 2),
                "source_agents": source_ids,
                "raw_evidence": raw_evidence_panel[factor_name],
            }
            factor_scores.append(factor_score)
            factor_confidences.append(factor_confidence)

        composite_score = sum(factor_scores) / len(factor_scores) if factor_scores else 0.0
        composite_confidence = (
            sum(factor_confidences) / len(factor_confidences) if factor_confidences else 0.0
        )
        disagreement = statistics.pstdev(factor_scores) if len(factor_scores) > 1 else 0.0

        style_views: dict[str, dict] = {}
        for style_name, factor_weights in STYLE_FACTOR_WEIGHTS.items():
            style_score = sum(
                factor_panel.get(factor_name, {}).get("score", 0.0) * factor_weight
                for factor_name, factor_weight in factor_weights.items()
            )
            style_confidence = sum(
                factor_panel.get(factor_name, {}).get("confidence", 0.0) * factor_weight
                for factor_name, factor_weight in factor_weights.items()
            )
            style_views[style_name] = {
                "signal": _score_to_signal(style_score),
                "score": round(style_score, 4),
                "confidence": round(style_confidence, 2),
            }

        summary = {
            "factor_panel": factor_panel,
            "composite_signal": _score_to_signal(composite_score),
            "composite_score": round(composite_score, 4),
            "composite_confidence": round(composite_confidence, 2),
            "disagreement": round(disagreement, 4),
            "bullish_agents": sorted(set(bullish_agents)),
            "bearish_agents": sorted(set(bearish_agents)),
            "neutral_agents": sorted(set(neutral_agents)),
            "style_views": style_views,
           # "raw_evidence": raw_evidence_panel,
        }
        research_summary[ticker] = summary
        synthesizer_signals[ticker] = {
            "signal": summary["composite_signal"],
            "confidence": summary["composite_confidence"],
            "reasoning": summary,
        }

    data["research_summary"] = research_summary
    data.setdefault("workflow_outputs", {})[agent_id] = research_summary
    data["analyst_signals"][agent_id] = synthesizer_signals

    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(research_summary, "Research Synthesizer")

    progress.update_status(agent_id, None, "Done")
    message = HumanMessage(content=json.dumps(research_summary), name=agent_id)
    return {"messages": [message], "data": data}
