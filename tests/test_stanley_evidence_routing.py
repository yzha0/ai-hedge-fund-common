import sys
import types
from types import SimpleNamespace

import pandas as pd


langchain_core = types.ModuleType("langchain_core")
langchain_core_messages = types.ModuleType("langchain_core.messages")
langchain_core_prompts = types.ModuleType("langchain_core.prompts")
src_utils_llm = types.ModuleType("src.utils.llm")


class _Message:
    def __init__(self, content: str = "", name: str | None = None):
        self.content = content
        self.name = name


class _Prompt:
    def __init__(self, messages):
        self.messages = messages

    @classmethod
    def from_messages(cls, messages):
        return cls(messages)

    def invoke(self, values):
        return {"messages": self.messages, "values": values}


langchain_core_messages.BaseMessage = _Message
langchain_core_messages.HumanMessage = _Message
langchain_core_prompts.ChatPromptTemplate = _Prompt
src_utils_llm.call_llm = lambda *args, **kwargs: None
sys.modules.setdefault("langchain_core", langchain_core)
sys.modules.setdefault("langchain_core.messages", langchain_core_messages)
sys.modules.setdefault("langchain_core.prompts", langchain_core_prompts)
sys.modules.setdefault("src.utils.llm", src_utils_llm)

from src.agents.central_risk import _extract_evidence_risk_flags
from src.agents.investors.manager_proposal import (
    ManagerProposalOutput,
    ManagerTickerProposal,
    manager_proposal_agent,
)
from src.agents.researchers.growth_agent import analyze_multi_period_growth
from src.agents.researchers.news_sentiment import analyze_headline_risk_flags
from src.agents.researchers.sentiment import analyze_insider_trade_metrics
from src.agents.researchers.technicals import calculate_momentum_signals
from src.agents.researchers.valuation import calculate_simple_multiples


def test_research_helpers_expose_stanley_style_quant_evidence():
    growth = analyze_multi_period_growth(
        [
            SimpleNamespace(revenue=200, earnings_per_share=8, free_cash_flow=50),
            SimpleNamespace(revenue=100, earnings_per_share=4, free_cash_flow=25),
        ]
    )
    assert growth["metrics"]["revenue_cagr"] == 1.0
    assert growth["metrics"]["eps_cagr"] == 1.0
    assert growth["metrics"]["free_cash_flow_cagr"] == 1.0

    prices = pd.DataFrame(
        {
            "close": [100 + i for i in range(130)],
            "volume": [1_000_000 + i for i in range(130)],
        }
    )
    momentum = calculate_momentum_signals(prices)
    assert momentum["metrics"]["period_return"] == 1.29

    insider = analyze_insider_trade_metrics(
        [
            SimpleNamespace(transaction_shares=10, transaction_value=1000),
            SimpleNamespace(transaction_shares=-5, transaction_value=-250),
        ]
    )
    assert insider["buy_count"] == 1
    assert insider["sell_count"] == 1
    assert insider["buy_value"] == 1000
    assert insider["sell_value"] == 250

    headline_flags = analyze_headline_risk_flags(
        [
            SimpleNamespace(title="Company faces fraud investigation"),
            SimpleNamespace(title="Company announces product expansion"),
        ]
    )
    assert headline_flags["metrics"]["flagged_headline_count"] == 1
    assert "fraud" in headline_flags["metrics"]["matched_keywords"]

    multiples = calculate_simple_multiples(
        metrics=SimpleNamespace(
            enterprise_value=1200,
            price_to_earnings_ratio=20,
            enterprise_value_to_ebitda_ratio=12,
        ),
        line_item=SimpleNamespace(
            net_income=50,
            free_cash_flow=40,
            ebit=100,
            ebitda=100,
            total_debt=300,
            cash_and_equivalents=100,
        ),
        market_cap=1000,
    )
    assert multiples == {
        "price_to_earnings_ratio": 20,
        "price_to_free_cash_flow": 25.0,
        "enterprise_value_to_ebit": 12.0,
        "enterprise_value_to_ebitda": 12,
    }


def test_central_risk_extracts_evidence_haircut_reasons():
    research_summary = {
        "AAPL": {
            "factor_panel": {
                "fundamentals": {
                    "raw_evidence": [
                        {
                            "evidence": {
                                "components": {
                                    "financial_health_signal": {
                                        "metrics": {
                                            "debt_to_equity": 2.5,
                                            "current_ratio": 0.8,
                                        }
                                    }
                                }
                            }
                        }
                    ]
                },
                "valuation": {
                    "raw_evidence": [
                        {
                            "evidence": {
                                "metrics": {
                                    "weighted_gap": -0.30,
                                    "market_cap": 1000,
                                },
                                "components": {
                                    "dcf_scenarios": {
                                        "downside": 500,
                                    }
                                },
                            }
                        }
                    ]
                },
            }
        }
    }

    flags = _extract_evidence_risk_flags(research_summary, "AAPL")

    assert flags["risk_adjustment"] < 1.0
    assert "Very high leverage in research evidence" in flags["reasons"]
    assert "Weak liquidity in research evidence" in flags["reasons"]
    assert "Poor valuation support in research evidence" in flags["reasons"]
    assert "Weak valuation downside case in research evidence" in flags["reasons"]


def test_centralized_stanley_manager_uses_synthesized_research_only(monkeypatch):
    captured = {}

    def fake_call_llm(prompt, pydantic_model, agent_name, state, default_factory):
        captured["prompt"] = prompt
        captured["agent_name"] = agent_name
        return ManagerProposalOutput(
            proposals={
                "AAPL": ManagerTickerProposal(
                    signal="bullish",
                    conviction=80,
                    desired_weight_pct=8.0,
                    holding_period_days=20,
                    thesis="Momentum and news evidence align",
                    risk_notes=["Watch valuation downside"],
                )
            }
        )

    monkeypatch.setattr("src.agents.investors.manager_proposal.call_llm", fake_call_llm)

    state = {
        "messages": [],
        "data": {
            "tickers": ["AAPL"],
            "analyst_signals": {},
            "research_summary": {
                "AAPL": {
                    "composite_signal": "bullish",
                    "composite_score": 0.5,
                    "composite_confidence": 80,
                    "disagreement": 0.1,
                    "style_views": {"macro": {"signal": "bullish", "score": 0.7, "confidence": 85}},
                    "factor_panel": {"technical": {"score": 0.9, "raw_evidence": []}},
                }
            },
        },
        "metadata": {"show_reasoning": False},
    }

    result = manager_proposal_agent(state, agent_id="stanley_druckenmiller_agent")

    proposal = result["data"]["manager_proposals"]["stanley_druckenmiller_agent"]["AAPL"]
    assert captured["agent_name"] == "stanley_druckenmiller_agent"
    assert proposal["style"] == "macro"
    assert proposal["holding_period_days"] == 20
    assert "factor_panel" in captured["prompt"]["values"]["research_packets"]
