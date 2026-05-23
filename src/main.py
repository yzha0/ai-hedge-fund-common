import sys
from functools import partial

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph
from colorama import Fore, Style, init
import questionary
from src.agents.capital_allocator import capital_allocator_agent
from src.agents.central_risk import central_risk_agent
from src.agents.investors.manager_proposal import manager_proposal_agent
from src.agents.portfolio_manager import portfolio_management_agent
from src.agents.research_synthesizer import research_synthesizer_agent
from src.agents.risk_manager import risk_management_agent
from src.graph.state import AgentState
from src.utils.display import print_trading_output
from src.utils.analysts import ANALYST_ORDER, get_analyst_nodes
from src.utils.architecture import (
    FLAT_CURRENT_ARCHITECTURE,
    MANAGER_SLEEVES_ARCHITECTURE,
    get_selected_agent_groups,
)
from src.utils.progress import progress
from src.utils.visualize import save_graph_as_png
from src.cli.input import (
    parse_cli_inputs,
)

import argparse
from datetime import datetime
from dateutil.relativedelta import relativedelta
import json

# Load environment variables from .env file
load_dotenv()

init(autoreset=True)


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


##### Run the Hedge Fund #####
def run_hedge_fund(
    tickers: list[str],
    start_date: str,
    end_date: str,
    portfolio: dict,
    show_reasoning: bool = False,
    show_agent_graph: bool = True,
    selected_analysts: list[str] | None = None,
    model_name: str = "gpt-4.1",
    model_provider: str = "OpenAI",
    architecture_mode: str = FLAT_CURRENT_ARCHITECTURE,
):
    # Start progress tracking
    progress.start()

    try:
        # Build workflow (default to all analysts when none provided)
        workflow = create_workflow(
            selected_analysts if selected_analysts else None,
            architecture_mode=architecture_mode,
        )
        agent = workflow.compile()
         # Visualize graph if requested
        if show_agent_graph:
            agent.get_graph().draw_mermaid_png(output_file_path="agent_graph.png")

        final_state = agent.invoke(
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
                    "show_reasoning": show_reasoning,
                    "model_name": model_name,
                    "model_provider": model_provider,
                    "architecture_mode": architecture_mode,
                },
            },
        )

        return {
            "decisions": parse_hedge_fund_response(final_state["messages"][-1].content),
            "analyst_signals": final_state["data"]["analyst_signals"],
            "model_name": final_state["metadata"]["model_name"],
            "architecture_mode": architecture_mode,
            "current_prices": final_state["data"].get("current_prices", {}),
        }
    finally:
        # Stop progress tracking
        progress.stop()


def start(state: AgentState):
    """Initialize the workflow with the input message."""
    return state


def _create_flat_workflow(workflow: StateGraph, selected_analysts: list[str] | None):
    """Create the existing flat analyst -> risk -> portfolio manager flow."""
    analyst_nodes = get_analyst_nodes()
    if selected_analysts is None:
        selected_analysts = list(analyst_nodes.keys())

    for analyst_key in selected_analysts:
        node_name, node_func = analyst_nodes[analyst_key]
        workflow.add_node(node_name, node_func)
        workflow.add_edge("start_node", node_name)

    workflow.add_node("risk_management_agent", risk_management_agent)
    workflow.add_node("portfolio_manager", portfolio_management_agent)

    for analyst_key in selected_analysts:
        node_name = analyst_nodes[analyst_key][0]
        workflow.add_edge(node_name, "risk_management_agent")

    workflow.add_edge("risk_management_agent", "portfolio_manager")
    workflow.add_edge("portfolio_manager", END)


def _create_manager_sleeves_workflow(workflow: StateGraph, selected_analysts: list[str] | None):
    """Create the research -> investor managers -> central risk -> allocator flow."""
    analyst_nodes = get_analyst_nodes()
    research_keys, manager_keys = get_selected_agent_groups(selected_analysts)

    workflow.add_node("research_synthesizer_agent", research_synthesizer_agent)
    workflow.add_node("central_risk_agent", central_risk_agent)
    workflow.add_node("capital_allocator_agent", capital_allocator_agent)
    workflow.add_node("portfolio_manager", portfolio_management_agent)

    for research_key in research_keys:
        node_name, node_func = analyst_nodes[research_key]
        workflow.add_node(node_name, node_func)
        workflow.add_edge("start_node", node_name)
        workflow.add_edge(node_name, "research_synthesizer_agent")

    for manager_key in manager_keys:
        node_name = f"{manager_key}_agent"
        workflow.add_node(node_name, partial(manager_proposal_agent, agent_id=node_name))
        workflow.add_edge("research_synthesizer_agent", node_name)
        workflow.add_edge(node_name, "central_risk_agent")

    workflow.add_edge("central_risk_agent", "capital_allocator_agent")
    workflow.add_edge("capital_allocator_agent", "portfolio_manager")
    workflow.add_edge("portfolio_manager", END)


def create_workflow(selected_analysts=None, architecture_mode: str = FLAT_CURRENT_ARCHITECTURE):
    """Create the workflow with selected analysts."""
    workflow = StateGraph(AgentState)
    workflow.add_node("start_node", start)

    if architecture_mode == MANAGER_SLEEVES_ARCHITECTURE:
        _create_manager_sleeves_workflow(workflow, selected_analysts)
    else:
        _create_flat_workflow(workflow, selected_analysts)

    workflow.set_entry_point("start_node")
    return workflow


if __name__ == "__main__":
    inputs = parse_cli_inputs(
        description="Run the hedge fund trading system",
        require_tickers=True,
        default_months_back=None,
        include_graph_flag=True,
        include_reasoning_flag=True,
    )

    tickers = inputs.tickers
    selected_analysts = inputs.selected_analysts

    # Construct portfolio here
    portfolio = {
        "cash": inputs.initial_cash,
        "margin_requirement": inputs.margin_requirement,
        "margin_used": 0.0,
        "positions": {
            ticker: {
                "long": 0,
                "short": 0,
                "long_cost_basis": 0.0,
                "short_cost_basis": 0.0,
                "short_margin_used": 0.0,
            }
            for ticker in tickers
        },
        "realized_gains": {
            ticker: {
                "long": 0.0,
                "short": 0.0,
            }
            for ticker in tickers
        },
    }

    result = run_hedge_fund(
        tickers=tickers,
        start_date=inputs.start_date,
        end_date=inputs.end_date,
        portfolio=portfolio,
        show_reasoning=inputs.show_reasoning,
        show_agent_graph=inputs.show_agent_graph,
        selected_analysts=inputs.selected_analysts,
        model_name=inputs.model_name,
        model_provider=inputs.model_provider,
        architecture_mode=inputs.architecture_mode,
    )
    print_trading_output(result)
