import datetime
import logging
import os
import pandas as pd
import requests
from requests import RequestException
import time

logger = logging.getLogger(__name__)
NEWS_API_MAX_LIMIT = 10

from src.data.cache import get_cache
from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    FinancialMetricsResponse,
    Price,
    PriceResponse,
    LineItem,
    LineItemResponse,
    InsiderTrade,
    InsiderTradeResponse,
    CompanyFactsResponse,
)

# Global cache instance
_cache = get_cache()


def _build_error_response(url: str, status_code: int, message: str) -> requests.Response:
    """Create a synthetic response object for transport-level request failures."""
    response = requests.Response()
    response.status_code = status_code
    response.url = url
    response.reason = message
    response._content = message.encode("utf-8")
    return response


def _response_error_detail(response: requests.Response, max_length: int = 300) -> str:
    """Return a compact response detail suitable for logs."""
    detail = (response.text or response.reason or "").strip().replace("\n", " ")
    if len(detail) > max_length:
        detail = f"{detail[:max_length]}..."
    return detail


def _log_api_failure(resource: str, ticker: str, response: requests.Response) -> None:
    detail = _response_error_detail(response)
    if detail:
        logger.warning("Could not fetch %s for %s (HTTP %s): %s", resource, ticker, response.status_code, detail)
    else:
        logger.warning("Could not fetch %s for %s (HTTP %s)", resource, ticker, response.status_code)


def _rate_limit_backoff_delay(attempt: int) -> int:
    """Return the retry delay in seconds for a zero-based rate-limit retry attempt."""
    return 60 + (30 * attempt)


def _transport_backoff_delay(attempt: int) -> int:
    """Return the retry delay in seconds for a zero-based transport retry attempt."""
    return 5 * (attempt + 1)


def _make_api_request(
    url: str,
    headers: dict,
    method: str = "GET",
    json_data: dict = None,
    max_retries: int = 3,
    timeout: int = 30,
) -> requests.Response:
    """
    Make an API request with rate limiting handling and moderate backoff.
    
    Args:
        url: The URL to request
        headers: Headers to include in the request
        method: HTTP method (GET or POST)
        json_data: JSON data for POST requests
        max_retries: Maximum number of retries (default: 3)
        timeout: Request timeout in seconds (default: 30)
    
    Returns:
        requests.Response: The response object. Transport-level request failures
        are converted into a synthetic non-200 response so callers can degrade
        gracefully instead of raising.
    """
    for attempt in range(max_retries + 1):  # +1 for initial attempt
        try:
            if method.upper() == "POST":
                response = requests.post(url, headers=headers, json=json_data, timeout=timeout)
            else:
                response = requests.get(url, headers=headers, timeout=timeout)
        except RequestException as exc:
            if attempt < max_retries:
                delay = _transport_backoff_delay(attempt)
                print(
                    f"API Request Error: {exc}. "
                    f"Attempt {attempt + 1}/{max_retries + 1}. Waiting {delay}s before retrying..."
                )
                time.sleep(delay)
                continue

            logger.warning("API request failed after %s attempts: %s", max_retries + 1, exc)
            return _build_error_response(url, 503, str(exc))
        
        if response.status_code == 429 and attempt < max_retries:
            delay = _rate_limit_backoff_delay(attempt)
            print(f"Rate limited (429). Attempt {attempt + 1}/{max_retries + 1}. Waiting {delay}s before retrying...")
            time.sleep(delay)
            continue
        
        # Return the response (whether success, other errors, or final 429)
        return response


def get_prices(ticker: str, start_date: str, end_date: str, api_key: str = None) -> list[Price]:
    """Fetch price data from cache or API."""
    # Create a cache key that includes all parameters to ensure exact matches
    cache_key = f"{ticker}_{start_date}_{end_date}"
    
    # Check cache first - simple exact match
    if cached_data := _cache.get_prices(cache_key):
        return [Price(**price) for price in cached_data]

    # If not in cache, fetch from API
    headers = {"Accept": "application/json", "User-Agent": "ai-hedge-fund/1.0"}
    financial_api_key = api_key or os.environ.get("FINANCIAL_DATASETS_API_KEY")
    if financial_api_key:
        headers["X-API-KEY"] = financial_api_key

    url = f"https://api.financialdatasets.ai/prices/?ticker={ticker}&interval=day&interval_multiplier=1&start_date={start_date}&end_date={end_date}"
    response = _make_api_request(url, headers)
    if response.status_code != 200:
        _log_api_failure("prices", ticker, response)
        return []

    # Parse response with Pydantic model
    try:
        price_response = PriceResponse(**response.json())
        prices = price_response.prices
    except (ValueError, KeyError) as e:
        logger.warning("Failed to parse price data for %s: %s", ticker, e)
        return []

    if not prices:
        return []

    # Cache the results using the comprehensive cache key
    _cache.set_prices(cache_key, [p.model_dump() for p in prices])
    return prices


