from __future__ import annotations

from typing import Iterable

from src.utils.agent_ids import get_agent_key


FLAT_CURRENT_ARCHITECTURE = "flat_current_v1"
MANAGER_SLEEVES_ARCHITECTURE = "manager_sleeves_v1"

ARCHITECTURE_MODES = (
    FLAT_CURRENT_ARCHITECTURE,
    MANAGER_SLEEVES_ARCHITECTURE,
)

RESEARCH_ANALYST_KEYS = (
    "growth_analyst",
    "technical_analyst",
    "fundamentals_analyst",
    "sentiment_analyst",
    "valuation_analyst",
    "news_sentiment_analyst",
)

MANAGER_AGENT_KEYS = (
    "warren_buffett",
    "ben_graham",
    "charlie_munger",
    "mohnish_pabrai",
    "aswath_damodaran",
    "cathie_wood",
    "peter_lynch",
    "phil_fisher",
    "rakesh_jhunjhunwala",
    "stanley_druckenmiller",
    "michael_burry",
    "bill_ackman",
    "nassim_taleb",
)

RESEARCH_ANALYST_TO_FACTOR = {
    "growth_analyst": "growth",
    "technical_analyst": "technical",
    "fundamentals_analyst": "fundamentals",
    "sentiment_analyst": "sentiment",
    "valuation_analyst": "valuation",
    "news_sentiment_analyst": "news_sentiment",
}

MANAGER_STYLE_BY_KEY = {
    "warren_buffett": "value",
    "ben_graham": "value",
    "charlie_munger": "value",
    "mohnish_pabrai": "value",
    "aswath_damodaran": "value",
    "cathie_wood": "growth",
    "peter_lynch": "growth",
    "phil_fisher": "growth",
    "rakesh_jhunjhunwala": "growth",
    "stanley_druckenmiller": "macro",
    "michael_burry": "contrarian",
    "bill_ackman": "contrarian",
    "nassim_taleb": "contrarian",
}

STYLE_DEFAULT_HORIZONS = {
    "value": 90,
    "growth": 60,
    "macro": 20,
    "contrarian": 30,
}

STYLE_PRIOR_WEIGHTS = {
    "value": 0.35,
    "growth": 0.30,
    "macro": 0.20,
    "contrarian": 0.15,
}

STYLE_FACTOR_WEIGHTS = {
    "value": {
        "valuation": 0.35,
        "fundamentals": 0.30,
        "growth": 0.10,
        "technical": 0.05,
        "sentiment": 0.10,
        "news_sentiment": 0.10,
    },
    "growth": {
        "growth": 0.35,
        "fundamentals": 0.20,
        "technical": 0.15,
        "sentiment": 0.10,
        "valuation": 0.10,
        "news_sentiment": 0.10,
    },
    "macro": {
        "technical": 0.30,
        "sentiment": 0.20,
        "news_sentiment": 0.25,
        "fundamentals": 0.10,
        "valuation": 0.05,
        "growth": 0.10,
    },
    "contrarian": {
        "valuation": 0.25,
        "sentiment": 0.20,
        "news_sentiment": 0.20,
        "fundamentals": 0.15,
        "technical": 0.10,
        "growth": 0.10,
    },
}


def is_research_analyst(identifier: str) -> bool:
    return get_agent_key(identifier) in RESEARCH_ANALYST_KEYS


def is_manager_agent(identifier: str) -> bool:
    return get_agent_key(identifier) in MANAGER_AGENT_KEYS


def get_selected_agent_groups(selected_analysts: Iterable[str] | None) -> tuple[list[str], list[str]]:
    selected = list(selected_analysts or [])
    research = [key for key in selected if key in RESEARCH_ANALYST_KEYS]
    managers = [key for key in selected if key in MANAGER_AGENT_KEYS]

    if not selected:
        research = list(RESEARCH_ANALYST_KEYS)
        managers = list(MANAGER_AGENT_KEYS)
    else:
        if not research:
            research = list(RESEARCH_ANALYST_KEYS)
        if not managers:
            managers = list(MANAGER_AGENT_KEYS)

    return research, managers


def create_default_attribution_state() -> dict:
    return {
        "analyst_weights": {key: 1.0 for key in RESEARCH_ANALYST_KEYS},
        "pm_weights": {key: 1.0 for key in MANAGER_AGENT_KEYS},
        "analyst_scorecards": {},
        "pm_scorecards": {},
        "last_updated": None,
    }
