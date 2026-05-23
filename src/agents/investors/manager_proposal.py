from __future__ import annotations

import json

from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from typing_extensions import Literal

from src.graph.state import AgentState, show_agent_reasoning
from src.utils.agent_ids import get_agent_key
from src.utils.analysts import ANALYST_CONFIG
from src.utils.architecture import (
    MANAGER_STYLE_BY_KEY,
    STYLE_DEFAULT_HORIZONS,
    create_default_attribution_state,
)
from src.utils.llm import call_llm
from src.utils.progress import progress


class ManagerTickerProposal(BaseModel):
    signal: Literal["bullish", "bearish", "neutral"]
    conviction: int = Field(description="Conviction from 0 to 100")
    desired_weight_pct: float = Field(description="Desired absolute portfolio weight percentage")
    holding_period_days: int = Field(description="Expected holding period in days")
    thesis: str = Field(description="Concise thesis")
    risk_notes: list[str] = Field(description="Concise risk notes")


class ManagerProposalOutput(BaseModel):
    proposals: dict[str, ManagerTickerProposal]


def _manager_directives(manager_key: str) -> str:
    if manager_key == "stanley_druckenmiller":
        return (
            "Emphasize momentum, news/sentiment, growth evidence, asymmetric payoff, "
            "and shorter holding periods. Be decisive only when evidence aligns; "
            "otherwise stay neutral."
        )
    return "Use the persona style description and synthesized evidence only."


def manager_proposal_agent(state: AgentState, agent_id: str):
    """Investor-style PM node that reads synthesized research and proposes exposures."""
    data = state["data"]
    tickers = data["tickers"]
    research_summary = data.get("research_summary", {})
    attribution_state = data.setdefault("attribution_state", create_default_attribution_state())

    manager_key = get_agent_key(agent_id)
    manager_config = ANALYST_CONFIG[manager_key]
    style = MANAGER_STYLE_BY_KEY[manager_key]
    default_horizon = STYLE_DEFAULT_HORIZONS[style]
    pm_weight = float(attribution_state.setdefault("pm_weights", {}).get(manager_key, 1.0))
    manager_directives = _manager_directives(manager_key)

    progress.update_status(agent_id, None, "Generating manager proposals")

    template = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are acting as a hedge fund portfolio manager persona.\n"
                "You receive synthesized research, not raw data.\n"
                "Propose one stance per ticker. desired_weight_pct must be between 0 and 12.\n"
                "If evidence is weak or mixed, choose neutral and 0 weight.\n"
                "Keep thesis concise (max 300 chars). Return JSON only.",
            ),
            (
                "human",
                "Manager: {manager_name}\n"
                "Style: {style}\n"
                "Style description: {style_description}\n"
                "Manager directives: {manager_directives}\n"
                "Default horizon days: {default_horizon}\n"
                "Current PM weight: {pm_weight}\n\n"
                "Research packets:\n{research_packets}\n\n"
                "Format:\n"
                "{{\n"
                '  "proposals": {{\n'
                '    "TICKER": {{"signal":"bullish|bearish|neutral","conviction":0,"desired_weight_pct":0.0,"holding_period_days":0,"thesis":"","risk_notes":[]}}\n'
                "  }}\n"
                "}}"
            ),
        ]
    )

    prompt = template.invoke(
        {
            "manager_name": manager_config["display_name"],
            "style": style,
            "style_description": manager_config["investing_style"],
            "manager_directives": manager_directives,
            "default_horizon": default_horizon,
            "pm_weight": f"{pm_weight:.2f}",
            "research_packets": json.dumps(
                {
                    ticker: {
                        "composite_signal": research_summary.get(ticker, {}).get("composite_signal"),
                        "composite_score": research_summary.get(ticker, {}).get("composite_score"),
                        "composite_confidence": research_summary.get(ticker, {}).get("composite_confidence"),
                        "disagreement": research_summary.get(ticker, {}).get("disagreement"),
                        "style_view": research_summary.get(ticker, {}).get("style_views", {}).get(style, {}),
                        "factor_panel": research_summary.get(ticker, {}).get("factor_panel", {}),
                    }
                    for ticker in tickers
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
    )

    def create_default_output() -> ManagerProposalOutput:
        return ManagerProposalOutput(
            proposals={
                ticker: ManagerTickerProposal(
                    signal="neutral",
                    conviction=0,
                    desired_weight_pct=0.0,
                    holding_period_days=default_horizon,
                    thesis="Default neutral stance",
                    risk_notes=["No proposal generated"],
                )
                for ticker in tickers
            }
        )

    llm_out = call_llm(
        prompt=prompt,
        pydantic_model=ManagerProposalOutput,
        agent_name=agent_id,
        state=state,
        default_factory=create_default_output,
    )

    proposals: dict[str, dict] = {}
    analyst_signal_view: dict[str, dict] = {}
    for ticker in tickers:
        raw = llm_out.proposals.get(
            ticker,
            ManagerTickerProposal(
                signal="neutral",
                conviction=0,
                desired_weight_pct=0.0,
                holding_period_days=default_horizon,
                thesis="Missing proposal",
                risk_notes=["Proposal missing from response"],
            ),
        )
        desired_weight_pct = max(0.0, min(12.0, float(raw.desired_weight_pct)))
        holding_period_days = max(1, int(raw.holding_period_days or default_horizon))
        conviction = max(0, min(100, int(raw.conviction)))
        signal = raw.signal
        if signal == "neutral":
            desired_weight_pct = 0.0

        proposal = {
            "style": style,
            "signal": signal,
            "conviction": conviction,
            "desired_weight_pct": desired_weight_pct,
            "holding_period_days": holding_period_days,
            "thesis": raw.thesis,
            "risk_notes": raw.risk_notes[:3],
        }
        proposals[ticker] = proposal
        analyst_signal_view[ticker] = {
            "signal": signal,
            "confidence": conviction,
            "reasoning": proposal,
        }

    data.setdefault("manager_proposals", {})[agent_id] = proposals
    data.setdefault("workflow_outputs", {})[agent_id] = proposals
    data["analyst_signals"][agent_id] = analyst_signal_view

    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(proposals, manager_config["display_name"])

    progress.update_status(agent_id, None, "Done")
    message = HumanMessage(content=json.dumps(proposals), name=agent_id)
    return {"messages": [message], "data": data}