def get_financial_metrics(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    period: str = "ttm",
    limit: int = 10,
    api_key: str = None,
) -> list[FinancialMetrics]:
    """Fetch financial metrics from cache or API."""
    # Create a cache key that includes all parameters to ensure exact matches
    cache_key = f"{ticker}_{period}_{end_date}_{limit}"
    
    # Check cache first - simple exact match
    if cached_data := _cache.get_financial_metrics(cache_key):
        return [FinancialMetrics(**metric) for metric in cached_data]

    # If not in cache, fetch from API
    headers = {"Accept": "application/json", "User-Agent": "ai-hedge-fund/1.0"}
    financial_api_key = api_key or os.environ.get("FINANCIAL_DATASETS_API_KEY")
    if financial_api_key:
        headers["X-API-KEY"] = financial_api_key

    url = f"https://api.financialdatasets.ai/financial-metrics/?ticker={ticker}&period={period}&report_period_lte={end_date}&report_period_gte={start_date}"
    response = _make_api_request(url, headers)
    if response.status_code != 200:
        _log_api_failure("financial metrics", ticker, response)
        return []

    # Parse response with Pydantic model
    try:
        metrics_response = FinancialMetricsResponse(**response.json())
        financial_metrics = metrics_response.financial_metrics
    except (ValueError, KeyError) as e:
        logger.warning("Failed to parse financial metrics for %s: %s", ticker, e)
        return []

    if not financial_metrics:
        return []

    # Cache the results as dicts using the comprehensive cache key
    _cache.set_financial_metrics(cache_key, [m.model_dump() for m in financial_metrics])
    return financial_metrics


def search_line_items(
    ticker: str,
    line_items: list[str],
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
    api_key: str = None,
) -> list[LineItem]:
    """Fetch line items from API."""
    # If not in cache or insufficient data, fetch from API
    headers = {"Accept": "application/json", "User-Agent": "ai-hedge-fund/1.0"}
    financial_api_key = api_key or os.environ.get("FINANCIAL_DATASETS_API_KEY")
    if financial_api_key:
        headers["X-API-KEY"] = financial_api_key

    url = "https://api.financialdatasets.ai/financials/search/line-items"

    body = {
        "tickers": [ticker],
        "line_items": line_items,
        "end_date": end_date,
        "period": period,
        "limit": limit,
    }
    response = _make_api_request(url, headers, method="POST", json_data=body)
    if response.status_code != 200:
        _log_api_failure("line items", ticker, response)
        return []

    try:
        data = response.json()
        response_model = LineItemResponse(**data)
        search_results = response_model.search_results
    except (ValueError, KeyError) as e:
        logger.warning("Failed to parse line items for %s: %s", ticker, e)
        return []
    if not search_results:
        return []

    # Cache the results
    return search_results[:limit]


def get_insider_trades(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str = None,
) -> list[InsiderTrade]:
    """Fetch insider trades from cache or API."""
    # Create a cache key that includes all parameters to ensure exact matches
    cache_key = f"{ticker}_{start_date or 'none'}_{end_date}_{limit}"
    
    # Check cache first - simple exact match
    if cached_data := _cache.get_insider_trades(cache_key):
        return [InsiderTrade(**trade) for trade in cached_data]

    # If not in cache, fetch from API
    headers = {"Accept": "application/json", "User-Agent": "ai-hedge-fund/1.0"}
    financial_api_key = api_key or os.environ.get("FINANCIAL_DATASETS_API_KEY")
    if financial_api_key:
        headers["X-API-KEY"] = financial_api_key

    all_trades = []
    current_end_date = end_date

    while True:
        url = f"https://api.financialdatasets.ai/insider-trades/?ticker={ticker}&filing_date_lte={current_end_date}"
        if start_date:
            url += f"&filing_date_gte={start_date}"
        #url += f"&limit={limit}"

        response = _make_api_request(url, headers)
        if response.status_code != 200:
            _log_api_failure("insider trades", ticker, response)
            break

        try:
            data = response.json()
            response_model = InsiderTradeResponse(**data)
            insider_trades = response_model.insider_trades
        except (ValueError, KeyError) as e:
            logger.warning("Failed to parse insider trades for %s: %s", ticker, e)
            break

        if not insider_trades:
            break

        all_trades.extend(insider_trades)

        # Only continue pagination if we have a start_date and got a full page
        if not start_date or len(insider_trades) < limit:
            break

        # Update end_date to the oldest filing date from current batch for next iteration
        current_end_date = min(trade.filing_date for trade in insider_trades).split("T")[0]

        # If we've reached or passed the start_date, we can stop
        if start_date and current_end_date <= start_date:
            break

    if not all_trades:
        return []

    # Cache the results using the comprehensive cache key
    _cache.set_insider_trades(cache_key, [trade.model_dump() for trade in all_trades])
    return all_trades


