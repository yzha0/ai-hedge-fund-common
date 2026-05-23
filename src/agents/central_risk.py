from __future__ import annotations

import json

from langchain_core.messages import HumanMessage
import pandas as pd

from src.agents.risk_manager import (
    calculate_correlation_multiplier,
    calculate_volatility_adjusted_limit,
    calculate_volatility_metrics,
)
from src.graph.state import AgentState, show_agent_reasoning
from src.tools.api import get_prices, prices_to_df
from src.utils.api_key import get_api_key_from_state
from src.utils.architecture import create_default_attribution_state
from src.utils.progress import progress


def central_risk_agent(state: AgentState, agent_id: str = "central_risk_agent"):
    """Firm-level risk review for manager proposals."""
    data = state["data"]
    portfolio = data["portfolio"]
    tickers = data["tickers"]
    api_key = get_api_key_from_state(state, "FINANCIAL_DATASETS_API_KEY")
    data.setdefault("attribution_state", create_default_attribution_state())

    manager_proposals = data.get("manager_proposals", {})
    research_summary = data.get("research_summary", {})

    current_prices: dict[str, float] = {}
    volatility_data: dict[str, dict] = {}
    returns_by_ticker: dict[str, pd.Series] = {}

    all_tickers = set(tickers) | set(portfolio.get("positions", {}).keys())
    for ticker in all_tickers:
        progress.update_status(agent_id, ticker, "Fetching prices for firm risk review")
        prices = get_prices(
            ticker=ticker,
            start_date=data["start_date"],
            end_date=data["end_date"],
            api_key=api_key,
        )
        if not prices:
            current_prices[ticker] = 0.0
            volatility_data[ticker] = {
                "daily_volatility": 0.05,
                "annualized_volatility": 0.05,
                "volatility_percentile": 100.0,
                "data_points": 0,
            }
            continue

        prices_df = prices_to_df(prices)
        if prices_df.empty or len(prices_df) < 2:
            current_prices[ticker] = 0.0
            volatility_data[ticker] = {
                "daily_volatility": 0.05,
                "annualized_volatility": 0.05,
                "volatility_percentile": 100.0,
                "data_points": len(prices_df),
            }
            continue

        current_prices[ticker] = float(prices_df["close"].iloc[-1])
        volatility_data[ticker] = calculate_volatility_metrics(prices_df)
        daily_returns = prices_df["close"].pct_change().dropna()
        if len(daily_returns) > 0:
            returns_by_ticker[ticker] = daily_returns

    correlation_matrix = None
    if len(returns_by_ticker) >= 2:
        try:
            returns_df = pd.DataFrame(returns_by_ticker).dropna(how="any")
            if returns_df.shape[1] >= 2 and returns_df.shape[0] >= 5:
                correlation_matrix = returns_df.corr()
        except Exception:
            correlation_matrix = None

    positions = portfolio.get("positions", {})
    total_portfolio_value = float(portfolio.get("cash", 0.0))
    current_net_by_ticker: dict[str, float] = {}
    gross_exposure_before = 0.0
    net_exposure_before = 0.0
    for ticker, position in positions.items():
        price = float(current_prices.get(ticker, 0.0))
        long_value = float(position.get("long", 0) or 0) * price
        short_value = float(position.get("short", 0) or 0) * price
        net_value = long_value - short_value
        total_portfolio_value += net_value
        current_net_by_ticker[ticker] = net_value
        gross_exposure_before += abs(net_value)
        net_exposure_before += net_value

    active_positions = {ticker for ticker, value in current_net_by_ticker.items() if abs(value) > 0.0}

    ticker_limits: dict[str, dict] = {}
    for ticker in tickers:
        price = float(current_prices.get(ticker, 0.0))
        vol_data = volatility_data.get(ticker, {})
        vol_limit_pct = calculate_volatility_adjusted_limit(
            float(vol_data.get("annualized_volatility", 0.25))
        )
        avg_corr = None
        corr_multiplier = 1.0
        if correlation_matrix is not None and ticker in correlation_matrix.columns:
            comparable = [t for t in active_positions if t in correlation_matrix.columns and t != ticker]
            if not comparable:
                comparable = [t for t in correlation_matrix.columns if t != ticker]
            if comparable:
                series = correlation_matrix.loc[ticker, comparable].dropna()
                if len(series) > 0:
                    avg_corr = float(series.mean())
                    corr_multiplier = calculate_correlation_multiplier(avg_corr)

        combined_limit_pct = vol_limit_pct * corr_multiplier
        max_abs_notional = max(0.0, total_portfolio_value * combined_limit_pct)
        max_abs_weight_pct = combined_limit_pct * 100.0
        ticker_limits[ticker] = {
            "current_price": price,
            "annualized_volatility": float(vol_data.get("annualized_volatility", 0.25)),
            "volatility_percentile": float(vol_data.get("volatility_percentile", 50.0)),
            "avg_correlation_with_active": avg_corr,
            "max_abs_notional": max_abs_notional,
            "max_abs_weight_pct": max_abs_weight_pct,
            "current_net_notional": current_net_by_ticker.get(ticker, 0.0),
        }

    proposal_review: dict[str, dict] = {}
    for pm_id, proposals in manager_proposals.items():
        pm_reviews: dict[str, dict] = {}
        for ticker in tickers:
            proposal = proposals.get(ticker, {})
            signal = str(proposal.get("signal", "neutral")).lower()
            desired_weight_pct = abs(float(proposal.get("desired_weight_pct", 0.0) or 0.0))
            conviction = float(proposal.get("conviction", 0.0) or 0.0)
            ticker_limit = ticker_limits.get(ticker, {})
            price = float(ticker_limit.get("current_price", 0.0))
            disagreement = float(research_summary.get(ticker, {}).get("disagreement", 0.0) or 0.0)

            reasons: list[str] = []
            if price <= 0.0:
                pm_reviews[ticker] = {
                    "status": "block",
                    "desired_weight_pct": desired_weight_pct,
                    "approved_weight_pct": 0.0,
                    "risk_adjustment": 0.0,
                    "max_weight_pct": float(ticker_limit.get("max_abs_weight_pct", 0.0)),
                    "reasons": ["Missing valid price data"],
                }
                continue

            risk_adjustment = 1.0
            avg_corr = ticker_limit.get("avg_correlation_with_active")
            if avg_corr is not None and avg_corr >= 0.8:
                reasons.append("High correlation with existing positions")
            if disagreement >= 0.6:
                reasons.append("High disagreement across research analysts")
            risk_adjustment *= max(0.5, 1.0 - disagreement * 0.5)

            annualized_vol = float(ticker_limit.get("annualized_volatility", 0.25))
            if annualized_vol > 0.5:
                reasons.append("High realized volatility")
            risk_adjustment *= max(0.5, min(1.0, 0.6 / max(annualized_vol, 0.6)))

            max_weight_pct = float(ticker_limit.get("max_abs_weight_pct", 0.0))
            approved_weight_pct = min(desired_weight_pct * risk_adjustment, max_weight_pct)

            if signal == "neutral" or conviction <= 0 or desired_weight_pct <= 0:
                status = "block"
                approved_weight_pct = 0.0
                reasons.append("No active risk budget for neutral or zero-conviction proposal")
            elif approved_weight_pct <= 0.05:
                status = "block"
                approved_weight_pct = 0.0
                reasons.append("Proposal too small after risk adjustments")
            elif approved_weight_pct + 1e-9 < desired_weight_pct:
                status = "haircut"
            else:
                status = "pass"

            pm_reviews[ticker] = {
                "status": status,
                "desired_weight_pct": desired_weight_pct,
                "approved_weight_pct": round(approved_weight_pct, 4),
                "risk_adjustment": round(risk_adjustment, 4),
                "max_weight_pct": round(max_weight_pct, 4),
                "reasons": reasons,
            }
        proposal_review[pm_id] = pm_reviews

    central_risk_output = {
        "portfolio_limits": {
            "equity": total_portfolio_value,
            "gross_exposure_before": gross_exposure_before,
            "net_exposure_before": net_exposure_before,
            "max_gross_exposure": total_portfolio_value * 1.5,
            "max_net_exposure": total_portfolio_value * 0.6,
            "min_cash_buffer": total_portfolio_value * 0.10,
        },
        "ticker_limits": ticker_limits,
        "proposal_review": proposal_review,
    }

    data["current_prices"] = current_prices
    data["central_risk_review"] = central_risk_output
    data.setdefault("workflow_outputs", {})[agent_id] = central_risk_output

    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(central_risk_output, "Central Risk")

    progress.update_status(agent_id, None, "Done")
    message = HumanMessage(content=json.dumps(central_risk_output), name=agent_id)
    return {"messages": [message], "data": data}
