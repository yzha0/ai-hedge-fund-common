import sys
import types


langchain_core = types.ModuleType("langchain_core")
langchain_core_messages = types.ModuleType("langchain_core.messages")


class _Message:
    def __init__(self, content: str = "", name: str | None = None):
        self.content = content
        self.name = name


langchain_core_messages.BaseMessage = _Message
langchain_core_messages.HumanMessage = _Message
sys.modules.setdefault("langchain_core", langchain_core)
sys.modules.setdefault("langchain_core.messages", langchain_core_messages)

from src.agents.research_synthesizer import research_synthesizer_agent
from src.utils.agent_ids import get_agent_key


def test_get_agent_key_maps_legacy_news_sentiment_agent_to_canonical_key():
    assert get_agent_key("news_sentiment_agent") == "news_sentiment_analyst"


def test_research_synthesizer_includes_legacy_news_sentiment_output():
    state = {
        "messages": [],
        "data": {
            "tickers": ["AAPL"],
            "analyst_signals": {
                "news_sentiment_agent": {
                    "AAPL": {
                        "signal": "bullish",
                        "confidence": 80.0,
                        "reasoning": {"news_sentiment": {"metrics": {"total_articles": 10}}},
                        "raw_evidence": {
                            "schema_version": "research_evidence_v1",
                            "factor": "news_sentiment",
                            "signal": "bullish",
                            "confidence": 80.0,
                            "metrics": {"total_articles": 10},
                        },
                    }
                }
            },
        },
        "metadata": {"show_reasoning": False},
    }

    result = research_synthesizer_agent(state)

    panel = result["data"]["research_summary"]["AAPL"]["factor_panel"]["news_sentiment"]
    assert panel["signal"] == "bullish"
    assert panel["score"] == 1.0
    assert panel["confidence"] == 80.0
    assert panel["source_agents"] == ["news_sentiment_agent"]
    assert panel["raw_evidence"]


def test_research_synthesizer_summary_starts_with_composite_fields():
    state = {
        "messages": [],
        "data": {
            "tickers": ["AAPL"],
            "analyst_signals": {
                "fundamentals_analyst_agent": {
                    "AAPL": {
                        "signal": "bearish",
                        "confidence": 60.0,
                        "reasoning": {"profitability_signal": {"signal": "bearish"}},
                    }
                }
            },
        },
        "metadata": {"show_reasoning": False},
    }

    result = research_synthesizer_agent(state)

    summary = result["data"]["research_summary"]["AAPL"]
    assert list(summary.keys())[:4] == [
        "composite_signal",
        "composite_score",
        "composite_confidence",
        "disagreement",
    ]
    assert "factor_panel" in summary


def test_research_synthesizer_passes_expanded_raw_evidence_through_factor_panel():
    state = {
        "messages": [],
        "data": {
            "tickers": ["AAPL"],
            "analyst_signals": {
                "valuation_analyst_agent": {
                    "AAPL": {
                        "signal": "bullish",
                        "confidence": 75.0,
                        "reasoning": {},
                        "raw_evidence": {
                            "schema_version": "research_evidence_v1",
                            "factor": "valuation",
                            "signal": "bullish",
                            "confidence": 75.0,
                            "metrics": {
                                "simple_multiples": {
                                    "price_to_earnings_ratio": 20,
                                    "price_to_free_cash_flow": 25,
                                    "enterprise_value_to_ebit": 12,
                                    "enterprise_value_to_ebitda": 10,
                                }
                            },
                        },
                    }
                }
            },
        },
        "metadata": {"show_reasoning": False},
    }

    result = research_synthesizer_agent(state)

    evidence = result["data"]["research_summary"]["AAPL"]["factor_panel"]["valuation"]["raw_evidence"][0]
    assert evidence["evidence"]["metrics"]["simple_multiples"]["enterprise_value_to_ebit"] == 12
