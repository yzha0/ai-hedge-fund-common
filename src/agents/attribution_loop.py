from __future__ import annotations

import json

from langchain_core.messages import HumanMessage

from src.graph.state import AgentState, show_agent_reasoning
from src.utils.agent_ids import get_agent_key
from src.utils.architecture import (
    MANAGER_AGENT_KEYS,
    RESEARCH_ANALYST_KEYS,
    create_default_attribution_state,
)
from src.utils.progress import progress


def attribution_loop_agent(state: AgentState, agent_id: str = "attribution_loop_agent"):
    """Persist lightweight scorecard metadata for future attribution updates."""
    data = state["data"]
    analyst_signals = data.get("analyst_signals", {})
    manager_proposals = data.get("manager_proposals", {})
    attribution_state = data.setdefault("attribution_state", create_default_attribution_state())

    attribution_state["last_updated"] = data.get("end_date")
    attribution_state["analyst_scorecards"] = {
        key: {
            "participated": any(get_agent_key(agent_id_key) == key for agent_id_key in analyst_signals),
            "status": "pending_future_return_update",
        }
        for key in RESEARCH_ANALYST_KEYS
    }
    attribution_state["pm_scorecards"] = {
        key: {
            "participated": any(get_agent_key(agent_id_key) == key for agent_id_key in manager_proposals),
            "status": "pending_future_return_update",
        }
        for key in MANAGER_AGENT_KEYS
    }

    data["attribution_state"] = attribution_state
    data.setdefault("workflow_outputs", {})[agent_id] = attribution_state

    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(attribution_state, "Attribution Loop")

    progress.update_status(agent_id, None, "Done")
    message = HumanMessage(content=json.dumps(attribution_state), name=agent_id)
    return {"messages": [message], "data": data}
