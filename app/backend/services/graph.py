import asyncio
import json
from collections import defaultdict
from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

from app.backend.services.agent_service import create_agent_function
from src.agents.capital_allocator import capital_allocator_agent
from src.agents.central_risk import central_risk_agent
from src.agents.investors.manager_proposal import manager_proposal_agent
from src.agents.portfolio_manager import portfolio_management_agent
from src.agents.research_synthesizer import research_synthesizer_agent
from src.agents.risk_manager import risk_management_agent
from src.main import start
from src.utils.agent_ids import extract_base_agent_key as _extract_base_agent_key, get_agent_key
from src.utils.analysts import ANALYST_CONFIG
from src.utils.architecture import (
    FLAT_CURRENT_ARCHITECTURE,
    MANAGER_AGENT_KEYS,
    MANAGER_SLEEVES_ARCHITECTURE,
    RESEARCH_ANALYST_KEYS,
)
from src.graph.state import AgentState


def extract_base_agent_key(unique_id: str) -> str:
    """Backwards-compatible wrapper for shared agent-id normalization."""
    return _extract_base_agent_key(unique_id)


def _create_flat_graph(graph: StateGraph, graph_nodes: list, graph_edges: list) -> StateGraph:
    """Create the existing flat workflow based on the React Flow graph structure."""
    analyst_nodes = {key: (f"{key}_agent", config["agent_func"]) for key, config in ANALYST_CONFIG.items()}
    agent_ids = [node.id for node in graph_nodes]
    agent_ids_set = set(agent_ids)
    portfolio_manager_nodes = set()

    for unique_agent_id in agent_ids:
        base_agent_key = get_agent_key(unique_agent_id)
        if base_agent_key == "portfolio_manager":
            portfolio_manager_nodes.add(unique_agent_id)
            continue
        if base_agent_key not in ANALYST_CONFIG:
            continue
        _, node_func = analyst_nodes[base_agent_key]
        graph.add_node(unique_agent_id, create_agent_function(node_func, unique_agent_id))

    risk_manager_nodes = {}
    for portfolio_manager_id in portfolio_manager_nodes:
        graph.add_node(
            portfolio_manager_id,
            create_agent_function(portfolio_management_agent, portfolio_manager_id),
        )
        suffix = portfolio_manager_id.split("_")[-1]
        risk_manager_id = f"risk_management_agent_{suffix}"
        risk_manager_nodes[portfolio_manager_id] = risk_manager_id
        graph.add_node(risk_manager_id, create_agent_function(risk_management_agent, risk_manager_id))

    nodes_with_incoming_edges = set()
    direct_to_portfolio_managers = {}
    for edge in graph_edges:
        if edge.source not in agent_ids_set or edge.target not in agent_ids_set:
            continue

        source_base_key = get_agent_key(edge.source)
        target_base_key = get_agent_key(edge.target)
        nodes_with_incoming_edges.add(edge.target)

        if source_base_key in ANALYST_CONFIG and target_base_key == "portfolio_manager":
            direct_to_portfolio_managers[edge.source] = edge.target
        else:
            graph.add_edge(edge.source, edge.target)

    for agent_id in agent_ids:
        if agent_id not in nodes_with_incoming_edges:
            base_agent_key = get_agent_key(agent_id)
            if base_agent_key in ANALYST_CONFIG and base_agent_key != "portfolio_manager":
                graph.add_edge("start_node", agent_id)

    for analyst_id, portfolio_manager_id in direct_to_portfolio_managers.items():
        graph.add_edge(analyst_id, risk_manager_nodes[portfolio_manager_id])

    for portfolio_manager_id, risk_manager_id in risk_manager_nodes.items():
        graph.add_edge(risk_manager_id, portfolio_manager_id)

    for portfolio_manager_id in portfolio_manager_nodes:
        graph.add_edge(portfolio_manager_id, END)

    return graph


