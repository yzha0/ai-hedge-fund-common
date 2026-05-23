from __future__ import annotations

import re


_SUFFIX_RE = re.compile(r"^[a-z0-9]{6}$")
_AGENT_KEY_ALIASES = {
    "news_sentiment": "news_sentiment_analyst",
}


def extract_base_agent_key(unique_id: str) -> str:
    """Strip the generated 6-char suffix from frontend node ids when present."""
    parts = unique_id.split("_")
    if len(parts) >= 2 and _SUFFIX_RE.match(parts[-1]):
        return "_".join(parts[:-1])
    return unique_id


def get_agent_key(agent_id: str) -> str:
    """Normalize an agent identifier to the canonical config key."""
    base = extract_base_agent_key(agent_id)
    if base.endswith("_agent"):
        base = base[:-6]
    return _AGENT_KEY_ALIASES.get(base, base)
