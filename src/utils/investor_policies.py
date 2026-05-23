from __future__ import annotations

from src.utils.architecture import MANAGER_AGENT_KEYS, RESEARCH_ANALYST_TO_FACTOR


FACTOR_KEYS = tuple(RESEARCH_ANALYST_TO_FACTOR.values())


def _placeholder_factor_weights() -> dict[str, float | None]:
    return {factor: None for factor in FACTOR_KEYS}


INVESTOR_FACTOR_WEIGHTS: dict[str, dict[str, float | None]] = {
    investor_key: _placeholder_factor_weights()
    for investor_key in MANAGER_AGENT_KEYS
}


INVESTOR_POLICIES: dict[str, dict] = {
    investor_key: {
        "factor_weights": INVESTOR_FACTOR_WEIGHTS[investor_key],
        "gates": {},
        "notes": "Placeholder only. Fill with researched investor-specific weights before wiring into manager proposals.",
    }
    for investor_key in MANAGER_AGENT_KEYS
}