def _create_manager_sleeves_graph(graph: StateGraph, graph_nodes: list, graph_edges: list) -> StateGraph:
    """Create the new research -> manager -> risk -> allocator workflow."""
    graph = StateGraph(AgentState)
    graph.add_node("start_node", start)
    agent_ids = [node.id for node in graph_nodes]
    agent_ids_set = set(agent_ids)
    portfolio_manager_nodes = set()

    present_research_ids: set[str] = set()
    present_manager_ids: set[str] = set()

    for unique_agent_id in agent_ids:
        base_agent_key = get_agent_key(unique_agent_id)
        if base_agent_key == "portfolio_manager":
            portfolio_manager_nodes.add(unique_agent_id)
            continue
        if base_agent_key in RESEARCH_ANALYST_KEYS:
            graph.add_node(
                unique_agent_id,
                create_agent_function(ANALYST_CONFIG[base_agent_key]["agent_func"], unique_agent_id),
            )
            present_research_ids.add(unique_agent_id)
        elif base_agent_key in MANAGER_AGENT_KEYS:
            graph.add_node(unique_agent_id, create_agent_function(manager_proposal_agent, unique_agent_id))
            present_manager_ids.add(unique_agent_id)

    synthesizer_nodes = {}
    central_risk_nodes = {}
    capital_allocator_nodes = {}
    for portfolio_manager_id in portfolio_manager_nodes:
        graph.add_node(
            portfolio_manager_id,
            create_agent_function(portfolio_management_agent, portfolio_manager_id),
        )
        suffix = portfolio_manager_id.split("_")[-1]
        synthesizer_id = f"research_synthesizer_agent_{suffix}"
        central_risk_id = f"central_risk_agent_{suffix}"
        capital_allocator_id = f"capital_allocator_agent_{suffix}"

        synthesizer_nodes[portfolio_manager_id] = synthesizer_id
        central_risk_nodes[portfolio_manager_id] = central_risk_id
        capital_allocator_nodes[portfolio_manager_id] = capital_allocator_id

        graph.add_node(synthesizer_id, create_agent_function(research_synthesizer_agent, synthesizer_id))
        graph.add_node(central_risk_id, create_agent_function(central_risk_agent, central_risk_id))
        graph.add_node(
            capital_allocator_id,
            create_agent_function(capital_allocator_agent, capital_allocator_id),
        )

    research_assignments = defaultdict(set)
    manager_assignments = defaultdict(set)
    for edge in graph_edges:
        if edge.source not in agent_ids_set or edge.target not in agent_ids_set:
            continue
        source_key = get_agent_key(edge.source)
        target_key = get_agent_key(edge.target)
        if target_key != "portfolio_manager":
            continue
        if source_key in RESEARCH_ANALYST_KEYS:
            research_assignments[edge.target].add(edge.source)
        elif source_key in MANAGER_AGENT_KEYS:
            manager_assignments[edge.target].add(edge.source)

    if len(portfolio_manager_nodes) == 1:
        sole_pm_id = next(iter(portfolio_manager_nodes))
        assigned_research = set().union(*research_assignments.values()) if research_assignments else set()
        assigned_managers = set().union(*manager_assignments.values()) if manager_assignments else set()
        for research_id in present_research_ids - assigned_research:
            research_assignments[sole_pm_id].add(research_id)
        for manager_id in present_manager_ids - assigned_managers:
            manager_assignments[sole_pm_id].add(manager_id)

    added_hidden_nodes = set(agent_ids)
    for portfolio_manager_id in portfolio_manager_nodes:
        suffix = portfolio_manager_id.split("_")[-1]
        if not research_assignments[portfolio_manager_id]:
            for research_key in RESEARCH_ANALYST_KEYS:
                hidden_id = f"{research_key}_{suffix}"
                if hidden_id not in added_hidden_nodes:
                    graph.add_node(
                        hidden_id,
                        create_agent_function(ANALYST_CONFIG[research_key]["agent_func"], hidden_id),
                    )
                    added_hidden_nodes.add(hidden_id)
                research_assignments[portfolio_manager_id].add(hidden_id)

        if not manager_assignments[portfolio_manager_id]:
            for manager_key in MANAGER_AGENT_KEYS:
                hidden_id = f"{manager_key}_{suffix}"
                if hidden_id not in added_hidden_nodes:
                    graph.add_node(hidden_id, create_agent_function(manager_proposal_agent, hidden_id))
                    added_hidden_nodes.add(hidden_id)
                manager_assignments[portfolio_manager_id].add(hidden_id)

        for research_id in sorted(research_assignments[portfolio_manager_id]):
            graph.add_edge("start_node", research_id)
            graph.add_edge(research_id, synthesizer_nodes[portfolio_manager_id])

        for manager_id in sorted(manager_assignments[portfolio_manager_id]):
            graph.add_edge(synthesizer_nodes[portfolio_manager_id], manager_id)
            graph.add_edge(manager_id, central_risk_nodes[portfolio_manager_id])

        graph.add_edge(central_risk_nodes[portfolio_manager_id], capital_allocator_nodes[portfolio_manager_id])
        graph.add_edge(capital_allocator_nodes[portfolio_manager_id], portfolio_manager_id)
        graph.add_edge(portfolio_manager_id, END)

    return graph