def get_company_news(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str = None,
) -> list[CompanyNews]:
    """Fetch company news from cache or API."""
    requested_limit = max(0, limit)
    if requested_limit == 0:
        return []

    # Create a cache key that includes all parameters to ensure exact matches
    cache_key = f"{ticker}_{start_date or 'none'}_{end_date}_{requested_limit}"
    
    # Check cache first - simple exact match
    if cached_data := _cache.get_company_news(cache_key):
        return [CompanyNews(**news) for news in cached_data]

    # If not in cache, fetch from API
    headers = {"Accept": "application/json", "User-Agent": "ai-hedge-fund/1.0"}
    financial_api_key = api_key or os.environ.get("FINANCIAL_DATASETS_API_KEY")
    if financial_api_key:
        headers["X-API-KEY"] = financial_api_key

    all_news = []
    current_end_date = end_date
    seen_news = set()

    while len(all_news) < requested_limit:
        page_limit = min(NEWS_API_MAX_LIMIT, requested_limit - len(all_news))
        url = f"https://api.financialdatasets.ai/news/?ticker={ticker}&end_date={current_end_date}"
        if start_date:
            url += f"&start_date={start_date}"
        url += f"&limit={page_limit}"

        response = _make_api_request(url, headers)
        if response.status_code != 200:
            _log_api_failure("company news", ticker, response)
            break

        try:
            data = response.json()
            raw_news = data.get("news", [])
            if not isinstance(raw_news, list):
                logger.warning(
                    "Unexpected company news payload for %s: keys=%s",
                    ticker,
                    list(data.keys()),
                )
                break

            company_news = []
            skipped_items = 0
            for item in raw_news:
                try:
                    company_news.append(CompanyNews(**item))
                except Exception:
                    skipped_items += 1

            if skipped_items:
                logger.warning(
                    "Skipped %s malformed news items for %s (limit=%s, end_date=%s)",
                    skipped_items,
                    ticker,
                    page_limit,
                    current_end_date,
                )
        except Exception as e:
            logger.warning("Failed to parse company news for %s: %s", ticker, e)
            break

        if not company_news:
            break

        new_items = []
        for news in company_news:
            news_key = (news.url, news.title, news.date)
            if news_key in seen_news:
                continue
            seen_news.add(news_key)
            new_items.append(news)

        if not new_items:
            break

        all_news.extend(new_items)

        if len(company_news) < page_limit:
            break

        # Move before the oldest date from this page; the API treats end_date as inclusive.
        dated_news = [news.date for news in company_news if news.date]
        if not dated_news:
            break
        oldest_date = min(dated_news).split("T")[0]
        try:
            current_end_date = (
                datetime.datetime.strptime(oldest_date, "%Y-%m-%d") - datetime.timedelta(days=1)
            ).strftime("%Y-%m-%d")
        except ValueError:
            break

        # If we've reached or passed the start_date, we can stop
        if start_date and current_end_date <= start_date:
            break

    if not all_news:
        return []

    # Cache the results using the comprehensive cache key
    _cache.set_company_news(cache_key, [news.model_dump() for news in all_news])
    return all_news


def get_market_cap(
    ticker: str,
    end_date: str,
    api_key: str = None,
) -> float | None:
    """Fetch market cap from the API."""
    # Check if end_date is today
    '''
    if end_date == datetime.datetime.now().strftime("%Y-%m-%d"):
        # Get the market cap from company facts API
        headers = {"Accept": "application/json", "User-Agent": "ai-hedge-fund/1.0"}
        financial_api_key = api_key or os.environ.get("FINANCIAL_DATASETS_API_KEY")
        if financial_api_key:
            headers["X-API-KEY"] = financial_api_key

        url = f"https://api.financialdatasets.ai/company/facts/?ticker={ticker}"
        response = _make_api_request(url, headers)
        if response.status_code != 200:
            _log_api_failure("company facts", ticker, response)
            return None

        data = response.json()
        response_model = CompanyFactsResponse(**data)
        return response_model.company_facts.market_cap
'''
    financial_metrics = get_financial_metrics(ticker, end_date, api_key=api_key)
    if not financial_metrics:
        return None

    market_cap = financial_metrics[0].market_cap

    if not market_cap:
        return None

    return market_cap


def prices_to_df(prices: list[Price]) -> pd.DataFrame:
    """Convert prices to a DataFrame."""
    df = pd.DataFrame([p.model_dump() for p in prices])
    df["Date"] = pd.to_datetime(df["time"])
    df.set_index("Date", inplace=True)
    numeric_cols = ["open", "close", "high", "low", "volume"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.sort_index(inplace=True)
    return df


# Update the get_price_data function to use the new functions
def get_price_data(ticker: str, start_date: str, end_date: str, api_key: str = None) -> pd.DataFrame:
    prices = get_prices(ticker, start_date, end_date, api_key=api_key)
    return prices_to_df(prices)