# Helper function to create the agent graph
def create_graph(
    graph_nodes: list,
    graph_edges: list,
    architecture_mode: str = FLAT_CURRENT_ARCHITECTURE,
) -> StateGraph:
    """Create the workflow based on the React Flow graph structure."""
    graph = StateGraph(AgentState)
    graph.add_node("start_node", start)

    if architecture_mode == MANAGER_SLEEVES_ARCHITECTURE:
        graph = _create_manager_sleeves_graph(graph, graph_nodes, graph_edges)
    else:
        graph = _create_flat_graph(graph, graph_nodes, graph_edges)

    # Set the entry point to the start node
    graph.set_entry_point("start_node")
    return graph


async def run_graph_async(
    graph,
    portfolio,
    tickers,
    start_date,
    end_date,
    model_name,
    model_provider,
    request=None,
    architecture_mode: str = FLAT_CURRENT_ARCHITECTURE,
):
    """Async wrapper for run_graph to work with asyncio."""
    # Use run_in_executor to run the synchronous function in a separate thread
    # so it doesn't block the event loop
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: run_graph(
            graph,
            portfolio,
            tickers,
            start_date,
            end_date,
            model_name,
            model_provider,
            request,
            architecture_mode,
        ),
    )
    return result


def run_graph(
    graph: StateGraph,
    portfolio: dict,
    tickers: list[str],
    start_date: str,
    end_date: str,
    model_name: str,
    model_provider: str,
    request=None,
    architecture_mode: str = FLAT_CURRENT_ARCHITECTURE,
) -> dict:
    """
    Run the graph with the given portfolio, tickers,
    start date, end date, show reasoning, model name,
    and model provider.
    """
    return graph.invoke(
        {
            "messages": [
                HumanMessage(
                    content="Make trading decisions based on the provided data.",
                )
            ],
            "data": {
                "tickers": tickers,
                "portfolio": portfolio,
                "start_date": start_date,
                "end_date": end_date,
                "analyst_signals": {},
            },
            "metadata": {
                "show_reasoning": False,
                "model_name": model_name,
                "model_provider": model_provider,
                "architecture_mode": architecture_mode,
                "request": request,  # Pass the request for agent-specific model access
            },
        },
    )


def parse_hedge_fund_response(response):
    """Parses a JSON string and returns a dictionary."""
    try:
        return json.loads(response)
    except json.JSONDecodeError as e:
        print(f"JSON decoding error: {e}\nResponse: {repr(response)}")
        return None
    except TypeError as e:
        print(f"Invalid response type (expected string, got {type(response).__name__}): {e}")
        return None
    except Exception as e:
        print(f"Unexpected error while parsing response: {e}\nResponse: {repr(response)}")
        return None
